"""R1 (#307) — Built-in ChatOps handlers.

Registered with :mod:`backend.chatops_bridge` on import. Provides:

* ``pep_approve`` / ``pep_reject`` button handlers — the ChatOps button
  callback for a held PEP tool call.
* ``omnisight`` command handler — dispatch for the ``/omnisight <verb>``
  convention: ``inspect`` / ``inject`` / ``rollback`` / ``status``.

Each handler returns a markdown string that the transport layer echoes
back to the user (Discord embed body, Teams card, Line reply).
"""

from __future__ import annotations

import logging

from backend import chatops_bridge as bridge
from backend import agent_hints
from backend.chatops_bridge import Inbound

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Button handlers — PEP approve / reject
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _resolve_pep(pep_id: str, decision: str, resolver: str) -> tuple[bool, str]:
    """Shared implementation for the ChatOps approve / reject button.

    Returns ``(ok, message)``. Uses the same look-up-by-held-id strategy
    as ``POST /pep/decision/{pep_id}`` so the behaviour stays identical
    across entry points.
    """
    from backend import pep_gateway as pep
    from backend import decision_engine as de

    held = {d["id"]: d for d in pep.held_snapshot()}
    entry = held.get(pep_id)
    if not entry:
        return False, f"PEP {pep_id} not in held queue (may already be resolved)"
    de_id = entry.get("decision_id")
    if not de_id:
        return False, f"PEP {pep_id} has no linked decision_engine id"
    existing = de.get(de_id)
    if existing is None:
        return False, "decision not found in engine"
    if existing.status != de.DecisionStatus.pending:
        return False, f"not pending (status={existing.status.value})"
    if decision == "approve":
        out = de.resolve(de_id, "approve", resolver=resolver,
                         status=de.DecisionStatus.approved)
        verb = "approved"
    else:
        out = de.resolve(de_id, "__rejected__", resolver=resolver,
                         status=de.DecisionStatus.rejected)
        verb = "rejected"
    return bool(out), f"PEP {pep_id} {verb} by {resolver}"


async def _pep_approve(inbound: Inbound) -> str:
    bridge.authorize_inject(inbound)
    pep_id = inbound.button_value or inbound.button_id.split(":", 1)[-1]
    ok, msg = await _resolve_pep(
        pep_id, "approve",
        resolver=f"chatops:{inbound.author or inbound.user_id}",
    )
    return ("✅ " if ok else "⚠️ ") + msg


async def _pep_reject(inbound: Inbound) -> str:
    bridge.authorize_inject(inbound)
    pep_id = inbound.button_value or inbound.button_id.split(":", 1)[-1]
    ok, msg = await _resolve_pep(
        pep_id, "reject",
        resolver=f"chatops:{inbound.author or inbound.user_id}",
    )
    return ("✅ " if ok else "⚠️ ") + msg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /omnisight verbs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _split_verb(args: str) -> tuple[str, str]:
    verb, _, rest = (args or "").strip().partition(" ")
    return verb.lower(), rest.strip()


async def _inspect(agent_id: str) -> str:
    """Return the last three ReAct rounds for an agent in markdown."""
    if not agent_id:
        return "Usage: `/omnisight inspect <agent_id>`"
    from backend import db
    try:
        findings = await db.list_debug_findings(agent_id=agent_id, limit=3)
    except Exception as exc:
        return f"⚠️ inspect lookup failed: {exc}"
    if not findings:
        return f"_No recent findings for `{agent_id}`_"
    lines = [f"### 🔎 Inspect · `{agent_id}`"]
    for f in findings[:3]:
        lines.append(
            f"- **{f.get('finding_type','?')}** [{f.get('severity','info')}] "
            f"{(f.get('content') or '')[:300]}"
        )
    pending = agent_hints.peek(agent_id)
    if pending:
        lines.append(f"\n_Pending hint: {pending.text[:200]} (from {pending.author})_")
    return "\n".join(lines)


