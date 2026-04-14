"""Phase 47B — Stuck-agent detection + strategy switch.

Scans agent + task state for the classic "stuck" patterns and proposes a
remediation strategy through the DecisionEngine:

    - repeat_error:     same error 3+ times in a row  → switch_model
    - long_running:     running > stuck_timeout_s     → spawn_alternate
    - blocked_forever:  task blocked > 2× timeout     → escalate

The detector is deliberately stateless — it reads state the callers
already manage (agent.thought_chain, retry counters, _running_tasks
start times) and returns action recommendations. The actual model
switch / alt-spawn happens in the caller after the DecisionEngine
proposal is resolved (auto in full_auto+, queued otherwise).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

logger = logging.getLogger(__name__)


class StuckReason(str, Enum):
    repeat_error = "repeat_error"
    long_running = "long_running"
    blocked_forever = "blocked_forever"
    repeat_retry = "repeat_retry"


class Strategy(str, Enum):
    retry_same = "retry_same"
    switch_model = "switch_model"
    spawn_alternate = "spawn_alternate"
    escalate = "escalate"
    # Phase 47-Fix Batch E (Open Agents borrow #4): pause the agent's
    # docker container instead of cancelling work. Worktree state is
    # preserved; operator (or auto-resume in higher modes) can
    # `docker unpause` to resume. Used as a lightweight first-line
    # response when the agent isn't progressing but isn't broken.
    hibernate_and_wait = "hibernate_and_wait"


# Heuristic thresholds — intentionally conservative. A caller can override
# via env / settings later; the defaults fit the existing watchdog cadence.
DEFAULT_REPEAT_ERROR_THRESHOLD = 3          # N consecutive identical errors
DEFAULT_LONG_RUNNING_S = 900                # 15 min of wall-clock "running"
DEFAULT_BLOCKED_FOREVER_S = 3600            # 1 h stuck in blocked
DEFAULT_RETRY_BURN_THRESHOLD = 5            # retry_count that's clearly not helping


@dataclass(frozen=True)
class StuckSignal:
    agent_id: str | None
    task_id: str | None
    reason: StuckReason
    suggested_strategy: Strategy
    detail: str
    source: dict[str, Any]

    def as_decision_source(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "reason": self.reason.value,
            **self.source,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Primitive checks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def has_repeat_error(
    error_history: Iterable[str] | None,
    threshold: int = DEFAULT_REPEAT_ERROR_THRESHOLD,
) -> bool:
    """True if the last *threshold* error keys are all identical.

    GraphState.error_history is already capped at 50 (audit Batch 2) and
    populated by error_check_node with normalized keys.
    """
    if not error_history:
        return False
    keys = list(error_history)
    if len(keys) < threshold:
        return False
    tail = keys[-threshold:]
    return all(k == tail[0] and k for k in tail)


def is_long_running(
    started_at: float | None,
    now: float | None = None,
    limit_s: float = DEFAULT_LONG_RUNNING_S,
) -> bool:
    if not started_at:
        return False
    return ((now or time.time()) - started_at) > limit_s


def is_blocked_forever(
    blocked_since: float | None,
    now: float | None = None,
    limit_s: float = DEFAULT_BLOCKED_FOREVER_S,
) -> bool:
    if not blocked_since:
        return False
    return ((now or time.time()) - blocked_since) > limit_s


def has_retry_burn(retry_count: int, threshold: int = DEFAULT_RETRY_BURN_THRESHOLD) -> bool:
    return (retry_count or 0) >= threshold


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Strategy picker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def pick_strategy(reason: StuckReason) -> Strategy:
    """Default policy — callers can override by reading the signal directly."""
    if reason == StuckReason.repeat_error:
        # Same error N+ times → the current model isn't getting past it.
        return Strategy.switch_model
    if reason == StuckReason.repeat_retry:
        return Strategy.switch_model
    if reason == StuckReason.long_running:
        # Not obviously failing, just slow/stuck — spawn an alt agent so the
        # user isn't blocked on this one agent's latency.
        return Strategy.spawn_alternate
    if reason == StuckReason.blocked_forever:
        # Blocked state past 1h is almost always a human-escalation case.
        return Strategy.escalate
    return Strategy.retry_same


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  High-level analysis
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def analyze_agent(
    agent_id: str,
    *,
    error_history: Iterable[str] | None = None,
    retry_count: int = 0,
    started_at: float | None = None,
    task_id: str | None = None,
    now: float | None = None,
) -> StuckSignal | None:
    """Return a StuckSignal for the first matching rule, or None."""
    if has_repeat_error(error_history):
        last = list(error_history or [])[-1]
        return StuckSignal(
            agent_id=agent_id,
            task_id=task_id,
            reason=StuckReason.repeat_error,
            suggested_strategy=pick_strategy(StuckReason.repeat_error),
            detail=f"Same error {DEFAULT_REPEAT_ERROR_THRESHOLD}× in a row: {last[:120]}",
            source={"last_error": last, "window": DEFAULT_REPEAT_ERROR_THRESHOLD},
        )
    if has_retry_burn(retry_count):
        return StuckSignal(
            agent_id=agent_id,
            task_id=task_id,
            reason=StuckReason.repeat_retry,
            suggested_strategy=pick_strategy(StuckReason.repeat_retry),
            detail=f"retry_count={retry_count} exceeded burn threshold",
            source={"retry_count": retry_count},
        )
    if is_long_running(started_at, now=now):
        elapsed = int((now or time.time()) - (started_at or 0))
        return StuckSignal(
            agent_id=agent_id,
            task_id=task_id,
            reason=StuckReason.long_running,
            suggested_strategy=pick_strategy(StuckReason.long_running),
            detail=f"running {elapsed}s (>{DEFAULT_LONG_RUNNING_S}s)",
            source={"elapsed_s": elapsed},
        )
    return None


def analyze_blocked_task(
    task_id: str,
    blocked_since: float | None,
    *,
    now: float | None = None,
) -> StuckSignal | None:
    if is_blocked_forever(blocked_since, now=now):
        elapsed = int((now or time.time()) - (blocked_since or 0))
        return StuckSignal(
            agent_id=None,
            task_id=task_id,
            reason=StuckReason.blocked_forever,
            suggested_strategy=pick_strategy(StuckReason.blocked_forever),
            detail=f"blocked for {elapsed}s — human intervention likely needed",
            source={"elapsed_s": elapsed},
        )
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DecisionEngine bridge
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def propose_remediation(signal: StuckSignal):
    """Turn a StuckSignal into a DecisionEngine proposal.

    Returns the resulting Decision (already auto-executed if the mode
    permits — see `should_auto_execute`). Severity maps:

        switch_model    → risky       (auto in full_auto+)
        spawn_alternate → risky
        escalate        → destructive (only turbo auto-executes; otherwise
                                         queued for human approval)
        retry_same      → routine
    """
    from backend import decision_engine as de

    strat_to_severity = {
        Strategy.retry_same: de.DecisionSeverity.routine,
        Strategy.switch_model: de.DecisionSeverity.risky,
        Strategy.spawn_alternate: de.DecisionSeverity.risky,
        Strategy.escalate: de.DecisionSeverity.destructive,
        # Phase 47-Fix Batch E: hibernate is non-destructive (state
        # preserved, can resume any time) → routine severity, safe to
        # auto-execute under SUPERVISED+.
        Strategy.hibernate_and_wait: de.DecisionSeverity.routine,
    }
    severity = strat_to_severity.get(
        signal.suggested_strategy, de.DecisionSeverity.routine
    )

    # Build option set with the suggested strategy marked default. Offer
    # "keep trying" as an explicit alternative so the user can override.
    options = [
        {"id": signal.suggested_strategy.value,
         "label": signal.suggested_strategy.value.replace("_", " ").title(),
         "description": signal.detail},
        {"id": Strategy.retry_same.value,
         "label": "Retry same",
         "description": "Keep current model/agent; give it another shot."},
    ]
    if signal.suggested_strategy != Strategy.escalate:
        options.append({
            "id": Strategy.escalate.value,
            "label": "Escalate to human",
            "description": "Stop and wait for human input.",
        })
    if signal.suggested_strategy != Strategy.hibernate_and_wait:
        options.append({
            "id": Strategy.hibernate_and_wait.value,
            "label": "Hibernate (docker pause)",
            "description": "Pause the agent's container; preserve state; "
                           "operator can `docker unpause` to resume.",
        })

    title = f"Stuck: {signal.reason.value} ({signal.agent_id or signal.task_id})"
    return de.propose(
        kind=f"stuck/{signal.reason.value}",
        title=title,
        detail=signal.detail,
        options=options,
        default_option_id=signal.suggested_strategy.value,
        severity=severity,
        timeout_s=120.0,
        source=signal.as_decision_source(),
    )
