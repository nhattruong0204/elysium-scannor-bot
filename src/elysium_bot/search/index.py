"""Message search (SQLite FTS5 / PostgreSQL tsvector). Always chat-scoped (SECURITY §5).

Supported query syntax: words (AND), ``"exact phrases"``, ``$TICKER`` and full ``0x…`` addresses
(exact match through ``contract_mentions``). Everything else is treated as literal text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from elysium_bot.db.models import ContractMention, Message
from elysium_bot.db.scope import ChatScope
from elysium_bot.extraction.contracts import EVM_RE
from elysium_bot.ingestion.coverage import load_utc

MARK_OPEN, MARK_CLOSE = "⁣«", "»⁣"  # invisible separators keep markers unambiguous
_TERM = re.compile(r'"([^"]+)"|(\S+)')


@dataclass(frozen=True)
class SearchHit:
    message_id: int
    tg_message_id: int
    message_thread_id: int | None
    sent_at: datetime
    snippet: str  # plain text with MARK_OPEN/MARK_CLOSE around matches


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    total: int
    mode: str  # "fts" | "contract" | "empty"


def fts5_query(user_query: str) -> str | None:
    """Turn free text into a safe FTS5 query: every term/phrase becomes a quoted string."""
    terms = []
    for phrase, word in _TERM.findall(user_query):
        term = (phrase or word).strip()
        term = term.strip('()*^:+-"') if not phrase else term
        if term:
            terms.append('"' + term.replace('"', '""') + '"')
    return " ".join(terms) or None


def contract_term(user_query: str) -> str | None:
    q = user_query.strip()
    m = EVM_RE.fullmatch(q)
    return m.group(0).lower() if m else None


def _window_sql(
    start: datetime | None, end: datetime | None, sqlite: bool
) -> tuple[str, dict[str, object]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for name, value, op in (("start", start, ">="), ("end", end, "<=")):
        if value is not None:
            clauses.append(f"m.sent_at {op} :{name}")
            params[name] = value.replace(tzinfo=None).isoformat(sep=" ") if sqlite else value
    return "".join(f" AND {c}" for c in clauses), params


async def search_messages(
    session: AsyncSession,
    scope: ChatScope,
    query: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 8,
    offset: int = 0,
) -> SearchResult:
    address = contract_term(query)
    if address:
        # IN (subquery) rather than JOIN + DISTINCT: PostgreSQL can't DISTINCT over json columns.
        mentioned = select(ContractMention.message_id).where(ContractMention.normalized == address)
        base = select(Message).where(Message.chat_id == scope.chat_id, Message.id.in_(mentioned))
        if start is not None:
            base = base.where(Message.sent_at >= start)
        if end is not None:
            base = base.where(Message.sent_at <= end)
        total = (
            await session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        found = (
            await session.execute(base.order_by(Message.sent_at.desc()).limit(limit).offset(offset))
        ).scalars()
        hits = [
            SearchHit(m.id, m.tg_message_id, m.message_thread_id, m.sent_at, _mark(m.text, address))
            for m in found
        ]
        return SearchResult(hits, int(total), "contract")

    dialect = session.get_bind().dialect.name
    sqlite = dialect == "sqlite"
    if sqlite:
        fts = fts5_query(query)
        if fts is None:
            return SearchResult([], 0, "empty")
        where, params = _window_sql(start, end, True)
        params.update(q=fts, chat=scope.chat_id, limit=limit, offset=offset)
        core = (
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            f"WHERE messages_fts MATCH :q AND m.chat_id = :chat{where}"
        )
        total = (await session.execute(text(f"SELECT count(*) {core}"), params)).scalar_one()
        rows = await session.execute(
            text(
                "SELECT m.id, m.tg_message_id, m.message_thread_id, m.sent_at, "
                f"snippet(messages_fts, 0, '{MARK_OPEN}', '{MARK_CLOSE}', '…', 16) AS snip "
                f"{core} ORDER BY bm25(messages_fts), m.sent_at DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        )
    else:
        terms = [p or w for p, w in _TERM.findall(query)]
        if not terms:
            return SearchResult([], 0, "empty")
        where, params = _window_sql(start, end, False)
        # The 'simple' tsvector config drops '$', so cashtags also need an exact substring match.
        for i, term in enumerate(t for t in terms if t.startswith("$")):
            where += f" AND strpos(lower(m.text), :cash{i}) > 0"
            params[f"cash{i}"] = term.lower()
        params.update(q=" ".join(terms), chat=scope.chat_id, limit=limit, offset=offset)
        core = (
            "FROM messages m WHERE m.text_tsv @@ plainto_tsquery('simple', :q) "
            f"AND m.chat_id = :chat{where}"
        )
        total = (await session.execute(text(f"SELECT count(*) {core}"), params)).scalar_one()
        rows = await session.execute(
            text(
                "SELECT m.id, m.tg_message_id, m.message_thread_id, m.sent_at, "
                "ts_headline('simple', m.text, plainto_tsquery('simple', :q), "
                f"'StartSel={MARK_OPEN},StopSel={MARK_CLOSE},MaxWords=24,MinWords=8') AS snip "
                f"{core} ORDER BY ts_rank_cd(m.text_tsv, plainto_tsquery('simple', :q)) DESC, "
                "m.sent_at DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        )
    hits = [
        SearchHit(
            r.id,
            r.tg_message_id,
            r.message_thread_id,
            load_utc(r.sent_at) or datetime.min,
            r.snip or "",
        )
        for r in rows
    ]
    return SearchResult(hits, int(total), "fts")


def _mark(text_value: str, needle: str, width: int = 60) -> str:
    idx = text_value.lower().find(needle.lower())
    if idx < 0:
        return text_value[: width * 2]
    start = max(0, idx - width)
    end = min(len(text_value), idx + len(needle) + width)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text_value) else ""
    return (
        prefix
        + text_value[start:idx]
        + MARK_OPEN
        + text_value[idx : idx + len(needle)]
        + MARK_CLOSE
        + text_value[idx + len(needle) : end]
        + suffix
    )
