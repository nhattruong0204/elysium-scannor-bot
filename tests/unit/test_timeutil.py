from datetime import UTC, datetime, timedelta, timezone

import pytest
import time_machine

from elysium_bot.timeutil import ensure_utc, format_local, from_unix, to_local, utcnow

ICT = timezone(timedelta(hours=7))


def test_utcnow_is_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is UTC


@time_machine.travel(datetime(2026, 11, 1, 3, 0, tzinfo=UTC), tick=False)
def test_utcnow_follows_clock() -> None:
    assert utcnow() == datetime(2026, 11, 1, 3, 0, tzinfo=UTC)


def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 11, 1, 10, 0))


def test_ensure_utc_converts_offset() -> None:
    local = datetime(2026, 11, 1, 10, 0, tzinfo=ICT)
    assert ensure_utc(local) == datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
    assert ensure_utc(local).tzinfo is UTC


def test_to_local_and_format() -> None:
    dt = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
    local = to_local(dt, "Asia/Ho_Chi_Minh")
    assert local.hour == 10
    assert format_local(dt, "Asia/Ho_Chi_Minh") == "Sun 01 Nov 2026 10:00"


def test_to_local_crosses_midnight() -> None:
    dt = datetime(2026, 10, 31, 17, 30, tzinfo=UTC)
    assert to_local(dt, "Asia/Ho_Chi_Minh").date().isoformat() == "2026-11-01"


def test_from_unix() -> None:
    assert from_unix(1_793_502_000) == datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
