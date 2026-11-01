from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram.ext import CommandHandler

from elysium_bot.app import ALLOWED_UPDATES, ConfigurationError, build_application, build_services
from elysium_bot.config import Settings
from elysium_bot.telegram_ui.start import NON_OWNER_TEXT, OWNER_TEXT, start_command

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmn"


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    env: dict[str, Any] = {
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        "owner_user_ids": "111",
        **overrides,
    }
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


def fake_update(user_id: int | None) -> tuple[Any, AsyncMock]:
    reply = AsyncMock()
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    update = SimpleNamespace(
        effective_message=SimpleNamespace(reply_text=reply), effective_user=user
    )
    return update, reply


@pytest.mark.parametrize(
    ("user_id", "expected"),
    [(111, OWNER_TEXT), (222, NON_OWNER_TEXT), (None, NON_OWNER_TEXT)],
)
async def test_start_reply_depends_on_owner(
    tmp_path: Path, user_id: int | None, expected: str
) -> None:
    update, reply = fake_update(user_id)
    context = SimpleNamespace(bot_data={"settings": settings(tmp_path)})
    await start_command(update, context)  # type: ignore[arg-type]
    reply.assert_awaited_once_with(expected)


async def test_start_with_no_owners_configured_treats_everyone_as_non_owner(
    tmp_path: Path,
) -> None:
    update, reply = fake_update(111)
    context = SimpleNamespace(bot_data={"settings": settings(tmp_path, owner_user_ids="")})
    await start_command(update, context)  # type: ignore[arg-type]
    reply.assert_awaited_once_with(NON_OWNER_TEXT)


def test_owner_text_has_no_chat_data() -> None:
    assert "setup pending" in OWNER_TEXT


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
        assert len(start) == 1
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
