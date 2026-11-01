from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from telegram.ext import CommandHandler

from elysium_bot.app import ALLOWED_UPDATES, ConfigurationError, build_application, build_services
from elysium_bot.config import Settings

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmn"


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    env: dict[str, Any] = {
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "owner_user_ids": "111",
        **overrides,
    }
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


async def test_build_application_without_token_fails(tmp_path: Path) -> None:
    services = build_services(settings(tmp_path))
    try:
        with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN"):
            build_application(services)
    finally:
        await services.engine.dispose()


async def test_build_application_wires_handlers_and_rate_limiter(tmp_path: Path) -> None:
    services = build_services(settings(tmp_path, telegram_bot_token=FAKE_TOKEN))
    try:
        app = build_application(services)
        handlers = [h for group in app.handlers.values() for h in group]
        start = [h for h in handlers if isinstance(h, CommandHandler) and "start" in h.commands]
        assert len(start) == 1  # /start opens the control center (Phase 3)
        assert any(isinstance(h, CommandHandler) and "menu" in h.commands for h in handlers)
        assert app.bot_data["services"] is services
        assert app.bot.rate_limiter is not None
        assert app.job_queue is not None
    finally:
        await services.engine.dispose()


def test_allowed_updates_cover_collection_needs() -> None:
    assert set(ALLOWED_UPDATES) == {
        "message",
        "edited_message",
        "my_chat_member",
        "chat_member",
        "callback_query",
    }
