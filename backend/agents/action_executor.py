"""P5 execution — run an APPROVED dangerous-action proposal, safely.

This is the "act" half of the propose-and-approve gate. Sora only ever PROPOSES
(backend/agents/tools.propose_action → a 'pending' row). A HUMAN approves via the
auth-gated API (backend/routers/proposed_actions). ONLY THEN does this module run
anything, and it is deliberately the narrowest, most-reversible surface:

  * ``restart`` — ``systemctl --user restart <service>`` for an ALLOWLISTED,
    non-critical user unit only. The prod backend / postgres / caddy / frontend
    are ``docker compose`` containers, NOT systemd --user units, so a user-scoped
    restart structurally cannot touch them; the explicit allowlist is defence in
    depth on top of that.
  * ``deploy`` / ``promote`` / ``rollback`` — NOT auto-executed here. They remain
    proposal-only; an operator runs them through the sanctioned release-train /
    ``POST /api/v1/prod/deploy`` path (which has its own dual-signal approval).
    This module returns a clear "run it via the release-train" result for them.

Two independent safety gates, both must pass for a real restart:
  1. ``OMNISIGHT_P5_EXECUTE`` must be truthy (default OFF → everything is a
     dry-run, so approving does not run anything until an operator opts the
     environment in).
  2. the service must be in the restart allowlist.

Nothing here is reachable by Sora — she has no execute tool; execution is only
called from the operator-approved API path.
"""
from __future__ import annotations

import asyncio
import json
import os

# Non-critical, reversible user units a restart proposal may target. Override
# with OMNISIGHT_P5_RESTART_ALLOWLIST (comma-separated). Deliberately excludes
# anything whose restart risks prod availability.
# Kept EXACTLY in sync with the host executor's hardcoded _ALLOWED_UNITS (audit
# r4 — a unit the container defers but the host refuses is a confusing dead-end).
# scripts/p5_host_executor.py is the FINAL gate; this is the container-side match.
_DEFAULT_RESTART_ALLOWLIST = frozenset({
    "omnisight-slo-monitor.service",
    "pipeline-coordinator.service",
})

EXECUTABLE_KINDS = frozenset({"restart"})  # kinds this module will auto-run
PROPOSAL_ONLY_KINDS = frozenset({"deploy", "promote", "rollback"})


def restart_allowlist() -> frozenset[str]:
    raw = os.environ.get("OMNISIGHT_P5_RESTART_ALLOWLIST", "").strip()
    if not raw:
        return _DEFAULT_RESTART_ALLOWLIST
    # reject dash-leading entries (audit r3 EXEC-03: an operator-misconfigured
    # allowlist entry that looks like a flag can't slip past the `--` guard).
    return frozenset(s.strip() for s in raw.split(",") if s.strip() and not s.strip().startswith("-"))


def is_execute_enabled() -> bool:
    """Real execution is OFF unless the environment explicitly opts in."""
    return os.environ.get("OMNISIGHT_P5_EXECUTE", "").strip().lower() in ("1", "true", "yes", "on")


async def execute_approved_action(action: dict, *, actor: str, dry_run: bool = False) -> dict:
    """Resolve an APPROVED proposal. ``action`` is a proposed_actions row dict.

    IMPORTANT: the backend runs in a container with NO systemd (verified
    2026-07-07: no systemctl, no DBus, no socket mounts), so a real restart is
    NOT run here — it is DEFERRED to a strictly-scoped HOST agent
    (scripts/p5_host_executor.py) that owns the actual `systemctl --user restart`.
    This module only gates (allowlist + flag + dry-run) and decides between:
      * dry-run / disabled / off-allowlist / proposal-only → a terminal result, or
      * a real allowlisted restart → ``deferred_to_host=True`` (the API leaves the
        row 'executing' for the host agent; nothing runs in the container).
    Returns ``{ok, executed, dry_run, [deferred_to_host], detail}``. Never raises
    for an expected refusal.
    """
    kind = (action.get("action_kind") or "").strip().lower()
    try:
        params = json.loads(action.get("params") or "{}")
        if not isinstance(params, dict):
            params = {}
    except (ValueError, TypeError):
        params = {}

    # deploy / promote / rollback are never auto-executed here.
    if kind in PROPOSAL_ONLY_KINDS:
        return {
            "ok": False, "executed": False, "dry_run": False, "proposal_only": True,
            "detail": (
                f"{kind} is not auto-executed by the P5 gate — approve records intent, "
                f"but an operator must run it via the sanctioned release-train / "
                f"POST /api/v1/prod/deploy flow (which has its own dual-signal approval)."
            ),
        }
    if kind != "restart":
        return {"ok": False, "executed": False, "dry_run": False,
                "detail": f"unknown/unsupported action_kind {kind!r}."}

    # ── restart ──────────────────────────────────────────────────────
    # Validate the param TYPE (audit r3 EXEC-01): a Sora-seeded non-string
    # 'service' must be a clean refusal, never an unhandled crash that strands
    # an approved proposal.
    svc = params.get("service")
    if not isinstance(svc, str) or not svc.strip():
        return {"ok": False, "executed": False, "dry_run": False,
                "detail": "restart requires a non-empty string 'service' param."}
    service = svc.strip()
    allow = restart_allowlist()
    if service not in allow:
        return {
            "ok": False, "executed": False, "dry_run": False,
            "detail": f"service {service!r} is NOT in the restart allowlist {sorted(allow)}; refused.",
        }
    # gate 1: real execution must be explicitly enabled; else dry-run.
    if dry_run or not is_execute_enabled():
        why = "dry_run requested" if dry_run else "OMNISIGHT_P5_EXECUTE is off"
        return {
            "ok": True, "executed": False, "dry_run": True,
            "detail": f"[dry-run: {why}] would restart {service} (in allowlist) via the host agent.",
        }
    # Real, allowlisted, enabled restart → DEFER to the host agent. The container
    # cannot run systemctl; the API keeps the row 'executing' and the host
    # executor (scripts/p5_host_executor.py) performs + records it.
    return {
        "ok": True, "executed": False, "deferred_to_host": True, "dry_run": False,
        "detail": (f"restart {service} approved + queued for the HOST executor "
                   f"(allowlisted); the row stays 'executing' until the host agent "
                   f"completes it. Requested by {actor}."),
    }
