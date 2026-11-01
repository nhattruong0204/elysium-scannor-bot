"""Composition root: builds services and the python-telegram-bot Application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    MenuButtonCommands,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from elysium_bot.config import Settings
from elysium_bot.db.models import Heartbeat
from elysium_bot.db.session import create_engine, create_session_factory
from elysium_bot.ingestion import coverage, indexer
from elysium_bot.ingestion.bot_updates import on_chat_member, on_group_message, on_my_chat_member
from elysium_bot.logging import get_logger
from elysium_bot.ops.health import POLLING, beat
from elysium_bot.scheduler.leases import JobService
from elysium_bot.telegram_ui.flows import FlowStore
from elysium_bot.telegram_ui.group import (
    CONSENT_PREFIX,
    activate,
    consent_callback,
    consent_command,
    deactivate,
)
from elysium_bot.telegram_ui.imports import on_document
from elysium_bot.telegram_ui.screens import (
    COMMAND_SCREENS,
    group_menu,
    on_private_input,
    open_control_center,
    router,
)
from elysium_bot.timeutil import utcnow

log = get_logger(__name__)

App = Application[Any, Any, Any, Any, Any, Any]

# Persisting updates queued during a restart matters (HISTORY_INGESTION §3.1).
ALLOWED_UPDATES = [
    Update.MESSAGE,
    Update.EDITED_MESSAGE,
    Update.MY_CHAT_MEMBER,
    Update.CHAT_MEMBER,
    Update.CALLBACK_QUERY,
]
LEASE_RECOVERY_INTERVAL = timedelta(seconds=60)
INDEXER_INTERVAL = timedelta(seconds=2)
FLOW_SWEEP_INTERVAL = timedelta(hours=1)
PRIVATE_COMMANDS = [
    ("menu", "Open the control center"),
    ("scan", "Scan messages"),
    ("export", "Export raw chat"),
    ("analyze", "AI analysis"),
    ("projects", "Research projects"),
    ("ask", "Ask research AI"),
    ("history", "Chat history & coverage"),
    ("schedule", "Scheduled scans"),
    ("settings", "Settings"),
    ("status", "Status"),
    ("help", "Help"),
]
GROUP_COMMANDS = [
    ("menu", "Open the control center (in DM)"),
    ("activate", "Owner: start collecting this group"),
    ("consent", "Choose whether your messages may be used for AI analysis"),
    ("help", "What this bot does"),
]
# Telegram keeps unfetched Bot API updates for at most 24 hours.
UPDATE_RETENTION = timedelta(hours=24)


class ConfigurationError(RuntimeError):
    pass


@dataclass
class Services:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    jobs: JobService


def build_services(settings: Settings) -> Services:
    engine = create_engine(settings.database_url)
    sessions = create_session_factory(engine)
    return Services(settings, engine, sessions, JobService(sessions))


def build_application(services: Services) -> App:
    token = services.settings.telegram_bot_token
    if token is None or not token.get_secret_value():
        raise ConfigurationError("ELYSIUM_TELEGRAM_BOT_TOKEN is not set")

    app: App = (
        Application.builder()
        .token(token.get_secret_value())
        .rate_limiter(AIORateLimiter())
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["services"] = services
    app.bot_data["settings"] = services.settings
    # Group -1 runs first for every update: persist before any command handling.
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
            on_group_message,
        ),
        group=-1,
    )
    app.add_handler(
        CommandHandler(list(COMMAND_SCREENS), open_control_center, filters=filters.ChatType.PRIVATE)
    )
    app.add_handler(CommandHandler("menu", group_menu, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("activate", activate, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("deactivate", deactivate, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("consent", consent_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CallbackQueryHandler(consent_callback, pattern=f"^{CONSENT_PREFIX}"))
    app.add_handler(CallbackQueryHandler(router.dispatch, pattern=r"^v1:"))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Document.ALL, on_document))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ((filters.TEXT & ~filters.COMMAND) | filters.FORWARDED),
            on_private_input,
        )
    )
    return app


def _services(app: App) -> Services:
    services: Services = app.bot_data["services"]
    return services


async def _post_init(app: App) -> None:
    services = _services(app)
    recovered = await services.jobs.recover_expired()
    if recovered:
        log.warning("startup.recovered_jobs", count=len(recovered))
    gap_alert = await _detect_downtime(services)
    await beat(services.sessions, POLLING, {"phase": "startup"})

    job_queue = app.job_queue
    if job_queue is None:  # pragma: no cover - job-queue extra is a hard dependency
        raise ConfigurationError("python-telegram-bot[job-queue] is required")
    job_queue.run_repeating(
        _heartbeat_job,
        interval=services.settings.heartbeat_interval_s,
        first=services.settings.heartbeat_interval_s,
        name="heartbeat",
    )
    if gap_alert:
        job_queue.run_once(_notify_owners_job, when=0, data=gap_alert, name="downtime_alert")
    job_queue.run_repeating(
        _flow_sweep_job, interval=FLOW_SWEEP_INTERVAL, first=FLOW_SWEEP_INTERVAL, name="flow_sweep"
    )
    await _register_commands(app)
    job_queue.run_repeating(
        _index_job, interval=INDEXER_INTERVAL, first=INDEXER_INTERVAL, name="indexer"
    )
    job_queue.run_repeating(
        _lease_recovery_job,
        interval=LEASE_RECOVERY_INTERVAL,
        first=LEASE_RECOVERY_INTERVAL,
        name="lease_recovery",
    )
    log.info("startup.ready", owners=len(services.settings.owner_user_ids))


async def _post_shutdown(app: App) -> None:
    await _services(app).engine.dispose()
    log.info("shutdown.complete")


async def _heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    updater_running = bool(app.updater and app.updater.running)
    if not updater_running:
        # Let the heartbeat go stale so the container health check reports the outage.
        log.warning("heartbeat.skipped", reason="updater_not_running")
        return
    await beat(_services(app).sessions, POLLING, {"updater_running": True})


async def _detect_downtime(services: Services) -> str | None:
    """Record a suspected gap per active chat if the bot was down > 24 h (updates were lost)."""
    async with services.sessions() as session:
        previous = await session.get(Heartbeat, POLLING)
        now = utcnow()
        chats = await coverage.detect_downtime_gaps(
            session, previous.beat_at if previous else None, now, UPDATE_RETENTION
        )
        await session.commit()
    if not chats or previous is None:
        return None
    hours = int((now - previous.beat_at).total_seconds() // 3600)
    names = ", ".join(c.title or str(c.tg_chat_id) for c in chats)
    log.warning("startup.downtime_gap", hours=hours, chats=len(chats))
    return (
        f"⚠️ The bot was offline for ~{hours} h. Telegram keeps updates for 24 h at most, "
        f"so messages in that window may be missing for: {names}.\n"
        "Fix: import a Telegram Desktop export covering the gap "
        "(or run MTProto backfill if enabled)."
    )


async def _notify_owners_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    text = str(context.job.data) if context.job else ""
    services = _services(context.application)
    for owner in services.settings.owner_user_ids:
        try:
            await context.bot.send_message(owner, text)
        except Exception:
            log.warning("alert.delivery_failed", owner=owner)


async def _register_commands(app: App) -> None:
    """Persistent command menus (TELEGRAM_UX §4). Failures are logged, never fatal."""
    try:
        await app.bot.set_my_commands(
            [BotCommand(c, d) for c, d in PRIVATE_COMMANDS], scope=BotCommandScopeAllPrivateChats()
        )
        await app.bot.set_my_commands(
            [BotCommand(c, d) for c, d in GROUP_COMMANDS], scope=BotCommandScopeAllGroupChats()
        )
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramError:
        log.warning("startup.commands_not_set")


async def _flow_sweep_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await FlowStore(_services(context.application).sessions).sweep()


async def _index_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await indexer.drain(_services(context.application).sessions)


async def _lease_recovery_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _services(context.application).jobs.recover_expired()


def run(settings: Settings) -> None:
    if not settings.owner_user_ids:
        log.warning(
            "startup.no_owners",
            detail="ELYSIUM_OWNER_USER_IDS is empty; every privileged action will be refused",
        )
    services = build_services(settings)
    app = build_application(services)
    log.info("startup.polling", allowed_updates=ALLOWED_UPDATES)
    app.run_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False)
