"""Phase 50B — Decision Rules Engine.

Operators can declare rules that override the default severity / default
option for a class of proposals. A rule matches a decision by glob on
`kind` and (optionally) pins the severity, the auto-eligible modes, and
the default option. At propose() time the engine walks rules in priority
order (first hit wins).

Rule shape:
    {
        "id":                str        # stable id for CRUD
        "kind_pattern":      str        # fnmatch glob, e.g. "stuck/*"
        "severity":          str|None   # info|routine|risky|destructive
        "auto_in_modes":     list[str]  # modes where this rule auto-executes
        "default_option_id": str|None
        "priority":          int        # lower runs first (0 = top)
        "enabled":           bool
        "note":              str        # operator-facing comment
    }

Rules persist to SQLite (table `decision_rules`) — the in-memory list is
a cache loaded at startup via `load_from_db()`. `replace_rules()` writes
through to the DB so operator edits survive restart. Phase 53 audit
layer will add hash-chaining on top of this.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import threading
import uuid
from typing import Any

from backend.decision_engine import DecisionSeverity, OperationMode

logger = logging.getLogger(__name__)

# Fix-B B7: threading.Lock guarding sync `_RULES[:] = ...` mutations only.
# `_persist()` and `load_from_db()` do their `await db.*` OUTSIDE the lock.
_RULES_LOCK = threading.Lock()
_RULES: list[dict[str, Any]] = []


def _normalise(rule: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalise a rule dict. Raises ValueError on bad input."""
    if "kind_pattern" not in rule or not isinstance(rule["kind_pattern"], str) or not rule["kind_pattern"].strip():
        raise ValueError("kind_pattern is required")
    sev = rule.get("severity")
    if sev is not None:
        try:
            DecisionSeverity(sev)
        except ValueError as exc:
            raise ValueError(f"unknown severity: {sev}") from exc
    modes = rule.get("auto_in_modes") or []
    if not isinstance(modes, list):
        raise ValueError("auto_in_modes must be a list")
    bad = [m for m in modes if m not in {e.value for e in OperationMode}]
    if bad:
        raise ValueError(f"unknown mode(s): {bad}")
    return {
        "id": rule.get("id") or f"rule-{uuid.uuid4().hex[:8]}",
        "kind_pattern": rule["kind_pattern"].strip(),
        "severity": sev,
        "auto_in_modes": list(modes),
        "default_option_id": rule.get("default_option_id"),
        "priority": int(rule.get("priority", 100)),
        "enabled": bool(rule.get("enabled", True)),
        "note": str(rule.get("note", ""))[:240],
    }


def list_rules() -> list[dict[str, Any]]:
    """Snapshot of rules in priority order (stable sort)."""
    with _RULES_LOCK:
        return sorted(_RULES, key=lambda r: (r["priority"], r["id"]))


def replace_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace the rule set wholesale. The editor PUTs the whole list.

    Persists the new list to SQLite so operator edits survive restart.
    DB failure is logged but does not abort the in-memory update —
    operators should not lose their current session because of a
    transient DB hiccup.
    """
    normalised = [_normalise(r) for r in rules]
    # Reject duplicate ids.
    ids = [r["id"] for r in normalised]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate rule id")
    with _RULES_LOCK:
        _RULES[:] = normalised
    # Schedule DB write on the running loop if available; best-effort.
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_persist(normalised))
    except RuntimeError:
        # No running loop (sync test path) — skip persistence.
        pass
    return list_rules()


async def _persist(rules: list[dict[str, Any]]) -> None:
    try:
        from backend import db
        await db.replace_decision_rules(rules)
    except Exception as exc:
        logger.warning("decision_rules persist failed: %s", exc)


async def load_from_db() -> int:
    """Populate the in-memory rule list from SQLite. Called once at
    backend startup. Returns the number of rules loaded."""
    try:
        from backend import db
        rows = await db.load_decision_rules()
    except Exception as exc:
        logger.warning("decision_rules load failed: %s", exc)
        return 0
    # Normalise each row so legacy/partial DB rows cannot poison the
    # engine with malformed data.
    normalised: list[dict[str, Any]] = []
    for r in rows:
        try:
            normalised.append(_normalise(r))
        except ValueError as exc:
            logger.warning("skipping invalid persisted rule %r: %s", r.get("id"), exc)
    with _RULES_LOCK:
        _RULES[:] = normalised
    return len(normalised)


def clear() -> None:
    """Test hook — reset to empty."""
    with _RULES_LOCK:
        _RULES.clear()


def match(kind: str, current_mode: OperationMode | str) -> dict[str, Any] | None:
    """First enabled rule whose pattern matches *kind*.

    Returns the rule dict on hit, None otherwise. Mode is passed through
    so tests can dry-run rules without flipping the global mode.
    """
    if isinstance(current_mode, str):
        try:
            current_mode = OperationMode(current_mode)
        except ValueError:
            return None  # unknown mode → no rule applies
    for r in list_rules():
        if not r["enabled"]:
            continue
        if fnmatch.fnmatchcase(kind, r["kind_pattern"]):
            return r
    return None


def apply(kind: str, proposed_severity: DecisionSeverity, default_option_id: str | None,
          current_mode: OperationMode) -> tuple[DecisionSeverity, str | None, dict[str, Any] | None, bool]:
    """Compute the effective (severity, default_option_id) for a proposal.

    Returns (severity, default_option_id, matched_rule_or_None, rule_forces_auto).
    `rule_forces_auto` is True when the matched rule explicitly lists the
    current mode in its auto_in_modes — the propose() path should then
    auto-execute regardless of the normal mode × severity matrix.
    """
    rule = match(kind, current_mode)
    if not rule:
        return proposed_severity, default_option_id, None, False
    sev = DecisionSeverity(rule["severity"]) if rule["severity"] else proposed_severity
    default = rule["default_option_id"] or default_option_id
    forces_auto = current_mode.value in (rule.get("auto_in_modes") or [])
    return sev, default, rule, forces_auto


def test_against(kind_samples: list[str], current_mode: OperationMode | str) -> list[dict[str, Any]]:
    """Dry-run helper — return the rule hit (if any) for each sample kind.

    Shape: [{kind, rule_id|None, severity|None, auto|False}]
    """
    out: list[dict[str, Any]] = []
    for k in kind_samples:
        rule = match(k, current_mode)
        if not rule:
            out.append({"kind": k, "rule_id": None, "severity": None, "auto": False})
        else:
            mode_val = current_mode.value if isinstance(current_mode, OperationMode) else current_mode
            out.append({
                "kind": k,
                "rule_id": rule["id"],
                "severity": rule["severity"],
                "auto": mode_val in (rule.get("auto_in_modes") or []),
            })
    return out
