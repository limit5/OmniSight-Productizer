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
_DEFAULT_RESTART_ALLOWLIST = frozenset({
    "omnisight-slo-monitor.service",
    "omnisight-staging-compose.service",
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


async def _do_restart(service: str, timeout: float = 30.0) -> tuple[bool, str]:
    # Cancellable child (audit r3 EXEC-04): a wedged systemctl is KILLED on
    # timeout rather than leaking a to_thread worker; `--` ends option parsing
    # (EXEC-03) so a service token can never be read as a flag. argv list, no
    # shell — no injection path from the (already allowlisted) service name.
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "restart", "--", service,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return False, f"timed out after {timeout:.0f}s (killed)"
    ok = proc.returncode == 0
    stderr = (err or b"").decode(errors="replace").strip()[:200]
    detail = f"rc={proc.returncode}" + (f" stderr={stderr}" if not ok else "")
    return ok, detail


async def execute_approved_action(action: dict, *, actor: str, dry_run: bool = False) -> dict:
    """Execute an APPROVED proposal. ``action`` is a proposed_actions row dict.

    Returns ``{ok, executed, dry_run, detail}``. Never raises for an expected
    refusal (bad kind / off-allowlist / disabled) — those return ok=False. Only
    a genuinely unexpected failure surfaces as ok=False with the exception text.
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
            "ok": False, "executed": False, "dry_run": False,
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
            "detail": f"[dry-run: {why}] would `systemctl --user restart {service}` (in allowlist).",
        }
    try:
        ok, detail = await _do_restart(service)   # timeout + kill are internal now
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "executed": True, "dry_run": False,
                "detail": f"restart {service} errored: {exc}"}
    return {
        "ok": ok, "executed": True, "dry_run": False,
        "detail": (f"restarted {service} ({detail}) by {actor}" if ok
                   else f"restart {service} FAILED ({detail})"),
    }
