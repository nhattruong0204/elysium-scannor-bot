"""Deployment-level settings, loaded from environment variables (prefix ``ELYSIUM_``).

Per-chat runtime settings live in the ``chat_settings`` table, not here.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]
LogFormat = Literal["json", "console"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ELYSIUM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram -----------------------------------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    owner_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- Storage ------------------------------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:////data/elysium.db"
    data_dir: Path = Path("/data")

    # --- Behaviour ----------------------------------------------------------------------------
    default_timezone: str = "Asia/Ho_Chi_Minh"
    log_level: str = "INFO"
    log_format: LogFormat = "json"

    # --- AI (used from Phase 5) ---------------------------------------------------------------
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "ELYSIUM_ANTHROPIC_API_KEY"),
    )
    ai_routine_model: str = "claude-sonnet-5-5"
    ai_routine_effort: Effort = "medium"
    ai_deep_model: str = "claude-opus-5-5"
    ai_deep_effort: Effort = "high"
    ai_daily_budget_usd: Decimal = Decimal("5.0")
    ai_daily_budget_cap_usd: Decimal = Decimal("20.0")
    ai_max_messages_per_run: int = 20_000
    ai_fallbacks_enabled: bool = True
    log_ai_payloads: bool = False
    pricing_overrides_json: str | None = None
    web_verification: bool = False  # used from Phase 8

    # --- Data lifecycle / exports (used from Phase 4 / 9) -------------------------------------
    retention_days: int | None = None
    export_max_per_hour: int = 5
    analyst_can_export: bool = False

    # --- MTProto backfill (optional, used from Phase 2c) --------------------------------------
    mtproto_enabled: bool = False
    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr | None = None
    mtproto_session_key: SecretStr | None = None

    # --- On-chain providers (used from Phase 7) -----------------------------------------------
    onchain_providers_json: str | None = None

    # --- Health -------------------------------------------------------------------------------
    heartbeat_interval_s: int = 30
    heartbeat_max_age_s: int = 120

    @field_validator("owner_user_ids", mode="before")
    @classmethod
    def _split_owner_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, int):
            return [value]
        return value

    @field_validator("default_timezone")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown IANA timezone: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _budget_within_cap(self) -> Settings:
        if self.ai_daily_budget_usd > self.ai_daily_budget_cap_usd:
            raise ValueError("ai_daily_budget_usd must not exceed ai_daily_budget_cap_usd (cap)")
        return self

    def is_owner(self, tg_user_id: int | None) -> bool:
        return tg_user_id is not None and tg_user_id in self.owner_user_ids


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
