"""Path B — Telegram Desktop JSON export import (HISTORY_INGESTION §4).

Streams ``result.json`` (or a ``.zip`` containing it) with ijson, so memory stays flat for
multi-GB exports. Batches commit with a checkpoint; ``resume_run_id`` continues a crashed run.
"""

from __future__ import annotations

import asyncio
import hashlib
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import ijson
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.access import get_chat_by_tg_id
from elysium_bot.db.enums import ImportStatus, JobFamily, JobKind, Source
from elysium_bot.db.models import ImportErrorRecord, ImportRun, Message, TelegramChat
from elysium_bot.ingestion import coverage
from elysium_bot.ingestion.normalizer import (
    ExportChatContext,
    ExportRecordError,
    NormalizedMessage,
    export_chat_tg_id,
    from_export,
)
from elysium_bot.ingestion.store import MessageStore, UpsertStats
from elysium_bot.logging import get_logger, redact
from elysium_bot.scheduler.leases import JobService
from elysium_bot.timeutil import format_local, utcnow

log = get_logger(__name__)

BATCH_SIZE = 1000
PARTIAL_FAILURE_RATIO = 0.01
FULL_EXPORT_BASE = "chats.list.item"


class ImportAbort(RuntimeError):
    """The import cannot start (wrong chat, unregistered chat, lease busy, bad file)."""


@dataclass(frozen=True)
class ExportHeader:
    export_id: int
    export_type: str
    name: str | None
    base: str  # ijson prefix of the chat object ("" for single-chat exports)

    @property
    def tg_chat_id(self) -> int:
        return export_chat_tg_id(self.export_type, self.export_id)


@dataclass
class ImportResult:
    run: ImportRun
    chat: TelegramChat
    duplicate_of: int | None = None


# --- file access ----------------------------------------------------------------------------


@contextmanager
def open_export(path: Path) -> Iterator[IO[bytes]]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.endswith("result.json")] or [
                n for n in zf.namelist() if n.endswith(".json")
            ]
            if not names:
                raise ImportAbort("zip archive contains no result.json")
            with zf.open(sorted(names, key=len)[0]) as fh:
                yield fh
    else:
        with path.open("rb") as fh:
            yield fh


