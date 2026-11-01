from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.enums import (
    ChatStatus,
    ChatType,
    CoverageKind,
    ImportStatus,
    JobFamily,
    JobKind,
    Source,
)
from elysium_bot.db.models import (
    CoverageSegment,
    ImportErrorRecord,
    ImportRun,
    Message,
    MessageSource,
    TelegramChat,
)
from elysium_bot.ingestion import desktop_import
from elysium_bot.ingestion.desktop_import import (
    ImportAbort,
    format_import_report,
    import_export,
    read_headers,
)
from elysium_bot.ingestion.normalizer import NormalizedMessage, SenderRef
from elysium_bot.ingestion.store import MessageStore
from elysium_bot.scheduler.leases import JobService
from tests.fixtures import exports as fx

SF = async_sessionmaker[AsyncSession]


async def register(
    sf: SF, tg_chat_id: int = fx.SUPERGROUP_TG_ID, chat_type: ChatType = ChatType.SUPERGROUP
) -> int:
    async with sf() as s:
        chat = TelegramChat(
            tg_chat_id=tg_chat_id, type=chat_type, title="Synthetic Plays", status=ChatStatus.ACTIVE
        )
        s.add(chat)
        await s.commit()
        return chat.id


async def count(sf: SF, model: type, *where: object) -> int:
    async with sf() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()  # type: ignore[arg-type]


@pytest.fixture
def supergroup_file(tmp_path: Path) -> Path:
    return fx.write_json(tmp_path / "result.json", fx.chat_export(fx.supergroup_messages()))


async def test_import_and_reimport_is_idempotent(
    session_factory: SF, supergroup_file: Path
) -> None:
    await register(session_factory)
    first = await import_export(session_factory, supergroup_file, batch_size=10)
    assert first.run.status == ImportStatus.SUCCEEDED
    assert (first.run.seen, first.run.inserted, first.run.failed) == (27, 27, 0)

    second = await import_export(session_factory, supergroup_file, batch_size=10)
    assert (second.run.inserted, second.run.updated, second.run.unchanged) == (0, 0, 27)
    assert second.duplicate_of == first.run.id
    assert await count(session_factory, Message) == 27
    assert "already imported" in format_import_report(second, "ok", "Asia/Ho_Chi_Minh")


async def test_import_maps_fields_and_uses_unixtime(
    session_factory: SF, supergroup_file: Path
) -> None:
    await register(session_factory)
    await import_export(session_factory, supergroup_file)
    async with session_factory() as s:
        m5 = (await s.execute(select(Message).where(Message.tg_message_id == 5))).scalar_one()
        assert m5.text == f"CA {fx.CA} docs here"
        assert m5.sent_at == fx.BASE_TIME + timedelta(
            minutes=5
        )  # UTC, not the exporter's +7 local time
        assert any(e["type"] == "text_link" for e in m5.entities or [])
        m7 = (await s.execute(select(Message).where(Message.tg_message_id == 7))).scalar_one()
        assert m7.reply_to_tg_message_id == 5
        pin = (await s.execute(select(Message).where(Message.tg_message_id == 26))).scalar_one()
        assert pin.is_service and pin.service_action == "pin_message"
        photo = (await s.execute(select(Message).where(Message.tg_message_id == 27))).scalar_one()
        assert photo.media_type == "photo"
        seg = (await s.execute(select(CoverageSegment))).scalar_one()
        assert (seg.kind, seg.source, seg.start_tg_message_id, seg.end_tg_message_id) == (
            CoverageKind.OBSERVED,
            Source.DESKTOP_EXPORT,
            1,
            27,
        )


