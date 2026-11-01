"""Raw chat exports — no AI (TELEGRAM_UX §5.4, SECURITY §6).

Rows are streamed with keyset pagination (``sent_at``, ``id``) so memory stays flat. Output is
rotated into parts of at most ``PART_RAW_BYTES``; parts above ``ZIP_THRESHOLD_BYTES`` are
deflated, so every delivered file stays under Telegram's 50 MB upload limit.
"""

from __future__ import annotations

import os
import zipfile
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import IO, Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot import __version__
from elysium_bot.db.enums import AuditAction, JobFamily, JobKind, JobStatus
from elysium_bot.db.models import (
    AuditLog,
    ChatMember,
    ChatSettings,
    ContractMention,
    Job,
    Message,
    MessageLink,
    MessageSource,
    MessageVersion,
    TelegramChat,
    TelegramUser,
)
from elysium_bot.db.repositories import AuditLogRepository
from elysium_bot.db.scope import ChatScope
from elysium_bot.exports.writers import CONTRACT_NOTE, WRITERS
from elysium_bot.ingestion import coverage
from elysium_bot.logging import get_logger
from elysium_bot.scheduler.leases import JobService
from elysium_bot.telegram_ui.components import Window
from elysium_bot.telegram_ui.render import message_url
from elysium_bot.timeutil import format_local, utcnow

log = get_logger(__name__)

BATCH = 1000
PART_RAW_BYTES = 40 * 1024 * 1024
ZIP_THRESHOLD_BYTES = 5 * 1024 * 1024
FORMATS = ("json", "csv", "txt", "md")


class ExportRefused(RuntimeError):
    pass


class ExportCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ExportRequest:
    window: Window
    fmt: str
    pseudonymize: bool = False
    include_service: bool = False
    include_bots: bool = False


@dataclass
class ExportResult:
    files: list[Path]
    rows: int
    raw_bytes: int
    coverage_text: str
    job_id: int | None = None
    parts: int = 1
    meta: dict[str, Any] = field(default_factory=dict)


def tmp_dir(data_dir: Path) -> Path:
    path = data_dir / "tmp" / "exports"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def sweep_tmp(data_dir: Path) -> int:
    """Delete leftover export files (e.g. after a crash). Run at startup."""
    path = data_dir / "tmp" / "exports"
    if not path.exists():
        return 0
    removed = 0
    for item in path.iterdir():
        if item.is_file():
            item.unlink(missing_ok=True)
            removed += 1
    return removed


def _filters(scope: ChatScope, req: ExportRequest) -> list[Any]:
    clauses: list[Any] = [
        Message.chat_id == scope.chat_id,
        Message.sent_at >= req.window.start,
        Message.sent_at <= req.window.end,
    ]
    if not req.include_service:
        clauses.append(Message.is_service.is_(False))
    if not req.include_bots:
        clauses.append(
            Message.sender_user_id.is_(None)
            | Message.sender_user_id.not_in(
                select(TelegramUser.id).where(TelegramUser.is_bot.is_(True))
            )
        )
    return clauses


async def count_rows(session: AsyncSession, scope: ChatScope, req: ExportRequest) -> int:
    return int(
        (await session.execute(select(func.count()).where(*_filters(scope, req)))).scalar_one()
    )


async def recent_exports(
    session: AsyncSession, scope: ChatScope, within: timedelta = timedelta(hours=1)
) -> int:
    since = utcnow() - within
    return int(
        (
            await session.execute(
                select(func.count()).where(
                    AuditLog.actor_tg_user_id == scope.actor_tg_user_id,
                    AuditLog.action == AuditAction.EXPORT,
                    AuditLog.created_at >= since,
                )
            )
        ).scalar_one()
    )


