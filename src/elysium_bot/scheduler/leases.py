"""Job records with DB-backed leases.

The overlap guard is the partial unique index ``uq_jobs_running_lease_key``: at most one job per
``lease_key`` (chat + family) can be ``running``. Taking a lease is a single INSERT or a single
conditional UPDATE, so correctness never depends on read-then-write.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.enums import JobFamily, JobKind, JobStatus, JobTrigger
from elysium_bot.db.models import Job
from elysium_bot.logging import get_logger
from elysium_bot.timeutil import utcnow

log = get_logger(__name__)

PROCESS_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
DEFAULT_LEASE_TTL = timedelta(minutes=5)
RESUMABLE_KINDS = frozenset(
    {JobKind.IMPORT, JobKind.MTPROTO_BACKFILL, JobKind.AI_ANALYSIS, JobKind.DIGEST_BUILD}
)


def lease_key(chat_id: int | None, family: JobFamily) -> str:
    return f"chat:{chat_id}:{family.value}" if chat_id is not None else f"global:{family.value}"


@dataclass(frozen=True)
class RecoveredJob:
    job_id: int
    kind: JobKind
    requeued_job_id: int | None


class JobService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        owner_id: str = PROCESS_ID,
        lease_ttl: timedelta = DEFAULT_LEASE_TTL,
    ) -> None:
        self._sessions = session_factory
        self.owner_id = owner_id
        self.lease_ttl = lease_ttl

    async def create(
        self,
        chat_id: int | None,
        kind: JobKind,
        family: JobFamily,
        *,
        trigger: JobTrigger = JobTrigger.MANUAL,
        params: dict[str, Any] | None = None,
        requested_by: int | None = None,
    ) -> Job:
        job = Job(
            chat_id=chat_id,
            kind=kind,
            family=family,
            lease_key=lease_key(chat_id, family),
            status=JobStatus.QUEUED,
            trigger=trigger,
            params=params,
            requested_by_tg_user_id=requested_by,
        )
        async with self._sessions() as s:
            s.add(job)
            await s.commit()
        return job

    async def acquire_lease(
        self,
        chat_id: int | None,
        family: JobFamily,
        kind: JobKind,
        *,
        trigger: JobTrigger = JobTrigger.MANUAL,
        params: dict[str, Any] | None = None,
        requested_by: int | None = None,
    ) -> Job | None:
        """Create a job directly in ``running`` state, or return None if the lease is held."""
        now = utcnow()
        job = Job(
            chat_id=chat_id,
            kind=kind,
            family=family,
            lease_key=lease_key(chat_id, family),
            status=JobStatus.RUNNING,
            trigger=trigger,
            params=params,
            requested_by_tg_user_id=requested_by,
            lease_owner=self.owner_id,
            lease_expires_at=now + self.lease_ttl,
            heartbeat_at=now,
            started_at=now,
        )
        async with self._sessions() as s:
            s.add(job)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                log.info("job.lease_busy", chat_id=chat_id, family=family.value, kind=kind.value)
                return None
        return job

    async def start(self, job_id: int) -> Job | None:
        """Move a queued job to running; None if it isn't queued or the lease is held."""
        now = utcnow()
        async with self._sessions() as s:
            try:
                result = await s.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
                    .values(
                        status=JobStatus.RUNNING,
                        lease_owner=self.owner_id,
                        lease_expires_at=now + self.lease_ttl,
                        heartbeat_at=now,
                        started_at=now,
                    )
                )
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return None
            if not getattr(result, "rowcount", 0):
                return None
            return await s.get(Job, job_id)

    async def heartbeat(self, job_id: int, progress: dict[str, Any] | None = None) -> bool:
        now = utcnow()
        values: dict[str, Any] = {"heartbeat_at": now, "lease_expires_at": now + self.lease_ttl}
        if progress is not None:
            values["progress"] = progress
        async with self._sessions() as s:
            result = await s.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.RUNNING,
                    Job.lease_owner == self.owner_id,
                )
                .values(**values)
            )
            await s.commit()
        return bool(getattr(result, "rowcount", 0))

    async def complete(self, job_id: int, result: dict[str, Any] | None = None) -> None:
        await self._finish(job_id, JobStatus.SUCCEEDED, result=result)

    async def fail(self, job_id: int, error: str) -> None:
        await self._finish(job_id, JobStatus.FAILED, error=error)

    async def cancel(self, job_id: int) -> None:
        await self._finish(job_id, JobStatus.CANCELLED)

    async def _finish(
        self,
        job_id: int,
        status: JobStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        async with self._sessions() as s:
            await s.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(
                    status=status,
                    finished_at=utcnow(),
                    lease_expires_at=None,
                    result=result,
                    error=error,
                )
            )
            await s.commit()

    async def record_skipped(
        self,
        chat_id: int | None,
        family: JobFamily,
        kind: JobKind,
        reason: str,
        *,
        trigger: JobTrigger = JobTrigger.SCHEDULE,
    ) -> Job:
        now = utcnow()
        job = Job(
            chat_id=chat_id,
            kind=kind,
            family=family,
            lease_key=lease_key(chat_id, family),
            status=JobStatus.SKIPPED,
            trigger=trigger,
            error=reason,
            started_at=now,
            finished_at=now,
        )
        async with self._sessions() as s:
            s.add(job)
            await s.commit()
        return job

    async def recover_expired(self) -> list[RecoveredJob]:
        """Fail running jobs whose lease expired; re-queue resumable kinds."""
        now = utcnow()
        recovered: list[RecoveredJob] = []
        async with self._sessions() as s:
            expired = (
                await s.execute(
                    select(Job).where(Job.status == JobStatus.RUNNING, Job.lease_expires_at < now)
                )
            ).scalars()
            for job in list(expired):
                job.status = JobStatus.FAILED
                job.error = "lease_expired"
                job.finished_at = now
                job.lease_expires_at = None
                requeued: Job | None = None
                if job.kind in RESUMABLE_KINDS:
                    requeued = Job(
                        chat_id=job.chat_id,
                        kind=job.kind,
                        family=job.family,
                        lease_key=job.lease_key,
                        status=JobStatus.QUEUED,
                        trigger=JobTrigger.STARTUP_RESUME,
                        params={**(job.params or {}), "resume_of": job.id},
                        requested_by_tg_user_id=job.requested_by_tg_user_id,
                    )
                    s.add(requeued)
                await s.flush()
                recovered.append(
                    RecoveredJob(job.id, job.kind, requeued.id if requeued is not None else None)
                )
                log.warning(
                    "job.lease_expired",
                    job_id=job.id,
                    kind=job.kind.value,
                    previous_owner=job.lease_owner,
                    requeued_job_id=requeued.id if requeued else None,
                )
            await s.commit()
        return recovered
