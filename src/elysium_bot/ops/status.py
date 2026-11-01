"""Read-only status snapshot for the control center. Every number comes from the DB."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from elysium_bot.config import Settings
from elysium_bot.db.enums import (
    ChatStatus,
    CoverageKind,
    ImportStatus,
    IndexState,
    JobStatus,
    Source,
)
from elysium_bot.db.models import (
    CoverageSegment,
    Heartbeat,
    ImportRun,
    Job,
    Message,
    TelegramChat,
)
from elysium_bot.ingestion.coverage import load_utc
from elysium_bot.ops.health import POLLING
from elysium_bot.timeutil import utcnow


@dataclass(frozen=True)
class JobInfo:
    id: int
    kind: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    progress: dict[str, object] | None
    error: str | None


@dataclass
class StatusSnapshot:
    chat_id: int
    chat_title: str | None
    chat_status: ChatStatus
    messages_stored: int
    earliest_at: datetime | None
    latest_at: datetime | None
    polling_beat_at: datetime | None
    polling_fresh: bool
    succeeded_imports: int
    last_import_at: datetime | None
    suspected_gaps: int
    index_pending: int
    index_failed: int
    running_jobs: list[JobInfo] = field(default_factory=list)
    recent_failures: list[JobInfo] = field(default_factory=list)
    ai_configured: bool = False
    mtproto_enabled: bool = False
    onchain_configured: bool = False
    projects_tracked: int | None = None  # Phase 6

    @property
    def has_history_import(self) -> bool:
        return self.succeeded_imports > 0


def _job(j: Job) -> JobInfo:
    return JobInfo(
        j.id, j.kind.value, j.status.value, j.started_at, j.finished_at, j.progress, j.error
    )


async def snapshot(
    session: AsyncSession, settings: Settings, chat: TelegramChat, now: datetime | None = None
) -> StatusSnapshot:
    now = now or utcnow()
    count, earliest, latest = (
        await session.execute(
            select(func.count(), func.min(Message.sent_at), func.max(Message.sent_at)).where(
                Message.chat_id == chat.id
            )
        )
    ).one()
    beat = await session.get(Heartbeat, POLLING)
    imports, last_import = (
        await session.execute(
            select(func.count(), func.max(ImportRun.finished_at)).where(
                ImportRun.chat_id == chat.id,
                ImportRun.source == Source.DESKTOP_EXPORT,
                ImportRun.status.in_([ImportStatus.SUCCEEDED, ImportStatus.PARTIAL]),
            )
        )
    ).one()
    gaps = (
        await session.execute(
            select(func.count()).where(
                CoverageSegment.chat_id == chat.id,
                CoverageSegment.kind == CoverageKind.GAP_SUSPECTED,
            )
        )
    ).scalar_one()
    index_counts = dict(
        (
            await session.execute(
                select(Message.index_state, func.count())
                .where(Message.chat_id == chat.id, Message.index_state != IndexState.INDEXED)
                .group_by(Message.index_state)
            )
        ).all()
    )
    running = (
        await session.execute(
            select(Job)
            .where(Job.chat_id == chat.id, Job.status == JobStatus.RUNNING)
            .order_by(Job.id)
        )
    ).scalars()
    failures = (
        await session.execute(
            select(Job)
            .where(Job.chat_id == chat.id, Job.status == JobStatus.FAILED)
            .order_by(Job.id.desc())
            .limit(5)
        )
    ).scalars()
    return StatusSnapshot(
        chat_id=chat.id,
        chat_title=chat.title,
        chat_status=chat.status,
        messages_stored=int(count),
        earliest_at=load_utc(earliest),
        latest_at=load_utc(latest),
        polling_beat_at=beat.beat_at if beat else None,
        polling_fresh=bool(
            beat and now - beat.beat_at <= timedelta(seconds=settings.heartbeat_max_age_s)
        ),
        succeeded_imports=int(imports),
        last_import_at=load_utc(last_import),
        suspected_gaps=int(gaps),
        index_pending=int(index_counts.get(IndexState.PENDING, 0)),
        index_failed=int(index_counts.get(IndexState.FAILED, 0)),
        running_jobs=[_job(j) for j in running],
        recent_failures=[_job(j) for j in failures],
        ai_configured=settings.anthropic_api_key is not None,
        mtproto_enabled=settings.mtproto_enabled,
        onchain_configured=bool(settings.onchain_providers_json),
    )


async def schema_revision(session: AsyncSession) -> str | None:
    try:
        return (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
    except Exception:
        return None


async def database_size_bytes(session: AsyncSession) -> int | None:
    dialect = session.get_bind().dialect.name
    try:
        if dialect == "sqlite":
            pages = (await session.execute(text("PRAGMA page_count"))).scalar_one()
            size = (await session.execute(text("PRAGMA page_size"))).scalar_one()
            return int(pages) * int(size)
        if dialect == "postgresql":
            return int(
                (
                    await session.execute(text("SELECT pg_database_size(current_database())"))
                ).scalar_one()
            )
    except Exception:
        return None
    return None
