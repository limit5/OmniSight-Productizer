"""OP-217 -- rebuild ``agent_skill_state`` from historical task outcomes.

Recovery / rollback companion to RPG.W12 (see ADR-0008 §"Skill leveling
(W12)" — *Recovery / rollback*). The alembic 0226 migration is
forward-only; if ``agent_skill_state`` is corrupted (truncate, restore
mismatch, manual purge), this script re-derives the rows by walking
``tasks.success_history`` and replaying outcomes through
:func:`backend.agents.skill_leveling.award_skill_xp`.

The rebuild is idempotent — running it twice produces the same row
state because the input is the immutable task history. Existing
``branch_choice`` values are preserved unless ``--clear-branches`` is
passed (operator escape hatch when the operator intends to re-fork).

Usage::

    python -m scripts.rpg_rebuild_skill_state                # rebuild all
    python -m scripts.rpg_rebuild_skill_state --agent A1     # one agent
    python -m scripts.rpg_rebuild_skill_state --dry-run      # print plan

Output (one line per ``(agent_id, skill_id)`` rebuilt) plus a final
``ROWS_REBUILT=<n>`` line.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


LOG = logging.getLogger("rpg_rebuild_skill_state")


def _parse_iso(raw: str | datetime | None) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc)
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    cleaned = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _coerce_outcome(value: Any) -> str:
    """Map a free-form task outcome to one of the W12 statuses."""
    if not isinstance(value, str):
        return "fail"
    clean = value.strip().lower()
    if clean in ("success", "passed", "done", "complete", "completed"):
        return "success"
    if clean in ("partial", "partial_success", "warn", "warning"):
        return "partial"
    return "fail"


async def _fetch_task_history(conn: Any, agent_filter: str | None) -> list[Mapping[str, Any]]:
    """Return ``tasks.success_history`` rows that carry an assigned agent.

    The shape of ``success_history`` is project-specific; we accept any
    column set that exposes ``(agent_id, skill_id, outcome,
    completed_at)``. Rows missing skill_id are skipped — they pre-date
    W12 and cannot be re-classified deterministically.
    """
    where = "WHERE assigned_agent_id IS NOT NULL"
    params: list[Any] = []
    if agent_filter:
        where += " AND assigned_agent_id = $1"
        params.append(agent_filter)
    sql = f"""
        SELECT assigned_agent_id AS agent_id,
               rpg_skill_id      AS skill_id,
               COALESCE(outcome, status) AS outcome,
               COALESCE(completed_at, created_at) AS completed_at
        FROM tasks
        {where}
        ORDER BY COALESCE(completed_at, created_at) ASC
    """
    rows = await conn.fetch(sql, *params)
    return [dict(row) for row in rows]


def _group_by_agent_skill(rows: Iterable[Mapping[str, Any]]):
    bucket: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        skill_id = row.get("skill_id")
        agent_id = row.get("agent_id")
        if not isinstance(skill_id, str) or not isinstance(agent_id, str):
            continue
        if not skill_id.strip() or not agent_id.strip():
            continue
        bucket.setdefault((agent_id, skill_id), []).append(row)
    return bucket


async def _rebuild(
    agent_filter: str | None,
    dry_run: bool,
    clear_branches: bool,
    as_of: datetime,
) -> int:
    from backend.agents.skill_leveling import (
        InMemorySkillStateStore,
        PostgresSkillStateStore,
    )

    if dry_run:
        rows = _DUMMY_HISTORY
        LOG.info("rpg_rebuild_skill_state dry_run=1 — using static fixture")
        store = InMemorySkillStateStore()
        return await _replay(store, rows, clear_branches, as_of)

    from backend.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await _fetch_task_history(conn, agent_filter)

        def _factory():  # noqa: ANN202
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def cm():
                yield conn

            return cm()

        store = PostgresSkillStateStore(_factory)
        return await _replay(store, rows, clear_branches, as_of)


async def _replay(
    store: Any,
    rows: Iterable[Mapping[str, Any]],
    clear_branches: bool,
    as_of: datetime,
) -> int:
    from backend.agents.skill_leveling import award_skill_xp
    rebuilt: set[tuple[str, str]] = set()
    grouped = _group_by_agent_skill(rows)
    for (agent_id, skill_id), history in grouped.items():
        existing_branch = None
        if not clear_branches:
            prior = await store.get_state(agent_id, skill_id)
            if prior is not None:
                existing_branch = prior.branch_choice
        last_when = as_of
        for row in history:
            outcome = _coerce_outcome(row.get("outcome"))
            when = _parse_iso(row.get("completed_at")) or as_of
            await award_skill_xp(
                store,
                agent_id,
                skill_id,
                delta=100,
                outcome=outcome,
                now=when,
            )
            last_when = when
        if existing_branch is not None:
            # Re-apply the locked branch after the replay (replay clears it
            # because the in-memory store starts empty). We bypass
            # ``lock_branch_choice`` here because the source-of-truth
            # column is the historical operator pick we just preserved.
            from dataclasses import replace as dc_replace

            current = await store.get_state(agent_id, skill_id)
            if current is not None and current.branch_choice != existing_branch:
                await store.upsert_state(
                    dc_replace(
                        current,
                        branch_choice=existing_branch,
                        last_active_at=last_when,
                    )
                )
        rebuilt.add((agent_id, skill_id))
        print(
            f"agent={agent_id} skill={skill_id} "
            f"events={len(history)} branch={existing_branch or '-'}"
        )
    print(f"ROWS_REBUILT={len(rebuilt)}")
    return 0


# A small fixed fixture so ``--dry-run`` exercises the replay logic
# without needing a live DB. The fixture intentionally exercises a
# Lv 1→3 transition and an anti-grind duplicate so the printout shows
# the rule applied.
_DUMMY_HISTORY: list[Mapping[str, Any]] = [
    {
        "agent_id": "dryrun-agent",
        "skill_id": "enterprise_web",
        "outcome": "success",
        "completed_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    },
    {
        "agent_id": "dryrun-agent",
        "skill_id": "enterprise_web",
        "outcome": "success",
        "completed_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    },
    {
        "agent_id": "dryrun-agent",
        "skill_id": "enterprise_web",
        "outcome": "partial",
        "completed_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
    },
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default=None, help="Single agent_id to rebuild")
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 evaluation time stamped on rebuilt rows (default: now UTC)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run against a small static fixture; no DB access",
    )
    parser.add_argument(
        "--clear-branches",
        action="store_true",
        help="Reset branch_choice during rebuild (operator escape hatch)",
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

    as_of = _parse_iso(args.as_of)
    if args.output == "json":
        LOG.info(json.dumps({"as_of": as_of.isoformat(), "agent": args.agent}))
    return asyncio.run(
        _rebuild(
            agent_filter=args.agent,
            dry_run=args.dry_run,
            clear_branches=args.clear_branches,
            as_of=as_of,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
