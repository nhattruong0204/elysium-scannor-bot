"""``NormalizedMessage``: the single internal message shape (HISTORY_INGESTION §2).

Entity offsets/lengths are kept in Telegram's UTF-16 code units, exactly as Telegram reports them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict
from telegram import Message as PtbMessage
from telegram import MessageOrigin

from elysium_bot.db.enums import Source
from elysium_bot.timeutil import ensure_utc, from_unix

# Telegram's placeholder sender for anonymous group admins.
GROUP_ANONYMOUS_BOT_ID = 1087968824


class SenderRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["user", "chat"]
    tg_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    is_bot: bool = False

    @property
    def display_name(self) -> str | None:
        if self.kind == "chat":
            return self.title
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        return name or self.username


class Entity(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str
    offset: int
    length: int
    url: str | None = None
    user_id: int | None = None


class NormalizedMessage(BaseModel):
    source: Source
    tg_chat_id: int
    tg_message_id: int
    message_thread_id: int | None = None
    sender: SenderRef | None = None
    sent_at: AwareDatetime
    edited_at: AwareDatetime | None = None
    text: str = ""
    entities: list[Entity] = []
    reply_to_tg_message_id: int | None = None
    forward_origin: dict[str, Any] | None = None
    media_type: str | None = None
    media_meta: dict[str, Any] | None = None
    is_service: bool = False
    service_action: str | None = None
    raw_ref: str | None = None

    def content_hash(self) -> str:
        media_sig = json.dumps(
            [
                self.media_type,
                (self.media_meta or {}).get("file_name"),
                (self.media_meta or {}).get("question"),
            ],
            ensure_ascii=False,
        )
        return hashlib.sha256(f"{self.text}\x00{media_sig}".encode()).hexdigest()

    def entities_json(self) -> list[dict[str, Any]] | None:
        return [e.model_dump(exclude_none=True) for e in self.entities] or None


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


# --------------------------------------------------------------------------------------------
# Bot API (python-telegram-bot)
# --------------------------------------------------------------------------------------------

_PTB_SERVICE_FIELDS = (
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "migrate_to_chat_id",
    "migrate_from_chat_id",
    "pinned_message",
    "forum_topic_created",
    "forum_topic_edited",
    "forum_topic_closed",
    "forum_topic_reopened",
    "video_chat_started",
    "video_chat_ended",
    "message_auto_delete_timer_changed",
)
_PTB_MEDIA_FIELDS = (
    "photo",
    "video",
    "animation",
    "document",
    "sticker",
    "voice",
    "audio",
    "video_note",
    "poll",
    "location",
    "contact",
    "dice",
)


def _origin_to_dict(origin: MessageOrigin) -> dict[str, Any]:
    data: dict[str, Any] = {"type": origin.type, "date": origin.date.isoformat()}
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        data["sender_user_id"] = sender_user.id
        data["name"] = sender_user.full_name
    if getattr(origin, "sender_user_name", None):
        data["name"] = origin.sender_user_name  # type: ignore[attr-defined]
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat is not None:
        data["tg_chat_id"] = chat.id
        data["name"] = chat.title or chat.username
    if getattr(origin, "message_id", None) is not None:
        data["tg_message_id"] = origin.message_id  # type: ignore[attr-defined]
    return data


def _ptb_media(message: PtbMessage) -> tuple[str | None, dict[str, Any] | None]:
    for name in _PTB_MEDIA_FIELDS:
        value = getattr(message, name, None)
        if not value:
            continue
        meta: dict[str, Any] = {}
        if name == "document" and message.document:
            meta = {
                "file_name": message.document.file_name,
                "mime_type": message.document.mime_type,
            }
        elif name == "poll" and message.poll:
            meta = {
                "question": message.poll.question,
                "options": [o.text for o in message.poll.options],
            }
        elif name in ("video", "voice", "audio", "video_note", "animation"):
            meta = {"duration": getattr(value, "duration", None)}
        elif name == "sticker" and message.sticker:
            meta = {"emoji": message.sticker.emoji}
        return name, {k: v for k, v in meta.items() if v is not None} or None
    return None, None


def from_ptb(message: PtbMessage, source: Source = Source.BOT_API) -> NormalizedMessage:
    sender: SenderRef | None = None
    if message.sender_chat is not None:
        sc = message.sender_chat
        sender = SenderRef(kind="chat", tg_id=sc.id, title=sc.title, username=sc.username)
    elif message.from_user is not None:
        u = message.from_user
        sender = SenderRef(
            kind="user",
            tg_id=u.id,
            username=u.username,
            first_name=u.first_name,
            last_name=u.last_name,
            is_bot=u.is_bot,
        )

    text = message.text or message.caption or ""
    raw_entities = message.entities if message.text else message.caption_entities
    entities = [
        Entity(
            type=str(e.type),
            offset=e.offset,
            length=e.length,
            url=e.url,
            user_id=e.user.id if e.user else None,
        )
        for e in raw_entities or ()
    ]
    service_action = next((f for f in _PTB_SERVICE_FIELDS if getattr(message, f, None)), None)
    media_type, media_meta = _ptb_media(message)
    reply = message.reply_to_message
    return NormalizedMessage(
        source=source,
        tg_chat_id=message.chat.id,
        tg_message_id=message.message_id,
        message_thread_id=message.message_thread_id,
        sender=sender,
        sent_at=ensure_utc(message.date),
        edited_at=ensure_utc(message.edit_date) if message.edit_date else None,
        text=text,
        entities=entities,
        reply_to_tg_message_id=reply.message_id if reply is not None else None,
        forward_origin=_origin_to_dict(message.forward_origin) if message.forward_origin else None,
        media_type=media_type,
        media_meta=media_meta,
        is_service=service_action is not None,
        service_action=service_action,
    )


# --------------------------------------------------------------------------------------------
# Telegram Desktop JSON export
# --------------------------------------------------------------------------------------------

EXPORT_KNOWN_FIELDS = frozenset(
    {
        "id",
        "type",
        "date",
        "date_unixtime",
        "edited",
        "edited_unixtime",
        "from",
        "from_id",
        "actor",
        "actor_id",
        "action",
        "text",
        "text_entities",
        "reply_to_message_id",
        "reply_to_peer_id",
        "forwarded_from",
        "forwarded_from_id",
        "saved_from",
        "media_type",
        "mime_type",
        "file",
        "file_name",
        "file_size",
        "thumbnail",
        "thumbnail_file_size",
        "duration_seconds",
        "width",
        "height",
        "photo",
        "photo_file_size",
        "poll",
        "sticker_emoji",
        "via_bot",
        "title",
        "members",
        "message_id",
        "new_title",
        "new_icon_emoji_id",
        "inviter",
        "boosts",
        "reactions",
        "contact_information",
        "location_information",
        "live_location_period_seconds",
        "self_destruct_period_seconds",
        "author",
        "performer",
        "game_title",
        "game_description",
        "game_link",
        "invoice_information",
        "duration",
        "discard_reason",
        "emoticon",
        "period",
        "schedule_date",
        "story",
    }
)
_EXPORT_ENTITY_TYPES = {"link": "url", "mention_name": "text_mention"}


@dataclass
class ExportChatContext:
    tg_chat_id: int
    fallback_tz: str = "Asia/Ho_Chi_Minh"
    used_local_dates: int = 0
    unknown_fields: dict[str, int] = field(default_factory=dict)


class ExportRecordError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def export_chat_tg_id(export_type: str, export_id: int) -> int:
    """Map a Desktop-export chat id to Bot API form (``-100…`` for supergroups/channels)."""
    if export_type in (
        "private_supergroup",
        "public_supergroup",
        "private_channel",
        "public_channel",
    ):
        return int(f"-100{export_id}")
    if export_type == "private_group":
        return -export_id
    return export_id


def _peer(value: Any) -> tuple[str, int] | None:
    if not isinstance(value, str):
        return None
    for prefix, kind in (("user", "user"), ("channel", "chat"), ("chat", "chat")):
        if value.startswith(prefix) and value[len(prefix) :].isdigit():
            raw = int(value[len(prefix) :])
            return kind, (
                int(f"-100{raw}") if prefix == "channel" else (-raw if prefix == "chat" else raw)
            )
    return None


def _export_time(record: dict[str, Any], key: str, ctx: ExportChatContext) -> datetime | None:
    unix = record.get(f"{key}_unixtime")
    if unix not in (None, ""):
        return from_unix(int(unix))
    local = record.get(key)
    if not local:
        return None
    ctx.used_local_dates += 1
    return datetime.fromisoformat(str(local)).replace(tzinfo=ZoneInfo(ctx.fallback_tz))


def _export_text(record: dict[str, Any]) -> tuple[str, list[Entity]]:
    pieces: list[dict[str, Any]]
    if isinstance(record.get("text_entities"), list):
        pieces = record["text_entities"]
    elif isinstance(record.get("text"), list):
        pieces = [
            p if isinstance(p, dict) else {"type": "plain", "text": p} for p in record["text"]
        ]
    else:
        pieces = [{"type": "plain", "text": str(record.get("text") or "")}]

    text_parts: list[str] = []
    entities: list[Entity] = []
    offset = 0
    for piece in pieces:
        chunk = str(piece.get("text", ""))
        kind = str(piece.get("type", "plain"))
        length = utf16_len(chunk)
        if kind != "plain" and length:
            entities.append(
                Entity(
                    type=_EXPORT_ENTITY_TYPES.get(kind, kind),
                    offset=offset,
                    length=length,
                    url=piece.get("href"),
                    user_id=piece.get("user_id"),
                )
            )
        text_parts.append(chunk)
        offset += length
    return "".join(text_parts), entities


def _export_media(record: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    if "poll" in record and isinstance(record["poll"], dict):
        poll = record["poll"]
        return "poll", {
            "question": poll.get("question"),
            "options": [a.get("text") for a in poll.get("answers", []) if isinstance(a, dict)],
        }
    if "photo" in record:
        return "photo", None
    media_type = record.get("media_type")
    if media_type or "file" in record:
        meta = {
            "file_name": record.get("file_name"),
            "mime_type": record.get("mime_type"),
            "duration": record.get("duration_seconds"),
            "emoji": record.get("sticker_emoji"),
        }
        return str(media_type or "document"), {
            k: v for k, v in meta.items() if v is not None
        } or None
    return None, None


def from_export(record: dict[str, Any], ctx: ExportChatContext, index: int) -> NormalizedMessage:
    for key in record:
        if key not in EXPORT_KNOWN_FIELDS:
            ctx.unknown_fields[key] = ctx.unknown_fields.get(key, 0) + 1

    raw_ref = f"messages[{index}]"
    if not isinstance(record.get("id"), int):
        raise ExportRecordError("missing_id", f"{raw_ref} has no integer id")
    rtype = record.get("type")
    if rtype not in ("message", "service"):
        raise ExportRecordError("unsupported_type", f"{raw_ref} type={rtype!r}")

    sent_at = _export_time(record, "date", ctx)
    if sent_at is None:
        raise ExportRecordError("missing_date", f"{raw_ref} (id {record['id']}) has no date")
    is_service = rtype == "service"
    peer = _peer(record.get("actor_id" if is_service else "from_id"))
    name = record.get("actor" if is_service else "from")
    sender: SenderRef | None = None
    if peer is not None:
        kind, tg_id = peer
        sender = (
            SenderRef(kind="user", tg_id=tg_id, first_name=name)
            if kind == "user"
            else SenderRef(kind="chat", tg_id=tg_id, title=name)
        )

    text, entities = _export_text(record)
    media_type, media_meta = _export_media(record)
    if record.get("reply_to_peer_id"):
        media_meta = {**(media_meta or {}), "reply_peer": record["reply_to_peer_id"]}
    forward = None
    if record.get("forwarded_from") or record.get("forwarded_from_id"):
        forward = {"type": "export", "name": record.get("forwarded_from")}
        fpeer = _peer(record.get("forwarded_from_id"))
        if fpeer:
            forward["tg_id"] = fpeer[1]

    return NormalizedMessage(
        source=Source.DESKTOP_EXPORT,
        tg_chat_id=ctx.tg_chat_id,
        tg_message_id=record["id"],
        sender=sender,
        sent_at=sent_at,
        edited_at=_export_time(record, "edited", ctx),
        text=text,
        entities=entities,
        reply_to_tg_message_id=None
        if record.get("reply_to_peer_id")
        else record.get("reply_to_message_id"),
        forward_origin=forward,
        media_type=media_type,
        media_meta=media_meta,
        is_service=is_service,
        service_action=str(record["action"]) if is_service and record.get("action") else None,
        raw_ref=raw_ref,
    )
