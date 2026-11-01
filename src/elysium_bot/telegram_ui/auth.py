"""Role → permission matrix and checks (SECURITY §2). Checked in services AND at callback time."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from elysium_bot.config import Settings
from elysium_bot.db.access import resolve_role
from elysium_bot.db.enums import ChatStatus, Role
from elysium_bot.db.models import ChatRole, TelegramChat
from elysium_bot.db.scope import ChatScope


class Permission(StrEnum):
    VIEW_STATUS = "view_status"
    VIEW_REPORTS = "view_reports"
    SEARCH = "search"
    EXPORT = "export"
    RUN_AI = "run_ai"
    ASK = "ask"
    IMPORT = "import"
    CURATE_PROJECTS = "curate_projects"
    MANAGE_SETTINGS = "manage_settings"
    MANAGE_SCHEDULES = "manage_schedules"
    MANAGE_ROLES = "manage_roles"
    CANCEL_JOBS = "cancel_jobs"
    # owner-only
    MANAGE_OWNER_SETTINGS = "manage_owner_settings"  # consent policy, retention
    GRANT_ADMIN = "grant_admin"
    DEACTIVATE_CHAT = "deactivate_chat"
    DELETE_DATA = "delete_data"
    OVERRIDE_BUDGET = "override_budget"


OWNER_ONLY = frozenset(
    {
        Permission.MANAGE_OWNER_SETTINGS,
        Permission.GRANT_ADMIN,
        Permission.DEACTIVATE_CHAT,
        Permission.DELETE_DATA,
        Permission.OVERRIDE_BUDGET,
    }
)
_ANALYST = frozenset(
    {
        Permission.VIEW_STATUS,
        Permission.VIEW_REPORTS,
        Permission.SEARCH,
        Permission.RUN_AI,
        Permission.ASK,
    }
)
_VIEWER = frozenset({Permission.VIEW_STATUS, Permission.VIEW_REPORTS})


def permissions_for(
    role: Role | None, *, analyst_can_export: bool = False
) -> frozenset[Permission]:
    if role == Role.OWNER:
        return frozenset(Permission)
    if role == Role.ADMIN:
        return frozenset(Permission) - OWNER_ONLY
    if role == Role.ANALYST:
        return _ANALYST | ({Permission.EXPORT} if analyst_can_export else frozenset())
    if role == Role.VIEWER:
        return _VIEWER
    return frozenset()


class PermissionDenied(PermissionError):
    def __init__(self, permission: Permission, role: Role | None) -> None:
        super().__init__(f"{role or 'no role'} may not {permission.value}")
        self.permission = permission
        self.role = role


@dataclass(frozen=True)
class AccessibleChat:
    chat: TelegramChat
    role: Role


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def can(self, role: Role | None, permission: Permission) -> bool:
        return permission in permissions_for(
            role, analyst_can_export=self.settings.analyst_can_export
        )

    def require(self, scope: ChatScope, permission: Permission) -> None:
        if not self.can(scope.role, permission):
            raise PermissionDenied(permission, scope.role)

    async def scope(self, session: AsyncSession, chat_id: int, tg_user_id: int) -> ChatScope | None:
        """Fresh role lookup (never cached) so revocations apply on the next action."""
        role = await resolve_role(session, self.settings, chat_id, tg_user_id)
        return ChatScope(chat_id, tg_user_id, role) if role is not None else None

    async def accessible_chats(
        self, session: AsyncSession, tg_user_id: int
    ) -> list[AccessibleChat]:
        visible = (ChatStatus.ACTIVE, ChatStatus.PAUSED, ChatStatus.DEACTIVATED)
        if self.settings.is_owner(tg_user_id):
            chats = (
                await session.execute(
                    select(TelegramChat)
                    .where(TelegramChat.status.in_(visible))
                    .order_by(TelegramChat.id)
                )
            ).scalars()
            return [AccessibleChat(c, Role.OWNER) for c in chats]
        rows = await session.execute(
            select(TelegramChat, ChatRole.role)
            .join(ChatRole, ChatRole.chat_id == TelegramChat.id)
            .where(ChatRole.tg_user_id == tg_user_id, TelegramChat.status.in_(visible))
            .order_by(TelegramChat.id)
        )
        return [AccessibleChat(chat, Role(role.value)) for chat, role in rows.all()]
