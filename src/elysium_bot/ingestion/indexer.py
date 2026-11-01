"""Drains ``index_state='pending'`` messages into deterministic extraction tables.

FTS rows are maintained by DB triggers; this only writes ``contract_mentions``/``message_links``.
Extraction failures mark the row ``failed`` and never block persistence (ARCHITECTURE §5.1).
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.enums import IndexState
from elysium_bot.db.models import ContractMention, Message, MessageLink
from elysium_bot.extraction.contracts import extract_contracts
from elysium_bot.extraction.links import extract_links
from elysium_bot.logging import get_logger

log = get_logger(__name__)
BATCH_SIZE = 200


def _index_one(session: AsyncSession, msg: Message) -> None:
    for c in extract_contracts(msg.text):
        session.add(
            ContractMention(
                message_id=msg.id,
                raw=c.raw,
                normalized=c.normalized,
                format=c.format,
                checksum_state=c.checksum_state,
                position=c.position,
            )
        )
    for i, link in enumerate(extract_links(msg.text, msg.entities)):
        session.add(
            MessageLink(
                message_id=msg.id, url=link.url, domain=link.domain, kind=link.kind, position=i
            )
        )


async def drain(
    sessions: async_sessionmaker[AsyncSession], limit: int = BATCH_SIZE, chat_id: int | None = None
) -> int:
    """Index up to ``limit`` pending messages (optionally of one chat). Returns rows processed."""
    async with sessions() as session:
        stmt = select(Message).where(Message.index_state == IndexState.PENDING)
        if chat_id is not None:
            stmt = stmt.where(Message.chat_id == chat_id)
        pending = list((await session.execute(stmt.order_by(Message.id).limit(limit))).scalars())
        if not pending:
            return 0
        ids = [m.id for m in pending]
        # Edited messages are re-indexed from scratch.
        await session.execute(delete(ContractMention).where(ContractMention.message_id.in_(ids)))
        await session.execute(delete(MessageLink).where(MessageLink.message_id.in_(ids)))
        for msg in pending:
            try:
                async with session.begin_nested():
                    _index_one(session, msg)
                    await session.flush()
                msg.index_state = IndexState.INDEXED
            except Exception:
                log.exception("indexer.failed", message_id=msg.id)
                msg.index_state = IndexState.FAILED
        await session.commit()
        return len(pending)


async def drain_all(
    sessions: async_sessionmaker[AsyncSession],
    chat_id: int | None = None,
    max_batches: int = 10_000,
) -> int:
    total = 0
    for _ in range(max_batches):
        processed = await drain(sessions, chat_id=chat_id)
        if not processed:
            break
        total += processed
    return total
