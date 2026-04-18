"""R0 #306 — Policy Enforcement Point (PEP) Gateway.

A middleware that sits in front of every tool_executor invocation.
Each tool call is classified by rule matching into one of three
outcomes:

* **auto_allow** — tool is on the tier's whitelist and the command
  carries no destructive / production-scope markers. The call is
  waved through after an audit entry + SSE broadcast.
* **hold** — the tool call lands inside a production-scope pattern
  (``deploy.sh prod``, ``kubectl apply --context production``,
  ``terraform apply`` …). A Decision Engine proposal is raised; the
  caller blocks on ``wait_for_decision`` until an operator approves
  or rejects (or the deadline passes).
* **deny** — the tool call matches a destructive catastrophe
  pattern (``rm -rf /``, fork-bomb, ``dd if=/dev/zero`` …).  The
  call is refused synchronously and an audit entry is written.

The module is intentionally small / side-effect free so it can be
plugged into the tool-executor node and unit-tested in isolation.
When the PEP itself is unavailable (e.g. decision_engine raises on
``propose``), the circuit breaker opens and the module falls back to
a *degraded but alive* tier-local rule: auto_allow the T1 whitelist,
HOLD-degrade everything else to a deny so we fail closed.

The companion router :mod:`backend.routers.pep` exposes the HELD
queue + approve/reject flow to the frontend; the `pep-live-feed`
React component subscribes to the ``pep.decision`` SSE channel for
real-time updates.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Policy tables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class PepAction(str, Enum):
    auto_allow = "auto_allow"
    hold = "hold"
    deny = "deny"


# Destructive / catastrophic commands — always denied regardless of tier.
# Pattern table is deliberately a list[(name, regex)] so an audit row can
# name *which* rule fired.
_DESTRUCTIVE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("rm_rf_root", re.compile(r"\brm\s+-[rRf]{1,3}\s+/(?:\s|$)")),
    ("rm_rf_glob_root", re.compile(r"\brm\s+-[rRf]{1,3}\s+/\*")),
    ("chown_root_rec", re.compile(r"\bchown\s+-[Rr]\b[^|;&\n]*\s/(?:\s|$)")),
    ("chmod_777_root", re.compile(r"\bchmod\s+-[Rr]\s+777\s+/(?:\s|$)")),
    ("chmod_777_rec", re.compile(r"\bchmod\s+-[Rr]\s+777\b")),
    ("dd_to_device", re.compile(r"\bdd\s+.*of=/dev/(?:sd|nvme|xvd|vd|mmcblk)")),
    ("dd_zero_full", re.compile(r"\bdd\s+if=/dev/zero")),
    ("dd_random_full", re.compile(r"\bdd\s+if=/dev/u?random")),
    ("mkfs_any", re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\s+/dev/")),
    ("fork_bomb", re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")),
    ("shutdown", re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b")),
    ("curl_pipe_bash", re.compile(r"curl\b[^|;&\n]*\|\s*(?:bash|sh|zsh)\b")),
    ("wget_pipe_bash", re.compile(r"wget\b[^|;&\n]*-O\s*-[^|;&\n]*\|\s*(?:bash|sh)\b")),
    ("redir_to_sd", re.compile(r">\s*/dev/(?:sd|nvme|xvd|vd|mmcblk)")),
    ("drop_database", re.compile(r"\bDROP\s+DATABASE\b", re.IGNORECASE)),
    ("drop_schema_all", re.compile(r"\bDROP\s+SCHEMA\s+public\s+CASCADE", re.IGNORECASE)),
    ("destroy_terraform", re.compile(r"\bterraform\s+destroy\b")),
    ("git_push_force", re.compile(r"\bgit\s+push\s+.*(?:--force|-f\b)")),
]

# Production-scope commands — held for human approval. These are
# operations that are safe in isolation but high-blast-radius when
# executed against shared infrastructure (prod K8s clusters, prod DBs,
# money endpoints, etc.).
_PROD_HOLD_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("deploy_prod", re.compile(r"\bdeploy(?:\.sh)?\b[^|;&\n]*\bprod(?:uction)?\b")),
    ("kubectl_prod_context", re.compile(
        r"\bkubectl\b[^|;&\n]*--context[=\s]+(?:production|prod)\b"
    )),
    ("kubectl_prod_ns", re.compile(
        r"\bkubectl\b[^|;&\n]*-n[=\s]+(?:production|prod)\b"
    )),
    ("terraform_apply", re.compile(r"\bterraform\s+apply\b")),
    ("helm_upgrade_prod", re.compile(
        r"\bhelm\s+upgrade\b[^|;&\n]*--namespace[=\s]+(?:production|prod)\b"
    )),
    ("ansible_playbook_prod", re.compile(
        r"\bansible-playbook\b[^|;&\n]*\bprod(?:uction)?\b"
    )),
    ("aws_prod_profile", re.compile(
        r"\baws\b[^|;&\n]*--profile[=\s]+(?:production|prod)\b"
    )),
    ("gcloud_prod_project", re.compile(
        r"\bgcloud\b[^|;&\n]*--project[=\s]+[^\s|;&]*prod"
    )),
    ("psql_prod_host", re.compile(r"\bpsql\b[^|;&\n]*-h\s+[^\s|;&]*prod[^\s|;&]*")),
    ("docker_push_prod", re.compile(
        r"\bdocker\s+push\s+[^\s|;&]*:prod(?:uction)?\b"
    )),
]

# Tier → whitelist of tools that never need approval in that tier.
# Everything not on the tier whitelist is HELD (goes through decision
# engine) rather than denied outright — the operator approves it.
# T3 is the "fat" tier: sudo permitted, but prod-deploy still held.
TIER_T1_WHITELIST: frozenset[str] = frozenset({
    # filesystem (sandboxed read/write)
    "read_file", "write_file", "create_file", "patch_file", "list_directory",
    "read_yaml", "write_yaml", "search_in_files",
    # git local-only
    "git_status", "git_log", "git_diff", "git_diff_staged", "git_branch",
    "git_add", "git_commit", "git_checkout_branch", "git_remote_list",
    # planning / reporting
    "get_platform_config", "register_build_artifact", "generate_artifact_report",
    "get_next_task", "update_task_status", "add_task_comment",
})

# T2 (networked) adds outbound git/push, Gerrit review, PR creation.
TIER_T2_EXTRA: frozenset[str] = frozenset({
    "git_push", "git_add_remote", "create_pr",
    "gerrit_get_diff", "gerrit_post_comment", "gerrit_submit_review",
    "check_evk_connection", "list_uvc_devices",
})

# T3 adds anything that spawns a shell inside the sandbox.
TIER_T3_EXTRA: frozenset[str] = frozenset({
    "run_bash", "deploy_to_evk",
})


def tier_whitelist(tier: str) -> frozenset[str]:
    """Return the cumulative whitelist for the given tier."""
    t = (tier or "t1").lower()
    if t in ("t1",):
        return TIER_T1_WHITELIST
    if t in ("t2", "networked"):
        return TIER_T1_WHITELIST | TIER_T2_EXTRA
    if t in ("t3",):
        return TIER_T1_WHITELIST | TIER_T2_EXTRA | TIER_T3_EXTRA
    # Unknown tier → assume most restrictive.
    return TIER_T1_WHITELIST


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Decision dataclass
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class PepDecision:
    """Outcome of a single ``evaluate()`` call.

    ``decision_id`` is the ``decision_engine`` id for HOLD outcomes; it
    is ``None`` on auto_allow / deny because no DE proposal is raised.
    """
    id: str
    ts: float
    agent_id: str
    tool: str
    command: str
    tier: str
    action: PepAction
    rule: str = ""
    reason: str = ""
    impact_scope: str = ""        # "local" | "prod" | "destructive"
    decision_id: Optional[str] = None
    degraded: bool = False        # True when circuit breaker is open

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["action"] = self.action.value
        return d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Circuit breaker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_breaker_lock = threading.Lock()
_breaker_state: dict[str, Any] = {
    "open": False,
    "consecutive_failures": 0,
    "opened_at": 0.0,
    "last_failure": 0.0,
    "last_reason": "",
}

# Open after N consecutive propose() errors. The breaker stays open for
# COOLDOWN_SECONDS then half-opens automatically.
_BREAKER_FAILURE_THRESHOLD = 3
_BREAKER_COOLDOWN_SECONDS = 60


def breaker_status() -> dict[str, Any]:
    with _breaker_lock:
        st = dict(_breaker_state)
    now = time.time()
    cooldown = 0
    if st.get("open"):
        elapsed = now - st.get("opened_at", now)
        cooldown = max(0, int(_BREAKER_COOLDOWN_SECONDS - elapsed))
        if cooldown == 0:
            st["open"] = False  # half-open — next evaluate will retry
    st["cooldown_remaining"] = cooldown
    return st


def _breaker_record_success() -> None:
    with _breaker_lock:
        if _breaker_state["open"] or _breaker_state["consecutive_failures"]:
            _breaker_state["open"] = False
            _breaker_state["consecutive_failures"] = 0


def _breaker_record_failure(reason: str) -> None:
    with _breaker_lock:
        _breaker_state["consecutive_failures"] += 1
        _breaker_state["last_failure"] = time.time()
        _breaker_state["last_reason"] = reason[:200]
        if _breaker_state["consecutive_failures"] >= _BREAKER_FAILURE_THRESHOLD:
            if not _breaker_state["open"]:
                _breaker_state["open"] = True
                _breaker_state["opened_at"] = time.time()


def _breaker_is_open() -> bool:
    with _breaker_lock:
        if not _breaker_state["open"]:
            return False
        if time.time() - _breaker_state.get("opened_at", 0.0) > _BREAKER_COOLDOWN_SECONDS:
            # Half-open — the next caller gets a retry chance.
            _breaker_state["open"] = False
            _breaker_state["consecutive_failures"] = 0
            return False
        return True


def reset_breaker() -> None:
    """Operator / test helper."""
    with _breaker_lock:
        _breaker_state["open"] = False
        _breaker_state["consecutive_failures"] = 0
        _breaker_state["opened_at"] = 0.0
        _breaker_state["last_failure"] = 0.0
        _breaker_state["last_reason"] = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Classification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _extract_command(tool: str, arguments: dict[str, Any]) -> str:
    """Flatten the most interesting bit of a tool call into a searchable
    string so the regex table can match across shell-ish tools.

    For ``run_bash`` the ``command`` argument is authoritative. For
    everything else we concatenate ``str(arguments.values())`` so that
    a ``deploy_to_evk(target="prod")`` call still lights up the
    production pattern.
    """
    if isinstance(arguments, dict):
        if tool in ("run_bash", "shell", "exec") and "command" in arguments:
            return str(arguments.get("command") or "")
        # Common one-shot patterns
        if "cmd" in arguments:
            return f"{tool} {arguments.get('cmd')}"
        # Generic: "tool arg1=val1 arg2=val2"
        parts = [tool]
        for k, v in arguments.items():
            parts.append(f"{k}={v}")
        return " ".join(parts)
    return f"{tool} {arguments!r}"


def classify(tool: str, arguments: dict[str, Any], tier: str) -> tuple[PepAction, str, str, str]:
    """Pure function: return (action, rule_id, reason, impact_scope).

    Ordering matters: destructive ⟶ production hold ⟶ tier whitelist.
    A tool *not* on the tier whitelist but without a destructive /
    production rule hit still gets HELD (so the operator can wave it
    through). This is safer than a pure allow-list deny because it
    keeps the LLM usable while humans expand the allow-list over time.
    """
    command = _extract_command(tool, arguments)
    # 1. Destructive patterns — always DENY.
    for rule_id, rx in _DESTRUCTIVE_RULES:
        if rx.search(command):
            return PepAction.deny, rule_id, f"destructive pattern: {rule_id}", "destructive"
    # 2. Production-scope patterns — HOLD.
    for rule_id, rx in _PROD_HOLD_RULES:
        if rx.search(command):
            return PepAction.hold, rule_id, f"production-scope: {rule_id}", "prod"
    # 3. Tier whitelist membership.
    allow = tier_whitelist(tier)
    if tool in allow:
        return PepAction.auto_allow, "tier_whitelist", f"tool in {tier} whitelist", "local"
    # 4. Not destructive, not prod, not whitelisted → HOLD with "unlisted"
    return PepAction.hold, "tier_unlisted", f"tool {tool!r} not in {tier} whitelist", "local"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit / SSE / metric emit helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _emit_sse(dec: PepDecision) -> None:
    try:
        from backend.events import bus
        bus.publish("pep.decision", dec.to_dict())
    except Exception as exc:
        logger.debug("pep.decision SSE publish skipped: %s", exc)


def _emit_audit(dec: PepDecision, action_override: str | None = None) -> None:
    try:
        from backend import audit
        action = action_override or {
            PepAction.auto_allow: "pep.auto_allow",
            PepAction.hold: "pep.intercept",
            PepAction.deny: "pep.deny",
        }[dec.action]
        audit.log_sync(
            action=action,
            entity_kind="tool_call",
            entity_id=dec.id,
            after={
                "agent_id": dec.agent_id,
                "tool": dec.tool,
                "command": dec.command[:500],
                "tier": dec.tier,
                "rule": dec.rule,
                "impact_scope": dec.impact_scope,
                "reason": dec.reason,
                "degraded": dec.degraded,
            },
            actor="pep",
        )
    except Exception as exc:
        logger.debug("pep audit skipped: %s", exc)


def _bump_metric(dec: PepDecision) -> None:
    try:
        from backend import metrics
        if not metrics.is_available():
            return
        metrics.pep_decisions_total.labels(
            decision=dec.action.value, tier=dec.tier, rule=dec.rule or "none"
        ).inc()
        if dec.action == PepAction.deny:
            metrics.pep_deny_total.labels(rule=dec.rule or "none").inc()
    except Exception as exc:
        logger.debug("pep metric bump failed: %s", exc)


def _bump_hold_duration(duration_seconds: float, outcome: str) -> None:
    try:
        from backend import metrics
        if not metrics.is_available():
            return
        metrics.pep_hold_duration_seconds.labels(outcome=outcome).observe(duration_seconds)
    except Exception as exc:
        logger.debug("pep hold-duration bump failed: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Held-decision registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# id → PepDecision (only HELD entries) so the router can list them
# without having to join against the decision_engine queue.
_held_registry_lock = threading.Lock()
_held_registry: dict[str, PepDecision] = {}
_HELD_MAX = 512


def _held_add(dec: PepDecision) -> None:
    with _held_registry_lock:
        _held_registry[dec.id] = dec
        if len(_held_registry) > _HELD_MAX:
            # drop oldest
            oldest = sorted(_held_registry.items(), key=lambda kv: kv[1].ts)[0][0]
            _held_registry.pop(oldest, None)


def _held_pop(pep_id: str) -> PepDecision | None:
    with _held_registry_lock:
        return _held_registry.pop(pep_id, None)


def held_snapshot() -> list[dict[str, Any]]:
    """Return current HELD queue for router / UI."""
    with _held_registry_lock:
        items = sorted(_held_registry.values(), key=lambda d: d.ts, reverse=True)
    return [d.to_dict() for d in items]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Recent-decisions ring (UI live feed uses this for initial paint)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_recent_lock = threading.Lock()
_recent: list[PepDecision] = []
_RECENT_MAX = 200


def _record_recent(dec: PepDecision) -> None:
    with _recent_lock:
        _recent.append(dec)
        if len(_recent) > _RECENT_MAX:
            _recent.pop(0)


def recent_decisions(limit: int = 100) -> list[dict[str, Any]]:
    with _recent_lock:
        items = list(_recent[-limit:])
    items.reverse()
    return [d.to_dict() for d in items]


def stats() -> dict[str, int]:
    """Cheap summary for the live feed header."""
    with _recent_lock:
        items = list(_recent)
    auto = held = denied = 0
    for d in items:
        if d.action == PepAction.auto_allow:
            auto += 1
        elif d.action == PepAction.hold:
            held += 1
        elif d.action == PepAction.deny:
            denied += 1
    return {
        "auto_allowed": auto,
        "held": held,
        "denied": denied,
        "total": len(items),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Main entrypoint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def evaluate(
    *,
    tool: str,
    arguments: dict[str, Any],
    agent_id: str = "",
    tier: str = "t1",
    propose_fn: Callable[..., Any] | None = None,
    wait_for_decision: Callable[[str, float], Awaitable[Any]] | None = None,
    hold_timeout_s: float = 1800.0,
) -> PepDecision:
    """Classify the tool call and apply the policy.

    * ``auto_allow`` → returns immediately with action=auto_allow.
    * ``deny`` → returns immediately with action=deny (caller refuses).
    * ``hold`` → raises a Decision Engine proposal (or the injected
      ``propose_fn`` for tests); blocks on ``wait_for_decision`` until
      the deadline or the operator resolves it. Rejection becomes a
      ``deny`` outcome; approval becomes ``auto_allow`` but the
      decision_id is preserved so the UI can cross-reference.

    On PEP internal failure (propose raises, etc.) the circuit breaker
    opens and subsequent calls fall back to a "degraded" HOLD→deny
    path so we fail closed.
    """
    action, rule, reason, scope = classify(tool, arguments, tier)
    command_flat = _extract_command(tool, arguments)
    dec = PepDecision(
        id=f"pep-{uuid.uuid4().hex[:10]}",
        ts=time.time(),
        agent_id=agent_id or "",
        tool=tool,
        command=command_flat,
        tier=tier,
        action=action,
        rule=rule,
        reason=reason,
        impact_scope=scope,
    )

    if action is PepAction.auto_allow:
        _finalize(dec)
        return dec

    if action is PepAction.deny:
        _finalize(dec)
        return dec

    # HELD path — propose + wait.
    if _breaker_is_open():
        dec.degraded = True
        dec.action = PepAction.deny  # fail closed
        dec.reason = "PEP circuit open — fallback deny"
        _finalize(dec)
        return dec

    try:
        de_id = _propose_hold(dec, propose_fn=propose_fn, timeout_s=hold_timeout_s)
        dec.decision_id = de_id
        _breaker_record_success()
    except Exception as exc:
        _breaker_record_failure(f"propose failed: {exc}")
        dec.degraded = True
        dec.action = PepAction.deny
        dec.reason = f"PEP propose failed ({exc}) — fallback deny"
        _finalize(dec)
        return dec

    _held_add(dec)
    _finalize(dec, skip_recent=False)  # emit hold SSE + audit + metric

    # Wait for resolution (operator approve / reject / timeout).
    t0 = time.time()
    if wait_for_decision is None:
        wait_for_decision = _default_wait_for_decision
    try:
        outcome = await wait_for_decision(de_id, hold_timeout_s)
    except Exception as exc:
        _breaker_record_failure(f"wait failed: {exc}")
        _held_pop(dec.id)
        dec.degraded = True
        dec.action = PepAction.deny
        dec.reason = f"PEP wait failed ({exc}) — fallback deny"
        _emit_sse(dec)
        _emit_audit(dec, action_override="pep.deny")
        _bump_metric(dec)
        return dec
    duration = time.time() - t0

    _held_pop(dec.id)

    if outcome == "approved":
        dec.action = PepAction.auto_allow
        dec.reason = "operator approved"
        _bump_hold_duration(duration, "approved")
        _emit_sse(dec)
        _emit_audit(dec, action_override="pep.approve")
        _bump_metric(dec)
    elif outcome == "rejected":
        dec.action = PepAction.deny
        dec.reason = "operator rejected"
        _bump_hold_duration(duration, "rejected")
        _emit_sse(dec)
        _emit_audit(dec, action_override="pep.reject")
        _bump_metric(dec)
    else:  # timeout / unknown → fail closed
        dec.action = PepAction.deny
        dec.reason = f"operator decision timed out ({outcome})"
        _bump_hold_duration(duration, "timeout")
        _emit_sse(dec)
        _emit_audit(dec, action_override="pep.deny")
        _bump_metric(dec)

    return dec


def _finalize(dec: PepDecision, *, skip_recent: bool = False) -> None:
    """Emit SSE + audit + metric for a terminal (or HELD) decision."""
    _record_recent(dec)
    _emit_sse(dec)
    _emit_audit(dec)
    _bump_metric(dec)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Decision-engine hop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _propose_hold(
    dec: PepDecision,
    *,
    propose_fn: Callable[..., Any] | None,
    timeout_s: float,
) -> str:
    """Raise a DE proposal. Returns the DE id so we can wait on it.

    ``propose_fn`` is injectable for unit tests — by default we call
    ``decision_engine.propose``. The kind is always
    ``"pep_tool_intercept"`` so the Decision Dashboard filter tab can
    surface these.
    """
    from backend import decision_engine as de

    fn = propose_fn or de.propose
    # destructive-scope severity for impact_scope="prod"; risky otherwise
    severity = (
        de.DecisionSeverity.destructive
        if dec.impact_scope == "prod"
        else de.DecisionSeverity.risky
    )
    options = [
        {"id": "approve", "label": "APPROVE",
         "description": f"Allow {dec.tool} to execute."},
        {"id": "reject", "label": "REJECT",
         "description": "Block this tool call and return an error to the agent."},
    ]
    prop = fn(
        kind="pep_tool_intercept",
        title=f"PEP HOLD · {dec.tool}",
        detail=(
            f"Agent {dec.agent_id or 'unknown'} wants to run {dec.tool!r} "
            f"(tier {dec.tier}, impact {dec.impact_scope}, rule {dec.rule}). "
            f"Command: {dec.command[:500]}"
        ),
        options=options,
        default_option_id="reject",  # safe default if operator ignores
        severity=severity,
        timeout_s=timeout_s,
        source={
            "pep_id": dec.id,
            "agent_id": dec.agent_id,
            "tool": dec.tool,
            "command": dec.command[:500],
            "tier": dec.tier,
            "impact_scope": dec.impact_scope,
            "rule": dec.rule,
            "category": "pep_tool_intercept",
        },
    )
    return prop.id


async def _default_wait_for_decision(decision_id: str, timeout_s: float) -> str:
    """Poll the Decision Engine until the id is no longer pending.

    Returns one of ``"approved"``, ``"rejected"``, ``"timeout"`` or
    ``"auto_executed"``. Poll interval is 250 ms for snappiness; the
    caller enforces ``timeout_s`` as the hard cap.
    """
    import asyncio
    from backend import decision_engine as de

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        dec = de.get(decision_id)
        if dec is None:
            return "rejected"  # vanished — treat as rejection
        status = dec.status
        if status == de.DecisionStatus.pending:
            await asyncio.sleep(0.25)
            continue
        if status == de.DecisionStatus.approved:
            chosen = dec.chosen_option_id or ""
            return "approved" if chosen == "approve" else "rejected"
        if status == de.DecisionStatus.rejected:
            return "rejected"
        if status == de.DecisionStatus.auto_executed:
            chosen = dec.chosen_option_id or ""
            return "approved" if chosen == "approve" else "rejected"
        if status == de.DecisionStatus.timeout_default:
            chosen = dec.chosen_option_id or ""
            return "approved" if chosen == "approve" else "rejected"
        # undone / unknown
        return "rejected"
    return "timeout"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _reset_for_tests() -> None:
    """Wipe module state. For pytest fixtures only."""
    reset_breaker()
    with _held_registry_lock:
        _held_registry.clear()
    with _recent_lock:
        _recent.clear()
