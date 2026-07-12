"""OP-2597 — U6-0 T6 authorize_action kernel (dormant).

Offline unit tests for ``backend.agents.authorization_kernel``: pure
decision function that resolves each request against the authoritative
``tool_registry`` and classifies the resolved descriptor into
``allow`` / ``requires_grant`` / ``deny``.

Frozen design §1 core invariant exercised here: a mutating side effect
can never be authorized from the model-side kernel path — it only ever
reaches ``requires_grant`` (grant/challenge machinery is a LATER ticket).
The kernel is principal-independent EXCEPT an ``unbound`` principal (a
missing/whole-invalid identity), which hard-DENYs protected (mutating) ops
instead of ``requires_grant`` — case (f) below asserts that even a
machine-principal context still gets ``requires_grant`` for a mutating
tool, never ``allow``.

Kernel reachability (asserted programmatically in this file):
``grep -rn "authorize_action\\|OperationRequest\\|authorization_kernel"
backend/ --include=*.py`` returns ONLY the kernel module, the T7-0
dispatch guard (``backend/agents/action_guard.py``), and their tests.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

import pytest

from backend.agents import execution_context
from backend.agents.authorization_kernel import (
    AuthorizationDecision,
    OperationRequest,
    authorize_action,
)
from backend.auth import User


# ── Fixtures: real factory kwargs (frozen design §1 identity substrate) ──
def _ctx_human() -> execution_context.ExecutionContext:
    return execution_context.for_human(
        user=User(id="u1", email="u@x", name="U", role="operator"),
        tenant_id="t-default",
        session_id="s1",
        request_id="r1",
        message_id="m1",
        authorization_source="chat",
    )


def _ctx_machine() -> execution_context.ExecutionContext:
    return execution_context.for_machine(service_name="runner", request_id="r2")


def _ctx_unbound() -> execution_context.ExecutionContext:
    return execution_context.for_unbound()


def _req(tool_name: str) -> OperationRequest:
    return OperationRequest(
        adapter_namespace="chat",
        tool_name=tool_name,
        schema_version="v1",
        raw_args={},
    )


# ── AC (a): read_file (read_only) ⇒ allow ────────────────────────────────
def test_read_only_tool_is_allowed() -> None:
    ctx_h = _ctx_human()
    d = authorize_action(ctx_h, _req("read_file"))
    assert d.verdict == "allow"
    assert d.reason == "read_only"
    assert d.operation_descriptor.tool_name == "read_file"
    assert d.operation_descriptor.effect == "read_only"
    assert d.operation_descriptor.family == "read_only"
    # Context is carried through for later grant-matching + audit.
    assert d.execution_context is ctx_h


# ── AC (b): git_push (code_write) ⇒ requires_grant ───────────────────────
def test_git_push_requires_grant_with_code_write_family_in_reason() -> None:
    d = authorize_action(_ctx_human(), _req("git_push"))
    assert d.verdict == "requires_grant"
    assert "code_write" in d.reason
    assert d.reason == "mutating_needs_grant:code_write"
    assert d.operation_descriptor.family == "code_write"
    assert d.operation_descriptor.effect == "mutating"


# ── AC (c): save_solution (memory_write) ⇒ requires_grant ────────────────
def test_save_solution_requires_grant_with_memory_write_family_in_reason() -> None:
    d = authorize_action(_ctx_human(), _req("save_solution"))
    assert d.verdict == "requires_grant"
    assert "memory_write" in d.reason
    assert d.reason == "mutating_needs_grant:memory_write"
    assert d.operation_descriptor.family == "memory_write"


# ── AC (d): propose_action (dangerous_propose) ⇒ requires_grant ──────────
def test_propose_action_requires_grant_with_dangerous_propose_family_in_reason() -> None:
    d = authorize_action(_ctx_human(), _req("propose_action"))
    assert d.verdict == "requires_grant"
    assert "dangerous_propose" in d.reason
    assert d.reason == "mutating_needs_grant:dangerous_propose"
    assert d.operation_descriptor.family == "dangerous_propose"


# ── AC (e): unknown tool ⇒ deny (with unknown_tool_default_deny reason) ──
def test_unknown_tool_denies_with_unknown_tool_default_deny_reason() -> None:
    """The family/unknown check MUST precede the effect branch: unknown
    tools ALSO carry ``effect="mutating"`` (see :func:`tool_registry.resolve`),
    so if effect were checked first they'd wrongly become ``requires_grant``.
    """
    d = authorize_action(_ctx_human(), _req("totally_unknown_tool"))
    assert d.verdict == "deny"
    assert d.reason == "unknown_tool_default_deny"
    assert d.operation_descriptor.family == "__unknown_deny__"
    # Sanity: even though the unknown descriptor synthesised by the
    # registry has effect="mutating", the verdict is NOT requires_grant —
    # the family check fired first.
    assert d.operation_descriptor.effect == "mutating"
    assert d.verdict != "requires_grant"


# ── AC (f): machine principal + mutating ⇒ STILL requires_grant ──────────
def test_machine_principal_mutating_still_requires_grant_never_allow() -> None:
    """Fail-closed and principal-independent EXCEPT an ``unbound`` principal
    (a missing/whole-invalid identity), which hard-DENYs protected (mutating)
    ops instead of ``requires_grant``. A machine principal is BOUND, so a
    machine-principal ExecutionContext on a mutating operation MUST still
    classify to ``requires_grant`` — never ``allow``. Machine-principal /
    grant-matching branching lands with T9/T10.
    """
    ctx_m = _ctx_machine()
    d = authorize_action(ctx_m, _req("git_push"))
    assert d.verdict == "requires_grant"
    assert d.verdict != "allow"
    assert d.reason == "mutating_needs_grant:code_write"
    assert d.execution_context.principal_type == "machine"


# ── AC (g): AuthorizationDecision frozen ────────────────────────────────
def test_authorization_decision_is_frozen() -> None:
    d = authorize_action(_ctx_human(), _req("read_file"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.verdict = "deny"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.reason = "tampered"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.provenance_snapshot_ids = ("x",)  # type: ignore[misc]


# ── Bonus invariants (not literal AC lines but codify the design) ────────
def test_operation_request_is_frozen() -> None:
    r = _req("read_file")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.tool_name = "git_push"  # type: ignore[misc]


def test_provenance_snapshot_ids_default_empty_tuple() -> None:
    d = authorize_action(_ctx_human(), _req("read_file"))
    assert d.provenance_snapshot_ids == ()
    assert isinstance(d.provenance_snapshot_ids, tuple)


def test_provenance_snapshot_ids_are_recorded_when_supplied() -> None:
    """The provenance-snapshot MECHANISM is a later ticket, but the field
    must round-trip whatever the caller supplies — recorded for INV-3
    audit — as a tuple."""
    d = authorize_action(
        _ctx_human(),
        _req("read_file"),
        provenance_snapshot_ids=("snap-1", "snap-2"),
    )
    assert d.provenance_snapshot_ids == ("snap-1", "snap-2")
    assert isinstance(d.provenance_snapshot_ids, tuple)


def test_kernel_resolves_via_registry_not_caller_supplied_descriptor() -> None:
    """The kernel must resolve against the authoritative registry itself —
    not accept a caller-supplied descriptor. This is enforced by the
    ``authorize_action`` signature: it takes an ``OperationRequest`` (which
    only carries ``tool_name``) rather than an ``OperationDescriptor``.
    """
    import inspect

    sig = inspect.signature(authorize_action)
    param_annots = {p.name: p.annotation for p in sig.parameters.values()}
    # Must accept a request (raw), NOT a resolved descriptor.
    assert "request" in param_annots
    assert "OperationRequest" in str(param_annots["request"])
    # Must NOT take an OperationDescriptor as a parameter.
    for name, annot in param_annots.items():
        assert "OperationDescriptor" not in str(annot), (
            f"authorize_action must resolve internally; parameter {name!r} "
            f"({annot!r}) exposes an OperationDescriptor to the caller."
        )


def test_read_only_never_requires_grant_and_mutating_never_allows() -> None:
    """Spot the §1 invariant across a broad sample of tools."""
    ctx = _ctx_human()
    read_only_samples = ["read_file", "git_status", "WebSearch", "supervisor_ticket_detail"]
    for t in read_only_samples:
        d = authorize_action(ctx, _req(t))
        assert d.verdict == "allow", f"{t} should be allow but got {d.verdict}"

    mutating_samples = [
        "git_push",
        "save_solution",
        "propose_action",
        "gerrit_post_comment",
        "deploy_to_evk",
        "supervisor_comment_ticket",
        "Bash",
        "Agent",
    ]
    for t in mutating_samples:
        d = authorize_action(ctx, _req(t))
        assert d.verdict == "requires_grant", (
            f"{t} should be requires_grant but got {d.verdict}"
        )
        assert d.verdict != "allow"


# ── OP-2600 (U6-0 P-ID-A): unbound-hard-deny kernel branch ───────────────
def test_unbound_principal_mutating_is_hard_denied() -> None:
    """unbound + KNOWN mutating tool ⇒ deny — never requires_grant (so it
    can never structurally enter the T9/T10 grant-matching path), never
    allow."""
    d = authorize_action(_ctx_unbound(), _req("git_push"))
    assert d.verdict == "deny"
    assert d.reason == "unbound_principal_denied"
    assert d.verdict != "requires_grant"
    assert d.verdict != "allow"
    assert d.execution_context.principal_type == "unbound"


def test_unbound_principal_unknown_tool_keeps_unknown_deny_reason() -> None:
    """The family/unknown check precedence holds under unbound: unknown is
    denied for every principal with the unknown-tool reason; the
    unbound-specific reason applies only to KNOWN mutating tools."""
    d = authorize_action(_ctx_unbound(), _req("totally_unknown_tool"))
    assert d.verdict == "deny"
    assert d.reason == "unknown_tool_default_deny"


def test_unbound_principal_read_only_is_allowed() -> None:
    """Frozen §1 invariant: read_only always allows — a read has no side
    effect by the registry's classification. A missing context is surfaced
    by the guard's authorization_source="unbound" metric, not by denying
    reads."""
    d = authorize_action(_ctx_unbound(), _req("read_file"))
    assert d.verdict == "allow"
    assert d.reason == "read_only"


def test_bound_principals_mutating_still_requires_grant_regression() -> None:
    """Regression: the unbound branch changes NOTHING for bound principals —
    human/service/machine + git_push all still classify to requires_grant."""
    ctx_service = execution_context.for_service(
        service_name="runner",
        tenant_id="t-default",
        request_id="r3",
        roles=["operator"],
        authorization_source="a2a",
    )
    for ctx in (_ctx_human(), ctx_service, _ctx_machine()):
        d = authorize_action(ctx, _req("git_push"))
        assert d.verdict == "requires_grant", (
            f"{ctx.principal_type} should still be requires_grant, got {d.verdict}"
        )
        assert d.reason == "mutating_needs_grant:code_write"


# ── Kernel reachability guard ────────────────────────────────────────────
def test_kernel_reachable_only_via_action_guard() -> None:
    """Adapters must NEVER import ``authorize_action`` / ``OperationRequest``
    directly — the only legitimate kernel caller is the dispatch guard in
    ``backend/agents/action_guard.py`` (T7-0). Beyond the kernel module,
    the guard module, and their two test files, no backend file may
    reference the kernel tokens. T7a/T7b wire the adapters to the GUARD;
    T11 flips enforce.

    This guard fails loudly if a direct kernel caller sneaks in — the
    frozen design forbids it.
    """
    backend_root = pathlib.Path(__file__).resolve().parents[1]
    allowed = {
        backend_root / "agents" / "authorization_kernel.py",
        backend_root / "agents" / "action_guard.py",
        backend_root / "tests" / "test_action_guard.py",
        # T7a fault-injection tests patch the guard's kernel entry
        # (monkeypatch target names the token); still no ADAPTER imports
        # the kernel directly.
        backend_root / "tests" / "test_nodes_action_guard.py",
        # U6-0 T5a: NOT adapter→kernel imports. provenance.py's DOCSTRING
        # explains the anti-forge invariant (names authorize_action; the
        # module is a stdlib-only leaf — the import-direction test proves
        # it imports nothing from backend). test_provenance.py's anti-forge
        # test CALLS the kernel to PROVE the verdict is invariant to
        # provenance content. Both legitimate references, not offenders.
        backend_root / "agents" / "provenance.py",
        backend_root / "tests" / "test_provenance.py",
        pathlib.Path(__file__).resolve(),
    }
    pattern = re.compile(
        r"\b(authorize_action|OperationRequest|authorization_kernel)\b"
    )
    offenders: list[str] = []
    for py in backend_root.rglob("*.py"):
        if py.resolve() in allowed:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern.search(text):
            offenders.append(str(py.relative_to(backend_root)))
    assert not offenders, (
        f"authorize_action kernel must be dormant. Unexpected references: "
        f"{sorted(offenders)}"
    )
