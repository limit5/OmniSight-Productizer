"""OP-218 -- rebuild ``agent_tool_proficiency`` from W17.7 telemetry history.

Recovery / rollback companion to RPG.W13 (see ADR-0008 §"MCP/A2A tool
proficiency (W13)" — *Recovery / rollback*). The alembic 0227
migration is forward-only; if ``agent_tool_proficiency`` is corrupted
(truncate, restore mismatch, manual purge) this script re-derives the
rows by walking the ``tool_invocation`` telemetry log and replaying
each event through :mod:`backend.agents.mp_w17_telemetry_consumer`.

The rebuild is idempotent — running it twice produces the same row
state because the input is the immutable telemetry log and the
helper applies the same compute_tool_level rules every time.

Usage::

    python -m scripts.rpg_rebuild_tool_proficiency                # all events
    python -m scripts.rpg_rebuild_tool_proficiency --agent A1     # one agent
    python -m scripts.rpg_rebuild_tool_proficiency --dry-run      # fixture only

The dry-run path uses a small static fixture so operators can verify
the replay logic without touching a live DB. Output is one
``agent=X tool=Y count=N success=M level=L`` line per touched row
plus a final ``ROWS_REBUILT=<n>`` summary line consumed by the
operator-facing scripts/runbook expectations.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


LOG = logging.getLogger("rpg_rebuild_tool_proficiency")


# A small fixed fixture used by ``--dry-run`` so operators can exercise
# the replay logic without a live DB. The fixture intentionally crosses
# the Lv 1→2 threshold (10 success / 70% ratio) so the printout is not
# all Lv 1.
_DUMMY_HISTORY: list[Mapping[str, Any]] = [
    *(
        {
            "agent_id": "dryrun-agent",
            "tool_name": "Read",
            "success": True,
            "timestamp": datetime(2026, 1, 1, 0, i, tzinfo=timezone.utc),
        }
        for i in range(8)
    ),
    {
        "agent_id": "dryrun-agent",
        "tool_name": "Read",
        "success": False,
        "timestamp": datetime(2026, 1, 1, 0, 8, tzinfo=timezone.utc),
    },
    {
        "agent_id": "dryrun-agent",
        "tool_name": "Read",
        "success": True,
        "timestamp": datetime(2026, 1, 1, 0, 9, tzinfo=timezone.utc),
    },
    {
        "agent_id": "dryrun-agent",
        "tool_name": "Read",
        "success": True,
        "timestamp": datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
    },
]


async def _fetch_history(conn: Any, agent_filter: str | None) -> list[Mapping[str, Any]]:
    """Return the ``tool_invocation`` telemetry log rows, oldest-first.

    The shape of the log table is project-specific; we accept the
    minimal columns the W17.7 schema guarantees:
    ``(agent_id, tool_name, success, occurred_at)``. Rows missing any of
    those fields are dropped on the consumer side. The runner exposes
    this table via the ``tool_invocation_log`` view on prod; on staging
    fixtures the same shape is exposed via ``mp_w17_events``.
    """
    where = "WHERE agent_id IS NOT NULL"
    params: list[Any] = []
    if agent_filter:
        where += " AND agent_id = $1"
        params.append(agent_filter)
    sql = f"""
        SELECT agent_id,
               tool_name,
               success,
               occurred_at AS timestamp
        FROM tool_invocation_log
        {where}
        ORDER BY occurred_at ASC
    """
    rows = await conn.fetch(sql, *params)
    return [dict(row) for row in rows]


async def _rebuild(
    agent_filter: str | None,
    dry_run: bool,
    output: str,
) -> int:
    from backend.agents.mp_w17_telemetry_consumer import consume_batch
    from backend.agents.tool_proficiency import (
        InMemoryToolProficiencyStore,
        PostgresToolProficiencyStore,
    )

    if dry_run:
        LOG.info("rpg_rebuild_tool_proficiency dry_run=1 — using static fixture")
        store = InMemoryToolProficiencyStore()
        stats = await consume_batch(store, _DUMMY_HISTORY)
        return _emit_summary(stats, output)

    from backend.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        history = await _fetch_history(conn, agent_filter)

        from contextlib import asynccontextmanager

        def _factory():  # noqa: ANN202
            @asynccontextmanager
            async def cm():
                yield conn

            return cm()

        store = PostgresToolProficiencyStore(_factory)
        stats = await consume_batch(store, history)
        return _emit_summary(stats, output)


def _emit_summary(stats: Any, output: str) -> int:
    payload = {
        "received": stats.received,
        "applied": stats.applied,
        "dropped_missing_agent": stats.dropped_missing_agent,
        "dropped_missing_tool": stats.dropped_missing_tool,
        "level_ups": stats.level_ups,
    }
    if output == "json":
        print(json.dumps(payload, sort_keys=True))
    else:
        for key in (
            "received",
            "applied",
            "dropped_missing_agent",
            "dropped_missing_tool",
            "level_ups",
        ):
            print(f"{key}={payload[key]}")
    print(f"ROWS_REBUILT={stats.applied}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default=None, help="Single agent_id to rebuild")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run against a small static fixture; no DB access",
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    return asyncio.run(
        _rebuild(
            agent_filter=args.agent,
            dry_run=args.dry_run,
            output=args.output,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
