"""Full-text search over messages (SQLite FTS5 / PostgreSQL tsvector).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01

Tokenizer notes: `$` and `_` are token characters so `$NEST` and snake_case stay whole.
PostgreSQL uses the `simple` configuration: tickers and project names must not be stemmed.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQLITE_UP = [
    """
    CREATE VIRTUAL TABLE messages_fts USING fts5(
        text,
        content='messages',
        content_rowid='id',
        tokenize="unicode61 remove_diacritics 2 tokenchars '$_'"
    )
    """,
    """
    CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    """
    CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END
    """,
    """
    CREATE TRIGGER messages_fts_au AFTER UPDATE OF text ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    "INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')",
]

SQLITE_DOWN = [
    "DROP TRIGGER IF EXISTS messages_fts_au",
    "DROP TRIGGER IF EXISTS messages_fts_ad",
    "DROP TRIGGER IF EXISTS messages_fts_ai",
    "DROP TABLE IF EXISTS messages_fts",
]

POSTGRES_UP = [
    """
    ALTER TABLE messages ADD COLUMN text_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED
    """,
    "CREATE INDEX ix_messages_text_tsv ON messages USING gin (text_tsv)",
]

POSTGRES_DOWN = [
    "DROP INDEX IF EXISTS ix_messages_text_tsv",
    "ALTER TABLE messages DROP COLUMN IF EXISTS text_tsv",
]


def _statements(up: bool) -> list[str]:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        return SQLITE_UP if up else SQLITE_DOWN
    if dialect == "postgresql":
        return POSTGRES_UP if up else POSTGRES_DOWN
    raise NotImplementedError(f"FTS migration not implemented for dialect {dialect!r}")


def upgrade() -> None:
    for statement in _statements(up=True):
        op.execute(statement)


def downgrade() -> None:
    for statement in _statements(up=False):
        op.execute(statement)
