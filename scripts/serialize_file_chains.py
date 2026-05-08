#!/usr/bin/env python3
"""OP-734 operator trigger for file-touch chain serialization."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import file_coordinator, jira_dispatch  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-class", default="subscription-codex")
    parser.add_argument("--tenant", default="t-default")
    parser.add_argument("--apply", action="store_true", help="create missing blockedBy links")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    client = jira_dispatch.make_client(args.agent_class)
    graph = file_coordinator.build_file_graph(client, tenant=args.tenant)
    print(json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=True))
    if args.apply:
        created = file_coordinator.serialize_file_chains(client, graph)
        print(f"created_links={created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
