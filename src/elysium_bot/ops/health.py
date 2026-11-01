"""Component heartbeats and the health check used by Docker's HEALTHCHECK."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.models import Heartbeat
from elysium_bot.timeutil import utcnow

POLLING = "polling"


async def beat(
    session_factory: async_sessionmaker[AsyncSession],
    component: str,
    detail: dict[str, Any] | None = None,
) -> None:
    async with session_factory() as s:
        await s.merge(Heartbeat(component=component, beat_at=utcnow(), detail=detail))
        await s.commit()


@dataclass
class HealthResult:
    ok: bool
    problems: list[str] = field(default_factory=list)


async def check_health(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    max_age: timedelta,
    components: tuple[str, ...] = (POLLING,),
) -> HealthResult:
    problems: list[str] = []
    try:
        async with session_factory() as s:
            await s.execute(text("SELECT 1"))
            rows = (
                await s.execute(select(Heartbeat).where(Heartbeat.component.in_(components)))
            ).scalars()
            beats = {row.component: row for row in rows}
    except Exception as exc:  # any DB failure means unhealthy
        return HealthResult(False, [f"database unreachable: {type(exc).__name__}"])

    now = utcnow()
    for component in components:
        hb = beats.get(component)
        if hb is None:
            problems.append(f"{component}: no heartbeat recorded")
        elif now - hb.beat_at > max_age:
            age = int((now - hb.beat_at).total_seconds())
            problems.append(f"{component}: heartbeat stale ({age}s old)")
    return HealthResult(not problems, problems)
