from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import time_machine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram import Chat, Message, Update, User

from elysium_bot.app import Services
from elysium_bot.config import Settings
from elysium_bot.db.enums import (
    AiConsentPolicy,
    AuditAction,
    ChatStatus,
    ChatType,
    ImportStatus,
    JobFamily,
    JobKind,
    JobStatus,
    ScheduleKind,
    Source,
    StoredRole,
)
from elysium_bot.db.models import (
    AuditLog,
    ChatRole,
    ChatSettings,
    ImportErrorRecord,
    ImportRun,
    Job,
    Schedule,
    TelegramChat,
    UiFlow,
)
from elysium_bot.ingestion import coverage
from elysium_bot.ingestion.normalizer import NormalizedMessage, SenderRef
from elysium_bot.ingestion.store import MessageStore
from elysium_bot.ops.health import POLLING, beat
from elysium_bot.scheduler.leases import JobService
from elysium_bot.telegram_ui import router as router_module
from elysium_bot.telegram_ui import screens
from elysium_bot.telegram_ui.router import EXPIRED, FOREIGN, NO_ACCESS, Throttle
from tests.integration.telegram_ui.harness import BOT_USER, Harness

OWNER = User(id=111, first_name="Owner", is_bot=False)
ADMIN = User(id=222, first_name="Admin", is_bot=False)
ANALYST = User(id=333, first_name="Analyst", is_bot=False)
VIEWER = User(id=444, first_name="Viewer", is_bot=False)
STRANGER = User(id=555, first_name="Stranger", is_bot=False)
NOW = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)  # 14:00 in Asia/Ho_Chi_Minh
SF = async_sessionmaker[AsyncSession]


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_module.router, "throttle", Throttle(limit=10_000))


@pytest.fixture
def services(engine: AsyncEngine, session_factory: SF, tmp_path: Path) -> Services:
    settings = Settings(_env_file=None, owner_user_ids="111", data_dir=str(tmp_path))  # type: ignore[call-arg]
    return Services(settings, engine, session_factory, JobService(session_factory))


async def seed(sf: SF, *, messages: int = 0, title: str = "Elysium Early Plays") -> int:
    async with sf() as s:
        chat = TelegramChat(
            tg_chat_id=-1001234567890,
            type=ChatType.SUPERGROUP,
            title=title,
            status=ChatStatus.ACTIVE,
        )
        s.add(chat)
        await s.flush()
        s.add(ChatSettings(chat_id=chat.id))
        for uid, role in (
            (222, StoredRole.ADMIN),
            (333, StoredRole.ANALYST),
            (444, StoredRole.VIEWER),
        ):
            s.add(ChatRole(chat_id=chat.id, tg_user_id=uid, role=role, granted_by_tg_user_id=111))
        if messages:
            base = datetime(2026, 10, 28, 3, 0, tzinfo=UTC)
            batch = [
                NormalizedMessage(
                    source=Source.BOT_API,
                    tg_chat_id=chat.tg_chat_id,
                    tg_message_id=i,
                    sender=SenderRef(kind="user", tg_id=100 + i % 4),
                    sent_at=base + timedelta(hours=i),
                    text=f"m{i}",
                )
                for i in range(1, messages + 1)
            ]
            await MessageStore(s, chat).upsert(batch)
            for m in batch:
                await coverage.extend_observed(
                    s, chat.id, Source.BOT_API, m.sent_at, m.tg_message_id
                )
        await s.commit()
        return chat.id


# --- entry & main menu ------------------------------------------------------------------------


@time_machine.travel(NOW, tick=False)
async def test_main_menu_with_empty_db_shows_only_real_numbers(services: Services) -> None:
    await seed(services.sessions)
    view = await Harness(services).open(OWNER)
    assert view.text == "\n".join(
        [
            "<b>ELYSIUM RESEARCH INTELLIGENCE</b>",
            "Group: <b>Elysium Early Plays</b>  ·  tz Asia/Ho_Chi_Minh",
            "",
            "Messages stored: <b>0</b>",
            "",
            "🟡 Collection  bot heartbeat stale",
            "🟡 History     live only — no export imported",
            "🟢 Index       up to date",
            "⚪ AI          not configured",
        ]
    )
    assert view.labels() == [
        "🔎 Scan Messages",
        "🧠 AI Analysis",
        "📄 Export Raw Chat",
        "📊 Research Projects",
        "🗂 Chat History",
        "⏰ Scheduled Scan",
        "⚙️ Settings",
        "ℹ️ Status",
        "❔ Help",
    ]


