"""Time helpers. Storage is always aware UTC; display is in the chat's timezone."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` converted to UTC; reject naive datetimes."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"naive datetime not allowed (got {dt.isoformat()}); attach a timezone")
    return dt.astimezone(UTC)


def from_unix(seconds: int | float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


@lru_cache(maxsize=64)
def zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def to_local(dt: datetime, tz_name: str = DEFAULT_TIMEZONE) -> datetime:
    return ensure_utc(dt).astimezone(zone(tz_name))


def format_local(
    dt: datetime, tz_name: str = DEFAULT_TIMEZONE, fmt: str = "%a %d %b %Y %H:%M"
) -> str:
    return to_local(dt, tz_name).strftime(fmt)
