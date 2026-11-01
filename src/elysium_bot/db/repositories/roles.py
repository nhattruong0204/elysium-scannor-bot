from __future__ import annotations

from sqlalchemy import delete, select

from elysium_bot.db.enums import StoredRole
from elysium_bot.db.models import ChatRole
from elysium_bot.db.repositories.base import BaseRepository
from elysium_bot.timeutil import utcnow


class ChatRoleRepository(BaseRepository):
    async def get(self, tg_user_id: int) -> StoredRole | None:
        stmt = self.scoped(select(ChatRole.role), ChatRole.chat_id).where(
            ChatRole.tg_user_id == tg_user_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def grant(self, tg_user_id: int, role: StoredRole) -> ChatRole:
        row = await self.session.get(ChatRole, (self.scope.chat_id, tg_user_id))
        if row is None:
            row = ChatRole(chat_id=self.scope.chat_id, tg_user_id=tg_user_id)
            self.session.add(row)
        row.role = role
        row.granted_by_tg_user_id = self.scope.actor_tg_user_id
        row.granted_at = utcnow()
        await self.session.flush()
        return row

    async def revoke(self, tg_user_id: int) -> bool:
        result = await self.session.execute(
            delete(ChatRole).where(
                ChatRole.chat_id == self.scope.chat_id, ChatRole.tg_user_id == tg_user_id
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def list(self) -> list[ChatRole]:
        stmt = self.scoped(select(ChatRole), ChatRole.chat_id).order_by(ChatRole.granted_at)
        return list((await self.session.execute(stmt)).scalars())
