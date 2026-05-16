#!/usr/bin/env python3
"""Run governance v1 preflight checks over JSONL TicketContractV1 payloads."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_roster() -> list[object]:
    sys.path.insert(0, str(REPO_ROOT))
    from governance_engine.schema.v1 import TicketContractV1

    roster = []
    for line_no, line in enumerate(sys.stdin, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid JSON on stdin line {line_no}: {exc}") from exc
        roster.append(TicketContractV1.model_validate(payload))
    return roster


def _error_payload(error: object) -> dict[str, object]:
    if is_dataclass(error):
        return asdict(error)
    raise TypeError(f"preflight error is not a dataclass: {type(error)!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run governance v1 preflight checks for a JSONL roster.",
    )
    parser.add_argument(
        "--claimed-child-count",
        default="{}",
        help="JSON object mapping meta ticket keys to claimed child counts.",
    )
    args = parser.parse_args(argv)

    try:
        claimed_child_count = json.loads(args.claimed_child_count)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid --claimed-child-count JSON: {exc}") from exc
    if not isinstance(claimed_child_count, dict):
        raise SystemExit("--claimed-child-count must be a JSON object")

    sys.path.insert(0, str(REPO_ROOT))
    from governance_engine.preflight.orchestrator import run_all_preflight_checks
    from governance_engine.preflight.plugin_version import PluginRegistry

    roster = _load_roster()
    errors = run_all_preflight_checks(
        roster,
        PluginRegistry(),
        {str(key): int(value) for key, value in claimed_child_count.items()},
    )
    for error in errors:
        print(json.dumps(_error_payload(error), sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