def file_digest(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def read_headers(path: Path) -> list[ExportHeader]:
    """Chats contained in the export (one for a chat export, many for a full-account export)."""
    headers: list[ExportHeader] = []
    current: dict[str, Any] = {}
    with open_export(path) as fh:
        for prefix, event, value in ijson.parse(fh):
            if prefix in ("name", "type", "id"):
                current[prefix] = value
            elif prefix == "messages" and event == "start_array":
                if "id" not in current or "type" not in current:
                    raise ImportAbort("export has no chat id/type before messages")
                return [
                    ExportHeader(int(current["id"]), str(current["type"]), current.get("name"), "")
                ]
            elif prefix in (
                f"{FULL_EXPORT_BASE}.name",
                f"{FULL_EXPORT_BASE}.type",
                f"{FULL_EXPORT_BASE}.id",
            ):
                current[prefix.rsplit(".", 1)[1]] = value
            elif prefix == FULL_EXPORT_BASE and event == "end_map":
                if "id" in current and "type" in current:
                    headers.append(
                        ExportHeader(
                            int(current["id"]),
                            str(current["type"]),
                            current.get("name"),
                            FULL_EXPORT_BASE,
                        )
                    )
                current = {}
    if not headers:
        raise ImportAbort("file is not a Telegram Desktop JSON export")
    return headers


def iter_records(fh: IO[bytes], header: ExportHeader) -> Iterator[dict[str, Any]]:
    if not header.base:
        yield from ijson.items(fh, "messages.item", use_float=True)
        return
    # Full-account export: build message objects only for the selected chat.
    msg_prefix = f"{header.base}.messages.item"
    chat_id: int | None = None
    builder: ijson.ObjectBuilder | None = None
    for prefix, event, value in ijson.parse(fh, use_float=True):
        if builder is not None:
            builder.event(event, value)
            if prefix == msg_prefix and event == "end_map":
                yield builder.value
                builder = None
            continue
        if prefix == f"{header.base}.id":
            chat_id = int(value)
        elif prefix == msg_prefix and event == "start_map" and chat_id == header.export_id:
            builder = ijson.ObjectBuilder()
            builder.event(event, value)


def _skip(it: Iterator[dict[str, Any]], n: int) -> None:
    for _ in zip(range(n), it, strict=False):
        pass


def _take(it: Iterator[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    out = []
    for record in it:
        out.append(record)
        if len(out) >= n:
            break
    return out


# --- import ---------------------------------------------------------------------------------


async def _select_target(
    session: AsyncSession, headers: list[ExportHeader], tg_chat_id: int | None, force_chat: bool
) -> tuple[ExportHeader, TelegramChat]:
    if tg_chat_id is not None:
        matching = [h for h in headers if h.tg_chat_id == tg_chat_id]
        if not matching:
            if force_chat and len(headers) == 1:
                matching = headers
            else:
                found = ", ".join(str(h.tg_chat_id) for h in headers[:5])
                raise ImportAbort(
                    f"export is for chat {found}, not {tg_chat_id} (use --force-chat to override)"
                )
        chat = await get_chat_by_tg_id(session, tg_chat_id)
        if chat is None:
            raise ImportAbort(
                f"chat {tg_chat_id} is not registered; run /activate in the group first"
            )
        return matching[0], chat
    for header in headers:
        chat = await get_chat_by_tg_id(session, header.tg_chat_id)
        if chat is not None:
            return header, chat
    raise ImportAbort(
        "none of the chats in this export are activated; run /activate in the group first"
    )


async def resolve_topics(session: AsyncSession, chat_id: int) -> int:
    """Derive forum topic ids for imported messages (exports only record reply targets)."""
    roots = set(
        (
            await session.execute(
                select(Message.tg_message_id).where(
                    Message.chat_id == chat_id, Message.service_action == "topic_created"
                )
            )
        ).scalars()
    )
    if not roots:
        return 0
    rows = (
        await session.execute(
            select(
                Message.id,
                Message.tg_message_id,
                Message.reply_to_tg_message_id,
                Message.message_thread_id,
            )
            .where(Message.chat_id == chat_id)
            .order_by(Message.tg_message_id)
        )
    ).all()
    thread_of: dict[int, int] = {
        r.tg_message_id: r.message_thread_id for r in rows if r.message_thread_id
    }
    thread_of.update({root: root for root in roots})
    updates = []
    for r in rows:
        if r.message_thread_id is not None:
            continue
        thread = thread_of.get(r.tg_message_id)
        if thread is None and r.reply_to_tg_message_id is not None:
            thread = thread_of.get(r.reply_to_tg_message_id)
        if thread is not None:
            thread_of[r.tg_message_id] = thread
            updates.append({"id": r.id, "message_thread_id": thread})
    if updates:
        await session.execute(update(Message), updates)
    return len(updates)


async def import_export(
    sessions: async_sessionmaker[AsyncSession],
    path: Path,
    *,
    tg_chat_id: int | None = None,
    force_chat: bool = False,
    resume_run_id: int | None = None,
    triggered_by: int | None = None,
    fallback_tz: str = "Asia/Ho_Chi_Minh",
    batch_size: int = BATCH_SIZE,
    jobs: JobService | None = None,
) -> ImportResult:
    path = Path(path)
    if not path.exists():
        raise ImportAbort(f"file not found: {path}")
    sha, size = await asyncio.to_thread(file_digest, path)
    headers = await asyncio.to_thread(read_headers, path)

    async with sessions() as session:
        header, chat = await _select_target(session, headers, tg_chat_id, force_chat)
        duplicate_of = (
            (
                await session.execute(
                    select(ImportRun.id)
                    .where(
                        ImportRun.chat_id == chat.id,
                        ImportRun.file_sha256 == sha,
                        ImportRun.id != (resume_run_id or 0),
                    )
                    .order_by(ImportRun.id)
                )
            )
            .scalars()
            .first()
        )
        if resume_run_id is not None:
            run = await session.get(ImportRun, resume_run_id)
            if run is None or run.chat_id != chat.id:
                raise ImportAbort(f"import run {resume_run_id} not found for this chat")
            if run.file_sha256 != sha:
                raise ImportAbort("resume file differs from the original import (sha256 mismatch)")
            if run.status == ImportStatus.SUCCEEDED:
                raise ImportAbort(f"import run {resume_run_id} already succeeded")
            run.status = ImportStatus.RUNNING
        else:
            run = ImportRun(
                chat_id=chat.id,
                source=Source.DESKTOP_EXPORT,
                status=ImportStatus.RUNNING,
                file_name=path.name,
                file_sha256=sha,
                file_bytes=size,
                triggered_by_tg_user_id=triggered_by,
                checkpoint={"records_done": 0},
                summary={"export_name": header.name, "export_type": header.export_type},
            )
            session.add(run)
        await session.commit()
        run_id, chat_id = run.id, chat.id

    job = None
    if jobs is not None:
        job = await jobs.acquire_lease(
            chat_id,
            JobFamily.INGEST,
            JobKind.IMPORT,
            params={"import_run_id": run_id, "file": path.name},
            requested_by=triggered_by,
        )
        if job is None:
            raise ImportAbort("another ingestion job is running for this chat; retry later")

    try:
        await _run_batches(
            sessions, path, header, run_id, chat_id, fallback_tz, batch_size, jobs, job
        )
    except BaseException as exc:
        async with sessions() as session:
            run = await session.get(ImportRun, run_id)
            if run is not None:
                run.status = ImportStatus.FAILED
                run.finished_at = utcnow()
                run.summary = {
                    **(run.summary or {}),
                    "error": redact(f"{type(exc).__name__}: {exc}"),
                }
                await session.commit()
        if jobs is not None and job is not None:
            await jobs.fail(job.id, f"{type(exc).__name__}: {exc}")
        raise

    async with sessions() as session:
        final_run = await session.get(ImportRun, run_id)
        final_chat = await session.get(TelegramChat, chat_id)
        assert final_run is not None and final_chat is not None
        if jobs is not None and job is not None:
            await jobs.complete(job.id, {"import_run_id": run_id, "inserted": final_run.inserted})
        return ImportResult(run=final_run, chat=final_chat, duplicate_of=duplicate_of)


async def _run_batches(
    sessions: async_sessionmaker[AsyncSession],
    path: Path,
    header: ExportHeader,
    run_id: int,
    chat_id: int,
    fallback_tz: str,
    batch_size: int,
    jobs: JobService | None,
    job: Any,
) -> None:
    async with sessions() as session:
        run = await session.get(ImportRun, run_id)
        assert run is not None
        done = int((run.checkpoint or {}).get("records_done", 0))
        summary = dict(run.summary or {})
    ctx = ExportChatContext(
        tg_chat_id=header.tg_chat_id,
        fallback_tz=fallback_tz,
        unknown_fields=dict(summary.get("unknown_fields", {})),
        used_local_dates=int(summary.get("used_local_dates", 0)),
    )
    totals = UpsertStats()
    if summary.get("min_id") is not None:
        totals.min_id, totals.max_id = summary["min_id"], summary["max_id"]
        totals.min_sent_at = _parse_dt(summary["min_sent_at"])
        totals.max_sent_at = _parse_dt(summary["max_sent_at"])
    saw_topics = bool(summary.get("saw_topics"))

    with open_export(path) as fh:
        records = iter_records(fh, header)
        if done:  # resume: skip records already committed
            await asyncio.to_thread(_skip, records, done)
        index = done
        while True:
            raw = await asyncio.to_thread(_take, records, batch_size)
            if not raw:
                break
            normalized: list[NormalizedMessage] = []
            errors: list[ImportErrorRecord] = []
            skipped = 0
            for record in raw:
                try:
                    normalized.append(from_export(record, ctx, index))
                    saw_topics = saw_topics or record.get("action") == "topic_created"
                except ExportRecordError as exc:
                    if exc.code == "unsupported_type":
                        skipped += 1
                    else:
                        errors.append(_error(run_id, record, exc.code, exc.detail))
                except Exception as exc:  # malformed record: keep going
                    errors.append(
                        _error(run_id, record, "normalize_failed", f"{type(exc).__name__}: {exc}")
                    )
                index += 1

            async with sessions() as session:
                chat = await session.get(TelegramChat, chat_id)
                assert chat is not None
                stats = await MessageStore(session, chat).upsert(normalized, import_run_id=run_id)
                for ref, code in stats.skipped_refs:
                    errors.append(
                        ImportErrorRecord(import_run_id=run_id, record_ref=ref, error_code=code)
                    )
                session.add_all(errors)
                totals.add(stats)
                run = await session.get(ImportRun, run_id)
                assert run is not None
                run.seen += len(raw)
                run.inserted += stats.inserted
                run.updated += stats.updated
                run.unchanged += stats.unchanged
                run.skipped += skipped + stats.skipped
                run.failed += len(errors) - len(stats.skipped_refs)
                run.checkpoint = {"records_done": index, "last_tg_message_id": stats.max_id}
                run.summary = {
                    **summary,
                    **_range_summary(totals),
                    "saw_topics": saw_topics,
                    "unknown_fields": ctx.unknown_fields,
                    "used_local_dates": ctx.used_local_dates,
                }
                await session.commit()
            if jobs is not None and job is not None:
                await jobs.heartbeat(job.id, {"records_done": index})

    async with sessions() as session:
        run = await session.get(ImportRun, run_id)
        assert run is not None
        if saw_topics:
            run.summary = {
                **(run.summary or {}),
                "topics_resolved": await resolve_topics(session, chat_id),
            }
        if totals.min_sent_at is not None and totals.max_sent_at is not None:
            await coverage.add_observed(
                session,
                chat_id,
                Source.DESKTOP_EXPORT,
                start_at=totals.min_sent_at,
                end_at=totals.max_sent_at,
                start_id=totals.min_id,
                end_id=totals.max_id,
                count=run.inserted + run.updated + run.unchanged,
                import_run_id=run_id,
            )
        ratio = run.failed / run.seen if run.seen else 0.0
        run.status = (
            ImportStatus.PARTIAL if ratio > PARTIAL_FAILURE_RATIO else ImportStatus.SUCCEEDED
        )
        run.finished_at = utcnow()
        await session.commit()
    log.info("import.finished", import_run_id=run_id, chat_id=chat_id)


def _error(run_id: int, record: dict[str, Any], code: str, detail: str) -> ImportErrorRecord:
    excerpt = redact(str(record))[:2048]
    return ImportErrorRecord(
        import_run_id=run_id,
        record_ref=f"message id {record.get('id')}" if record.get("id") is not None else None,
        error_code=code,
        detail=redact(detail)[:2000],
        raw_excerpt=excerpt,
    )


def _range_summary(t: UpsertStats) -> dict[str, Any]:
    return {
        "min_id": t.min_id,
        "max_id": t.max_id,
        "min_sent_at": t.min_sent_at.isoformat() if t.min_sent_at else None,
        "max_sent_at": t.max_sent_at.isoformat() if t.max_sent_at else None,
    }


def _parse_dt(value: str | None) -> Any:
    return datetime.fromisoformat(value) if value else None


def format_import_report(result: ImportResult, coverage_text: str, tz: str) -> str:
    """Plain-text import report (HISTORY_INGESTION §4.5)."""
    run, chat = result.run, result.chat
    summary = run.summary or {}
    elapsed = (run.finished_at - run.started_at) if run.finished_at else None
    mb = (run.file_bytes or 0) / 1_000_000
    lines = [
        f"Import #{run.id} — {chat.title or chat.tg_chat_id} — {run.status.value.upper()}",
        f"File {run.file_name} ({mb:,.1f} MB, sha256 {(run.file_sha256 or '')[:8]}…)"
        + (f" · {str(elapsed).split('.')[0]} elapsed" if elapsed else ""),
        f"Records seen {run.seen:,} · new {run.inserted:,} · merged {run.updated:,}"
        f" · unchanged {run.unchanged:,}",
        f"Skipped {run.skipped:,} · Failed {run.failed:,}"
        + (" (see import errors)" if run.failed else ""),
    ]
    if summary.get("min_sent_at"):
        lines.append(
            f"Range {format_local(_parse_dt(summary['min_sent_at']), tz, '%Y-%m-%d %H:%M')} → "
            f"{format_local(_parse_dt(summary['max_sent_at']), tz, '%Y-%m-%d %H:%M')} ({tz})"
        )
    if result.duplicate_of:
        lines.append(
            f"Note: this file was already imported (run #{result.duplicate_of}); "
            "nothing was duplicated."
        )
    if summary.get("used_local_dates"):
        lines.append(
            f"⚠️ {summary['used_local_dates']} records had no unix timestamps; interpreted in {tz}."
        )
    if summary.get("unknown_fields"):
        lines.append("Unrecognized export fields: " + ", ".join(sorted(summary["unknown_fields"])))
    lines.append(f"Coverage: {coverage_text}")
    return "\n".join(lines)
