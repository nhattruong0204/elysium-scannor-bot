from __future__ import annotations

import random
import string
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser

import pytest

from elysium_bot.db.enums import Role
from elysium_bot.telegram_ui.auth import OWNER_ONLY, Permission, permissions_for
from elysium_bot.telegram_ui.callbacks import CallbackDataError, decode, encode
from elysium_bot.telegram_ui.components import (
    ProgressMessage,
    RangeParseError,
    month_grid,
    paginate,
    parse_typed_range,
    preset_window,
    shift_month,
)
from elysium_bot.telegram_ui.render import esc, message_url, split_html

TZ = "Asia/Ho_Chi_Minh"


# --- codec ------------------------------------------------------------------------------------


def test_codec_round_trip_property() -> None:
    rng = random.Random(42)
    alphabet = string.ascii_letters + string.digits + "_-"
    for _ in range(2000):
        op = rng.choice(string.ascii_lowercase) + "".join(
            rng.choices(string.ascii_lowercase + "_", k=rng.randint(0, 6))
        )
        token = "".join(rng.choices(alphabet, k=8))
        arg = rng.choice(
            [
                None,
                "".join(
                    rng.choices(string.ascii_letters + string.digits + "_.+-", k=rng.randint(0, 40))
                ),
            ]
        )
        data = encode(op, token, arg)
        assert len(data.encode()) <= 64
        decoded = decode(data)
        assert decoded is not None and (decoded.op, decoded.token, decoded.arg) == (op, token, arg)


@pytest.mark.parametrize(
    ("op", "token", "arg"),
    [
        ("Nav", "abcd1234", None),
        ("nav", "bad token", None),
        ("nav", "abcd1234", "x" * 41),
        ("nav", "abcd1234", "a:b"),
    ],
)
def test_codec_rejects_invalid(op: str, token: str, arg: str | None) -> None:
    with pytest.raises(CallbackDataError):
        encode(op, token, arg)


@pytest.mark.parametrize(
    "data", [None, "", "v2:nav:abcd1234", "v1:nav", "v1:NAV:abcd1234", "v1:consent:grant:x:y:z<"]
)
def test_decode_garbage(data: str | None) -> None:
    assert decode(data) is None


# --- permissions ------------------------------------------------------------------------------


def test_permission_matrix() -> None:
    assert permissions_for(Role.OWNER) == frozenset(Permission)
    admin = permissions_for(Role.ADMIN)
    assert not (admin & OWNER_ONLY)
    assert {
        Permission.MANAGE_SETTINGS,
        Permission.MANAGE_ROLES,
        Permission.EXPORT,
        Permission.IMPORT,
    } <= admin
    analyst = permissions_for(Role.ANALYST)
    assert Permission.EXPORT not in analyst and Permission.RUN_AI in analyst
    assert Permission.EXPORT in permissions_for(Role.ANALYST, analyst_can_export=True)
    assert permissions_for(Role.VIEWER) == {Permission.VIEW_STATUS, Permission.VIEW_REPORTS}
    assert permissions_for(Role.MEMBER) == frozenset() and permissions_for(None) == frozenset()


@pytest.mark.parametrize("role", [*list(Role), None])
@pytest.mark.parametrize("permission", list(Permission))
def test_every_role_permission_pair_is_decided(role: Role | None, permission: Permission) -> None:
    allowed = permission in permissions_for(role)
    expected = {
        Role.OWNER: True,
        Role.ADMIN: permission not in OWNER_ONLY,
        Role.ANALYST: permission
        in {
            Permission.VIEW_STATUS,
            Permission.VIEW_REPORTS,
            Permission.SEARCH,
            Permission.RUN_AI,
            Permission.ASK,
        },
        Role.VIEWER: permission in {Permission.VIEW_STATUS, Permission.VIEW_REPORTS},
        Role.MEMBER: False,
        None: False,
    }[role]
    assert allowed is expected


# --- rendering --------------------------------------------------------------------------------


def test_esc_neutralizes_html() -> None:
    assert esc('<b>x</b> & "q" </a>') == '&lt;b&gt;x&lt;/b&gt; &amp; "q" &lt;/a&gt;'
    assert esc(None) == ""


def test_message_urls() -> None:
    assert message_url(-1001234567890, 48211) == "https://t.me/c/1234567890/48211"
    assert message_url(-1001234567890, 48211, thread_id=7) == "https://t.me/c/1234567890/7/48211"
    assert (
        message_url(-1001234567890, 5, username="elysium_plays") == "https://t.me/elysium_plays/5"
    )


