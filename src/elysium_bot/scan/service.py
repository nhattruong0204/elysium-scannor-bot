"""ScanService — no AI. Refreshes the index for a window and computes window statistics."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.enums import JobFamily, JobKind
from elysium_bot.db.models import (
    ChatMember,
    ContractMention,
    Message,
    MessageLink,
    ScanCheckpoint,
    TelegramChat,
    TelegramUser,
)
from elysium_bot.db.scope import ChatScope
from elysium_bot.extraction.tickers import extract_tickers
from elysium_bot.ingestion import coverage, indexer
from elysium_bot.scheduler.leases import JobService
from elysium_bot.telegram_ui.components import Window
from elysium_bot.timeutil import utcnow

TOP_N = 5
TICKER_SCAN_LIMIT = 200_000  # beyond this, tickers are counted on the newest messages only


class ScanBusy(RuntimeError):
    pass


@dataclass
class ContractStat:
    address: str  # display form (first raw occurrence)
    checksum_state: str
    mentions: int
    first_tg_message_id: int


@dataclass
class ScanResult:
    window: Window
    messages: int
    service_messages: int
    active_members: int
    top_senders: list[tuple[str, int]]
    contracts: list[ContractStat]
    tickers: list[tuple[str, int]]
    top_domains: list[tuple[str, int]]
    replies: int
    threads: int
    indexed_now: int
    coverage: coverage.CoverageStatement
    ticker_sample_limited: bool = False
    job_id: int | None = None
    notes: list[str] = field(default_factory=list)


def sender_label(user: TelegramUser | None, member: ChatMember | None) -> str:
    if user is None:
        return "channel/anonymous"
    name = " ".join(p for p in (user.first_name, user.last_name) if p) or (
        f"@{user.username}" if user.username else str(user.tg_user_id)
    )
    return f"{name} ({member.pseudonym})" if member else name


async def window_stats(
    session: AsyncSession, scope: ChatScope, window: Window, *, tz: str
) -> ScanResult:
    chat = await session.get(TelegramChat, scope.chat_id)
    assert chat is not None
    in_window = (
        Message.chat_id == scope.chat_id,
        Message.sent_at >= window.start,
        Message.sent_at <= window.end,
    )
    total, service = (
        await session.execute(
            select(func.count(), func.count().filter(Message.is_service.is_(True))).where(
                *in_window
            )
        )
    ).one()
    members = (
        await session.execute(
            select(func.count(func.distinct(Message.sender_user_id))).where(
                *in_window, Message.is_service.is_(False)
            )
        )
    ).scalar_one()
    top = (
        await session.execute(
            select(TelegramUser, ChatMember, func.count().label("n"))
            .select_from(Message)
            .join(TelegramUser, TelegramUser.id == Message.sender_user_id)
            .outerjoin(
                ChatMember,
                (ChatMember.user_id == TelegramUser.id) & (ChatMember.chat_id == scope.chat_id),
            )
            .where(*in_window, Message.is_service.is_(False))
            .group_by(TelegramUser.id, ChatMember.chat_id, ChatMember.user_id)
            .order_by(func.count().desc(), TelegramUser.id)
            .limit(TOP_N)
        )
    ).all()
    contract_rows = (
        await session.execute(
            select(
                ContractMention.normalized,
                func.min(ContractMention.raw),
                func.min(ContractMention.checksum_state),
                func.count(),
                func.min(Message.tg_message_id),
            )
            .join(Message, Message.id == ContractMention.message_id)
            .where(*in_window)
            .group_by(ContractMention.normalized)
            .order_by(func.count().desc(), ContractMention.normalized)
        )
    ).all()
    domains = (
        await session.execute(
            select(MessageLink.domain, func.count())
            .join(Message, Message.id == MessageLink.message_id)
            .where(*in_window, MessageLink.domain.is_not(None))
            .group_by(MessageLink.domain)
            .order_by(func.count().desc(), MessageLink.domain)
            .limit(TOP_N)
        )
    ).all()
    replies = (
        await session.execute(
            select(func.count()).where(*in_window, Message.reply_to_tg_message_id.is_not(None))
        )
    ).scalar_one()
    threads = (
        await session.execute(
            select(func.count(func.distinct(Message.message_thread_id))).where(
                *in_window, Message.message_thread_id.is_not(None)
            )
        )
    ).scalar_one()
    texts = (
        await session.execute(
            select(Message.text)
            .where(*in_window, Message.text.contains("$"))
            .order_by(Message.sent_at.desc())
            .limit(TICKER_SCAN_LIMIT)
        )
    ).scalars()
    ticker_counts: Counter[str] = Counter()
    seen = 0
    for body in texts:
        seen += 1
        ticker_counts.update(set(extract_tickers(body)))
    cov = await coverage.statement(session, chat, window.start, window.end, tz=tz)
    return ScanResult(
        window=window,
        messages=int(total) - int(service),
        service_messages=int(service),
        active_members=int(members),
        top_senders=[(sender_label(u, m), int(n)) for u, m, n in top],
        contracts=[
            ContractStat(
                raw, state.value if hasattr(state, "value") else str(state), int(n), int(first)
            )
            for _, raw, state, n, first in contract_rows
        ],
        tickers=sorted(ticker_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N],
        top_domains=[(str(d), int(n)) for d, n in domains],
        replies=int(replies),
        threads=int(threads),
        indexed_now=0,
        coverage=cov,
        ticker_sample_limited=seen >= TICKER_SCAN_LIMIT,
    )


async def run_scan(
    sessions: async_sessionmaker[AsyncSession],
    scope: ChatScope,
    window: Window,
    *,
    tz: str,
    jobs: JobService | None = None,
    checkpoint_user: int | None = None,
) -> ScanResult:
    """Drain this chat's pending index, compute stats, and record the scan checkpoint."""
    job = None
    if jobs is not None:
        job = await jobs.acquire_lease(
            scope.chat_id,
            JobFamily.MAINTENANCE,
            JobKind.SCAN,
            params={"start": window.start.isoformat(), "end": window.end.isoformat()},
            requested_by=scope.actor_tg_user_id,
        )
        if job is None:
            raise ScanBusy("another maintenance job is running for this group")
    try:
        indexed = await indexer.drain_all(sessions, chat_id=scope.chat_id)
        async with sessions() as session:
            result = await window_stats(session, scope, window, tz=tz)
            result.indexed_now = indexed
            user = scope.actor_tg_user_id if checkpoint_user is None else checkpoint_user
            row = await session.get(ScanCheckpoint, (scope.chat_id, user))
            if row is None:
                row = ScanCheckpoint(
                    chat_id=scope.chat_id,
                    tg_user_id=user,
                    last_scan_start=window.start,
                    last_scan_end=window.end,
                )
                session.add(row)
            row.last_scan_start, row.last_scan_end = window.start, window.end
            row.last_scan_job_id = job.id if job else None
            row.updated_at = utcnow()
            await session.commit()
    except BaseException as exc:
        if jobs is not None and job is not None:
            await jobs.fail(job.id, type(exc).__name__)
        raise
    if jobs is not None and job is not None:
        await jobs.complete(job.id, {"messages": result.messages, "indexed": indexed})
        result.job_id = job.id
    return result


async def last_scan(session: AsyncSession, chat_id: int, tg_user_id: int) -> ScanCheckpoint | None:
    return await session.get(ScanCheckpoint, (chat_id, tg_user_id))