async def _inject(inbound: Inbound, agent_id: str, text: str) -> str:
    """Write an operator hint to the agent's blackboard."""
    if not agent_id or not text:
        return "Usage: `/omnisight inject <agent_id> <hint text>`"
    bridge.authorize_inject(inbound)
    try:
        hint = agent_hints.inject(
            agent_id, text,
            author=inbound.author or inbound.user_id or "chatops",
            channel=inbound.channel,
        )
    except agent_hints.HintRateLimitError as exc:
        return f"⚠️ rate limit: {exc}"
    except ValueError as exc:
        return f"⚠️ invalid hint: {exc}"
    return f"✅ Hint injected into `{agent_id}` ({len(hint.text)} chars). Agent will wake on next tick."


async def _rollback(inbound: Inbound, agent_id: str) -> str:
    """Trigger the R8 worktree discard + recreate for an agent."""
    if not agent_id:
        return "Usage: `/omnisight rollback <agent_id>`"
    bridge.authorize_inject(inbound)
    # R8 discard+recreate lives in backend.workspace — best-effort call.
    try:
        from backend import workspace as _ws
    except Exception:
        return "⚠️ workspace module unavailable; rollback recorded but not executed"
    rollback_fn = None
    for name in ("rollback_agent_worktree", "discard_and_recreate", "reset_agent_worktree"):
        fn = getattr(_ws, name, None)
        if callable(fn):
            rollback_fn = fn
            break
    if rollback_fn is None:
        return (
            f"⚠️ no rollback primitive on backend.workspace for `{agent_id}` "
            "(operator invocation recorded to audit)."
        )
    try:
        res = rollback_fn(agent_id) if not _is_coro(rollback_fn) else await rollback_fn(agent_id)
    except Exception as exc:
        return f"⚠️ rollback failed: {exc}"
    return f"✅ Worktree rollback issued for `{agent_id}`: {res}"


def _is_coro(fn) -> bool:
    import inspect as _inspect
    return _inspect.iscoroutinefunction(fn)


async def _status(_: str = "") -> str:
    """Return a compact KPI snapshot for the operator."""
    lines = ["### 📊 OmniSight Status"]
    try:
        from backend.routers.agents import _agents as _agent_reg
        running = sum(1 for a in _agent_reg.values()
                      if getattr(a.status, "value", str(a.status)) == "running")
        lines.append(f"- Active agents: **{running}** (of {len(_agent_reg)})")
    except Exception as exc:
        lines.append(f"- Active agents: _unavailable ({exc})_")
    try:
        from backend import pep_gateway as _pep
        lines.append(
            f"- PEP held: **{len(_pep.held_snapshot())}** · "
            f"stats {_pep.stats()}"
        )
    except Exception as exc:
        lines.append(f"- PEP: _unavailable ({exc})_")
    try:
        from backend import decision_engine as _de
        pending = len(_de.list_pending())
        lines.append(f"- Decision queue: **{pending}** pending")
    except Exception as exc:
        lines.append(f"- Decisions: _unavailable ({exc})_")
    try:
        pending_hints = agent_hints.snapshot()
        lines.append(f"- Pending hints: **{len(pending_hints)}**")
    except Exception:
        pass
    return "\n".join(lines)


async def _omnisight_cmd(inbound: Inbound) -> str:
    verb, rest = _split_verb(inbound.command_args)
    if verb in ("", "help"):
        return (
            "**/omnisight** usage:\n"
            "- `/omnisight inspect <agent_id>` — last 3 ReAct findings\n"
            "- `/omnisight inject <agent_id> <hint>` — blackboard hint\n"
            "- `/omnisight rollback <agent_id>` — R8 worktree reset\n"
            "- `/omnisight status` — system KPI snapshot"
        )
    if verb == "status":
        return await _status()
    # For the rest, the first word of `rest` is the agent id.
    aid, _, tail = rest.partition(" ")
    if verb == "inspect":
        return await _inspect(aid.strip())
    if verb == "inject":
        return await _inject(inbound, aid.strip(), tail.strip())
    if verb == "rollback":
        return await _rollback(inbound, aid.strip())
    return f"Unknown verb: `{verb}`. Try `/omnisight help`."


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def register_defaults() -> None:
    """Wire up the built-in handlers. Idempotent; callable from startup."""
    bridge.on_button_click("pep_approve", _pep_approve)
    bridge.on_button_click("pep_reject", _pep_reject)
    bridge.on_command("omnisight", _omnisight_cmd)


# Auto-register on import so any caller that touches the bridge gets the
# built-ins wired up without needing a startup-hook dance.
register_defaults()
