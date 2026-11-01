from __future__ import annotations

import csv
import io
import json
import os
import stat
import sys
import zipfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import time_machine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.enums import AuditAction, JobFamily, JobKind, JobStatus, Role, ScheduleKind
from elysium_bot.db.models import AuditLog, Job, ScanCheckpoint, Schedule
from elysium_bot.db.scope import ChatScope
from elysium_bot.exports import service as export_service
from elysium_bot.exports.service import ExportRefused, ExportRequest, export_chat, sweep_tmp
from elysium_bot.exports.writers import FIELDS
from elysium_bot.scan.service import run_scan
from elysium_bot.scheduler.leases import JobService
from elysium_bot.scheduler.registry import ScheduleRegistry
from elysium_bot.search.index import MARK_CLOSE, MARK_OPEN, fts5_query, search_messages
from elysium_bot.telegram_ui.components import Window
from tests.integration.scan_export import fixtures as fx

SF = async_sessionmaker[AsyncSession]
WINDOW = Window(fx.BASE, fx.BASE + timedelta(hours=1), "test")


def scope(chat_id: int, user: int = 111, role: Role = Role.OWNER) -> ChatScope:
    return ChatScope(chat_id, user, role)


# --- search -----------------------------------------------------------------------------------


def test_fts5_query_sanitizer() -> None:
    assert fts5_query("nest launch") == '"nest" "launch"'
    assert fts5_query('"mainnet date" $NEST') == '"mainnet date" "$NEST"'
    assert fts5_query('a" OR b*') == '"a" "OR" "b"'
    assert fts5_query("   ") is None
    assert fts5_query('NEAR(x y) "un""bal') == '"NEAR(x" "y" "un" "bal"'


async def test_search_ticker_is_not_a_plain_word(session_factory: SF) -> None:
    chat_id = await fx.seed_chat(session_factory)
    async with session_factory() as s:
        tick = await search_messages(s, scope(chat_id), "$NEST")
        word = await search_messages(s, scope(chat_id), "nest")
        other = await search_messages(s, scope(chat_id + 99), "$NEST")
    assert sorted(h.tg_message_id for h in tick.hits) == [1, 2, 7]
    assert tick.total == 3 and tick.mode == "fts"
    # Plain "nest" matches the word (2) and the URL host part docs.nest.example (1), but never the
    # cashtag-only message 7: "$NEST" is a single token.
    assert sorted(h.tg_message_id for h in word.hits) == [1, 2]
    assert all(MARK_OPEN in h.snippet and MARK_CLOSE in h.snippet for h in word.hits)
    assert other.total == 0  # chat-scoped


async def test_search_full_contract_address_is_exact(session_factory: SF) -> None:
    chat_id = await fx.seed_chat(session_factory)
    async with session_factory() as s:
        result = await search_messages(s, scope(chat_id), fx.CA_VALID)
        prefix = await search_messages(s, scope(chat_id), fx.CA_VALID[:20])
    assert result.mode == "contract" and sorted(h.tg_message_id for h in result.hits) == [1, 3]
    assert prefix.mode == "fts"


async def test_search_window_and_paging(session_factory: SF) -> None:
    chat_id = await fx.seed_chat(session_factory)
    async with session_factory() as s:
        late = await search_messages(
            s, scope(chat_id), "$NEST", start=fx.BASE + timedelta(minutes=30)
        )
        page = await search_messages(s, scope(chat_id), "$NEST", limit=2, offset=2)
    assert [h.tg_message_id for h in late.hits] == [7]
    assert page.total == 3 and len(page.hits) == 1


# --- scan -------------------------------------------------------------------------------------


