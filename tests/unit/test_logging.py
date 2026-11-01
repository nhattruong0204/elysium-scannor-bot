import io
import json
import logging

import pytest
import structlog

from elysium_bot.logging import configure_logging, redact, redact_processor

# Fake secrets, assembled at runtime so secret scanners don't flag the source.
BOT_TOKEN = "1234567890:" + "AAHfake" + "x" * 28
ANTHROPIC_KEY = "sk-ant-" + "api03-" + "fakekey" * 3
HEX32_VALUE = "0123456789abcdef" * 2
FERNET_KEY = "Zm9v" * 10 + "YmE" + "="


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        (f"GET https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", BOT_TOKEN),
        (f"token={BOT_TOKEN}", BOT_TOKEN),
        (f"key is {ANTHROPIC_KEY} ok", ANTHROPIC_KEY),
        (f"api_hash {HEX32_VALUE}", HEX32_VALUE),
        (f"fernet {FERNET_KEY}", FERNET_KEY),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    ],
)
def test_redact_strings(raw: str, secret: str) -> None:
    out = redact(raw)
    assert secret not in out
    assert "REDACTED" in out


def test_redact_leaves_normal_text() -> None:
    text = "imported 412880 messages from chat -1001234567890 at 10:42"
    assert redact(text) == text


def test_redact_does_not_mangle_sha256() -> None:
    sha = "3f2a" * 16
    assert redact(f"file sha256 {sha}") == f"file sha256 {sha}"


def test_processor_redacts_nested_values_and_sensitive_keys() -> None:
    event = {
        "event": "startup",
        "config": {"token": "plain-but-sensitive", "nested": [f"bot{BOT_TOKEN}"]},
        "api_key": "whatever",
        "count": 3,
    }
    out = redact_processor(None, "info", event)  # type: ignore[arg-type]
    dumped = json.dumps(out)
    assert BOT_TOKEN not in dumped
    assert "plain-but-sensitive" not in dumped
    assert "whatever" not in dumped
    assert out["count"] == 3


def test_structlog_and_stdlib_output_is_redacted() -> None:
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream)
    structlog.get_logger("test").info("calling", url=f"https://api.telegram.org/bot{BOT_TOKEN}/x")
    logging.getLogger("httpx").warning(
        "HTTP Request: POST https://api.telegram.org/bot%s/x", BOT_TOKEN
    )
    logging.getLogger("other").error("key=%s", ANTHROPIC_KEY)
    output = stream.getvalue()
    assert BOT_TOKEN not in output
    assert ANTHROPIC_KEY not in output
    lines = [json.loads(line) for line in output.strip().splitlines()]
    assert len(lines) == 3
    assert all("timestamp" in line and "level" in line for line in lines)
