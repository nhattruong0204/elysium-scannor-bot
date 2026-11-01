from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import elysium_bot.db.repositories as repo_pkg
from elysium_bot.config import Settings
from elysium_bot.db.access import resolve_role
from elysium_bot.db.enums import AuditAction, Role, StoredRole
from elysium_bot.db.repositories import AuditLogRepository, ChatRoleRepository
from elysium_bot.db.repositories.base import BaseRepository, MissingScopeError
from elysium_bot.db.scope import ChatScope
from tests.factories import make_chat


def all_repository_classes() -> list[type]:
    found: dict[str, type] = {}
    for info in pkgutil.walk_packages(repo_pkg.__path__, prefix=f"{repo_pkg.__name__}."):
        module = importlib.import_module(info.name)
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ == module.__name__ and name.endswith("Repository"):
                found[f"{module.__name__}.{name}"] = obj
    return list(found.values())


def test_repository_discovery_finds_classes() -> None:
    names = {cls.__name__ for cls in all_repository_classes()}
    assert {"ChatRoleRepository", "AuditLogRepository"} <= names


@pytest.mark.parametrize("cls", all_repository_classes(), ids=lambda c: c.__name__)
def test_every_repository_requires_scope(cls: type, session: AsyncSession) -> None:
    if cls is BaseRepository:
        pytest.skip("abstract base")
    assert issubclass(cls, BaseRepository), f"{cls.__name__} must subclass BaseRepository"
    with pytest.raises(MissingScopeError):
        cls(session, None)
    with pytest.raises(MissingScopeError):
        cls(session, {"chat_id": 1})  # not a ChatScope


def test_base_repository_cannot_be_constructed_unscoped(session: AsyncSession) -> None:
    with pytest.raises(MissingScopeError):
        BaseRepository(session, None)  # type: ignore[arg-type]


async def test_roles_and_audit_are_chat_scoped(session: AsyncSession) -> None:
    chat_a = await make_chat(session, tg_chat_id=-1001)
    chat_b = await make_chat(session, tg_chat_id=-1002)
    scope_a = ChatScope(chat_id=chat_a.id, actor_tg_user_id=1, role=Role.OWNER)
    scope_b = ChatScope(chat_id=chat_b.id, actor_tg_user_id=1, role=Role.OWNER)

    await ChatRoleRepository(session, scope_a).grant(tg_user_id=55, role=StoredRole.ANALYST)
    await AuditLogRepository(session, scope_a).record(AuditAction.ROLE_CHANGE, {"user": 55})
    await session.commit()

    assert await ChatRoleRepository(session, scope_a).get(55) == StoredRole.ANALYST
    assert await ChatRoleRepository(session, scope_b).get(55) is None
    assert len(await AuditLogRepository(session, scope_a).recent()) == 1
    assert await AuditLogRepository(session, scope_b).recent() == []


async def test_grant_updates_existing_role_and_revoke(session: AsyncSession) -> None:
    chat = await make_chat(session)
    repo = ChatRoleRepository(session, ChatScope(chat.id, 1, Role.OWNER))
    await repo.grant(tg_user_id=9, role=StoredRole.VIEWER)
    await repo.grant(tg_user_id=9, role=StoredRole.ADMIN)
    await session.commit()
    assert await repo.get(9) == StoredRole.ADMIN
    assert await repo.revoke(9) is True
    assert await repo.get(9) is None


async def test_resolve_role(session: AsyncSession) -> None:
    chat = await make_chat(session)
    await ChatRoleRepository(session, ChatScope(chat.id, 1, Role.OWNER)).grant(7, StoredRole.ADMIN)
    await session.commit()
    settings = Settings(_env_file=None, owner_user_ids="1")  # type: ignore[call-arg]
    assert await resolve_role(session, settings, chat.id, 1) == Role.OWNER
    assert await resolve_role(session, settings, chat.id, 7) == Role.ADMIN
    assert await resolve_role(session, settings, chat.id, 8) is None
