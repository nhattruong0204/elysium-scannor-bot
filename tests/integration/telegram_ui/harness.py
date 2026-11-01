"""Drive the control center like a Telegram client: open, press buttons, type replies."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from telegram import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageOriginUser,
    Update,
    User,
)

from elysium_bot.app import Services
from elysium_bot.telegram_ui import screens
from elysium_bot.telegram_ui.router import router

T = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
BOT_USER = User(id=999, first_name="ResearchBot", is_bot=True, username="research_bot")
_ids = itertools.count(1000)


@dataclass
class View:
    text: str = ""
    markup: InlineKeyboardMarkup | None = None
    message_id: int | None = None
    alerts: list[str] = field(default_factory=list)

    def buttons(self) -> list[tuple[str, str | None, str | None]]:
        if self.markup is None:
            return []
        return [
            (b.text, b.callback_data, b.url) for row in self.markup.inline_keyboard for b in row
        ]

    def labels(self) -> list[str]:
        return [label for label, _, _ in self.buttons()]

    def data_for(self, label_part: str) -> str:
        for label, data, _ in self.buttons():
            if label_part in label and data:
                return data
        raise AssertionError(f"no button containing {label_part!r} in {self.labels()}")


class Harness:
    def __init__(self, services: Services) -> None:
        self.services = services
        self.bot = MagicMock()
        self.bot.username = BOT_USER.username
        self.bot.id = BOT_USER.id
        self.bot.defaults = None
        self.bot.send_message = AsyncMock(side_effect=self._sent)
        self.bot.edit_message_text = AsyncMock(side_effect=self._edited)
        self.bot.answer_callback_query = AsyncMock(side_effect=self._answered)
        self.views: dict[int, View] = {}
        self.replies: dict[int, list[str]] = {}
        self.context = SimpleNamespace(
            bot=self.bot, bot_data={"services": services, "settings": services.settings}, args=[]
        )

    # --- fake Bot API --------------------------------------------------------------------------

    async def _sent(self, chat_id: int, text: str | None = None, **kw: Any) -> Any:
        text = kw.get("text", text)
        mid = next(_ids)
        markup = kw.get("reply_markup")
        if isinstance(markup, InlineKeyboardMarkup) and any(
            b.callback_data for row in markup.inline_keyboard for b in row
        ):
            self.views[chat_id] = View(text or "", markup, mid)
        else:
            self.replies.setdefault(chat_id, []).append(text or "")
            self.last_reply_markup = markup
        return SimpleNamespace(message_id=mid)

    async def _edited(self, text: str | None = None, **kw: Any) -> Any:
        chat_id = kw["chat_id"]
        view = self.views.setdefault(chat_id, View())
        view.text, view.markup = kw.get("text", text) or "", kw.get("reply_markup")
        view.message_id = kw.get("message_id", view.message_id)
        return True

    async def _answered(self, callback_query_id: str, text: str | None = None, **kw: Any) -> Any:
        if text:
            self._alert_sink.append(text)
        return True

    # --- client actions ------------------------------------------------------------------------

    def _message(self, user: User, text: str, **kw: Any) -> Message:
        chat = Chat(id=user.id, type="private")
        msg = Message(message_id=next(_ids), date=T, chat=chat, from_user=user, text=text, **kw)
        msg.set_bot(self.bot)
        return msg

    async def open(self, user: User, command: str = "/menu", args: list[str] | None = None) -> View:
        self.context.args = args or []
        msg = self._message(user, command)
        await screens.open_control_center(Update(update_id=next(_ids), message=msg), self.context)  # type: ignore[arg-type]
        return self.view(user)

    def view(self, user: User) -> View:
        return self.views.setdefault(user.id, View())

    async def press_data(
        self, user: User, data: str, *, owner_view: User | None = None
    ) -> list[str]:
        """Press callback data on the menu shown to ``owner_view`` (default: the presser's own)."""
        holder = owner_view or user
        view = self.view(holder)
        message = Message(
            message_id=view.message_id or 1,
            date=T,
            chat=Chat(id=holder.id, type="private"),
            from_user=BOT_USER,
            text="menu",
        )
        message.set_bot(self.bot)
        query = CallbackQuery(
            id=str(next(_ids)), from_user=user, chat_instance="ci", message=message, data=data
        )
        query.set_bot(self.bot)
        self._alert_sink: list[str] = []
        await router.dispatch(Update(update_id=next(_ids), callback_query=query), self.context)  # type: ignore[arg-type]
        view.alerts = list(self._alert_sink)
        return view.alerts

    async def press(self, user: User, label_part: str) -> View:
        await self.press_data(user, self.view(user).data_for(label_part))
        return self.view(user)

    async def type(self, user: User, text: str, *, forward_from: User | None = None) -> View:
        kw: dict[str, Any] = {}
        if forward_from is not None:
            kw["forward_origin"] = MessageOriginUser(date=T, sender_user=forward_from)
        msg = self._message(user, text, **kw)
        await screens.on_private_input(Update(update_id=next(_ids), message=msg), self.context)  # type: ignore[arg-type]
        return self.view(user)
