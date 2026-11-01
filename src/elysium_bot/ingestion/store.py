"""``MessageStore``: the only writer to ``messages`` (HISTORY_INGESTION §3.2, §4.4).

Supergroups dedup on ``(chat_id, tg_message_id)``. Basic groups (per-user message ids) dedup on a
fingerprint ``(sender, sent_at, content_hash)`` — see DATABASE_DESIGN §3.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from elysium_bot.db.enums import ChatType, IndexState, Source
from elysium_bot.db.models import (
    ChatMember,
    Message,
    MessageSource,
    MessageVersion,
    TelegramChat,
    TelegramUser,
)
from elysium_bot.ingestion.normalizer import NormalizedMessage, SenderRef
from elysium_bot.timeutil import utcnow


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    versions_added: int = 0
    skipped: int = 0
    skipped_refs: list[tuple[str | None, str]] = field(default_factory=list)
    min_sent_at: Any = None
    max_sent_at: Any = None
    min_id: int | None = None
    max_id: int | None = None

    def add(self, other: UpsertStats) -> None:
        for name in ("inserted", "updated", "unchanged", "versions_added", "skipped"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.skipped_refs.extend(other.skipped_refs)
        for attr, pick in (
            ("min_sent_at", min),
            ("max_sent_at", max),
            ("min_id", min),
            ("max_id", max),
        ):
            ours, theirs = getattr(self, attr), getattr(other, attr)
            setattr(
                self,
                attr,
                theirs if ours is None else ours if theirs is None else pick(ours, theirs),
            )

    def track(self, msg: NormalizedMessage) -> None:
        self.min_sent_at = (
            msg.sent_at if self.min_sent_at is None else min(self.min_sent_at, msg.sent_at)
        )
        self.max_sent_at = (
            msg.sent_at if self.max_sent_at is None else max(self.max_sent_at, msg.sent_at)
        )
        self.min_id = (
            msg.tg_message_id if self.min_id is None else min(self.min_id, msg.tg_message_id)
        )
        self.max_id = (
            msg.tg_message_id if self.max_id is None else max(self.max_id, msg.tg_message_id)
        )


class MessageStore:
    def __init__(self, session: AsyncSession, chat: TelegramChat) -> None:
        self.session = session
        self.chat = chat

    # ----- users & members ------------------------------------------------------------------

    async def _ensure_users(self, senders: list[SenderRef], *, refresh: bool) -> dict[int, int]:
        """Map tg_user_id -> telegram_users.id, inserting unknown users."""
        ids = sorted({s.tg_id for s in senders})
        if not ids:
            return {}
        rows = await self.session.execute(
            select(TelegramUser).where(TelegramUser.tg_user_id.in_(ids))
        )
        users = {u.tg_user_id: u for u in rows.scalars()}
        now = utcnow()
        latest = {s.tg_id: s for s in senders}
        for tg_id in ids:
            ref = latest[tg_id]
            user = users.get(tg_id)
            if user is None:
                user = TelegramUser(
                    tg_user_id=tg_id,
                    username=ref.username,
                    first_name=ref.first_name,
                    last_name=ref.last_name,
                    is_bot=ref.is_bot,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                self.session.add(user)
                users[tg_id] = user
            elif refresh:
                # Live updates carry authoritative profile data; exports only have display names.
                user.username = ref.username
                user.first_name = ref.first_name
                user.last_name = ref.last_name
                user.last_seen_at = now
        await self.session.flush()
        await self._ensure_members([users[i].id for i in ids])
        return {tg_id: users[tg_id].id for tg_id in ids}

    async def _ensure_members(self, user_ids: list[int]) -> None:
        existing = set(
            (
                await self.session.execute(
                    select(ChatMember.user_id).where(
                        ChatMember.chat_id == self.chat.id, ChatMember.user_id.in_(user_ids)
                    )
                )
            ).scalars()
        )
        missing = [uid for uid in user_ids if uid not in existing]
        if not missing:
            return
        count = (
            await self.session.execute(
                select(func.count())
                .select_from(ChatMember)
                .where(ChatMember.chat_id == self.chat.id)
            )
        ).scalar_one()
        for n, uid in enumerate(missing, start=count + 1):
            self.session.add(ChatMember(chat_id=self.chat.id, user_id=uid, pseudonym=f"U{n}"))
        await self.session.flush()

    async def member_for(self, sender: SenderRef) -> ChatMember:
        """Ensure the user and their chat membership exist (e.g. for /consent)."""
        uid = (await self._ensure_users([sender], refresh=True))[sender.tg_id]
        member = await self.session.get(ChatMember, (self.chat.id, uid))
        assert member is not None
        return member

    # ----- messages -------------------------------------------------------------------------

    async def upsert(
        self, batch: Sequence[NormalizedMessage], *, import_run_id: int | None = None
    ) -> UpsertStats:
        """Upsert one batch inside the caller's transaction. Retries once on a write race."""
        try:
            async with self.session.begin_nested():
                return await self._upsert(batch, import_run_id)
        except IntegrityError:
            # A concurrent writer (live collector vs importer) inserted the same row; re-run so
            # it is seen as existing.
            async with self.session.begin_nested():
                return await self._upsert(batch, import_run_id)

    async def _upsert(
        self, batch: Sequence[NormalizedMessage], import_run_id: int | None
    ) -> UpsertStats:
        stats = UpsertStats()
        if not batch:
            return stats
        source = batch[0].source
        user_senders = [m.sender for m in batch if m.sender is not None and m.sender.kind == "user"]
        user_map = await self._ensure_users(user_senders, refresh=source == Source.BOT_API)

        existing = await self._existing(batch, user_map)
        now = utcnow()
        touched: list[Message] = []
        pending_insert = False
        for msg in batch:
            stats.track(msg)
            sender_uid = (
                user_map.get(msg.sender.tg_id) if msg.sender and msg.sender.kind == "user" else None
            )
            key = self._key(msg, sender_uid)
            row = existing.get(key)
            if row is None:
                if self.chat.type == ChatType.GROUP and await self._id_taken(msg.tg_message_id):
                    stats.skipped += 1
                    stats.skipped_refs.append((msg.raw_ref, "basic_group_id_collision"))
                    continue
                row = self._new_row(msg, sender_uid, now)
                self.session.add(row)
                existing[key] = row
                pending_insert = True
                stats.inserted += 1
            else:
                if row.id is None:  # same message twice in one batch: flush the first copy
                    await self.session.flush()
                changed, versions = await self._merge(row, msg, sender_uid, now)
                stats.versions_added += versions
                if changed:
                    stats.updated += 1
                else:
                    stats.unchanged += 1
            touched.append(row)
        if pending_insert:
            await self.session.flush()  # one multi-row INSERT per batch
        await self._touch_sources([r.id for r in touched], source, import_run_id, now)
        return stats

    def _key(self, msg: NormalizedMessage, sender_uid: int | None) -> tuple[Any, ...]:
        if self.chat.type == ChatType.GROUP:
            return (msg.sender.tg_id if msg.sender else None, msg.sent_at, msg.content_hash())
        return (msg.tg_message_id,)

    async def _existing(
        self, batch: Sequence[NormalizedMessage], user_map: dict[int, int]
    ) -> dict[tuple[Any, ...], Message]:
        if self.chat.type == ChatType.GROUP:
            times = {m.sent_at for m in batch}
            rows = await self.session.execute(
                select(Message, TelegramUser.tg_user_id)
                .outerjoin(TelegramUser, Message.sender_user_id == TelegramUser.id)
                .where(Message.chat_id == self.chat.id, Message.sent_at.in_(times))
            )
            return {
                (
                    tg_uid if tg_uid is not None else r.sender_chat_tg_id,
                    r.sent_at,
                    r.content_hash,
                ): r
                for r, tg_uid in rows.all()
            }
        ids = [m.tg_message_id for m in batch]
        rows2 = await self.session.execute(
            select(Message).where(Message.chat_id == self.chat.id, Message.tg_message_id.in_(ids))
        )
        return {(r.tg_message_id,): r for r in rows2.scalars()}

    async def _id_taken(self, tg_message_id: int) -> bool:
        found = await self.session.execute(
            select(Message.id).where(
                Message.chat_id == self.chat.id, Message.tg_message_id == tg_message_id
            )
        )
        return found.first() is not None

    def _new_row(self, msg: NormalizedMessage, sender_uid: int | None, now: Any) -> Message:
        return Message(
            chat_id=self.chat.id,
            tg_message_id=msg.tg_message_id,
            message_thread_id=msg.message_thread_id,
            sender_user_id=sender_uid,
            sender_chat_tg_id=msg.sender.tg_id
            if msg.sender and msg.sender.kind == "chat"
            else None,
            sender_name_snapshot=msg.sender.display_name if msg.sender else None,
            sent_at=msg.sent_at,
            edited_at=msg.edited_at,
            text=msg.text,
            entities=msg.entities_json(),
            reply_to_tg_message_id=msg.reply_to_tg_message_id,
            forward_origin=msg.forward_origin,
            media_type=msg.media_type,
            media_meta=msg.media_meta,
            is_service=msg.is_service,
            service_action=msg.service_action,
            content_hash=msg.content_hash(),
            first_ingested_at=now,
            last_ingested_at=now,
            primary_source=msg.source,
            index_state=IndexState.PENDING,
        )

    async def _merge(
        self, row: Message, msg: NormalizedMessage, sender_uid: int | None, now: Any
    ) -> tuple[bool, int]:
        changed = False
        versions = 0
        # Fill gaps from richer sources without overwriting known values.
        fills = {
            "message_thread_id": msg.message_thread_id,
            "reply_to_tg_message_id": msg.reply_to_tg_message_id,
            "sender_user_id": sender_uid,
            "forward_origin": msg.forward_origin,
            "media_type": msg.media_type,
            "media_meta": msg.media_meta,
        }
        for attr, value in fills.items():
            if value is not None and getattr(row, attr) is None:
                setattr(row, attr, value)
                changed = True

        new_entities = msg.entities_json()
        content_differs = row.text != msg.text or (row.entities or None) != new_entities
        incoming_newer = msg.edited_at is not None and (
            row.edited_at is None or msg.edited_at > row.edited_at
        )
        if content_differs and incoming_newer:
            versions = await self._record_edit(row, msg, new_entities)
            row.text = msg.text
            row.entities = new_entities
            row.edited_at = msg.edited_at
            row.content_hash = msg.content_hash()
            row.index_state = IndexState.PENDING
            changed = True
        elif incoming_newer and row.edited_at != msg.edited_at:
            row.edited_at = msg.edited_at
            changed = True
        if changed:
            row.last_ingested_at = now
        return changed, versions

    async def _record_edit(
        self, row: Message, msg: NormalizedMessage, new_entities: list[dict[str, Any]] | None
    ) -> int:
        last = (
            await self.session.execute(
                select(func.max(MessageVersion.version_no)).where(
                    MessageVersion.message_id == row.id
                )
            )
        ).scalar_one()
        added = 0
        if last is None:
            # Version 1 is the first-seen text; only materialized once an edit exists.
            self.session.add(
                MessageVersion(
                    message_id=row.id,
                    version_no=1,
                    text=row.text,
                    entities=row.entities,
                    edited_at=row.edited_at,
                    source=row.primary_source,
                )
            )
            last, added = 1, 1
        self.session.add(
            MessageVersion(
                message_id=row.id,
                version_no=last + 1,
                text=msg.text,
                entities=new_entities,
                edited_at=msg.edited_at,
                source=msg.source,
            )
        )
        return added + 1

    async def _touch_sources(
        self, message_ids: list[int], source: Source, import_run_id: int | None, now: Any
    ) -> None:
        if not message_ids:
            return
        have = set(
            (
                await self.session.execute(
                    select(MessageSource.message_id).where(
                        MessageSource.message_id.in_(message_ids), MessageSource.source == source
                    )
                )
            ).scalars()
        )
        for mid in dict.fromkeys(message_ids):
            if mid not in have:
                self.session.add(
                    MessageSource(
                        message_id=mid,
                        source=source,
                        import_run_id=import_run_id,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
        if have:
            await self.session.execute(
                update(MessageSource)
                .where(MessageSource.message_id.in_(have), MessageSource.source == source)
                .values(last_seen_at=now)
            )
        await self.session.flush()
