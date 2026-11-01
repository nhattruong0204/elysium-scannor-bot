from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import time_machine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from elysium_bot.app import Services, _heartbeat_job, _post_init
from elysium_bot.config import Settings
from elysium_bot.db.enums import JobFamily, JobKind, JobStatus
from elysium_bot.db.models import Heartbeat, Job
from elysium_bot.scheduler.leases import JobService
from tests.factories import make_chat

T0 = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)


def _services(engine: AsyncEngine, sf: async_sessionmaker[AsyncSession]) -> Services:
    settings = Settings(_env_file=None, owner_user_ids="1")  # type: ignore[call-arg]
    return Services(settings, engine, sf, JobService(sf, owner_id="new"))


def _fake_app(services: Services, updater_running: bool = True) -> Any:
    return SimpleNamespace(
        bot_data={"services": services, "settings": services.settings},
        job_queue=MagicMock(),
        updater=SimpleNamespace(running=updater_running),
        bot=SimpleNamespace(set_my_commands=AsyncMock(), set_chat_menu_button=AsyncMock()),
    )


async def test_post_init_recovers_leases_beats_and_schedules_jobs(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as s:
        chat = await make_chat(s)
        await s.commit()
    with time_machine.travel(T0, tick=False):
        stale = await JobService(session_factory, owner_id="old").acquire_lease(
            chat.id, JobFamily.AI, JobKind.AI_ANALYSIS
        )
    assert stale is not None

    services = _services(engine, session_factory)
    app = _fake_app(services)
    with time_machine.travel(T0 + timedelta(hours=1), tick=False):
        await _post_init(app)

    names = {call.kwargs["name"] for call in app.job_queue.run_repeating.call_args_list}
    assert names == {"heartbeat", "lease_recovery", "indexer", "flow_sweep"}
    scopes = [type(c.kwargs["scope"]).__name__ for c in app.bot.set_my_commands.call_args_list]
    assert scopes == ["BotCommandScopeAllPrivateChats", "BotCommandScopeAllGroupChats"]
    private = [c.command for c in app.bot.set_my_commands.call_args_list[0].args[0]]
    assert private[:3] == ["menu", "scan", "export"]
    app.bot.set_chat_menu_button.assert_awaited_once()
    app.job_queue.run_once.assert_not_called()  # first start: no downtime alert
    async with session_factory() as s:
        hb = await s.get(Heartbeat, "polling")
        assert hb is not None and hb.beat_at == T0 + timedelta(hours=1)
        job = await s.get(Job, stale.id)
        assert job is not None and job.status == JobStatus.FAILED


async def test_heartbeat_job_skips_when_updater_stopped(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    services = _services(engine, session_factory)
    stopped = SimpleNamespace(application=_fake_app(services, updater_running=False))
    await _heartbeat_job(stopped)  # type: ignore[arg-type]
    async with session_factory() as s:
        assert (await s.execute(select(Heartbeat))).first() is None

    running = SimpleNamespace(application=_fake_app(services, updater_running=True))
    await _heartbeat_job(running)  # type: ignore[arg-type]
    async with session_factory() as s:
        assert await s.get(Heartbeat, "polling") is not None


async def test_downtime_over_24h_records_gap_and_queues_owner_alert(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from elysium_bot.db.enums import ChatStatus, CoverageKind
    from elysium_bot.db.models import CoverageSegment
    from elysium_bot.ops.health import POLLING, beat

    async with session_factory() as s:
        chat = await make_chat(s)
        chat.status = ChatStatus.ACTIVE
        await s.commit()
    with time_machine.travel(T0, tick=False):
        await beat(session_factory, POLLING)
    services = _services(engine, session_factory)
    app = _fake_app(services)
    with time_machine.travel(T0 + timedelta(hours=30), tick=False):
        await _post_init(app)

    async with session_factory() as s:
        gap = (await s.execute(select(CoverageSegment))).scalar_one()
    assert gap.kind == CoverageKind.GAP_SUSPECTED
    assert (gap.start_at, gap.end_at) == (T0, T0 + timedelta(hours=30))
    app.job_queue.run_once.assert_called_once()
    assert "offline for ~30 h" in app.job_queue.run_once.call_args.kwargs["data"]
