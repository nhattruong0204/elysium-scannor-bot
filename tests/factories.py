"""Synthetic test data builders. Never put real Telegram data here."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from elysium_bot.db.enums import ChatType, Source
from elysium_bot.db.models import Message, TelegramChat


async def make_chat(
    session: AsyncSession, tg_chat_id: int = -1001234567890, title: str = "Synthetic Group"
) -> TelegramChat:
    chat = TelegramChat(tg_chat_id=tg_chat_id, type=ChatType.SUPERGROUP, title=title)
    session.add(chat)
    await session.flush()
    return chat


async def make_message(
    session: AsyncSession,
    chat: TelegramChat,
    tg_message_id: int,
    text: str,
    sent_at: datetime | None = None,
) -> Message:
    msg = Message(
        chat_id=chat.id,
        tg_message_id=tg_message_id,
        sent_at=sent_at or datetime(2026, 11, 1, 3, 0, tzinfo=UTC),
        text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        primary_source=Source.BOT_API,
    )
    session.add(msg)
    await session.flush()
    return msg
