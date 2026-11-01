"""DM upload of a Telegram Desktop export (≤ 20 MB, the Bot API download limit)."""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from elysium_bot.db.access import get_chat_by_tg_id, resolve_role
from elysium_bot.db.enums import ChatStatus, Role, StoredRole
from elysium_bot.db.models import ChatRole, ChatSettings
from elysium_bot.ingestion import coverage
from elysium_bot.ingestion.desktop_import import (
    ImportAbort,
    format_import_report,
    import_export,
    read_headers,
)
from elysium_bot.logging import get_logger

if TYPE_CHECKING:
    from elysium_bot.app import Services

log = get_logger(__name__)

MAX_BOT_DOWNLOAD_BYTES = 20 * 1024 * 1024
TOO_LARGE = (
    "This file is larger than 20 MB, the most a bot can download. Copy it to the server and run:\n"
    "elysium-bot import-export /data/imports/result.json --chat <chat id>"
)
NOT_AN_EXPORT = (
    "Send a Telegram Desktop export: result.json (Machine-readable JSON) or a .zip containing it."
)
NO_PERMISSION = "Only the owner or a chat admin can import history."


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80] or "export.json"


async def _may_import_anything(services: Services, tg_user_id: int) -> bool:
    if services.settings.is_owner(tg_user_id):
        return True
    async with services.sessions() as session:
        found = await session.execute(
            select(ChatRole.chat_id).where(
                ChatRole.tg_user_id == tg_user_id, ChatRole.role == StoredRole.ADMIN
            )
        )
        return found.first() is not None


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None or message.document is None:
        return
    services: Services = context.bot_data["services"]
    doc = message.document
    if not (doc.file_name or "").lower().endswith((".json", ".zip")):
        await message.reply_text(NOT_AN_EXPORT)
        return
    if not await _may_import_anything(services, user.id):
        await message.reply_text(NO_PERMISSION)
        return
    if (doc.file_size or 0) > MAX_BOT_DOWNLOAD_BYTES:
        await message.reply_text(TOO_LARGE)
        return

    dest_dir = services.settings.data_dir / "imports"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex[:8]}-{_safe_name(doc.file_name or '')}"
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(dest)

    try:
        headers = await asyncio.to_thread(read_headers, dest)
    except (ImportAbort, ValueError) as exc:
        dest.unlink(missing_ok=True)
        await message.reply_text(f"{NOT_AN_EXPORT}\n({exc})")
        return

    async with services.sessions() as session:
        target = None
        for header in headers:
            chat = await get_chat_by_tg_id(session, header.tg_chat_id)
            if chat is not None:
                target = chat
                break
        if target is None:
            dest.unlink(missing_ok=True)
            await message.reply_text(
                "None of the chats in this export are activated. Run /activate in the group first."
            )
            return
        role = await resolve_role(session, services.settings, target.id, user.id)
        settings_row = await session.get(ChatSettings, target.id)
        tz = settings_row.timezone if settings_row else services.settings.default_timezone
        title, tg_chat_id, inactive = (
            target.title,
            target.tg_chat_id,
            target.status != ChatStatus.ACTIVE,
        )
    if role not in (Role.OWNER, Role.ADMIN):
        dest.unlink(missing_ok=True)
        await message.reply_text(NO_PERMISSION)
        return

    note = " (note: this chat is not currently active)" if inactive else ""
    await message.reply_text(
        f"⏳ Import started for “{title}”{note}. I'll send a report when it finishes."
    )
    context.application.create_task(
        run_import_and_report(services, context, dest, tg_chat_id, user.id, tz),
        name=f"import:{dest.name}",
    )


async def run_import_and_report(
    services: Services,
    context: ContextTypes.DEFAULT_TYPE,
    path: Path,
    tg_chat_id: int,
    tg_user_id: int,
    tz: str,
) -> None:
    try:
        result = await import_export(
            services.sessions,
            path,
            tg_chat_id=tg_chat_id,
            triggered_by=tg_user_id,
            fallback_tz=tz,
            jobs=services.jobs,
        )
    except ImportAbort as exc:
        await context.bot.send_message(tg_user_id, f"Import not started: {exc}")
        return
    except Exception as exc:
        log.exception("import.dm_failed", file=path.name)
        await context.bot.send_message(
            tg_user_id,
            f"Import failed ({type(exc).__name__}). The file was kept on the server as "
            f"{path.name}; "
            "resume with: elysium-bot import-export <file> --resume <run id>",
        )
        return
    async with services.sessions() as session:
        st = await coverage.statement(session, result.chat, tz=tz)
    await asyncio.to_thread(path.unlink, missing_ok=True)
    await context.bot.send_message(tg_user_id, format_import_report(result, st.human_text, tz))
