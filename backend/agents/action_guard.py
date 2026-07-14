"""U6-0 T7-0 shared action-guard core (dormant).

Single shared helper every T7 adapter calls before running a
model-chosen tool: computes the kernel verdict, resolves an enforcement
mode from a CLOSED ``(adapter_namespace, family)`` matrix, and returns a
:class:`GuardOutcome` telling the adapter whether to proceed.

Live call sites: the T7a ``nodes.py`` adapters (chat ``_run_tool_rounds``,
specialist ``tool_executor_node``, A2A ``external_agent_node``) and the
T7b runner-SDK chokepoint ``ToolDispatcher.execute``. Importing this
module adds two Prometheus counters to the exposition, which is expected.

Adapters must NEVER import ``authorize_action`` / ``OperationRequest``
directly — the kernel-reachability test in
``backend/tests/test_authorization_kernel.py`` enforces that this guard
module is the only legitimate kernel caller.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from backend import metrics
from backend.agents import tool_registry
from backend.agents.authorization_kernel import (
    AuthorizationDecision,
    OperationRequest,
    authorize_action,
)
from backend.agents.execution_context import ExecutionContext, for_unbound
from backend.agents.provenance import audit_ids

if TYPE_CHECKING:
    from backend.agents.provenance import TurnProvenance

logger = logging.getLogger(__name__)

# ── Closed vocabularies ────────────────────────────────────────────────
# Frozen T7 adapter map: T7a wires chat/specialist/a2a; T7b wires
# runner_sdk. A namespace outside this enum fails closed at call time.
ADAPTER_NAMESPACES = frozenset({"chat", "specialist", "a2a", "runner_sdk"})

# DERIVED from the registry (single source of truth). __unknown_deny__
# is naturally absent: it is only resolve()'s synthetic fallback, never
# a metadata row.
KNOWN_FAMILIES = tool_registry.KNOWN_FAMILIES
# The guard recognizes the sentinel as a VALUE (resolve_mode rule 4 can
# see it via a kernel deny), but it is NOT configurable in the matrix.
FAMILY_VOCAB = KNOWN_FAMILIES | {"__unknown_deny__"}

GUARD_MODES = frozenset({"shadow", "enforce"})

_ENV_VAR = "OMNISIGHT_ACTION_GUARD_MODE"
_ERROR_FAMILY = "__error__"

_SHADOW_MAX_PAYLOAD_CHARS = 262_144  # 256 KiB cap on the synchronous path
_SHADOW_MAX_ARG_ITEMS = 1024  # bound the O(keys) scan
_SHADOW_KNOWN_ADAPTERS = frozenset({"chat", "specialist", "runner_sdk", "a2a"})


def _load_mode_matrix() -> dict[tuple[str, str], str]:
    """Parse ``OMNISIGHT_ACTION_GUARD_MODE`` into the mode matrix.

    Format: ``adapter:family=mode[,adapter:family=mode…]``. Empty/unset
    ⇒ empty matrix ⇒ all-shadow. Any malformed entry or unknown token
    raises ValueError naming the offending entry — a typo'd flip must be
    loud at startup, never a silent no-op.
    """
    raw = os.environ.get(_ENV_VAR, "").strip()
    matrix: dict[tuple[str, str], str] = {}
    if not raw:
        return matrix
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        key, sep, mode = entry.partition("=")
        adapter, colon, family = key.partition(":")
        if not sep or not colon:
            raise ValueError(
                f"{_ENV_VAR}: malformed entry {entry!r} "
                f"(expected adapter:family=mode)"
            )
        adapter, family, mode = adapter.strip(), family.strip(), mode.strip()
        if adapter not in ADAPTER_NAMESPACES:
            raise ValueError(
                f"{_ENV_VAR}: unknown adapter {adapter!r} in entry {entry!r} "
                f"(allowed: {sorted(ADAPTER_NAMESPACES)})"
            )
        if family not in KNOWN_FAMILIES:
            # NB: deliberately KNOWN_FAMILIES, not FAMILY_VOCAB — the
            # __unknown_deny__ sentinel is not a configurable key.
            raise ValueError(
                f"{_ENV_VAR}: unknown family {family!r} in entry {entry!r} "
                f"(allowed: {sorted(KNOWN_FAMILIES)})"
            )
        if mode not in GUARD_MODES:
            raise ValueError(
                f"{_ENV_VAR}: unknown mode {mode!r} in entry {entry!r} "
                f"(allowed: {sorted(GUARD_MODES)})"
            )
        matrix[(adapter, family)] = mode
    return matrix


_MODE_MATRIX: dict[tuple[str, str], str] = _load_mode_matrix()


def reload_mode_matrix_for_tests() -> None:
    """Re-read OMNISIGHT_ACTION_GUARD_MODE (tests monkeypatch the env)."""
    global _MODE_MATRIX
    _MODE_MATRIX = _load_mode_matrix()


def resolve_mode(adapter_namespace: str, family: str) -> str:
    """Call-time fail-closed mode resolution (frozen design).

    An unknown/typo key must NEVER silently default to shadow — that
    would be an enforcement bypass:

      1. unknown adapter ⇒ enforce.
      2. ``__error__`` (stage-1 error sentinel) ⇒ enforce if the matrix
         has ANY enforce entry for that adapter, else shadow — an error
         must block wherever that adapter is under any enforcement; in
         an all-shadow config it observes-and-proceeds.
      3. family outside the vocabulary ⇒ enforce.
      4. else ⇒ matrix lookup, default shadow.
    """
    if adapter_namespace not in ADAPTER_NAMESPACES:
        return "enforce"
    if family == _ERROR_FAMILY:
        adapter_has_enforce = any(
            mode == "enforce"
            for (adapter, _f), mode in _MODE_MATRIX.items()
            if adapter == adapter_namespace
        )
        return "enforce" if adapter_has_enforce else "shadow"
    if family not in FAMILY_VOCAB:
        return "enforce"
    return _MODE_MATRIX.get((adapter_namespace, family), "shadow")


@dataclass(frozen=True)
class GuardOutcome:
    """What the guard tells an adapter about one model tool call.

    ``decision`` is Optional: None exactly on the stage-1 error path —
    the guard never fabricates a kernel verdict.
    """

    proceed: bool
    decision: AuthorizationDecision | None
    family: str
    mode: str
    blocked_reason: str | None = None
    error_reason: str | None = None


_FAILSAFE_OUTCOME = GuardOutcome(
    proceed=False,
    decision=None,
    family=_ERROR_FAMILY,
    mode="enforce",
    blocked_reason="guard_error",
    error_reason="guard_failsafe",
)


# blocked_reason is a BOUNDED enum for metric cardinality — never the
# raw kernel reason f-string (which embeds the family). Any FUTURE
# kernel deny-reason MUST get its own explicit mapping here; do not let
# it silently fall into the requires_grant bucket.
def _bounded_blocked_reason(kernel_reason: str) -> str:
    if kernel_reason == "unknown_tool_default_deny":
        return "unknown_tool"
    if kernel_reason == "unbound_principal_denied":
        return "unbound_principal"
    if kernel_reason.startswith("mutating_needs_grant:"):
        return "requires_grant"
    # Unmapped kernel reason = a mapping gap in THIS module; surface it
    # loudly as guard_error rather than mislabeling the block.
    logger.warning("action_guard: unmapped kernel reason %r", kernel_reason)
    return "guard_error"


def _shadow_adapter_label(adapter_namespace: object) -> str:
    """Normalize to a closed label set so metric cardinality stays bounded."""
    return (
        adapter_namespace
        if isinstance(adapter_namespace, str)
        and adapter_namespace in _SHADOW_KNOWN_ADAPTERS
        else "unknown"
    )


def _shadow_payload_too_large(raw_args) -> bool:
    """Skip unknown shapes, oversized strings, and excessive argument counts."""
    try:
        total = 0
        count = 0
        for _key, value in raw_args.items():
            count += 1
            if count > _SHADOW_MAX_ARG_ITEMS:
                return True
            if isinstance(value, str):
                total += len(value)
                if total >= _SHADOW_MAX_PAYLOAD_CHARS:
                    return True
        return False
    except Exception:  # noqa: BLE001 — unknown shape skips shadow telemetry
        return True


def _inc_shadow(
    adapter_namespace: object,
    coverage: str,
    refinement: str,
    would_verdict: str,
) -> None:
    metrics.action_guard_shadow_classification_total.labels(
        adapter=_shadow_adapter_label(adapter_namespace),
        coverage=coverage,
        refinement=refinement,
        would_verdict=would_verdict,
    ).inc()


def _emit_shadow_classification(
    adapter_namespace,
    tool_name,
    schema_version,
    raw_args,
    ctx,
) -> None:
    """Classify one canonical descriptor for isolated shadow telemetry."""
    if ctx is None:
        return
    if _shadow_payload_too_large(raw_args):
        _inc_shadow(adapter_namespace, "oversized", "n/a", "n/a")
        return

    from backend.agents.action_canonicalize import (
        CoverageStatus,
        canonicalize_if_registered,
        classify_refinement,
    )
    from backend.agents.authorization_kernel import classify_for_telemetry
    from backend.agents.shadow_context_resolver import resolve_shadow_context
    from backend.agents.tool_registry import resolve

    context = resolve_shadow_context(
        adapter_namespace,
        tool_name,
        schema_version,
    )
    if context is None:
        _inc_shadow(adapter_namespace, "no_context", "n/a", "n/a")
        return
    coverage = canonicalize_if_registered(
        context,
        adapter_namespace,
        tool_name,
        schema_version,
        raw_args,
    )
    if coverage.status is CoverageStatus.UNREGISTERED:
        _inc_shadow(adapter_namespace, "unregistered", "n/a", "n/a")
        return
    if coverage.status is CoverageStatus.ERROR:
        _inc_shadow(adapter_namespace, "error", "n/a", "n/a")
        return
    assert coverage.prepared is not None
    descriptor = coverage.prepared.operation_descriptor
    refinement = classify_refinement(resolve(tool_name), descriptor)
    would = classify_for_telemetry(ctx, descriptor).would_verdict
    _inc_shadow(adapter_namespace, "prepared", refinement, would)


def guard_tool_dispatch(
    *,
    adapter_namespace: str,
    tool_name: str,
    raw_args: dict,
    execution_context: ExecutionContext | None,
    schema_version: str = "v1",
    turn_provenance: "TurnProvenance | None" = None,
) -> GuardOutcome:
    """Three-stage, telemetry-isolated dispatch guard.

    Stage 1 computes the verdict + proceed; stages 2 and 3 emit telemetry.
    Each stage has its own try/except: a stage-1 raise yields a
    well-formed ERROR outcome (fails-closed-IF-enforce), while a stage-2
    or stage-3 raise never alters the already-computed outcome. No ordinary
    Exception escapes (SystemExit/KeyboardInterrupt/GeneratorExit propagate);
    a total mode-resolution failure returns a fail-closed error outcome.
    """
    # ctx starts None so a stage-1 raise BEFORE the context is built
    # still lets stage 2 label authorization_source="unknown".
    ctx: ExecutionContext | None = None
    decision: AuthorizationDecision | None = None
    outcome = _FAILSAFE_OUTCOME

    # ── Stage 1: verdict + proceed ─────────────────────────────────────
    try:
        ctx = (
            execution_context
            if execution_context is not None
            else for_unbound()
        )
        _ids = audit_ids(turn_provenance) if turn_provenance is not None else ()
        decision = authorize_action(
            ctx,
            OperationRequest(
                adapter_namespace=adapter_namespace,
                tool_name=tool_name,
                schema_version=schema_version,
                raw_args=raw_args,
            ),
            provenance_snapshot_ids=_ids,
        )
        family = decision.operation_descriptor.family
        mode = resolve_mode(adapter_namespace, family)

        if decision.verdict == "allow":
            proceed = True
        elif (
            decision.verdict == "deny"
            and decision.reason == "unknown_tool_default_deny"
        ):
            # Locked decision #3: post-P-REG an unregistered tool was
            # never a legitimate model tool — deny even in shadow.
            # Dashboard NIT: under a default matrix this deny carries
            # mode="shadow" — the deny is mode-INDEPENDENT; the label
            # just reflects the resolved matrix value.
            proceed = False
        else:
            # deny/unbound_principal_denied or requires_grant: shadow
            # observes-and-proceeds; enforce blocks (T11 flips per key).
            proceed = mode != "enforce"

        blocked_reason = (
            _bounded_blocked_reason(decision.reason) if not proceed else None
        )
        outcome = GuardOutcome(
            proceed=proceed,
            decision=decision,
            family=family,
            mode=mode,
            blocked_reason=blocked_reason,
        )
    except Exception as exc:  # noqa: BLE001 — guard must never raise
        try:
            try:
                mode = resolve_mode(adapter_namespace, _ERROR_FAMILY)
            except Exception:  # noqa: BLE001 — recovery must fail closed
                mode = "enforce"
            proceed = mode != "enforce"
            outcome = GuardOutcome(
                proceed=proceed,
                decision=None,
                family=_ERROR_FAMILY,
                mode=mode,
                blocked_reason="guard_error" if not proceed else None,
                error_reason=repr(exc)[:200],
            )
        except Exception:  # noqa: BLE001 — recovery must never escape
            outcome = _FAILSAFE_OUTCOME

    # ── Stage 2: telemetry (never alters the outcome) ──────────────────
    try:
        metrics.action_guard_decision_total.labels(
            adapter=adapter_namespace,
            family=outcome.family,
            verdict=(
                outcome.decision.verdict
                if outcome.decision is not None
                else "error"
            ),
            authorization_source=(
                ctx.authorization_source if ctx is not None else "unknown"
            ),
            mode=outcome.mode,
        ).inc()
        if not outcome.proceed:
            metrics.action_guard_fail_closed_total.labels(
                adapter=adapter_namespace,
                reason=outcome.blocked_reason,
            ).inc()
        logger.info(
            "action_guard adapter=%s tool=%s family=%s mode=%s proceed=%s "
            "verdict=%s blocked_reason=%s error_reason=%s",
            adapter_namespace,
            tool_name,
            outcome.family,
            outcome.mode,
            outcome.proceed,
            outcome.decision.verdict if outcome.decision is not None else "error",
            outcome.blocked_reason,
            outcome.error_reason,
        )
    except Exception:  # noqa: BLE001 — telemetry must never alter outcome
        try:
            logger.exception(
                "action_guard telemetry failed (outcome unchanged) "
                "adapter=%s tool=%s",
                adapter_namespace,
                tool_name,
            )
        except Exception:  # noqa: BLE001 — failure logging must not escape
            pass

    # ── Stage 3: SHADOW canonical telemetry (G6.3b; never alters outcome) ──
    try:
        if _CANONICAL_BOOTSTRAP_OK:
            _emit_shadow_classification(
                adapter_namespace,
                tool_name,
                schema_version,
                raw_args,
                ctx,
            )
    except Exception:  # noqa: BLE001 — shadow telemetry must never alter outcome
        try:
            logger.debug(
                "action_guard shadow telemetry failed (outcome unchanged) "
                "adapter=%s tool=%s",
                adapter_namespace,
                tool_name,
            )
        except Exception:  # noqa: BLE001 — failure logging must not escape
            pass

    return outcome


_CANONICAL_BOOTSTRAP_OK = False


def _bootstrap_canonicalizers() -> None:
    """Populate and freeze canonicalizers once per guard-module import.

    Fail-isolated: any ordinary exception (including a leaf import,
    registration, freeze, or logging failure) disables canonical telemetry
    without breaking guard import or process startup. Process workers derive
    the same registry independently. Nothing reads the registry until G6.3,
    so name-path guard decisions remain unchanged.
    """
    global _CANONICAL_BOOTSTRAP_OK
    try:
        from backend.agents import action_canonicalize
        from backend.agents.canonicalize_code_write_file import (
            register_code_write_file_canonicalizers,
        )

        register_code_write_file_canonicalizers()
        action_canonicalize.freeze_registry()
        _CANONICAL_BOOTSTRAP_OK = True
    except Exception as exc:  # noqa: BLE001 — guard import is fail-isolated
        _CANONICAL_BOOTSTRAP_OK = False
        try:
            logger.warning(
                "canonicalizer_bootstrap_failed; canonical telemetry "
                "disabled: %r",
                exc,
            )
        except Exception:  # noqa: BLE001 — logging cannot break import
            pass


try:
    _bootstrap_canonicalizers()
except Exception:  # noqa: BLE001 — last-resort module-import containment
    _CANONICAL_BOOTSTRAP_OK = False
