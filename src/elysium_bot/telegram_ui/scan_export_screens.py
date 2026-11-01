"""Phase 4 screens: scan results, raw export flow, source messages and message search. No AI."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from telegram.constants import ParseMode

from elysium_bot.db.models import ChatMember, Message, TelegramUser
from elysium_bot.exports.service import (
    FORMATS,
    ExportCancelled,
    ExportRefused,
    ExportRequest,
    count_rows,
    export_chat,
    recent_exports,
)
from elysium_bot.ingestion import coverage
from elysium_bot.logging import get_logger
from elysium_bot.scan.service import ScanBusy, ScanResult, run_scan, sender_label
from elysium_bot.search.index import MARK_CLOSE, MARK_OPEN, search_messages
from elysium_bot.telegram_ui import render as r
from elysium_bot.telegram_ui.auth import Permission
from elysium_bot.telegram_ui.components import ProgressMessage, Window, paginate
from elysium_bot.telegram_ui.render import Screen, esc, link, message_url
from elysium_bot.telegram_ui.router import UiContext, router
from elysium_bot.timeutil import format_local

if TYPE_CHECKING:
    from elysium_bot.app import Services
    from elysium_bot.db.scope import ChatScope

log = get_logger(__name__)

PER_PAGE = 8
FORMAT_LABELS = {"json": "JSON", "csv": "CSV", "txt": "TXT", "md": "Markdown"}
BYTES_PER_ROW = {"json": 520, "csv": 380, "txt": 180, "md": 200}
BACKGROUND: set[asyncio.Task[None]] = set()  # strong refs so export tasks aren't GC'd


def _span(window: Window, tz: str) -> str:
    return f"{format_local(window.start, tz)} → {format_local(window.end, tz)} ({tz})"


def _snippet(text_value: str) -> str:
    """Escape a search snippet and turn match markers into bold."""
    return esc(text_value.replace("\n", " ")).replace(MARK_OPEN, "<b>").replace(MARK_CLOSE, "</b>")


def _window(ctx: UiContext) -> Window:
    return Window.from_state(ctx.flow.state["rng"]["window"])


# --- scan ---------------------------------------------------------------------------------------


async def scan_done(ctx: UiContext, window: Window) -> Screen:
    try:
        result = await run_scan(
            ctx.services.sessions, ctx.scope, window, tz=ctx.tz, jobs=ctx.services.jobs
        )
    except ScanBusy as exc:
        return Screen(
            f"{r.CAVEAT} {esc(exc)}. Try again in a moment.",
            [[ctx.btn("⌂ Menu", "menu")]],
            alert=str(exc),
        )
    return scan_screen(ctx, result)


def scan_screen(ctx: UiContext, res: ScanResult) -> Screen:
    tz = ctx.tz
    lines = [
        f"<b>Scan — {esc(res.window.label)}</b>",
        esc(_span(res.window, tz)),
        f"Messages <b>{res.messages:,}</b> · service {res.service_messages:,} · active members {res.active_members:,}",
        f"Replies {res.replies:,} · threads {res.threads:,} · newly indexed {res.indexed_now:,}",
    ]
    if res.top_senders:
        lines += ["", "<b>Most active</b>"] + [
            f"• {esc(name)} — {n:,}" for name, n in res.top_senders
        ]
    if res.contracts:
        lines += ["", "<b>Contract strings</b> <i>(extracted, not verified)</i>"]
        for c in res.contracts[:8]:
            flag = (
                f" {r.CAVEAT} checksum mismatch" if c.checksum_state == "invalid_checksum" else ""
            )
            url = message_url(
                ctx.chat.tg_chat_id, c.first_tg_message_id, username=ctx.chat.username
            )
            lines.append(f"• <code>{esc(c.address)}</code> — {c.mentions}× {link(url, '🔗')}{flag}")
        if len(res.contracts) > 8:
            lines.append(f"… and {len(res.contracts) - 8} more")
    if res.tickers:
        note = " (newest 200k messages)" if res.ticker_sample_limited else ""
        lines += [
            "",
            f"<b>Tickers</b>{note}: " + " · ".join(f"${esc(t)} {n}" for t, n in res.tickers),
        ]
    if res.top_domains:
        lines += [
            "",
            "<b>Top domains</b>: " + " · ".join(f"{esc(d)} {n}" for d, n in res.top_domains),
        ]
    lines += ["", esc(res.coverage.human_text), "<i>No AI was used.</i>"]
    rows = []
    if ctx.can(Permission.RUN_AI):
        rows.append([ctx.btn("🧠 AI Analysis of this range", "scn", "ai")])
    if ctx.can(Permission.EXPORT):
        rows.append([ctx.btn("📄 Export this range", "scn", "export")])
    rows.append([ctx.btn("🗂 Source messages", "src", 0)])
    if ctx.services.settings.mtproto_enabled and res.coverage.gaps_in_window:
        rows.append([ctx.btn("🧩 Fill gap via MTProto", "scn", "mtproto")])
    rows.append([ctx.btn("✏️ Change range", "rng", "chg"), ctx.btn("⌂ Menu", "menu")])
    return Screen("\n".join(lines), rows)


@router.route("scn", Permission.SEARCH)
async def scan_followup(ctx: UiContext) -> Screen:
    from elysium_bot.telegram_ui.screens import range_screen

    st = ctx.flow.state.get("rng") or {}
    if "window" not in st:
        from elysium_bot.telegram_ui.screens import main_menu

        return await main_menu(ctx)
    if ctx.arg == "mtproto":
        w = _window(ctx)
        return Screen(
            "<b>Fill gap via MTProto</b>\nRun on the server (interactive login required once):\n"
            f"<code>elysium-bot mtproto backfill --chat {ctx.chat.tg_chat_id}</code>\n\n"
            f"Window: {esc(_span(w, ctx.tz))}",
            [[ctx.btn("‹ Back", "src", "back"), ctx.btn("⌂ Menu", "menu")]],
        )
    target = {"ai": ("ai", Permission.RUN_AI), "export": ("export", Permission.EXPORT)}.get(
        ctx.arg or ""
    )
    if target is None or not ctx.can(target[1]):
        return await range_screen(ctx)
    ctx.flow.state["rng"] = {"purpose": target[0], "stage": "done", "window": st["window"]}
    return await range_screen(ctx)


@router.route("src", Permission.SEARCH)
async def source_messages(ctx: UiContext) -> Screen:
    if ctx.arg == "back":
        from elysium_bot.telegram_ui.screens import range_screen

        return await range_screen(ctx)
    window = _window(ctx)
    page = max(0, int(ctx.arg or 0)) if (ctx.arg or "0").isdigit() else 0
    ctx.flow.state["page_of"] = "src"
    base = (
        Message.chat_id == ctx.chat.id,
        Message.sent_at >= window.start,
        Message.sent_at <= window.end,
        Message.is_service.is_(False),
    )
    total = (await ctx.session.execute(select(func.count()).where(*base))).scalar_one()
    rows = (
        await ctx.session.execute(
            select(Message, TelegramUser, ChatMember)
            .outerjoin(TelegramUser, TelegramUser.id == Message.sender_user_id)
            .outerjoin(
                ChatMember,
                (ChatMember.user_id == Message.sender_user_id)
                & (ChatMember.chat_id == ctx.chat.id),
            )
            .where(*base)
            .order_by(Message.sent_at, Message.id)
            .limit(PER_PAGE)
            .offset(page * PER_PAGE)
        )
    ).all()
    pages = max(1, -(-int(total) // PER_PAGE))
    lines = [f"<b>Source messages</b> ({int(total):,}) — {esc(window.label)}", ""]
    for msg, user, member in rows:
        url = message_url(
            ctx.chat.tg_chat_id,
            msg.tg_message_id,
            username=ctx.chat.username,
            thread_id=msg.message_thread_id,
        )
        body = (msg.text or (f"[{msg.media_type}]" if msg.media_type else "")).replace("\n", " ")
        lines.append(
            f"{format_local(msg.sent_at, ctx.tz, '%m-%d %H:%M')} {link(url, '🔗')} <b>{esc(sender_label(user, member))}</b>: "
            f"{esc(body[:140])}{'…' if len(body) > 140 else ''}"
        )
    return Screen(
        "\n".join(lines),
        [
            _pager(ctx, "src", page, pages),
            [ctx.btn("‹ Back", "src", "back"), ctx.btn("⌂ Menu", "menu")],
        ],
    )


def _pager(ctx: UiContext, op: str, page: int, pages: int) -> list[Any]:
    row = []
    if page > 0:
        row.append(ctx.btn("‹ Prev", op, page - 1))
    row.append(ctx.btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(ctx.btn("Next ›", op, page + 1))
    return row


# --- search -------------------------------------------------------------------------------------


@router.route("srch", Permission.SEARCH)
async def search_action(ctx: UiContext) -> Screen:
    from elysium_bot.telegram_ui.screens import set_pending, take_input

    arg = ctx.arg or "ask"
    if arg == "ask":
        set_pending(ctx, "srch", "typed", "Search query")
        return Screen(
            '<b>Search messages</b>\nSend words, a <code>"phrase"</code>, a <code>$TICKER</code> or a full '
            "<code>0x…</code> contract address in this chat.",
            [[ctx.btn("‹ Back", "nav", "history"), ctx.btn("⌂ Menu", "menu")]],
        )
    if arg == "typed":
        query = (take_input(ctx) or "").strip()
        ctx.flow.state.pop("pending", None)
        ctx.flow.state["search"] = {"q": query[:200]}
        page = 0
    else:
        page = int(arg) if arg.isdigit() else 0
    query = (ctx.flow.state.get("search") or {}).get("q", "")
    result = await search_messages(
        ctx.session, ctx.scope, query, limit=PER_PAGE, offset=page * PER_PAGE
    )
    pages = max(1, -(-result.total // PER_PAGE))
    mode = " (exact contract match)" if result.mode == "contract" else ""
    lines = [
        f"<b>Search</b>: {esc(query)}{mode} — {result.total:,} result{'s' if result.total != 1 else ''}",
        "",
    ]
    if result.mode == "empty":
        lines.append("Type at least one word.")
    for hit in result.hits:
        url = message_url(
            ctx.chat.tg_chat_id,
            hit.tg_message_id,
            username=ctx.chat.username,
            thread_id=hit.message_thread_id,
        )
        lines.append(
            f"{format_local(hit.sent_at, ctx.tz, '%Y-%m-%d %H:%M')} {link(url, '🔗')} {_snippet(hit.snippet)}"
        )
    rows = [_pager(ctx, "srch", page, pages)] if pages > 1 else []
    rows.append(
        [
            ctx.btn("🔎 New search", "srch", "ask"),
            ctx.btn("‹ Back", "nav", "history"),
            ctx.btn("⌂ Menu", "menu"),
        ]
    )
    return Screen("\n".join(lines), rows)


# --- export -------------------------------------------------------------------------------------


def _exp(ctx: UiContext) -> dict[str, Any]:
    state: dict[str, Any] = ctx.flow.state.setdefault(
        "exp",
        {
            "fmt": None,
            "pseudo": ctx.chat_settings.pseudonymize_exports,
            "svc": False,
            "bots": False,
        },
    )
    return state


async def export_done(ctx: UiContext, window: Window) -> Screen:
    st = _exp(ctx)
    if st.get("fmt") is None:
        rows = [[ctx.btn(FORMAT_LABELS[f], "exp", f"fmt_{f}") for f in FORMATS]]
        rows.append([ctx.btn("✏️ Change range", "rng", "chg"), ctx.btn("⌂ Menu", "menu")])
        return Screen(
            f"<b>Export Raw Chat</b>\n{esc(_span(window, ctx.tz))}\n\nChoose a format. No AI is used.",
            rows,
        )
    return await export_confirm(ctx, window, st)


async def export_confirm(ctx: UiContext, window: Window, st: dict[str, Any]) -> Screen:
    req = _request(window, st)
    rows_n = await count_rows(ctx.session, ctx.scope, req)
    cov = await coverage.statement(ctx.session, ctx.chat, window.start, window.end, tz=ctx.tz)
    est = rows_n * BYTES_PER_ROW[req.fmt]
    used = await recent_exports(ctx.session, ctx.scope)
    limit = ctx.services.settings.export_max_per_hour
    lines = [
        "<b>Confirm export</b>",
        f"Window {esc(_span(window, ctx.tz))}",
        f"Format {FORMAT_LABELS[req.fmt]} · rows {rows_n:,} · est. {est / 1e6:,.1f} MB"
        + (" (zipped / split into parts)" if est > 5e6 else ""),
        esc(cov.human_text),
        "<i>No AI is used. The file contains private group data — handle accordingly.</i>",
        f"Exports used this hour: {used}/{limit}",
    ]
    rows = [
        [ctx.btn(f"{'☑' if st['pseudo'] else '☐'} Pseudonymize", "exp", "pseudo")],
        [
            ctx.btn(f"{'☑' if st['svc'] else '☐'} Service messages", "exp", "svc"),
            ctx.btn(f"{'☑' if st['bots'] else '☐'} Bot messages", "exp", "bots"),
        ],
    ]
    if used >= limit:
        lines.append(f"{r.CAVEAT} Export limit reached for this hour.")
    else:
        rows.append([ctx.btn("✅ Export", "exp", "go")])
    rows.append(
        [
            ctx.btn("‹ Format", "exp", "fmt"),
            ctx.btn("✏️ Change range", "rng", "chg"),
            ctx.btn("⌂ Menu", "menu"),
        ]
    )
    return Screen("\n".join(lines), rows)


def _request(window: Window, st: dict[str, Any]) -> ExportRequest:
    return ExportRequest(window, st["fmt"], bool(st["pseudo"]), bool(st["svc"]), bool(st["bots"]))


@router.route("exp", Permission.EXPORT)
async def export_action(ctx: UiContext) -> Screen:
    if "window" not in (ctx.flow.state.get("rng") or {}):
        from elysium_bot.telegram_ui.screens import main_menu

        return await main_menu(ctx)
    window = _window(ctx)
    st = _exp(ctx)
    arg = ctx.arg or ""
    if arg.startswith("fmt_") and arg[4:] in FORMATS:
        st["fmt"] = arg[4:]
    elif arg == "fmt":
        st["fmt"] = None
    elif arg in ("pseudo", "svc", "bots"):
        st[arg] = not st[arg]
    elif arg == "go" and st.get("fmt"):
        return await start_export(ctx, window, st)
    return await export_done(ctx, window)


async def start_export(ctx: UiContext, window: Window, st: dict[str, Any]) -> Screen:
    target = ctx.flow.state.get("msg") or [ctx.scope.actor_tg_user_id, None]
    req = _request(window, st)
    ctx.flow.state.pop("exp", None)
    task = asyncio.create_task(
        run_export_task(ctx.services, ctx.bot, ctx.scope, req, int(target[0]), target[1])
    )
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)
    return Screen(
        f"{r.RUNNING} <b>Exporting…</b>\n{esc(_span(window, ctx.tz))} · {FORMAT_LABELS[req.fmt]}\n"
        "The file(s) will arrive in this chat. You can cancel from Status.",
        [[ctx.btn("ℹ️ Status", "nav", "status"), ctx.btn("⌂ Menu", "menu")]],
    )


async def run_export_task(
    services: Services,
    bot: Any,
    scope: ChatScope,
    req: ExportRequest,
    chat_id: int,
    message_id: int | None,
) -> None:
    async def edit(text: str) -> object:
        if message_id is None:
            return None
        try:
            return await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML
            )
        except Exception:  # progress is best-effort
            return None

    progress = ProgressMessage(edit)

    async def on_progress(done: int, total: int) -> None:
        await progress.update(
            f"{r.RUNNING} <b>Exporting…</b> {done * 100 // max(total, 1)}% ({done:,}/{total:,} rows)"
        )

    files = []
    try:
        result = await export_chat(
            services.sessions,
            scope,
            req,
            data_dir=services.settings.data_dir,
            max_per_hour=services.settings.export_max_per_hour,
            jobs=services.jobs,
            progress=on_progress,
        )
        files = result.files
        for i, path in enumerate(result.files, start=1):
            part = f" · part {i}/{len(result.files)}" if len(result.files) > 1 else ""
            caption = f"{result.rows:,} rows{part} · no AI used · private group data"
            with path.open("rb") as fh:
                await bot.send_document(chat_id, document=fh, filename=path.name, caption=caption)
            path.unlink(missing_ok=True)
        await progress.update(
            f"{r.OK} <b>Export ready</b> — {result.rows:,} rows in {len(result.files)} file(s).\n{esc(result.coverage_text)}",
            final=True,
        )
    except ExportCancelled:
        await progress.update(f"{r.DISABLED} Export cancelled.", final=True)
    except ExportRefused as exc:
        await progress.update(f"{r.CAVEAT} Export not started: {esc(exc)}", final=True)
    except Exception:
        log.exception("export.failed")
        await progress.update(f"{r.FAILED} Export failed. Nothing was sent.", final=True)
    finally:
        for path in files:
            path.unlink(missing_ok=True)


__all__ = ["BACKGROUND", "export_done", "paginate", "scan_done"]
