from __future__ import annotations

from datetime import UTC, datetime

import pytest
from telegram import (
    Chat,
    Document,
    Message,
    MessageEntity,
    MessageOriginUser,
    Poll,
    PollOption,
    User,
)

from elysium_bot.db.enums import Source
from elysium_bot.ingestion.normalizer import (
    ExportChatContext,
    ExportRecordError,
    export_chat_tg_id,
    from_export,
    from_ptb,
    utf16_len,
)

CHAT = Chat(id=-1001234567890, type="supergroup", title="Synthetic Plays")
ALICE = User(id=101, first_name="Alice", last_name="A", is_bot=False, username="alice")
T = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)


def msg(**kw):  # type: ignore[no-untyped-def]
    base = {"message_id": 10, "date": T, "chat": CHAT, "from_user": ALICE}
    return Message(**{**base, **kw})


def test_from_ptb_text_entities_reply_thread() -> None:
    parent = msg(message_id=9, text="parent")
    m = msg(
        text="see https://kinetiq.xyz $NEST",
        entities=[MessageEntity(type="url", offset=4, length=19)],
        reply_to_message=parent,
        message_thread_id=9,
        edit_date=datetime(2026, 11, 1, 3, 5, tzinfo=UTC),
    )
    n = from_ptb(m)
    assert n.source == Source.BOT_API
    assert (n.tg_chat_id, n.tg_message_id, n.message_thread_id) == (CHAT.id, 10, 9)
    assert n.reply_to_tg_message_id == 9
    assert n.sender is not None and n.sender.tg_id == 101 and n.sender.display_name == "Alice A"
    assert n.entities[0].type == "url" and n.entities[0].length == 19
    assert n.edited_at == datetime(2026, 11, 1, 3, 5, tzinfo=UTC)
    assert not n.is_service


def test_from_ptb_caption_media_and_forward() -> None:
    m = msg(
        caption="deck attached",
        caption_entities=[MessageEntity(type="bold", offset=0, length=4)],
        document=Document(
            file_id="f", file_unique_id="u", file_name="deck.pdf", mime_type="application/pdf"
        ),
        forward_origin=MessageOriginUser(date=T, sender_user=ALICE),
    )
    n = from_ptb(m)
    assert n.text == "deck attached" and n.entities[0].type == "bold"
    assert n.media_type == "document"
    assert n.media_meta == {"file_name": "deck.pdf", "mime_type": "application/pdf"}
    assert n.forward_origin is not None and n.forward_origin["sender_user_id"] == 101


def test_from_ptb_poll_service_and_sender_chat() -> None:
    poll = Poll(
        id="p",
        question="Which launch?",
        options=[PollOption("A", 0, persistent_id="a"), PollOption("B", 0, persistent_id="b")],
        total_voter_count=0,
        is_closed=False,
        is_anonymous=True,
        type="regular",
        allows_multiple_answers=False,
        allows_revoting=False,
        members_only=False,
    )
    assert from_ptb(msg(poll=poll)).media_meta == {
        "question": "Which launch?",
        "options": ["A", "B"],
    }

    service = from_ptb(msg(new_chat_title="Renamed"))
    assert service.is_service and service.service_action == "new_chat_title"

    anon = from_ptb(msg(sender_chat=CHAT, text="admin speaking"))
    assert anon.sender is not None and anon.sender.kind == "chat" and anon.sender.tg_id == CHAT.id


def test_content_hash_changes_with_text_and_media() -> None:
    a = from_ptb(msg(text="hello"))
    b = from_ptb(msg(text="hello!"))
    assert a.content_hash() != b.content_hash()
    assert a.content_hash() == from_ptb(msg(text="hello", message_id=99)).content_hash()


def test_export_chat_id_mapping() -> None:
    assert export_chat_tg_id("private_supergroup", 1234567890) == -1001234567890
    assert export_chat_tg_id("public_channel", 55) == -10055
    assert export_chat_tg_id("private_group", 4242) == -4242


def ctx() -> ExportChatContext:
    return ExportChatContext(tg_chat_id=-1001234567890)


