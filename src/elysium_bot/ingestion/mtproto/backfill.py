"""Path C — optional, read-only history backfill through an authorized user account.

Compliance boundaries (HISTORY_INGESTION §5.2): the account must already be a member; we never
join chats, send, mark read, or react; flood waits are honoured exactly; one request at a time.
Telethon is imported lazily so the base install works without the ``mtproto`` extra.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.enums import ImportStatus, JobFamily, JobKind, Source
from elysium_bot.db.models import ImportRun, Message, TelegramChat
from elysium_bot.ingestion import coverage
from elysium_bot.ingestion.normalizer import Entity, NormalizedMessage, SenderRef
from elysium_bot.ingestion.store import MessageStore, UpsertStats
from elysium_bot.logging import get_logger
from elysium_bot.scheduler.leases import JobService
from elysium_bot.timeutil import ensure_utc, utcnow

log = get_logger(__name__)

BATCH_SIZE = 500
WAIT_TIME_S = 1.0  # Telethon pause between history requests (~1 request/s)
MAX_FLOOD_RETRIES = 20

_ENTITY_NAMES = {
    "TextUrl": "text_link",
    "Url": "url",
    "MentionName": "text_mention",
    "InputMessageEntityMentionName": "text_mention",
    "Strike": "strikethrough",
    "Phone": "phone_number",
}
_ACTION_NAMES = {
    "TopicCreate": "topic_created",
    "PinMessage": "pin_message",
    "ChatAddUser": "invite_members",
}


class MTProtoClient(Protocol):
    async def get_entity(self, entity: Any) -> Any: ...

    def iter_messages(self, entity: Any, **kwargs: Any) -> AsyncIterator[Any]: ...


class BackfillAbort(RuntimeError):
    pass


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def from_telethon(msg: Any, tg_chat_id: int) -> NormalizedMessage:
    entities = []
    for ent in getattr(msg, "entities", None) or ():
        raw = type(ent).__name__.removeprefix("MessageEntity")
        entities.append(
            Entity(
                type=_ENTITY_NAMES.get(raw, _snake(raw)),
                offset=ent.offset,
                length=ent.length,
                url=getattr(ent, "url", None),
                user_id=getattr(ent, "user_id", None),
            )
        )
    sender = None
    sender_id = getattr(msg, "sender_id", None)
    who = getattr(msg, "sender", None)
    if sender_id is not None:
        if sender_id > 0:
            sender = SenderRef(
                kind="user",
                tg_id=sender_id,
                username=getattr(who, "username", None),
                first_name=getattr(who, "first_name", None),
                last_name=getattr(who, "last_name", None),
                is_bot=bool(getattr(who, "bot", False)),
            )
        else:
            sender = SenderRef(kind="chat", tg_id=sender_id, title=getattr(who, "title", None))
    reply = getattr(msg, "reply_to", None)
    reply_id = getattr(reply, "reply_to_msg_id", None) if reply else None
    thread = None
    if reply is not None:
        thread = getattr(reply, "reply_to_top_id", None) or (
            reply_id if getattr(reply, "forum_topic", False) else None
        )
    action = getattr(msg, "action", None)
    service_action = None
    if action is not None:
        raw = type(action).__name__.removeprefix("MessageAction")
        service_action = _ACTION_NAMES.get(raw, _snake(raw))
    media = getattr(msg, "media", None)
    media_type = (
        _snake(type(media).__name__.removeprefix("MessageMedia")) if media is not None else None
    )
    fwd = getattr(msg, "fwd_from", None)
    edit_date = getattr(msg, "edit_date", None)
    return NormalizedMessage(
        source=Source.MTPROTO,
        tg_chat_id=tg_chat_id,
        tg_message_id=msg.id,
        message_thread_id=thread,
        sender=sender,
        sent_at=ensure_utc(msg.date),
        edited_at=ensure_utc(edit_date) if edit_date else None,
        text=getattr(msg, "message", None) or "",
        entities=entities,
        reply_to_tg_message_id=reply_id,
        forward_origin={"type": "mtproto", "date": fwd.date.isoformat()}
        if fwd is not None and getattr(fwd, "date", None)
        else None,
        media_type=media_type,
        is_service=action is not None,
        service_action=service_action,
    )


@dataclass
class BackfillResult:
    run: ImportRun
    deleted_marked: int
    flood_waits: int


def _flood_wait_seconds(exc: BaseException) -> int | None:
    if type(exc).__name__ in ("FloodWaitError", "FloodPremiumWaitError") and hasattr(
        exc, "seconds"
    ):
        return int(exc.seconds)
    return None


async def backfill(
    sessions: async_sessionmaker[AsyncSession],
    client: MTProtoClient,
    chat: TelegramChat,
    *,
    min_id: int = 0,
    max_id: int = 0,
    resume_run_id: int | None = None,
    triggered_by: int | None = None,
    batch_size: int = BATCH_SIZE,
    jobs: JobService | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> BackfillResult:
    """Fetch history oldest→newest for ``(min_id, max_id)`` (exclusive; 0 = unbounded)."""
    try:
        entity = await client.get_entity(chat.tg_chat_id)
    except Exception as exc:
        raise BackfillAbort(
            f"account cannot access chat {chat.tg_chat_id} ({type(exc).__name__}); "
            "it must already be a member"
        ) from exc

    async with sessions() as session:
        if resume_run_id is not None:
            run = await session.get(ImportRun, resume_run_id)
            if run is None or run.chat_id != chat.id or run.source != Source.MTPROTO:
                raise BackfillAbort(f"MTProto run {resume_run_id} not found for this chat")
            run.status = ImportStatus.RUNNING
        else:
            run = ImportRun(
                chat_id=chat.id,
                source=Source.MTPROTO,
                status=ImportStatus.RUNNING,
                triggered_by_tg_user_id=triggered_by,
                checkpoint={"last_tg_message_id": min_id},
                summary={"min_id_bound": min_id, "max_id_bound": max_id},
            )
            session.add(run)
        await session.commit()
        run_id = run.id
        cursor = int((run.checkpoint or {}).get("last_tg_message_id") or min_id)
        first_id = (run.summary or {}).get("first_returned_id")

    job = None
    if jobs is not None:
        job = await jobs.acquire_lease(
            chat.id,
            JobFamily.INGEST,
            JobKind.MTPROTO_BACKFILL,
            params={"run_id": run_id},
            requested_by=triggered_by,
        )
        if job is None:
            raise BackfillAbort("another ingestion job is running for this chat; retry later")

    totals = UpsertStats()
    returned: set[int] = set()
    flood_waits = 0
    try:
        while True:
            batch: list[NormalizedMessage] = []
            try:
                async for msg in client.iter_messages(
                    entity, reverse=True, min_id=cursor, max_id=max_id, wait_time=WAIT_TIME_S
                ):
                    batch.append(from_telethon(msg, chat.tg_chat_id))
                    if len(batch) >= batch_size:
                        cursor = await _commit(sessions, chat.id, run_id, batch, totals, returned)
                        first_id = first_id or min(returned)
                        batch = []
                        if jobs is not None and job is not None:
                            await jobs.heartbeat(job.id, {"last_tg_message_id": cursor})
                if batch:
                    cursor = await _commit(sessions, chat.id, run_id, batch, totals, returned)
                break
            except Exception as exc:
                seconds = _flood_wait_seconds(exc)
                if seconds is None:
                    raise
                flood_waits += 1
                if flood_waits > MAX_FLOOD_RETRIES:
                    raise BackfillAbort(f"gave up after {flood_waits} flood waits") from exc
                if batch:
                    cursor = await _commit(sessions, chat.id, run_id, batch, totals, returned)
                wait = seconds + random.uniform(1, 5)
                log.warning(
                    "mtproto.flood_wait", seconds=seconds, sleeping=round(wait, 1), cursor=cursor
                )
                await sleep(wait)
        first_id = first_id or (min(returned) if returned else None)
        deleted = await _finish(
            sessions, chat.id, run_id, totals, returned, first_id, cursor, flood_waits
        )
    except BaseException as exc:
        async with sessions() as session:
            run = await session.get(ImportRun, run_id)
            if run is not None:
                run.status = ImportStatus.FAILED
                run.finished_at = utcnow()
                run.summary = {**(run.summary or {}), "error": f"{type(exc).__name__}"}
                await session.commit()
        if jobs is not None and job is not None:
            await jobs.fail(job.id, type(exc).__name__)
        raise
    if jobs is not None and job is not None:
        await jobs.complete(job.id, {"run_id": run_id})
    async with sessions() as session:
        run = await session.get(ImportRun, run_id)
        assert run is not None
        return BackfillResult(run, deleted, flood_waits)


async def _commit(
    sessions: async_sessionmaker[AsyncSession],
    chat_id: int,
    run_id: int,
    batch: list[NormalizedMessage],
    totals: UpsertStats,
    returned: set[int],
) -> int:
    async with sessions() as session:
        chat = await session.get(TelegramChat, chat_id)
        assert chat is not None
        stats = await MessageStore(session, chat).upsert(batch, import_run_id=run_id)
        totals.add(stats)
        returned.update(m.tg_message_id for m in batch)
        run = await session.get(ImportRun, run_id)
        assert run is not None
        run.seen += len(batch)
        run.inserted += stats.inserted
        run.updated += stats.updated
        run.unchanged += stats.unchanged
        run.skipped += stats.skipped
        last = max(m.tg_message_id for m in batch)
        run.checkpoint = {"last_tg_message_id": last}
        await session.commit()
        return last


async def _finish(
    sessions: async_sessionmaker[AsyncSession],
    chat_id: int,
    run_id: int,
    totals: UpsertStats,
    returned: set[int],
    first_id: int | None,
    last_id: int,
    flood_waits: int,
) -> int:
    deleted = 0
    async with sessions() as session:
        if first_id is not None and returned:
            # Previously stored messages inside the fetched id range that Telegram no longer
            # returns were deleted.
            stored = (
                await session.execute(
                    select(Message.id, Message.tg_message_id).where(
                        Message.chat_id == chat_id,
                        Message.tg_message_id >= first_id,
                        Message.tg_message_id <= last_id,
                        Message.is_deleted.is_(False),
                    )
                )
            ).all()
            gone = [row.id for row in stored if row.tg_message_id not in returned]
            if gone:
                await session.execute(
                    update(Message)
                    .where(Message.id.in_(gone))
                    .values(is_deleted=True, deleted_at=utcnow())
                )
            deleted = len(gone)
        if totals.min_sent_at is not None:
            await coverage.add_observed(
                session,
                chat_id,
                Source.MTPROTO,
                start_at=totals.min_sent_at,
                end_at=totals.max_sent_at,
                start_id=totals.min_id,
                end_id=totals.max_id,
                count=len(returned),
                import_run_id=run_id,
            )
        run = await session.get(ImportRun, run_id)
        assert run is not None
        run.status = ImportStatus.SUCCEEDED
        run.finished_at = utcnow()
        run.summary = {
            **(run.summary or {}),
            "first_returned_id": first_id,
            "flood_waits": flood_waits,
            "deleted_marked": deleted,
        }
        await session.commit()
    return deleted


async def open_client(api_id: int, api_hash: str, session_string: str) -> Any:  # pragma: no cover
    """Create and connect a Telethon client (requires the ``mtproto`` extra)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise BackfillAbort(
            "MTProto session is not authorized; run `elysium-bot mtproto login` again"
        )
    return client
