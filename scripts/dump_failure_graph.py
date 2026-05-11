#!/usr/bin/env python3
"""OP-858 (C8) — render the runner failure graph as Mermaid for incident review.

Operator usage::

    scripts/dump_failure_graph.py --since=7d --format=mermaid
    scripts/dump_failure_graph.py --since=24h --format=json --from-json fixture.json

The C2 ``runner_incidents`` Postgres table is the source-of-truth in
production; until that migration ships, the script accepts a JSON
fixture via ``--from-json`` so it remains usable for fixture-driven
incident review and CI smoke tests.

JSON shape (one object per incident, list at top level)::

    [
      {
        "incident_id": "inc-001",
        "ticket_key": "OP-100",
        "failure_class": "F-PEER-CONFLICT",
        "mutex_label": "backend/agents/scheduler.py",
        "occurred_at": "2026-05-10T12:00:00Z",
        "summary": "peer ps on backend/agents/scheduler.py"
      }
    ]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents.failure_graph import (  # noqa: E402
    FailureGraph,
    RunnerIncident,
)


def parse_since(value: str) -> datetime:
    """Accept ``Nd`` / ``Nh`` / ``Nm`` shorthand or ISO-8601."""
    if value.endswith("d"):
        return datetime.now(timezone.utc) - timedelta(days=int(value[:-1]))
    if value.endswith("h"):
        return datetime.now(timezone.utc) - timedelta(hours=int(value[:-1]))
    if value.endswith("m"):
        return datetime.now(timezone.utc) - timedelta(minutes=int(value[:-1]))
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _safe_node_id(node_id: str) -> str:
    """Mermaid node IDs allow alphanumerics + ``_``; quote anything else."""
    return "n_" + "".join(c if c.isalnum() else "_" for c in node_id)


def render_mermaid(graph: FailureGraph) -> str:
    if not graph.nodes:
        return 'flowchart LR\n  empty["(no incidents in window)"]\n'
    lines = ["flowchart LR"]
    for nid, inc in graph.nodes.items():
        # Mermaid label: escape double quotes and embed line breaks via \\n
        label = (
            f"{nid}\\n{inc.ticket_key}\\n{inc.failure_class}"
        ).replace('"', "'")
        lines.append(f'  {_safe_node_id(nid)}["{label}"]')
    for e in graph.edges:
        lines.append(
            f"  {_safe_node_id(e.src_id)} -- {e.causality_type} "
            f"--> {_safe_node_id(e.dst_id)}"
        )
    return "\n".join(lines) + "\n"


def render_json(graph: FailureGraph) -> str:
    return json.dumps(
        {
            "nodes": [
                {
                    "id": n.incident_id,
                    "ticket_key": n.ticket_key,
                    "failure_class": n.failure_class,
                    "mutex_label": n.mutex_label,
                    "occurred_at": n.occurred_at.isoformat(),
                    "summary": n.summary,
                }
                for n in graph.nodes.values()
            ],
            "edges": [
                {
                    "src": e.src_id,
                    "dst": e.dst_id,
                    "type": e.causality_type,
                }
                for e in graph.edges
            ],
        },
        indent=2,
    )


def load_incidents_from_json(path: Path) -> list[RunnerIncident]:
    raw = json.loads(path.read_text())
    out: list[RunnerIncident] = []
    for row in raw:
        out.append(
            RunnerIncident(
                incident_id=row["incident_id"],
                ticket_key=row["ticket_key"],
                failure_class=row["failure_class"],
                mutex_label=row.get("mutex_label"),
                occurred_at=datetime.fromisoformat(
                    row["occurred_at"].replace("Z", "+00:00")
                ),
                summary=row.get("summary", ""),
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default="7d")
    parser.add_argument(
        "--format", choices=("mermaid", "json"), default="mermaid"
    )
    parser.add_argument(
        "--from-json",
        type=Path,
        default=None,
        help=(
            "Load incidents from JSON file (used until C2 Postgres "
            "table ships, and for CI fixtures)."
        ),
    )
    args = parser.parse_args(argv)

    since = parse_since(args.since)
    incidents = (
        load_incidents_from_json(args.from_json) if args.from_json else []
    )
    incidents = [i for i in incidents if i.occurred_at >= since]
    graph = FailureGraph.build(incidents)

    if args.format == "mermaid":
        sys.stdout.write(render_mermaid(graph))
    else:
        sys.stdout.write(render_json(graph) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
