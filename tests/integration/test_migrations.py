from __future__ import annotations

import asyncio
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from elysium_bot.db import migrate
from elysium_bot.db.base import Base
from elysium_bot.db.migrate import include_object
from elysium_bot.db.session import create_engine

EXPECTED_TABLES = {
    "telegram_chats",
    "chat_settings",
    "telegram_users",
    "chat_members",
    "messages",
    "message_versions",
    "message_sources",
    "message_links",
    "contract_mentions",
    "import_runs",
    "import_errors",
    "coverage_segments",
    "ingestion_state",
    "jobs",
    "schedules",
    "heartbeats",
    "chat_roles",
    "audit_log",
    "ui_flows",
}


def _run(url: str, fn):  # type: ignore[no-untyped-def]
    async def go():  # type: ignore[no-untyped-def]
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(fn)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _tables(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names())


def test_metadata_matches_design_doc_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_upgrade_creates_all_tables(any_db_url: str) -> None:
    migrate.upgrade(any_db_url)
    tables = _run(any_db_url, _tables)
    assert tables >= EXPECTED_TABLES
    if any_db_url.startswith("sqlite"):
        assert "messages_fts" in tables
    else:
        cols = _run(
            any_db_url, lambda c: {col["name"] for col in inspect(c).get_columns("messages")}
        )
        assert "text_tsv" in cols


def test_upgrade_downgrade_upgrade_round_trip(any_db_url: str) -> None:
    migrate.upgrade(any_db_url)
    migrate.downgrade(any_db_url, "base")
    remaining = _run(any_db_url, _tables) - {"alembic_version"}
    assert remaining == set()
    migrate.upgrade(any_db_url)
    assert _run(any_db_url, _tables) >= EXPECTED_TABLES


def test_models_and_migrations_have_no_drift(any_db_url: str) -> None:
    migrate.upgrade(any_db_url)

    def diff(conn: Connection) -> list[object]:
        ctx = MigrationContext.configure(
            conn, opts={"include_object": include_object, "compare_type": True}
        )
        return list(compare_metadata(ctx, Base.metadata))

    assert _run(any_db_url, diff) == []


def test_sqlite_pragmas_applied(migrated_url: str) -> None:
    def pragmas(conn: Connection) -> tuple[str, int, int]:
        jm = conn.execute(text("PRAGMA journal_mode")).scalar_one()
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
        bt = conn.execute(text("PRAGMA busy_timeout")).scalar_one()
        return jm, fk, bt

    assert _run(migrated_url, pragmas) == ("wal", 1, 5000)


def test_missing_parent_directory_is_created(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "elysium.db"
    migrate.upgrade(f"sqlite+aiosqlite:///{db_path}")
    assert db_path.exists()
