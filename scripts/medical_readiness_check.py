#!/usr/bin/env python3
"""Medical ticket closure readiness gate (OP-2414).

The runner imports this module before moving any medical ticket toward a
terminal state. The CLI entry point exists for operator probes, but the control
is the runner/JIRA hook.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable, Sequence


REGULATED_MEDICAL_LABEL = "regulated:medical"
REGULATORY_CLEARED_LABEL = "regulatory-cleared"
REGULATED_LANE_LABEL = "regulated-lane"
DEFAULT_NEGATIVE_LEAK_TEST = "backend/tests/test_y10_row2_cross_tenant_leak.py"

_MEDICAL_SIGNAL_LABELS = frozenset(
    {
        REGULATED_MEDICAL_LABEL,
        REGULATED_LANE_LABEL,
        "area:medical",
        "medical",
    }
)


@dataclass(frozen=True)
class MedicalReadinessResult:
    passed: bool
    required: bool
    reasons: tuple[str, ...] = ()
    negative_leak_command: tuple[str, ...] = ()


def is_medical_ticket(labels: Iterable[str], *, summary: str = "") -> bool:
    label_set = {str(label).strip().lower() for label in labels}
    if label_set & _MEDICAL_SIGNAL_LABELS:
        return True
    return "[medical]" in summary.lower()


def negative_leak_command() -> tuple[str, ...]:
    override = os.environ.get("OMNISIGHT_MEDICAL_NEGATIVE_LEAK_COMMAND", "").strip()
    if override:
        return tuple(override.split())
    return ("backend/.venv/bin/pytest", DEFAULT_NEGATIVE_LEAK_TEST)


def check_medical_readiness(
    *,
    labels: Iterable[str],
    summary: str = "",
    run_negative_leak: bool = True,
    command: Sequence[str] | None = None,
    runner=subprocess.run,
) -> MedicalReadinessResult:
    labels_set = {str(label).strip() for label in labels}
    required = is_medical_ticket(labels_set, summary=summary)
    if not required:
        return MedicalReadinessResult(passed=True, required=False)

    reasons: list[str] = []
    if REGULATED_MEDICAL_LABEL not in labels_set:
        reasons.append(f"missing required JIRA label {REGULATED_MEDICAL_LABEL!r}")
    if REGULATORY_CLEARED_LABEL not in labels_set:
        reasons.append(f"missing human clearance label {REGULATORY_CLEARED_LABEL!r}")

    cmd = tuple(command or negative_leak_command())
    if run_negative_leak and not reasons:
        try:
            proc = runner(cmd, capture_output=True, text=True, timeout=300)
        except Exception as exc:  # noqa: BLE001 - readiness must fail closed
            reasons.append(
                f"negative-leak test command failed to run: {type(exc).__name__}: {exc}"
            )
        else:
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                suffix = f": {detail[-1]}" if detail else ""
                reasons.append(f"negative-leak test failed rc={proc.returncode}{suffix}")

    return MedicalReadinessResult(
        passed=not reasons,
        required=True,
        reasons=tuple(reasons),
        negative_leak_command=cmd,
    )


def _labels_from_args(raw: Sequence[str]) -> tuple[str, ...]:
    labels: list[str] = []
    for item in raw:
        labels.extend(part.strip() for part in item.split(",") if part.strip())
    return tuple(labels)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--summary", default="")
    parser.add_argument("--skip-negative-leak", action="store_true")
    args = parser.parse_args(argv)

    result = check_medical_readiness(
        labels=_labels_from_args(args.label),
        summary=args.summary,
        run_negative_leak=not args.skip_negative_leak,
    )
    if result.passed:
        if result.required:
            print("medical readiness gate passed")
        else:
            print("medical readiness gate not required")
        return 0
    for reason in result.reasons:
        print(reason, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
