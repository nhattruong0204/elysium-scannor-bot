"""Composition root: builds services and the python-telegram-bot Application."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram import Update
from telegram.ext import AIORateLimiter, Application, CommandHandler, ContextTypes, filters

from elysium_bot.config import Settings
from elysium_bot.db.session import create_engine, create_session_factory
from elysium_bot.logging import get_logger
from elysium_bot.ops.health import POLLING, beat
from elysium_bot.scheduler.leases import JobService
from elysium_bot.telegram_ui.start import start_command

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
    app.add_handler(CommandHandler("start", start_command, filters=filters.ChatType.PRIVATE))
    return app


def _services(app: App) -> Services:
    services: Services = app.bot_data["services"]
    return services


async def _post_init(app: App) -> None:
    services = _services(app)
    recovered = await services.jobs.recover_expired()
    if recovered:
        log.warning("startup.recovered_jobs", count=len(recovered))
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
