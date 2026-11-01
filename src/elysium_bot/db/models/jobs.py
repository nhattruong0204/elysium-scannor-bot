from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from elysium_bot.db.base import Base, UTCDateTime, created_at_column, str_enum
from elysium_bot.db.enums import JobFamily, JobKind, JobStatus, JobTrigger, ScheduleKind


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # Overlap guard: at most one running job per lease key (chat + family).
        Index(
            "uq_jobs_running_lease_key",
            "lease_key",
            unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
        ),
        Index("ix_jobs_chat_id_kind_created_at", "chat_id", "kind", "created_at"),
        Index("ix_jobs_status_lease_expires_at", "status", "lease_expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_chats.id", ondelete="CASCADE"))
    kind: Mapped[JobKind] = mapped_column(str_enum(JobKind, "job_kind"), nullable=False)
    family: Mapped[JobFamily] = mapped_column(str_enum(JobFamily, "job_family"), nullable=False)
    lease_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, "job_status"), default=JobStatus.QUEUED, nullable=False
    )
    trigger: Mapped[JobTrigger] = mapped_column(
        str_enum(JobTrigger, "job_trigger"), default=JobTrigger.MANUAL, nullable=False
    )
    requested_by_tg_user_id: Mapped[int | None] = mapped_column(BigInteger)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = created_at_column()


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (UniqueConstraint("chat_id", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_chats.id", ondelete="CASCADE"))
    kind: Mapped[ScheduleKind] = mapped_column(
        str_enum(ScheduleKind, "schedule_kind"), nullable=False
    )
    interval_min: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))


class Heartbeat(Base):
    __tablename__ = "heartbeats"

    component: Mapped[str] = mapped_column(String(32), primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
