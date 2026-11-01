"""Command-line interface: ``elysium-bot <command>``."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from elysium_bot import __version__
from elysium_bot.config import Settings
from elysium_bot.db import migrate
from elysium_bot.db.access import get_chat_by_tg_id
from elysium_bot.db.enums import AuditAction, Role, StoredRole
from elysium_bot.db.repositories import AuditLogRepository, ChatRoleRepository
from elysium_bot.db.scope import ChatScope
from elysium_bot.db.session import create_engine, create_session_factory
from elysium_bot.logging import configure_logging
from elysium_bot.ops.health import check_health

app = typer.Typer(help="Elysium Early-Play Intelligence Bot", no_args_is_help=True)
db_app = typer.Typer(help="Database migrations", no_args_is_help=True)
mtproto_app = typer.Typer(
    help="Optional MTProto history backfill (user account)", no_args_is_help=True
)
app.add_typer(db_app, name="db")
app.add_typer(mtproto_app, name="mtproto")


def _settings() -> Settings:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    return settings


@app.command()
def version() -> None:
    """Print the application version."""
    typer.echo(__version__)


@app.command()
def run() -> None:
    """Start the bot (long polling)."""
    from elysium_bot.app import ConfigurationError
    from elysium_bot.app import run as run_bot

    try:
        run_bot(_settings())
    except ConfigurationError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(2) from exc


@db_app.command("upgrade")
def db_upgrade(revision: Annotated[str, typer.Option(help="Target revision")] = "head") -> None:
    """Apply migrations up to REVISION (default: head)."""
    migrate.upgrade(_settings().database_url, revision)
    typer.echo(f"Database upgraded to {revision}")


@db_app.command("downgrade")
def db_downgrade(revision: Annotated[str, typer.Option(help="Target revision (required)")]) -> None:
    """Revert migrations down to REVISION (use 'base' to drop everything)."""
    migrate.downgrade(_settings().database_url, revision)
    typer.echo(f"Database downgraded to {revision}")


@db_app.command("current")
def db_current() -> None:
    """Show the current migration revision."""
    migrate.current(_settings().database_url)


@app.command()
def healthcheck() -> None:
    """Exit 0 if the database is reachable and heartbeats are fresh, else 1."""
    from datetime import timedelta

    settings = Settings()

    async def check() -> tuple[bool, list[str]]:
        engine = create_engine(settings.database_url)
        try:
            result = await check_health(
                create_session_factory(engine),
                max_age=timedelta(seconds=settings.heartbeat_max_age_s),
            )
        finally:
            await engine.dispose()
        return result.ok, result.problems

    ok, problems = asyncio.run(check())
    if not ok:
        for problem in problems:
            typer.echo(f"unhealthy: {problem}", err=True)
        raise typer.Exit(1)
    typer.echo("healthy")


@app.command("grant-role")
def grant_role(
    chat: Annotated[int, typer.Option(help="Telegram chat id (e.g. -1001234567890)")],
    user: Annotated[int, typer.Option(help="Telegram user id to grant")],
    role: Annotated[StoredRole, typer.Option(help="Role to grant")],
    by: Annotated[int | None, typer.Option(help="Owner user id (default: first owner)")] = None,
) -> None:
    """Grant a chat role to a user (owners are configured via ELYSIUM_OWNER_USER_IDS)."""
    settings = _settings()
    actor = (
        by if by is not None else (settings.owner_user_ids[0] if settings.owner_user_ids else None)
    )
    if actor is None or not settings.is_owner(actor):
        typer.echo("--by must be an owner listed in ELYSIUM_OWNER_USER_IDS", err=True)
        raise typer.Exit(2)

    async def grant() -> str | None:
        engine = create_engine(settings.database_url)
        try:
            async with create_session_factory(engine)() as session:
                target = await get_chat_by_tg_id(session, chat)
                if target is None:
                    return None
                scope = ChatScope(target.id, actor, Role.OWNER)
                await ChatRoleRepository(session, scope).grant(user, role)
                await AuditLogRepository(session, scope).record(
                    AuditAction.ROLE_CHANGE,
                    {"tg_user_id": user, "role": role.value, "via": "cli"},
                )
                await session.commit()
                return target.title or str(target.tg_chat_id)
        finally:
            await engine.dispose()

    title = asyncio.run(grant())
    if title is None:
        typer.echo(f"Chat {chat} is not registered. Activate the bot in the group first.", err=True)
        raise typer.Exit(2)
    typer.echo(f"Granted {role.value} on {title!r} to user {user}")


@app.command("import-export")
def import_export_cmd(
    path: Annotated[Path, typer.Argument(help="result.json or .zip from Telegram Desktop")],
    chat: Annotated[
        int | None, typer.Option(help="Target Telegram chat id (default: auto)")
    ] = None,
    resume: Annotated[
        int | None, typer.Option(help="Resume a failed/interrupted import run")
    ] = None,
    force_chat: Annotated[
        bool, typer.Option(help="Import even if the export's chat id differs")
    ] = False,
) -> None:
    """Import a Telegram Desktop JSON export (streaming, resumable)."""
    from elysium_bot.ingestion import coverage
    from elysium_bot.ingestion.desktop_import import (
        ImportAbort,
        format_import_report,
        import_export,
    )
    from elysium_bot.scheduler.leases import JobService

    settings = _settings()

    async def go() -> str:
        engine = create_engine(settings.database_url)
        sessions = create_session_factory(engine)
        try:
            result = await import_export(
                sessions,
                path,
                tg_chat_id=chat,
                force_chat=force_chat,
                resume_run_id=resume,
                fallback_tz=settings.default_timezone,
                jobs=JobService(sessions),
            )
            async with sessions() as session:
                st = await coverage.statement(session, result.chat, tz=settings.default_timezone)
            return format_import_report(result, st.human_text, settings.default_timezone)
        finally:
            await engine.dispose()

    try:
        typer.echo(asyncio.run(go()))
    except ImportAbort as exc:
        typer.echo(f"Import not started: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command("coverage")
def coverage_cmd(
    chat: Annotated[int, typer.Option(help="Telegram chat id")],
    start: Annotated[
        datetime | None, typer.Option("--from", help="Window start (ISO, chat tz if naive)")
    ] = None,
    end: Annotated[
        datetime | None, typer.Option("--to", help="Window end (ISO, chat tz if naive)")
    ] = None,
) -> None:
    """Show stored-history coverage and gaps for a chat."""
    from zoneinfo import ZoneInfo

    from elysium_bot.ingestion import coverage
    from elysium_bot.timeutil import format_local

    settings = _settings()
    tz = settings.default_timezone

    def aware(dt: datetime | None) -> datetime | None:
        return dt.replace(tzinfo=ZoneInfo(tz)) if dt is not None and dt.tzinfo is None else dt

    async def go() -> list[str] | None:
        engine = create_engine(settings.database_url)
        try:
            async with create_session_factory(engine)() as session:
                target = await get_chat_by_tg_id(session, chat)
                if target is None:
                    return None
                st = await coverage.statement(session, target, aware(start), aware(end), tz=tz)
        finally:
            await engine.dispose()
        lines = [st.human_text, f"Messages in window: {st.messages_in_window:,}"]
        lines += [f"  source {k}: {v:,}" for k, v in sorted(st.sources_in_window.items())]
        for g in st.gaps_in_window:
            span = " → ".join(
                format_local(d, tz, "%Y-%m-%d %H:%M") for d in (g.start_at, g.end_at) if d
            )
            lines.append(f"  {g.kind}: {span} {g.note or ''}".rstrip())
        return lines

    lines = asyncio.run(go())
    if lines is None:
        typer.echo(f"Chat {chat} is not registered.", err=True)
        raise typer.Exit(2)
    typer.echo("\n".join(lines))


MTPROTO_RISKS = (
    "MTProto backfill logs in as a real Telegram user account.\n"
    "- Use only an account that is already a legitimate member of the group, with its "
    "owner's consent.\n"
    "- Telegram may limit or ban accounts for automated behaviour.\n"
    "- The saved session grants FULL account access; it is encrypted, but protect the "
    "server and key.\n"
    "- Revoke any time: `elysium-bot mtproto logout` or Telegram → Settings → Devices."
)


def _mtproto_settings() -> tuple[Settings, int, str, str]:
    settings = _settings()
    if not settings.mtproto_enabled:
        typer.echo("MTProto backfill is disabled (set ELYSIUM_MTPROTO_ENABLED=true).", err=True)
        raise typer.Exit(2)
    missing = [
        name
        for name, value in (
            ("ELYSIUM_TELEGRAM_API_ID", settings.telegram_api_id),
            ("ELYSIUM_TELEGRAM_API_HASH", settings.telegram_api_hash),
            ("ELYSIUM_MTPROTO_SESSION_KEY", settings.mtproto_session_key),
        )
        if not value
    ]
    if missing:
        typer.echo(f"Missing settings: {', '.join(missing)}", err=True)
        raise typer.Exit(2)
    assert settings.telegram_api_id and settings.telegram_api_hash and settings.mtproto_session_key
    return (
        settings,
        settings.telegram_api_id,
        settings.telegram_api_hash.get_secret_value(),
        settings.mtproto_session_key.get_secret_value(),
    )


@mtproto_app.command("login")
def mtproto_login(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the risk confirmation")] = False,
) -> None:
    """Interactive login (phone, code, 2FA in this terminal only). Saves an encrypted session."""
    settings, api_id, api_hash, key = _mtproto_settings()
    from elysium_bot.ingestion.mtproto.session_store import default_session_path, save_session

    typer.echo(MTPROTO_RISKS)
    if not yes and not typer.confirm("Continue?"):
        raise typer.Exit(1)

    async def go() -> str:  # pragma: no cover - needs a real account
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.start()  # prompts in the terminal; nothing is logged or sent to the bot
        me = await client.get_me()
        save_session(default_session_path(settings.data_dir), client.session.save(), key)
        await client.disconnect()
        return f"Logged in as {getattr(me, 'username', None) or getattr(me, 'id', '?')}"

    typer.echo(asyncio.run(go()))


@mtproto_app.command("logout")
def mtproto_logout() -> None:
    """Terminate the session server-side and delete the local encrypted session."""
    settings, api_id, api_hash, key = _mtproto_settings()
    from elysium_bot.ingestion.mtproto.backfill import open_client
    from elysium_bot.ingestion.mtproto.session_store import (
        default_session_path,
        delete_session,
        load_session,
    )

    path = default_session_path(settings.data_dir)

    async def go() -> None:  # pragma: no cover - needs a real account
        client = await open_client(api_id, api_hash, load_session(path, key))
        await client.log_out()

    try:
        asyncio.run(go())
    finally:
        delete_session(path)
    typer.echo("Logged out; local session deleted.")


@mtproto_app.command("status")
def mtproto_status() -> None:
    """Show whether an encrypted session exists and which account it belongs to."""
    settings, api_id, api_hash, key = _mtproto_settings()
    from elysium_bot.ingestion.mtproto.backfill import open_client
    from elysium_bot.ingestion.mtproto.session_store import default_session_path, load_session

    path = default_session_path(settings.data_dir)
    if not path.exists():
        typer.echo("No MTProto session. Run `elysium-bot mtproto login`.")
        raise typer.Exit(1)

    async def go() -> str:  # pragma: no cover - needs a real account
        client = await open_client(api_id, api_hash, load_session(path, key))
        me = await client.get_me()
        await client.disconnect()
        return f"Session OK: {getattr(me, 'username', None) or getattr(me, 'id', '?')}"

    typer.echo(asyncio.run(go()))


@mtproto_app.command("backfill")
def mtproto_backfill(
    chat: Annotated[int, typer.Option(help="Telegram chat id (account must already be a member)")],
    min_id: Annotated[int, typer.Option(help="Fetch messages with id > MIN_ID")] = 0,
    max_id: Annotated[int, typer.Option(help="Fetch messages with id < MAX_ID (0 = no limit)")] = 0,
    resume: Annotated[int | None, typer.Option(help="Resume an interrupted backfill run")] = None,
) -> None:
    """Read-only history backfill for an activated chat."""
    settings, api_id, api_hash, key = _mtproto_settings()
    from elysium_bot.ingestion.mtproto.backfill import BackfillAbort, backfill, open_client
    from elysium_bot.ingestion.mtproto.session_store import default_session_path, load_session
    from elysium_bot.scheduler.leases import JobService

    async def go() -> str:  # pragma: no cover - needs a real account
        engine = create_engine(settings.database_url)
        sessions = create_session_factory(engine)
        client = await open_client(
            api_id, api_hash, load_session(default_session_path(settings.data_dir), key)
        )
        try:
            async with sessions() as session:
                target = await get_chat_by_tg_id(session, chat)
            if target is None:
                raise BackfillAbort(f"chat {chat} is not registered; run /activate first")
            result = await backfill(
                sessions,
                client,
                target,
                min_id=min_id,
                max_id=max_id,
                resume_run_id=resume,
                jobs=JobService(sessions),
            )
            run = result.run
            return (
                f"Backfill run #{run.id} {run.status.value}: seen {run.seen:,}, "
                f"new {run.inserted:,}, "
                f"merged {run.updated:,}, deleted detected {result.deleted_marked:,}, "
                f"flood waits {result.flood_waits}"
            )
        finally:
            await client.disconnect()
            await engine.dispose()

    try:
        typer.echo(asyncio.run(go()))
    except BackfillAbort as exc:
        typer.echo(f"Backfill not completed: {exc}", err=True)
        raise typer.Exit(2) from exc


if __name__ == "__main__":  # pragma: no cover
    app()
