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
from elysium_bot.db.enums import AddressFormat, ChecksumState, IndexState, LinkKind, Source
from elysium_bot.timeutil import utcnow


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "tg_message_id"),
        Index("ix_messages_chat_id_sent_at", "chat_id", "sent_at"),
        Index("ix_messages_chat_id_sender_user_id_sent_at", "chat_id", "sender_user_id", "sent_at"),
        Index("ix_messages_chat_id_reply_to_tg_message_id", "chat_id", "reply_to_tg_message_id"),
        Index(
            "ix_messages_chat_id_message_thread_id_sent_at",
            "chat_id",
            "message_thread_id",
            "sent_at",
        ),
        Index(
            "ix_messages_pending_index",
            "chat_id",
            sqlite_where=text("index_state = 'pending'"),
            postgresql_where=text("index_state = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_chats.id", ondelete="CASCADE"), nullable=False
    )
    tg_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_thread_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="SET NULL")
    )
    sender_chat_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_name_snapshot: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    entities: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    reply_to_tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    forward_origin: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    media_type: Mapped[str | None] = mapped_column(String(32))
    media_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_service: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    service_action: Mapped[str | None] = mapped_column(String(64))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    first_ingested_at: Mapped[datetime] = created_at_column()
    last_ingested_at: Mapped[datetime] = created_at_column()
    primary_source: Mapped[Source] = mapped_column(str_enum(Source, "source"), nullable=False)
    index_state: Mapped[IndexState] = mapped_column(
        str_enum(IndexState, "index_state"), default=IndexState.PENDING, nullable=False
    )


class MessageVersion(Base):
    __tablename__ = "message_versions"
    __table_args__ = (UniqueConstraint("message_id", "version_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    entities: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source: Mapped[Source] = mapped_column(str_enum(Source, "source"), nullable=False)
    captured_at: Mapped[datetime] = created_at_column()


class MessageSource(Base):
    __tablename__ = "message_sources"

    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[Source] = mapped_column(str_enum(Source, "source"), primary_key=True)
    import_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_runs.id", ondelete="SET NULL")
    )
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class MessageLink(Base):
    __tablename__ = "message_links"
    __table_args__ = (Index("ix_message_links_domain", "domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[LinkKind] = mapped_column(str_enum(LinkKind, "link_kind"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class ContractMention(Base):
    __tablename__ = "contract_mentions"
    __table_args__ = (Index("ix_contract_mentions_normalized", "normalized"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    raw: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized: Mapped[str] = mapped_column(String(128), nullable=False)
    format: Mapped[AddressFormat] = mapped_column(
        str_enum(AddressFormat, "address_format"), nullable=False
    )
    checksum_state: Mapped[ChecksumState] = mapped_column(
        str_enum(ChecksumState, "checksum_state"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