async def test_scan_stats_match_hand_counts(session_factory: SF) -> None:
    chat_id = await fx.seed_chat(session_factory)
    jobs = JobService(session_factory)
    result = await run_scan(
        session_factory, scope(chat_id), WINDOW, tz="Asia/Ho_Chi_Minh", jobs=jobs
    )
    assert (result.messages, result.service_messages) == (7, 1)
    assert result.active_members == 4  # Alice, Bob, Carol, PriceBot
    assert result.top_senders[:3] == [("Alice Anders (U1)", 2), ("Bob (U2)", 2), ("Carol (U3)", 2)]
    by_addr = {c.address.lower(): c for c in result.contracts}
    assert by_addr[fx.CA_VALID.lower()].mentions == 2
    assert by_addr[fx.CA_LOWER].checksum_state == "all_lower_or_upper"
    assert result.tickers == [("NEST", 3), ("HYPE", 1)]
    assert dict(result.top_domains) == {
        "docs.nest.example": 1,
        "elysium.kinetiq.xyz": 1,
        "dexscreener.com": 1,
    }
    assert (result.replies, result.threads) == (2, 1)
    assert result.coverage.complete_for_window
    async with session_factory() as s:
        cp = await s.get(ScanCheckpoint, (chat_id, 111))
        job = await s.get(Job, result.job_id)
    assert cp is not None and cp.last_scan_end == WINDOW.end
    assert job is not None and job.status == JobStatus.SUCCEEDED


# --- exports ----------------------------------------------------------------------------------


async def _export(
    sf: SF, chat_id: int, tmp_path: Path, fmt: str, **kw: object
) -> export_service.ExportResult:
    return await export_chat(
        sf,
        scope(chat_id),
        ExportRequest(WINDOW, fmt, **kw),
        data_dir=tmp_path,
        max_per_hour=50,  # type: ignore[arg-type]
        jobs=JobService(sf),
    )


async def test_json_export_round_trip(session_factory: SF, tmp_path: Path) -> None:
    chat_id = await fx.seed_chat(session_factory)
    result = await _export(session_factory, chat_id, tmp_path, "json")
    (path,) = result.files
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["messages"]
    assert [r["tg_message_id"] for r in rows] == [
        1,
        2,
        3,
        4,
        5,
        8,
    ]  # no service (6), no bot (7) by default
    assert set(rows[0]) == set(FIELDS)
    first = rows[0]
    assert first["contract_strings"] == [
        {"value": fx.CA_VALID, "format": "evm", "checksum_state": "valid_checksum"}
    ]
    assert first["links"] == ["https://docs.nest.example/start"]
    assert first["message_link"] == "https://t.me/c/1234567890/1"
    assert rows[1]["reply_to_tg_message_id"] == 1 and rows[1]["edit_count"] == 1
    assert "NOT verified" in data["meta"]["note"]
    assert data["meta"]["coverage_complete"] is True and data["summary"]["row_count"] == 6


