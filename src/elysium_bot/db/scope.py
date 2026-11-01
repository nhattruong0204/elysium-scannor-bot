from __future__ import annotations

from typing import NamedTuple

from elysium_bot.db.enums import Role


class ChatScope(NamedTuple):
    """Who is acting on which chat. Every chat-data repository requires one (SECURITY §5)."""

    chat_id: int  # internal telegram_chats.id
    actor_tg_user_id: int
    role: Role
