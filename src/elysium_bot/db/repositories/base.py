from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from elysium_bot.db.scope import ChatScope

S = TypeVar("S", bound=Select[Any])


class MissingScopeError(TypeError):
    """Raised when a chat-data repository is built without a ChatScope."""


class BaseRepository:
    """Base for every repository over chat data.

    Subclass methods never take a ``chat_id`` argument: the chat always comes from ``scope``.
    """

    def __init__(self, session: AsyncSession, scope: ChatScope | None) -> None:
        if not isinstance(scope, ChatScope):
            raise MissingScopeError(
                f"{type(self).__name__} requires a ChatScope, got {type(scope).__name__}"
            )
        self.session = session
        self.scope = scope

    def scoped(self, stmt: S, chat_column: InstrumentedAttribute[Any]) -> S:
        return stmt.where(chat_column == self.scope.chat_id)