async def iter_rows(
    session: AsyncSession, scope: ChatScope, req: ExportRequest, chat: TelegramChat, tz: str
) -> AsyncIterator[dict[str, Any]]:
    cursor: tuple[Any, int] | None = None
    while True:
        stmt = (
            select(Message, TelegramUser, ChatMember)
            .outerjoin(TelegramUser, TelegramUser.id == Message.sender_user_id)
            .outerjoin(
                ChatMember,
                (ChatMember.user_id == Message.sender_user_id)
                & (ChatMember.chat_id == scope.chat_id),
            )
            .where(*_filters(scope, req))
            .order_by(Message.sent_at, Message.id)
            .limit(BATCH)
        )
        if cursor is not None:
            stmt = stmt.where(tuple_(Message.sent_at, Message.id) > tuple_(*cursor))
        batch = (await session.execute(stmt)).all()
        if not batch:
            return
        ids = [m.id for m, _, _ in batch]
        links: dict[int, list[str]] = defaultdict(list)
        for mid, url in (
            await session.execute(
                select(MessageLink.message_id, MessageLink.url)
                .where(MessageLink.message_id.in_(ids))
                .order_by(MessageLink.position)
            )
        ).all():
            links[mid].append(url)
        contracts: dict[int, list[dict[str, str]]] = defaultdict(list)
        for mid, raw, fmt, state in (
            await session.execute(
                select(
                    ContractMention.message_id,
                    ContractMention.raw,
                    ContractMention.format,
                    ContractMention.checksum_state,
                )
                .where(ContractMention.message_id.in_(ids))
                .order_by(ContractMention.position)
            )
        ).all():
            contracts[mid].append(
                {"value": raw, "format": fmt.value, "checksum_state": state.value}
            )
        sources: dict[int, list[str]] = defaultdict(list)
        for mid, src in (
            await session.execute(
                select(MessageSource.message_id, MessageSource.source).where(
                    MessageSource.message_id.in_(ids)
                )
            )
        ).all():
            sources[mid].append(src.value)
        versions = dict(
            (
                await session.execute(
                    select(MessageVersion.message_id, func.count())
                    .where(MessageVersion.message_id.in_(ids))
                    .group_by(MessageVersion.message_id)
                )
            ).all()
        )
        for msg, user, member in batch:
            yield _row(
                msg, user, member, chat, tz, req.pseudonymize, links, contracts, sources, versions
            )
        last = batch[-1][0]
        cursor = (last.sent_at, last.id)
        session.expunge_all()  # keep the identity map from growing across batches


def _row(
    msg: Message,
    user: TelegramUser | None,
    member: ChatMember | None,
    chat: TelegramChat,
    tz: str,
    pseudonymize: bool,
    links: dict[int, list[str]],
    contracts: dict[int, list[dict[str, str]]],
    sources: dict[int, list[str]],
    versions: dict[int, int],
) -> dict[str, Any]:
    if pseudonymize:
        label = member.pseudonym if member else ("channel" if msg.sender_chat_tg_id else "unknown")
        sender_id: Any = label
        sender_name: Any = label
        sender_username = None
    else:
        sender_id = user.tg_user_id if user else msg.sender_chat_tg_id
        sender_name = msg.sender_name_snapshot
        sender_username = user.username if user else None
    forward = (msg.forward_origin or {}).get("name") if msg.forward_origin else None
    edit_count = max(0, int(versions.get(msg.id, 0)) - 1)
    return {
        "chat_id": chat.tg_chat_id,
        "tg_message_id": msg.tg_message_id,
        "message_link": message_url(
            chat.tg_chat_id,
            msg.tg_message_id,
            username=chat.username,
            thread_id=msg.message_thread_id,
        ),
        "sent_at_utc": msg.sent_at.isoformat(),
        "sent_at_local": format_local(msg.sent_at, tz, "%Y-%m-%d %H:%M:%S"),
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_username": sender_username,
        "reply_to_tg_message_id": msg.reply_to_tg_message_id,
        "message_thread_id": msg.message_thread_id,
        "text": msg.text,
        "edited_at_utc": msg.edited_at.isoformat() if msg.edited_at else None,
        "edit_count": edit_count,
        "forward_from": None if pseudonymize else forward,
        "media_type": msg.media_type,
        "is_service": msg.is_service,
        "links": links.get(msg.id, []),
        "contract_strings": contracts.get(msg.id, []),
        "sources": sorted(sources.get(msg.id, [])),
    }


class _PartWriter:
    """Rotates output into raw-size-capped part files."""

    def __init__(self, directory: Path, stem: str, fmt: str, meta: dict[str, Any]) -> None:
        self.directory, self.stem, self.fmt, self.meta = directory, stem, fmt, meta
        self.paths: list[Path] = []
        self.fh: IO[str] | None = None
        self.writer: Any = None
        self.part_rows = 0
        self.total_bytes = 0

    def _open(self) -> None:
        n = len(self.paths) + 1
        path = self.directory / f"{self.stem}.part{n}.{WRITERS[self.fmt].extension}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self.fh = os.fdopen(fd, "w", encoding="utf-8", newline="")
        self.paths.append(path)
        self.writer = WRITERS[self.fmt](self.fh)
        self.writer.begin({**self.meta, "part": n})
        self.part_rows = 0

    def _close(self) -> None:
        if self.fh is not None:
            self.writer.end({"row_count": self.part_rows, "part": len(self.paths)})
            self.total_bytes += self.fh.tell()
            self.fh.close()
            self.fh = None

    def write(self, row: dict[str, Any]) -> None:
        if self.fh is None:
            self._open()
        elif self.fh.tell() >= PART_RAW_BYTES:
            self._close()
            self._open()
        self.writer.row(row)
        self.part_rows += 1

    def finish(self) -> list[Path]:
        if self.fh is None:
            self._open()  # empty export still gets a file with header + summary
        self._close()
        return self.paths


