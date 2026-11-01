"""Large-import benchmark (marked slow; run with `pytest -m slow -s`).

Imports a synthetic 1,000,000-message export in a subprocess and checks peak RSS < 300 MB.
Override the size with ELYSIUM_BENCH_MESSAGES.
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

SCRIPT = textwrap.dedent(
    """
    import asyncio, json, resource, sys, time
    from pathlib import Path
    from elysium_bot.db.enums import ChatStatus, ChatType
    from elysium_bot.db.models import TelegramChat
    from elysium_bot.db.session import create_engine, create_session_factory
    from elysium_bot.ingestion.desktop_import import import_export

    async def main(url, path, tg_chat_id):
        engine = create_engine(url)
        sessions = create_session_factory(engine)
        async with sessions() as s:
            s.add(TelegramChat(tg_chat_id=tg_chat_id, type=ChatType.SUPERGROUP, title="Bulk",
                               status=ChatStatus.ACTIVE))
            await s.commit()
        t0 = time.perf_counter()
        result = await import_export(sessions, Path(path))
        elapsed = time.perf_counter() - t0
        await engine.dispose()
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(json.dumps({"inserted": result.run.inserted, "status": result.run.status.value,
                          "seconds": round(elapsed, 1), "peak_rss_mb": round(rss_mb, 1)}))

    asyncio.run(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
    """
)


def test_one_million_message_import_memory(tmp_path: Path) -> None:
    n = int(os.environ.get("ELYSIUM_BENCH_MESSAGES", "1000000"))
    export = fx.stream_large_export(tmp_path / "result.json", n)
    url = f"sqlite+aiosqlite:///{tmp_path / 'bench.db'}"
    migrate.upgrade(url)
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT, url, str(export), str(fx.SUPERGROUP_TG_ID)],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    stats = json.loads(proc.stdout.strip().splitlines()[-1])
    size_mb = export.stat().st_size / 1e6
    print(f"\nimport benchmark: {n:,} messages, export {size_mb:.0f} MB -> {stats}")
    assert stats["status"] == "succeeded"
    assert stats["inserted"] == n
    assert stats["peak_rss_mb"] < 300
