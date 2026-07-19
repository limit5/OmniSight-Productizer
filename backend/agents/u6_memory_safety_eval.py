"""U6-5a — L3 memory-safety eval: the action-influence negative control (DORMANT).

The load-bearing safety gate (frozen design §2.E + §11 RB3): before an L3 candidate
fact may be promoted, PROVE it is ACTION-INERT — a scripted mutating decision
through the REAL action guard (``authorize_action``) is BYTE-IDENTICAL whether or
not the candidate is injected as memory provenance. This is a NEGATIVE CONTROL: the
kernel is content-invariant by design (INV-1), so a correct kernel always passes;
the eval catches, PER CANDIDATE, any regression (or crafted content) that would let
a memory fact move an action verdict. "Memory can NEVER authorize a side effect" is
enforced by the kernel; this eval continuously re-proves it for each fact.

This is a REGRESSION DETECTOR against INV-1, NOT per-fact semantic adjudication: it
proves "this fact does not move the kernel's verdict," not "this fact is
semantically harmless" (set-composition / paraphrase / rate-cap are separate,
deferred mechanisms). It deliberately does NOT probe the auto-auth layer
(``auto_auth_policy``) — that layer's provenance gate is monotone-RESTRICTIVE, so an
injected memory source can only ADD a human challenge, never flip to auto-grant;
memory can never authorize a side effect there either, without this eval probing it.

Composes only already-built, offline pieces (the kernel + provenance + the U6-1a
renderer) — no LLM, no DB. DORMANT: the U6-5 producer / U6-6 publication consume it;
nothing runs it in production yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.agents import execution_context
from backend.agents import provenance as prov
from backend.agents.authorization_kernel import OperationRequest, authorize_action
from backend.agents.provenance import ProvenanceCollector, SnapshotCache
from backend.agents.u6_fact_schema import Fact, render_fact
from backend.agents.u6_memory_scope import MemoryScope

#: Scripted MUTATING operations the control probes. Each must return the SAME
#: verdict+reason with vs. without the candidate injected. Mutating ops require a
#: grant; a memory fact must never turn one into ``allow``.
_PROBE_TOOLS: tuple[str, ...] = ("write_file", "patch_file", "create_pr", "run_bash")


def _req(tool_name: str) -> OperationRequest:
    return OperationRequest(
        adapter_namespace="chat", tool_name=tool_name, schema_version="v1", raw_args={}
    )


def _fact_provenance_id(fact: Fact, scope: MemoryScope) -> str:
    """Seal the rendered candidate fact as an untrusted EPISODIC memory record —
    exactly how U6-7 would inject it — so the control tests the real injection path."""
    rec = prov.untrusted_record(
        prov.EPISODIC, "u6-l3-candidate", render_fact(fact),
        tenant_id=scope.tenant_id, visibility="tenant",
    )
    collector = ProvenanceCollector()
    collector.record(rec)
    sealed = collector.seal(SnapshotCache())
    return sealed.snapshot.snapshot_id


@dataclass(frozen=True, slots=True)
class MemorySafetyDecision:
    decision: str            # "promote" | "reject"
    reasons: tuple[str, ...]  # empty on promote; the diverging probes on reject

    @property
    def promoted(self) -> bool:
        return self.decision == "promote"


def action_influence_divergences(fact: Fact, scope: MemoryScope) -> tuple[str, ...]:
    """Return the probe tools whose verdict/reason CHANGED when the candidate fact
    was injected (empty tuple = fully action-inert = the control holds)."""
    if not isinstance(fact, Fact):
        raise TypeError("fact must be a validated Fact")
    if not isinstance(scope, MemoryScope):
        raise TypeError("scope must be a MemoryScope")
    ctx = execution_context.for_machine(
        service_name="sora", tenant_id=scope.tenant_id, request_id="u6-mem-safety"
    )
    injected = (_fact_provenance_id(fact, scope),)
    diverged: list[str] = []
    for tool in _PROBE_TOOLS:
        req = _req(tool)
        without = authorize_action(ctx, req, ())
        with_ = authorize_action(ctx, req, injected)
        if (without.verdict, without.reason) != (with_.verdict, with_.reason):
            diverged.append(tool)
    return tuple(diverged)


def evaluate_memory_safety(fact: Fact, scope: MemoryScope) -> MemorySafetyDecision:
    """Emit a real promote/reject decision for a candidate fact. Promote iff the
    action-influence negative control holds (the fact moves NO action verdict)."""
    diverged = action_influence_divergences(fact, scope)
    if diverged:
        return MemorySafetyDecision("reject", ("action_influence:" + ",".join(diverged),))
    return MemorySafetyDecision("promote", ())
