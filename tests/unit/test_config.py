import pytest
from pydantic import ValidationError

from elysium_bot.config import Settings


def make(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


def test_defaults() -> None:
    s = make()
    assert s.default_timezone == "Asia/Ho_Chi_Minh"
    assert s.ai_routine_model == "claude-sonnet-5-5"
    assert s.ai_routine_effort == "medium"
    assert s.ai_deep_model == "claude-opus-5-5"
    assert s.ai_deep_effort == "high"
    assert s.database_url.startswith("sqlite+aiosqlite:///")
    assert s.mtproto_enabled is False
    assert s.owner_user_ids == []


def test_owner_ids_from_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELYSIUM_OWNER_USER_IDS", "123, 456,789")
    assert Settings(_env_file=None).owner_user_ids == [123, 456, 789]  # type: ignore[call-arg]


def test_owner_ids_from_single_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELYSIUM_OWNER_USER_IDS", "42")
    assert Settings(_env_file=None).owner_user_ids == [42]  # type: ignore[call-arg]


def test_owner_ids_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELYSIUM_OWNER_USER_IDS", "")
    assert Settings(_env_file=None).owner_user_ids == []  # type: ignore[call-arg]


def test_invalid_owner_id_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELYSIUM_OWNER_USER_IDS", "12,abc")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError, match="Unknown IANA timezone"):
        make(default_timezone="Mars/Olympus")


def test_invalid_effort_rejected() -> None:
    with pytest.raises(ValidationError):
        make(ai_routine_effort="extreme")


def test_secrets_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "123456789:AAH-super-secret-token-value-abcdefghij"
    monkeypatch.setenv("ELYSIUM_TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-secretsecret")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.telegram_bot_token is not None
    assert s.telegram_bot_token.get_secret_value() == token
    assert token not in repr(s)
    assert "sk-ant-api03-secretsecret" not in repr(s)
    assert "sk-ant-api03-secretsecret" not in str(s.model_dump())


def test_budget_must_not_exceed_cap() -> None:
    with pytest.raises(ValidationError, match="cap"):
        make(ai_daily_budget_usd="30", ai_daily_budget_cap_usd="20")


def test_is_owner() -> None:
    s = make(owner_user_ids="1,2")
    assert s.is_owner(1)
    assert not s.is_owner(3)
