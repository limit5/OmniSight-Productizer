"""O9 (#272) — Orchestration observability layer.

Centralises three pieces the dashboard / Prometheus exporter / SSE bus
all need to share:

  1. **Awaiting-human-+2 registry** — a process-singleton dict tracking
     every change for which the Merger Agent has cast its +2 but a human
     +2 has not yet arrived. ``merge_arbiter`` owns the lifecycle:
       * ``register_awaiting_human(...)`` on ``plus_two_voted``,
       * ``clear_awaiting_human(...)`` on ``submitted`` /
         ``human_disagreed_merger_withdrew``.
     A Prometheus gauge mirrors the count so alerts can fire on
     "double-sign pending > 24h".

  2. **Snapshot accessor** — ``snapshot_orchestration()`` returns a
     single JSON-friendly dict pulling queue depth (by priority), held
     locks (by task), merger vote totals, awaiting-human list, and worker
     pool capacity. The dashboard polls this every 10 s; the SSE bus
     pushes the same shape on every ``orchestration.queue.tick`` so the
     frontend reconciles cheaply.

  3. **SSE event publishers** — ``emit_queue_tick`` /
     ``emit_lock_acquired`` / ``emit_lock_released`` /
     ``emit_merger_voted`` / ``emit_change_awaiting_human`` route every
     event through the shared event bus with a stable
     ``orchestration.<domain>.<action>`` schema.

The module is import-side-effect-free (the registry starts empty; no
threads are spawned) and degrades gracefully if Redis / metrics /
prometheus_client aren't available.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables (env-tunable for ops)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import os

AWAITING_HUMAN_WARN_HOURS = int(
    os.environ.get("OMNISIGHT_OBS_AWAITING_HUMAN_WARN_HOURS", "24")
)
"""Threshold (in hours) above which a pending dual-sign change becomes
the source of an alert. Mirrors the Merge Arbiter's
``OMNISIGHT_ARBITER_HUMAN_WARN_HOURS`` so alert + UI agree."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Awaiting-human-+2 registry (in-process)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class AwaitingHumanEntry:
    """One change waiting for the human +2 hard gate."""

    change_id: str
    project: str
    file_path: str
    merger_confidence: float
    merger_rationale: str = ""
    review_url: str = ""
    push_sha: str = ""
    awaiting_since: float = 0.0
    jira_ticket: str = ""

    @property
    def age_seconds(self) -> float:
        if self.awaiting_since <= 0.0:
            return 0.0
        return max(0.0, time.time() - self.awaiting_since)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["age_seconds"] = round(self.age_seconds, 1)
        return d


_awaiting: dict[str, AwaitingHumanEntry] = {}
_awaiting_lock = threading.Lock()


def register_awaiting_human(
    *,
    change_id: str,
    project: str,
    file_path: str,
    merger_confidence: float,
    merger_rationale: str = "",
    review_url: str = "",
    push_sha: str = "",
    awaiting_since: float | None = None,
    jira_ticket: str = "",
) -> AwaitingHumanEntry:
    """Insert / refresh a change in the awaiting-human registry.

    Idempotent on ``change_id``: subsequent calls update fields in place
    so a re-fired Merger event doesn't bump the count or reset the wait
    clock spuriously.
    """
    if not change_id:
        raise ValueError("change_id is required")
    now = float(awaiting_since) if awaiting_since is not None else time.time()
    with _awaiting_lock:
        existing = _awaiting.get(change_id)
        if existing is not None:
            # Keep the original awaiting_since — don't reset the clock.
            existing.project = project or existing.project
            existing.file_path = file_path or existing.file_path
            existing.merger_confidence = merger_confidence
            existing.merger_rationale = merger_rationale or existing.merger_rationale
            existing.review_url = review_url or existing.review_url
            existing.push_sha = push_sha or existing.push_sha
            existing.jira_ticket = jira_ticket or existing.jira_ticket
            entry = existing
        else:
            entry = AwaitingHumanEntry(
                change_id=change_id,
                project=project,
                file_path=file_path,
                merger_confidence=merger_confidence,
                merger_rationale=merger_rationale,
                review_url=review_url,
                push_sha=push_sha,
                awaiting_since=now,
                jira_ticket=jira_ticket,
            )
            _awaiting[change_id] = entry
    _refresh_awaiting_gauge()
    return entry


def clear_awaiting_human(change_id: str) -> bool:
    """Remove ``change_id`` from the registry (e.g. on submitted / withdrawn).

    Returns True if an entry was removed, False if there wasn't one.
    """
    if not change_id:
        return False
    with _awaiting_lock:
        existed = _awaiting.pop(change_id, None) is not None
    if existed:
        _refresh_awaiting_gauge()
    return existed


def list_awaiting_human() -> list[AwaitingHumanEntry]:
    """Snapshot of the registry, sorted by oldest-waiting first."""
    with _awaiting_lock:
        items = list(_awaiting.values())
    items.sort(key=lambda e: e.awaiting_since or 0.0)
    return items


