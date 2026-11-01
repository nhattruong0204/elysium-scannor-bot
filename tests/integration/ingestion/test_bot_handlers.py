"""Live-collection and group-command handlers, driven with real PTB objects and a fake bot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberMember,
    Message,
    Update,
    User,
)

from elysium_bot.app import Services
from elysium_bot.config import Settings
from elysium_bot.db.enums import AiConsent, AuditAction, ChatStatus, CoverageKind, Source
from elysium_bot.db.models import (
    AuditLog,
    ChatMember,
    ChatSettings,
    CoverageSegment,
    IngestionState,
    MessageSource,
    MessageVersion,
    TelegramChat,
)
from elysium_bot.db.models import (
    Message as MessageRow,
)
from elysium_bot.ingestion.bot_updates import on_group_message
from elysium_bot.scheduler.leases import JobService
from elysium_bot.telegram_ui import group as group_ui
from elysium_bot.telegram_ui import imports as imports_ui
from tests.fixtures import exports as fx

OWNER = User(id=111, first_name="Owner", is_bot=False)
ALICE = User(id=101, first_name="Alice", is_bot=False, username="alice")
BOT = User(id=999, first_name="ResearchBot", is_bot=True, username="research_bot")
GROUP = Chat(id=-1001234567890, type="supergroup", title="Synthetic Plays")
T = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)


@pytest.fixture
def services(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> Services:
    settings = Settings(_env_file=None, owner_user_ids="111", data_dir=str(tmp_path))  # type: ignore[call-arg]
    return Services(settings, engine, session_factory, JobService(session_factory))


def make_bot(*, admin: bool = False, can_read_all: bool = True) -> Any:
    bot = MagicMock()
    bot.id = BOT.id
    bot.can_read_all_group_messages = can_read_all
    member = (
        ChatMemberAdministrator(
            BOT, True, False, False, False, False, False, False, False, False, False, False, False
        )
        if admin
        else ChatMemberMember(BOT)
    )
    bot.get_chat_member = AsyncMock(return_value=member)
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=4242))
    bot.pin_chat_message = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    bot.defaults = None
    return bot


def make_context(services: Services, **bot_kw: Any) -> Any:
    app = MagicMock()
    app.create_task = MagicMock(side_effect=lambda coro, name=None: coro.close())
    return SimpleNamespace(
        bot=make_bot(**bot_kw),
        bot_data={"services": services, "settings": services.settings},
        application=app,
    )


def group_update(
    ctx: Any,
    text: str,
    *,
    mid: int = 10,
    user: User = ALICE,
    edit: datetime | None = None,
    update_id: int = 1,
    chat: Chat = GROUP,
    **kw: Any,
) -> Update:
    msg = Message(
        message_id=mid, date=T, chat=chat, from_user=user, text=text, edit_date=edit, **kw
    )
    msg.set_bot(ctx.bot)
    if edit is not None:
        return Update(update_id=update_id, edited_message=msg)
    return Update(update_id=update_id, message=msg)


def sent(ctx: Any, chat_id: int | None = None) -> list[str]:
    """Texts the fake bot sent (reply_text and send_message both end up here)."""
    out = []
    for call in ctx.bot.send_message.call_args_list:
        target = call.kwargs.get("chat_id", call.args[0] if call.args else None)
        text = call.kwargs.get("text", call.args[1] if len(call.args) > 1 else None)
        if chat_id is None or target == chat_id:
            out.append(text)
    return out


async def count(sf: async_sessionmaker[AsyncSession], model: type) -> int:
    async with sf() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def activate_group(services: Services) -> Any:
    ctx = make_context(services)
    await group_ui.activate(group_update(ctx, "/activate", user=OWNER, mid=1), ctx)
    ctx.bot.send_message.reset_mock()
    return ctx


# --- live collection --------------------------------------------------------------------------


async def test_messages_from_unactivated_chat_are_not_stored(services: Services) -> None:
    ctx = make_context(services)
    await on_group_message(group_update(ctx, "gm"), ctx)
    assert await count(services.sessions, MessageRow) == 0


async def test_message_edit_and_redelivery(services: Services) -> None:
    ctx = await activate_group(services)
    await on_group_message(group_update(ctx, "launch Nov 9", mid=10), ctx)
    edited = group_update(ctx, "launch Nov 10", mid=10, edit=T + timedelta(minutes=2), update_id=2)
    await on_group_message(edited, ctx)
    await on_group_message(edited, ctx)  # PTB redelivery of the same update
    assert await count(services.sessions, MessageRow) == 1
    assert await count(services.sessions, MessageVersion) == 2
    async with services.sessions() as s:
        assert (await s.execute(select(MessageSource.source))).scalars().all() == [Source.BOT_API]
        state = (await s.execute(select(IngestionState))).scalar_one()
        assert (state.last_tg_message_id, state.last_update_id) == (10, 2)
        seg = (await s.execute(select(CoverageSegment))).scalar_one()
        assert seg.kind == CoverageKind.OBSERVED and seg.source == Source.BOT_API


async def test_deactivated_chat_stops_collection(services: Services) -> None:
    ctx = await activate_group(services)
    await group_ui.deactivate(group_update(ctx, "/deactivate", user=OWNER, mid=2), ctx)
    await on_group_message(group_update(ctx, "after", mid=3), ctx)
    assert await count(services.sessions, MessageRow) == 0


async def test_basic_group_migration_continues_collection(services: Services) -> None:
    basic = Chat(id=-4242, type="group", title="Old basic group")
    ctx = make_context(services)
    await group_ui.activate(group_update(ctx, "/activate", user=OWNER, mid=1, chat=basic), ctx)
    migrated = Message(
        message_id=1, date=T, chat=GROUP, from_user=OWNER, migrate_from_chat_id=-4242
    )
    await on_group_message(Update(update_id=2, message=migrated), ctx)
    async with services.sessions() as s:
        chats = {c.tg_chat_id: c for c in (await s.execute(select(TelegramChat))).scalars()}
        assert chats[-4242].status == ChatStatus.DEACTIVATED
        assert chats[GROUP.id].status == ChatStatus.ACTIVE
        assert chats[GROUP.id].migrated_from_tg_chat_id == -4242
        assert await s.get(ChatSettings, chats[GROUP.id].id) is not None


# --- activation -------------------------------------------------------------------------------


async def test_activate_refuses_without_read_access(services: Services) -> None:
    ctx = make_context(services, can_read_all=False)
    await group_ui.activate(group_update(ctx, "/activate", user=OWNER, mid=1), ctx)
    assert sent(ctx) == [group_ui.PRIVACY_HELP]
    assert "/setprivacy" in group_ui.PRIVACY_HELP
    assert await count(services.sessions, TelegramChat) == 0


async def test_activate_accepts_admin_bot_even_in_privacy_mode(services: Services) -> None:
    ctx = make_context(services, admin=True, can_read_all=False)
    await group_ui.activate(group_update(ctx, "/activate", user=OWNER, mid=1), ctx)
    assert await count(services.sessions, TelegramChat) == 1


async def test_activate_requires_owner(services: Services) -> None:
    ctx = make_context(services)
    await group_ui.activate(group_update(ctx, "/activate", user=ALICE, mid=1), ctx)
    assert sent(ctx) == [group_ui.NOT_OWNER]


async def test_activate_posts_notice_and_audits(services: Services) -> None:
    ctx = make_context(services)
    await group_ui.activate(group_update(ctx, "/activate", user=OWNER, mid=1), ctx)
    assert sent(ctx, GROUP.id) == [group_ui.NOTICE]
    ctx.bot.pin_chat_message.assert_awaited_once()
    (dm,) = sent(ctx, OWNER.id)
    assert "Import earlier history" in dm
    async with services.sessions() as s:
        chat = (await s.execute(select(TelegramChat))).scalar_one()
        assert (chat.status, chat.notice_message_id, chat.activated_by_tg_user_id) == (
            ChatStatus.ACTIVE,
            4242,
            OWNER.id,
        )
        assert (await s.execute(select(AuditLog.action))).scalars().all() == [AuditAction.ACTIVATE]


# --- consent ----------------------------------------------------------------------------------


def consent_update(ctx: Any, action: str, user: User = ALICE) -> tuple[Update, AsyncMock]:
    message = Message(message_id=50, date=T, chat=GROUP, from_user=BOT, text="consent?")
    query = CallbackQuery(
        id="q1", from_user=user, chat_instance="ci", message=message, data=f"v1:consent:{action}"
    )
    query.set_bot(ctx.bot)
    ctx.bot.answer_callback_query.reset_mock()
    return Update(update_id=9, callback_query=query), ctx.bot.answer_callback_query


async def test_consent_grant_and_revoke(services: Services) -> None:
    ctx = await activate_group(services)
    upd, answer = consent_update(ctx, "grant")
    await group_ui.consent_callback(upd, ctx)
    assert answer.await_args is not None and answer.await_args.kwargs["show_alert"] is True
    async with services.sessions() as s:
        member = (await s.execute(select(ChatMember))).scalar_one()
        assert member.ai_consent == AiConsent.GRANTED and member.pseudonym == "U1"

    upd, _ = consent_update(ctx, "revoke")
    await group_ui.consent_callback(upd, ctx)
    upd, _ = consent_update(ctx, "revoke")  # no change → no extra audit row
    await group_ui.consent_callback(upd, ctx)
    async with services.sessions() as s:
        assert (await s.execute(select(ChatMember.ai_consent))).scalar_one() == AiConsent.REVOKED
        actions = (await s.execute(select(AuditLog.action).order_by(AuditLog.id))).scalars().all()
    assert actions == [AuditAction.ACTIVATE, AuditAction.CONSENT_CHANGE, AuditAction.CONSENT_CHANGE]


async def test_consent_info_and_alert_lengths(services: Services) -> None:
    ctx = make_context(services)
    upd, answer = consent_update(ctx, "info")
    await group_ui.consent_callback(upd, ctx)
    assert (
        answer.await_args is not None and answer.await_args.kwargs["text"] == group_ui.CONSENT_INFO
    )
    assert len(group_ui.CONSENT_INFO) <= 200  # Telegram callback alert limit


async def test_consent_command_shows_buttons(services: Services) -> None:
    ctx = make_context(services)
    await group_ui.consent_command(group_update(ctx, "/consent", mid=5), ctx)
    markup = ctx.bot.send_message.call_args.kwargs["reply_markup"]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == ["v1:consent:grant", "v1:consent:revoke", "v1:consent:info"]
    assert all(len(d.encode()) <= 64 for d in data)


# --- DM upload --------------------------------------------------------------------------------


def dm_document(file_name: str, size: int, user: User = OWNER) -> tuple[Update, AsyncMock, Any]:
    dm = Chat(id=user.id, type="private")
    doc = MagicMock()
    doc.file_name, doc.file_size = file_name, size
    tg_file = MagicMock()
    doc.get_file = AsyncMock(return_value=tg_file)
    message = MagicMock(document=doc, chat=dm)
    message.reply_text = AsyncMock()
    update = SimpleNamespace(effective_message=message, effective_user=user)
    return update, message.reply_text, tg_file  # type: ignore[return-value]


async def test_dm_upload_rejects_large_and_unauthorized(services: Services) -> None:
    update, reply, _ = dm_document("result.json", 25 * 1024 * 1024)
    await imports_ui.on_document(update, make_context(services))  # type: ignore[arg-type]
    assert reply.await_args is not None and "larger than 20 MB" in reply.await_args.args[0]

    update, reply, _ = dm_document("result.json", 1000, user=ALICE)
    await imports_ui.on_document(update, make_context(services))  # type: ignore[arg-type]
    assert reply.await_args is not None and reply.await_args.args[0] == imports_ui.NO_PERMISSION

    update, reply, _ = dm_document("photo.png", 1000)
    await imports_ui.on_document(update, make_context(services))  # type: ignore[arg-type]
    assert reply.await_args is not None and reply.await_args.args[0] == imports_ui.NOT_AN_EXPORT


async def test_dm_upload_runs_import_and_reports(services: Services, tmp_path: Path) -> None:
    ctx = await activate_group(services)
    source = fx.write_json(tmp_path / "upload.json", fx.chat_export(fx.supergroup_messages(10)))
    update, reply, tg_file = dm_document("result.json", source.stat().st_size)

    async def download(dest: Path) -> None:
        Path(dest).write_bytes(source.read_bytes())  # noqa: ASYNC240

    tg_file.download_to_drive = AsyncMock(side_effect=download)
    tasks: list[Any] = []
    ctx.application.create_task = MagicMock(side_effect=lambda coro, name=None: tasks.append(coro))
    await imports_ui.on_document(update, ctx)  # type: ignore[arg-type]
    assert reply.await_args is not None and "Import started" in reply.await_args.args[0]
    (task,) = tasks
    await task
    report = ctx.bot.send_message.await_args.args[1]
    assert "SUCCEEDED" in report and "new 12" in report
    assert await count(services.sessions, MessageRow) == 12
    assert list((tmp_path / "imports").iterdir()) == []  # uploaded file removed after success
