from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import time_machine
from sqlalchemy import select
from typer.testing import CliRunner

from elysium_bot import __version__
from elysium_bot.cli import app
from elysium_bot.db.enums import AuditAction, StoredRole
from elysium_bot.db.models import AuditLog, ChatRole
from elysium_bot.db.session import create_engine, create_session_factory
from elysium_bot.ops.health import POLLING, beat
from tests.factories import make_chat

runner = CliRunner()
T0 = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("ELYSIUM_DATABASE_URL", url)
    monkeypatch.setenv("ELYSIUM_OWNER_USER_IDS", "111")
    monkeypatch.setenv("ELYSIUM_LOG_FORMAT", "console")
    monkeypatch.chdir(tmp_path)  # no stray .env
    return url


def _db(url: str, coro_fn):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        engine = create_engine(url)
        try:
            return await coro_fn(create_session_factory(engine))
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_db_upgrade_and_downgrade(db_url: str) -> None:
    result = runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert "upgraded to head" in result.output
    result = runner.invoke(app, ["db", "downgrade", "--revision", "base"])
    assert result.exit_code == 0, result.output


def test_healthcheck_exit_codes(db_url: str) -> None:
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    with time_machine.travel(T0, tick=False) as t:
        _db(db_url, lambda sf: beat(sf, POLLING))
        t.shift(timedelta(seconds=30))
        fresh = runner.invoke(app, ["healthcheck"])
        t.shift(timedelta(seconds=300))
        stale = runner.invoke(app, ["healthcheck"])
    assert fresh.exit_code == 0, fresh.output
    assert stale.exit_code == 1
    assert "stale" in stale.output


def test_healthcheck_fails_without_database_schema(db_url: str) -> None:
    result = runner.invoke(app, ["healthcheck"])
    assert result.exit_code == 1


def test_grant_role_writes_role_and_audit(db_url: str) -> None:
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

    async def seed(sf):  # type: ignore[no-untyped-def]
        async with sf() as s:
            await make_chat(s, tg_chat_id=-1009)
            await s.commit()

    _db(db_url, seed)
    result = runner.invoke(
        app, ["grant-role", "--chat", "-1009", "--user", "555", "--role", "analyst"]
    )
    assert result.exit_code == 0, result.output

    async def read(sf):  # type: ignore[no-untyped-def]
        async with sf() as s:
            role = (await s.execute(select(ChatRole))).scalar_one()
            audit = (await s.execute(select(AuditLog))).scalar_one()
            return role, audit

    role, audit = _db(db_url, read)
    assert (role.tg_user_id, role.role, role.granted_by_tg_user_id) == (
        555,
        StoredRole.ANALYST,
        111,
    )
    assert audit.action == AuditAction.ROLE_CHANGE
    assert audit.actor_tg_user_id == 111


def test_grant_role_rejects_non_owner_and_unknown_chat(db_url: str) -> None:
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    not_owner = runner.invoke(
        app, ["grant-role", "--chat", "-1", "--user", "5", "--role", "admin", "--by", "999"]
    )
    assert not_owner.exit_code == 2
    unknown = runner.invoke(app, ["grant-role", "--chat", "-42", "--user", "5", "--role", "admin"])
    assert unknown.exit_code == 2
    assert "not registered" in unknown.output


def test_run_without_token_exits_with_config_error(db_url: str) -> None:
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2
    assert "TELEGRAM_BOT_TOKEN" in result.output
