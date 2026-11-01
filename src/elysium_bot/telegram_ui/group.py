"""Group-side commands: /activate, /deactivate, /consent (SECURITY §3, HISTORY_INGESTION §3.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import ChatMember as PtbChatMember
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from elysium_bot.db.access import get_chat_by_tg_id
from elysium_bot.db.enums import AiConsent, AuditAction, ChatStatus, Role
from elysium_bot.db.models import ChatSettings, TelegramChat
from elysium_bot.db.repositories import AuditLogRepository
from elysium_bot.db.scope import ChatScope
from elysium_bot.ingestion.bot_updates import GROUP_TYPES, chat_type_of
from elysium_bot.ingestion.normalizer import SenderRef
from elysium_bot.ingestion.store import MessageStore
from elysium_bot.logging import get_logger
from elysium_bot.timeutil import utcnow

if TYPE_CHECKING:
    from elysium_bot.app import Services

log = get_logger(__name__)

CONSENT_PREFIX = "v1:consent:"

PRIVACY_HELP = (
    "I can't read all messages in this group yet, so history collection would be incomplete.\n\n"
    "Fix it with one of:\n"
    "1. In @BotFather: /setprivacy → choose this bot → Disable. Then remove me from the group "
    "and add me again (the change only applies after re-adding).\n"
    "2. Or promote me to admin in this group.\n\n"
    "Then run /activate again."
)

NOT_OWNER = "Only the bot owner can do this."

NOTICE = (
    "📌 Research archive notice\n\n"
    "This group is now archived by a private research bot run by the group owner.\n"
    "• What is stored: messages sent here from now on (text, links, reply structure; no media "
    "files) and, if the owner imports it, earlier history from a Telegram export.\n"
    "• Where: a private server operated by the owner. Only authorized admins can search or "
    "export it.\n"
    "• AI analysis: summaries are produced with Anthropic's Claude API, and only messages from "
    "members who consent are sent to it. Tap /consent to choose. You can change your mind at "
    "any time.\n"
    "• Retention and deletion: ask the owner to delete your stored messages at any time.\n\n"
    "Not financial advice. Nothing here is a trading signal."
)

CONSENT_PROMPT = (
    "AI analysis consent\n\n"
    "If you consent, your messages in this group may be included in AI analysis (Anthropic Claude "
    "API) for the group's research reports. Without consent, your messages are still archived "
    "for search/export by authorized admins but are withheld from AI."
)

CONSENT_INFO = (
    "Consent lets your messages here be sent to Anthropic's Claude API for research summaries. "
    "Without it they are withheld from AI. You can revoke at any time."
)


def consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("I consent", callback_data=f"{CONSENT_PREFIX}grant"),
                InlineKeyboardButton("I do not consent", callback_data=f"{CONSENT_PREFIX}revoke"),
            ],
            [InlineKeyboardButton("What does this mean?", callback_data=f"{CONSENT_PREFIX}info")],
        ]
    )


async def can_read_all_messages(context: ContextTypes.DEFAULT_TYPE, tg_chat_id: int) -> bool:
    member = await context.bot.get_chat_member(tg_chat_id, context.bot.id)
    if member.status in (PtbChatMember.ADMINISTRATOR, PtbChatMember.OWNER):
        return True
    return bool(context.bot.can_read_all_group_messages)


async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user, ptb_chat = update.effective_message, update.effective_user, update.effective_chat
    if message is None or user is None or ptb_chat is None or ptb_chat.type not in GROUP_TYPES:
        return
    services: Services = context.bot_data["services"]
    if not services.settings.is_owner(user.id):
        await message.reply_text(NOT_OWNER)
        return
    if not await can_read_all_messages(context, ptb_chat.id):
        await message.reply_text(PRIVACY_HELP)
        return

    async with services.sessions() as session:
        chat = await get_chat_by_tg_id(session, ptb_chat.id)
        if chat is not None and chat.status == ChatStatus.ACTIVE:
            await message.reply_text("This group is already active. Collection is running.")
            return
        if chat is None:
            chat = TelegramChat(tg_chat_id=ptb_chat.id, type=chat_type_of(ptb_chat.type))
            session.add(chat)
        chat.title = ptb_chat.title
        chat.username = ptb_chat.username
        chat.is_forum = bool(ptb_chat.is_forum)
        chat.status = ChatStatus.ACTIVE
        chat.activated_at = utcnow()
        chat.activated_by_tg_user_id = user.id
        await session.flush()
        if await session.get(ChatSettings, chat.id) is None:
            session.add(ChatSettings(chat_id=chat.id, timezone=services.settings.default_timezone))

        notice = await context.bot.send_message(ptb_chat.id, NOTICE)
        chat.notice_posted_at = utcnow()
        chat.notice_message_id = notice.message_id
        try:
            await context.bot.pin_chat_message(
                ptb_chat.id, notice.message_id, disable_notification=True
            )
        except TelegramError:
            log.info("activate.pin_failed", tg_chat_id=ptb_chat.id)
        await AuditLogRepository(session, ChatScope(chat.id, user.id, Role.OWNER)).record(
            AuditAction.ACTIVATE, {"title": ptb_chat.title, "type": ptb_chat.type}
        )
        await session.commit()

    checklist = (
        f"✅ Activated “{ptb_chat.title}”. Live collection is on.\n\n"
        "Next steps:\n"
        "1. Import earlier history: in Telegram Desktop → group → ⋮ → Export chat history → "
        "uncheck media → format JSON. Send result.json (≤ 20 MB) here, or run "
        "`elysium-bot import-export` on the server for larger files.\n"
        "2. Ask members to tap /consent in the group so their messages can be used for AI analysis."
    )
    try:
        await context.bot.send_message(user.id, checklist)
    except TelegramError:
        await message.reply_text(
            "Activated. Open a DM with me (/start) to receive the setup checklist."
        )


async def deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user, ptb_chat = update.effective_message, update.effective_user, update.effective_chat
    if message is None or user is None or ptb_chat is None or ptb_chat.type not in GROUP_TYPES:
        return
    services: Services = context.bot_data["services"]
    if not services.settings.is_owner(user.id):
        await message.reply_text(NOT_OWNER)
        return
    async with services.sessions() as session:
        chat = await get_chat_by_tg_id(session, ptb_chat.id)
        if chat is None or chat.status != ChatStatus.ACTIVE:
            await message.reply_text("This group is not active.")
            return
        chat.status = ChatStatus.DEACTIVATED
        await AuditLogRepository(session, ChatScope(chat.id, user.id, Role.OWNER)).record(
            AuditAction.DEACTIVATE
        )
        await session.commit()
    await message.reply_text("Collection stopped. Stored data is kept; the owner can delete it.")


async def consent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, ptb_chat = update.effective_message, update.effective_chat
    if message is None or ptb_chat is None or ptb_chat.type not in GROUP_TYPES:
        return
    await message.reply_text(CONSENT_PROMPT, reply_markup=consent_keyboard())


async def consent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or query.message is None:
        return
    action = query.data.removeprefix(CONSENT_PREFIX)
    if action == "info":
        await query.answer(CONSENT_INFO, show_alert=True)
        return
    if action not in ("grant", "revoke"):
        await query.answer("Unknown action.")
        return
    services: Services = context.bot_data["services"]
    user = query.from_user
    async with services.sessions() as session:
        chat = await get_chat_by_tg_id(session, query.message.chat.id)
        if chat is None or chat.status != ChatStatus.ACTIVE:
            await query.answer("This group is not active.", show_alert=True)
            return
        member = await MessageStore(session, chat).member_for(
            SenderRef(
                kind="user",
                tg_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                is_bot=user.is_bot,
            )
        )
        new_state = AiConsent.GRANTED if action == "grant" else AiConsent.REVOKED
        changed = member.ai_consent != new_state
        member.ai_consent = new_state
        member.ai_consent_at = utcnow()
        if changed:
            await AuditLogRepository(session, ChatScope(chat.id, user.id, Role.MEMBER)).record(
                AuditAction.CONSENT_CHANGE, {"consent": new_state.value}
            )
        await session.commit()
    text = (
        "Thanks — your messages here may be included in AI analysis. Revoke any time via /consent."
        if new_state == AiConsent.GRANTED
        else "Noted — your messages are withheld from AI analysis."
    )
    # The alert is shown only to the user who tapped, so nothing is posted to the group.
    await query.answer(text, show_alert=True)
