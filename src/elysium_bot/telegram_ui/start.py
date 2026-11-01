"""``/start`` placeholder (Phase 1). The real control center arrives in Phase 3."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from elysium_bot.config import Settings

OWNER_TEXT = (
    "Elysium Research bot is running — setup pending.\n\n"
    "Next steps: add the bot to your research group (privacy mode disabled in @BotFather) "
    "and activate it once live collection ships."
)
NON_OWNER_TEXT = (
    "This is a private research bot. If you were given access, ask the owner to add you."
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    is_owner = settings.is_owner(user.id if user else None)
    await message.reply_text(OWNER_TEXT if is_owner else NON_OWNER_TEXT)
