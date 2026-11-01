"""Core schema: chats, members, raw messages, ingestion tracking, jobs, access control.

Revision ID: 0001
Revises:
Create Date: 2026-11-01 21:11:18.108783
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "heartbeats",
        sa.Column("component", sa.String(length=32), nullable=False),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("component", name=op.f("pk_heartbeats")),
    )
    op.create_table(
        "telegram_chats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "supergroup",
                "group",
                "channel",
                name="chat_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("is_forum", sa.Boolean(), nullable=False),
        sa.Column("migrated_from_tg_chat_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "active",
                "paused",
                "deactivated",
                name="chat_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_by_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("notice_posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notice_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_chats")),
        sa.UniqueConstraint("tg_chat_id", name=op.f("uq_telegram_chats_tg_chat_id")),
    )
    op.create_table(
        "telegram_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("is_bot", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_users")),
        sa.UniqueConstraint("tg_user_id", name=op.f("uq_telegram_users_tg_user_id")),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=True),
        sa.Column("actor_tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "export",
                "analysis",
                "ask",
                "settings_change",
                "role_change",
                "import",
                "activate",
                "deactivate",
                "consent_change",
                "purge_user",
                "delete_chat_data",
                name="audit_action",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_audit_log_chat_id_telegram_chats"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.create_index(
            "ix_audit_log_chat_id_created_at", ["chat_id", "created_at"], unique=False
        )

    op.create_table(
        "chat_members",
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("pseudonym", sa.String(length=16), nullable=False),
        sa.Column(
            "ai_consent",
            sa.Enum(
                "unknown",
                "granted",
                "revoked",
                name="ai_consent",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("ai_consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_chat_members_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["telegram_users.id"],
            name=op.f("fk_chat_members_user_id_telegram_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chat_id", "user_id", name=op.f("pk_chat_members")),
        sa.UniqueConstraint("chat_id", "pseudonym", name=op.f("uq_chat_members_chat_id_pseudonym")),
    )
    op.create_table(
        "chat_roles",
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "admin", "analyst", "viewer", name="role", native_enum=False, create_constraint=True
            ),
            nullable=False,
        ),
        sa.Column("granted_by_tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_chat_roles_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chat_id", "tg_user_id", name=op.f("pk_chat_roles")),
    )
    op.create_table(
        "chat_settings",
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("routine_model", sa.String(length=64), nullable=False),
        sa.Column("routine_effort", sa.String(length=16), nullable=False),
        sa.Column("deep_model", sa.String(length=64), nullable=False),
        sa.Column("deep_effort", sa.String(length=16), nullable=False),
        sa.Column("collection_interval_min", sa.Integer(), nullable=True),
        sa.Column("summary_interval_min", sa.Integer(), nullable=True),
        sa.Column("summary_mode", sa.String(length=32), nullable=False),
        sa.Column("summary_min_messages", sa.Integer(), nullable=False),
        sa.Column("summary_destination", sa.String(length=32), nullable=False),
        sa.Column("daily_ai_budget_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column(
            "ai_consent_policy",
            sa.Enum(
                "strict",
                "notice_only",
                name="ai_consent_policy",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("pseudonymize_exports", sa.Boolean(), nullable=False),
        sa.Column("send_display_names_to_ai", sa.Boolean(), nullable=False),
        sa.Column("updated_by_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "collection_interval_min IS NULL OR collection_interval_min > 0",
            name=op.f("ck_chat_settings_collection_interval"),
        ),
        sa.CheckConstraint(
            "summary_interval_min IS NULL OR summary_interval_min IN (60, 120, 180, 240)",
            name=op.f("ck_chat_settings_summary_interval"),
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_chat_settings_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chat_id", name=op.f("pk_chat_settings")),
    )
    op.create_table(
        "import_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "bot_api",
                "desktop_export",
                "mtproto",
                name="source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "partial",
                "failed",
                "cancelled",
                name="import_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("file_sha256", sa.String(length=64), nullable=True),
        sa.Column("file_bytes", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triggered_by_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("seen", sa.Integer(), nullable=False),
        sa.Column("inserted", sa.Integer(), nullable=False),
        sa.Column("updated", sa.Integer(), nullable=False),
        sa.Column("unchanged", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_import_runs_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_runs")),
    )
    with op.batch_alter_table("import_runs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_import_runs_chat_id_file_sha256", ["chat_id", "file_sha256"], unique=False
        )

    op.create_table(
        "ingestion_state",
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "bot_api",
                "desktop_export",
                "mtproto",
                name="source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_update_id", sa.BigInteger(), nullable=True),
        sa.Column("cursor", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_ingestion_state_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chat_id", "source", name=op.f("pk_ingestion_state")),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=True),
        sa.Column(
            "kind",
            sa.Enum(
                "scan",
                "import",
                "mtproto_backfill",
                "reindex",
                "export",
                "ai_analysis",
                "scheduled_summary",
                "dossier_rebuild",
                "digest_build",
                "retention_purge",
                "backup",
                name="job_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "family",
            sa.Enum(
                "ingest",
                "ai",
                "export",
                "maintenance",
                name="job_family",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("lease_key", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                "skipped",
                name="job_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "trigger",
            sa.Enum(
                "manual",
                "schedule",
                "startup_resume",
                name="job_trigger",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("requested_by_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("progress", sa.JSON(), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_jobs_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_jobs_chat_id_kind_created_at", ["chat_id", "kind", "created_at"], unique=False
        )
        batch_op.create_index(
            "ix_jobs_status_lease_expires_at", ["status", "lease_expires_at"], unique=False
        )
        batch_op.create_index(
            "uq_jobs_running_lease_key",
            ["lease_key"],
            unique=True,
            sqlite_where=sa.text("status = 'running'"),
            postgresql_where=sa.text("status = 'running'"),
        )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_thread_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_user_id", sa.Integer(), nullable=True),
        sa.Column("sender_chat_tg_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_name_snapshot", sa.String(length=255), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("entities", sa.JSON(), nullable=True),
        sa.Column("reply_to_tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("forward_origin", sa.JSON(), nullable=True),
        sa.Column("media_type", sa.String(length=32), nullable=True),
        sa.Column("media_meta", sa.JSON(), nullable=True),
        sa.Column("is_service", sa.Boolean(), nullable=False),
        sa.Column("service_action", sa.String(length=64), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("first_ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "primary_source",
            sa.Enum(
                "bot_api",
                "desktop_export",
                "mtproto",
                name="source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "index_state",
            sa.Enum(
                "pending",
                "indexed",
                "failed",
                name="index_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_messages_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_user_id"],
            ["telegram_users.id"],
            name=op.f("fk_messages_sender_user_id_telegram_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "chat_id", "tg_message_id", name=op.f("uq_messages_chat_id_tg_message_id")
        ),
    )
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.create_index(
            "ix_messages_chat_id_message_thread_id_sent_at",
            ["chat_id", "message_thread_id", "sent_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_messages_chat_id_reply_to_tg_message_id",
            ["chat_id", "reply_to_tg_message_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_messages_chat_id_sender_user_id_sent_at",
            ["chat_id", "sender_user_id", "sent_at"],
            unique=False,
        )
        batch_op.create_index("ix_messages_chat_id_sent_at", ["chat_id", "sent_at"], unique=False)
        batch_op.create_index(
            "ix_messages_pending_index",
            ["chat_id"],
            unique=False,
            sqlite_where=sa.text("index_state = 'pending'"),
            postgresql_where=sa.text("index_state = 'pending'"),
        )

    op.create_table(
        "ui_flows",
        sa.Column("token", sa.String(length=16), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=True),
        sa.Column("flow", sa.String(length=64), nullable=False),
        sa.Column("state", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_ui_flows_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("token", name=op.f("pk_ui_flows")),
    )
    with op.batch_alter_table("ui_flows", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ui_flows_expires_at"), ["expires_at"], unique=False)

    op.create_table(
        "contract_mentions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("raw", sa.String(length=128), nullable=False),
        sa.Column("normalized", sa.String(length=128), nullable=False),
        sa.Column(
            "format",
            sa.Enum(
                "evm",
                "base58_like",
                "other",
                name="address_format",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "checksum_state",
            sa.Enum(
                "valid_checksum",
                "all_lower_or_upper",
                "invalid_checksum",
                "not_applicable",
                name="checksum_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_contract_mentions_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contract_mentions")),
    )
    with op.batch_alter_table("contract_mentions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_contract_mentions_message_id"), ["message_id"], unique=False
        )
        batch_op.create_index("ix_contract_mentions_normalized", ["normalized"], unique=False)

    op.create_table(
        "coverage_segments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "bot_api",
                "desktop_export",
                "mtproto",
                name="source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "observed",
                "gap_suspected",
                "gap_confirmed_empty",
                name="coverage_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("end_tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("import_run_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_coverage_segments_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["import_runs.id"],
            name=op.f("fk_coverage_segments_import_run_id_import_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coverage_segments")),
    )
    with op.batch_alter_table("coverage_segments", schema=None) as batch_op:
        batch_op.create_index(
            "ix_coverage_segments_chat_id_start_at", ["chat_id", "start_at"], unique=False
        )

    op.create_table(
        "import_errors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("import_run_id", sa.Integer(), nullable=False),
        sa.Column("record_ref", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("raw_excerpt", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["import_runs.id"],
            name=op.f("fk_import_errors_import_run_id_import_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_errors")),
    )
    with op.batch_alter_table("import_errors", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_import_errors_import_run_id"), ["import_run_id"], unique=False
        )

    op.create_table(
        "message_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column(
            "kind",
            sa.Enum(
                "website",
                "x",
                "telegram",
                "docs",
                "github",
                "explorer",
                "dex",
                "other",
                name="link_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_message_links_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_links")),
    )
    with op.batch_alter_table("message_links", schema=None) as batch_op:
        batch_op.create_index("ix_message_links_domain", ["domain"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_message_links_message_id"), ["message_id"], unique=False
        )

    op.create_table(
        "message_sources",
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "bot_api",
                "desktop_export",
                "mtproto",
                name="source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("import_run_id", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["import_runs.id"],
            name=op.f("fk_message_sources_import_run_id_import_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_message_sources_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id", "source", name=op.f("pk_message_sources")),
    )
    op.create_table(
        "message_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("entities", sa.JSON(), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "source",
            sa.Enum(
                "bot_api",
                "desktop_export",
                "mtproto",
                name="source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_message_versions_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_versions")),
        sa.UniqueConstraint(
            "message_id", "version_no", name=op.f("uq_message_versions_message_id_version_no")
        ),
    )
    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=True),
        sa.Column(
            "kind",
            sa.Enum(
                "collection",
                "ai_summary",
                "digest",
                "backup",
                "retention",
                name="schedule_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("interval_min", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_job_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["telegram_chats.id"],
            name=op.f("fk_schedules_chat_id_telegram_chats"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["last_job_id"],
            ["jobs.id"],
            name=op.f("fk_schedules_last_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedules")),
        sa.UniqueConstraint("chat_id", "kind", name=op.f("uq_schedules_chat_id_kind")),
    )


def downgrade() -> None:
    op.drop_table("schedules")
    op.drop_table("message_versions")
    op.drop_table("message_sources")
    with op.batch_alter_table("message_links", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_message_links_message_id"))
        batch_op.drop_index("ix_message_links_domain")

    op.drop_table("message_links")
    with op.batch_alter_table("import_errors", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_import_errors_import_run_id"))

    op.drop_table("import_errors")
    with op.batch_alter_table("coverage_segments", schema=None) as batch_op:
        batch_op.drop_index("ix_coverage_segments_chat_id_start_at")

    op.drop_table("coverage_segments")
    with op.batch_alter_table("contract_mentions", schema=None) as batch_op:
        batch_op.drop_index("ix_contract_mentions_normalized")
        batch_op.drop_index(batch_op.f("ix_contract_mentions_message_id"))

    op.drop_table("contract_mentions")
    with op.batch_alter_table("ui_flows", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ui_flows_expires_at"))

    op.drop_table("ui_flows")
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_index(
            "ix_messages_pending_index",
            sqlite_where=sa.text("index_state = 'pending'"),
            postgresql_where=sa.text("index_state = 'pending'"),
        )
        batch_op.drop_index("ix_messages_chat_id_sent_at")
        batch_op.drop_index("ix_messages_chat_id_sender_user_id_sent_at")
        batch_op.drop_index("ix_messages_chat_id_reply_to_tg_message_id")
        batch_op.drop_index("ix_messages_chat_id_message_thread_id_sent_at")

    op.drop_table("messages")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_jobs_running_lease_key",
            sqlite_where=sa.text("status = 'running'"),
            postgresql_where=sa.text("status = 'running'"),
        )
        batch_op.drop_index("ix_jobs_status_lease_expires_at")
        batch_op.drop_index("ix_jobs_chat_id_kind_created_at")

    op.drop_table("jobs")
    op.drop_table("ingestion_state")
    with op.batch_alter_table("import_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_import_runs_chat_id_file_sha256")

    op.drop_table("import_runs")
    op.drop_table("chat_settings")
    op.drop_table("chat_roles")
    op.drop_table("chat_members")
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_log_chat_id_created_at")

    op.drop_table("audit_log")
    op.drop_table("telegram_users")
    op.drop_table("telegram_chats")
    op.drop_table("heartbeats")
