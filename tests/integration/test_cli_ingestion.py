from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from elysium_bot.cli import app
from elysium_bot.db.enums import ChatStatus, ChatType
from elysium_bot.db.models import TelegramChat
from elysium_bot.db.session import create_engine, create_session_factory
from tests.fixtures import exports as fx

runner = CliRunner()


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}"
    for key, value in {
        "ELYSIUM_DATABASE_URL": url,
        "ELYSIUM_OWNER_USER_IDS": "111",
        "ELYSIUM_LOG_FORMAT": "console",
        "ELYSIUM_LOG_LEVEL": "WARNING",
        "ELYSIUM_DATA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    return url


def register(url: str) -> None:
    async def go() -> None:
        engine = create_engine(url)
        async with create_session_factory(engine)() as s:
            s.add(
                TelegramChat(
                    tg_chat_id=fx.SUPERGROUP_TG_ID,
                    type=ChatType.SUPERGROUP,
                    title="Synthetic",
                    status=ChatStatus.ACTIVE,
                )
            )
            await s.commit()
        await engine.dispose()

    asyncio.run(go())


def test_import_export_and_coverage(db_url: str, tmp_path: Path) -> None:
    register(db_url)
    path = fx.write_json(tmp_path / "result.json", fx.chat_export(fx.supergroup_messages(10)))
    result = runner.invoke(app, ["import-export", str(path)])
    assert result.exit_code == 0, result.output
    assert "SUCCEEDED" in result.output and "new 12" in result.output

    again = runner.invoke(app, ["import-export", str(path), "--chat", str(fx.SUPERGROUP_TG_ID)])
    assert again.exit_code == 0 and "already imported" in again.output

    cov = runner.invoke(app, ["coverage", "--chat", str(fx.SUPERGROUP_TG_ID)])
    assert cov.exit_code == 0, cov.output
    assert "Messages in window: 12" in cov.output
    assert "source desktop_export: 12" in cov.output


def test_import_export_errors(db_url: str, tmp_path: Path) -> None:
    path = fx.write_json(tmp_path / "result.json", fx.chat_export(fx.supergroup_messages(10)))
    unregistered = runner.invoke(app, ["import-export", str(path)])
    assert unregistered.exit_code == 2 and "activate" in unregistered.output
    missing = runner.invoke(app, ["coverage", "--chat", "-42"])
    assert missing.exit_code == 2


def test_mtproto_commands_require_opt_in(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    disabled = runner.invoke(app, ["mtproto", "status"])
    assert disabled.exit_code == 2 and "disabled" in disabled.output
    monkeypatch.setenv("ELYSIUM_MTPROTO_ENABLED", "true")
    incomplete = runner.invoke(app, ["mtproto", "backfill", "--chat", "-1"])
    assert incomplete.exit_code == 2 and "ELYSIUM_TELEGRAM_API_ID" in incomplete.output
