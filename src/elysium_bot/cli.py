"""Command-line interface: ``elysium-bot <command>``."""

from __future__ import annotations

import asyncio
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
app.add_typer(db_app, name="db")


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


if __name__ == "__main__":  # pragma: no cover
    app()
