"""Cross-priority ticket scheduler — score-based dispatch, not pure FIFO.

Per ``docs/sop/jira-ticket-conventions.md`` §16. Runner pickup loop
fetches top-K candidates via JQL, scores each via :func:`score`, then
picks the highest-scoring ticket that passes pre-pickup checks
(mutex / live-state / hard-blocker).

Score formula::

    score = priority_weight
          + min(downstream_blocked × per_downstream_unblock, max_unblock_bonus)
          + (deadline_pressure_coefficient / max(days_to_fix_version, 1))
          + (log10(days_since_created + 1) × age_bonus_coefficient)
          − (mutex_in_progress penalty if same mutex_with label has In Progress sibling)

Weights live in ``config/scheduler_weights.yaml`` and are re-read on
every dispatch loop, so operators can tune without restarting runners.

Phase 0 (now → 4 weeks) logs every dispatch decision (winner +
runner-up + scores) to ``metrics/jira_ticket_lifecycle.jsonl``;
Phase 1 review tunes weights from observed routing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
WEIGHTS_PATH = REPO_ROOT / "config" / "scheduler_weights.yaml"
METRICS_PATH = REPO_ROOT / "metrics" / "jira_ticket_lifecycle.jsonl"


@dataclass(frozen=True)
class TicketSnapshot:
    """Minimal view of a JIRA ticket needed for scoring.

    Populated by the runner from JIRA REST API responses + Prerequisites
    YAML parse. Frozen so two snapshots with same fields produce
    identical scores (determinism contract).
    """

    key: str
    component: str
    fix_version: str | None
    created_at: str  # ISO 8601
    days_since_created: float
    days_to_fix_version: float | None  # None when fix_version is "backlog" / "incident"
    downstream_blocked_count: int
    mutex_labels: tuple[str, ...]
    has_mutex_in_progress_sibling: bool


@dataclass(frozen=True)
class SchedulerWeights:
    """Parsed view of config/scheduler_weights.yaml."""

    schema_version: int
    phase: int
    priority_weights: dict[str, float]  # Component → weight; "default" key for fallback
    per_downstream_unblock: float
    max_unblock_bonus: float
    deadline_pressure_coefficient: float
    age_bonus_coefficient: float
    mutex_in_progress_penalty: float


# ── Public API ─────────────────────────────────────────────────────


def load_weights(path: Path = WEIGHTS_PATH) -> SchedulerWeights:
    """Parse YAML weights config. Validates schema_version == 1."""
    import yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError(
            f"unsupported schema_version {raw.get('schema_version')!r} in {path}"
        )
    bonuses = raw.get("bonuses", {})
    penalties = raw.get("penalties", {})
    return SchedulerWeights(
        schema_version=raw["schema_version"],
        phase=int(raw.get("phase", 0)),
        priority_weights={k: float(v) for k, v in raw["priority_weights"].items()},
        per_downstream_unblock=float(bonuses.get("per_downstream_unblock", 5)),
        max_unblock_bonus=float(bonuses.get("max_unblock_bonus", 30)),
        deadline_pressure_coefficient=float(bonuses.get("deadline_pressure_coefficient", 10)),
        age_bonus_coefficient=float(bonuses.get("age_bonus_coefficient", 3)),
        mutex_in_progress_penalty=float(penalties.get("mutex_in_progress", 50)),
    )


def score(ticket: TicketSnapshot, weights: SchedulerWeights) -> float:
    """Compute scheduling score per §16 formula.

    Determinism contract: identical (ticket, weights) → identical score.
    """
    return (
        _priority_weight(ticket.component, weights)
        + _unblock_score(ticket.downstream_blocked_count, weights)
        + _deadline_pressure(ticket.days_to_fix_version, weights)
        + _age_bonus(ticket.days_since_created, weights)
        - _mutex_penalty(ticket.has_mutex_in_progress_sibling, weights)
    )


def _priority_weight(component: str, weights: SchedulerWeights) -> float:
    """Look up Component weight; fall back to weights.priority_weights['default']."""
    return weights.priority_weights.get(
        component, weights.priority_weights.get("default", 50.0)
    )


def _unblock_score(downstream_blocked: int, weights: SchedulerWeights) -> float:
    """Capped-linear unblock bonus."""
    return min(
        downstream_blocked * weights.per_downstream_unblock,
        weights.max_unblock_bonus,
    )


def _deadline_pressure(days_to_fix_version: float | None, weights: SchedulerWeights) -> float:
    """Inverse-distance to fix_version target; 0 if fix_version is backlog/incident."""
    if days_to_fix_version is None:
        return 0.0
    return weights.deadline_pressure_coefficient / max(days_to_fix_version, 1.0)


def _age_bonus(days_since_created: float, weights: SchedulerWeights) -> float:
    """log10-scaled age bonus to prevent low-priority starvation."""
    return math.log10(days_since_created + 1) * weights.age_bonus_coefficient


def _mutex_penalty(has_sibling_in_progress: bool, weights: SchedulerWeights) -> float:
    """Heavy penalty when same mutex label is held by an In Progress sibling."""
    return weights.mutex_in_progress_penalty if has_sibling_in_progress else 0.0


def dispatch(
    candidates: list[TicketSnapshot],
    weights: SchedulerWeights,
    pre_pickup_check,
) -> TicketSnapshot | None:
    """Score-sort candidates, return first that passes pre_pickup_check.

    pre_pickup_check is a callable (TicketSnapshot) -> bool. Returns
    None when all candidates fail checks (caller should idle).
    """
    import datetime as _dt
    scored = [(score(t, weights), t) for t in candidates]
    scored.sort(key=lambda x: x[0], reverse=True)
    winner: TicketSnapshot | None = None
    for s, ticket in scored:
        if pre_pickup_check(ticket):
            winner = ticket
            break
    log_dispatch_decision(winner, scored, _dt.datetime.utcnow().isoformat())
    return winner


def log_dispatch_decision(
    winner: TicketSnapshot | None,
    scored: list[tuple[float, "TicketSnapshot"]],
    timestamp: str,
) -> None:
    """Append one JSONL row to METRICS_PATH for observability."""
    import json
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": timestamp,
        "winner": winner.key if winner else None,
        "runner_up": scored[1][1].key if len(scored) > 1 else None,
        "candidate_count": len(scored),
        "top_scores": [
            {"key": t.key, "score": round(s, 2), "component": t.component}
            for s, t in scored[:5]
        ],
    }
    with METRICS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
