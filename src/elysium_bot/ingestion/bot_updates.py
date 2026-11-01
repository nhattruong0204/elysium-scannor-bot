"""Path A — live Bot API collection handlers (HISTORY_INGESTION §3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import ChatMember as PtbChatMember
from telegram import Update
from telegram.constants import ChatType as PtbChatType
from telegram.ext import ContextTypes

from elysium_bot.db.access import get_chat_by_tg_id
from elysium_bot.db.enums import ChatStatus, ChatType, Source
from elysium_bot.db.models import ChatSettings, IngestionState, TelegramChat
from elysium_bot.ingestion import coverage
from elysium_bot.ingestion.normalizer import SenderRef, from_ptb
from elysium_bot.ingestion.store import MessageStore
from elysium_bot.logging import get_logger
from elysium_bot.timeutil import utcnow

if TYPE_CHECKING:
    from elysium_bot.app import Services

log = get_logger(__name__)

GROUP_TYPES = {PtbChatType.GROUP, PtbChatType.SUPERGROUP}


def chat_type_of(ptb_type: str) -> ChatType:
    return {
        PtbChatType.SUPERGROUP: ChatType.SUPERGROUP,
        PtbChatType.GROUP: ChatType.GROUP,
        PtbChatType.CHANNEL: ChatType.CHANNEL,
    }[ptb_type]  # type: ignore[index]


async def _migrated_chat(
    session: AsyncSession, tg_chat_id: int, from_tg_chat_id: int
) -> TelegramChat | None:
    """Basic group upgraded to a supergroup: continue collection under the new chat id."""
    old = await get_chat_by_tg_id(session, from_tg_chat_id)
    if old is None or old.status != ChatStatus.ACTIVE:
        return None
    new = TelegramChat(
        tg_chat_id=tg_chat_id,
        type=ChatType.SUPERGROUP,
        title=old.title,
        migrated_from_tg_chat_id=old.tg_chat_id,
        status=ChatStatus.ACTIVE,
        activated_at=utcnow(),
        activated_by_tg_user_id=old.activated_by_tg_user_id,
        notice_posted_at=old.notice_posted_at,
    )
    session.add(new)
    await session.flush()
    old_settings = await session.get(ChatSettings, old.id)
    settings = ChatSettings(chat_id=new.id)
    if old_settings is not None:
        for col in ChatSettings.__table__.columns.keys():  # noqa: SIM118 - ColumnCollection
            if col not in ("chat_id", "updated_at"):
                setattr(settings, col, getattr(old_settings, col))
    session.add(settings)
    old.status = ChatStatus.DEACTIVATED
    log.info("chat.migrated", old_tg_chat_id=old.tg_chat_id, new_tg_chat_id=tg_chat_id)
    return new


async def _touch_state(
    session: AsyncSession, chat_id: int, tg_message_id: int, update_id: int | None
) -> None:
    state = await session.get(IngestionState, (chat_id, Source.BOT_API))
    if state is None:
        state = IngestionState(chat_id=chat_id, source=Source.BOT_API)
        session.add(state)
    state.last_seen_at = utcnow()
    state.last_tg_message_id = max(state.last_tg_message_id or 0, tg_message_id)
    if update_id is not None:
        state.last_update_id = update_id
    state.status = "ok"


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persist every group message/edit from an activated chat. Runs before command handlers."""
    message = update.message or update.edited_message
    if message is None or message.chat.type not in GROUP_TYPES:
        return
    services: Services = context.bot_data["services"]
    async with services.sessions() as session:
        chat = await get_chat_by_tg_id(session, message.chat.id)
        if chat is None and message.migrate_from_chat_id:
            chat = await _migrated_chat(session, message.chat.id, message.migrate_from_chat_id)
        if chat is None or chat.status != ChatStatus.ACTIVE:
            return  # nothing is stored for chats that were never activated
        normalized = from_ptb(message, Source.BOT_API)
        await MessageStore(session, chat).upsert([normalized])
        await coverage.extend_observed(
            session, chat.id, Source.BOT_API, normalized.sent_at, normalized.tg_message_id
        )
        await _touch_state(session, chat.id, normalized.tg_message_id, update.update_id)
        if message.chat.title and chat.title != message.chat.title:
            chat.title = message.chat.title
        await session.commit()


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    if change is None:
        return
    services: Services = context.bot_data["services"]
    async with services.sessions() as session:
        chat = await get_chat_by_tg_id(session, change.chat.id)
        if chat is None:
            return
        status = change.new_chat_member.status
        if (
            status in (PtbChatMember.LEFT, PtbChatMember.BANNED)
            and chat.status == ChatStatus.ACTIVE
        ):
            chat.status = ChatStatus.PAUSED
            log.warning("chat.bot_removed", tg_chat_id=chat.tg_chat_id, status=status)
        await session.commit()


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.chat_member
    if change is None:
        return
    services: Services = context.bot_data["services"]
    user = change.new_chat_member.user
    async with services.sessions() as session:
        chat = await get_chat_by_tg_id(session, change.chat.id)
        if chat is None or chat.status != ChatStatus.ACTIVE:
            return
        await MessageStore(session, chat).member_for(
            SenderRef(
                kind="user",
                tg_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                is_bot=user.is_bot,
            )
        )
        await session.commit()


async def active_chats(session: AsyncSession) -> list[TelegramChat]:
    rows = await session.execute(
        select(TelegramChat).where(TelegramChat.status == ChatStatus.ACTIVE)
    )
    return list(rows.scalars())
