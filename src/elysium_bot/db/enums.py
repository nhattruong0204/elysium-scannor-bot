"""Enumerations stored as VARCHAR + CHECK (see DATABASE_DESIGN §1.5)."""

from enum import StrEnum


class ChatType(StrEnum):
    SUPERGROUP = "supergroup"
    GROUP = "group"
    CHANNEL = "channel"


class ChatStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    DEACTIVATED = "deactivated"


class AiConsentPolicy(StrEnum):
    STRICT = "strict"
    NOTICE_ONLY = "notice_only"


class AiConsent(StrEnum):
    UNKNOWN = "unknown"
    GRANTED = "granted"
    REVOKED = "revoked"


class Source(StrEnum):
    BOT_API = "bot_api"
    DESKTOP_EXPORT = "desktop_export"
    MTPROTO = "mtproto"


class IndexState(StrEnum):
    PENDING = "pending"
    INDEXED = "indexed"
    FAILED = "failed"


class LinkKind(StrEnum):
    WEBSITE = "website"
    X = "x"
    TELEGRAM = "telegram"
    DOCS = "docs"
    GITHUB = "github"
    EXPLORER = "explorer"
    DEX = "dex"
    OTHER = "other"


class AddressFormat(StrEnum):
    EVM = "evm"
    BASE58_LIKE = "base58_like"
    OTHER = "other"


class ChecksumState(StrEnum):
    VALID_CHECKSUM = "valid_checksum"
    ALL_LOWER_OR_UPPER = "all_lower_or_upper"
    INVALID_CHECKSUM = "invalid_checksum"
    NOT_APPLICABLE = "not_applicable"


class ImportStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CoverageKind(StrEnum):
    OBSERVED = "observed"
    GAP_SUSPECTED = "gap_suspected"
    GAP_CONFIRMED_EMPTY = "gap_confirmed_empty"


class JobKind(StrEnum):
    SCAN = "scan"
    IMPORT = "import"
    MTPROTO_BACKFILL = "mtproto_backfill"
    REINDEX = "reindex"
    EXPORT = "export"
    AI_ANALYSIS = "ai_analysis"
    SCHEDULED_SUMMARY = "scheduled_summary"
    DOSSIER_REBUILD = "dossier_rebuild"
    DIGEST_BUILD = "digest_build"
    RETENTION_PURGE = "retention_purge"
    BACKUP = "backup"


class JobFamily(StrEnum):
    INGEST = "ingest"
    AI = "ai"
    EXPORT = "export"
    MAINTENANCE = "maintenance"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class JobTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    STARTUP_RESUME = "startup_resume"


class ScheduleKind(StrEnum):
    COLLECTION = "collection"
    AI_SUMMARY = "ai_summary"
    DIGEST = "digest"
    BACKUP = "backup"
    RETENTION = "retention"


class Role(StrEnum):
    """Access roles. OWNER comes from env and is never stored in ``chat_roles``."""

    OWNER = "owner"
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


class StoredRole(StrEnum):
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


class AuditAction(StrEnum):
    EXPORT = "export"
    ANALYSIS = "analysis"
    ASK = "ask"
    SETTINGS_CHANGE = "settings_change"
    ROLE_CHANGE = "role_change"
    IMPORT = "import"
    ACTIVATE = "activate"
    DEACTIVATE = "deactivate"
    CONSENT_CHANGE = "consent_change"
    PURGE_USER = "purge_user"
    DELETE_CHAT_DATA = "delete_chat_data"
