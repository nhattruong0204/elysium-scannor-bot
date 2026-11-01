"""Callback dispatch for the control center (TELEGRAM_UX §3).

Every press: decode → load flow (owner + expiry check) → fresh role lookup → permission check →
handler → persist flow → edit the same message. Roles are never cached, so revocations apply on
the very next press.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardButton, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from elysium_bot.db.models import ChatSettings, TelegramChat
from elysium_bot.db.scope import ChatScope
from elysium_bot.logging import get_logger
from elysium_bot.telegram_ui.auth import AuthService, Permission
from elysium_bot.telegram_ui.callbacks import decode, encode
from elysium_bot.telegram_ui.flows import Flow, FlowExpired, FlowForbidden, FlowStore
from elysium_bot.telegram_ui.render import Screen

if TYPE_CHECKING:
    from elysium_bot.app import Services

log = get_logger(__name__)

EXPIRED = "This menu expired — send /menu to open a new one."
FOREIGN = "This menu belongs to someone else."
NO_ACCESS = "You no longer have access to this group."
NOT_ALLOWED = "Your role doesn't allow this."
SLOW_DOWN = "Slow down a little 🙂"
UNKNOWN = "Unknown action."
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


@dataclass
class UiContext:
    services: Services
    session: AsyncSession
    flows: FlowStore
    auth: AuthService
    flow: Flow
    scope: ChatScope
    chat: TelegramChat
    chat_settings: ChatSettings
    arg: str | None
    bot: Any

    @property
    def tz(self) -> str:
        return self.chat_settings.timezone

    def can(self, permission: Permission) -> bool:
        return self.auth.can(self.scope.role, permission)

    def btn(self, label: str, op: str, arg: str | int | None = None) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=encode(op, self.flow.token, arg))


Handler = Callable[[UiContext], Awaitable[Screen]]


@dataclass(frozen=True)
class Route:
    handler: Handler
    permission: Permission


class Throttle:
    def __init__(
        self, limit: int = 3, per_seconds: float = 1.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit, self.per, self.clock = limit, per_seconds, clock
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        now = self.clock()
        hits = self._hits[user_id]
        while hits and now - hits[0] > self.per:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


class CallbackRouter:
    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.throttle = Throttle()

    def route(
        self, op: str, permission: Permission = Permission.VIEW_STATUS
    ) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            self.routes[op] = Route(fn, permission)
            return fn

        return register

    async def dispatch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.from_user is None:
            return
        data = decode(query.data)
        route = self.routes.get(data.op) if data else None
        if data is None or route is None:
            await query.answer(UNKNOWN)
            return
        user_id = query.from_user.id
        if not self.throttle.allow(user_id):
            await query.answer(SLOW_DOWN)
            return
        services: Services = context.bot_data["services"]
        flows = FlowStore(services.sessions)
        try:
            flow = await flows.load(data.token, user_id)
        except FlowExpired:
            await query.answer(EXPIRED, show_alert=True)
            return
        except FlowForbidden:
            await query.answer(FOREIGN, show_alert=True)
            return

        screen = await self.render(services, flows, flow, data.op, data.arg, user_id, context.bot)
        if screen is None:
            await query.answer(NO_ACCESS, show_alert=True)
            return
        await flows.save(flow)
        await self._show(query, screen)

    async def render(
        self,
        services: Services,
        flows: FlowStore,
        flow: Flow,
        op: str,
        arg: str | None,
        user_id: int,
        bot: Any,
    ) -> Screen | None:
        """Run a route with fresh authorization. None means the user lost access to the chat."""
        auth = AuthService(services.settings)
        async with services.sessions() as session:
            if flow.chat_id is None:
                return None
            chat = await session.get(TelegramChat, flow.chat_id)
            scope = await auth.scope(session, flow.chat_id, user_id) if chat else None
            if chat is None or scope is None:
                flow.state.pop("pending", None)
                return None
            chat_settings = await session.get(ChatSettings, chat.id)
            if chat_settings is None:
                chat_settings = ChatSettings(
                    chat_id=chat.id, timezone=services.settings.default_timezone
                )
                session.add(chat_settings)
                await session.flush()
            route = self.routes[op]
            ctx = UiContext(
                services, session, flows, auth, flow, scope, chat, chat_settings, arg, bot
            )
            if not auth.can(scope.role, route.permission):
                from elysium_bot.telegram_ui.screens import main_menu

                screen = await main_menu(ctx)
                screen.alert = NOT_ALLOWED
            else:
                screen = await route.handler(ctx)
            await session.commit()
            return screen

    @staticmethod
    async def _show(query: Any, screen: Screen) -> None:
        try:
            await query.edit_message_text(
                screen.text,
                parse_mode=ParseMode.HTML,
                reply_markup=screen.markup(),
                link_preview_options=NO_PREVIEW,
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        await query.answer(screen.alert, show_alert=bool(screen.alert and len(screen.alert) > 60))


router = CallbackRouter()
