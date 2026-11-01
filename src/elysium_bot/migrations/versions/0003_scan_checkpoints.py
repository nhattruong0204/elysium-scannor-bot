"""Scan checkpoints (backs "Since last scan").

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-01 22:28:43.855420
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scan_checkpoints",
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("last_scan_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scan_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scan_job_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_scan_checkpoints_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_scan_job_id"],
            ["jobs.id"],
            name=op.f("fk_scan_checkpoints_last_scan_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("chat_id", "tg_user_id", name=op.f("pk_scan_checkpoints")),
    )


def downgrade() -> None:
    op.drop_table("scan_checkpoints")
