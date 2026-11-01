from __future__ import annotations

import os
import stat
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon.errors import FloodWaitError

from elysium_bot.db.enums import ChatStatus, ChatType, CoverageKind, ImportStatus, Source
from elysium_bot.db.models import CoverageSegment, Message, MessageSource, TelegramChat
from elysium_bot.ingestion.mtproto import session_store
from elysium_bot.ingestion.mtproto.backfill import BackfillAbort, backfill, from_telethon
from elysium_bot.ingestion.normalizer import NormalizedMessage, SenderRef
from elysium_bot.ingestion.store import MessageStore

T = datetime(2026, 10, 1, tzinfo=UTC)
TG_CHAT = -1001234567890


class MessageEntityTextUrl(SimpleNamespace):
    pass


class MessageActionTopicCreate(SimpleNamespace):
    pass


class MessageMediaPhoto(SimpleNamespace):
    pass


def tl_msg(mid: int, text: str = "", **kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": mid,
        "date": T + timedelta(minutes=mid),
        "message": text or f"history {mid}",
        "sender_id": 101,
        "sender": SimpleNamespace(username="alice", first_name="Alice", last_name=None, bot=False),
        "entities": None,
        "reply_to": None,
        "action": None,
        "media": None,
        "fwd_from": None,
        "edit_date": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


class FakeClient:
    """Mimics Telethon's iter_messages(reverse=True, min_id=...) with an optional flood wait."""

    def __init__(
        self, ids: list[int], flood_after: int | None = None, flood_seconds: int = 7
    ) -> None:
        self.messages = [tl_msg(i) for i in ids]
        self.flood_after = flood_after
        self.flood_seconds = flood_seconds
        self.calls: list[dict[str, Any]] = []

    async def get_entity(self, entity: Any) -> Any:
        if entity != TG_CHAT:
            raise ValueError("ChannelPrivateError")
        return SimpleNamespace(id=entity)

    async def iter_messages(self, entity: Any, **kw: Any) -> AsyncIterator[Any]:
        self.calls.append(kw)
        assert kw["reverse"] is True
        for n, msg in enumerate(m for m in self.messages if m.id > kw["min_id"]):
            if self.flood_after is not None and n == self.flood_after:
                self.flood_after = None
                raise FloodWaitError(request=None, capture=self.flood_seconds)
            yield msg


async def make_chat(sf: async_sessionmaker[AsyncSession]) -> TelegramChat:
    async with sf() as s:
        chat = TelegramChat(
            tg_chat_id=TG_CHAT,
            type=ChatType.SUPERGROUP,
            title="Synthetic",
            status=ChatStatus.ACTIVE,
        )
        s.add(chat)
        await s.commit()
        return chat


async def test_backfill_honours_flood_wait_and_resumes_from_checkpoint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = await make_chat(session_factory)
    client = FakeClient(list(range(1, 31)), flood_after=12, flood_seconds=7)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    result = await backfill(session_factory, client, chat, batch_size=5, sleep=fake_sleep)
    assert result.run.status == ImportStatus.SUCCEEDED
    assert result.flood_waits == 1
    assert len(sleeps) == 1 and 8 <= sleeps[0] <= 12  # 7 s + 1..5 s jitter
    # The second pass restarted after the last committed checkpoint: 10 messages were committed in
    # full batches and the 2 in flight were flushed before sleeping.
    assert client.calls[1]["min_id"] == 12
    assert (result.run.seen, result.run.inserted) == (30, 30)
    async with session_factory() as s:
        ids = (
            (await s.execute(select(Message.tg_message_id).order_by(Message.tg_message_id)))
            .scalars()
            .all()
        )
        assert ids == list(range(1, 31))
        assert set((await s.execute(select(MessageSource.source))).scalars()) == {Source.MTPROTO}
        seg = (await s.execute(select(CoverageSegment))).scalar_one()
        assert (seg.kind, seg.source, seg.start_tg_message_id, seg.end_tg_message_id) == (
            CoverageKind.OBSERVED,
            Source.MTPROTO,
            1,
            30,
        )


async def test_backfill_marks_deleted_messages(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = await make_chat(session_factory)
    async with session_factory() as s:
        c = await s.get(TelegramChat, chat.id)
        assert c is not None
        await MessageStore(s, c).upsert(
            [
                NormalizedMessage(
                    source=Source.BOT_API,
                    tg_chat_id=TG_CHAT,
                    tg_message_id=5,
                    sender=SenderRef(kind="user", tg_id=101),
                    sent_at=T,
                    text="later deleted",
                ),
            ]
        )
        await s.commit()
    result = await backfill(session_factory, FakeClient([1, 2, 3, 4, 6, 7]), chat)
    assert result.deleted_marked == 1
    async with session_factory() as s:
        row = (await s.execute(select(Message).where(Message.tg_message_id == 5))).scalar_one()
        assert row.is_deleted and row.deleted_at is not None


async def test_backfill_aborts_without_access(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = await make_chat(session_factory)
    async with session_factory() as s:
        c = await s.get(TelegramChat, chat.id)
        assert c is not None
        c.tg_chat_id = -100999
        await s.commit()
        with pytest.raises(BackfillAbort, match="must already be a member"):
            await backfill(session_factory, FakeClient([1]), c)


def test_from_telethon_mapping() -> None:
    msg = tl_msg(
        9,
        "see docs",
        entities=[MessageEntityTextUrl(offset=4, length=4, url="https://docs.x")],
        reply_to=SimpleNamespace(reply_to_msg_id=3, reply_to_top_id=None, forum_topic=True),
        media=MessageMediaPhoto(),
        edit_date=T + timedelta(hours=1),
    )
    n = from_telethon(msg, TG_CHAT)
    assert n.source == Source.MTPROTO
    assert (n.entities[0].type, n.entities[0].url) == ("text_link", "https://docs.x")
    assert (n.reply_to_tg_message_id, n.message_thread_id) == (3, 3)
    assert n.media_type == "photo"
    service = from_telethon(
        tl_msg(1, action=MessageActionTopicCreate(title="Launches"), sender_id=-1001), TG_CHAT
    )
    assert service.is_service and service.service_action == "topic_created"
    assert service.sender is not None and service.sender.kind == "chat"


def test_session_store_encrypts_with_0600(tmp_path: Path) -> None:
    key = Fernet.generate_key().decode()
    path = session_store.default_session_path(tmp_path)
    session_store.save_session(path, "1AbCdEf-session-string", key)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert b"1AbCdEf" not in path.read_bytes()
    assert session_store.load_session(path, key) == "1AbCdEf-session-string"
    with pytest.raises(session_store.SessionStoreError, match="wrong"):
        session_store.load_session(path, Fernet.generate_key().decode())
    os.chmod(path, 0o644)
    with pytest.raises(session_store.SessionStoreError, match="readable by other users"):
        session_store.load_session(path, key)
    session_store.delete_session(path)
    assert not path.exists()
