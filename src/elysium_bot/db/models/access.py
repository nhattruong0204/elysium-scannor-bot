from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from elysium_bot.db.base import Base, UTCDateTime, created_at_column, str_enum
from elysium_bot.db.enums import AuditAction, StoredRole
from elysium_bot.timeutil import utcnow


class ChatRole(Base):
    __tablename__ = "chat_roles"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), primary_key=True
    )
    tg_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[StoredRole] = mapped_column(str_enum(StoredRole, "role"), nullable=False)
    granted_by_tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_chat_id_created_at", "chat_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="SET NULL")
    )
    actor_tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[AuditAction] = mapped_column(
        str_enum(AuditAction, "audit_action"), nullable=False
    )
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = created_at_column()


class UiFlow(Base):
    __tablename__ = "ui_flows"

    token: Mapped[str] = mapped_column(String(16), primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_chats.id", ondelete="CASCADE"))
    flow: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
