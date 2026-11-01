# Elysium Scannor Bot

AI-powered Telegram conversation-intelligence and blockchain-research assistant for groups following the Elysium ecosystem (Kinetiq's Hyperliquid-aligned L2).

This project is a research tool. It does not trade, and nothing it produces is financial advice.

- Architecture & design: [`docs/`](docs/) (start with [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md))
- Roadmap & phases: [`docs/IMPLEMENTATION_ROADMAP.md`](docs/IMPLEMENTATION_ROADMAP.md), tracker issue #10
- Rules for coding agents: [`AGENTS.md`](AGENTS.md)

> **Status:** Phase 1 (foundation). The bot starts, migrates its database, reports health, and answers `/start` with a placeholder. Message collection arrives in Phase 2.

## Quickstart — local development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras
cp .env.example .env              # then set ELYSIUM_TELEGRAM_BOT_TOKEN and ELYSIUM_OWNER_USER_IDS
export ELYSIUM_DATABASE_URL=sqlite+aiosqlite:///./data/elysium.db
uv run elysium-bot db upgrade     # create / migrate the schema
uv run elysium-bot run            # long polling; DM the bot /start
```

Checks:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
# PostgreSQL-backed tests (optional):
ELYSIUM_TEST_POSTGRES_URL=postgresql+asyncpg://user:pass@localhost:5432/db uv run pytest -q -m postgres
```

## Quickstart — Docker (VPS)

```bash
cp .env.example .env              # fill in the token and owner IDs
mkdir -p data && sudo chown 10001:10001 data   # the container runs as UID 10001
docker compose up -d --build
docker compose ps                 # STATUS should become "healthy"
docker compose logs -f bot
```

The entrypoint applies migrations (serialized with `flock`) before starting. Data lives in `./data` (SQLite database, later backups and exports). To use PostgreSQL instead, set `POSTGRES_PASSWORD`, point `ELYSIUM_DATABASE_URL` at `postgresql+asyncpg://elysium:<password>@db:5432/elysium`, and run `docker compose --profile postgres up -d`.

### Telegram bot setup

1. Create the bot with [@BotFather](https://t.me/BotFather) and copy the token into `ELYSIUM_TELEGRAM_BOT_TOKEN`.
2. **`/setprivacy` → Disable** *before* adding the bot to the research group (or make the bot an admin). Otherwise the bot only sees commands.
3. Put your numeric Telegram user ID in `ELYSIUM_OWNER_USER_IDS` (comma-separated for several owners).

## CLI

| Command | Purpose |
|---|---|
| `elysium-bot run` | Start the bot (long polling) |
| `elysium-bot db upgrade [--revision R]` | Apply migrations (default `head`) |
| `elysium-bot db downgrade --revision R` | Revert migrations (`base` drops everything) |
| `elysium-bot db current` | Show the current revision |
| `elysium-bot healthcheck` | Exit 0 if DB reachable and heartbeat fresh, else 1 (used by Docker) |
| `elysium-bot grant-role --chat ID --user ID --role admin\|analyst\|viewer` | Grant chat access (owners come from env) |
| `elysium-bot version` | Print version |

## Configuration

All settings are environment variables; see [`.env.example`](.env.example) for the full, commented list. Settings used by later phases are already listed there and marked with their phase.
