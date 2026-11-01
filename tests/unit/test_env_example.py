from pathlib import Path

from elysium_bot.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _env_names(field: str) -> set[str]:
    info = Settings.model_fields[field]
    alias = info.validation_alias
    if alias is not None and hasattr(alias, "choices"):
        return {str(c) for c in alias.choices}
    return {f"ELYSIUM_{field.upper()}"}


def _example_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            keys[name] = value
    return keys


def test_env_example_lists_every_settings_field() -> None:
    keys = _example_keys()
    missing = [f for f in Settings.model_fields if not (_env_names(f) & keys.keys())]
    assert missing == []


def test_env_example_contains_no_secret_values() -> None:
    keys = _example_keys()
    for secret in (
        "ELYSIUM_TELEGRAM_BOT_TOKEN",
        "ANTHROPIC_API_KEY",
        "ELYSIUM_TELEGRAM_API_HASH",
        "ELYSIUM_MTPROTO_SESSION_KEY",
        "POSTGRES_PASSWORD",
    ):
        assert keys[secret] == "", f"{secret} must be empty in .env.example"
