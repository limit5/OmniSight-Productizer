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

⚠ DORMANT. Nothing calls :func:`authorize_action` in this ticket. T7
wires the six adapters to invoke it before their handlers; T11 flips
enforcement. Add NO caller here — after this ticket
``grep -rn "authorize_action\\|OperationRequest\\|authorization_kernel"
backend/ --include=*.py`` returns only this module + its test.

⚠ Naming: this module exposes ``AuthorizationDecision`` — NOT ``Decision``.
``backend.decision_engine`` already defines an unrelated ``Decision``
dataclass; do not clash/shadow it.

The kernel resolves the tool via :func:`backend.agents.tool_registry.resolve`
itself; it does NOT accept a caller-supplied ``OperationDescriptor`` (an
adapter could otherwise lie about what a tool does).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from backend.agents.execution_context import ExecutionContext
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


def authorize_action(
    execution_context: ExecutionContext,
    request: OperationRequest,
    provenance_snapshot_ids: tuple[str, ...] = (),
) -> AuthorizationDecision:
    """Classify a model-side tool call into ``allow`` / ``requires_grant``
    / ``deny`` per frozen design §2.C.

    Check order is load-bearing:

      1. ``family == "__unknown_deny__"`` ⇒ ``deny``. Unknown tools ALSO
         carry ``effect="mutating"`` (see :func:`tool_registry.resolve`),
         so if the effect branch fired first they'd wrongly become
         ``requires_grant``. Family/unknown check MUST precede effect.
      2. ``effect == "read_only"`` ⇒ ``allow``.
      3. ``effect == "mutating"`` ⇒ ``requires_grant``. NEVER ``allow`` on
         this model-side path — that's the core §1 invariant.

    Principal-independent: does NOT branch on
    ``execution_context.principal_type``. Machine-principal /
    grant-matching logic lands with the grant flow (T9/T10). Here we are
    fail-closed for mutating operations regardless of principal type.
    """
    descriptor = resolve(request.tool_name)

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

    return AuthorizationDecision(
        verdict="requires_grant",
        operation_descriptor=descriptor,
        reason=f"mutating_needs_grant:{descriptor.family}",
        execution_context=execution_context,
        provenance_snapshot_ids=tuple(provenance_snapshot_ids),
    )
