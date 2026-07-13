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

⚠ DORMANT. The T7-0 dispatch guard in
``backend/agents/action_guard.py`` calls the name-only
:func:`authorize_action` shim; adapters call the guard, never the kernel
directly. A future shadow stage may classify a bare canonical descriptor only
through :func:`classify_for_telemetry`; T11 flips enforcement. Add NO other
caller — the caller-scan test covers the kernel entry points.

⚠ Naming: this module exposes ``AuthorizationDecision`` — NOT ``Decision``.
``backend.decision_engine`` already defines an unrelated ``Decision``
dataclass; do not clash/shadow it.

The authorizing :func:`classify_operation` entry point requires an opaque,
process-local capability issued into the kernel's private weak ledger from a
tool name. A bare or canonical descriptor can reach only
:func:`classify_for_telemetry`, whose result is deliberately not an
``AuthorizationDecision`` and cannot authorize guard proceed.
"""

from __future__ import annotations

import threading
import weakref
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


@dataclass(frozen=True, slots=True)
class _AuthorizedPayload:
    descriptor: OperationDescriptor
    execution_context: ExecutionContext
    provenance_snapshot_ids: tuple[str, ...]


def _build_authority_boundary():
    ledger: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
    lock = threading.RLock()

    class AuthorizedOperation:
        """Opaque process-local authorization capability with no payload."""

        __slots__ = ("__weakref__",)

        def __new__(cls, *args, **kwargs):
            raise TypeError("AuthorizedOperation is issued internally only")

        def __init_subclass__(cls, **kwargs):
            raise TypeError("AuthorizedOperation cannot be subclassed")

        def __copy__(self):
            return self

        def __deepcopy__(self, memo):
            return self

        def __reduce__(self):
            raise TypeError("AuthorizedOperation cannot be serialized")

        def __reduce_ex__(self, protocol):
            raise TypeError("AuthorizedOperation cannot be serialized")

    def issue_from_name(
        execution_context: ExecutionContext,
        tool_name: str,
        provenance_snapshot_ids: tuple[str, ...] = (),
    ) -> AuthorizedOperation:
        """Issue the only AT-1 capability from a trusted name resolution."""
        descriptor = resolve(tool_name)
        operation = object.__new__(AuthorizedOperation)
        with lock:
            ledger[operation] = _AuthorizedPayload(
                descriptor,
                execution_context,
                tuple(provenance_snapshot_ids),
            )
        return operation

    def unwrap(operation) -> _AuthorizedPayload:
        if type(operation) is not AuthorizedOperation:
            raise TypeError("exact AuthorizedOperation required")
        with lock:
            try:
                return ledger[operation]
            except KeyError:
                raise ValueError("unissued AuthorizedOperation") from None

    return AuthorizedOperation, issue_from_name, unwrap


AuthorizedOperation, _issue_from_name, _unwrap_authorized = (
    _build_authority_boundary()
)


def _verdict_for(
    descriptor: OperationDescriptor,
    execution_context: ExecutionContext,
) -> tuple[Verdict, str]:
    """Return the shared policy verdict and reason for a descriptor.

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
        return "deny", "unknown_tool_default_deny"

    if descriptor.effect == "read_only":
        return "allow", "read_only"

    if is_unbound(execution_context):
        return "deny", "unbound_principal_denied"

    return "requires_grant", f"mutating_needs_grant:{descriptor.family}"


def classify_operation(
    op: AuthorizedOperation,  # type: ignore[valid-type]
) -> AuthorizationDecision:
    """Authorizing classifier; only an issued capability can reach policy."""
    payload = _unwrap_authorized(op)
    verdict, reason = _verdict_for(
        payload.descriptor,
        payload.execution_context,
    )

    return AuthorizationDecision(
        verdict=verdict,
        operation_descriptor=payload.descriptor,
        reason=reason,
        execution_context=payload.execution_context,
        provenance_snapshot_ids=payload.provenance_snapshot_ids,
    )


@dataclass(frozen=True)
class TelemetryVerdict:
    """Telemetry-only policy result, intentionally not decision-compatible."""

    would_verdict: Verdict
    family: str
    reason: str


def classify_for_telemetry(
    execution_context: ExecutionContext,
    descriptor: OperationDescriptor,
) -> TelemetryVerdict:
    """Classify a bare descriptor without producing authorization authority."""
    verdict, reason = _verdict_for(descriptor, execution_context)
    return TelemetryVerdict(
        would_verdict=verdict,
        family=descriptor.family,
        reason=reason,
    )


def authorize_action(
    execution_context: ExecutionContext,
    request: OperationRequest,
    provenance_snapshot_ids: tuple[str, ...] = (),
) -> AuthorizationDecision:
    """Name-only shim that issues internally before authorizing classify."""
    return classify_operation(
        _issue_from_name(
            execution_context,
            request.tool_name,
            provenance_snapshot_ids,
        )
    )