class _Validator(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack and self.stack[-1] == tag, f"unbalanced </{tag}> in {self.stack}"
        self.stack.pop()


def _assert_valid(chunk: str) -> None:
    v = _Validator()
    v.feed(chunk)
    v.close()
    assert v.stack == [], f"unclosed tags {v.stack}"
    assert not chunk.rstrip().endswith("&") and "<" not in chunk[chunk.rfind(">") + 1 :]


def test_split_html_short_text_unchanged() -> None:
    assert split_html("<b>hi</b>") == ["<b>hi</b>"]


def test_split_html_nested_tags_and_blockquote() -> None:
    rng = random.Random(7)
    parts = []
    for i in range(400):
        name = esc(f"Project <{i}> & co")
        parts.append(
            f'<b>{i}. <a href="https://t.me/c/1/{i}">{name}</a></b> claim &amp; detail '
            + "x" * rng.randint(5, 60)
        )
    text = "<blockquote expandable>" + "\n".join(parts) + "</blockquote>\n\n" + "\n".join(parts)
    chunks = split_html(text, limit=4000)
    assert len(chunks) > 3
    for chunk in chunks:
        assert len(chunk) <= 4000
        _assert_valid(chunk)
    # Re-joined content keeps every item.
    joined = "".join(chunks)
    assert all(f"{i}. " in joined for i in range(400))


def test_split_html_long_unbroken_word() -> None:
    chunks = split_html("<i>" + "a" * 9000 + "</i>", limit=4000)
    assert all(len(c) <= 4000 for c in chunks)
    for c in chunks:
        _assert_valid(c)


# --- ranges & calendar ------------------------------------------------------------------------


def test_preset_last_24h_across_local_midnight() -> None:
    # 2026-11-01 00:30 in Asia/Ho_Chi_Minh == 2026-10-31 17:30 UTC
    now = datetime(2026, 10, 31, 17, 30, tzinfo=UTC)
    w = preset_window("24h", now)
    assert (w.start, w.end) == (
        datetime(2026, 10, 30, 17, 30, tzinfo=UTC),
        datetime(2026, 10, 31, 17, 30, tzinfo=UTC),
    )
    assert w.label == "Last 24h"


def test_month_navigation_and_grid() -> None:
    assert shift_month(2026, 12, 1) == (2027, 1)
    assert shift_month(2027, 1, -1) == (2026, 12)
    grid = month_grid(2026, 11, {date(2026, 11, 1)})
    labels = [c.label for week in grid for c in week if c.day]
    assert labels[0] == "1" and labels[1] == "·2" and len(labels) == 30
    assert all(len(week) == 7 for week in grid)
    assert grid[0][6].day == date(2026, 11, 1)  # Nov 1 2026 is a Sunday


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        (
            "2026-11-01 10:00 → 2026-11-01 14:00",
            datetime(2026, 11, 1, 3, tzinfo=UTC),
            datetime(2026, 11, 1, 7, tzinfo=UTC),
        ),
        (
            "2026-11-01 10:00 to 2026-11-01 14:00",
            datetime(2026, 11, 1, 3, tzinfo=UTC),
            datetime(2026, 11, 1, 7, tzinfo=UTC),
        ),
        (
            "2026-10-30",
            datetime(2026, 10, 29, 17, tzinfo=UTC),
            datetime(2026, 10, 30, 17, tzinfo=UTC),
        ),
        (
            "2026-10-30 → 2026-10-31",
            datetime(2026, 10, 29, 17, tzinfo=UTC),
            datetime(2026, 10, 31, 17, tzinfo=UTC),
        ),
        ("last 3h", datetime(2026, 11, 1, 4, tzinfo=UTC), datetime(2026, 11, 1, 7, tzinfo=UTC)),
        (
            "yesterday",
            datetime(2026, 10, 30, 17, tzinfo=UTC),
            datetime(2026, 10, 31, 17, tzinfo=UTC),
        ),
        ("today", datetime(2026, 10, 31, 17, tzinfo=UTC), datetime(2026, 11, 1, 7, tzinfo=UTC)),
        (
            "2026-11-01 13:00 -> now",
            datetime(2026, 11, 1, 6, tzinfo=UTC),
            datetime(2026, 11, 1, 7, tzinfo=UTC),
        ),
    ],
)
def test_typed_ranges(text: str, start: datetime, end: datetime) -> None:
    w = parse_typed_range(text, TZ, now=datetime(2026, 11, 1, 7, 0, tzinfo=UTC))
    assert (w.start, w.end) == (start, end)


@pytest.mark.parametrize(
    "text", ["next week", "2026-11-01 14:00 → 2026-11-01 10:00", "2026-13-01", "last 0h", "10:00"]
)
def test_typed_range_errors(text: str) -> None:
    with pytest.raises(RangeParseError):
        parse_typed_range(text, TZ, now=datetime(2026, 11, 1, 7, 0, tzinfo=UTC))


# --- paging & progress ------------------------------------------------------------------------


def test_paginate_bounds() -> None:
    page = paginate(list(range(20)), 5, per_page=8)
    assert (page.page, page.pages, list(page.items)) == (2, 3, [16, 17, 18, 19])
    assert page.has_prev and not page.has_next
    assert paginate([], 0).pages == 1


async def test_progress_throttle() -> None:
    sent: list[str] = []
    clock = [0.0]

    async def edit(text: str) -> None:
        sent.append(text)

    p = ProgressMessage(edit, min_interval=3.0, clock=lambda: clock[0])
    assert await p.update("10%")
    clock[0] = 1.0
    assert not await p.update("20%")
    clock[0] = 3.5
    assert await p.update("30%")
    assert not await p.update("30%")  # identical text never re-sent
    clock[0] = 4.0
    assert await p.update("done", final=True)
    assert sent == ["10%", "30%", "done"]


def test_window_state_round_trip() -> None:
    w = preset_window("7d", datetime(2026, 11, 1, tzinfo=UTC))
    from elysium_bot.telegram_ui.components import Window

    assert Window.from_state(w.to_state()) == w
    assert w.end - w.start == timedelta(days=7)
