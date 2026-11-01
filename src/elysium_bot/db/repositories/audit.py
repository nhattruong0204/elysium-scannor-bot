from __future__ import annotations

from typing import Any

from sqlalchemy import select

from elysium_bot.db.enums import AuditAction
from elysium_bot.db.models import AuditLog
from elysium_bot.db.repositories.base import BaseRepository
from elysium_bot.logging import redact


class AuditLogRepository(BaseRepository):
    """Append-only audit trail for privileged actions."""

    async def record(self, action: AuditAction, params: dict[str, Any] | None = None) -> AuditLog:
        clean = {k: redact(v) if isinstance(v, str) else v for k, v in (params or {}).items()}
        row = AuditLog(
            chat_id=self.scope.chat_id,
            actor_tg_user_id=self.scope.actor_tg_user_id,
            action=action,
            params=clean or None,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def recent(self, limit: int = 50) -> list[AuditLog]:
        stmt = (
            self.scoped(select(AuditLog), AuditLog.chat_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())
