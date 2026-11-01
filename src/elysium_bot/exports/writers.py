"""Streaming export writers. See docs/EXPORT_FORMAT.md for the field reference."""

from __future__ import annotations

import csv
import io
import json
from typing import IO, Any, Protocol

FIELDS = [
    "chat_id",
    "tg_message_id",
    "message_link",
    "sent_at_utc",
    "sent_at_local",
    "sender_id",
    "sender_name",
    "sender_username",
    "reply_to_tg_message_id",
    "message_thread_id",
    "text",
    "edited_at_utc",
    "edit_count",
    "forward_from",
    "media_type",
    "is_service",
    "links",
    "contract_strings",
    "sources",
]
CONTRACT_NOTE = "contract_strings are extracted strings, NOT verified contracts"


class ExportWriter(Protocol):
    extension: str

    def begin(self, meta: dict[str, Any]) -> None: ...
    def row(self, row: dict[str, Any]) -> None: ...
    def end(self, summary: dict[str, Any]) -> None: ...


class _Base:
    extension = "txt"

    def __init__(self, fh: IO[str]) -> None:
        self.fh = fh
        self.count = 0


class JsonWriter(_Base):
    extension = "json"

    def begin(self, meta: dict[str, Any]) -> None:
        self.fh.write(
            '{"meta": ' + json.dumps(meta, ensure_ascii=False, default=str) + ', "messages": [\n'
        )

    def row(self, row: dict[str, Any]) -> None:
        if self.count:
            self.fh.write(",\n")
        self.fh.write(json.dumps(row, ensure_ascii=False, default=str))
        self.count += 1

    def end(self, summary: dict[str, Any]) -> None:
        self.fh.write(
            '\n], "summary": ' + json.dumps(summary, ensure_ascii=False, default=str) + "}\n"
        )


class CsvWriter(_Base):
    """RFC 4180 CSV with UTF-8 BOM. Metadata lines start with '#' (read with comment='#')."""

    extension = "csv"

    def begin(self, meta: dict[str, Any]) -> None:
        self.fh.write("﻿")
        for key, value in meta.items():
            flat = (
                json.dumps(value, ensure_ascii=False, default=str)
                if isinstance(value, dict | list)
                else value
            )
            self.fh.write(f"# {key}: {str(flat).replace(chr(10), ' ')}\n")
        self.writer = csv.DictWriter(self.fh, fieldnames=FIELDS, lineterminator="\r\n")
        self.writer.writeheader()

    def row(self, row: dict[str, Any]) -> None:
        flat = {
            k: json.dumps(v, ensure_ascii=False)
            if isinstance(v, list | dict)
            else ("" if v is None else v)
            for k, v in row.items()
        }
        self.writer.writerow(flat)
        self.count += 1

    def end(self, summary: dict[str, Any]) -> None:
        return None


def _who(row: dict[str, Any]) -> str:
    return str(row.get("sender_name") or row.get("sender_id") or "unknown")


class TxtWriter(_Base):
    extension = "txt"

    def begin(self, meta: dict[str, Any]) -> None:
        for key, value in meta.items():
            self.fh.write(f"{key}: {value}\n")
        self.fh.write("=" * 72 + "\n")

    def row(self, row: dict[str, Any]) -> None:
        reply = f" ↩{row['reply_to_tg_message_id']}" if row.get("reply_to_tg_message_id") else ""
        edited = " (edited)" if row.get("edit_count") else ""
        body = row.get("text") or (f"[{row['media_type']}]" if row.get("media_type") else "")
        if row.get("is_service"):
            body = f"[service] {body}".strip()
        body = body.replace("\n", "\n    ")  # continuation lines are indented
        self.fh.write(
            f"[{row['sent_at_local']}] #{row['tg_message_id']}{reply} {_who(row)}: {body}{edited}\n"
        )
        self.count += 1

    def end(self, summary: dict[str, Any]) -> None:
        self.fh.write("=" * 72 + f"\nrows: {summary.get('row_count')}\n")


def _md_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("<", "&lt;")


class MarkdownWriter(_Base):
    extension = "md"

    def begin(self, meta: dict[str, Any]) -> None:
        self.day: str | None = None
        self.fh.write(f"# Chat export — {meta.get('chat_title')}\n\n")
        for key, value in meta.items():
            self.fh.write(f"- **{key}**: {value}\n")
        self.fh.write("\n")

    def row(self, row: dict[str, Any]) -> None:
        day = str(row["sent_at_local"])[:10]
        if day != self.day:
            self.fh.write(f"\n## {day}\n\n")
            self.day = day
        reply = f" ↩ [{row['reply_to_tg_message_id']}]" if row.get("reply_to_tg_message_id") else ""
        body = _md_escape(
            row.get("text") or (f"[{row['media_type']}]" if row.get("media_type") else "")
        )
        body = body.replace("\n", "  \n  ")  # hard line breaks inside the list item
        link = f"[#{row['tg_message_id']}]({row['message_link']})"
        when = str(row["sent_at_local"])[11:16]
        self.fh.write(f"- **{when}** {link} **{_md_escape(_who(row))}**{reply}: {body}\n")
        self.count += 1

    def end(self, summary: dict[str, Any]) -> None:
        self.fh.write(f"\n---\nRows: {summary.get('row_count')}\n")


WRITERS: dict[str, type[_Base]] = {
    "json": JsonWriter,
    "csv": CsvWriter,
    "txt": TxtWriter,
    "md": MarkdownWriter,
}


def render_to_string(fmt: str, meta: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """Convenience for tests and tiny exports."""
    buf = io.StringIO()
    writer = WRITERS[fmt](buf)
    writer.begin(meta)  # type: ignore[attr-defined]
    for row in rows:
        writer.row(row)  # type: ignore[attr-defined]
    writer.end({"row_count": len(rows)})  # type: ignore[attr-defined]
    return buf.getvalue()
