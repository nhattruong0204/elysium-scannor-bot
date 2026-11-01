"""Control-center screens (TELEGRAM_UX §5). Handlers are thin: read DB → build a Screen."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from elysium_bot import __version__
from elysium_bot.db.enums import (
    AiConsentPolicy,
    AuditAction,
    ChatStatus,
    ImportStatus,
    JobStatus,
    ScheduleKind,
    StoredRole,
)
from elysium_bot.db.models import ImportErrorRecord, ImportRun, Job, Message, Schedule, TelegramChat
from elysium_bot.db.repositories import AuditLogRepository, ChatRoleRepository
from elysium_bot.ingestion import coverage
from elysium_bot.ops import status as status_service
from elysium_bot.scan.service import last_scan
from elysium_bot.telegram_ui import render as r
from elysium_bot.telegram_ui.auth import AuthService, Permission
from elysium_bot.telegram_ui.components import (
    PRESETS,
    RangeParseError,
    Window,
    local_to_utc,
    month_grid,
    paginate,
    parse_typed_range,
    preset_window,
    shift_month,
)
from elysium_bot.telegram_ui.flows import FlowStore
from elysium_bot.telegram_ui.render import Screen, esc
from elysium_bot.telegram_ui.router import UiContext, router
from elysium_bot.telegram_ui.scan_export_screens import export_done, scan_done
from elysium_bot.timeutil import format_local, to_local, utcnow

if TYPE_CHECKING:
    from elysium_bot.app import Services

CC_FLOW = "cc"
NO_ACCESS_TEXT = "You don't have access to any research group. Ask the owner to add you."
OWNER_NO_CHATS = (
    "No research group is active yet. Add me to your group (privacy mode disabled in @BotFather) "
    "and send /activate there."
)
COMMON_TIMEZONES = [
    "Asia/Ho_Chi_Minh",
    "Asia/Singapore",
    "UTC",
    "Europe/London",
    "America/New_York",
]
MODELS = {"s": ("claude-sonnet-5-5", "Sonnet 5.5"), "o": ("claude-opus-5-5", "Opus 5.5")}
EFFORTS = ("low", "medium", "high")
RETENTION_CHOICES = (0, 30, 90, 180, 365)
INTERVALS = (0, 60, 120, 180, 240)
MIN_MESSAGES = (10, 20, 50, 100)
PURPOSE_PERMISSION = {
    "scan": Permission.SEARCH,
    "export": Permission.EXPORT,
    "ai": Permission.RUN_AI,
}
PURPOSE_TITLE = {"scan": "Scan Messages", "export": "Export Raw Chat", "ai": "AI Analysis"}
AI_MODES = {
    "summary": ("📝 Conversation Summary", 5),
    "early": ("🚀 Elysium Early Plays", 5),
    "deep": ("🔬 Deep Project Research", 8),
    "changed": ("🔄 What's Changed?", 8),
    "ask": ("💬 Ask Research AI", 8),
}
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


# --- helpers ----------------------------------------------------------------------------------


def nav_row(ctx: UiContext, back: str | None = "menu") -> list[InlineKeyboardButton]:
    row = []
    if back and back != "menu":
        row.append(ctx.btn("‹ Back", "nav", back))
    row.append(ctx.btn("⌂ Menu", "menu"))
    return row


def pairs(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def check(flag: bool) -> str:
    return "☑" if flag else "☐"


def mark(label: str, selected: bool) -> str:
    return f"{label} ✓" if selected else label


def local_hm(dt: datetime | None, tz: str) -> str:
    return format_local(dt, tz, "%H:%M") if dt else "—"


def local_date(dt: datetime | None, tz: str) -> str:
    return format_local(dt, tz, "%Y-%m-%d") if dt else "—"


async def audit(ctx: UiContext, action: AuditAction, params: dict[str, Any]) -> None:
    await AuditLogRepository(ctx.session, ctx.scope).record(action, params)


def set_pending(ctx: UiContext, op: str, arg: str, prompt: str) -> None:
    ctx.flow.state["pending"] = {"op": op, "arg": arg, "prompt": prompt}


def take_input(ctx: UiContext) -> str | None:
    value = ctx.flow.state.pop("input", None)
    return str(value) if value is not None else None


def placeholder(
    ctx: UiContext, title: str, phase: int, detail: str = "", back: str | None = "menu"
) -> Screen:
    text = (
        f"<b>{esc(title)}</b>\n\n"
        + (detail + "\n\n" if detail else "")
        + f"This feature arrives in Phase {phase}. Nothing was run."
    )
    return Screen(text, [nav_row(ctx, back)])


# --- main menu --------------------------------------------------------------------------------


@router.route("menu")
async def main_menu(ctx: UiContext) -> Screen:
    ctx.flow.state.pop("pending", None)
    snap = await status_service.snapshot(ctx.session, ctx.services.settings, ctx.chat)
    tz = ctx.tz
    lines = [
        "<b>ELYSIUM RESEARCH INTELLIGENCE</b>",
        f"Group: <b>{esc(ctx.chat.title or ctx.chat.tg_chat_id)}</b>  ·  tz {esc(tz)}",
        "",
    ]
    if snap.messages_stored:
        lines.append(
            f"Messages stored: <b>{snap.messages_stored:,}</b>  ({local_date(snap.earliest_at, tz)} → now)"
        )
    else:
        lines.append("Messages stored: <b>0</b>")
    if snap.projects_tracked is not None:
        lines.append(f"Projects tracked: <b>{snap.projects_tracked:,}</b>")
    lines.append("")
    if snap.chat_status != ChatStatus.ACTIVE:
        lines.append(f"{r.DISABLED} Collection  {snap.chat_status.value}")
    elif snap.polling_fresh:
        last = f"last msg {local_hm(snap.latest_at, tz)}" if snap.latest_at else "no messages yet"
        lines.append(f"{r.OK} Collection  live · {last}")
    else:
        lines.append(f"{r.DEGRADED} Collection  bot heartbeat stale")
    gaps = (
        f" · {snap.suspected_gaps} possible gap{'s' if snap.suspected_gaps != 1 else ''}"
        if snap.suspected_gaps
        else ""
    )
    if snap.has_history_import:
        lines.append(f"{r.DEGRADED if gaps else r.OK} History     export imported{gaps}")
    else:
        lines.append(f"{r.DEGRADED} History     live only — no export imported{gaps}")
    if snap.index_failed:
        lines.append(f"{r.DEGRADED} Index       {snap.index_failed:,} failed")
    elif snap.index_pending:
        lines.append(f"{r.RUNNING} Index       {snap.index_pending:,} pending")
    else:
        lines.append(f"{r.OK} Index       up to date")
    ai_detail = (
        "key configured · analysis arrives in Phase 5" if snap.ai_configured else "not configured"
    )
    lines.append(f"{r.DISABLED} AI          {ai_detail}")

    candidates: list[tuple[str, str, str, Permission]] = [
        ("🔎 Scan Messages", "nav", "scan", Permission.SEARCH),
        ("🧠 AI Analysis", "nav", "ai", Permission.RUN_AI),
        ("📄 Export Raw Chat", "nav", "export", Permission.EXPORT),
        ("📊 Research Projects", "nav", "projects", Permission.VIEW_REPORTS),
        ("🗂 Chat History", "nav", "history", Permission.VIEW_STATUS),
        ("⏰ Scheduled Scan", "nav", "schedule", Permission.MANAGE_SCHEDULES),
        ("⚙️ Settings", "nav", "settings", Permission.MANAGE_SETTINGS),
        ("ℹ️ Status", "nav", "status", Permission.VIEW_STATUS),
    ]
    buttons = [ctx.btn(label, op, arg) for label, op, arg, perm in candidates if ctx.can(perm)]
    rows = []
    chats = await ctx.auth.accessible_chats(ctx.session, ctx.scope.actor_tg_user_id)
    if len(chats) > 1:
        rows.append([ctx.btn(f"Group: {ctx.chat.title or ctx.chat.tg_chat_id} ▾", "nav", "chats")])
    rows += pairs(buttons)
    rows.append([ctx.btn("❔ Help", "nav", "help")])
    return Screen("\n".join(lines), rows)


async def chats_screen(ctx: UiContext) -> Screen:
    chats = await ctx.auth.accessible_chats(ctx.session, ctx.scope.actor_tg_user_id)
    rows = [
        [
            ctx.btn(
                mark(
                    f"{c.chat.title or c.chat.tg_chat_id} ({c.role.value})",
                    c.chat.id == ctx.chat.id,
                ),
                "chat",
                c.chat.id,
            )
        ]
        for c in chats
    ]
    rows.append(nav_row(ctx))
    return Screen("<b>Select group</b>", rows)


@router.route("chat")
async def switch_chat(ctx: UiContext) -> Screen:
    target = int(ctx.arg or 0)
    scope = await ctx.auth.scope(ctx.session, target, ctx.scope.actor_tg_user_id)
    chat = await ctx.session.get(TelegramChat, target)
    if scope is None or chat is None:
        screen = await main_menu(ctx)
        screen.alert = "You don't have access to that group."
        return screen
    ctx.flow.chat_id = target
    ctx.flow.state = {k: v for k, v in ctx.flow.state.items() if k == "msg"}
    from elysium_bot.db.models import ChatSettings

    settings_row = await ctx.session.get(ChatSettings, target) or ChatSettings(
        chat_id=target, timezone=ctx.services.settings.default_timezone
    )
    new_ctx = UiContext(
        ctx.services,
        ctx.session,
        ctx.flows,
        ctx.auth,
        ctx.flow,
        scope,
        chat,
        settings_row,
        None,
        ctx.bot,
    )
    return await main_menu(new_ctx)


# --- navigation -------------------------------------------------------------------------------


@router.route("nav")
async def navigate(ctx: UiContext) -> Screen:
    target = ctx.arg or "menu"
    ctx.flow.state.pop("pending", None)
    screens: dict[str, tuple[Any, Permission]] = {
        "menu": (main_menu, Permission.VIEW_STATUS),
        "chats": (chats_screen, Permission.VIEW_STATUS),
        "status": (status_screen, Permission.VIEW_STATUS),
        "history": (history_screen, Permission.VIEW_STATUS),
        "settings": (settings_screen, Permission.MANAGE_SETTINGS),
        "access": (access_screen, Permission.MANAGE_ROLES),
        "schedule": (schedule_screen, Permission.MANAGE_SCHEDULES),
        "projects": (projects_screen, Permission.VIEW_REPORTS),
        "help": (help_screen, Permission.VIEW_STATUS),
        "scan": (lambda c: start_range(c, "scan"), Permission.SEARCH),
        "export": (lambda c: start_range(c, "export"), Permission.EXPORT),
        "ai": (lambda c: start_range(c, "ai"), Permission.RUN_AI),
        "ask": (ask_screen, Permission.ASK),
    }
    fn, permission = screens.get(target, (main_menu, Permission.VIEW_STATUS))
    if not ctx.can(permission):
        screen = await main_menu(ctx)
        screen.alert = "Your role doesn't allow this."
        return screen
    result: Screen = await fn(ctx)
    return result


@router.route("noop")
async def noop(ctx: UiContext) -> Screen:
    return await current_screen(ctx)


async def current_screen(ctx: UiContext) -> Screen:
    if "rng" in ctx.flow.state:
        return await range_screen(ctx)
    return await main_menu(ctx)


async def help_screen(ctx: UiContext) -> Screen:
    text = (
        "<b>Help</b>\n\n"
        "Everything is reachable from the buttons; commands are shortcuts:\n"
        "/menu · /scan · /export · /analyze · /projects · /ask · /history · /schedule · /settings · /status\n\n"
        "In the group: /activate (owner) starts collection, members tap /consent to opt in to AI analysis.\n"
        "Import earlier history by sending a Telegram Desktop export (result.json) here.\n\n"
        "Research notes are derived from group messages. Not financial advice."
    )
    return Screen(text, [nav_row(ctx)])


async def projects_screen(ctx: UiContext) -> Screen:
    return placeholder(
        ctx, "Research Projects", 6, "Project tracking builds on the research memory layer."
    )


async def ask_screen(ctx: UiContext) -> Screen:
    return placeholder(ctx, "Ask Research AI", 8)


# --- status -----------------------------------------------------------------------------------


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    seconds = int((utcnow() - dt).total_seconds())
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


async def status_screen(ctx: UiContext) -> Screen:
    snap = await status_service.snapshot(ctx.session, ctx.services.settings, ctx.chat)
    revision = await status_service.schema_revision(ctx.session)
    size = await status_service.database_size_bytes(ctx.session)
    tz = ctx.tz
    lines = [
        "<b>Status</b>",
        f"Group: {esc(ctx.chat.title)} · {ctx.chat.status.value}",
        "",
        f"{r.OK if snap.polling_fresh else r.DEGRADED} Polling      heartbeat {_ago(snap.polling_beat_at)}",
        f"{r.OK} Last message  {format_local(snap.latest_at, tz, '%Y-%m-%d %H:%M') if snap.latest_at else '—'}",
        f"{r.RUNNING if snap.index_pending else r.OK} Index backlog {snap.index_pending:,}"
        + (f" · {snap.index_failed:,} failed" if snap.index_failed else ""),
        "",
        "<b>Jobs</b>",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    if snap.running_jobs:
        for job in snap.running_jobs:
            progress = f" · {esc(job.progress)}" if job.progress else ""
            lines.append(
                f"{r.RUNNING} #{job.id} {job.kind} since {local_hm(job.started_at, tz)}{progress}"
            )
            if ctx.can(Permission.CANCEL_JOBS):
                rows.append([ctx.btn(f"✖ Cancel #{job.id}", "job", job.id)])
    else:
        lines.append("No running jobs.")
    for job in snap.recent_failures[:3]:
        lines.append(f"{r.FAILED} #{job.id} {job.kind} failed: {esc((job.error or '')[:80])}")
    lines += [
        "",
        "<b>System</b>",
        f"Version {esc(__version__)} · schema {esc(revision or '?')}"
        + (f" · DB {size / 1e6:,.1f} MB" if size else ""),
        f"{r.OK if snap.ai_configured else r.DISABLED} Anthropic API {'key set' if snap.ai_configured else 'not configured'}",
        f"{r.OK if snap.mtproto_enabled else r.DISABLED} MTProto backfill {'enabled' if snap.mtproto_enabled else 'off'}",
        f"{r.OK if snap.onchain_configured else r.DISABLED} On-chain providers {'configured' if snap.onchain_configured else 'not configured'}",
    ]
    rows.append([ctx.btn("↻ Refresh", "nav", "status")])
    rows.append(nav_row(ctx))
    return Screen("\n".join(lines), rows)


@router.route("job", Permission.CANCEL_JOBS)
async def cancel_job(ctx: UiContext) -> Screen:
    job = await ctx.session.get(Job, int(ctx.arg or 0))
    if job is None or job.chat_id != ctx.chat.id or job.status != JobStatus.RUNNING:
        screen = await status_screen(ctx)
        screen.alert = "That job is no longer running."
        return screen
    job.status = JobStatus.CANCELLED
    job.finished_at = utcnow()
    job.lease_expires_at = None
    job.error = f"cancelled by {ctx.scope.actor_tg_user_id}"
    await ctx.session.flush()
    screen = await status_screen(ctx)
    screen.alert = f"Job #{job.id} cancelled."
    return screen


# --- chat history -----------------------------------------------------------------------------


async def history_screen(ctx: UiContext) -> Screen:
    tz = ctx.tz
    st = await coverage.statement(ctx.session, ctx.chat, tz=tz)
    lines = ["<b>Chat History</b>", "", esc(st.human_text), ""]
    if st.sources_in_window:
        lines.append(
            "Stored by source: "
            + " · ".join(f"{esc(k)} {v:,}" for k, v in sorted(st.sources_in_window.items()))
        )
    for gap in st.gaps_in_window[:5]:
        span = " → ".join(
            format_local(d, tz, "%Y-%m-%d %H:%M") for d in (gap.start_at, gap.end_at) if d
        )
        lines.append(f"{r.CAVEAT} {esc(gap.note or gap.kind)} ({span})")
    if len(st.gaps_in_window) > 5:
        lines.append(f"… and {len(st.gaps_in_window) - 5} more")
    runs = list(
        (
            await ctx.session.execute(
                select(ImportRun)
                .where(ImportRun.chat_id == ctx.chat.id)
                .order_by(ImportRun.id.desc())
                .limit(5)
            )
        ).scalars()
    )
    rows: list[list[InlineKeyboardButton]] = []
    lines += ["", "<b>Recent imports</b>"]
    if not runs:
        lines.append("None yet.")
    for run in runs:
        icon = {
            ImportStatus.SUCCEEDED: r.OK,
            ImportStatus.PARTIAL: r.DEGRADED,
            ImportStatus.RUNNING: r.RUNNING,
        }.get(run.status, r.FAILED)
        lines.append(
            f"{icon} #{run.id} {run.source.value} · {local_date(run.started_at, tz)} · new {run.inserted:,} · failed {run.failed:,}"
        )
        if run.failed and ctx.can(Permission.IMPORT):
            rows.append([ctx.btn(f"Errors in #{run.id}", "imp", run.id)])
    if ctx.can(Permission.IMPORT):
        lines += [
            "",
            "To import earlier history, send a Telegram Desktop export (result.json, ≤ 20 MB) in this chat, "
            "or run <code>elysium-bot import-export</code> on the server for larger files.",
        ]
    if ctx.can(Permission.SEARCH):
        rows.append([ctx.btn("🔎 Search messages", "srch", "ask")])
    rows.append([ctx.btn("↻ Refresh", "nav", "history")])
    rows.append(nav_row(ctx))
    return Screen("\n".join(lines), rows)


@router.route("imp", Permission.IMPORT)
async def import_errors(ctx: UiContext) -> Screen:
    run_id = int(ctx.arg or ctx.flow.state.get("imp_run", 0))
    run = await ctx.session.get(ImportRun, run_id)
    if run is None or run.chat_id != ctx.chat.id:
        return await history_screen(ctx)
    ctx.flow.state["imp_run"] = run_id
    ctx.flow.state["page_of"] = "imp"
    errors = list(
        (
            await ctx.session.execute(
                select(ImportErrorRecord)
                .where(ImportErrorRecord.import_run_id == run_id)
                .order_by(ImportErrorRecord.id)
            )
        ).scalars()
    )
    page = paginate(errors, int(ctx.flow.state.get("page", 0)))
    lines = [f"<b>Import #{run_id} — errors</b> ({page.total:,})", ""]
    for err in page.items:
        lines.append(
            f"• <code>{esc(err.error_code)}</code> {esc(err.record_ref or '')} {esc((err.detail or '')[:120])}"
        )
    rows = [page_row(ctx, page.page, page.pages)] if page.pages > 1 else []
    rows.append(nav_row(ctx, "history"))
    return Screen("\n".join(lines), rows)


def page_row(ctx: UiContext, page: int, pages: int) -> list[InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(ctx.btn("‹ Prev", "pg", page - 1))
    row.append(ctx.btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(ctx.btn("Next ›", "pg", page + 1))
    return row


@router.route("pg")
async def change_page(ctx: UiContext) -> Screen:
    ctx.flow.state["page"] = max(0, int(ctx.arg or 0))
    if ctx.flow.state.get("page_of") == "imp" and ctx.can(Permission.IMPORT):
        ctx.arg = None
        return await import_errors(ctx)
    return await main_menu(ctx)


# --- settings ---------------------------------------------------------------------------------


async def settings_screen(ctx: UiContext) -> Screen:
    s = ctx.chat_settings
    owner = ctx.can(Permission.MANAGE_OWNER_SETTINGS)
    retention = f"{s.retention_days} days" if s.retention_days else "keep forever"
    lines = [
        "<b>Settings</b>",
        f"Group: {esc(ctx.chat.title)}",
        "",
        f"Timezone: {esc(s.timezone)}",
        f"Routine AI: {esc(s.routine_model)} · {esc(s.routine_effort)}",
        f"Deep research AI: {esc(s.deep_model)} · {esc(s.deep_effort)}",
        f"Pseudonymize exports by default: {check(s.pseudonymize_exports)}",
        f"Send display names to AI: {check(s.send_display_names_to_ai)}",
        f"AI consent policy: {esc(s.ai_consent_policy.value)}"
        + (f"  {r.CAVEAT}" if s.ai_consent_policy == AiConsentPolicy.NOTICE_ONLY else ""),
        f"Retention: {retention}",
    ]
    rows = [
        [ctx.btn("🕒 Timezone", "set", "tz"), ctx.btn("🧠 AI defaults", "set", "ai")],
        [ctx.btn(f"{check(s.pseudonymize_exports)} Pseudonymize exports", "set", "pseudo")],
        [ctx.btn(f"{check(s.send_display_names_to_ai)} Display names to AI", "set", "names")],
    ]
    if ctx.can(Permission.MANAGE_ROLES):
        rows.append([ctx.btn("👥 Manage access", "nav", "access")])
    if owner:
        rows.append(
            [ctx.btn("🛡 AI consent policy", "set", "consent"), ctx.btn("🗑 Retention", "set", "ret")]
        )
        if ctx.chat.status == ChatStatus.ACTIVE:
            rows.append([ctx.btn("⛔ Deactivate group", "set", "deact")])
    rows.append(nav_row(ctx))
    return Screen("\n".join(lines), rows)


async def _change(ctx: UiContext, field: str, value: Any, *, owner_only: bool = False) -> None:
    if owner_only and not ctx.can(Permission.MANAGE_OWNER_SETTINGS):
        raise PermissionError(field)
    old = getattr(ctx.chat_settings, field)
    if old == value:
        return
    setattr(ctx.chat_settings, field, value)
    ctx.chat_settings.updated_by_tg_user_id = ctx.scope.actor_tg_user_id
    await audit(
        ctx, AuditAction.SETTINGS_CHANGE, {"field": field, "old": str(old), "new": str(value)}
    )


@router.route("set", Permission.MANAGE_SETTINGS)
async def settings_action(ctx: UiContext) -> Screen:
    arg = ctx.arg or ""
    s = ctx.chat_settings
    try:
        if arg == "tz":
            return timezone_screen(ctx)
        if arg.startswith("tz") and arg[2:].isdigit():
            await _change(ctx, "timezone", COMMON_TIMEZONES[int(arg[2:]) % len(COMMON_TIMEZONES)])
        elif arg == "tztype":
            set_pending(ctx, "set", "tzin", "Send an IANA timezone name, e.g. Asia/Bangkok")
            return Screen(
                "Send an IANA timezone name in this chat, e.g. <code>Asia/Bangkok</code>.",
                [nav_row(ctx, "settings")],
            )
        elif arg == "tzin":
            value = (take_input(ctx) or "").strip()
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                screen = timezone_screen(ctx)
                screen.alert = f"Unknown timezone: {value}"
                return screen
            ctx.flow.state.pop("pending", None)
            await _change(ctx, "timezone", value)
        elif arg == "ai":
            return ai_defaults_screen(ctx)
        elif arg[:6] in ("rmodel", "dmodel") and arg[-1] in MODELS:
            await _change(
                ctx, "routine_model" if arg.startswith("r") else "deep_model", MODELS[arg[-1]][0]
            )
            return ai_defaults_screen(ctx)
        elif arg[:4] in ("reff", "deff") and arg[5:] in EFFORTS:
            await _change(ctx, "routine_effort" if arg.startswith("r") else "deep_effort", arg[5:])
            return ai_defaults_screen(ctx)
        elif arg == "pseudo":
            await _change(ctx, "pseudonymize_exports", not s.pseudonymize_exports)
        elif arg == "names":
            await _change(ctx, "send_display_names_to_ai", not s.send_display_names_to_ai)
        elif arg == "consent":
            return consent_policy_screen(ctx)
        elif arg == "cons_strict":
            await _change(ctx, "ai_consent_policy", AiConsentPolicy.STRICT, owner_only=True)
        elif arg == "cons_notice":
            if not ctx.can(Permission.MANAGE_OWNER_SETTINGS):
                raise PermissionError(arg)
            return confirm_screen(
                ctx,
                "cons_notice",
                f"{r.CAVEAT} <b>Switch AI consent policy to notice-only?</b>\n\n"
                "All members' messages may then be sent to the AI, including members who never consented. "
                "Telegram's terms require explicit per-user consent for AI use (docs/SECURITY.md §3). "
                "This is your decision and it will be audited.",
            )
        elif arg == "ret":
            return retention_screen(ctx)
        elif arg.startswith("ret_") and arg[4:].isdigit():
            days = int(arg[4:])
            await _change(ctx, "retention_days", days or None, owner_only=True)
        elif arg == "deact":
            if not ctx.can(Permission.DEACTIVATE_CHAT):
                raise PermissionError(arg)
            ctx.flow.state["confirm"] = {
                "action": "deact",
                "title": ctx.chat.title or str(ctx.chat.tg_chat_id),
            }
            set_pending(ctx, "cfm", "typed", "Type the group title to confirm")
            return Screen(
                f"{r.CAVEAT} <b>Deactivate “{esc(ctx.chat.title)}”?</b>\n\n"
                "Collection stops; stored data is kept. To confirm, type the exact group title in this chat.",
                [[ctx.btn("✖ Cancel", "cfm", "no")]],
            )
    except PermissionError:
        screen = await settings_screen(ctx)
        screen.alert = "Only the owner can change this."
        return screen
    return await settings_screen(ctx)


def timezone_screen(ctx: UiContext) -> Screen:
    rows = [
        [ctx.btn(mark(tz, tz == ctx.chat_settings.timezone), "set", f"tz{i}")]
        for i, tz in enumerate(COMMON_TIMEZONES)
    ]
    rows.append([ctx.btn("⌨️ Type a timezone", "set", "tztype")])
    rows.append(nav_row(ctx, "settings"))
    return Screen(f"<b>Timezone</b>\nCurrent: {esc(ctx.chat_settings.timezone)}", rows)


def ai_defaults_screen(ctx: UiContext) -> Screen:
    s = ctx.chat_settings
    rows = [
        [
            ctx.btn(mark(f"Routine: {label}", s.routine_model == model), "set", f"rmodel_{k}")
            for k, (model, label) in MODELS.items()
        ],
        [ctx.btn(mark(e, s.routine_effort == e), "set", f"reff_{e}") for e in EFFORTS],
        [
            ctx.btn(mark(f"Deep: {label}", s.deep_model == model), "set", f"dmodel_{k}")
            for k, (model, label) in MODELS.items()
        ],
        [ctx.btn(mark(e, s.deep_effort == e), "set", f"deff_{e}") for e in EFFORTS],
        nav_row(ctx, "settings"),
    ]
    text = (
        "<b>AI defaults</b> (used from Phase 5)\n"
        "Routine analysis defaults to Sonnet 5.5 / medium; deep research to Opus 5.5 / high.\n\n"
        f"Routine: {esc(s.routine_model)} · {esc(s.routine_effort)}\nDeep: {esc(s.deep_model)} · {esc(s.deep_effort)}"
    )
    return Screen(text, rows)


def consent_policy_screen(ctx: UiContext) -> Screen:
    current = ctx.chat_settings.ai_consent_policy
    text = (
        "<b>AI consent policy</b>\n\n"
        "<b>strict</b> (default): only messages from members who tapped /consent are sent to the AI.\n"
        "<b>notice_only</b>: every message may be sent; not recommended (see docs/SECURITY.md §3)."
    )
    rows = [
        [ctx.btn(mark("strict", current == AiConsentPolicy.STRICT), "set", "cons_strict")],
        [
            ctx.btn(
                mark("notice_only", current == AiConsentPolicy.NOTICE_ONLY), "set", "cons_notice"
            )
        ],
        nav_row(ctx, "settings"),
    ]
    return Screen(text, rows)


def retention_screen(ctx: UiContext) -> Screen:
    current = ctx.chat_settings.retention_days or 0
    rows = pairs(
        [
            ctx.btn(mark(f"{d} days" if d else "Keep forever", current == d), "set", f"ret_{d}")
            for d in RETENTION_CHOICES
        ]
    )
    rows.append(nav_row(ctx, "settings"))
    return Screen(
        "<b>Retention</b>\nMessages older than this are purged (the purge job ships in Phase 9).",
        rows,
    )


def confirm_screen(ctx: UiContext, action: str, text: str) -> Screen:
    ctx.flow.state["confirm"] = {"action": action}
    return Screen(text, [[ctx.btn("✅ Confirm", "cfm", "yes"), ctx.btn("✖ Cancel", "cfm", "no")]])


@router.route("cfm")
async def confirm_action(ctx: UiContext) -> Screen:
    pending = ctx.flow.state.pop("confirm", None)
    arg = ctx.arg
    if arg == "typed":
        typed = (take_input(ctx) or "").strip()
        if pending and typed != pending.get("title"):
            ctx.flow.state["confirm"] = pending
            return Screen(
                "The title didn't match. Type the exact group title, or cancel.",
                [[ctx.btn("✖ Cancel", "cfm", "no")]],
                alert="Title didn't match.",
            )
        ctx.flow.state.pop("pending", None)
        arg = "yes"
    else:
        ctx.flow.state.pop("pending", None)
    if not pending or arg != "yes":
        screen = (
            await settings_screen(ctx)
            if ctx.can(Permission.MANAGE_SETTINGS)
            else await main_menu(ctx)
        )
        screen.alert = "Cancelled."
        return screen
    action = pending.get("action")
    if action == "cons_notice" and ctx.can(Permission.MANAGE_OWNER_SETTINGS):
        await _change(ctx, "ai_consent_policy", AiConsentPolicy.NOTICE_ONLY, owner_only=True)
        return await settings_screen(ctx)
    if action == "deact" and ctx.can(Permission.DEACTIVATE_CHAT):
        ctx.chat.status = ChatStatus.DEACTIVATED
        await audit(ctx, AuditAction.DEACTIVATE, {"via": "control_center"})
        screen = await main_menu(ctx)
        screen.alert = "Group deactivated. Stored data is kept."
        return screen
    screen = await main_menu(ctx)
    screen.alert = "Your role doesn't allow this."
    return screen


# --- access -----------------------------------------------------------------------------------


async def access_screen(ctx: UiContext) -> Screen:
    roles = await ChatRoleRepository(ctx.session, ctx.scope).list()
    owners = ctx.services.settings.owner_user_ids
    lines = [
        "<b>Manage access</b>",
        f"Owners (from server config): {', '.join(str(o) for o in owners) or '—'}",
        "",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    if not roles:
        lines.append("No other users have access yet.")
    for role in roles:
        lines.append(f"• {role.tg_user_id} — {role.role.value}")
        if role.role != StoredRole.ADMIN or ctx.can(Permission.GRANT_ADMIN):
            rows.append(
                [
                    ctx.btn(
                        f"✖ Remove {role.tg_user_id} ({role.role.value})",
                        "acc",
                        f"rm_{role.tg_user_id}",
                    )
                ]
            )
    rows.append([ctx.btn("➕ Add user", "acc", "add")])
    rows.append(nav_row(ctx, "settings"))
    return Screen("\n".join(lines), rows)


@router.route("acc", Permission.MANAGE_ROLES)
async def access_action(ctx: UiContext) -> Screen:
    arg = ctx.arg or ""
    repo = ChatRoleRepository(ctx.session, ctx.scope)
    if arg == "add":
        set_pending(
            ctx,
            "acc",
            "picked",
            "Forward a message from the user, or send their numeric Telegram ID",
        )
        return Screen(
            "<b>Add user</b>\nForward any message from the person here, or send their numeric Telegram user ID.",
            [nav_row(ctx, "access")],
        )
    if arg == "picked":
        raw = (take_input(ctx) or "").strip()
        if not raw.lstrip("-").isdigit() or int(raw) <= 0:
            return Screen(
                "That isn't a user ID. Forward a message from the person, or send their numeric ID.",
                [nav_row(ctx, "access")],
                alert="Not a user ID.",
            )
        ctx.flow.state.pop("pending", None)
        ctx.flow.state["grant_target"] = int(raw)
        roles = [StoredRole.ANALYST, StoredRole.VIEWER] + (
            [StoredRole.ADMIN] if ctx.can(Permission.GRANT_ADMIN) else []
        )
        return Screen(
            f"<b>Grant access to {int(raw)}</b>\nChoose a role:",
            [
                [ctx.btn(role.value, "acc", f"r_{role.value}") for role in roles],
                nav_row(ctx, "access"),
            ],
        )
    if arg.startswith("r_"):
        target = ctx.flow.state.pop("grant_target", None)
        try:
            role = StoredRole(arg[2:])
        except ValueError:
            role = None
        if target is None or role is None:
            return await access_screen(ctx)
        if role == StoredRole.ADMIN and not ctx.can(Permission.GRANT_ADMIN):
            screen = await access_screen(ctx)
            screen.alert = "Only the owner can grant admin."
            return screen
        if ctx.services.settings.is_owner(int(target)):
            screen = await access_screen(ctx)
            screen.alert = "That user is already an owner."
            return screen
        existing = await repo.get(int(target))
        if existing == StoredRole.ADMIN and not ctx.can(Permission.GRANT_ADMIN):
            screen = await access_screen(ctx)
            screen.alert = "Only the owner can change an admin's role."
            return screen
        await repo.grant(int(target), role)
        await audit(
            ctx,
            AuditAction.ROLE_CHANGE,
            {"tg_user_id": int(target), "role": role.value, "via": "ui"},
        )
        screen = await access_screen(ctx)
        screen.alert = f"Granted {role.value} to {target}."
        return screen
    if arg.startswith("rm_") and arg[3:].isdigit():
        target = int(arg[3:])
        existing = await repo.get(target)
        if existing == StoredRole.ADMIN and not ctx.can(Permission.GRANT_ADMIN):
            screen = await access_screen(ctx)
            screen.alert = "Only the owner can remove an admin."
            return screen
        if await repo.revoke(target):
            await audit(
                ctx, AuditAction.ROLE_CHANGE, {"tg_user_id": target, "role": None, "via": "ui"}
            )
        screen = await access_screen(ctx)
        screen.alert = f"Removed {target}."
        return screen
    return await access_screen(ctx)


# --- schedules --------------------------------------------------------------------------------


def _interval_label(minutes: int) -> str:
    return "Off" if not minutes else f"{minutes // 60}h"


async def _schedule_row(ctx: UiContext, kind: ScheduleKind) -> Schedule:
    row = (
        await ctx.session.execute(
            select(Schedule).where(Schedule.chat_id == ctx.chat.id, Schedule.kind == kind)
        )
    ).scalar_one_or_none()
    if row is None:
        row = Schedule(chat_id=ctx.chat.id, kind=kind, enabled=False, params={})
        ctx.session.add(row)
        await ctx.session.flush()
    return row


async def schedule_screen(ctx: UiContext) -> Screen:
    s = ctx.chat_settings
    col = s.collection_interval_min or 0
    ai = s.summary_interval_min or 0
    text = (
        "<b>Scheduled Scan</b>\n"
        "Live collection is always on; automatic collection only matters for MTProto gap-fill.\n"
        f"Mode {esc(s.summary_mode)} · min {s.summary_min_messages} new messages · destination {esc(s.summary_destination)}\n\n"
        "Schedules are saved now; collection runs from Phase 4 and AI summaries from Phase 5."
    )
    rows = [
        [ctx.btn("Collection:", "noop")]
        + [ctx.btn(mark(_interval_label(m), col == m), "sch", f"col_{m}") for m in INTERVALS],
        [ctx.btn("AI summary:", "noop")]
        + [ctx.btn(mark(_interval_label(m), ai == m), "sch", f"ai_{m}") for m in INTERVALS],
        [
            ctx.btn(
                mark("Summary", s.summary_mode == "conversation_summary"), "sch", "mode_summary"
            ),
            ctx.btn(mark("Early Plays", s.summary_mode == "early_plays"), "sch", "mode_early"),
        ],
        [
            ctx.btn(mark(str(n), s.summary_min_messages == n), "sch", f"min_{n}")
            for n in MIN_MESSAGES
        ],
        [
            ctx.btn(mark("Group", s.summary_destination == "group"), "sch", "dest_group"),
            ctx.btn(mark("My DM", s.summary_destination == "owner_dm"), "sch", "dest_dm"),
        ],
        nav_row(ctx),
    ]
    return Screen(text, rows)


@router.route("sch", Permission.MANAGE_SCHEDULES)
async def schedule_action(ctx: UiContext) -> Screen:
    arg = ctx.arg or ""
    kind_field = {
        "col": (ScheduleKind.COLLECTION, "collection_interval_min"),
        "ai": (ScheduleKind.AI_SUMMARY, "summary_interval_min"),
    }
    prefix, _, value = arg.partition("_")
    if prefix in kind_field and value.isdigit() and int(value) in INTERVALS:
        kind, field = kind_field[prefix]
        minutes = int(value)
        await _change(ctx, field, minutes or None)
        row = await _schedule_row(ctx, kind)
        row.interval_min = minutes or None
        row.enabled = bool(minutes)
        row.next_run_at = utcnow() + timedelta(minutes=minutes) if minutes else None
        if ctx.services.schedules is not None:
            await ctx.session.flush()
            ctx.services.schedules.register(row)
    elif prefix == "mode" and value in ("summary", "early"):
        await _change(
            ctx, "summary_mode", "conversation_summary" if value == "summary" else "early_plays"
        )
    elif prefix == "min" and value.isdigit() and int(value) in MIN_MESSAGES:
        await _change(ctx, "summary_min_messages", int(value))
    elif prefix == "dest" and value in ("group", "dm"):
        await _change(ctx, "summary_destination", "group" if value == "group" else "owner_dm")
    if prefix in ("mode", "min", "dest"):
        row = await _schedule_row(ctx, ScheduleKind.AI_SUMMARY)
        row.params = {
            "mode": ctx.chat_settings.summary_mode,
            "min_messages": ctx.chat_settings.summary_min_messages,
            "destination": ctx.chat_settings.summary_destination,
        }
    return await schedule_screen(ctx)


# --- range picker -----------------------------------------------------------------------------


async def start_range(ctx: UiContext, purpose: str) -> Screen:
    ctx.flow.state["rng"] = {"purpose": purpose, "stage": "menu"}
    return await range_screen(ctx)


def _rng(ctx: UiContext) -> dict[str, Any]:
    state: dict[str, Any] = ctx.flow.state.setdefault("rng", {"purpose": "scan", "stage": "menu"})
    return state


async def range_screen(ctx: UiContext) -> Screen:
    st = _rng(ctx)
    stage = st.get("stage", "menu")
    title = PURPOSE_TITLE.get(st["purpose"], "Select range")
    if stage == "menu":
        rows = [
            [ctx.btn(PRESETS[k][0], "rng", f"p{k}") for k in ("1h", "2h", "4h", "12h")],
            [
                ctx.btn("24h", "rng", "p24h"),
                ctx.btn("7 days", "rng", "p7d"),
                ctx.btn("Since last scan", "rng", "since"),
            ],
            [ctx.btn("Entire stored history", "rng", "all")],
            [ctx.btn("📅 Custom range", "rng", "cal"), ctx.btn("⌨️ Type range", "rng", "type")],
            nav_row(ctx),
        ]
        return Screen(
            f"<b>Select time range</b> — for: {esc(title)}\nTimezone: {esc(ctx.tz)}", rows
        )
    if stage in ("start_day", "end_day"):
        return await calendar_screen(ctx, st)
    if stage in ("start_hour", "end_hour"):
        which = "start" if stage == "start_hour" else "end"
        day = st["sd" if which == "start" else "ed"]
        rows = [
            [ctx.btn(f"{h:02d}", "rng", f"h{h}") for h in range(row * 6, row * 6 + 6)]
            for row in range(4)
        ]
        rows.append([ctx.btn("‹ Change day", "rng", "cal" if which == "start" else "endcal")])
        return Screen(
            f"<b>{which.capitalize()} time</b> on {esc(day)} — hour ({esc(ctx.tz)})", rows
        )
    if stage in ("start_min", "end_min"):
        which = "start" if stage == "start_min" else "end"
        rows = [[ctx.btn(f":{m:02d}", "rng", f"m{m}") for m in (0, 15, 30, 45)]]
        return Screen(f"<b>{which.capitalize()} time</b> — minutes", rows)
    if stage == "end_choice":
        start = datetime.fromisoformat(st["start"])
        rows = [
            [ctx.btn("End = now", "rng", "endnow"), ctx.btn("📅 Pick end", "rng", "endcal")],
            [ctx.btn("✏️ Change", "rng", "chg")],
        ]
        return Screen(
            f"<b>Range start</b>: {esc(format_local(start, ctx.tz))}\nChoose the end:", rows
        )
    if stage == "confirm":
        return await range_confirm_screen(ctx, Window.from_state(st["window"]))
    if stage == "done":
        return await range_done(ctx, Window.from_state(st["window"]))
    st["stage"] = "menu"
    return await range_screen(ctx)


async def _covered_days(ctx: UiContext, year: int, month: int) -> set[date]:
    zone = ZoneInfo(ctx.tz)
    days = set()
    first = date(year, month, 1)
    ny, nm = shift_month(year, month, 1)
    current = first
    while current < date(ny, nm, 1):
        start = datetime(current.year, current.month, current.day, tzinfo=zone).astimezone(UTC)
        hit = (
            await ctx.session.execute(
                select(Message.id)
                .where(
                    Message.chat_id == ctx.chat.id,
                    Message.sent_at >= start,
                    Message.sent_at < start + timedelta(days=1),
                )
                .limit(1)
            )
        ).first()
        if hit is not None:
            days.add(current)
        current += timedelta(days=1)
    return days


async def calendar_screen(ctx: UiContext, st: dict[str, Any]) -> Screen:
    year, month = int(st["y"]), int(st["m"])
    grid = month_grid(year, month, await _covered_days(ctx, year, month))
    rows = [
        [
            ctx.btn("‹", "rng", "mprev"),
            ctx.btn(date(year, month, 1).strftime("%b %Y"), "noop"),
            ctx.btn("›", "rng", "mnext"),
        ]
    ]
    rows.append([ctx.btn(d, "noop") for d in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")])
    for week in grid:
        rows.append(
            [
                ctx.btn(cell.label, "rng", f"d{cell.day:%Y%m%d}")
                if cell.day
                else ctx.btn(" ", "noop")
                for cell in week
            ]
        )
    rows.append([ctx.btn("✏️ Back to presets", "rng", "chg")])
    which = "start" if st["stage"] == "start_day" else "end"
    return Screen(
        f"<b>Pick the {which} day</b>\nDays marked · have no stored messages (still selectable).",
        rows,
    )


@router.route("rng")
async def range_action(ctx: UiContext) -> Screen:
    st = _rng(ctx)
    purpose = st.get("purpose", "scan")
    if not ctx.can(PURPOSE_PERMISSION.get(purpose, Permission.VIEW_STATUS)):
        screen = await main_menu(ctx)
        screen.alert = "Your role doesn't allow this."
        return screen
    arg = ctx.arg or ""
    tz = ctx.tz
    now_local = to_local(utcnow(), tz)
    alert = None
    if arg.startswith("p") and arg[1:] in PRESETS:
        st.update(stage="confirm", window=preset_window(arg[1:]).to_state())
    elif arg == "since":
        checkpoint = await last_scan(ctx.session, ctx.chat.id, ctx.scope.actor_tg_user_id)
        if checkpoint is None or checkpoint.last_scan_end >= utcnow():
            alert = "No previous scan yet — pick another range."
        else:
            st.update(
                stage="confirm",
                window=Window(checkpoint.last_scan_end, utcnow(), "Since last scan").to_state(),
            )
    elif arg == "all":
        cov = await coverage.statement(ctx.session, ctx.chat, tz=tz)
        if cov.earliest_stored_at is None:
            alert = "No messages are stored yet."
        else:
            st.update(
                stage="confirm",
                window=Window(cov.earliest_stored_at, utcnow(), "Entire stored history").to_state(),
            )
    elif arg in ("cal", "endcal"):
        st.update(
            stage="start_day" if arg == "cal" else "end_day", y=now_local.year, m=now_local.month
        )
    elif arg in ("mprev", "mnext"):
        st["y"], st["m"] = shift_month(
            int(st.get("y", now_local.year)),
            int(st.get("m", now_local.month)),
            -1 if arg == "mprev" else 1,
        )
    elif arg.startswith("d") and arg[1:].isdigit() and len(arg) == 9:
        day = datetime.strptime(arg[1:], "%Y%m%d").date().isoformat()
        if st.get("stage") == "start_day":
            st.update(stage="start_hour", sd=day)
        else:
            st.update(stage="end_hour", ed=day)
    elif arg.startswith("h") and arg[1:].isdigit() and 0 <= int(arg[1:]) <= 23:
        if st.get("stage") == "start_hour":
            st.update(stage="start_min", sh=int(arg[1:]))
        else:
            st.update(stage="end_min", eh=int(arg[1:]))
    elif arg.startswith("m") and arg[1:].isdigit() and int(arg[1:]) in (0, 15, 30, 45):
        minute = int(arg[1:])
        if st.get("stage") == "start_min":
            st.update(
                stage="end_choice",
                start=local_to_utc(
                    date.fromisoformat(st["sd"]), int(st["sh"]), minute, tz
                ).isoformat(),
            )
        else:
            start = datetime.fromisoformat(st["start"])
            end = local_to_utc(date.fromisoformat(st["ed"]), int(st["eh"]), minute, tz)
            if end <= start:
                alert = "The end must be after the start."
                st["stage"] = "end_day"
            else:
                st.update(stage="confirm", window=Window(start, end, "Custom").to_state())
    elif arg == "endnow" and "start" in st:
        start = datetime.fromisoformat(st["start"])
        if start >= utcnow():
            alert = "The start is in the future."
        else:
            st.update(stage="confirm", window=Window(start, utcnow(), "Custom").to_state())
    elif arg == "type":
        set_pending(ctx, "rng", "typed", "Type a range")
        return Screen(
            "<b>Type a range</b> in this chat, in "
            f"{esc(tz)} time. Examples:\n<code>2026-11-01 10:00 → 2026-11-01 14:00</code>\n"
            "<code>yesterday</code> · <code>today</code> · <code>last 3h</code> · <code>2026-10-30</code>",
            [[ctx.btn("✏️ Back to presets", "rng", "chg")]],
        )
    elif arg == "typed":
        try:
            window = parse_typed_range(take_input(ctx) or "", tz)
        except RangeParseError as exc:
            return Screen(
                f"Couldn't read that range: {esc(exc)}\nTry again, e.g. <code>2026-11-01 10:00 → 2026-11-01 14:00</code>",
                [[ctx.btn("✏️ Back to presets", "rng", "chg")]],
                alert=f"Couldn't read that range: {exc}",
            )
        ctx.flow.state.pop("pending", None)
        st.update(stage="confirm", window=window.to_state())
    elif arg == "chg":
        ctx.flow.state.pop("pending", None)
        st.clear()
        st.update(purpose=purpose, stage="menu")
    elif arg == "ok" and "window" in st:
        st["stage"] = "done"
    elif arg.startswith("mode_") and st.get("stage") == "done":
        st["mode"] = arg[5:]
    screen = await range_screen(ctx)
    screen.alert = alert or screen.alert
    return screen


async def range_confirm_screen(ctx: UiContext, window: Window) -> Screen:
    tz = ctx.tz
    cov = await coverage.statement(ctx.session, ctx.chat, window.start, window.end, tz=tz)
    lines = [
        f"<b>Range</b> — {esc(window.label)}",
        f"From  {esc(format_local(window.start, tz))}  ({esc(tz)})",
        f"To    {esc(format_local(window.end, tz))}",
        f"      = {window.start:%H:%M}–{window.end:%H:%M} UTC"
        if window.end - window.start < timedelta(days=1)
        else f"      = {window.start:%Y-%m-%d %H:%M} → {window.end:%Y-%m-%d %H:%M} UTC",
        f"Stored messages in range: {cov.messages_in_window:,}",
    ]
    if not cov.complete_for_window:
        lines.append(esc(cov.human_text))
    rows = [
        [
            ctx.btn("✅ Continue", "rng", "ok"),
            ctx.btn("✏️ Change", "rng", "chg"),
            ctx.btn("⌂ Menu", "menu"),
        ]
    ]
    return Screen("\n".join(lines), rows)


async def range_done(ctx: UiContext, window: Window) -> Screen:
    st = _rng(ctx)
    purpose = st.get("purpose", "scan")
    tz = ctx.tz
    span = f"{format_local(window.start, tz)} → {format_local(window.end, tz)} ({tz})"
    if purpose == "ai" and "mode" not in st:
        rows = [[ctx.btn(label, "rng", f"mode_{key}")] for key, (label, _) in AI_MODES.items()]
        rows.append([ctx.btn("✏️ Change range", "rng", "chg"), ctx.btn("⌂ Menu", "menu")])
        return Screen(f"<b>AI Analysis</b>\nWindow: {esc(span)}\n\nChoose the analysis mode:", rows)
    if purpose == "scan":
        return await scan_done(ctx, window)
    if purpose == "export":
        return await export_done(ctx, window)
    label, phase = AI_MODES.get(st["mode"], ("AI Analysis", 5))
    screen = placeholder(ctx, label, phase, f"Window: {esc(span)}")
    screen.rows.insert(0, [ctx.btn("✏️ Change range", "rng", "chg")])
    return screen


# --- entry points -----------------------------------------------------------------------------

COMMAND_SCREENS = {
    "menu": "menu",
    "start": "menu",
    "scan": "scan",
    "export": "export",
    "analyze": "ai",
    "projects": "projects",
    "ask": "ask",
    "history": "history",
    "schedule": "schedule",
    "settings": "settings",
    "status": "status",
    "help": "help",
}


async def send_screen(bot: Any, chat_id: int, screen: Screen) -> Any:
    return await bot.send_message(
        chat_id,
        screen.text,
        parse_mode=ParseMode.HTML,
        reply_markup=screen.markup(),
        link_preview_options=NO_PREVIEW,
    )


async def open_control_center(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /menu, /start [cc_<chat id>] and the other DM shortcuts."""
    message, user = update.effective_message, update.effective_user
    if message is None or user is None or message.chat.type != "private":
        return
    services: Services = context.bot_data["services"]
    command = (message.text or "/menu").split()[0].lstrip("/").split("@")[0].lower()
    target_screen = COMMAND_SCREENS.get(command, "menu")
    wanted_chat: int | None = None
    if context.args and context.args[0].startswith("cc_") and context.args[0][3:].isdigit():
        wanted_chat = int(context.args[0][3:])

    auth = AuthService(services.settings)
    async with services.sessions() as session:
        chats = await auth.accessible_chats(session, user.id)
    if not chats:
        await message.reply_text(
            OWNER_NO_CHATS if services.settings.is_owner(user.id) else NO_ACCESS_TEXT
        )
        return
    chat_id = next((c.chat.id for c in chats if c.chat.id == wanted_chat), chats[0].chat.id)
    flows = FlowStore(services.sessions)
    flow = await flows.create(user.id, chat_id, CC_FLOW)
    screen = await router.render(services, flows, flow, "nav", target_screen, user.id, context.bot)
    if screen is None:
        await message.reply_text(NO_ACCESS_TEXT)
        return
    sent = await send_screen(context.bot, message.chat.id, screen)
    flow.state["msg"] = [message.chat.id, getattr(sent, "message_id", None)]
    await flows.save(flow)


