"""Server-side UI flow state behind callback tokens (``ui_flows``; TELEGRAM_UX §3)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.models import UiFlow
from elysium_bot.timeutil import utcnow

FLOW_TTL = timedelta(minutes=30)


class FlowError(Exception):
    pass


class FlowExpired(FlowError):
    pass


class FlowForbidden(FlowError):
    pass


@dataclass
class Flow:
    token: str
    tg_user_id: int
    chat_id: int | None
    flow: str
    state: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None


def new_token() -> str:
    return secrets.token_urlsafe(6)[:8]


class FlowStore:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], ttl: timedelta = FLOW_TTL
    ) -> None:
        self._sessions = sessions
        self.ttl = ttl

    async def create(
        self, tg_user_id: int, chat_id: int | None, flow: str, state: dict[str, Any] | None = None
    ) -> Flow:
        now = utcnow()
        async with self._sessions() as s:
            for _ in range(5):
                token = new_token()
                if await s.get(UiFlow, token) is None:
                    break
            row = UiFlow(
                token=token,
                tg_user_id=tg_user_id,
                chat_id=chat_id,
                flow=flow,
                state=state or {},
                expires_at=now + self.ttl,
            )
            s.add(row)
            await s.commit()
        return Flow(token, tg_user_id, chat_id, flow, dict(state or {}), row.expires_at)

    async def load(self, token: str, tg_user_id: int) -> Flow:
        """Load and refresh a flow. Raises FlowExpired / FlowForbidden."""
        now = utcnow()
        async with self._sessions() as s:
            row = await s.get(UiFlow, token)
            if row is None or row.expires_at <= now:
                raise FlowExpired(token)
            if row.tg_user_id != tg_user_id:
                raise FlowForbidden(token)
            row.expires_at = now + self.ttl
            await s.commit()
            return Flow(
                row.token,
                row.tg_user_id,
                row.chat_id,
                row.flow,
                dict(row.state or {}),
                row.expires_at,
            )

    async def save(self, flow: Flow) -> None:
        async with self._sessions() as s:
            row = await s.get(UiFlow, flow.token)
            if row is None:
                raise FlowExpired(flow.token)
            row.state = dict(flow.state)
            row.chat_id = flow.chat_id
            row.expires_at = utcnow() + self.ttl
            await s.commit()

    async def find_pending_input(self, tg_user_id: int) -> Flow | None:
        """The user's most recent live flow that is waiting for a typed reply."""
        now = utcnow()
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(UiFlow)
                    .where(UiFlow.tg_user_id == tg_user_id, UiFlow.expires_at > now)
                    .order_by(UiFlow.expires_at.desc())
                )
            ).scalars()
            for row in rows:
                if (row.state or {}).get("pending"):
                    return Flow(
                        row.token,
                        row.tg_user_id,
                        row.chat_id,
                        row.flow,
                        dict(row.state or {}),
                        row.expires_at,
                    )
        return None

    async def sweep(self) -> int:
        async with self._sessions() as s:
            result = await s.execute(delete(UiFlow).where(UiFlow.expires_at <= utcnow()))
            await s.commit()
            return int(getattr(result, "rowcount", 0) or 0)