async def test_csv_txt_md_contain_every_message(session_factory: SF, tmp_path: Path) -> None:
    chat_id = await fx.seed_chat(session_factory)
    csv_path = (
        await _export(
            session_factory, chat_id, tmp_path, "csv", include_service=True, include_bots=True
        )
    ).files[0]
    raw = csv_path.read_text(encoding="utf-8-sig")
    assert raw.startswith("# generated_at_utc:") and "# coverage: Stored history covers" in raw
    reader = csv.DictReader(
        io.StringIO("".join(line for line in raw.splitlines(True) if not line.startswith("#")))
    )
    rows = list(reader)
    assert reader.fieldnames == FIELDS
    assert [int(r["tg_message_id"]) for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert rows[4]["text"] == 'gm, "quoted" text, commas, and\nnewlines'
    for fmt in ("txt", "md"):
        body = (
            (
                await _export(
                    session_factory, chat_id, tmp_path, fmt, include_service=True, include_bots=True
                )
            )
            .files[0]
            .read_text()
        )
        for mid in range(1, 9):
            assert f"#{mid}" in body, (fmt, mid)
    golden = Path(__file__).parent / "golden_txt_lines.txt"
    txt = (
        (await _export(session_factory, chat_id, tmp_path, "txt")).files[0].read_text().splitlines()
    )
    message_lines = [line for line in txt if line.startswith(("[", "    "))]
    assert message_lines == golden.read_text().splitlines()


async def test_pseudonymized_export_contains_no_identity(
    session_factory: SF, tmp_path: Path
) -> None:
    chat_id = await fx.seed_chat(session_factory)
    for fmt in ("json", "csv", "txt", "md"):
        body = (
            (
                await _export(
                    session_factory, chat_id, tmp_path, fmt, pseudonymize=True, include_bots=True
                )
            )
            .files[0]
            .read_text()
        )
        for needle in ("Alice", "Anders", "alice_x", "bobby", "Carol", "PriceBot", "price_bot"):
            assert needle not in body, (fmt, needle)
        assert "U1" in body
        if fmt == "json":
            rows = json.loads(body)["messages"]
            assert all(
                str(r["sender_id"]).startswith("U") and r["sender_name"] == r["sender_id"]
                for r in rows
            )
            assert all(r["sender_username"] is None and r["forward_from"] is None for r in rows)
            assert not {101, 202, 303, 909} & {r["sender_id"] for r in rows}


async def test_export_header_states_partial_coverage(session_factory: SF, tmp_path: Path) -> None:
    chat_id = await fx.seed_chat(session_factory)
    early = Window(fx.BASE - timedelta(days=5), fx.BASE + timedelta(hours=1), "early")
    result = await export_chat(
        session_factory,
        scope(chat_id),
        ExportRequest(early, "json"),
        data_dir=tmp_path,
        max_per_hour=50,
    )
    meta = json.loads(result.files[0].read_text())["meta"]
    assert meta["coverage_complete"] is False and meta["coverage"].startswith(
        "⚠️ Partial: no stored messages before"
    )


async def test_export_rate_limit_and_audit(session_factory: SF, tmp_path: Path) -> None:
    chat_id = await fx.seed_chat(session_factory)
    for _ in range(5):
        await export_chat(
            session_factory,
            scope(chat_id),
            ExportRequest(WINDOW, "txt"),
            data_dir=tmp_path,
            max_per_hour=5,
        )
    with pytest.raises(ExportRefused, match="limit"):
        await export_chat(
            session_factory,
            scope(chat_id),
            ExportRequest(WINDOW, "txt"),
            data_dir=tmp_path,
            max_per_hour=5,
        )
    # A different user still can.
    await export_chat(
        session_factory,
        scope(chat_id, user=222, role=Role.ADMIN),
        ExportRequest(WINDOW, "txt"),
        data_dir=tmp_path,
        max_per_hour=5,
    )
    async with session_factory() as s:
        actions = (await s.execute(select(AuditLog.action))).scalars().all()
    assert actions.count(AuditAction.EXPORT) == 6


async def test_large_parts_are_split_and_zipped(
    session_factory: SF, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_id = await fx.seed_chat(session_factory)
    monkeypatch.setattr(export_service, "PART_RAW_BYTES", 900)
    monkeypatch.setattr(export_service, "ZIP_THRESHOLD_BYTES", 1000)
    result = await _export(
        session_factory, chat_id, tmp_path, "json", include_service=True, include_bots=True
    )
    assert result.parts > 1
    total = 0
    for path in result.files:
        assert ".part" in path.name
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                (name,) = zf.namelist()
                data = json.loads(zf.read(name))
        else:
            data = json.loads(path.read_text())
        total += data["summary"]["row_count"]
    assert total == 8


async def test_cancelled_export_cleans_up(
    session_factory: SF, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_id = await fx.seed_chat(session_factory)
    monkeypatch.setattr(export_service, "BATCH", 2)
    jobs = JobService(session_factory)

    async def cancel_now(done: int, total: int) -> None:
        async with session_factory() as s:
            job = (await s.execute(select(Job).where(Job.kind == JobKind.EXPORT))).scalar_one()
            job.status = JobStatus.CANCELLED
            await s.commit()

    with pytest.raises(export_service.ExportCancelled):
        await export_chat(
            session_factory,
            scope(chat_id),
            ExportRequest(WINDOW, "json", include_service=True),
            data_dir=tmp_path,
            max_per_hour=50,
            jobs=jobs,
            progress=cancel_now,
        )
    assert list((tmp_path / "tmp" / "exports").iterdir()) == []


def test_sweep_tmp(tmp_path: Path) -> None:
    d = export_service.tmp_dir(tmp_path)
    (d / "left.json").write_text("x")
    assert sweep_tmp(tmp_path) == 1 and list(d.iterdir()) == []


def test_no_ai_sdk_loaded_by_zero_ai_paths() -> None:
    import elysium_bot.exports.service
    import elysium_bot.scan.service
    import elysium_bot.search.index  # noqa: F401

    assert "anthropic" not in sys.modules


# --- scheduler --------------------------------------------------------------------------------


class FakeJobQueue:
    def __init__(self) -> None:
        self.jobs: list[SimpleNamespace] = []

    def get_jobs_by_name(self, name: str) -> list[SimpleNamespace]:
        return [j for j in self.jobs if j.name == name and not j.removed]

    def run_repeating(self, callback, *, interval, first, name, data, job_kwargs):  # type: ignore[no-untyped-def]
        job = SimpleNamespace(
            callback=callback,
            interval=interval,
            first=first,
            name=name,
            data=data,
            job_kwargs=job_kwargs,
            removed=False,
        )
        job.schedule_removal = lambda: setattr(job, "removed", True)
        self.jobs.append(job)
        return job


async def _schedule(sf: SF, chat_id: int, **kw: object) -> int:
    async with sf() as s:
        row = Schedule(
            chat_id=chat_id,
            kind=ScheduleKind.COLLECTION,
            interval_min=60,
            enabled=True,
            params={},
            **kw,
        )  # type: ignore[arg-type]
        s.add(row)
        s.add(
            Schedule(
                chat_id=chat_id,
                kind=ScheduleKind.AI_SUMMARY,
                interval_min=120,
                enabled=True,
                params={},
            )
        )
        await s.commit()
        return row.id


async def test_registry_rehydrates_and_coalesces_missed_runs(session_factory: SF) -> None:
    chat_id = await fx.seed_chat(session_factory)
    with time_machine.travel(fx.BASE, tick=False):
        sid = await _schedule(session_factory, chat_id, next_run_at=fx.BASE - timedelta(hours=5))
        jq = FakeJobQueue()
        reg = ScheduleRegistry(session_factory, JobService(session_factory), jq)
        assert await reg.load_all() == 1  # AI summaries are not executed until Phase 5
        (job,) = jq.jobs
        assert job.first == timedelta(0) and job.interval == timedelta(hours=1)
        assert job.job_kwargs["coalesce"] is True and job.job_kwargs["max_instances"] == 1
        # After a "restart" the registry re-registers from the DB, replacing the old timer.
        reg2 = ScheduleRegistry(session_factory, JobService(session_factory), jq)
        await reg2.load_all()
        assert [j.removed for j in jq.jobs] == [True, False]
        result = await reg2.fire(sid)
    assert result is not None and result["indexed"] == 0
    async with session_factory() as s:
        row = await s.get(Schedule, sid)
        assert (
            row is not None
            and row.next_run_at == fx.BASE + timedelta(hours=1)
            and row.last_job_id is not None
        )


async def test_registry_future_next_run_and_disable(session_factory: SF) -> None:
    chat_id = await fx.seed_chat(session_factory)
    with time_machine.travel(fx.BASE, tick=False):
        sid = await _schedule(session_factory, chat_id, next_run_at=fx.BASE + timedelta(minutes=20))
        jq = FakeJobQueue()
        reg = ScheduleRegistry(session_factory, JobService(session_factory), jq)
        await reg.load_all()
        assert jq.jobs[0].first == timedelta(minutes=20)
        async with session_factory() as s:
            row = await s.get(Schedule, sid)
            assert row is not None
            row.enabled = False
            await s.commit()
            assert reg.register(row) is False
    assert jq.jobs[0].removed


async def test_overlapping_firing_is_skipped_and_recorded(session_factory: SF) -> None:
    chat_id = await fx.seed_chat(session_factory)
    sid = await _schedule(session_factory, chat_id)
    jobs = JobService(session_factory)
    blocker = await jobs.acquire_lease(chat_id, JobFamily.INGEST, JobKind.IMPORT)
    assert blocker is not None
    reg = ScheduleRegistry(session_factory, jobs, MagicMock())
    assert await reg.fire(sid) == {"skipped": True}
    async with session_factory() as s:
        skipped = (await s.execute(select(Job).where(Job.status == JobStatus.SKIPPED))).scalar_one()
        row = await s.get(Schedule, sid)
    assert (
        skipped.error == "lease held by another job"
        and row is not None
        and row.last_job_id == skipped.id
    )
