"""Chat-scoped repositories. Every class here must subclass ``BaseRepository``."""

from elysium_bot.db.repositories.audit import AuditLogRepository
from elysium_bot.db.repositories.base import BaseRepository, MissingScopeError
from elysium_bot.db.repositories.roles import ChatRoleRepository

__all__ = ["AuditLogRepository", "BaseRepository", "ChatRoleRepository", "MissingScopeError"]
