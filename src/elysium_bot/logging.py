"""Structured logging (structlog) with a secret-redaction processor.

Stdlib loggers (python-telegram-bot, httpx, alembic) are routed through the same processor
chain, because httpx logs request URLs and Bot API URLs embed the bot token.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import IO, Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Telegram bot token, also when embedded in ".../bot<token>/method" URLs.
    (re.compile(r"(?<![0-9])\d{6,12}:[A-Za-z0-9_-]{30,}"), "[REDACTED_BOT_TOKEN]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), "[REDACTED_ANTHROPIC_KEY]"),
    (
        re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(bearer\s+)?[^\s\"',]+"),
        r"\1[REDACTED]",
    ),
    # Fernet keys: 32 bytes url-safe base64 -> 43 chars + "=".
    (re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}="), "[REDACTED_KEY]"),
    # Telegram api_hash: exactly 32 hex chars (not part of a longer hex run such as sha256).
    (re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])"), "[REDACTED_HASH]"),
    # International phone numbers (MTProto login).
    (re.compile(r"(?<![\w+])\+\d{8,15}\b"), "[REDACTED_PHONE]"),
]

_SENSITIVE_KEYS = frozenset(
    {
        "token",
        "bot_token",
        "api_key",
        "api_hash",
        "password",
        "authorization",
        "session_key",
        "secret",
        "phone",
        "code",
    }
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {
            k: "[REDACTED]" if str(k).lower() in _SENSITIVE_KEYS else _redact_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_redact_value(v) for v in value)
    return value


def redact_processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    result: MutableMapping[str, Any] = {}
    for key, value in event_dict.items():
        if key.lower() in _SENSITIVE_KEYS:
            result[key] = "[REDACTED]"
        else:
            result[key] = _redact_value(value)
    return dict(result)


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.ExtraAdder(),
    ]


def configure_logging(
    level: str = "INFO", fmt: str = "json", stream: IO[str] | None = None
) -> None:
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    shared = _shared_processors()

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            redact_processor,
            renderer,
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # httpx logs every Bot API request at INFO; keep it quiet (and redacted if it does log).
    logging.getLogger("httpx").setLevel(max(logging.WARNING, root.level))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
