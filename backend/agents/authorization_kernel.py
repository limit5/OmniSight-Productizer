"""U6-0 T6 authorize_action kernel (dormant).

Pure decision kernel that classifies a model-chosen tool invocation into
one of three verdicts: ``allow`` (only ever for read_only operations),
``requires_grant`` (every mutating operation — needs a server-issued grant
or independent human approval to actually run; the grant/challenge
machinery is a LATER ticket, T9/T10), or ``deny`` (unknown/unlisted tool,
default-deny).

Frozen design §1, §2.C. The core invariant: since the model / memory can
never MINT a grant, a mutating side effect can never be authorized from
the model path — it only ever reaches ``requires_grant``, never ``allow``.

⚠ DORMANT. No production adapter reaches the canonical classification
entry point yet. The T7-0 dispatch guard in
``backend/agents/action_guard.py`` still calls the name-only
:func:`authorize_action` shim; adapters call the guard, never the kernel
directly. G6 will switch the guard to canonicalize then call
:func:`classify_operation`; T11 flips enforcement. Add NO other caller —
the caller-scan test covers both kernel entry points.

⚠ Naming: this module exposes ``AuthorizationDecision`` — NOT ``Decision``.
``backend.decision_engine`` already defines an unrelated ``Decision``
dataclass; do not clash/shadow it.

The pure :func:`classify_operation` entry point trusts its descriptor. Its
ONLY legitimate producers are (a) :func:`backend.agents.tool_registry.resolve`
via the name-only :func:`authorize_action` shim and (b) the trusted server-side
canonicalizer, which derives from ``resolve`` plus argument inspection. No
adapter, model, or memory value may construct a descriptor or call
``classify_operation`` directly; adapters call the guard, preserving the
anti-forge boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from backend.agents.execution_context import ExecutionContext, is_unbound
from backend.agents.tool_registry import OperationDescriptor, resolve

Verdict = Literal["allow", "deny", "requires_grant"]


@dataclass(frozen=True)
class OperationRequest:
    """What an adapter hands the kernel — the raw model tool call.

    ``schema_version`` pins the wire shape of ``raw_args`` for later
    canonicalization (T6+ prepared-action refinement, e.g. splitting bash
    into read vs write). The kernel here does NOT inspect ``raw_args`` —
    classification is purely per-``tool_name``.
    """

    adapter_namespace: str
    tool_name: str
    schema_version: str
    raw_args: dict


@dataclass(frozen=True)
class AuthorizationDecision:
    """Kernel verdict for one ``OperationRequest`` under one
    ``ExecutionContext``.

    ``provenance_snapshot_ids`` is recorded for INV-3 audit; the
    provenance-snapshot MECHANISM is a separate ticket, and here it is
    just a passed-through field (default empty). ``execution_context`` is
    carried into the decision so a later grant-matching hop can bind the
    grant to the same principal the classification was made against.
    """

    verdict: Verdict
    operation_descriptor: OperationDescriptor
    reason: str
    execution_context: ExecutionContext
    provenance_snapshot_ids: tuple[str, ...] = field(default_factory=tuple)


def classify_operation(
    execution_context: ExecutionContext,
    descriptor: OperationDescriptor,
    provenance_snapshot_ids: tuple[str, ...] = (),
) -> AuthorizationDecision:
    """Classify a trusted canonical descriptor per frozen design §2.C.

    This pure function trusts ``descriptor``. The caller boundary must admit
    descriptors only from the name shim or trusted server-side canonicalizer,
    never from an adapter, model, or memory value.

    Check order is load-bearing:

      1. ``family == "__unknown_deny__"`` ⇒ ``deny``. Unknown tools ALSO
         carry ``effect="mutating"`` (see :func:`tool_registry.resolve`),
         so if the effect branch fired first they'd wrongly become
         ``requires_grant``. Family/unknown check MUST precede effect.
      2. ``effect == "read_only"`` ⇒ ``allow``.
      3. ``effect == "mutating"``: an ``unbound`` principal ⇒ ``deny``;
         otherwise ``requires_grant``. NEVER ``allow`` on this model-side
         path — that's the core §1 invariant.

    Principal-independent EXCEPT an ``unbound`` principal (a
    missing/whole-invalid identity), which hard-DENYs protected (mutating)
    ops instead of ``requires_grant``. Machine-principal / grant-matching
    logic lands with the grant flow (T9/T10). Here we are fail-closed for
    mutating operations regardless of principal type.
    """
    if descriptor.family == "__unknown_deny__":
        return AuthorizationDecision(
            verdict="deny",
            operation_descriptor=descriptor,
            reason="unknown_tool_default_deny",
            execution_context=execution_context,
            provenance_snapshot_ids=tuple(provenance_snapshot_ids),
        )

    if descriptor.effect == "read_only":
        return AuthorizationDecision(
            verdict="allow",
            operation_descriptor=descriptor,
            reason="read_only",
            execution_context=execution_context,
            provenance_snapshot_ids=tuple(provenance_snapshot_ids),
        )

    if is_unbound(execution_context):
        return AuthorizationDecision(
            verdict="deny",
            operation_descriptor=descriptor,
            reason="unbound_principal_denied",
            execution_context=execution_context,
            provenance_snapshot_ids=tuple(provenance_snapshot_ids),
        )

    return AuthorizationDecision(
        verdict="requires_grant",
        operation_descriptor=descriptor,
        reason=f"mutating_needs_grant:{descriptor.family}",
        execution_context=execution_context,
        provenance_snapshot_ids=tuple(provenance_snapshot_ids),
    )


def authorize_action(
    execution_context: ExecutionContext,
    request: OperationRequest,
    provenance_snapshot_ids: tuple[str, ...] = (),
) -> AuthorizationDecision:
    """Name-only compatibility shim that resolves before classification."""
    descriptor = resolve(request.tool_name)
    return classify_operation(
        execution_context,
        descriptor,
        provenance_snapshot_ids,
    )