@time_machine.travel(NOW, tick=False)
async def test_main_menu_with_data(services: Services) -> None:
    chat_id = await seed(services.sessions, messages=30)
    await beat(services.sessions, POLLING)
    async with services.sessions() as s:
        s.add(
            ImportRun(
                chat_id=chat_id,
                source=Source.DESKTOP_EXPORT,
                status=ImportStatus.SUCCEEDED,
                finished_at=NOW,
            )
        )
        await coverage.record_gap(
            s, chat_id, Source.BOT_API, NOW - timedelta(days=2), NOW - timedelta(days=1), "down"
        )
        await s.commit()
    view = await Harness(services).open(OWNER)
    lines = view.text.split("\n")
    assert lines[3] == "Messages stored: <b>30</b>  (2026-10-28 → now)"
    assert lines[5] == "🟢 Collection  live · last msg 16:00"  # newest message 2026-10-29 09:00 UTC
    assert lines[6] == "🟡 History     export imported · 1 possible gap"
    assert lines[7] == "⏳ Index       30 pending"


async def test_roles_see_only_permitted_buttons(services: Services) -> None:
    await seed(services.sessions)
    h = Harness(services)
    analyst = (await h.open(ANALYST)).labels()
    assert (
        "📄 Export Raw Chat" not in analyst
        and "⚙️ Settings" not in analyst
        and "🧠 AI Analysis" in analyst
    )
    viewer = (await h.open(VIEWER)).labels()
    assert viewer == ["📊 Research Projects", "🗂 Chat History", "ℹ️ Status", "❔ Help"]


async def test_no_role_and_owner_without_chats(services: Services) -> None:
    h = Harness(services)
    await h.open(OWNER)
    assert h.replies[OWNER.id] == [screens.OWNER_NO_CHATS]
    await seed(services.sessions)
    await h.open(STRANGER)
    assert h.replies[STRANGER.id] == [screens.NO_ACCESS_TEXT]
    assert h.view(STRANGER).text == ""  # no control center was opened


async def test_shortcut_commands_open_screens(services: Services) -> None:
    await seed(services.sessions)
    h = Harness(services)
    assert (await h.open(OWNER, "/status")).text.startswith("<b>Status</b>")
    assert (await h.open(OWNER, "/settings")).text.startswith("<b>Settings</b>")
    blocked = await h.open(VIEWER, "/settings")
    assert blocked.text.startswith("<b>ELYSIUM RESEARCH INTELLIGENCE</b>")


async def test_group_menu_only_offers_deep_link(services: Services) -> None:
    chat_id = await seed(services.sessions, messages=3)
    h = Harness(services)
    group = Chat(id=-1001234567890, type="supergroup", title="Elysium Early Plays")
    msg = Message(message_id=1, date=NOW, chat=group, from_user=ADMIN, text="/menu")
    msg.set_bot(h.bot)
    await screens.group_menu(Update(update_id=1, message=msg), h.context)  # type: ignore[arg-type]
    (text,) = h.replies[group.id]
    assert text == "The research control center opens in a private chat with me."
    (button,) = h.last_reply_markup.inline_keyboard[0]
    assert button.url == f"https://t.me/{BOT_USER.username}?start=cc_{chat_id}"
    assert button.callback_data is None
    # The deep link opens the control center for that chat.
    view = await h.open(ADMIN, "/start", [f"cc_{chat_id}"])
    assert "Elysium Early Plays" in view.text


# --- callback safety --------------------------------------------------------------------------


