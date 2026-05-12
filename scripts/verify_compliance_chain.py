#!/usr/bin/env python3
"""OP-952 — verify release compliance ledger SHA256 chain."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.release_conductor import compliance_ledger  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=None, help="Optional YYYY-MM-DD filter.")
    args = parser.parse_args(argv)

    try:
        rows = compliance_ledger.list_rows(since=args.since)
        compliance_ledger.verify_chain()
    except compliance_ledger.ChainBroken as exc:
        print(f"ChainBroken: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"status": "ok", "verified_rows": len(rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
