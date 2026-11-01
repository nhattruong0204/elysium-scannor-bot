"""Synthetic Telegram Desktop exports. All names, ids and texts are invented.

Dates mimic an exporter on a UTC+7 machine: ``date`` is local time, ``date_unixtime`` is UTC.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

EXPORTER_TZ = timezone(timedelta(hours=7))
BASE_TIME = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
SUPERGROUP_EXPORT_ID = 1234567890
SUPERGROUP_TG_ID = -1001234567890
CA = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"


def _times(at: datetime) -> dict[str, str]:
    return {
        "date": at.astimezone(EXPORTER_TZ).replace(tzinfo=None).isoformat(),
        "date_unixtime": str(int(at.timestamp())),
    }


def message(
    mid: int, text: Any = None, *, minutes: int | None = None, user: int = 101, **extra: Any
) -> dict[str, Any]:
    at = BASE_TIME + timedelta(minutes=mid if minutes is None else minutes)
    record: dict[str, Any] = {
        "id": mid,
        "type": "message",
        **_times(at),
        "from": f"Member {user}",
        "from_id": f"user{user}",
        "text": text if text is not None else f"synthetic message {mid}",
    }
    if isinstance(record["text"], str):
        record["text_entities"] = (
            [{"type": "plain", "text": record["text"]}] if record["text"] else []
        )
    record.update(extra)
    return record


def service(mid: int, action: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": mid,
        "type": "service",
        **_times(BASE_TIME + timedelta(minutes=mid)),
        "actor": "Member 101",
        "actor_id": "user101",
        "action": action,
        "text": "",
        "text_entities": [],
        **extra,
    }


def chat_export(
    messages: list[dict[str, Any]],
    *,
    export_id: int = SUPERGROUP_EXPORT_ID,
    export_type: str = "private_supergroup",
    name: str = "Synthetic Plays",
) -> dict[str, Any]:
    return {"name": name, "type": export_type, "id": export_id, "messages": messages}


def supergroup_messages(n: int = 25) -> list[dict[str, Any]]:
    assert n >= 8, "fixture needs at least 8 messages"
    msgs = [message(i, f"msg {i} about $NEST", user=101 + i % 3) for i in range(1, n + 1)]
    msgs[4]["text"] = [
        "CA ",
        {"type": "code", "text": CA},
        " docs ",
        {"type": "text_link", "text": "here", "href": "https://docs.nest.example"},
    ]
    msgs[4].pop("text_entities")
    msgs[6]["reply_to_message_id"] = 5
    msgs[7]["edited"] = msgs[7]["date"]
    msgs[7]["edited_unixtime"] = str(int(msgs[7]["date_unixtime"]) + 60)
    msgs.append(service(n + 1, "pin_message", message_id=5))
    msgs.append(message(n + 2, "", photo="photos/p.jpg"))
    return msgs


def forum_messages() -> list[dict[str, Any]]:
    return [
        service(1, "topic_created", title="Launches"),
        message(2, "first in topic", reply_to_message_id=1),
        message(3, "reply inside topic", reply_to_message_id=2),
        message(4, "general chatter"),
    ]


def write_json(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def write_zip(path: Path, data: dict[str, Any]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ChatExport_2026-11-01/result.json", json.dumps(data))
    return path


def full_account_export(chats: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "about": "synthetic",
        "personal_information": {"user_id": 1},
        "chats": {"about": "x", "list": chats},
    }


def stream_large_export(path: Path, n: int) -> Path:
    """Write an n-message export without holding it in memory."""

    def records() -> Iterator[str]:
        for i in range(1, n + 1):
            yield json.dumps(
                message(i, f"bulk message {i} gm $HYPE", minutes=i // 10, user=100 + i % 500)
            )

    with path.open("w", encoding="utf-8") as fh:
        header = {"name": "Bulk", "type": "private_supergroup", "id": SUPERGROUP_EXPORT_ID}
        fh.write(json.dumps(header)[:-1] + ', "messages": [')
        for i, rec in enumerate(records()):
            if i:
                fh.write(",")
            fh.write(rec)
        fh.write("]}")
    return path
