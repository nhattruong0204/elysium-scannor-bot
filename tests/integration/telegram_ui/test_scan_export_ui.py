from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import time_machine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram import User

from elysium_bot.app import Services
from elysium_bot.config import Settings
from elysium_bot.db.enums import AuditAction, ScheduleKind, StoredRole
from elysium_bot.db.models import AuditLog, ChatRole, Schedule
from elysium_bot.scheduler.leases import JobService
from elysium_bot.scheduler.registry import ScheduleRegistry
from elysium_bot.telegram_ui import router as router_module
from elysium_bot.telegram_ui import scan_export_screens
from elysium_bot.telegram_ui.router import Throttle
from tests.integration.scan_export import fixtures as fx
from tests.integration.scan_export.test_services import FakeJobQueue
from tests.integration.telegram_ui.harness import Harness

OWNER = User(id=111, first_name="Owner", is_bot=False)
VIEWER = User(id=444, first_name="Viewer", is_bot=False)
ANALYST = User(id=333, first_name="Analyst", is_bot=False)
NOW = fx.BASE + timedelta(hours=2)  # 10:00 local
SF = async_sessionmaker[AsyncSession]


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_module.router, "throttle", Throttle(limit=10_000))


@pytest.fixture
def services(engine: AsyncEngine, session_factory: SF, tmp_path: Path) -> Services:
    settings = Settings(_env_file=None, owner_user_ids="111", data_dir=str(tmp_path))  # type: ignore[call-arg]
    return Services(settings, engine, session_factory, JobService(session_factory))


async def seed(services: Services) -> int:
    chat_id = await fx.seed_chat(services.sessions)
    async with services.sessions() as s:
        s.add(
            ChatRole(
                chat_id=chat_id, tg_user_id=444, role=StoredRole.VIEWER, granted_by_tg_user_id=111
            )
        )
        s.add(
            ChatRole(
                chat_id=chat_id, tg_user_id=333, role=StoredRole.ANALYST, granted_by_tg_user_id=111
            )
        )
        await s.commit()
    return chat_id


async def drain_background() -> None:
    if scan_export_screens.BACKGROUND:
        await asyncio.gather(*list(scan_export_screens.BACKGROUND))


async def test_scan_flow_shows_stats_and_follow_ups(services: Services) -> None:
    await seed(services)
    traveller = time_machine.travel(NOW, tick=False)
    clock = traveller.start()
    h = Harness(services)
    await h.open(OWNER, "/scan")
    await h.press(OWNER, "4h")
    view = await h.press(OWNER, "Continue")
    assert view.text.startswith("<b>Scan — Last 4h</b>")
    assert "Messages <b>7</b> · service 1 · active members 4" in view.text
    assert (
        f"<code>{fx.CA_VALID}</code> — 2×" in view.text and "(extracted, not verified)" in view.text
    )
    assert "$NEST 3 · $HYPE 1" in view.text and "No AI was used." in view.text
    src = await h.press(OWNER, "Source messages")
    assert src.text.startswith("<b>Source messages</b> (7)") and "1/1" in src.labels()
    back = await h.press(OWNER, "Back")
    export = await h.press(OWNER, "Export this range")
    assert "Choose a format" in export.text
    assert back is export
    # "Since last scan" now uses the checkpoint written by this scan.
    clock.shift(timedelta(minutes=20))  # stay inside the 30-min flow TTL
    await h.press(OWNER, "Menu")
    await h.press(OWNER, "Scan Messages")
    since = await h.press(OWNER, "Since last scan")
    traveller.stop()
    assert since.text.startswith("<b>Range</b> — Since last scan")
    assert (
        "From  Sun 01 Nov 2026 10:00" in since.text and "To    Sun 01 Nov 2026 10:20" in since.text
    )


@time_machine.travel(NOW, tick=False)
async def test_export_flow_delivers_documents_and_cleans_up(
    services: Services, tmp_path: Path
) -> None:
    await seed(services)
    h = Harness(services)
    await h.open(OWNER, "/export")
    await h.press(OWNER, "4h")
    await h.press(OWNER, "Continue")
    confirm = await h.press(OWNER, "JSON")
    assert "Format JSON · rows 6" in confirm.text and "No AI is used" in confirm.text
    await h.press(OWNER, "Pseudonymize")
    started = await h.press(OWNER, "✅ Export")
    assert "Exporting…" in started.text
    await drain_background()
    (doc,) = h.documents
    assert doc["chat_id"] == OWNER.id and doc["filename"].endswith(".json")
    rows = json.loads(doc["content"])["messages"]
    assert len(rows) == 6 and all(str(r["sender_id"]).startswith("U") for r in rows)
    assert "Export ready" in h.view(OWNER).text
    assert list((tmp_path / "tmp" / "exports").iterdir()) == []
    async with services.sessions() as s:
        assert (
            await s.execute(select(AuditLog.action).where(AuditLog.action == AuditAction.EXPORT))
        ).scalars().all() == [AuditAction.EXPORT]


async def test_viewer_and_analyst_cannot_export(services: Services) -> None:
    await seed(services)
    h = Harness(services)
    for user in (VIEWER, ANALYST):
        view = await h.open(user)
        assert "📄 Export Raw Chat" not in view.labels()
        token = view.data_for("Status").split(":")[2]
        assert await h.press_data(user, f"v1:exp:{token}:go") == ["Your role doesn't allow this."]
    assert h.documents == []


@time_machine.travel(NOW, tick=False)
async def test_search_from_history(services: Services) -> None:
    await seed(services)
    h = Harness(services)
    await h.open(OWNER, "/history")
    await h.press(OWNER, "Search messages")
    view = await h.type(OWNER, "$NEST")
    assert view.text.startswith("<b>Search</b>: $NEST — 3 results")
    assert view.text.count("🔗") == 3 and "<b>$NEST</b>" in view.text
    await h.press(OWNER, "New search")
    view = await h.type(OWNER, fx.CA_VALID)
    assert "(exact contract match) — 2 results" in view.text


@time_machine.travel(NOW, tick=False)
async def test_schedule_change_registers_timer(services: Services) -> None:
    chat_id = await seed(services)
    jq = FakeJobQueue()
    services.schedules = ScheduleRegistry(services.sessions, services.jobs, jq)
    h = Harness(services)
    await h.open(OWNER, "/schedule")
    await h.press_data(OWNER, h.view(OWNER).buttons()[2][1] or "")  # Collection: 1h
    assert [j.name for j in jq.jobs if not j.removed] == [
        f"schedule:{_id}" for _id in await _collection_ids(services, chat_id)
    ]
    await h.press_data(OWNER, h.view(OWNER).buttons()[1][1] or "")  # Collection: Off
    assert all(j.removed for j in jq.jobs)


async def _collection_ids(services: Services, chat_id: int) -> list[int]:
    async with services.sessions() as s:
        return list(
            (
                await s.execute(
                    select(Schedule.id).where(
                        Schedule.chat_id == chat_id, Schedule.kind == ScheduleKind.COLLECTION
                    )
                )
            ).scalars()
        )


_ = datetime, UTC
