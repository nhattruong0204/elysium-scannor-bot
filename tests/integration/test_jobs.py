from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import time_machine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.enums import JobFamily, JobKind, JobStatus, JobTrigger
from elysium_bot.db.models import Job
from elysium_bot.scheduler.leases import JobService, lease_key
from tests.factories import make_chat

T0 = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)


async def _chat_id(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as s:
        chat = await make_chat(s)
        await s.commit()
        return chat.id


def test_lease_key() -> None:
    assert lease_key(3, JobFamily.AI) == "chat:3:ai"
    assert lease_key(None, JobFamily.MAINTENANCE) == "global:maintenance"


async def test_concurrent_acquire_only_one_wins(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat_id = await _chat_id(session_factory)
    a = JobService(session_factory, owner_id="proc-a")
    b = JobService(session_factory, owner_id="proc-b")
    results = await asyncio.gather(
        a.acquire_lease(chat_id, JobFamily.AI, JobKind.AI_ANALYSIS),
        b.acquire_lease(chat_id, JobFamily.AI, JobKind.SCHEDULED_SUMMARY),
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    async with session_factory() as s:
        running = (await s.execute(select(Job).where(Job.status == JobStatus.RUNNING))).scalars()
        assert len(list(running)) == 1


async def test_different_families_and_chats_do_not_conflict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat_id = await _chat_id(session_factory)
    svc = JobService(session_factory)
    assert await svc.acquire_lease(chat_id, JobFamily.AI, JobKind.AI_ANALYSIS)
    assert await svc.acquire_lease(chat_id, JobFamily.INGEST, JobKind.IMPORT)
    assert await svc.acquire_lease(None, JobFamily.AI, JobKind.DIGEST_BUILD)


async def test_lease_is_released_on_complete_and_fail(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat_id = await _chat_id(session_factory)
    svc = JobService(session_factory)
    job = await svc.acquire_lease(chat_id, JobFamily.EXPORT, JobKind.EXPORT)
    assert job is not None
    assert await svc.acquire_lease(chat_id, JobFamily.EXPORT, JobKind.EXPORT) is None
    await svc.complete(job.id, {"rows": 10})
    job2 = await svc.acquire_lease(chat_id, JobFamily.EXPORT, JobKind.EXPORT)
    assert job2 is not None
    await svc.fail(job2.id, "boom")
    async with session_factory() as s:
        done = await s.get(Job, job.id)
        failed = await s.get(Job, job2.id)
        assert done is not None and done.status == JobStatus.SUCCEEDED
        assert done.result == {"rows": 10} and done.finished_at is not None
        assert failed is not None and failed.status == JobStatus.FAILED
        assert failed.error == "boom"


async def test_start_queued_job_respects_guard(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat_id = await _chat_id(session_factory)
    svc = JobService(session_factory)
    queued = await svc.create(chat_id, JobKind.IMPORT, JobFamily.INGEST)
    assert queued.status == JobStatus.QUEUED
    blocker = await svc.acquire_lease(chat_id, JobFamily.INGEST, JobKind.MTPROTO_BACKFILL)
    assert blocker is not None
    assert await svc.start(queued.id) is None
    await svc.complete(blocker.id)
    started = await svc.start(queued.id)
    assert started is not None and started.status == JobStatus.RUNNING


async def test_heartbeat_extends_lease_only_for_owner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat_id = await _chat_id(session_factory)
    owner = JobService(session_factory, owner_id="me", lease_ttl=timedelta(minutes=5))
    other = JobService(session_factory, owner_id="someone-else")
    with time_machine.travel(T0, tick=False) as traveller:
        job = await owner.acquire_lease(chat_id, JobFamily.AI, JobKind.AI_ANALYSIS)
        assert job is not None
        traveller.shift(timedelta(minutes=4))
        assert await owner.heartbeat(job.id, {"done": 3, "total": 7})
        assert not await other.heartbeat(job.id)
    async with session_factory() as s:
        row = await s.get(Job, job.id)
        assert row is not None
        assert row.lease_expires_at == T0 + timedelta(minutes=9)
        assert row.progress == {"done": 3, "total": 7}


async def test_expired_lease_recovered_and_resumable_requeued(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chat_id = await _chat_id(session_factory)
    dead = JobService(session_factory, owner_id="dead-process", lease_ttl=timedelta(minutes=5))
    with time_machine.travel(T0, tick=False):
        importing = await dead.acquire_lease(
            chat_id, JobFamily.INGEST, JobKind.IMPORT, params={"file": "result.json"}
        )
        exporting = await dead.acquire_lease(chat_id, JobFamily.EXPORT, JobKind.EXPORT)
        fresh = await dead.acquire_lease(chat_id, JobFamily.AI, JobKind.AI_ANALYSIS)
    assert importing and exporting and fresh

    survivor = JobService(session_factory, owner_id="new-process")
    with time_machine.travel(T0 + timedelta(minutes=10), tick=False):
        # Keep one lease alive: it must not be recovered.
        await JobService(session_factory, owner_id="dead-process").heartbeat(fresh.id)
        recovered = await survivor.recover_expired()

    assert {r.job_id for r in recovered} == {importing.id, exporting.id}
    async with session_factory() as s:
        imp = await s.get(Job, importing.id)
        exp = await s.get(Job, exporting.id)
        alive = await s.get(Job, fresh.id)
        assert imp is not None and imp.status == JobStatus.FAILED and imp.error == "lease_expired"
        assert exp is not None and exp.status == JobStatus.FAILED
        assert alive is not None and alive.status == JobStatus.RUNNING
        requeued = (
            await s.execute(select(Job).where(Job.trigger == JobTrigger.STARTUP_RESUME))
        ).scalar_one()
        assert requeued.kind == JobKind.IMPORT
        assert requeued.status == JobStatus.QUEUED
        assert requeued.params == {"file": "result.json", "resume_of": importing.id}
    # The ingest family is free again (the failed job no longer holds the guard).
    assert await survivor.acquire_lease(chat_id, JobFamily.INGEST, JobKind.IMPORT) is not None


async def test_record_skipped(session_factory: async_sessionmaker[AsyncSession]) -> None:
    chat_id = await _chat_id(session_factory)
    svc = JobService(session_factory)
    job = await svc.record_skipped(chat_id, JobFamily.AI, JobKind.SCHEDULED_SUMMARY, "lease held")
    assert job.status == JobStatus.SKIPPED
    assert job.error == "lease held"
    assert job.trigger == JobTrigger.SCHEDULE