async def test_export_merges_with_live_rows(session_factory: SF, supergroup_file: Path) -> None:
    chat_id = await register(session_factory)
    async with session_factory() as s:
        chat = await s.get(TelegramChat, chat_id)
        assert chat is not None
        live = NormalizedMessage(
            source=Source.BOT_API,
            tg_chat_id=fx.SUPERGROUP_TG_ID,
            tg_message_id=3,
            sender=SenderRef(kind="user", tg_id=104),
            sent_at=fx.BASE_TIME + timedelta(minutes=3),
            text="msg 3 about $NEST",
        )
        await MessageStore(s, chat).upsert([live])
        await s.commit()
    result = await import_export(session_factory, supergroup_file)
    assert result.run.inserted == 26
    assert await count(session_factory, Message) == 27
    async with session_factory() as s:
        srcs = (
            (
                await s.execute(
                    select(MessageSource.source)
                    .join(Message, Message.id == MessageSource.message_id)
                    .where(Message.tg_message_id == 3)
                )
            )
            .scalars()
            .all()
        )
        assert sorted(srcs) == [Source.BOT_API, Source.DESKTOP_EXPORT]


async def test_crash_and_resume(
    session_factory: SF, supergroup_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register(session_factory)
    original = MessageStore.upsert
    calls = {"n": 0}

    async def flaky(self: MessageStore, batch, **kw):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("simulated crash")
        return await original(self, batch, **kw)

    monkeypatch.setattr(MessageStore, "upsert", flaky)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await import_export(session_factory, supergroup_file, batch_size=5)
    async with session_factory() as s:
        run = (await s.execute(select(ImportRun))).scalar_one()
        assert run.status == ImportStatus.FAILED
        assert run.checkpoint == {"records_done": 15, "last_tg_message_id": 15}
    assert await count(session_factory, Message) == 15

    monkeypatch.setattr(MessageStore, "upsert", original)
    resumed = await import_export(
        session_factory, supergroup_file, batch_size=5, resume_run_id=run.id
    )
    assert resumed.run.status == ImportStatus.SUCCEEDED
    assert (resumed.run.seen, resumed.run.inserted, resumed.run.unchanged) == (27, 27, 0)
    assert await count(session_factory, Message) == 27
    assert await count(session_factory, ImportRun) == 1


async def test_full_account_export_selects_registered_chat(
    session_factory: SF, tmp_path: Path
) -> None:
    await register(session_factory)
    data = fx.full_account_export(
        [
            fx.chat_export(
                [fx.message(1, "other chat")], export_id=999, export_type="personal_chat", name="DM"
            ),
            fx.chat_export(fx.supergroup_messages(10)),
        ]
    )
    path = fx.write_json(tmp_path / "result.json", data)
    assert [h.tg_chat_id for h in read_headers(path)] == [999, fx.SUPERGROUP_TG_ID]
    result = await import_export(session_factory, path)
    assert result.run.inserted == 12
    async with session_factory() as s:
        texts = (await s.execute(select(Message.text))).scalars().all()
    assert "other chat" not in texts


async def test_zip_export(session_factory: SF, tmp_path: Path) -> None:
    await register(session_factory)
    path = fx.write_zip(tmp_path / "export.zip", fx.chat_export(fx.supergroup_messages(10)))
    assert (await import_export(session_factory, path)).run.inserted == 12


async def test_forum_topics_resolved(session_factory: SF, tmp_path: Path) -> None:
    await register(session_factory)
    path = fx.write_json(tmp_path / "result.json", fx.chat_export(fx.forum_messages()))
    result = await import_export(session_factory, path)
    assert result.run.summary and result.run.summary["topics_resolved"] == 3
    async with session_factory() as s:
        threads = dict(
            (await s.execute(select(Message.tg_message_id, Message.message_thread_id))).all()
        )
    assert threads == {1: 1, 2: 1, 3: 1, 4: None}


async def test_legacy_export_without_unixtime(session_factory: SF, tmp_path: Path) -> None:
    await register(session_factory)
    rec = fx.message(1, "old")
    del rec["date_unixtime"]
    path = fx.write_json(tmp_path / "result.json", fx.chat_export([rec]))
    result = await import_export(session_factory, path, fallback_tz="Asia/Ho_Chi_Minh")
    assert result.run.summary and result.run.summary["used_local_dates"] == 1
    async with session_factory() as s:
        sent = (await s.execute(select(Message.sent_at))).scalar_one()
    assert sent == fx.BASE_TIME + timedelta(minutes=1)
    assert "no unix timestamps" in format_import_report(result, "x", "Asia/Ho_Chi_Minh")


async def test_malformed_records_are_reported_and_mark_partial(
    session_factory: SF, tmp_path: Path
) -> None:
    await register(session_factory)
    msgs = fx.supergroup_messages(10)
    msgs.insert(3, {"type": "message", "text": "no id"})
    msgs.insert(5, {"id": 500, "type": "message", "text": "no date"})
    msgs.append({"id": 600, "type": "mystery"})
    path = fx.write_json(tmp_path / "result.json", fx.chat_export(msgs))
    result = await import_export(session_factory, path)
    assert (result.run.failed, result.run.skipped, result.run.inserted) == (2, 1, 12)
    assert result.run.status == ImportStatus.PARTIAL
    async with session_factory() as s:
        codes = sorted((await s.execute(select(ImportErrorRecord.error_code))).scalars().all())
    assert codes == ["missing_date", "missing_id"]


async def test_aborts_for_wrong_or_unregistered_chat(
    session_factory: SF, supergroup_file: Path
) -> None:
    with pytest.raises(ImportAbort, match="activate"):
        await import_export(session_factory, supergroup_file)
    await register(session_factory, tg_chat_id=-100555)
    with pytest.raises(ImportAbort, match="force-chat"):
        await import_export(session_factory, supergroup_file, tg_chat_id=-100555)
    forced = await import_export(
        session_factory, supergroup_file, tg_chat_id=-100555, force_chat=True
    )
    assert forced.run.inserted == 27


async def test_import_takes_ingest_lease(session_factory: SF, supergroup_file: Path) -> None:
    chat_id = await register(session_factory)
    jobs = JobService(session_factory)
    blocker = await jobs.acquire_lease(chat_id, JobFamily.INGEST, JobKind.MTPROTO_BACKFILL)
    assert blocker is not None
    with pytest.raises(ImportAbort, match="another ingestion job"):
        await import_export(session_factory, supergroup_file, jobs=jobs)
    await jobs.complete(blocker.id)
    assert (await import_export(session_factory, supergroup_file, jobs=jobs)).run.inserted == 27


async def test_not_an_export(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text('{"hello": [1, 2]}')
    with pytest.raises(ImportAbort, match="not a Telegram Desktop"):
        read_headers(path)


def test_report_format() -> None:
    run = ImportRun(
        id=12,
        status=ImportStatus.SUCCEEDED,
        file_name="result.json",
        file_bytes=184_000_000,
        file_sha256="3f2a" * 16,
        started_at=datetime(2026, 11, 1, 3, 0, tzinfo=UTC),
        finished_at=datetime(2026, 11, 1, 3, 2, 14, tzinfo=UTC),
        seen=412_880,
        inserted=401_233,
        updated=11_402,
        unchanged=0,
        skipped=231,
        failed=14,
        summary={
            "min_sent_at": "2025-03-02T02:11:00+00:00",
            "max_sent_at": "2026-11-01T06:58:00+00:00",
        },
    )
    chat = TelegramChat(title="Elysium Early Plays", tg_chat_id=-1001)
    text = format_import_report(
        desktop_import.ImportResult(run, chat), "Stored history covers …", "Asia/Ho_Chi_Minh"
    )
    assert "Import #12 — Elysium Early Plays — SUCCEEDED" in text
    assert "184.0 MB, sha256 3f2a3f2a…) · 0:02:14 elapsed" in text
    assert "Records seen 412,880 · new 401,233 · merged 11,402" in text
    assert "Range 2025-03-02 09:11 → 2026-11-01 13:58 (Asia/Ho_Chi_Minh)" in text
