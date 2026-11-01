"""Callback-data codec: ``v1:<op>:<token>[:<arg>]``, at most 64 bytes (TELEGRAM_UX §3)."""

from __future__ import annotations

import re
from dataclasses import dataclass

VERSION = "v1"
MAX_BYTES = 64
_OP = re.compile(r"^[a-z][a-z0-9_]{0,11}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{4,12}$")
_ARG = re.compile(r"^[A-Za-z0-9_.+-]{0,40}$")


class CallbackDataError(ValueError):
    pass


@dataclass(frozen=True)
class CallbackData:
    op: str
    token: str
    arg: str | None = None


def encode(op: str, token: str, arg: str | int | None = None) -> str:
    arg_s = None if arg is None else str(arg)
    if not _OP.match(op):
        raise CallbackDataError(f"invalid op {op!r}")
    if not _TOKEN.match(token):
        raise CallbackDataError(f"invalid token {token!r}")
    if arg_s is not None and not _ARG.match(arg_s):
        raise CallbackDataError(f"invalid arg {arg_s!r}")
    data = f"{VERSION}:{op}:{token}" + (f":{arg_s}" if arg_s is not None else "")
    if len(data.encode()) > MAX_BYTES:
        raise CallbackDataError(f"callback data too long ({len(data.encode())} bytes): {data}")
    return data


def decode(data: str | None) -> CallbackData | None:
    if not data:
        return None
    parts = data.split(":", 3)
    if len(parts) < 3 or parts[0] != VERSION:
        return None
    op, token = parts[1], parts[2]
    arg = parts[3] if len(parts) == 4 else None
    if not _OP.match(op) or not _TOKEN.match(token) or (arg is not None and not _ARG.match(arg)):
        return None
    return CallbackData(op, token, arg)