def reset_awaiting_for_tests() -> None:
    """Test helper — wipe the registry between cases."""
    with _awaiting_lock:
        _awaiting.clear()
    _refresh_awaiting_gauge()


def _refresh_awaiting_gauge() -> None:
    try:
        from backend import metrics as _m
        if hasattr(_m, "awaiting_human_pending"):
            _m.awaiting_human_pending.set(len(_awaiting))
    except Exception as exc:  # pragma: no cover — metrics shouldn't break the path
        logger.debug("awaiting_human_pending gauge update failed: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Snapshot — single-shot view for the dashboard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _snapshot_queue() -> dict[str, Any]:
    """Pull queue depth by priority + state from the live queue backend.

    Returns ``{"by_priority": {"P0": int, ...}, "by_state": {...},
    "total": int}`` so the dashboard can render two side-by-side charts
    without a second round-trip.
    """
    out: dict[str, Any] = {"by_priority": {}, "by_state": {}, "total": 0}
    try:
        from backend import queue_backend as qb
    except Exception as exc:  # pragma: no cover — shouldn't happen
        logger.debug("queue_backend import failed: %s", exc)
        return out
    try:
        for prio in qb.PriorityLevel.ordered():
            depth = qb.depth(priority=prio)
            out["by_priority"][prio.value] = depth
        for state in qb.TaskState:
            out["by_state"][state.value] = qb.depth(state=state)
        out["total"] = qb.depth()
    except Exception as exc:
        logger.debug("queue snapshot failed: %s", exc)
    return out


def _snapshot_locks() -> dict[str, Any]:
    """Group held lock entries by ``task_id`` for the dashboard list."""
    out: dict[str, Any] = {"by_task": {}, "total_paths": 0, "total_tasks": 0}
    try:
        from backend import dist_lock as dl
    except Exception as exc:  # pragma: no cover
        logger.debug("dist_lock import failed: %s", exc)
        return out
    try:
        entries = dl.all_entries()
    except Exception as exc:
        logger.debug("dist_lock all_entries failed: %s", exc)
        return out
    by_task: dict[str, dict[str, Any]] = {}
    for entry in entries:
        bucket = by_task.setdefault(entry.task_id, {
            "task_id": entry.task_id,
            "paths": [],
            "oldest_acquired_at": entry.acquired_at,
            "earliest_expiry": entry.expires_at,
        })
        bucket["paths"].append(entry.path)
        if entry.acquired_at < bucket["oldest_acquired_at"]:
            bucket["oldest_acquired_at"] = entry.acquired_at
        if entry.expires_at < bucket["earliest_expiry"]:
            bucket["earliest_expiry"] = entry.expires_at
    for bucket in by_task.values():
        bucket["paths"].sort()
    out["by_task"] = by_task
    out["total_paths"] = len(entries)
    out["total_tasks"] = len(by_task)
    return out


def _sum_metric_samples(metric: Any, **filt: str) -> float:
    """Sum the value of every Counter sample whose name ends ``_total`` and
    whose labels match ``filt``. Tolerates the no-op metric stubs."""
    try:
        for fam in metric.collect():
            for s in fam.samples:
                if not s.name.endswith("_total"):
                    continue
                if all(s.labels.get(k) == v for k, v in filt.items()):
                    return float(s.value)
    except Exception:
        pass
    return 0.0


def _gauge_value(metric: Any) -> float:
    """Best-effort value extractor for an unlabeled Gauge."""
    try:
        for fam in metric.collect():
            for s in fam.samples:
                return float(s.value)
    except Exception:
        pass
    return 0.0


def _snapshot_merger() -> dict[str, Any]:
    """Compute Merger Agent vote rates (plus_two / abstain / security_refusal).

    Numerators come straight from the Counter; the rate is computed
    against the sum so the dashboard can render it as a percentage with
    no extra division logic on the frontend.
    """
    out: dict[str, Any] = {
        "plus_two_total": 0.0,
        "abstain_total": 0.0,
        "security_refusal_total": 0.0,
        "total_votes": 0.0,
        "plus_two_rate": 0.0,
        "abstain_rate": 0.0,
        "security_refusal_rate": 0.0,
    }
    try:
        from backend import metrics as _m
    except Exception:  # pragma: no cover
        return out
    plus_two = _sum_metric_samples(_m.merger_plus_two_total)
    abstain = 0.0
    try:
        for fam in _m.merger_abstain_total.collect():
            for s in fam.samples:
                if s.name.endswith("_total"):
                    abstain += float(s.value)
    except Exception:
        pass
    security = _sum_metric_samples(_m.merger_security_refusal_total)
    total = plus_two + abstain + security
    out["plus_two_total"] = plus_two
    out["abstain_total"] = abstain
    out["security_refusal_total"] = security
    out["total_votes"] = total
    if total > 0:
        out["plus_two_rate"] = plus_two / total
        out["abstain_rate"] = abstain / total
        out["security_refusal_rate"] = security / total
    return out


def _snapshot_workers() -> dict[str, Any]:
    """Worker pool gauges: how many workers are registered + in-flight."""
    out: dict[str, Any] = {
        "active": 0.0,
        "inflight": 0.0,
        "capacity": 0.0,
        "utilisation": 0.0,
    }
    try:
        from backend import metrics as _m
    except Exception:  # pragma: no cover
        return out
    out["active"] = _gauge_value(_m.worker_active)
    out["inflight"] = _gauge_value(_m.worker_inflight)
    if hasattr(_m, "worker_pool_capacity"):
        out["capacity"] = _gauge_value(_m.worker_pool_capacity)
    if out["capacity"] > 0:
        out["utilisation"] = min(1.0, out["inflight"] / out["capacity"])
    return out


def _snapshot_awaiting_human() -> list[dict[str, Any]]:
    return [e.to_dict() for e in list_awaiting_human()]


def snapshot_orchestration() -> dict[str, Any]:
    """Single roll-up the dashboard polls every 10 s.

    Refreshes the awaiting-human gauge as a side effect so a Prometheus
    scrape that lands between events still sees the right number.
    """
    _refresh_awaiting_gauge()
    return {
        "checked_at": time.time(),
        "queue": _snapshot_queue(),
        "locks": _snapshot_locks(),
        "merger": _snapshot_merger(),
        "workers": _snapshot_workers(),
        "awaiting_human_plus_two": _snapshot_awaiting_human(),
        "awaiting_human_warn_hours": AWAITING_HUMAN_WARN_HOURS,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SSE event publishers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Stable list of orchestration.* SSE events this module emits — the
# frontend SSE registry pins this same set.
ORCHESTRATION_EVENT_TYPES: tuple[str, ...] = (
    "orchestration.queue.tick",
    "orchestration.lock.acquired",
    "orchestration.lock.released",
    "orchestration.merger.voted",
    "orchestration.change.awaiting_human_plus_two",
)


def _publish(event: str, payload: dict[str, Any]) -> None:
    """Best-effort publish via the global event bus.

    Failures are demoted to debug — observability emits must never break
    the hot path of the orchestrator / lock manager / merger.
    """
    try:
        from backend.events import bus
        bus.publish(event, dict(payload))
    except Exception as exc:  # pragma: no cover — bus shouldn't fail
        logger.debug("orchestration emit(%s) failed: %s", event, exc)


def emit_queue_tick() -> None:
    """Take a queue depth snapshot and broadcast it.

    Called by the orchestrator's housekeeping loop (and by tests
    directly). Cheap: depth() only reads in-memory dicts on the
    in-memory backend; on Redis it's a single XLEN per priority.
    """
    payload = {
        "queue": _snapshot_queue(),
        "workers": _snapshot_workers(),
    }
    _publish("orchestration.queue.tick", payload)


def emit_lock_acquired(
    *,
    task_id: str,
    paths: list[str],
    priority: int,
    wait_seconds: float,
    expires_at: float,
) -> None:
    _publish("orchestration.lock.acquired", {
        "task_id": task_id,
        "paths": list(paths),
        "priority": int(priority),
        "wait_seconds": round(float(wait_seconds), 4),
        "expires_at": float(expires_at),
    })


def emit_lock_released(
    *,
    task_id: str,
    released_count: int,
) -> None:
    _publish("orchestration.lock.released", {
        "task_id": task_id,
        "released_count": int(released_count),
    })


def emit_merger_voted(
    *,
    change_id: str,
    file_path: str,
    reason: str,
    voted_score: int,
    confidence: float,
    push_sha: str = "",
    review_url: str = "",
) -> None:
    _publish("orchestration.merger.voted", {
        "change_id": change_id,
        "file_path": file_path,
        "reason": reason,
        "voted_score": int(voted_score),
        "confidence": round(float(confidence), 4),
        "push_sha": push_sha,
        "review_url": review_url,
    })


def emit_change_awaiting_human(
    *,
    change_id: str,
    project: str,
    file_path: str,
    merger_confidence: float,
    review_url: str = "",
    push_sha: str = "",
    awaiting_since: float | None = None,
    jira_ticket: str = "",
) -> None:
    _publish("orchestration.change.awaiting_human_plus_two", {
        "change_id": change_id,
        "project": project,
        "file_path": file_path,
        "merger_confidence": round(float(merger_confidence), 4),
        "review_url": review_url,
        "push_sha": push_sha,
        "awaiting_since": float(awaiting_since) if awaiting_since else time.time(),
        "jira_ticket": jira_ticket,
    })


__all__ = [
    "AWAITING_HUMAN_WARN_HOURS",
    "AwaitingHumanEntry",
    "ORCHESTRATION_EVENT_TYPES",
    "clear_awaiting_human",
    "emit_change_awaiting_human",
    "emit_lock_acquired",
    "emit_lock_released",
    "emit_merger_voted",
    "emit_queue_tick",
    "list_awaiting_human",
    "register_awaiting_human",
    "reset_awaiting_for_tests",
    "snapshot_orchestration",
]
