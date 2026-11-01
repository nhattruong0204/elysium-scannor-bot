"""Alembic environment (async engine; SQLite and PostgreSQL)."""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection

import elysium_bot.db.models  # noqa: F401  (registers tables)
from elysium_bot.db.base import Base
from elysium_bot.db.migrate import include_object, render_item
from elysium_bot.db.session import create_engine

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from elysium_bot.config import get_settings

    return get_settings().database_url


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_object=include_object,
        render_item=render_item,
        render_as_batch=True,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
