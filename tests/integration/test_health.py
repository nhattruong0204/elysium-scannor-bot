from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import time_machine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.session import create_engine, create_session_factory
from elysium_bot.ops.health import POLLING, beat, check_health

T0 = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
MAX_AGE = timedelta(seconds=120)


async def test_fresh_heartbeat_is_healthy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with time_machine.travel(T0, tick=False) as t:
        await beat(session_factory, POLLING, {"updater_running": True})
        t.shift(timedelta(seconds=60))
        result = await check_health(session_factory, max_age=MAX_AGE)
    assert result.ok, result.problems


async def test_stale_heartbeat_is_unhealthy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with time_machine.travel(T0, tick=False) as t:
        await beat(session_factory, POLLING)
        t.shift(timedelta(seconds=200))
        result = await check_health(session_factory, max_age=MAX_AGE)
    assert not result.ok
    assert "stale (200s old)" in result.problems[0]


async def test_missing_heartbeat_is_unhealthy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await check_health(session_factory, max_age=MAX_AGE)
    assert not result.ok
    assert "no heartbeat" in result.problems[0]


async def test_unmigrated_database_is_unhealthy(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    try:
        result = await check_health(create_session_factory(engine), max_age=MAX_AGE)
    finally:
        await engine.dispose()
    assert not result.ok
    assert "database unreachable" in result.problems[0]
