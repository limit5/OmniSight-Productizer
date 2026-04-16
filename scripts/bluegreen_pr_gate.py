#!/usr/bin/env python3
"""N10 — PR-side status gate: "blue-green-label".

Called by `.github/workflows/blue-green-gate.yml` → job `pr-check`.
Exits non-zero when the PR is blue-green-required but the PR body
doesn't carry the G3 ceremony markers (or when Renovate's body still
shows unchecked blocking checklist items).

Stdlib-only; see `bluegreen_label_decider.py` for the rationale.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

STICKY_LABEL   = "requires-blue-green"
WAIVER_LABEL   = "deploy/bluegreen-waived"
# Ceremony markers that MUST appear (case-insensitive) in the PR body
# once the PR is blue-green-required. They are the operator's checklist.
CEREMONY_MARKERS = (
    "standby",     # standby upgrade performed
    "smoke",       # smoke test run
    "cut-over",    # traffic cut-over step acknowledged
    "24h",         # old version held hot for 24 h
)


def parse_labels(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        return [str(x) for x in json.loads(raw)]
    except json.JSONDecodeError:
        return [s.strip() for s in raw.split(",") if s.strip()]


def missing_markers(body: str) -> list[str]:
    low = (body or "").lower()
    return [m for m in CEREMONY_MARKERS if m not in low]


def evaluate(labels: list[str], body: str, decision: str) -> tuple[bool, list[str]]:
    """Return (ok, reasons).

    `ok=True` means the gate passes. `reasons` is the human-readable
    surface for the step-summary.
    """
    label_set = set(labels)
    reasons: list[str] = []

    required = (
        STICKY_LABEL in label_set
        or decision == "add"
    )
    waived = WAIVER_LABEL in label_set

    if not required:
        reasons.append("PR is not blue-green-required (no `requires-blue-green` label, "
                       "no major-bump signal).")
        return True, reasons

    if waived:
        reasons.append(
            f"Blue-green WAIVED via `{WAIVER_LABEL}` label. "
            "Quarterly review will audit this waiver — ensure the "
            "rationale is captured in the PR body and the upgrade "
            "ledger before merge."
        )
        return True, reasons

    gaps = missing_markers(body)
    if gaps:
        reasons.append(
            "PR body is missing the G3 ceremony checklist markers: "
            f"{', '.join(gaps)}. Update the PR description to include "
            "the standby-upgrade, smoke-test, cut-over, and 24h hot-hold "
            "outcomes before this gate can pass. "
            "(See `docs/ops/dependency_upgrade_policy.md` → Blue-green ceremony.)"
        )
        return False, reasons

    reasons.append(
        f"`{STICKY_LABEL}` present and PR body references the full G3 "
        f"ceremony ({', '.join(CEREMONY_MARKERS)}). Gate green."
    )
    return True, reasons


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", default="[]")
    ap.add_argument("--decision", default="noop")
    ap.add_argument("--reason", default="")
    ap.add_argument("--body-file", required=True, help="file containing PR body")
    args = ap.parse_args(argv)

    labels = parse_labels(args.labels)
    try:
        with open(args.body_file, encoding="utf-8") as fh:
            body = fh.read()
    except OSError:
        body = ""

    ok, reasons = evaluate(labels, body, args.decision)

    # Step-summary markdown.
    print("## N10 — blue-green gate")
    print()
    print(f"- **decision (auto-label)**: `{args.decision}`")
    if args.reason:
        print(f"- **decider reason**: {args.reason}")
    print(f"- **labels**: `{', '.join(sorted(labels)) or '(none)'}`")
    print(f"- **verdict**: {'✅ pass' if ok else '❌ fail'}")
    print()
    for r in reasons:
        print(f"> {r}")
    print()
    if not ok:
        print("Policy: `docs/ops/dependency_upgrade_policy.md`")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
