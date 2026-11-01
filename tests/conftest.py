from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from elysium_bot.db import migrate
from elysium_bot.db.session import create_engine, create_session_factory

POSTGRES_URL = os.environ.get("ELYSIUM_TEST_POSTGRES_URL")


def _backends() -> list[pytest.param]:  # type: ignore[valid-type]
    params = [pytest.param("sqlite", id="sqlite")]
    params.append(
        pytest.param(
            "postgres",
            id="postgres",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(not POSTGRES_URL, reason="ELYSIUM_TEST_POSTGRES_URL not set"),
            ],
        )
    )
    return params


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.fixture(params=_backends())
def any_db_url(request: pytest.FixtureRequest, sqlite_url: str) -> Iterator[str]:
    """A fresh, empty database URL for each backend (schema not created)."""
    if request.param == "sqlite":
        yield sqlite_url
        return
    assert POSTGRES_URL
    migrate.downgrade(POSTGRES_URL, "base")
    yield POSTGRES_URL
    migrate.downgrade(POSTGRES_URL, "base")


@pytest.fixture
def migrated_url(sqlite_url: str) -> str:
    """SQLite database upgraded to head (sync fixture: Alembic runs its own event loop)."""
    migrate.upgrade(sqlite_url)
    return sqlite_url


@pytest.fixture
async def engine(migrated_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(migrated_url)
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s
