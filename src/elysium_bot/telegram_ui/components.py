"""Reusable, Telegram-independent UI building blocks (range math, calendar, paging, progress)."""

from __future__ import annotations

import calendar
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from elysium_bot.timeutil import utcnow

PRESETS: dict[str, tuple[str, timedelta]] = {
    "1h": ("1h", timedelta(hours=1)),
    "2h": ("2h", timedelta(hours=2)),
    "4h": ("4h", timedelta(hours=4)),
    "12h": ("12h", timedelta(hours=12)),
    "24h": ("24h", timedelta(hours=24)),
    "7d": ("7 days", timedelta(days=7)),
}


@dataclass(frozen=True)
class Window:
    start: datetime  # aware UTC
    end: datetime  # aware UTC
    label: str

    def to_state(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "label": self.label}

    @classmethod
    def from_state(cls, data: dict[str, str]) -> Window:
        return cls(
            datetime.fromisoformat(data["start"]),
            datetime.fromisoformat(data["end"]),
            data["label"],
        )


def preset_window(key: str, now: datetime | None = None) -> Window:
    label, delta = PRESETS[key]
    end = (now or utcnow()).astimezone(UTC)
    return Window(end - delta, end, f"Last {label}")


def local_to_utc(d: date, hour: int, minute: int, tz: str) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=ZoneInfo(tz)).astimezone(UTC)


# --- calendar ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarCell:
    label: str
    day: date | None  # None for padding cells


WEEKDAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


def month_grid(
    year: int, month: int, covered_days: set[date] | None = None
) -> list[list[CalendarCell]]:
    """Weeks (Monday-first) of day cells; days without stored messages are prefixed with '·'."""
    covered = covered_days or set()
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        row = []
        for d in week:
            if d.month != month:
                row.append(CalendarCell(" ", None))
            else:
                row.append(CalendarCell(str(d.day) if d in covered else f"·{d.day}", d))
        weeks.append(row)
    return weeks


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


# --- typed ranges -----------------------------------------------------------------------------


class RangeParseError(ValueError):
    pass


_SEP = re.compile(r"\s*(?:→|->|–|—|\bto\b|\s-\s)\s*", re.IGNORECASE)
_LAST = re.compile(r"^last\s+(\d+)\s*(h|hours?|d|days?|m|min|minutes?)$", re.IGNORECASE)


def _parse_point(text: str, tz: str, *, end_of_day: bool = False) -> datetime:
    text = text.strip()
    zone = ZoneInfo(tz)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=zone).astimezone(UTC)
        except ValueError:
            pass
    try:
        d = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise RangeParseError(f"cannot read date/time {text!r}") from exc
    if end_of_day:
        d = d + timedelta(days=1)
    return d.replace(tzinfo=zone).astimezone(UTC)


def parse_typed_range(text: str, tz: str, now: datetime | None = None) -> Window:
    """Parse a typed range in the chat timezone.

    Accepts '2026-11-01 10:00 → 2026-11-01 14:00', 'yesterday', 'today', 'last 3h', '2026-11-01'.
    """
    now = (now or utcnow()).astimezone(UTC)
    raw = text.strip()
    zone = ZoneInfo(tz)
    lowered = raw.lower()
    local_today = now.astimezone(zone).date()
    if lowered in ("today", "yesterday"):
        day = local_today if lowered == "today" else local_today - timedelta(days=1)
        start = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(UTC)
        end = min(start + timedelta(days=1), now)
        return Window(start, end, lowered.capitalize())
    m = _LAST.match(lowered)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "m": timedelta(minutes=n)}[unit]
        if delta <= timedelta(0):
            raise RangeParseError("range must be positive")
        return Window(now - delta, now, f"Last {n}{unit}")
    parts = _SEP.split(raw, maxsplit=1)
    if len(parts) == 2:
        start = _parse_point(parts[0], tz)
        end = (
            now
            if parts[1].strip().lower() == "now"
            else _parse_point(parts[1], tz, end_of_day=True)
        )
    else:
        start = _parse_point(raw, tz)
        if len(raw) == 10:  # a single date = that whole day
            end = start + timedelta(days=1)
        else:
            raise RangeParseError(
                "give a start and an end, e.g. 2026-11-01 10:00 → 2026-11-01 14:00"
            )
    if end <= start:
        raise RangeParseError("the end must be after the start")
    return Window(start, end, "Custom")


# --- pagination -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Page[T]:
    items: Sequence[T]
    page: int
    pages: int
    total: int

    @property
    def has_prev(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return self.page < self.pages - 1


def paginate[U](items: Sequence[U], page: int, per_page: int = 8) -> Page[U]:
    pages = max(1, -(-len(items) // per_page))
    page = min(max(page, 0), pages - 1)
    return Page(items[page * per_page : (page + 1) * per_page], page, pages, len(items))


# --- progress ---------------------------------------------------------------------------------


class ProgressMessage:
    """Throttled progress edits (≥ ``min_interval`` seconds apart; the final update always goes)."""

    def __init__(
        self,
        edit: Callable[[str], Awaitable[object]],
        min_interval: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._edit = edit
        self._min = min_interval
        self._clock = clock
        self._last: float | None = None
        self._last_text: str | None = None

    async def update(self, text: str, *, final: bool = False) -> bool:
        now = self._clock()
        if text == self._last_text:
            return False
        if not final and self._last is not None and now - self._last < self._min:
            return False
        await self._edit(text)
        self._last, self._last_text = now, text
        return True
