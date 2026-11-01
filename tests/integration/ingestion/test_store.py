from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from elysium_bot.db.enums import ChatType, IndexState, Source
from elysium_bot.db.models import ChatMember, Message, MessageSource, MessageVersion, TelegramUser
from elysium_bot.ingestion.normalizer import NormalizedMessage, SenderRef
from elysium_bot.ingestion.store import MessageStore
from tests.factories import make_chat

T = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
ALICE = SenderRef(kind="user", tg_id=101, username="alice", first_name="Alice")
BOB = SenderRef(kind="user", tg_id=202, first_name="Bob")


def nm(
    mid: int,
    text: str,
    *,
    source: Source = Source.BOT_API,
    edited: datetime | None = None,
    sender: SenderRef | None = ALICE,
    sent: datetime = T,
    **kw: object,
) -> NormalizedMessage:
    return NormalizedMessage(
        source=source,
        tg_chat_id=-1001234567890,
        tg_message_id=mid,
        sender=sender,
        sent_at=sent,
        edited_at=edited,
        text=text,
        **kw,  # type: ignore[arg-type]
    )


async def count(session: AsyncSession, model: type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_message_edit_and_duplicate_redelivery(session: AsyncSession) -> None:
    chat = await make_chat(session)
    store = MessageStore(session, chat)
    original = nm(10, "Nest launches Nov 9")
    edit = nm(10, "Nest launches Nov 10 (corrected)", edited=T + timedelta(minutes=3))

    s1 = await store.upsert([original])
    s2 = await store.upsert([edit])
    s3 = await store.upsert([edit])  # duplicate redelivery
    await session.commit()

    assert (s1.inserted, s2.updated, s2.versions_added, s3.unchanged) == (1, 1, 2, 1)
    assert await count(session, Message) == 1
    versions = (
        (await session.execute(select(MessageVersion).order_by(MessageVersion.version_no)))
        .scalars()
        .all()
    )
    assert [v.text for v in versions] == ["Nest launches Nov 9", "Nest launches Nov 10 (corrected)"]
    assert [(s.source) for s in (await session.execute(select(MessageSource))).scalars()] == [
        Source.BOT_API
    ]
    row = (await session.execute(select(Message))).scalar_one()
    assert row.text.endswith("(corrected)") and row.index_state == IndexState.PENDING


async def test_stale_edit_does_not_overwrite(session: AsyncSession) -> None:
    chat = await make_chat(session)
    store = MessageStore(session, chat)
    await store.upsert([nm(1, "v2", edited=T + timedelta(minutes=5))])
    stats = await store.upsert([nm(1, "v1", edited=T + timedelta(minutes=1))])
    assert stats.unchanged == 1
    assert (await session.execute(select(Message.text))).scalar_one() == "v2"


async def test_export_merges_with_live_and_fills_nulls(session: AsyncSession) -> None:
    chat = await make_chat(session)
    store = MessageStore(session, chat)
    await store.upsert([nm(5, "gm")])
    stats = await store.upsert(
        [
            nm(5, "gm", source=Source.DESKTOP_EXPORT, reply_to_tg_message_id=4),
            nm(6, "new", source=Source.DESKTOP_EXPORT),
        ],
        import_run_id=None,
    )
    await session.commit()
    assert (stats.inserted, stats.updated) == (1, 1)
    assert await count(session, Message) == 2
    sources = (
        (await session.execute(select(MessageSource.source).order_by(MessageSource.source)))
        .scalars()
        .all()
    )
    assert sorted(sources) == [Source.BOT_API, Source.DESKTOP_EXPORT, Source.DESKTOP_EXPORT]
    row = (await session.execute(select(Message).where(Message.tg_message_id == 5))).scalar_one()
    assert row.reply_to_tg_message_id == 4
    assert row.primary_source == Source.BOT_API


async def test_users_members_and_pseudonyms(session: AsyncSession) -> None:
    chat = await make_chat(session)
    store = MessageStore(session, chat)
    await store.upsert([nm(1, "a"), nm(2, "b", sender=BOB), nm(3, "c")])
    await store.upsert([nm(4, "d", sender=SenderRef(kind="user", tg_id=101, username="alice_new"))])
    await session.commit()
    assert await count(session, TelegramUser) == 2
    pseudonyms = (
        (await session.execute(select(ChatMember.pseudonym).order_by(ChatMember.pseudonym)))
        .scalars()
        .all()
    )
    assert pseudonyms == ["U1", "U2"]
    alice = (
        await session.execute(select(TelegramUser).where(TelegramUser.tg_user_id == 101))
    ).scalar_one()
    assert alice.username == "alice_new"  # live updates refresh profile


async def test_channel_sender_stored_without_user(session: AsyncSession) -> None:
    chat = await make_chat(session)
    await MessageStore(session, chat).upsert(
        [nm(1, "official", sender=SenderRef(kind="chat", tg_id=-100777, title="Kinetiq"))]
    )
    row = (await session.execute(select(Message))).scalar_one()
    assert (row.sender_user_id, row.sender_chat_tg_id, row.sender_name_snapshot) == (
        None,
        -100777,
        "Kinetiq",
    )


async def test_basic_group_fingerprint_dedup(session: AsyncSession) -> None:
    chat = await make_chat(session, tg_chat_id=-4242)
    chat.type = ChatType.GROUP
    store = MessageStore(session, chat)
    await store.upsert([nm(500, "hello", sent=T)])
    # Same message seen from the exporter's account with a different per-user id:
    same = await store.upsert([nm(731, "hello", sent=T, source=Source.DESKTOP_EXPORT)])
    # A different message whose per-user id collides with the bot's id 500:
    clash = await store.upsert(
        [nm(500, "other", sent=T + timedelta(minutes=1), source=Source.DESKTOP_EXPORT)]
    )
    assert (same.inserted, same.unchanged) == (0, 1)
    assert (clash.inserted, clash.skipped) == (0, 1)
    assert clash.skipped_refs[0][1] == "basic_group_id_collision"
    assert await count(session, Message) == 1


async def test_stats_track_ranges(session: AsyncSession) -> None:
    chat = await make_chat(session)
    stats = await MessageStore(session, chat).upsert(
        [nm(7, "x", sent=T + timedelta(hours=1)), nm(3, "y", sent=T)]
    )
    assert (stats.min_id, stats.max_id, stats.min_sent_at, stats.max_sent_at) == (
        3,
        7,
        T,
        T + timedelta(hours=1),
    )
