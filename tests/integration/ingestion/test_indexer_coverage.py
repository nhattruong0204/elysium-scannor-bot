from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.enums import ChecksumState, CoverageKind, IndexState, LinkKind, Source
from elysium_bot.db.models import ContractMention, CoverageSegment, Message, MessageLink
from elysium_bot.ingestion import coverage
from elysium_bot.ingestion.indexer import drain_all
from elysium_bot.ingestion.normalizer import NormalizedMessage, SenderRef
from elysium_bot.ingestion.store import MessageStore
from tests.factories import make_chat

T = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
CA = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
ALICE = SenderRef(kind="user", tg_id=101, first_name="Alice")


def nm(
    mid: int, text: str, sent: datetime, source: Source = Source.BOT_API, **kw: object
) -> NormalizedMessage:
    return NormalizedMessage(
        source=source,
        tg_chat_id=-1001,
        tg_message_id=mid,
        sender=ALICE,
        sent_at=sent,
        text=text,
        **kw,  # type: ignore[arg-type]
    )


async def test_indexer_extracts_and_reindexes_edits(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        chat = await make_chat(s, tg_chat_id=-1001)
        await MessageStore(s, chat).upsert([nm(1, f"CA {CA} docs https://docs.nest.xyz", T)])
        await s.commit()
    assert await drain_all(session_factory) == 1

    async with session_factory() as s:
        (cm,) = (await s.execute(select(ContractMention))).scalars()
        assert cm.checksum_state == ChecksumState.VALID_CHECKSUM
        (link,) = (await s.execute(select(MessageLink))).scalars()
        assert link.kind == LinkKind.DOCS
        assert (await s.execute(select(Message.index_state))).scalar_one() == IndexState.INDEXED

        await MessageStore(s, chat).upsert(
            [nm(1, "edited: no address now", T, edited_at=T + timedelta(minutes=1))]
        )
        await s.commit()
    assert await drain_all(session_factory) == 1
    async with session_factory() as s:
        assert (await s.execute(select(ContractMention))).first() is None
        assert (await s.execute(select(MessageLink))).first() is None


async def test_live_segment_extends_until_gap(session: AsyncSession) -> None:
    chat = await make_chat(session)
    a = await coverage.extend_observed(session, chat.id, Source.BOT_API, T, 1)
    b = await coverage.extend_observed(session, chat.id, Source.BOT_API, T + timedelta(hours=1), 5)
    assert a.id == b.id and b.message_count == 2 and b.end_tg_message_id == 5
    await coverage.record_gap(
        session, chat.id, Source.BOT_API, T + timedelta(hours=2), T + timedelta(hours=40), "down"
    )
    c = await coverage.extend_observed(session, chat.id, Source.BOT_API, T + timedelta(hours=41), 9)
    assert c.id != a.id


async def _seed(session: AsyncSession, ids_times: list[tuple[int, datetime]]) -> object:
    chat = await make_chat(session, tg_chat_id=-1001)
    store = MessageStore(session, chat)
    await store.upsert([nm(i, f"m{i}", t) for i, t in ids_times])
    for i, t in ids_times:
        await coverage.extend_observed(session, chat.id, Source.BOT_API, t, i)
    return chat


async def test_statement_complete_window(session: AsyncSession) -> None:
    chat = await _seed(session, [(i, T + timedelta(minutes=i)) for i in range(1, 21)])
    st = await coverage.statement(
        session,
        chat,
        T + timedelta(minutes=2),
        T + timedelta(minutes=10),
        now=T + timedelta(minutes=30),
    )  # type: ignore[arg-type]
    assert st.complete_for_window
    assert st.messages_in_window == 9
    assert st.sources_in_window == {"bot_api": 9}
    assert "complete" in st.human_text


async def test_statement_partial_before_earliest_message(session: AsyncSession) -> None:
    chat = await _seed(session, [(i, T + timedelta(minutes=i)) for i in range(1, 5)])
    st = await coverage.statement(
        session, chat, T - timedelta(days=3), T + timedelta(hours=1), now=T + timedelta(hours=2)
    )  # type: ignore[arg-type]
    assert not st.complete_for_window
    assert st.human_text.startswith("⚠️ Partial: no stored messages before 2026-11-01 10:01")


async def test_statement_reports_suspected_gap(session: AsyncSession) -> None:
    chat = await _seed(session, [(1, T), (2, T + timedelta(hours=1))])
    await coverage.record_gap(
        session, chat.id, Source.BOT_API, T + timedelta(hours=2), T + timedelta(hours=30), "down"
    )  # type: ignore[attr-defined]
    await coverage.extend_observed(session, chat.id, Source.BOT_API, T + timedelta(hours=31), 3)  # type: ignore[attr-defined]
    st = await coverage.statement(
        session, chat, T, T + timedelta(hours=32), now=T + timedelta(hours=32)
    )  # type: ignore[arg-type]
    assert not st.complete_for_window
    assert [g.kind for g in st.gaps_in_window] == ["gap_suspected"]


async def test_id_gap_detection(session: AsyncSession) -> None:
    ids = [*range(1, 101), *range(500, 520)]  # 399 ids unaccounted for
    chat = await _seed(session, [(i, T + timedelta(seconds=i)) for i in ids])
    st = await coverage.statement(
        session, chat, T, T + timedelta(hours=1), now=T + timedelta(hours=1)
    )  # type: ignore[arg-type]
    (gap,) = st.gaps_in_window
    assert (gap.kind, gap.start_id, gap.end_id) == ("id_gap", 101, 499)
    assert "possible gap (IDs 101–499 unaccounted for)" in (gap.note or "")
    assert not st.complete_for_window


async def test_detect_downtime_gaps(session: AsyncSession) -> None:
    from elysium_bot.db.enums import ChatStatus

    chat = await make_chat(session)
    chat.status = ChatStatus.ACTIVE
    await session.flush()
    assert (
        await coverage.detect_downtime_gaps(session, T, T + timedelta(hours=2), timedelta(hours=24))
        == []
    )
    hit = await coverage.detect_downtime_gaps(
        session, T, T + timedelta(hours=30), timedelta(hours=24)
    )
    assert [c.id for c in hit] == [chat.id]
    seg = (await session.execute(select(CoverageSegment))).scalar_one()
    assert seg.kind == CoverageKind.GAP_SUSPECTED and seg.start_at == T


async def test_empty_chat_statement(session: AsyncSession) -> None:
    chat = await make_chat(session)
    st = await coverage.statement(session, chat)
    assert st.messages_in_window == 0 and not st.complete_for_window
