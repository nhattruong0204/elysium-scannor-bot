"""Chat registry lookups and role resolution.

These are the only unscoped reads: they are how a ``ChatScope`` gets built in the first place.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from elysium_bot.config import Settings
from elysium_bot.db.enums import Role
from elysium_bot.db.models import ChatRole, TelegramChat
from elysium_bot.db.scope import ChatScope


async def get_chat_by_tg_id(session: AsyncSession, tg_chat_id: int) -> TelegramChat | None:
    stmt = select(TelegramChat).where(TelegramChat.tg_chat_id == tg_chat_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def resolve_role(
    session: AsyncSession, settings: Settings, chat_id: int, tg_user_id: int
) -> Role | None:
    if settings.is_owner(tg_user_id):
        return Role.OWNER
    stmt = select(ChatRole.role).where(
        ChatRole.chat_id == chat_id, ChatRole.tg_user_id == tg_user_id
    )
    stored = (await session.execute(stmt)).scalar_one_or_none()
    return Role(stored.value) if stored is not None else None


async def build_scope(
    session: AsyncSession, settings: Settings, chat_id: int, tg_user_id: int
) -> ChatScope | None:
    role = await resolve_role(session, settings, chat_id, tg_user_id)
    return ChatScope(chat_id, tg_user_id, role) if role is not None else None
