#!/usr/bin/env python3
"""[OP-775] Hotfix fast-track policy planner.

The production deploy executor is still owned by the Sprint D D9-D11
pipeline. This module pins the hotfix-specific contract in a small,
machine-readable layer so that executor can consume one decision shape:

* labels ``hotfix:vX.Y.Z`` + ``tier:S`` enter the hotfix path
* milestone wait and metric-baseline observation are skipped
* smoke + critical SLO remain required
* canary advances 25% -> 100% with no 5% stage
* operator approval remains the final production gate
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Iterable


HOTFIX_LABEL_RE = re.compile(
    r"^hotfix:v"
    r"(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)$"
)
GREEN_STATUSES = {"green", "ok", "pass", "passed", "success"}
FAST_TRACK_CANARY_STAGES = (25, 100)
FAST_TRACK_SKIP_GATES = (
    "milestone_wait",
    "metric_baseline_observation",
    "canary_5_percent",
)
FAST_TRACK_REQUIRED_GATES = ("smoke", "critical_slo", "operator_approval")
SYNTHETIC_STAGE_BUDGET_MINUTES = {
    "cherry_pick": 3,
    "smoke": 5,
    "critical_slo": 5,
    "operator_approval": 10,
    "canary_25_percent": 4,
    "canary_100_percent": 2,
}
SYNTHETIC_TOTAL_BUDGET_MINUTES = sum(SYNTHETIC_STAGE_BUDGET_MINUTES.values())


@dataclass(frozen=True)
class HotfixDecision:
    ticket_key: str
    event: str
    hotfix_version: str | None
    fast_track: bool
    required_gates: tuple[str, ...]
    skipped_gates: tuple[str, ...]
    canary_stages: tuple[int, ...]
    operator_approval_required: bool
    blocked_reasons: tuple[dict[str, str], ...]
    synthetic_budget_minutes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "ticket_key": self.ticket_key,
            "event": self.event,
            "hotfix_version": self.hotfix_version,
            "fast_track": self.fast_track,
            "required_gates": list(self.required_gates),
            "skipped_gates": list(self.skipped_gates),
            "canary_stages": list(self.canary_stages),
            "operator_approval_required": self.operator_approval_required,
            "blocked_reasons": list(self.blocked_reasons),
            "synthetic_budget_minutes": self.synthetic_budget_minutes,
        }


def extract_hotfix_version(labels: Iterable[str]) -> str | None:
    versions = []
    for label in labels:
        match = HOTFIX_LABEL_RE.match(label)
        if match:
            versions.append(label.split(":", 1)[1])
    if len(versions) > 1:
        raise ValueError(f"multiple hotfix labels found: {versions}")
    return versions[0] if versions else None


def is_green(status: str) -> bool:
    return status.strip().lower() in GREEN_STATUSES


def plan_hotfix(
    *,
    ticket_key: str,
    labels: Iterable[str],
    smoke_status: str,
    critical_slo_status: str,
    operator_approved: bool,
) -> HotfixDecision:
    labels_tuple = tuple(labels)
    blocked: list[dict[str, str]] = []
    hotfix_version = extract_hotfix_version(labels_tuple)

    if hotfix_version is None:
        blocked.append({"gate": "labels", "code": "missing_hotfix_label"})
    if "tier:S" not in labels_tuple:
        blocked.append({"gate": "labels", "code": "missing_tier_s"})
    if not is_green(smoke_status):
        blocked.append(
            {"gate": "smoke", "code": "status_not_green", "status": smoke_status}
        )
    if not is_green(critical_slo_status):
        blocked.append(
            {
                "gate": "critical_slo",
                "code": "status_not_green",
                "status": critical_slo_status,
            }
        )
    if not operator_approved:
        blocked.append({"gate": "operator_approval", "code": "approval_required"})

    fast_track = hotfix_version is not None and "tier:S" in labels_tuple
    event = (
        "hotfix_fast_track_ready"
        if fast_track and not blocked
        else "hotfix_fast_track_blocked"
    )
    return HotfixDecision(
        ticket_key=ticket_key,
        event=event,
        hotfix_version=hotfix_version,
        fast_track=fast_track,
        required_gates=FAST_TRACK_REQUIRED_GATES,
        skipped_gates=FAST_TRACK_SKIP_GATES,
        canary_stages=FAST_TRACK_CANARY_STAGES,
        operator_approval_required=True,
        blocked_reasons=tuple(blocked),
        synthetic_budget_minutes=SYNTHETIC_TOTAL_BUDGET_MINUTES,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--smoke-status", default="missing")
    parser.add_argument("--critical-slo-status", default="missing")
    parser.add_argument("--operator-approved", action="store_true")
    args = parser.parse_args(argv)

    try:
        decision = plan_hotfix(
            ticket_key=args.ticket,
            labels=args.label,
            smoke_status=args.smoke_status,
            critical_slo_status=args.critical_slo_status,
            operator_approved=args.operator_approved,
        )
    except ValueError as exc:
        print(f"hotfix_pipeline: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(decision.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if decision.event == "hotfix_fast_track_ready" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
