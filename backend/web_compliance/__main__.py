"""W5 #279 — CLI entry for the compliance bundle.

Usage:
    python3 -m backend.web_compliance --app-path=./frontend \
        [--url=https://staging.example.com] [--allowlist=readline,foo] \
        [--checklist=path/to/manual.json] [--json-out=/tmp/w5.json]

The command exits 0 iff the bundle passes and emits JSON to stdout (or
the path given by ``--json-out``). Intended for CI and simulate.sh.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backend.web_compliance.bundle import run_all


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python3 -m backend.web_compliance",
        description="Run the W5 web compliance bundle (WCAG / GDPR / SPDX).",
    )
    p.add_argument("--app-path", required=True, help="Project root to scan")
    p.add_argument("--url", default=None, help="Served URL for axe-core WCAG scan")
    p.add_argument(
        "--allowlist",
        default="",
        help="Comma-separated SPDX license allowlist (package name or name@license)",
    )
    p.add_argument(
        "--checklist",
        default=None,
        help="Path to a JSON file mapping WCAG SC id → {status, notes}",
    )
    p.add_argument(
        "--json-out",
        default=None,
        help="Write bundle JSON to this path (default: stdout)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    allowlist = [a.strip() for a in args.allowlist.split(",") if a.strip()]
    checklist_overrides = None
    if args.checklist:
        with open(args.checklist, "r", encoding="utf-8") as fh:
            checklist_overrides = json.load(fh)

    bundle = run_all(
        Path(args.app_path),
        url=args.url,
        checklist_overrides=checklist_overrides,
        spdx_allowlist=allowlist,
    )
    payload = json.dumps(bundle.to_dict(), indent=2, sort_keys=True)
    if args.json_out:
        Path(args.json_out).write_text(payload)
    else:
        sys.stdout.write(payload + "\n")
    return 0 if bundle.passed else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