def _finalize(paths: list[Path], stem: str) -> list[Path]:
    """Name parts (drop '.part1' when single) and deflate large parts."""
    out: list[Path] = []
    for path in paths:
        target = path
        if len(paths) == 1:
            target = path.with_name(path.name.replace(".part1", ""))
            path.rename(target)
        if target.stat().st_size > ZIP_THRESHOLD_BYTES:
            zipped = target.with_name(target.name + ".zip")
            fd = os.open(zipped, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with (
                os.fdopen(fd, "wb") as raw,
                zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf,
            ):
                zf.write(target, arcname=target.name)
            target.unlink()
            target = zipped
        out.append(target)
    return out


async def export_chat(
    sessions: async_sessionmaker[AsyncSession],
    scope: ChatScope,
    req: ExportRequest,
    *,
    data_dir: Path,
    max_per_hour: int,
    jobs: JobService | None = None,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> ExportResult:
    if req.fmt not in FORMATS:
        raise ExportRefused(f"unknown format {req.fmt}")
    async with sessions() as session:
        if await recent_exports(session, scope) >= max_per_hour:
            raise ExportRefused(f"export limit reached ({max_per_hour} per hour); try again later")
        chat = await session.get(TelegramChat, scope.chat_id)
        settings_row = await session.get(ChatSettings, scope.chat_id)
        assert chat is not None
        tz = settings_row.timezone if settings_row else "Asia/Ho_Chi_Minh"
        total = await count_rows(session, scope, req)
        cov = await coverage.statement(session, chat, req.window.start, req.window.end, tz=tz)

    job = None
    if jobs is not None:
        job = await jobs.acquire_lease(
            scope.chat_id,
            JobFamily.EXPORT,
            JobKind.EXPORT,
            params={
                "fmt": req.fmt,
                "start": req.window.start.isoformat(),
                "end": req.window.end.isoformat(),
            },
            requested_by=scope.actor_tg_user_id,
        )
        if job is None:
            raise ExportRefused("another export is running for this group; try again shortly")

    meta = {
        "generated_at_utc": utcnow().isoformat(),
        "chat_title": chat.title,
        "chat_id": chat.tg_chat_id,
        "window_local": (
            f"{format_local(req.window.start, tz)} → {format_local(req.window.end, tz)} ({tz})"
        ),
        "window_utc": f"{req.window.start.isoformat()} → {req.window.end.isoformat()}",
        "rows_expected": total,
        "filters": {
            "pseudonymized": req.pseudonymize,
            "service_messages": req.include_service,
            "bot_messages": req.include_bots,
        },
        "coverage": cov.human_text,
        "coverage_complete": cov.complete_for_window,
        "note": (
            f"Contains private group data, handle accordingly. No AI was used. {CONTRACT_NOTE}."
        ),
        "app_version": __version__,
    }
    stem = (
        f"elysium_export_{chat.tg_chat_id}_{req.window.start:%Y%m%d%H%M}"
        f"_{req.window.end:%Y%m%d%H%M}_{utcnow():%H%M%S%f}"
    )
    parts = _PartWriter(tmp_dir(data_dir), stem, req.fmt, meta)
    written = 0
    try:
        async with sessions() as session:
            async for row in iter_rows(session, scope, req, chat, tz):
                parts.write(row)
                written += 1
                if written % BATCH == 0:
                    if job is not None and await _cancelled(session, job.id):
                        raise ExportCancelled("export cancelled")
                    if jobs is not None and job is not None:
                        await jobs.heartbeat(job.id, {"rows": written, "total": total})
                    if progress is not None:
                        await progress(written, total)
        files = _finalize(parts.finish(), stem)
        async with sessions() as session:
            await AuditLogRepository(session, scope).record(
                AuditAction.EXPORT,
                {
                    "fmt": req.fmt,
                    "rows": written,
                    "start": req.window.start.isoformat(),
                    "end": req.window.end.isoformat(),
                    "pseudonymized": req.pseudonymize,
                    "parts": len(files),
                },
            )
            await session.commit()
    except BaseException as exc:
        for p in parts.paths:
            p.unlink(missing_ok=True)
            p.with_name(p.name.replace(".part1", "")).unlink(missing_ok=True)
        if jobs is not None and job is not None and not isinstance(exc, ExportCancelled):
            await jobs.fail(job.id, type(exc).__name__)
        raise
    if jobs is not None and job is not None:
        await jobs.complete(job.id, {"rows": written, "parts": len(files)})
    return ExportResult(
        files, written, parts.total_bytes, cov.human_text, job.id if job else None, len(files), meta
    )


async def _cancelled(session: AsyncSession, job_id: int) -> bool:
    status = (
        await session.execute(select(Job.status).where(Job.id == job_id))
    ).scalar_one_or_none()
    return status == JobStatus.CANCELLED
