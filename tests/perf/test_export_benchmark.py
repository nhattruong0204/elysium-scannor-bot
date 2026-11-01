"""Large-export benchmark (slow): 500k messages → JSON, peak RSS < 250 MB, every part ≤ 45 MB.

Seeds via the Desktop-export importer, then exports in a fresh subprocess so RSS reflects the export
only. Override the size with ELYSIUM_BENCH_EXPORT_MESSAGES.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from elysium_bot.db import migrate
from tests.fixtures import exports as fx

pytestmark = pytest.mark.slow

SEED = textwrap.dedent(
    """
    import asyncio, sys
    from pathlib import Path
    from elysium_bot.db.enums import ChatStatus, ChatType
    from elysium_bot.db.models import TelegramChat
    from elysium_bot.db.session import create_engine, create_session_factory
    from elysium_bot.ingestion.desktop_import import import_export

    async def main(url, path, tg):
        engine = create_engine(url); sf = create_session_factory(engine)
        async with sf() as s:
            s.add(TelegramChat(tg_chat_id=tg, type=ChatType.SUPERGROUP, title="Bulk", status=ChatStatus.ACTIVE))
            await s.commit()
        await import_export(sf, Path(path))
        await engine.dispose()

    asyncio.run(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
    """
)

EXPORT = textwrap.dedent(
    """
    import asyncio, json, resource, sys, time
    from datetime import UTC, datetime
    from pathlib import Path
    from elysium_bot.db.enums import Role
    from elysium_bot.db.models import TelegramChat
    from elysium_bot.db.scope import ChatScope
    from elysium_bot.db.session import create_engine, create_session_factory
    from elysium_bot.exports.service import ExportRequest, export_chat
    from elysium_bot.telegram_ui.components import Window
    from sqlalchemy import select

    async def main(url, data_dir):
        engine = create_engine(url); sf = create_session_factory(engine)
        async with sf() as s:
            chat_id = (await s.execute(select(TelegramChat.id))).scalar_one()
        window = Window(datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC), "all")
        t0 = time.perf_counter()
        result = await export_chat(sf, ChatScope(chat_id, 1, Role.OWNER), ExportRequest(window, "json"),
                                   data_dir=Path(data_dir), max_per_hour=10)
        elapsed = time.perf_counter() - t0
        await engine.dispose()
        print(json.dumps({"rows": result.rows, "seconds": round(elapsed, 1),
                          "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                          "files": [[p.name, p.stat().st_size] for p in result.files]}))

    asyncio.run(main(sys.argv[1], sys.argv[2]))
    """
)


def test_half_million_message_export_memory_and_part_sizes(tmp_path: Path) -> None:
    n = int(os.environ.get("ELYSIUM_BENCH_EXPORT_MESSAGES", "500000"))
    url = f"sqlite+aiosqlite:///{tmp_path / 'bench.db'}"
    migrate.upgrade(url)
    export = fx.stream_large_export(tmp_path / "result.json", n)
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [sys.executable, "-c", SEED, url, str(export), str(fx.SUPERGROUP_TG_ID)],
        check=True,
        cwd=root,
    )
    export.unlink()
    proc = subprocess.run(
        [sys.executable, "-c", EXPORT, url, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    )
    stats = json.loads(proc.stdout.strip().splitlines()[-1])
    print(f"\nexport benchmark: {n:,} messages -> {stats}")
    assert stats["rows"] == n
    assert stats["peak_rss_mb"] < 250
    assert all(size <= 45 * 1024 * 1024 for _, size in stats["files"])
