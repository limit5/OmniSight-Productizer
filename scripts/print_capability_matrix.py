#!/usr/bin/env python3
"""OP-855 — operator dashboard for the runner capability matrix.

Prints the full ``config/capability_matrix.yaml`` mapping as a flat
``ticket_type | area | tier | capabilities`` table so an operator can
audit which (ticket_type × area × tier) combinations grant which
capabilities. Also exits non-zero (and lists the offending cells) when
the YAML references a capability outside the canonical vocabulary.

Usage:
    python3 scripts/print_capability_matrix.py
    python3 scripts/print_capability_matrix.py --path config/capability_matrix.yaml
    python3 scripts/print_capability_matrix.py --resolve Story:backend:M
    python3 scripts/print_capability_matrix.py --resolve Story:backend:M --label capability:disable=gerrit_push

Stdlib-only (except the shared YAML loader inside backend.agents).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import capability_matrix  # noqa: E402


def _format_capability_set(caps: frozenset[str]) -> str:
    return ", ".join(sorted(caps)) if caps else "(none)"


def _print_table(matrix: capability_matrix.CapabilityMatrix) -> None:
    rows: list[tuple[str, str, str, str]] = []
    for ticket_type in matrix.known_ticket_types():
        for area in matrix.known_areas(ticket_type):
            for tier in matrix.known_tiers(ticket_type, area):
                caps = matrix._lookup(ticket_type, area, tier) or frozenset()
                rows.append((ticket_type, area, tier, _format_capability_set(caps)))

    if not rows:
        print("(empty matrix)")
        return

    headers = ("ticket_type", "area", "tier", "capabilities")
    widths = [
        max(len(headers[0]), max(len(r[0]) for r in rows)),
        max(len(headers[1]), max(len(r[1]) for r in rows)),
        max(len(headers[2]), max(len(r[2]) for r in rows)),
        len(headers[3]),
    ]
    fmt = f"{{:<{widths[0]}}}  {{:<{widths[1]}}}  {{:<{widths[2]}}}  {{}}"
    print(fmt.format(*headers))
    print(fmt.format("-" * widths[0], "-" * widths[1], "-" * widths[2], "-" * widths[3]))
    for row in rows:
        print(fmt.format(*row))

    print()
    print(
        f"schema_version={matrix.schema_version} "
        f"read_only_default=[{_format_capability_set(matrix.read_only_default)}] "
        f"canonical_capabilities={sorted(matrix.capabilities)}"
    )
    if matrix.source_path is not None:
        print(f"source={matrix.source_path}")


def _print_resolution(
    matrix: capability_matrix.CapabilityMatrix,
    triple: str,
    labels: list[str],
) -> int:
    parts = triple.split(":")
    if len(parts) != 3:
        print(
            f"--resolve expects 'ticket_type:area:tier' (got {triple!r})",
            file=sys.stderr,
        )
        return 2
    ticket_type, area, tier = parts
    caps = matrix.resolve(ticket_type, area, tier, labels=labels)
    print(
        f"resolved ticket_type={ticket_type!r} area={area!r} tier={tier!r} "
        f"labels={labels} → {_format_capability_set(caps)}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the runner capability matrix (OP-855)."
    )
    parser.add_argument(
        "--path",
        default=str(capability_matrix.DEFAULT_MATRIX_PATH),
        help="Path to capability_matrix.yaml (default: %(default)s).",
    )
    parser.add_argument(
        "--resolve",
        metavar="TICKET_TYPE:AREA:TIER",
        help="Print resolved capability set for a single triple instead of the full table.",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Apply a label override (repeatable). Use with --resolve.",
    )
    args = parser.parse_args(argv)

    try:
        matrix = capability_matrix.load_capability_matrix(args.path)
    except capability_matrix.CapabilityMatrixError as e:
        print(f"capability_matrix invalid: {e}", file=sys.stderr)
        return 1

    if args.resolve:
        return _print_resolution(matrix, args.resolve, args.label)

    _print_table(matrix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
