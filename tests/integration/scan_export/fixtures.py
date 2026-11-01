"""A small synthetic chat with hand-countable properties (no real data)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from elysium_bot.db.enums import ChatStatus, ChatType, Source
from elysium_bot.db.models import ChatSettings, TelegramChat
from elysium_bot.ingestion import coverage, indexer
from elysium_bot.ingestion.normalizer import NormalizedMessage, SenderRef
from elysium_bot.ingestion.store import MessageStore

BASE = datetime(2026, 11, 1, 1, 0, tzinfo=UTC)  # 08:00 Asia/Ho_Chi_Minh
TG_CHAT = -1001234567890
CA_VALID = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
CA_LOWER = "0x52908400098527886e0f7030069857d2e4169ee7"
ALICE = SenderRef(
    kind="user", tg_id=101, username="alice_x", first_name="Alice", last_name="Anders"
)
BOB = SenderRef(kind="user", tg_id=202, username="bobby", first_name="Bob")
CAROL = SenderRef(kind="user", tg_id=303, first_name="Carol")
BOT = SenderRef(kind="user", tg_id=909, username="price_bot", first_name="PriceBot", is_bot=True)


def _m(
    mid: int, sender: SenderRef | None, text: str, minutes: int, **kw: object
) -> NormalizedMessage:
    return NormalizedMessage(
        source=Source.BOT_API,
        tg_chat_id=TG_CHAT,
        tg_message_id=mid,
        sender=sender,
        sent_at=BASE + timedelta(minutes=minutes),
        text=text,
        **kw,  # type: ignore[arg-type]
    )


MESSAGES = [
    _m(1, ALICE, f"New launch $NEST, CA {CA_VALID} docs https://docs.nest.example/start", 0),
    _m(2, BOB, "is $NEST legit? the nest looks empty", 5, reply_to_tg_message_id=1),
    _m(
        3,
        CAROL,
        f"same CA {CA_VALID.lower()} confirmed on https://elysium.kinetiq.xyz/address/x",
        10,
        reply_to_tg_message_id=1,
    ),
    _m(
        4,
        ALICE,
        f"also watching $HYPE and {CA_LOWER}, chart https://dexscreener.com/hyperevm/abc",
        20,
        message_thread_id=4,
    ),
    _m(5, BOB, 'gm, "quoted" text, commas, and\nnewlines', 30, message_thread_id=4),
    _m(6, None, "", 40, is_service=True, service_action="pin_message"),
    _m(7, BOT, "$NEST price 0.01", 45),
    _m(8, CAROL, "", 50, media_type="photo"),
]
EDIT = _m(
    2,
    BOB,
    "is $NEST legit? the nest looks empty (edited)",
    5,
    reply_to_tg_message_id=1,
    edited_at=BASE + timedelta(minutes=6),
)


async def seed_chat(sf: async_sessionmaker[AsyncSession]) -> int:
    async with sf() as s:
        chat = TelegramChat(
            tg_chat_id=TG_CHAT,
            type=ChatType.SUPERGROUP,
            title="Synthetic Plays",
            status=ChatStatus.ACTIVE,
        )
        s.add(chat)
        await s.flush()
        s.add(ChatSettings(chat_id=chat.id))
        store = MessageStore(s, chat)
        await store.upsert(MESSAGES)
        await store.upsert([EDIT])
        for m in MESSAGES:
            await coverage.extend_observed(s, chat.id, Source.BOT_API, m.sent_at, m.tg_message_id)
        await s.commit()
        chat_id = chat.id
    await indexer.drain_all(sf)
    return chat_id
