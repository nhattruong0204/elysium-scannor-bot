from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from elysium_bot.db.base import Base, UTCDateTime, created_at_column, str_enum, updated_at_column
from elysium_bot.db.enums import AiConsent, AiConsentPolicy, ChatStatus, ChatType


class TelegramChat(Base):
    __tablename__ = "telegram_chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    type: Mapped[ChatType] = mapped_column(str_enum(ChatType, "chat_type"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    is_forum: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    migrated_from_tg_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[ChatStatus] = mapped_column(
        str_enum(ChatStatus, "chat_status"), default=ChatStatus.PENDING, nullable=False
    )
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    activated_by_tg_user_id: Mapped[int | None] = mapped_column(BigInteger)
    notice_posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    notice_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class ChatSettings(Base):
    __tablename__ = "chat_settings"
    __table_args__ = (
        CheckConstraint(
            "summary_interval_min IS NULL OR summary_interval_min IN (60, 120, 180, 240)",
            name="summary_interval",
        ),
        CheckConstraint(
            "collection_interval_min IS NULL OR collection_interval_min > 0",
            name="collection_interval",
        ),
    )

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), primary_key=True
    )
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Ho_Chi_Minh", nullable=False)
    routine_model: Mapped[str] = mapped_column(
        String(64), default="claude-sonnet-5-5", nullable=False
    )
    routine_effort: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    deep_model: Mapped[str] = mapped_column(String(64), default="claude-opus-5-5", nullable=False)
    deep_effort: Mapped[str] = mapped_column(String(16), default="high", nullable=False)
    collection_interval_min: Mapped[int | None] = mapped_column(Integer)
    summary_interval_min: Mapped[int | None] = mapped_column(Integer)
    summary_mode: Mapped[str] = mapped_column(
        String(32), default="conversation_summary", nullable=False
    )
    summary_min_messages: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    summary_destination: Mapped[str] = mapped_column(String(32), default="owner_dm", nullable=False)
    daily_ai_budget_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("5"), nullable=False
    )
    retention_days: Mapped[int | None] = mapped_column(Integer)
    ai_consent_policy: Mapped[AiConsentPolicy] = mapped_column(
        str_enum(AiConsentPolicy, "ai_consent_policy"),
        default=AiConsentPolicy.STRICT,
        nullable=False,
    )
    pseudonymize_exports: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    send_display_names_to_ai: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_by_tg_user_id: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = updated_at_column()


class TelegramUser(Base):
    __tablename__ = "telegram_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_seen_at: Mapped[datetime] = created_at_column()
    last_seen_at: Mapped[datetime] = created_at_column()


class ChatMember(Base):
    __tablename__ = "chat_members"
    __table_args__ = (UniqueConstraint("chat_id", "pseudonym"),)

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="CASCADE"), primary_key=True
    )
    pseudonym: Mapped[str] = mapped_column(String(16), nullable=False)
    ai_consent: Mapped[AiConsent] = mapped_column(
        str_enum(AiConsent, "ai_consent"), default=AiConsent.UNKNOWN, nullable=False
    )
    ai_consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    purged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
