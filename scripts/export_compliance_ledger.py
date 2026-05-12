#!/usr/bin/env python3
"""OP-952 — export release compliance ledger to PDF and signed CSV."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.release_conductor import compliance_ledger  # noqa: E402


def _parse_since(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--since must be YYYY-MM-DD") from exc
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, type=_parse_since)
    parser.add_argument(
        "--output-dir",
        default="data/compliance-ledger-exports",
        help="Directory for generated CSV/PDF artifacts.",
    )
    args = parser.parse_args(argv)

    try:
        result = compliance_ledger.export_since(
            since=args.since,
            output_dir=Path(args.output_dir),
        )
    except (compliance_ledger.ChainBroken, compliance_ledger.ExportFailed) as exc:
        print(f"ExportFailed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
