"""URL extraction and coarse classification."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from elysium_bot.db.enums import LinkKind

URL_RE = re.compile(r"(?:https?://|www\.|(?<![\w.@])t\.me/)[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING = ".,!?;:)]}>'\"…"

_X_DOMAINS = {"x.com", "twitter.com", "mobile.twitter.com", "fxtwitter.com", "vxtwitter.com"}
_TG_DOMAINS = {"t.me", "telegram.me", "telegram.dog"}
_DEX_DOMAINS = {"dexscreener.com", "geckoterminal.com", "dextools.io", "defined.fi", "birdeye.so"}
_EXPLORER_HINTS = ("explorer", "scan", "blockscout", "purrsec")
_DOCS_HINTS = ("gitbook.io", "notion.site", "mirror.xyz", "medium.com")


@dataclass(frozen=True)
class LinkHit:
    url: str
    domain: str | None
    display_domain: str | None
    is_idn: bool
    kind: LinkKind
    position: int


def _normalize(url: str) -> str:
    url = url.rstrip(_TRAILING)
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url


def _decode_idn(domain: str) -> str:
    try:
        return domain.encode("ascii").decode("idna")
    except (UnicodeError, ValueError):
        return domain


def classify(domain: str | None, path: str) -> LinkKind:
    if not domain:
        return LinkKind.OTHER
    bare = domain.removeprefix("www.")
    if bare in _X_DOMAINS:
        return LinkKind.X
    if bare in _TG_DOMAINS:
        return LinkKind.TELEGRAM
    if bare == "github.com" or bare.endswith(".github.io"):
        return LinkKind.GITHUB
    if bare in _DEX_DOMAINS or "dex" in bare.split(".")[0]:
        return LinkKind.DEX
    if any(h in bare for h in _EXPLORER_HINTS):
        return LinkKind.EXPLORER
    if (
        bare.startswith("docs.")
        or any(bare.endswith(h) for h in _DOCS_HINTS)
        or path.startswith("/docs")
    ):
        return LinkKind.DOCS
    return LinkKind.WEBSITE


def _hit(url: str, position: int) -> LinkHit:
    url = _normalize(url)
    parts = urlsplit(url)
    domain = (parts.hostname or "").lower() or None
    display = _decode_idn(domain) if domain else None
    return LinkHit(
        url=url,
        domain=domain,
        display_domain=display,
        is_idn=bool(domain and "xn--" in domain),
        kind=classify(domain, parts.path or ""),
        position=position,
    )


def extract_links(text: str, entities: Iterable[Mapping[str, Any]] | None) -> list[LinkHit]:
    """Links from ``text_link`` entities and URLs in text, de-duplicated by URL, in order."""
    found: list[LinkHit] = []
    for ent in entities or ():
        if ent.get("type") == "text_link" and ent.get("url"):
            found.append(_hit(str(ent["url"]), int(ent.get("offset", 0))))
    for m in URL_RE.finditer(text):
        found.append(_hit(m.group(0), m.start()))
    found.sort(key=lambda h: h.position)
    seen: set[str] = set()
    unique: list[LinkHit] = []
    for hit in found:
        if hit.url not in seen:
            seen.add(hit.url)
            unique.append(hit)
    return unique
