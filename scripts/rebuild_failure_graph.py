#!/usr/bin/env python3
"""OP-858 (C8) — nightly cron entrypoint that rebuilds the failure graph.

Two modes:

* ``--mode=incremental`` (nightly @ 03:00 local): rebuild over the last
  2 days of incidents, push the diff into Cognee. Fast path.
* ``--mode=full`` (weekly @ Sunday 03:00 local): rebuild from the first
  ``runner_incidents`` row to now. Defends against Cognee drift / index
  corruption.

The cron entries live in the operator runbook
(``docs/operations/failure-graph-runbook.md``). Until C2 ships, the
script reads incidents from a JSON fixture passed via ``--from-json``
so the cron job can be wired and exercised end-to-end on a staging host.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents.failure_graph import (  # noqa: E402
    FailureGraph,
    FailureGraphCogneeUnavailable,
    push_to_cognee,
)
from scripts.dump_failure_graph import (  # noqa: E402
    load_incidents_from_json,
    render_json,
)

log = logging.getLogger("rebuild_failure_graph")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mode", choices=("incremental", "full"), default="incremental"
    )
    parser.add_argument(
        "--from-json",
        type=Path,
        default=None,
        help=(
            "Load incidents from JSON file (used until C2 Postgres "
            "table ships, and for staging dry-runs)."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional path to write a JSON snapshot of the rebuilt graph.",
    )
    args = parser.parse_args(argv)

    if args.mode == "full":
        since = datetime(1970, 1, 1, tzinfo=timezone.utc)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=2)

    incidents = (
        load_incidents_from_json(args.from_json) if args.from_json else []
    )
    incidents = [i for i in incidents if i.occurred_at >= since]
    graph = FailureGraph.build(incidents)
    log.info(
        "rebuild.built mode=%s nodes=%d edges=%d since=%s",
        args.mode,
        len(graph.nodes),
        len(graph.edges),
        since.isoformat(),
    )

    try:
        push_to_cognee(graph)
        log.info("rebuild.cognee.pushed")
    except FailureGraphCogneeUnavailable as e:
        # Per AC error catalog: Cognee unavailable degrades to the
        # direct Postgres query path. The cron exits 0 so the schedule
        # keeps firing; the next run reattempts the push.
        log.warning(
            "rebuild.cognee.unavailable degrade=direct_query reason=%s", e
        )

    if args.out:
        args.out.write_text(render_json(graph))
        log.info("rebuild.snapshot.written path=%s", args.out)

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
