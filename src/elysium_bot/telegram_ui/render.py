"""HTML rendering helpers. All dynamic text MUST pass through ``esc`` (TELEGRAM_UX §1.6)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

MESSAGE_LIMIT = 4096
SAFE_LIMIT = 4000

OK, DEGRADED, FAILED, RUNNING, DISABLED, CAVEAT = "🟢", "🟡", "🔴", "⏳", "⚪", "⚠️"


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def attr(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def link(url: str, label: str) -> str:
    return f'<a href="{attr(url)}">{esc(label)}</a>'


def message_url(
    tg_chat_id: int,
    tg_message_id: int,
    *,
    username: str | None = None,
    thread_id: int | None = None,
) -> str:
    """t.me link to a message. Private-supergroup links only open for members, which is intended."""
    if username:
        return f"https://t.me/{username}/{tg_message_id}"
    internal = str(tg_chat_id).removeprefix("-100").removeprefix("-")
    if thread_id:
        return f"https://t.me/c/{internal}/{thread_id}/{tg_message_id}"
    return f"https://t.me/c/{internal}/{tg_message_id}"


def fmt_int(n: int | None) -> str:
    return "—" if n is None else f"{n:,}"


@dataclass
class Screen:
    """A rendered control-center view: HTML text + inline keyboard rows."""

    text: str
    rows: list[list[InlineKeyboardButton]] = field(default_factory=list)
    alert: str | None = None  # optional toast/alert for the callback answer

    def markup(self) -> InlineKeyboardMarkup | None:
        return InlineKeyboardMarkup(self.rows) if self.rows else None


# --- splitting --------------------------------------------------------------------------------

_TAG = re.compile(r"<(/?)([a-zA-Z]+)([^>]*)>")


def _open_tags_at_end(chunk: str) -> list[tuple[str, str]]:
    stack: list[tuple[str, str]] = []
    for m in _TAG.finditer(chunk):
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == name:
                    del stack[i:]
                    break
        else:
            stack.append((name, attrs))
    return stack


def _safe_cut(text: str, limit: int) -> int:
    """Largest cut <= limit that is not inside a tag or an HTML entity."""
    cut = limit
    lt, gt = text.rfind("<", 0, cut), text.rfind(">", 0, cut)
    if lt > gt:
        cut = lt
    amp = text.rfind("&", 0, cut)
    if amp != -1 and ";" not in text[amp:cut]:
        cut = amp
    return cut


def split_html(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Split Telegram HTML into chunks <= limit, preferring section/paragraph boundaries.

    Tags open at a cut are closed at the end of the chunk and re-opened in the next one, so every
    chunk is valid HTML on its own.
    """
    chunks: list[str] = []
    reopen = ""
    rest = text
    while rest:
        candidate = reopen + rest
        if len(candidate) <= limit:
            chunks.append(candidate)
            break
        budget = limit - 120  # room for closing/re-opening tags
        window = candidate[:budget]
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut <= len(reopen) + budget // 3:
            cut = window.rfind(" ")
        if cut <= len(reopen):
            cut = budget
        cut = _safe_cut(candidate, cut)
        if cut <= len(reopen):
            cut = budget
        head = candidate[:cut]
        stack = _open_tags_at_end(head)
        closing = "".join(f"</{name}>" for name, _ in reversed(stack))
        chunks.append(head.rstrip() + closing)
        reopen = "".join(f"<{name}{attrs}>" for name, attrs in stack)
        rest = candidate[cut:].lstrip("\n ")
    return chunks
