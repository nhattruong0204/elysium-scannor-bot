"""Schedule registry (ARCHITECTURE §5.3).

The DB (``schedules``) is the source of truth; PTB's JobQueue only holds timers. On startup every
enabled schedule is re-registered. A schedule whose ``next_run_at`` passed during downtime fires
once immediately (coalesced). Each firing takes a DB lease; if the family's lease is held the
firing is recorded as ``skipped``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.enums import JobFamily, JobKind, JobTrigger, ScheduleKind
from elysium_bot.db.models import Schedule
from elysium_bot.ingestion import indexer
from elysium_bot.logging import get_logger
from elysium_bot.scheduler.leases import JobService
from elysium_bot.timeutil import utcnow

log = get_logger(__name__)

# Kinds executed in this phase. AI summaries are registered from Phase 5.
EXECUTABLE = {ScheduleKind.COLLECTION: (JobFamily.INGEST, JobKind.SCAN)}
JOB_KWARGS = {"coalesce": True, "max_instances": 1, "misfire_grace_time": None}

Executor = Callable[[Schedule], Awaitable[dict[str, Any]]]


def job_name(schedule_id: int) -> str:
    return f"schedule:{schedule_id}"


class ScheduleRegistry:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        jobs: JobService,
        job_queue: Any,
        *,
        mtproto_enabled: bool = False,
    ) -> None:
        self.sessions = sessions
        self.jobs = jobs
        self.job_queue = job_queue
        self.mtproto_enabled = mtproto_enabled
        self.executors: dict[ScheduleKind, Executor] = {ScheduleKind.COLLECTION: self._collect}

    async def load_all(self) -> int:
        async with self.sessions() as s:
            rows = list(
                (await s.execute(select(Schedule).where(Schedule.enabled.is_(True)))).scalars()
            )
        count = 0
        for row in rows:
            if self.register(row):
                count += 1
        log.info("schedules.loaded", count=count)
        return count

    def register(self, row: Schedule) -> bool:
        """(Re)register one schedule's timer. Returns False if it is disabled or not executable."""
        for existing in self.job_queue.get_jobs_by_name(job_name(row.id)):
            existing.schedule_removal()
        if not row.enabled or not row.interval_min or row.kind not in EXECUTABLE:
            return False
        interval = timedelta(minutes=row.interval_min)
        now = utcnow()
        if row.next_run_at is None:
            first = interval
        elif row.next_run_at <= now:
            first = timedelta(0)  # missed during downtime: run once now (coalesced)
        else:
            first = row.next_run_at - now
        self.job_queue.run_repeating(
            self._callback,
            interval=interval,
            first=first,
            name=job_name(row.id),
            data=row.id,
            job_kwargs=JOB_KWARGS,
        )
        return True

    async def _callback(self, context: Any) -> None:
        await self.fire(int(context.job.data))

    async def fire(self, schedule_id: int) -> dict[str, Any] | None:
        async with self.sessions() as s:
            row = await s.get(Schedule, schedule_id)
            if row is None or not row.enabled or row.kind not in EXECUTABLE:
                return None
            family, kind = EXECUTABLE[row.kind]
            chat_id = row.chat_id
            now = utcnow()
            row.last_run_at = now
            row.next_run_at = now + timedelta(minutes=row.interval_min or 60)
            await s.commit()
        job = await self.jobs.acquire_lease(
            chat_id, family, kind, trigger=JobTrigger.SCHEDULE, params={"schedule_id": schedule_id}
        )
        if job is None:
            skipped = await self.jobs.record_skipped(
                chat_id, family, kind, "lease held by another job"
            )
            await self._set_last_job(schedule_id, skipped.id)
            log.info("schedule.skipped", schedule_id=schedule_id)
            return {"skipped": True}
        try:
            async with self.sessions() as s:
                row = await s.get(Schedule, schedule_id)
                assert row is not None
            result = await self.executors[row.kind](row)
        except Exception as exc:
            await self.jobs.fail(job.id, f"{type(exc).__name__}: {exc}")
            log.exception("schedule.failed", schedule_id=schedule_id)
            await self._set_last_job(schedule_id, job.id)
            return None
        await self.jobs.complete(job.id, result)
        await self._set_last_job(schedule_id, job.id)
        return result

    async def _set_last_job(self, schedule_id: int, job_id: int) -> None:
        async with self.sessions() as s:
            row = await s.get(Schedule, schedule_id)
            if row is not None:
                row.last_job_id = job_id
                await s.commit()

    async def _collect(self, row: Schedule) -> dict[str, Any]:
        indexed = await indexer.drain_all(self.sessions, chat_id=row.chat_id)
        result: dict[str, Any] = {"indexed": indexed}
        # MTProto gap-fill needs an interactive-login session and runs via the CLI in this phase.
        result["mtproto"] = (
            "use `elysium-bot mtproto backfill`" if self.mtproto_enabled else "disabled"
        )
        return result