def test_from_export_full_record_uses_unixtime() -> None:
    record = {
        "id": 48211,
        "type": "message",
        "date": "2026-11-01T10:42:00",  # exporter's local time (UTC+7) — must be ignored
        "date_unixtime": "1793504520",
        "edited": "2026-11-01T10:45:00",
        "edited_unixtime": "1793504700",
        "from": "Alice",
        "from_id": "user101",
        "reply_to_message_id": 48190,
        "text": [
            "CA ",
            {"type": "code", "text": "0xabc"},
            " 🚀 ",
            {"type": "text_link", "text": "docs", "href": "https://docs.x.io"},
        ],
        "text_entities": [
            {"type": "plain", "text": "CA "},
            {"type": "code", "text": "0xabc"},
            {"type": "plain", "text": " 🚀 "},
            {"type": "text_link", "text": "docs", "href": "https://docs.x.io"},
        ],
    }
    c = ctx()
    n = from_export(record, c, 0)
    assert n.sent_at == datetime(2026, 11, 1, 3, 42, tzinfo=UTC)
    assert n.edited_at == datetime(2026, 11, 1, 3, 45, tzinfo=UTC)
    assert n.text == "CA 0xabc 🚀 docs"
    code, link = n.entities
    assert (code.type, code.offset, code.length) == ("code", 3, 5)
    # "🚀" is 2 UTF-16 code units: "CA 0xabc " = 9, + " 🚀 " (4 units) = 12
    assert (link.type, link.offset, link.length, link.url) == (
        "text_link",
        12,
        4,
        "https://docs.x.io",
    )
    assert n.sender is not None and (n.sender.kind, n.sender.tg_id) == ("user", 101)
    assert n.reply_to_tg_message_id == 48190
    assert c.used_local_dates == 0 and c.unknown_fields == {}


def test_from_export_legacy_local_date_uses_fallback_tz() -> None:
    c = ctx()
    n = from_export(
        {"id": 1, "type": "message", "date": "2026-11-01T10:00:00", "text": "old"}, c, 0
    )
    assert n.sent_at == datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
    assert c.used_local_dates == 1


def test_from_export_service_channel_sender_media_unknown_fields() -> None:
    c = ctx()
    service = from_export(
        {
            "id": 2,
            "type": "service",
            "date_unixtime": "1793502000",
            "actor": "Kinetiq",
            "actor_id": "channel777",
            "action": "topic_created",
            "title": "Launches",
            "text": "",
        },
        c,
        1,
    )
    assert service.is_service and service.service_action == "topic_created"
    assert service.sender is not None and (service.sender.kind, service.sender.tg_id) == (
        "chat",
        -100777,
    )

    photo = from_export(
        {
            "id": 3,
            "type": "message",
            "date_unixtime": "1793502000",
            "from_id": "user5",
            "photo": "photos/1.jpg",
            "text": "chart",
            "brand_new_field": 1,
        },
        c,
        2,
    )
    assert photo.media_type == "photo"
    assert c.unknown_fields == {"brand_new_field": 1}

    poll = from_export(
        {
            "id": 4,
            "type": "message",
            "date_unixtime": "1793502000",
            "poll": {"question": "Wen?", "answers": [{"text": "soon"}, {"text": "never"}]},
            "text": "",
        },
        c,
        3,
    )
    assert poll.media_meta == {"question": "Wen?", "options": ["soon", "never"]}


def test_from_export_cross_chat_reply_is_not_local_reply() -> None:
    n = from_export(
        {
            "id": 5,
            "type": "message",
            "date_unixtime": "1793502000",
            "text": "x",
            "reply_to_message_id": 77,
            "reply_to_peer_id": "channel999",
        },
        ctx(),
        0,
    )
    assert n.reply_to_tg_message_id is None
    assert n.media_meta == {"reply_peer": "channel999"}


@pytest.mark.parametrize(
    ("record", "code"),
    [
        ({"type": "message", "date_unixtime": "1"}, "missing_id"),
        ({"id": 1, "type": "unsupported_thing", "date_unixtime": "1"}, "unsupported_type"),
        ({"id": 1, "type": "message", "text": "no date"}, "missing_date"),
    ],
)
def test_from_export_errors(record: dict[str, object], code: str) -> None:
    with pytest.raises(ExportRecordError) as exc:
        from_export(record, ctx(), 0)
    assert exc.value.code == code


def test_utf16_len() -> None:
    assert utf16_len("abc") == 3
    assert utf16_len("🚀") == 2
    assert utf16_len("Tiếng Việt") == 10