async def on_private_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Typed replies for pending prompts (typed range, timezone, user to grant, typed confirmation)."""
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return
    services: Services = context.bot_data["services"]
    flows = FlowStore(services.sessions)
    flow = await flows.find_pending_input(user.id)
    if flow is None:
        return
    pending = flow.state["pending"]
    origin = message.forward_origin
    if origin is not None:
        sender = getattr(origin, "sender_user", None)
        if sender is None:
            await message.reply_text(
                "That person's privacy settings hide their account in forwards. Send their numeric user ID instead."
            )
            return
        flow.state["input"] = str(sender.id)
    else:
        flow.state["input"] = message.text or ""
    screen = await router.render(
        services, flows, flow, pending["op"], pending["arg"], user.id, context.bot
    )
    if screen is None:
        flow.state.pop("pending", None)
        await flows.save(flow)
        await message.reply_text(NO_ACCESS_TEXT)
        return
    await flows.save(flow)
    target = flow.state.get("msg") or [message.chat.id, None]
    try:
        if target[1] is None:
            raise TelegramError("no control-center message")
        await context.bot.edit_message_text(
            screen.text,
            chat_id=target[0],
            message_id=target[1],
            parse_mode=ParseMode.HTML,
            reply_markup=screen.markup(),
            link_preview_options=NO_PREVIEW,
        )
    except TelegramError:
        sent = await send_screen(context.bot, message.chat.id, screen)
        flow.state["msg"] = [message.chat.id, getattr(sent, "message_id", None)]
        await flows.save(flow)
    if screen.alert:
        await message.reply_text(screen.alert)


async def group_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """In groups, /menu only offers a DM deep link — never chat data (ADR-012)."""
    message, chat = update.effective_message, update.effective_chat
    if message is None or chat is None:
        return
    services: Services = context.bot_data["services"]
    from elysium_bot.db.access import get_chat_by_tg_id

    async with services.sessions() as session:
        registered = await get_chat_by_tg_id(session, chat.id)
    start = f"cc_{registered.id}" if registered else "menu"
    url = f"https://t.me/{context.bot.username}?start={start}"
    await message.reply_text(
        "The research control center opens in a private chat with me.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open control center", url=url)]]),
    )


__all__ = ["group_menu", "on_private_input", "open_control_center", "router"]
