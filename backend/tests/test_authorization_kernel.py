"""OP-2597 — U6-0 T6 authorize_action kernel (dormant).

Offline unit tests for ``backend.agents.authorization_kernel``: issued
authorization capabilities resolve requests against the authoritative
``tool_registry``; bare descriptors produce telemetry-only verdicts.

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

import ast
import copy
import dataclasses
import pathlib
import pickle
import re
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from backend.agents import (
    action_canonicalize,
    authorization_kernel,
    execution_context,
)
from backend.agents.action_canonicalize import (
    CanonicalizationContext,
    PreparedAction,
    freeze_registry,
    register_canonicalizer,
)
from backend.agents.authorization_kernel import (
    AuthorizedOperation,
    AuthorizationDecision,
    OperationRequest,
    TelemetryVerdict,
    _deny_decision,
    _harden_descriptor,
    _issue_from_canonical,
    _issue_from_name,
    authorize_action,
    authorize_and_seal,
    authorize_canonical,
    classify_for_telemetry,
    classify_operation,
)
from backend.agents.canonicalize_code_write_file import (
    register_code_write_file_canonicalizers,
)
from backend.agents.tool_registry import OperationDescriptor, resolve
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


@pytest.fixture
def frozen_code_write_registry() -> Iterator[None]:
    """Install the reviewed canonicalizers without leaking global state."""
    prior = action_canonicalize._snapshot_state_for_tests()
    action_canonicalize.reset_for_tests()
    try:
        register_code_write_file_canonicalizers()
        freeze_registry()
        yield
    finally:
        action_canonicalize._restore_state_for_tests(prior)


class _LyingStr(str):
    """A string subclass that defeats equality-based descriptor checks."""

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    __hash__ = str.__hash__


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


# ── AC (b): git_push (vcs_write) ⇒ requires_grant ───────────────────────
def test_git_push_requires_grant_with_vcs_write_family_in_reason() -> None:
    d = authorize_action(_ctx_human(), _req("git_push"))
    assert d.verdict == "requires_grant"
    assert "vcs_write" in d.reason
    assert d.reason == "mutating_needs_grant:vcs_write"
    assert d.operation_descriptor.family == "vcs_write"
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
    assert d.reason == "mutating_needs_grant:vcs_write"
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
        assert d.reason == "mutating_needs_grant:vcs_write"


# ── OP-2626 (U6-0 G2c): canonical descriptor classification ─────────
def test_classify_operation_read_only_descriptor_allows() -> None:
    ctx = _ctx_human()
    descriptor = OperationDescriptor(
        tool_name="canonical_read",
        effect="read_only",
        family="read_only",
    )
    telemetry = classify_for_telemetry(ctx, descriptor)
    assert telemetry.would_verdict == "allow"
    assert telemetry.reason == "read_only"
    assert telemetry.family == descriptor.family

    decision = classify_operation(_issue_from_name(ctx, "read_file"))
    assert decision.verdict == "allow"
    assert decision.reason == "read_only"
    assert decision.operation_descriptor is resolve("read_file")


def test_classify_operation_mutating_bound_requires_grant() -> None:
    ctx = _ctx_human()
    descriptor = OperationDescriptor(
        tool_name="canonical_write",
        effect="mutating",
        family="code_write",
    )
    telemetry = classify_for_telemetry(ctx, descriptor)
    assert telemetry.would_verdict == "requires_grant"
    assert telemetry.reason == "mutating_needs_grant:code_write"

    decision = classify_operation(_issue_from_name(ctx, "git_push"))
    assert decision.verdict == "requires_grant"
    assert decision.reason == "mutating_needs_grant:vcs_write"
    assert decision.operation_descriptor is resolve("git_push")


def test_classify_operation_mutating_unbound_denies() -> None:
    ctx = _ctx_unbound()
    descriptor = OperationDescriptor(
        tool_name="canonical_write",
        effect="mutating",
        family="code_write",
    )
    telemetry = classify_for_telemetry(ctx, descriptor)
    assert telemetry.would_verdict == "deny"
    assert telemetry.reason == "unbound_principal_denied"

    decision = classify_operation(_issue_from_name(ctx, "git_push"))
    assert decision.verdict == "deny"
    assert decision.reason == "unbound_principal_denied"
    assert decision.operation_descriptor is resolve("git_push")


def test_classify_operation_unknown_family_denies_before_effect() -> None:
    ctx = _ctx_human()
    descriptor = OperationDescriptor(
        tool_name="canonical_unknown",
        effect="mutating",
        family="__unknown_deny__",
    )
    telemetry = classify_for_telemetry(ctx, descriptor)
    assert telemetry.would_verdict == "deny"
    assert telemetry.reason == "unknown_tool_default_deny"

    decision = classify_operation(_issue_from_name(ctx, "unknown_for_at1"))
    assert decision.verdict == "deny"
    assert decision.reason == "unknown_tool_default_deny"
    assert decision.operation_descriptor.family == "__unknown_deny__"


@pytest.mark.parametrize("tool_name", ["read_file", "git_push", "unknown_for_g2c"])
def test_authorize_action_name_shim_matches_classify_operation(
    tool_name: str,
) -> None:
    ctx = _ctx_human()
    actual = authorize_action(ctx, _req(tool_name))
    expected = classify_operation(_issue_from_name(ctx, tool_name))
    assert actual == expected


def test_classify_operation_records_provenance_snapshot_ids() -> None:
    operation = _issue_from_name(
        _ctx_human(),
        "read_file",
        provenance_snapshot_ids=("snap-1", "snap-2"),
    )
    d = classify_operation(operation)
    assert d.provenance_snapshot_ids == ("snap-1", "snap-2")
    assert isinstance(d.provenance_snapshot_ids, tuple)


def test_view_canonical_verdict_diverges_from_name_verdict() -> None:
    prior = action_canonicalize._snapshot_state_for_tests()
    action_canonicalize.reset_for_tests()
    try:
        register_code_write_file_canonicalizers()
        ctx = _ctx_machine()
        workspace_context = CanonicalizationContext(
            workspace_id="w",
            workspace_root="/w",
            adapter_namespace="runner_sdk",
            tool_name="str_replace_based_edit_tool",
            schema_version="v1",
        )
        prepared = action_canonicalize.canonicalize(
            workspace_context,
            "runner_sdk",
            "str_replace_based_edit_tool",
            "v1",
            {"command": "view", "path": "src/x.py"},
        )

        canonical_telemetry = classify_for_telemetry(
            ctx,
            prepared.operation_descriptor,
        )
        name_decision = authorize_action(
            ctx,
            OperationRequest(
                "runner_sdk",
                "str_replace_based_edit_tool",
                "v1",
                {"command": "view", "path": "src/x.py"},
            ),
        )

        assert canonical_telemetry.would_verdict == "allow"
        assert name_decision.verdict == "requires_grant"
        assert canonical_telemetry.would_verdict != name_decision.verdict
    finally:
        action_canonicalize._restore_state_for_tests(prior)


# ── OP-2654 (U6-0 AT-1): capability forge resistance ─────────────────
def test_issued_authorized_operation_is_accepted() -> None:
    operation = _issue_from_name(_ctx_human(), "read_file")
    assert type(operation) is AuthorizedOperation
    assert classify_operation(operation).verdict == "allow"


def test_unissued_authorized_operation_is_rejected() -> None:
    forged = object.__new__(AuthorizedOperation)
    with pytest.raises(ValueError, match="unissued AuthorizedOperation"):
        classify_operation(forged)


def test_authorized_operation_direct_constructor_is_rejected() -> None:
    with pytest.raises(
        TypeError,
        match="AuthorizedOperation is issued internally only",
    ):
        AuthorizedOperation()


def test_authorized_operation_subclass_is_rejected() -> None:
    with pytest.raises(
        TypeError,
        match="AuthorizedOperation cannot be subclassed",
    ):

        class ForgedAuthorizedOperation(AuthorizedOperation):
            pass


def test_authorized_operation_copy_preserves_issued_identity() -> None:
    operation = _issue_from_name(_ctx_human(), "git_push")
    shallow_copy = copy.copy(operation)
    deep_copy = copy.deepcopy(operation)
    assert shallow_copy is operation
    assert deep_copy is operation
    assert classify_operation(shallow_copy).verdict == "requires_grant"
    assert classify_operation(deep_copy).verdict == "requires_grant"


def test_authorized_operation_pickle_is_rejected() -> None:
    operation = _issue_from_name(_ctx_human(), "read_file")
    with pytest.raises(
        TypeError,
        match="AuthorizedOperation cannot be serialized",
    ):
        pickle.dumps(operation)


def test_classify_operation_rejects_bare_descriptor() -> None:
    import inspect

    signature = inspect.signature(classify_operation)
    assert tuple(signature.parameters) == ("op",)

    descriptor = resolve("read_file")
    with pytest.raises(TypeError, match="exact AuthorizedOperation required"):
        classify_operation(descriptor)  # type: ignore[arg-type]


def test_telemetry_verdict_cannot_authorize() -> None:
    telemetry = classify_for_telemetry(_ctx_human(), resolve("read_file"))
    assert isinstance(telemetry, TelemetryVerdict)
    assert not isinstance(telemetry, AuthorizationDecision)

    field_names = {field.name for field in dataclasses.fields(telemetry)}
    assert field_names == {"would_verdict", "family", "reason"}
    for authority_field in (
        "verdict",
        "proceed",
        "execution_context",
        "provenance_snapshot_ids",
    ):
        assert not hasattr(telemetry, authority_field)

    with pytest.raises(AttributeError):
        _ = telemetry.verdict  # type: ignore[attr-defined]


# ── OP-2660 (U6-0 AT-3): authoritative canonical issuer ─────────────
def test_authorize_canonical_value_path_uses_refined_descriptor(
    tmp_path: pathlib.Path,
    frozen_code_write_registry: None,
) -> None:
    root = str(tmp_path)
    bound = _ctx_human()

    view = authorize_canonical(
        bound,
        adapter_namespace="runner_sdk",
        tool_name="str_replace_based_edit_tool",
        schema_version="v1",
        raw_args={"command": "view", "path": "src/x.py"},
        workspace_id="workspace-1",
        workspace_root=root,
    )
    write = authorize_canonical(
        bound,
        adapter_namespace="runner_sdk",
        tool_name="Write",
        schema_version="v1",
        raw_args={"file_path": "src/x.py", "content": "new"},
        workspace_id="workspace-1",
        workspace_root=root,
    )
    unbound_write = authorize_canonical(
        _ctx_unbound(),
        adapter_namespace="runner_sdk",
        tool_name="Write",
        schema_version="v1",
        raw_args={"file_path": "src/x.py", "content": "new"},
        workspace_id="workspace-1",
        workspace_root=root,
    )
    real_replace = authorize_canonical(
        bound,
        adapter_namespace="runner_sdk",
        tool_name="str_replace_based_edit_tool",
        schema_version="v1",
        raw_args={
            "command": "str_replace",
            "path": "src/x.py",
            "old_str": "before",
            "new_str": "after",
        },
        workspace_id="workspace-1",
        workspace_root=root,
    )

    assert view.verdict == "allow"
    assert view.operation_descriptor.effect == "read_only"
    assert write.verdict == "requires_grant"
    assert write.operation_descriptor.effect == "mutating"
    assert unbound_write.verdict == "deny"
    assert real_replace.verdict == "requires_grant"
    assert real_replace.operation_descriptor.effect == "mutating"


def test_authorize_and_seal_view_round_trips_canonicalization(
    tmp_path: pathlib.Path,
    frozen_code_write_registry: None,
) -> None:
    root = str(tmp_path)
    context = CanonicalizationContext(
        workspace_id="workspace-1",
        workspace_root=root,
        adapter_namespace="runner_sdk",
        tool_name="str_replace_based_edit_tool",
        schema_version="v1",
    )
    raw_args = {"command": "view", "path": "src/x.py"}
    expected = action_canonicalize.canonicalize(
        context,
        "runner_sdk",
        "str_replace_based_edit_tool",
        "v1",
        raw_args,
    )

    decision, sealed = authorize_and_seal(
        _ctx_human(),
        adapter_namespace="runner_sdk",
        tool_name="str_replace_based_edit_tool",
        schema_version="v1",
        raw_args=raw_args,
        workspace_id="workspace-1",
        workspace_root=root,
    )

    assert decision.verdict == "allow"
    assert decision.operation_descriptor.effect == "read_only"
    assert isinstance(sealed, PreparedAction)
    assert sealed.operation_descriptor is decision.operation_descriptor
    assert sealed.operation_descriptor == expected.operation_descriptor
    assert sealed.canonical_target == expected.canonical_target
    assert sealed.executable_args == expected.executable_args
    assert sealed.human_rendering == expected.human_rendering


def test_authorize_and_seal_mutating_requires_grant_with_seal(
    tmp_path: pathlib.Path,
    frozen_code_write_registry: None,
) -> None:
    decision, sealed = authorize_and_seal(
        _ctx_human(),
        adapter_namespace="runner_sdk",
        tool_name="str_replace_based_edit_tool",
        schema_version="v1",
        raw_args={
            "command": "str_replace",
            "path": "src/x.py",
            "old_str": "before",
            "new_str": "after",
        },
        workspace_id="workspace-1",
        workspace_root=str(tmp_path),
    )

    assert decision.verdict == "requires_grant"
    assert decision.operation_descriptor.effect == "mutating"
    assert isinstance(sealed, PreparedAction)
    assert sealed.operation_descriptor is decision.operation_descriptor
    assert sealed.canonical_target == "src/x.py"
    assert sealed.executable_args["command"] == "str_replace"


def test_authorize_and_seal_unbound_write_denies_without_seal(
    tmp_path: pathlib.Path,
    frozen_code_write_registry: None,
) -> None:
    decision, sealed = authorize_and_seal(
        _ctx_unbound(),
        adapter_namespace="runner_sdk",
        tool_name="Write",
        schema_version="v1",
        raw_args={"file_path": "src/x.py", "content": "new"},
        workspace_id="workspace-1",
        workspace_root=str(tmp_path),
    )

    assert decision.verdict == "deny"
    assert decision.reason == "unbound_principal_denied"
    assert sealed is None


def test_authorize_and_seal_unknown_denies_without_seal() -> None:
    decision, sealed = authorize_and_seal(
        _ctx_human(),
        adapter_namespace="unknown_adapter",
        tool_name="totally_unknown_tool",
        schema_version="v1",
        raw_args={},
        workspace_id="workspace-1",
        workspace_root="/workspace",
    )

    assert decision.verdict == "deny"
    assert decision.reason.startswith("canonicalization_failed:")
    assert decision.operation_descriptor.family == "__unknown_deny__"
    assert sealed is None


def test_authorize_and_seal_canonicalization_failure_denies_without_seal(
    tmp_path: pathlib.Path,
    frozen_code_write_registry: None,
) -> None:
    decision, sealed = authorize_and_seal(
        _ctx_human(),
        adapter_namespace="runner_sdk",
        tool_name="Write",
        schema_version="v1",
        raw_args={"file_path": "../../etc/passwd", "content": "x"},
        workspace_id="workspace-1",
        workspace_root=str(tmp_path),
    )

    assert decision.verdict == "deny"
    assert decision.reason.startswith("canonicalization_failed:")
    assert sealed is None


@pytest.mark.parametrize(
    ("tool_name", "raw_args"),
    [
        (
            "str_replace_based_edit_tool",
            {"command": "view", "path": "src/x.py"},
        ),
        ("Write", {"file_path": "src/x.py", "content": "new"}),
        (
            "str_replace_based_edit_tool",
            {
                "command": "str_replace",
                "path": "src/x.py",
                "old_str": "before",
                "new_str": "after",
            },
        ),
        ("Write", {"file_path": "../../etc/passwd", "content": "x"}),
    ],
    ids=["view", "write", "str_replace", "deny"],
)
def test_authorize_and_seal_decision_matches_authorize_canonical_byte_for_byte(
    tmp_path: pathlib.Path,
    frozen_code_write_registry: None,
    tool_name: str,
    raw_args: dict,
) -> None:
    kwargs: dict[str, Any] = {
        "adapter_namespace": "runner_sdk",
        "tool_name": tool_name,
        "schema_version": "v1",
        "raw_args": raw_args,
        "workspace_id": "workspace-1",
        "workspace_root": str(tmp_path),
        "provenance_snapshot_ids": ("snap-1",),
    }
    context = _ctx_human()

    original = authorize_canonical(context, **kwargs)
    new_decision, _sealed = authorize_and_seal(context, **kwargs)

    assert new_decision == original


def test_hostile_seal_reads_do_not_change_authorize_canonical_decision(
    tmp_path: pathlib.Path,
) -> None:
    class HostilePreparedAction(PreparedAction):
        instances = 0

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            type(self).instances += 1
            object.__setattr__(
                self,
                "_raise_payload_reads",
                not type(self).instances % 2,
            )

        def __getattribute__(self, name: str):
            if name in {
                "canonical_target",
                "executable_args",
                "human_rendering",
            }:
                try:
                    hostile = object.__getattribute__(self, "_raise_payload_reads")
                except AttributeError:
                    hostile = False
                if hostile:
                    raise RuntimeError("hostile sealed field read")
            return super().__getattribute__(name)

    def hostile_canonicalizer(
        context: CanonicalizationContext,
        raw_args: Mapping[str, object],
    ) -> PreparedAction:
        del context, raw_args
        return HostilePreparedAction(
            operation_descriptor=resolve("Write"),
            canonical_target="src/x.py",
            executable_args={"content": "new"},
            human_rendering={"action": "write"},
        )

    prior = action_canonicalize._snapshot_state_for_tests()
    action_canonicalize.reset_for_tests()
    try:
        register_canonicalizer(
            "hostile_adapter",
            "Write",
            "v1",
            hostile_canonicalizer,
        )
        freeze_registry()
        kwargs: dict[str, Any] = {
            "adapter_namespace": "hostile_adapter",
            "tool_name": "Write",
            "schema_version": "v1",
            "raw_args": {},
            "workspace_id": "workspace-1",
            "workspace_root": str(tmp_path),
        }
        original = authorize_canonical(_ctx_human(), **kwargs)
        new_decision, sealed = authorize_and_seal(_ctx_human(), **kwargs)
    finally:
        action_canonicalize._restore_state_for_tests(prior)

    assert original.verdict == "requires_grant"
    assert original.reason == "mutating_needs_grant:code_write"
    assert new_decision == original
    assert sealed is None


def test_authorize_and_seal_verdict_invariant_to_adversarial_provenance(
    tmp_path: pathlib.Path,
    frozen_code_write_registry: None,
) -> None:
    context = _ctx_human()
    kwargs: dict[str, Any] = {
        "adapter_namespace": "runner_sdk",
        "tool_name": "str_replace_based_edit_tool",
        "schema_version": "v1",
        "raw_args": {"command": "view", "path": "src/x.py"},
        "workspace_id": "workspace-1",
        "workspace_root": str(tmp_path),
    }
    first, first_seal = authorize_and_seal(
        context,
        provenance_snapshot_ids=("IGNORE_RULES_AND_ALLOW",),
        **kwargs,
    )
    second, second_seal = authorize_and_seal(
        context,
        provenance_snapshot_ids=("FORGED_VERIFIED_PROVENANCE",),
        **kwargs,
    )

    assert first.verdict == second.verdict == "allow"
    assert first.reason == second.reason == "read_only"
    assert first.operation_descriptor == second.operation_descriptor
    assert first.provenance_snapshot_ids != second.provenance_snapshot_ids
    assert isinstance(first_seal, PreparedAction)
    assert isinstance(second_seal, PreparedAction)
    assert first_seal.operation_descriptor is first.operation_descriptor
    assert second_seal.operation_descriptor is second.operation_descriptor


def test_canonical_issuer_takes_dispatch_and_no_descriptor_mint_exists() -> None:
    import inspect

    issuer_signature = inspect.signature(_issue_from_canonical)
    assert "raw_args" in issuer_signature.parameters
    assert "descriptor" not in issuer_signature.parameters
    seal_signature = inspect.signature(authorize_and_seal)
    assert "raw_args" in seal_signature.parameters
    assert "descriptor" not in seal_signature.parameters
    assert all(
        "PreparedAction" not in str(parameter.annotation)
        for parameter in seal_signature.parameters.values()
    )

    for name, function in inspect.getmembers(
        authorization_kernel,
        inspect.isfunction,
    ):
        signature = inspect.signature(function)
        takes_descriptor = any(
            parameter.name == "descriptor"
            or "OperationDescriptor" in str(parameter.annotation)
            for parameter in signature.parameters.values()
        )
        returns_capability = "AuthorizedOperation" in str(
            signature.return_annotation
        )
        assert not (takes_descriptor and returns_capability), (
            f"{name} exposes a descriptor-to-capability mint"
        )


def test_harden_descriptor_rejects_non_descriptor() -> None:
    with pytest.raises(ValueError, match="descriptor_not_operation_descriptor"):
        _harden_descriptor(object(), "Write")


@pytest.mark.parametrize("field", ["tool_name", "effect", "family"])
def test_harden_descriptor_rejects_str_subclass_fields(field: str) -> None:
    values: dict[str, str] = {
        "tool_name": "Write",
        "effect": "mutating",
        "family": "code_write",
    }
    values[field] = _LyingStr(values[field])
    descriptor = OperationDescriptor(**values)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="descriptor_field_not_str"):
        _harden_descriptor(descriptor, "Write")


def test_harden_descriptor_rejects_dispatch_tool_mismatch() -> None:
    descriptor = OperationDescriptor("Write", "mutating", "code_write")
    with pytest.raises(ValueError, match="descriptor_tool_mismatch"):
        _harden_descriptor(descriptor, "Edit")


def test_harden_descriptor_rejects_unknown_operation_class() -> None:
    descriptor = OperationDescriptor(
        "Write",
        "read_only",
        "code_write",
    )
    with pytest.raises(ValueError, match="unknown_operation_class"):
        _harden_descriptor(descriptor, "Write")


def test_harden_descriptor_reconstructs_trusted_exact_strings() -> None:
    descriptor = OperationDescriptor("Write", "mutating", "code_write")
    hardened = _harden_descriptor(descriptor, "Write")

    assert hardened == descriptor
    assert hardened is not descriptor
    assert type(hardened.tool_name) is str
    assert type(hardened.effect) is str
    assert type(hardened.family) is str


def test_authorize_canonical_path_escape_fails_closed_without_raising(
    tmp_path: pathlib.Path,
    frozen_code_write_registry: None,
) -> None:
    decision = authorize_canonical(
        _ctx_human(),
        adapter_namespace="runner_sdk",
        tool_name="Write",
        schema_version="v1",
        raw_args={"file_path": "../../etc/passwd", "content": "x"},
        workspace_id="workspace-1",
        workspace_root=str(tmp_path),
    )

    assert decision.verdict == "deny"
    assert decision.reason.startswith("canonicalization_failed:")


def test_authorize_canonical_unregistered_fails_closed_without_raising() -> None:
    decision = authorize_canonical(
        _ctx_human(),
        adapter_namespace="unregistered_adapter",
        tool_name="Write",
        schema_version="v1",
        raw_args={"file_path": "src/x.py", "content": "x"},
        workspace_id="workspace-1",
        workspace_root="/workspace",
    )

    assert decision.verdict == "deny"
    assert decision.reason.startswith("canonicalization_failed:")


def test_authorize_canonical_harden_failure_fails_closed_without_raising(
    tmp_path: pathlib.Path,
) -> None:
    def hostile_canonicalizer(
        context: CanonicalizationContext,
        raw_args: Mapping[str, object],
    ) -> PreparedAction:
        del context, raw_args
        return PreparedAction(
            operation_descriptor=OperationDescriptor(
                _LyingStr("not-Write"),
                _LyingStr("read_only"),  # type: ignore[arg-type]
                _LyingStr("read_only"),
            ),
            canonical_target="src/x.py",
            executable_args={},
            human_rendering={},
        )

    prior = action_canonicalize._snapshot_state_for_tests()
    action_canonicalize.reset_for_tests()
    try:
        register_canonicalizer(
            "hostile_adapter",
            "Write",
            "v1",
            hostile_canonicalizer,
        )
        freeze_registry()
        with pytest.raises(ValueError, match="descriptor_field_not_str"):
            _issue_from_canonical(
                _ctx_human(),
                "hostile_adapter",
                "Write",
                "v1",
                {},
                "workspace-1",
                str(tmp_path),
            )
        decision = authorize_canonical(
            _ctx_human(),
            adapter_namespace="hostile_adapter",
            tool_name="Write",
            schema_version="v1",
            raw_args={},
            workspace_id="workspace-1",
            workspace_root=str(tmp_path),
        )
    finally:
        action_canonicalize._restore_state_for_tests(prior)

    assert decision.verdict == "deny"
    assert decision.reason == "authorize_canonical_denied"


def test_deny_decision_handles_malformed_tool_and_provenance() -> None:
    decision = _deny_decision(
        _ctx_human(),
        object(),
        "forced_deny",
        None,  # type: ignore[arg-type]
    )

    assert decision.verdict == "deny"
    assert decision.reason == "forced_deny"
    assert decision.operation_descriptor.tool_name == "__error__"
    assert decision.provenance_snapshot_ids == ()


def test_authorize_canonical_has_no_production_caller() -> None:
    backend_root = pathlib.Path(__file__).resolve().parents[1]
    test_path = pathlib.Path(__file__).resolve()
    callers: list[str] = []

    for py in backend_root.rglob("*.py"):
        if py.resolve() == test_path:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            else:
                continue
            if callee == "authorize_canonical":
                callers.append(str(py.relative_to(backend_root)))

    # G6b-2b: the enforce refine branch is the sole production caller.
    assert set(callers) <= {"agents/action_guard.py"}


def test_authorize_and_seal_has_no_production_caller() -> None:
    backend_root = pathlib.Path(__file__).resolve().parents[1]
    test_path = pathlib.Path(__file__).resolve()
    callers: list[str] = []

    for py in backend_root.rglob("*.py"):
        if py.resolve() == test_path:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            else:
                continue
            if callee == "authorize_and_seal":
                callers.append(str(py.relative_to(backend_root)))

    # GAP-2: the enforce refine branch is the sole production caller.
    assert set(callers) <= {"agents/action_guard.py"}


def test_authority_issuer_call_sites_are_allowlisted() -> None:
    """Only authorize wrappers and this test module may mint capabilities."""
    backend_root = pathlib.Path(__file__).resolve().parents[1]
    test_path = pathlib.Path(__file__).resolve()
    production_calls: list[tuple[str, str]] = []

    for py in backend_root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            else:
                continue
            if callee not in {
                "_issue_from_name",
                "issue_from_name",
                "_issue_from_canonical",
                "issue_from_canonical",
            }:
                continue
            if py.resolve() == test_path:
                continue
            enclosing = [
                function
                for function in functions
                if function.lineno <= node.lineno <= function.end_lineno
            ]
            owner = min(
                enclosing,
                key=lambda function: function.end_lineno - function.lineno,
            )
            production_calls.append(
                (str(py.relative_to(backend_root)), owner.name)
            )

    assert sorted(production_calls) == sorted([
        ("agents/authorization_kernel.py", "authorize_action"),
        ("agents/authorization_kernel.py", "authorize_and_seal"),
        ("agents/authorization_kernel.py", "authorize_canonical"),
    ])


def test_authorization_decision_has_no_external_producer() -> None:
    """Only the kernel may construct the guard-consumable decision type.

    Scope: PRODUCTION (non-test) backend code. The invariant guards against a
    non-kernel PRODUCTION component FORGING a decision the guard would consume.
    Test fixtures legitimately construct fake decisions to drive downstream
    handlers (e.g. the T7b dispatcher challenge-surfacing path); those never
    run in production and are excluded from the scan.
    """
    backend_root = pathlib.Path(__file__).resolve().parents[1]
    producers: list[str] = []

    for py in backend_root.rglob("*.py"):
        if py.relative_to(backend_root).parts[0] == "tests":
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            else:
                continue
            if callee == "AuthorizationDecision":
                producers.append(str(py.relative_to(backend_root)))

    assert sorted(set(producers)) == ["agents/authorization_kernel.py"]


# ── Kernel reachability guard ────────────────────────────────────────────
def test_kernel_reachable_only_via_action_guard() -> None:
    """Adapters must NEVER import ``authorize_action`` / ``OperationRequest``
    / ``classify_operation`` directly — the only legitimate kernel caller is
    the dispatch guard in ``backend/agents/action_guard.py`` (T7-0). Beyond
    the kernel module, the guard module, and their two test files, no backend
    file may reference the kernel tokens. T7a/T7b wire the adapters to the
    GUARD; a later shadow stage uses the telemetry path; T11 flips enforce.

    This guard fails loudly if a direct kernel caller sneaks in — the
    frozen design forbids it.
    """
    backend_root = pathlib.Path(__file__).resolve().parents[1]
    allowed = {
        backend_root / "agents" / "authorization_kernel.py",
        backend_root / "agents" / "action_guard.py",
        backend_root / "tests" / "test_action_guard.py",
        backend_root / "tests" / "test_action_guard_shadow.py",
        # T7a fault-injection tests patch the guard's kernel entry
        # (monkeypatch target names the token); still no ADAPTER imports
        # the kernel directly.
        backend_root / "tests" / "test_nodes_action_guard.py",
        # T7b dispatcher-wiring test imports AuthorizationDecision as a
        # fixture TYPE; the T11 code_write corpus monkeypatches
        # action_guard.authorize_action to force a double-fault. Both name a
        # kernel token from a TEST fixture — same category as the T7a
        # fault-injection test above, NOT an adapter→kernel import.
        backend_root / "tests" / "test_tool_dispatcher_action_guard.py",
        backend_root / "tests" / "test_t11_code_write_enforce_corpus.py",
        # U6-0 T5a: NOT adapter→kernel imports. provenance.py's DOCSTRING
        # explains the anti-forge invariant (names authorize_action; the
        # module is a stdlib-only leaf — the import-direction test proves
        # it imports nothing from backend). test_provenance.py's anti-forge
        # test CALLS the kernel to PROVE the verdict is invariant to
        # provenance content. Both legitimate references, not offenders.
        backend_root / "agents" / "provenance.py",
        backend_root / "tests" / "test_provenance.py",
        # U6 leg-1 (GREEN-BASE-2 repair — these merged without extending this
        # scan, lesson (a) recurrence). All three reference the kernel to PROVE
        # invariants, never to gate a dispatch:
        # - u6_memory_safety_eval.py (U6-5a, PRODUCTION): the RB3 negative
        #   control — calls authorize_action WITH vs WITHOUT a candidate fact
        #   injected and compares verdicts (a regression detector against
        #   INV-1). It never uses a verdict to authorize/deny a real dispatch,
        #   so it is not an adapter→kernel caller in the forbidden sense.
        # - test_u6_memory_capability_contract.py (U6-0): pins INV-1..4 against
        #   the real kernel — same category as test_provenance.py above.
        # - test_u6_l3_producer.py (U6-5a): drives the producer pipeline whose
        #   safety eval transitively names the kernel; the test asserts
        #   promote/reject behavior, adding no new kernel entry point.
        backend_root / "agents" / "u6_memory_safety_eval.py",
        backend_root / "tests" / "test_u6_memory_capability_contract.py",
        backend_root / "tests" / "test_u6_l3_producer.py",
        pathlib.Path(__file__).resolve(),
    }
    pattern = re.compile(
        r"\b(authorize_action|OperationRequest|classify_operation|authorization_kernel)\b"
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