async def test_all_callback_data_fits_64_bytes(services: Services) -> None:
    """Crawl every reachable screen as the owner and check every button's callback data."""
    await seed(services.sessions, messages=5)
    h = Harness(services)
    await h.open(OWNER)
    seen: set[str] = set()
    frontier = [d for _, d, _ in h.view(OWNER).buttons() if d]
    skip = (
        "set:deact",
        "cons_notice",
        "acc:",
        "cfm",
    )  # stateful/destructive paths covered elsewhere
    presses = 0
    while frontier and presses < 400:
        data = frontier.pop()
        if data in seen:
            continue
        seen.add(data)
        assert len(data.encode()) <= 64, data
        if any(s in data for s in skip):
            continue
        await h.press_data(OWNER, data)
        presses += 1
        for _, d, _ in h.view(OWNER).buttons():
            if d and d not in seen:
                frontier.append(d)
        if presses % 25 == 0:  # return to the root occasionally to explore other branches
            await h.open(OWNER)
            frontier += [d for _, d, _ in h.view(OWNER).buttons() if d and d not in seen]
    assert presses > 40
    assert len(seen) > 150


async def test_foreign_user_cannot_press_someone_elses_menu(services: Services) -> None:
    await seed(services.sessions)
    h = Harness(services)
    await h.open(OWNER)
    data = h.view(OWNER).data_for("Settings")
    before = h.view(OWNER).text
    alerts = await h.press_data(ADMIN, data, owner_view=OWNER)
    assert alerts == [FOREIGN]
    assert h.view(OWNER).text == before


async def test_expired_menu(services: Services) -> None:
    await seed(services.sessions)
    h = Harness(services)
    with time_machine.travel(NOW, tick=False) as t:
        await h.open(OWNER)
        t.shift(timedelta(minutes=31))
        assert await h.press_data(OWNER, h.view(OWNER).data_for("Status")) == [EXPIRED]


async def test_revoked_role_fails_on_next_press(services: Services) -> None:
    chat_id = await seed(services.sessions)
    h = Harness(services)
    await h.open(ADMIN)
    async with services.sessions() as s:
        await s.delete(await s.get(ChatRole, (chat_id, ADMIN.id)))
        await s.commit()
    assert await h.press_data(ADMIN, h.view(ADMIN).data_for("Status")) == [NO_ACCESS]


async def test_unknown_or_tampered_callback(services: Services) -> None:
    await seed(services.sessions)
    h = Harness(services)
    await h.open(OWNER)
    token = h.view(OWNER).data_for("Status").split(":")[2]
    assert await h.press_data(OWNER, f"v1:boom:{token}") == ["Unknown action."]
    assert await h.press_data(OWNER, "garbage") == ["Unknown action."]


