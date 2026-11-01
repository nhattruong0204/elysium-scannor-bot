from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from elysium_bot.db.base import Base, UTCDateTime, created_at_column, str_enum, updated_at_column
from elysium_bot.db.enums import CoverageKind, ImportStatus, Source
from elysium_bot.timeutil import utcnow


class ImportRun(Base):
    __tablename__ = "import_runs"
    __table_args__ = (Index("ix_import_runs_chat_id_file_sha256", "chat_id", "file_sha256"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[Source] = mapped_column(str_enum(Source, "source"), nullable=False)
    status: Mapped[ImportStatus] = mapped_column(
        str_enum(ImportStatus, "import_status"), default=ImportStatus.RUNNING, nullable=False
    )
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    file_bytes: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    triggered_by_tg_user_id: Mapped[int | None] = mapped_column(BigInteger)
    seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unchanged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ImportErrorRecord(Base):
    __tablename__ = "import_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_run_id: Mapped[int] = mapped_column(
        ForeignKey("import_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    record_ref: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    raw_excerpt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class CoverageSegment(Base):
    __tablename__ = "coverage_segments"
    __table_args__ = (Index("ix_coverage_segments_chat_id_start_at", "chat_id", "start_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[Source] = mapped_column(str_enum(Source, "source"), nullable=False)
    kind: Mapped[CoverageKind] = mapped_column(
        str_enum(CoverageKind, "coverage_kind"), nullable=False
    )
    start_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    end_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    start_tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    end_tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    import_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_runs.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = updated_at_column()


class IngestionState(Base):
    __tablename__ = "ingestion_state"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[Source] = mapped_column(str_enum(Source, "source"), primary_key=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_update_id: Mapped[int | None] = mapped_column(BigInteger)
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = updated_at_column()
