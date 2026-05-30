#!/usr/bin/env python3
"""List recent regulated-lane contribution PR audit events."""
from __future__ import annotations

import argparse
import json
import os
from collections import deque
from pathlib import Path
from typing import Iterable


AUDIT_PATH_ENV = "OMNISIGHT_REGULATED_LANE_AUDIT_PATH"
DEFAULT_AUDIT_PATH = Path("audit/regulated_lane_events.jsonl")


def _audit_path() -> Path:
    configured = os.environ.get(AUDIT_PATH_ENV)
    if configured:
        return Path(configured)
    return DEFAULT_AUDIT_PATH


def _iter_events(path: Path, *, ticket: str | None) -> Iterable[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if ticket and event.get("ticket_key") != ticket:
                continue
            yield event


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List recent regulated-lane contribution PR audit events.",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--ticket")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.limit < 1:
        raise SystemExit("--limit must be >= 1")

    recent = deque(
        _iter_events(_audit_path(), ticket=args.ticket),
        maxlen=args.limit,
    )
    for event in recent:
        print(json.dumps(event, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