async def test_throttle_blocks_double_taps(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(services.sessions)
    clock = [0.0]
    monkeypatch.setattr(
        router_module.router, "throttle", Throttle(limit=3, per_seconds=1.0, clock=lambda: clock[0])
    )
    h = Harness(services)
    await h.open(OWNER)
    data = h.view(OWNER).data_for("Status")
    for _ in range(3):
        await h.press_data(OWNER, data)
    assert await h.press_data(OWNER, data) == ["Slow down a little 🙂"]
    clock[0] = 2.0
    assert await h.press_data(OWNER, data) == []


async def test_viewer_crafted_callback_is_refused(services: Services) -> None:
    await seed(services.sessions)
    h = Harness(services)
    await h.open(VIEWER)
    token = h.view(VIEWER).data_for("Status").split(":")[2]
    alerts = await h.press_data(VIEWER, f"v1:set:{token}:pseudo")
    assert alerts == ["Your role doesn't allow this."]


# --- range picker -----------------------------------------------------------------------------


@time_machine.travel(NOW, tick=False)
async def test_range_preset_confirmation_card(services: Services) -> None:
    await seed(services.sessions, messages=5)
    h = Harness(services)
    await h.open(OWNER)
    await h.press(OWNER, "Export Raw Chat")
    assert h.view(OWNER).text.startswith("<b>Select time range</b> — for: Export Raw Chat")
    view = await h.press(OWNER, "4h")
    assert view.text.split("\n")[:5] == [
        "<b>Range</b> — Last 4h",
        "From  Sun 01 Nov 2026 10:00  (Asia/Ho_Chi_Minh)",
        "To    Sun 01 Nov 2026 14:00",
        "      = 03:00–07:00 UTC",
        "Stored messages in range: 0",
    ]
    done = await h.press(OWNER, "Continue")
    assert "Export Raw Chat" in done.text and "Choose a format. No AI is used." in done.text


@time_machine.travel(NOW, tick=False)
async def test_entire_stored_history_is_labelled_stored_with_caveat(services: Services) -> None:
    chat_id = await seed(services.sessions, messages=5)
    async with services.sessions() as s:
        await coverage.record_gap(
            s, chat_id, Source.BOT_API, NOW - timedelta(days=3), NOW - timedelta(days=2), "down"
        )
        await s.commit()
    h = Harness(services)
    await h.open(OWNER)
    await h.press(OWNER, "Scan Messages")
    view = await h.press(OWNER, "Entire stored history")
    assert view.text.startswith("<b>Range</b> — Entire stored history")
    assert "Stored messages in range: 5" in view.text
    assert "⚠️ Partial" in view.text


@time_machine.travel(NOW, tick=False)
async def test_calendar_custom_range_and_month_navigation(services: Services) -> None:
    await seed(services.sessions, messages=5)
    h = Harness(services)
    await h.open(OWNER)
    await h.press(OWNER, "Scan Messages")
    cal = await h.press(OWNER, "Custom range")
    assert "Nov 2026" in cal.labels()
    assert "·2" in cal.labels() and "·28" in cal.labels()  # no stored messages in November
    await h.press_data(OWNER, cal.data_for("‹"))
    assert "Oct 2026" in h.view(OWNER).labels()
    assert "28" in h.view(OWNER).labels()  # Oct 28 has stored messages
    for _ in range(3):
        await h.press_data(OWNER, h.view(OWNER).data_for("›"))
    assert "Jan 2027" in h.view(OWNER).labels()  # Dec → Jan rollover
    await h.press_data(OWNER, h.view(OWNER).data_for("‹"))
    await h.press_data(OWNER, h.view(OWNER).data_for("‹"))
    await h.press(OWNER, "·1")  # Nov 1
    await h.press(OWNER, "09")
    await h.press(OWNER, ":30")
    await h.press(OWNER, "End = now")
    view = h.view(OWNER)
    assert "From  Sun 01 Nov 2026 09:30" in view.text and "To    Sun 01 Nov 2026 14:00" in view.text


@time_machine.travel(NOW, tick=False)
async def test_typed_range_input(services: Services) -> None:
    await seed(services.sessions, messages=5)
    h = Harness(services)
    await h.open(OWNER)
    await h.press(OWNER, "AI Analysis")
    await h.press(OWNER, "Type range")
    view = await h.type(OWNER, "not a range")
    assert "Couldn't read that range" in view.text
    assert h.replies[OWNER.id][-1].startswith("Couldn't read that range")
    view = await h.type(OWNER, "2026-10-28 10:00 → 2026-10-28 16:00")
    assert "Stored messages in range: 5" in view.text
    modes = await h.press(OWNER, "Continue")
    assert "Choose the analysis mode" in modes.text
    final = await h.press(OWNER, "Early Plays")
    assert "Elysium Early Plays" in final.text and "Phase 5" in final.text


# --- settings, access, schedules --------------------------------------------------------------


async def audit_actions(sf: SF) -> list[AuditAction]:
    async with sf() as s:
        return list((await s.execute(select(AuditLog.action).order_by(AuditLog.id))).scalars())


async def test_settings_persist_and_audit(services: Services) -> None:
    chat_id = await seed(services.sessions)
    h = Harness(services)
    await h.open(OWNER, "/settings")
    await h.press(OWNER, "Pseudonymize exports")
    await h.press(OWNER, "Timezone")
    await h.press(OWNER, "Asia/Singapore")
    await h.press(OWNER, "Timezone")
    await h.press(OWNER, "Type a timezone")
    await h.type(OWNER, "Mars/Base")
    assert h.replies[OWNER.id][-1] == "Unknown timezone: Mars/Base"
    await h.type(OWNER, "Asia/Bangkok")
    await h.press(OWNER, "AI defaults")
    await h.press(OWNER, "Routine: Opus 5.5")
    async with services.sessions() as s:
        row = await s.get(ChatSettings, chat_id)
        assert row is not None
        assert (row.pseudonymize_exports, row.timezone, row.routine_model) == (
            True,
            "Asia/Bangkok",
            "claude-opus-5-5",
        )
        assert row.updated_by_tg_user_id == OWNER.id
    assert await audit_actions(services.sessions) == [AuditAction.SETTINGS_CHANGE] * 4


async def test_owner_only_settings_hidden_and_refused_for_admin(services: Services) -> None:
    chat_id = await seed(services.sessions)
    h = Harness(services)
    view = await h.open(ADMIN, "/settings")
    assert not any(
        "consent" in label.lower() or "Retention" in label or "Deactivate" in label
        for label in view.labels()
    )
    token = view.data_for("Timezone").split(":")[2]
    alerts = await h.press_data(ADMIN, f"v1:set:{token}:cons_strict")
    assert alerts == ["Only the owner can change this."]
    await h.press_data(ADMIN, f"v1:set:{token}:ret_30")
    async with services.sessions() as s:
        row = await s.get(ChatSettings, chat_id)
        assert row is not None and row.retention_days is None


async def test_notice_only_policy_requires_confirmation(services: Services) -> None:
    chat_id = await seed(services.sessions)
    h = Harness(services)
    await h.open(OWNER, "/settings")
    await h.press(OWNER, "AI consent policy")
    view = await h.press(OWNER, "notice_only")
    assert "Switch AI consent policy to notice-only?" in view.text
    await h.press(OWNER, "Cancel")
    await h.press(OWNER, "AI consent policy")
    await h.press(OWNER, "notice_only")
    view = await h.press(OWNER, "Confirm")
    assert "notice_only  ⚠️" in view.text
    async with services.sessions() as s:
        row = await s.get(ChatSettings, chat_id)
        assert row is not None and row.ai_consent_policy == AiConsentPolicy.NOTICE_ONLY


async def test_deactivate_requires_typed_title(services: Services) -> None:
    chat_id = await seed(services.sessions)
    h = Harness(services)
    await h.open(OWNER, "/settings")
    await h.press(OWNER, "Deactivate group")
    await h.type(OWNER, "wrong title")
    assert h.replies[OWNER.id][-1] == "Title didn't match."
    view = await h.type(OWNER, "Elysium Early Plays")
    assert "Collection  deactivated" in view.text
    async with services.sessions() as s:
        chat = await s.get(TelegramChat, chat_id)
        assert chat is not None and chat.status == ChatStatus.DEACTIVATED
    assert AuditAction.DEACTIVATE in await audit_actions(services.sessions)


async def test_manage_access_grant_by_id_and_forward_then_revoke(services: Services) -> None:
    chat_id = await seed(services.sessions)
    h = Harness(services)
    await h.open(OWNER, "/settings")
    await h.press(OWNER, "Manage access")
    await h.press(OWNER, "Add user")
    await h.type(OWNER, "hello")
    assert h.replies[OWNER.id][-1] == "Not a user ID."
    pick = await h.type(OWNER, "777")
    assert pick.labels()[:3] == ["analyst", "viewer", "admin"]
    view = await h.press(OWNER, "analyst")
    assert "• 777 — analyst" in view.text
    await h.press(OWNER, "Add user")
    await h.type(OWNER, "fwd", forward_from=User(id=888, first_name="Fwd", is_bot=False))
    await h.press(OWNER, "viewer")
    view = await h.press(OWNER, "Remove 777")
    assert "777" not in view.text.split("Owners")[1].split("\n", 1)[1]
    async with services.sessions() as s:
        roles = dict(
            (
                await s.execute(
                    select(ChatRole.tg_user_id, ChatRole.role).where(ChatRole.chat_id == chat_id)
                )
            ).all()
        )
    assert roles[888] == StoredRole.VIEWER and 777 not in roles


async def test_admin_cannot_grant_or_remove_admins(services: Services) -> None:
    await seed(services.sessions)
    h = Harness(services)
    await h.open(ADMIN, "/settings")
    view = await h.press(ADMIN, "Manage access")
    assert not any("Remove 222" in label for label in view.labels())  # admins can't remove admins
    await h.press(ADMIN, "Add user")
    pick = await h.type(ADMIN, "999000")
    assert pick.labels()[:2] == ["analyst", "viewer"] and "admin" not in pick.labels()


async def test_schedule_persists_rows(services: Services) -> None:
    chat_id = await seed(services.sessions)
    h = Harness(services)
    await h.open(OWNER, "/schedule")
    await h.press_data(OWNER, h.view(OWNER).buttons()[3][1] or "")  # Collection: 2h
    await h.press_data(OWNER, h.view(OWNER).buttons()[6 + 4][1] or "")  # AI summary: 3h
    await h.press(OWNER, "Early Plays")
    view = await h.press(OWNER, "50")
    assert "Mode early_plays · min 50 new messages" in view.text
    async with services.sessions() as s:
        rows = {
            r.kind: r
            for r in (
                await s.execute(select(Schedule).where(Schedule.chat_id == chat_id))
            ).scalars()
        }
        settings_row = await s.get(ChatSettings, chat_id)
    assert (
        rows[ScheduleKind.COLLECTION].interval_min == 120 and rows[ScheduleKind.COLLECTION].enabled
    )
    assert rows[ScheduleKind.AI_SUMMARY].interval_min == 180
    assert rows[ScheduleKind.AI_SUMMARY].params == {
        "mode": "early_plays",
        "min_messages": 50,
        "destination": "owner_dm",
    }
    assert settings_row is not None and settings_row.summary_interval_min == 180


# --- status, history ------------------------------------------------------------------------


async def test_status_lists_and_cancels_running_job(services: Services) -> None:
    chat_id = await seed(services.sessions)
    job = await services.jobs.acquire_lease(chat_id, JobFamily.INGEST, JobKind.IMPORT)
    assert job is not None
    h = Harness(services)
    view = await h.open(OWNER, "/status")
    assert f"#{job.id} import since" in view.text
    assert "schema 0003" in view.text and "Anthropic API not configured" in view.text
    view = await h.press(OWNER, f"Cancel #{job.id}")
    assert view.alerts == [f"Job #{job.id} cancelled."]
    async with services.sessions() as s:
        row = await s.get(Job, job.id)
        assert row is not None and row.status == JobStatus.CANCELLED


async def test_history_shows_coverage_imports_and_paginated_errors(services: Services) -> None:
    chat_id = await seed(services.sessions, messages=5)
    async with services.sessions() as s:
        run = ImportRun(
            chat_id=chat_id,
            source=Source.DESKTOP_EXPORT,
            status=ImportStatus.PARTIAL,
            inserted=10,
            failed=12,
        )
        s.add(run)
        await s.flush()
        s.add_all(
            [
                ImportErrorRecord(
                    import_run_id=run.id, error_code="missing_date", record_ref=f"message id {i}"
                )
                for i in range(12)
            ]
        )
        await s.commit()
    h = Harness(services)
    view = await h.open(OWNER, "/history")
    assert "Stored history covers" in view.text and "Stored by source: bot_api 5" in view.text
    assert f"#{run.id} desktop_export" in view.text
    errors = await h.press(OWNER, f"Errors in #{run.id}")
    assert errors.text.startswith(f"<b>Import #{run.id} — errors</b> (12)")
    assert errors.text.count("missing_date") == 8 and "1/2" in errors.labels()
    page2 = await h.press(OWNER, "Next")
    assert page2.text.count("missing_date") == 4 and "2/2" in page2.labels()


async def test_flow_sweep_removes_expired(services: Services) -> None:
    await seed(services.sessions)
    h = Harness(services)
    with time_machine.travel(NOW, tick=False) as t:
        await h.open(OWNER)
        t.shift(timedelta(hours=1))
        from elysium_bot.telegram_ui.flows import FlowStore

        assert await FlowStore(services.sessions).sweep() == 1
    async with services.sessions() as s:
        assert (await s.execute(select(UiFlow))).first() is None


async def test_multi_chat_selector(services: Services) -> None:
    await seed(services.sessions)
    async with services.sessions() as s:
        other = TelegramChat(
            tg_chat_id=-100777,
            type=ChatType.SUPERGROUP,
            title="Second Group",
            status=ChatStatus.ACTIVE,
        )
        s.add(other)
        await s.commit()
    h = Harness(services)
    view = await h.open(OWNER)
    assert view.labels()[0] == "Group: Elysium Early Plays ▾"
    await h.press(OWNER, "Group:")
    view = await h.press(OWNER, "Second Group")
    assert "Group: <b>Second Group</b>" in view.text
    single = await h.open(ADMIN)
    assert not single.labels()[0].startswith("Group:")  # admin only has one group
