"""History coverage: which time ranges are stored, from which sources, and what may be missing.

See HISTORY_INGESTION §6. The UI must never call partial history "complete".
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from elysium_bot.db.enums import ChatStatus, ChatType, CoverageKind, Source
from elysium_bot.db.models import CoverageSegment, Message, MessageSource, TelegramChat
from elysium_bot.timeutil import format_local, utcnow

MIN_ID_GAP = 50


@dataclass(frozen=True)
class Gap:
    kind: str  # "gap_suspected" | "id_gap"
    start_at: datetime | None
    end_at: datetime | None
    start_id: int | None = None
    end_id: int | None = None
    note: str | None = None


@dataclass
class CoverageStatement:
    earliest_stored_at: datetime | None
    latest_stored_at: datetime | None
    window_start: datetime | None
    window_end: datetime | None
    messages_in_window: int
    sources_in_window: dict[str, int] = field(default_factory=dict)
    gaps_in_window: list[Gap] = field(default_factory=list)
    complete_for_window: bool = False
    human_text: str = ""


# --- segment maintenance ---------------------------------------------------------------------


async def extend_observed(
    session: AsyncSession, chat_id: int, source: Source, sent_at: datetime, tg_message_id: int
) -> CoverageSegment:
    """Extend the open live segment, or start a new one after a recorded gap."""
    seg = (
        await session.execute(
            select(CoverageSegment)
            .where(
                CoverageSegment.chat_id == chat_id,
                CoverageSegment.source == source,
                CoverageSegment.kind == CoverageKind.OBSERVED,
                CoverageSegment.import_run_id.is_(None),
            )
            .order_by(CoverageSegment.end_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    later_gap = None
    if seg is not None:
        later_gap = (
            await session.execute(
                select(CoverageSegment.id).where(
                    CoverageSegment.chat_id == chat_id,
                    CoverageSegment.source == source,
                    CoverageSegment.kind == CoverageKind.GAP_SUSPECTED,
                    CoverageSegment.start_at >= seg.end_at,
                )
            )
        ).first()
    if seg is None or later_gap is not None:
        seg = CoverageSegment(
            chat_id=chat_id,
            source=source,
            kind=CoverageKind.OBSERVED,
            start_at=sent_at,
            end_at=sent_at,
            start_tg_message_id=tg_message_id,
            end_tg_message_id=tg_message_id,
            message_count=1,
        )
        session.add(seg)
    else:
        if sent_at < seg.start_at:
            seg.start_at, seg.start_tg_message_id = sent_at, tg_message_id
        if sent_at >= seg.end_at:
            seg.end_at, seg.end_tg_message_id = sent_at, tg_message_id
        seg.message_count += 1
    await session.flush()
    return seg


async def add_observed(
    session: AsyncSession,
    chat_id: int,
    source: Source,
    *,
    start_at: datetime,
    end_at: datetime,
    start_id: int | None,
    end_id: int | None,
    count: int,
    import_run_id: int | None,
    note: str | None = None,
) -> CoverageSegment:
    seg = CoverageSegment(
        chat_id=chat_id,
        source=source,
        kind=CoverageKind.OBSERVED,
        start_at=start_at,
        end_at=end_at,
        start_tg_message_id=start_id,
        end_tg_message_id=end_id,
        message_count=count,
        import_run_id=import_run_id,
        note=note,
    )
    session.add(seg)
    await session.flush()
    return seg


async def record_gap(
    session: AsyncSession,
    chat_id: int,
    source: Source,
    start_at: datetime,
    end_at: datetime,
    note: str,
) -> CoverageSegment:
    seg = CoverageSegment(
        chat_id=chat_id,
        source=source,
        kind=CoverageKind.GAP_SUSPECTED,
        start_at=start_at,
        end_at=end_at,
        note=note,
    )
    session.add(seg)
    await session.flush()
    return seg


# --- analysis --------------------------------------------------------------------------------


def _union(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _covers(union: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    return any(s <= start and end <= e for s, e in union)


async def _observed_intervals(
    session: AsyncSession, chat_id: int, now: datetime
) -> list[tuple[datetime, datetime]]:
    segs = list(
        (
            await session.execute(
                select(CoverageSegment).where(
                    CoverageSegment.chat_id == chat_id,
                    CoverageSegment.kind.in_(
                        [CoverageKind.OBSERVED, CoverageKind.GAP_CONFIRMED_EMPTY]
                    ),
                )
            )
        ).scalars()
    )
    gaps = list(
        (
            await session.execute(
                select(CoverageSegment.source, CoverageSegment.start_at).where(
                    CoverageSegment.chat_id == chat_id,
                    CoverageSegment.kind == CoverageKind.GAP_SUSPECTED,
                )
            )
        ).all()
    )
    intervals = []
    live = [s for s in segs if s.source == Source.BOT_API and s.import_run_id is None]
    open_live = max(live, key=lambda s: s.end_at) if live else None
    for seg in segs:
        end = seg.end_at
        # The newest live segment stays open until "now" unless a gap was recorded after it.
        if seg is open_live and not any(
            src == Source.BOT_API and start >= seg.end_at for src, start in gaps
        ):
            end = max(end, now)
        intervals.append((seg.start_at, end))
    return _union(intervals)


async def id_gaps(session: AsyncSession, chat_id: int, start: datetime, end: datetime) -> list[Gap]:
    """Unaccounted message-id runs (supergroups: ids are per-chat and monotonic)."""
    rows = (
        await session.execute(
            text(
                """
                SELECT tg_message_id, sent_at, prev_id, prev_sent FROM (
                    SELECT tg_message_id, sent_at,
                           LAG(tg_message_id) OVER (ORDER BY tg_message_id) AS prev_id,
                           LAG(sent_at) OVER (ORDER BY tg_message_id) AS prev_sent
                    FROM messages
                    WHERE chat_id = :chat AND sent_at >= :start AND sent_at <= :end
                ) t WHERE prev_id IS NOT NULL
                """
            ).bindparams(chat=chat_id, start=_bind(session, start), end=_bind(session, end))
        )
    ).all()
    diffs = [r.tg_message_id - r.prev_id for r in rows]
    if not diffs:
        return []
    threshold = max(MIN_ID_GAP, 3 * statistics.median(diffs))
    confirmed = list(
        (
            await session.execute(
                select(
                    CoverageSegment.start_tg_message_id, CoverageSegment.end_tg_message_id
                ).where(
                    CoverageSegment.chat_id == chat_id,
                    CoverageSegment.kind == CoverageKind.GAP_CONFIRMED_EMPTY,
                )
            )
        ).all()
    )
    gaps = []
    for r, d in zip(rows, diffs, strict=True):
        if d <= threshold:
            continue
        lo, hi = r.prev_id + 1, r.tg_message_id - 1
        if any(a is not None and b is not None and a <= lo and hi <= b for a, b in confirmed):
            continue
        gaps.append(
            Gap(
                "id_gap",
                _load(r.prev_sent),
                _load(r.sent_at),
                lo,
                hi,
                f"possible gap (IDs {lo:,}–{hi:,} unaccounted for)",  # noqa: RUF001,
            )
        )
    return gaps


def _bind(session: AsyncSession, dt: datetime) -> object:
    # Raw SQL bypasses UTCDateTime; SQLite stores naive UTC strings.
    if session.get_bind().dialect.name == "sqlite":
        return dt.replace(tzinfo=None).isoformat(sep=" ")
    return dt


def _load(value: object) -> datetime | None:
    if value is None:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def statement(
    session: AsyncSession,
    chat: TelegramChat,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    *,
    tz: str = "Asia/Ho_Chi_Minh",
    now: datetime | None = None,
) -> CoverageStatement:
    now = now or utcnow()
    earliest, latest = (
        await session.execute(
            select(func.min(Message.sent_at), func.max(Message.sent_at)).where(
                Message.chat_id == chat.id
            )
        )
    ).one()
    first_at, last_at = _load(earliest), _load(latest)
    start = window_start or first_at
    end = window_end or (now if last_at else None)

    if first_at is None or last_at is None or start is None or end is None:
        return CoverageStatement(
            None,
            None,
            window_start,
            window_end,
            0,
            human_text="No messages stored for this chat yet.",
        )

    in_window = and_(Message.chat_id == chat.id, Message.sent_at >= start, Message.sent_at <= end)
    count = (await session.execute(select(func.count()).where(in_window))).scalar_one()
    by_source = dict(
        (
            await session.execute(
                select(MessageSource.source, func.count())
                .join(Message, Message.id == MessageSource.message_id)
                .where(in_window)
                .group_by(MessageSource.source)
            )
        ).all()
    )

    gaps: list[Gap] = [
        Gap("gap_suspected", g.start_at, g.end_at, note=g.note)
        for g in (
            await session.execute(
                select(CoverageSegment).where(
                    CoverageSegment.chat_id == chat.id,
                    CoverageSegment.kind == CoverageKind.GAP_SUSPECTED,
                    CoverageSegment.end_at >= start,
                    CoverageSegment.start_at <= end,
                )
            )
        ).scalars()
    ]
    if chat.type != ChatType.GROUP:
        gaps.extend(await id_gaps(session, chat.id, start, end))

    union = await _observed_intervals(session, chat.id, now)
    covered = _covers(union, start, end)
    complete = covered and not gaps

    def fmt(dt: datetime) -> str:
        return format_local(dt, tz, "%Y-%m-%d %H:%M")

    stored = f"Stored history covers {fmt(first_at)} → {fmt(last_at)}."
    if complete:
        human = f"{stored} This window is complete."
    else:
        reasons = []
        if start < first_at:
            reasons.append(f"no stored messages before {fmt(first_at)}")
        elif not covered:
            reasons.append("parts of this window were not observed by any ingestion source")
        if gaps:
            reasons.append(f"{len(gaps)} possible gap{'s' if len(gaps) != 1 else ''}")
        human = f"⚠️ Partial: {'; '.join(reasons)}. {stored}"
    return CoverageStatement(
        earliest_stored_at=first_at,
        latest_stored_at=last_at,
        window_start=start,
        window_end=end,
        messages_in_window=count,
        sources_in_window={str(k): v for k, v in by_source.items()},
        gaps_in_window=gaps,
        complete_for_window=complete,
        human_text=human,
    )


async def detect_downtime_gaps(
    session: AsyncSession, last_beat: datetime | None, now: datetime, threshold: timedelta
) -> list[TelegramChat]:
    """If the bot was down longer than Telegram keeps updates (24 h), record a suspected gap."""
    if last_beat is None or now - last_beat <= threshold:
        return []
    chats = list(
        (
            await session.execute(
                select(TelegramChat).where(TelegramChat.status == ChatStatus.ACTIVE)
            )
        ).scalars()
    )
    hours = int((now - last_beat).total_seconds() // 3600)
    for chat in chats:
        await record_gap(
            session,
            chat.id,
            Source.BOT_API,
            last_beat,
            now,
            f"bot offline ~{hours} h; Bot API keeps updates for 24 h at most",
        )
    return chats
