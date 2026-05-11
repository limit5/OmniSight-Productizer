"""OP-217 -- weekly RPG.W12 skill XP decay sweep entrypoint.

Called by the rpg-skill-decay.timer systemd unit (via the
``rpg_skill_decay_weekly.sh`` wrapper). Walks ``agent_skill_state``
rows whose ``last_active_at`` is older than 30 days, applies the
5%/week decay capped at ``next_level_threshold - 1``, and writes the
new XP back. Levels never demote — only ``xp`` regresses.

Usage::

    python -m scripts.rpg_skill_decay                  # run for "now" UTC
    python -m scripts.rpg_skill_decay --as-of 2026-06-01T00:00:00Z
    python -m scripts.rpg_skill_decay --dry-run        # log-only

The script prints one line per row touched and finishes with
``ROWS_DECAYED=<n>`` so the systemd log + the synthetic
AC-verification fixture can grep a single integer.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


LOG = logging.getLogger("rpg_skill_decay")


def _parse_as_of(raw: str) -> datetime:
    cleaned = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _run(now: datetime, dry_run: bool) -> int:
    from backend.agents.skill_leveling import (
        InMemorySkillStateStore,
        PostgresSkillStateStore,
        decay_idle_skills,
    )

    if dry_run:
        store = InMemorySkillStateStore()
        LOG.info("rpg_skill_decay dry_run=1 — using empty in-memory store")
        decayed = await decay_idle_skills(store, now=now)
        for row in decayed:
            print(f"agent={row.agent_id} skill={row.skill_id} xp={row.xp}")
        print(f"ROWS_DECAYED={len(decayed)}")
        return 0

    from backend.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        def _factory():  # noqa: ANN202
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def cm():
                yield conn

            return cm()

        store = PostgresSkillStateStore(_factory)
        decayed = await decay_idle_skills(store, now=now)
    for row in decayed:
        print(
            f"agent={row.agent_id} skill={row.skill_id} "
            f"xp={row.xp} level={row.level}"
        )
    print(f"ROWS_DECAYED={len(decayed)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 evaluation time (default: now UTC)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log-only (in-memory store)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    now = _parse_as_of(args.as_of) if args.as_of else datetime.now(timezone.utc)
    return asyncio.run(_run(now, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
