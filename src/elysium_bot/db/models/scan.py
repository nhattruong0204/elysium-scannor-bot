from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from elysium_bot.db.base import Base, UTCDateTime, updated_at_column


class ScanCheckpoint(Base):
    """Backs "Since last scan". ``tg_user_id = 0`` holds the scheduler's checkpoint."""

    __tablename__ = "scan_checkpoints"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), primary_key=True
    )
    tg_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_scan_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_scan_end: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_scan_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = updated_at_column()
