"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from elysium_bot.db.models.access import AuditLog, ChatRole, UiFlow
from elysium_bot.db.models.chats import ChatMember, ChatSettings, TelegramChat, TelegramUser
from elysium_bot.db.models.ingestion import (
    CoverageSegment,
    ImportErrorRecord,
    ImportRun,
    IngestionState,
)
from elysium_bot.db.models.jobs import Heartbeat, Job, Schedule
from elysium_bot.db.models.messages import (
    ContractMention,
    Message,
    MessageLink,
    MessageSource,
    MessageVersion,
)
from elysium_bot.db.models.scan import ScanCheckpoint

__all__ = [
    "AuditLog",
    "ChatMember",
    "ChatRole",
    "ChatSettings",
    "ContractMention",
    "CoverageSegment",
    "Heartbeat",
    "ImportErrorRecord",
    "ImportRun",
    "IngestionState",
    "Job",
    "Message",
    "MessageLink",
    "MessageSource",
    "MessageVersion",
    "ScanCheckpoint",
    "Schedule",
    "TelegramChat",
    "TelegramUser",
    "UiFlow",
]
