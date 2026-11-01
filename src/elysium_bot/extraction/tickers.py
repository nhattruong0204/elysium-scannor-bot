"""Cashtags and handles. Persisted as project mentions from Phase 6; re-derivable from text."""

from __future__ import annotations

import re

TICKER_RE = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9]{1,9})\b")
HANDLE_RE = re.compile(r"(?<![\w@.])@([A-Za-z][A-Za-z0-9_]{3,31})\b")


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def extract_tickers(text: str) -> list[str]:
    return _unique([m.group(1).upper() for m in TICKER_RE.finditer(text)])


def extract_handles(text: str) -> list[str]:
    return _unique([m.group(1) for m in HANDLE_RE.finditer(text)])
