"""Programmatic Alembic access (used by the CLI and tests)."""

from __future__ import annotations

from typing import Any, Literal

from alembic import command
from alembic.autogenerate.api import AutogenContext
from alembic.config import Config

from elysium_bot.db.base import UTCDateTime

SCRIPT_LOCATION = "elysium_bot:migrations"

# Objects created by raw SQL in dialect-specific migrations (FTS) are invisible to the ORM.
_UNMANAGED_PREFIXES = ("messages_fts",)
_UNMANAGED_COLUMNS = {("messages", "text_tsv")}
_UNMANAGED_INDEXES = {"ix_messages_text_tsv"}


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Autogenerate filter: ignore objects managed by raw-SQL migrations."""
    if type_ == "table" and name and name.startswith(_UNMANAGED_PREFIXES):
        return False
    if type_ == "column" and (getattr(obj.table, "name", None), name) in _UNMANAGED_COLUMNS:
        return False
    return not (type_ == "index" and name in _UNMANAGED_INDEXES)


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    """Render app types as plain SQLAlchemy types so migrations never import app code."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPT_LOCATION)
    # configparser interpolation: escape '%' (e.g. in URL-encoded passwords).
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(database_url), revision)


def current(database_url: str) -> None:
    command.current(alembic_config(database_url), verbose=False)
