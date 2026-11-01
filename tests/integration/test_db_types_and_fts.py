from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from elysium_bot.db.models import Message
from tests.factories import make_chat, make_message

ICT = timezone(timedelta(hours=7))


async def fts_ids(session: AsyncSession, query: str) -> list[int]:
    rows = await session.execute(
        text("SELECT rowid FROM messages_fts WHERE messages_fts MATCH :q ORDER BY rowid"),
        {"q": query},
    )
    return [r[0] for r in rows]


async def test_naive_datetime_rejected(session: AsyncSession) -> None:
    chat = await make_chat(session)
    with pytest.raises(StatementError, match="naive datetime"):
        await make_message(session, chat, 1, "hello", sent_at=datetime(2026, 11, 1, 10, 0))


async def test_non_utc_datetime_stored_and_loaded_as_utc(session: AsyncSession) -> None:
    chat = await make_chat(session)
    msg = await make_message(
        session, chat, 1, "hi", sent_at=datetime(2026, 11, 1, 10, 0, tzinfo=ICT)
    )
    await session.commit()

    raw = (
        await session.execute(text("SELECT sent_at FROM messages WHERE id = :id"), {"id": msg.id})
    ).scalar_one()
    assert str(raw).startswith("2026-11-01 03:00:00")

    session.expunge_all()
    loaded = (await session.execute(select(Message).where(Message.id == msg.id))).scalar_one()
    assert loaded.sent_at == datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
    assert loaded.sent_at.tzinfo is UTC


async def test_fts_tracks_insert_update_delete(session: AsyncSession) -> None:
    chat = await make_chat(session)
    m1 = await make_message(session, chat, 1, "Nest DEX goes live on Elysium mainnet")
    m2 = await make_message(session, chat, 2, "gm everyone")
    await session.commit()
    assert await fts_ids(session, "elysium") == [m1.id]

    m2.text = "Elysium testnet bridge is up"
    await session.commit()
    assert await fts_ids(session, "elysium") == [m1.id, m2.id]

    await session.delete(m1)
    await session.commit()
    assert await fts_ids(session, "elysium") == [m2.id]
    assert await fts_ids(session, "nest") == []


async def test_fts_keeps_ticker_tokens_whole(session: AsyncSession) -> None:
    chat = await make_chat(session)
    ticker = await make_message(session, chat, 1, "aping into $NEST at launch")
    await make_message(session, chat, 2, "the nest is empty")
    await session.commit()
    assert await fts_ids(session, '"$nest"') == [ticker.id]


async def test_unique_message_per_chat(session: AsyncSession) -> None:
    chat = await make_chat(session)
    await make_message(session, chat, 7, "one")
    with pytest.raises(IntegrityError):
        await make_message(session, chat, 7, "dup")


async def test_foreign_keys_cascade(session: AsyncSession) -> None:
    chat = await make_chat(session)
    await make_message(session, chat, 1, "x")
    await session.commit()
    await session.execute(text("DELETE FROM telegram_chats WHERE id = :id"), {"id": chat.id})
    await session.commit()
    assert (await session.execute(text("SELECT count(*) FROM messages"))).scalar_one() == 0
