"""Phase 63-B — IIS Mitigation Layer.

Reads alerts from Phase 63-A's IntelligenceWindow and converts them
into Decision Engine proposals across three escalation tiers:

  L1 calibrate  (severity=routine,      kind=intelligence/calibrate)
                In-place: context reset, few-shot injection, COT
                enforcement. Auto-resolvable in BALANCED+ profiles.
  L2 route      (severity=risky,        kind=intelligence/route)
                Switch model via the existing fallback chain. Operator
                approve under default profile.
  L3 contain    (severity=destructive,  kind=intelligence/contain)
                Halt the agent, page on-call, optionally file Jira.
                Admin approve.

The actual *application* of an L1/L2/L3 strategy (e.g. the code that
clears chat history, switches provider, or halts the agent) lives at
the consumer side just like the stuck/* family — this module only
files the proposal. Consumers subscribe to `decision_resolved` SSE
or poll `de.list_history` to act on operator approvals.

Dedup: per-(agent_id, mitigation_level) we keep the most recent open
proposal id; we do NOT file a second proposal at the same level while
one is pending. This mirrors `routers/invoke.py::_open_proposals`.

Tier-1 COT length is profile-aware (decision locked in HANDOFF):
  cost_saver=0  BALANCED=200  QUALITY=500  sprint=100 (between)
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Literal

logger = logging.getLogger(__name__)


MitigationLevel = Literal["calibrate", "route", "contain"]
_KIND = {
    "calibrate": "intelligence/calibrate",
    "route": "intelligence/route",
    "contain": "intelligence/contain",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Profile-aware COT character budget
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Locked decision: cost_saver disables COT padding, QUALITY uses the
# fullest budget, BALANCED/sprint sit in-between. Real strategy reads
# come at proposal time so a profile flip is honoured immediately.
_COT_BY_STRATEGY: dict[str, int] = {
    "cost_saver": 0,
    "balanced": 200,
    "sprint": 100,
    "quality": 500,
}


def cot_chars_for_current_profile() -> int:
    try:
        from backend import budget_strategy as _bs
        s = _bs.get_strategy()
        return _COT_BY_STRATEGY.get(s.value, 200)
    except Exception:
        return 200  # safe default if budget_strategy missing


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-agent escalation state + dedup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_state_lock = threading.Lock()
# (agent_id, level) -> open decision_id
_open_proposals: dict[tuple[str, MitigationLevel], str] = {}
# agent_id -> {"l1": int, "l2": int, "l3": int} since reset
_escalation_count: dict[str, dict[str, int]] = {}


def _reset_for_tests() -> None:
    with _state_lock:
        _open_proposals.clear()
        _escalation_count.clear()


def _bump(agent_id: str, level: MitigationLevel) -> int:
    """Increment per-agent counter, return new value."""
    counts = _escalation_count.setdefault(
        agent_id, {"calibrate": 0, "route": 0, "contain": 0},
    )
    counts[level] += 1
    return counts[level]


def get_state_snapshot() -> dict:
    """Read-only snapshot for debugging / /healthz extension."""
    with _state_lock:
        return {
            "open_proposals": dict(_open_proposals),
            "escalation_count": {k: dict(v) for k, v in _escalation_count.items()},
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Alert → mitigation level mapping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def map_alerts_to_level(alerts: list[tuple[str, str, str]]) -> MitigationLevel | None:
    """Reduce a list of (level, dim, reason) alerts to a single
    mitigation level. None means no action needed.

    Rules (most-severe wins):
      * any "critical" alert                → "route"
      * any "warning"                       → "calibrate"
      * empty                               → None

    "contain" is NEVER produced from this single call — escalation to
    L3 is only allowed in `propose_for_agent` when an L2 proposal is
    already open (i.e. operator approved a switch_model and we're
    STILL drifting).
    """
    if not alerts:
        return None
    if any(lvl == "critical" for lvl, _, _ in alerts):
        return "route"
    if any(lvl == "warning" for lvl, _, _ in alerts):
        return "calibrate"
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Option builders per tier
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _options_calibrate() -> list[dict]:
    cot = cot_chars_for_current_profile()
    return [
        {"id": "calibrate", "label": "Calibrate in place",
         "description": (
             f"Clear chat history except HANDOFF summary; inject 1 "
             f"matching skill from configs/skills/ as few-shot; require "
             f"{cot}-char chain-of-thought before next code emission."
         )},
        {"id": "skip", "label": "Skip — accept current behaviour",
         "description": "No prompt changes; the alert decays as the "
                        "window slides."},
    ]


def _options_route(current_model: str | None) -> list[dict]:
    return [
        {"id": "switch_model", "label": "Switch to next fallback",
         "description": (
             f"Re-issue the next prompt against the next provider in "
             f"OMNISIGHT_LLM_FALLBACK_CHAIN. Current: {current_model or 'unknown'}."
         )},
        {"id": "calibrate", "label": "Try L1 calibration first",
         "description": "Stay on the current model but clear context "
                        "and retry."},
        {"id": "abort", "label": "Abort the agent's current task",
         "description": "Mark the task as blocked; operator picks it up."},
    ]


def _options_contain() -> list[dict]:
    return [
        {"id": "halt", "label": "Halt + page",
         "description": "Set agent.status=halted, send critical "
                        "Notification, optionally file Jira."},
        {"id": "switch_model", "label": "One more model swap",
         "description": "Try one more provider before halting."},
    ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def propose_for_agent(agent_id: str, *,
                            current_model: str | None = None) -> str | None:
    """Walk the agent's IIS window, decide if action is needed, and
    file ONE Decision Engine proposal at the appropriate tier.

    Returns the new decision id, or None if (a) nothing alerts, (b)
    a proposal at this tier is already open (dedup).

    Escalation: if an L2 (route) proposal is already open AND a
    *new* critical alert fires, escalate to L3 (contain) — meaning
    the previous switch_model didn't fix it.
    """
    from backend import intelligence as _iis
    w = _iis.get_window(agent_id)
    alerts = w.alerts()
    base_level = map_alerts_to_level(alerts)
    if base_level is None:
        return None

    # Escalation check — only for critical alerts.
    if base_level == "route" and (agent_id, "route") in _open_proposals:
        base_level = "contain"

    with _state_lock:
        if (agent_id, base_level) in _open_proposals:
            logger.debug(
                "[IIS-MIT] dedup: %s/%s proposal already open",
                agent_id, base_level,
            )
            return None

    return await _file_proposal(agent_id, base_level, alerts, current_model)


async def _file_proposal(agent_id: str, level: MitigationLevel,
                         alerts: list[tuple[str, str, str]],
                         current_model: str | None) -> str:
    """Build options, file the Decision Engine proposal, register dedup,
    bump escalation count, optionally send notification / Jira (L3)."""
    from backend import decision_engine as de

    if level == "calibrate":
        options = _options_calibrate()
        default = "calibrate"
        severity = de.DecisionSeverity.routine
        timeout_s = 600.0
    elif level == "route":
        options = _options_route(current_model)
        default = "calibrate"  # safer default than switch_model
        severity = de.DecisionSeverity.risky
        timeout_s = 900.0
    else:  # contain
        options = _options_contain()
        default = "halt"
        severity = de.DecisionSeverity.destructive
        timeout_s = 1800.0

    title = f"IIS {level}: {agent_id}"
    detail_lines = [
        f"agent_id={agent_id}",
        f"current_model={current_model or 'unknown'}",
        f"profile_cot_chars={cot_chars_for_current_profile()}",
        "alerts:",
    ]
    for lvl, dim, reason in alerts:
        detail_lines.append(f"  - [{lvl}] {dim}: {reason}")

    dec = de.propose(
        kind=_KIND[level],
        title=title,
        detail="\n".join(detail_lines),
        options=options,
        default_option_id=default,
        severity=severity,
        timeout_s=timeout_s,
        source={
            "subsystem": "iis",
            "agent_id": agent_id,
            "level": level,
            "alerts": [{"level": l, "dim": d, "reason": r} for l, d, r in alerts],
            "current_model": current_model or "",
        },
    )

    with _state_lock:
        _open_proposals[(agent_id, level)] = dec.id
    _bump(agent_id, level)

    # L3 always pages; L2 sends a warning-level notification; L1 silent.
    try:
        if level == "contain":
            await _notify_contain(agent_id, alerts)
            await _maybe_jira(agent_id, alerts, dec.id)
        elif level == "route":
            from backend import notifications as _n
            await _n.notify(
                level="warning",
                title=f"IIS L2 route proposal for {agent_id}",
                message=f"Decision {dec.id}; awaiting operator approval.",
                source=f"iis:{agent_id}",
            )
    except Exception as exc:
        logger.warning("IIS notification failed for %s: %s", agent_id, exc)

    logger.info(
        "[IIS-MIT] filed %s proposal=%s for agent=%s alerts=%d",
        level, dec.id, agent_id, len(alerts),
    )
    return dec.id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  L3 side-effects (notification + optional Jira)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _notify_contain(agent_id: str, alerts) -> None:
    from backend import notifications as _n
    summary = "; ".join(f"[{lvl}] {dim}" for lvl, dim, _ in alerts[:3])
    await _n.notify(
        level="critical",
        title=f"IIS L3 containment: {agent_id}",
        message=f"Agent halted after sustained drift. Alerts: {summary}",
        source=f"iis:{agent_id}",
    )


async def _maybe_jira(agent_id: str, alerts, decision_id: str) -> None:
    """File a Jira ticket if OMNISIGHT_IIS_JIRA_CONTAINMENT=true.
    Default OFF — design decision (avoid leaking code into Jira cloud
    by default). Reuses the existing Jira sender from notifications."""
    if (os.environ.get("OMNISIGHT_IIS_JIRA_CONTAINMENT") or "").strip().lower() not in {"true", "1", "yes"}:
        return
    try:
        from backend import notifications as _n
        await _n.notify(
            level="critical",
            title=f"[IIS-CONTAIN] {agent_id} sustained intelligence drift",
            message=(
                f"Decision {decision_id} filed at L3.\n"
                f"Alerts: {alerts}\n"
                f"Operator: review configs/skills/_pending and recent commits."
            ),
            source=f"iis:{agent_id}",
        )
        logger.info("IIS L3 Jira notification dispatched for %s", agent_id)
    except Exception as exc:
        logger.warning("IIS Jira dispatch failed for %s: %s", agent_id, exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Resolution callback (release dedup slot when operator decides)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def on_decision_resolved(agent_id: str, level: MitigationLevel) -> None:
    """Free the dedup slot so the next drift can re-fire. Call this
    from the consumer side when a Decision Engine `decision_resolved`
    SSE arrives for an intelligence/* kind."""
    with _state_lock:
        _open_proposals.pop((agent_id, level), None)
