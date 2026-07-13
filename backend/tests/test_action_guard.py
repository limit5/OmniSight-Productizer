"""OP-2603 — U6-0 T7-0 `guard_tool_dispatch` guard core (dormant).

Exercises the guard's full decision surface even though no production
adapter calls it yet: allow / shadow-observe / enforce-block /
unknown-tool mode-independent deny / unbound fallback / stage-1 error
isolation / stage-2 telemetry isolation / call-time fail-closed mode
resolution / startup matrix validation / metrics lockstep / dormancy.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from backend import metrics
from backend.agents import action_guard, execution_context, tool_registry
from backend.agents.action_guard import (
    ADAPTER_NAMESPACES,
    FAMILY_VOCAB,
    GUARD_MODES,
    KNOWN_FAMILIES,
    GuardOutcome,
    guard_tool_dispatch,
    reload_mode_matrix_for_tests,
    resolve_mode,
)
from backend.agents.provenance import (
    CaptureUnavailable,
    ModelSnapshot,
    ProvenanceCollector,
    SnapshotCache,
    audit_ids,
    for_model_response,
)
from backend.auth import User


@pytest.fixture(autouse=True)
def _clean_mode_matrix(monkeypatch: pytest.MonkeyPatch):
    """TEST-ISOLATION: pytest's monkeypatch restores the env at teardown
    but NOT the already-reloaded module-global matrix — without this
    reset an enforce entry from one test leaks into later "default
    shadow" tests (order-dependent failures)."""
    monkeypatch.delenv("OMNISIGHT_ACTION_GUARD_MODE", raising=False)
    reload_mode_matrix_for_tests()
    yield


def _set_matrix(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("OMNISIGHT_ACTION_GUARD_MODE", value)
    reload_mode_matrix_for_tests()


def _ctx_bound() -> execution_context.ExecutionContext:
    return execution_context.for_human(
        user=User(id="u1", email="u@x", name="U", role="operator"),
        tenant_id="t-default",
        session_id="s1",
        request_id="r1",
        message_id="m1",
        authorization_source="chat",
    )


def _guard(
    tool_name: str,
    *,
    adapter: str = "chat",
    ctx: execution_context.ExecutionContext | None = None,
) -> GuardOutcome:
    return guard_tool_dispatch(
        adapter_namespace=adapter,
        tool_name=tool_name,
        raw_args={},
        execution_context=ctx,
    )


# ── Vocabulary sanity ────────────────────────────────────────────────────
def test_vocabularies_are_closed_and_derived() -> None:
    assert ADAPTER_NAMESPACES == frozenset({"chat", "specialist", "a2a", "runner_sdk"})
    assert GUARD_MODES == frozenset({"shadow", "enforce"})
    # KNOWN_FAMILIES is DERIVED from the registry; the sentinel is only
    # in FAMILY_VOCAB, never a metadata row.
    assert KNOWN_FAMILIES is tool_registry.KNOWN_FAMILIES
    assert "__unknown_deny__" not in KNOWN_FAMILIES
    assert FAMILY_VOCAB == KNOWN_FAMILIES | {"__unknown_deny__"}
    assert "code_write" in KNOWN_FAMILIES
    assert "read_only" in KNOWN_FAMILIES


# ── 1. read_only + bound ctx ⇒ allow / proceed ───────────────────────────
def test_read_only_bound_ctx_proceeds_allow_shadow() -> None:
    out = _guard("read_file", ctx=_ctx_bound())
    assert out.proceed is True
    assert out.decision is not None
    assert out.decision.verdict == "allow"
    assert out.mode == "shadow"
    assert out.family == "read_only"
    assert out.blocked_reason is None
    assert out.error_reason is None


# ── 2. mutating + bound ctx, default shadow ⇒ observe-and-proceed ────────
def test_mutating_bound_ctx_default_shadow_observes_and_proceeds() -> None:
    out = _guard("git_push", ctx=_ctx_bound())
    assert out.proceed is True
    assert out.decision is not None
    assert out.decision.verdict == "requires_grant"
    assert out.mode == "shadow"
    assert out.blocked_reason is None


# ── 3. mutating + bound ctx, enforce entry ⇒ block ───────────────────────
def test_mutating_bound_ctx_enforce_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_matrix(monkeypatch, "chat:code_write=enforce")
    out = _guard("git_push", ctx=_ctx_bound())
    assert out.proceed is False
    assert out.mode == "enforce"
    assert out.decision is not None
    assert out.decision.verdict == "requires_grant"
    assert out.blocked_reason == "requires_grant"


# ── 4. unknown tool ⇒ deny in BOTH shadow AND enforce ────────────────────
def test_unknown_tool_denied_mode_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default shadow matrix: still denied (locked decision #3).
    out = _guard("totally_unknown_tool", ctx=_ctx_bound())
    assert out.proceed is False
    assert out.blocked_reason == "unknown_tool"
    assert out.decision is not None
    assert out.decision.verdict == "deny"
    assert out.family == "__unknown_deny__"
    # NB: mode label is "shadow" here (resolved matrix value); the deny
    # itself is mode-independent.
    assert out.mode == "shadow"

    # With enforcement present the outcome is the same block.
    _set_matrix(monkeypatch, "chat:code_write=enforce")
    out2 = _guard("totally_unknown_tool", ctx=_ctx_bound())
    assert out2.proceed is False
    assert out2.blocked_reason == "unknown_tool"


# ── 5. missing ctx ⇒ unbound fallback ────────────────────────────────────
def test_missing_ctx_falls_back_to_unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _guard("git_push", ctx=None)
    assert out.decision is not None
    assert out.decision.execution_context.principal_type == "unbound"
    assert out.decision.verdict == "deny"
    assert out.decision.reason == "unbound_principal_denied"
    # Default shadow: observe-and-proceed.
    assert out.proceed is True
    assert out.blocked_reason is None

    _set_matrix(monkeypatch, "chat:code_write=enforce")
    out2 = _guard("git_push", ctx=None)
    assert out2.proceed is False
    assert out2.blocked_reason == "unbound_principal"


def test_guard_derives_kernel_audit_ids_from_whole_turn_provenance() -> None:
    complete = for_model_response(ProvenanceCollector().seal(SnapshotCache()))
    assert isinstance(complete, ModelSnapshot)

    for turn_provenance in (
        complete,
        CaptureUnavailable("capture_failed"),
        None,
    ):
        out = guard_tool_dispatch(
            adapter_namespace="chat",
            tool_name="read_file",
            raw_args={},
            execution_context=_ctx_bound(),
            turn_provenance=turn_provenance,
        )
        assert out.decision is not None
        expected = audit_ids(turn_provenance) if turn_provenance is not None else ()
        assert out.decision.provenance_snapshot_ids == expected


# ── 6. stage-1 raise ⇒ well-formed error outcome, no escape ──────────────
def test_stage1_raise_yields_error_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a, **_kw):
        raise RuntimeError("kernel exploded")

    monkeypatch.setattr(action_guard, "authorize_action", _boom)

    # Default all-shadow: observes-and-proceeds.
    out = _guard("git_push", ctx=_ctx_bound())
    assert out.proceed is True
    assert out.decision is None
    assert out.family == "__error__"
    assert out.mode == "shadow"
    assert out.error_reason is not None
    assert "kernel exploded" in out.error_reason
    assert out.blocked_reason is None

    # ANY enforce entry for that adapter ⇒ the error blocks.
    _set_matrix(monkeypatch, "chat:code_write=enforce")
    out2 = _guard("git_push", ctx=_ctx_bound())
    assert out2.proceed is False
    assert out2.mode == "enforce"
    assert out2.blocked_reason == "guard_error"
    assert out2.decision is None


# ── 7. stage-2 raise ⇒ outcome unchanged, no escape ──────────────────────
def test_stage2_raise_never_alters_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = _guard("git_push", ctx=_ctx_bound())

    class _BoomCounter:
        def labels(self, *_a, **_kw):
            raise RuntimeError("telemetry exploded")

    monkeypatch.setattr(metrics, "action_guard_decision_total", _BoomCounter())
    monkeypatch.setattr(metrics, "action_guard_fail_closed_total", _BoomCounter())

    out = _guard("git_push", ctx=_ctx_bound())
    assert out.proceed == baseline.proceed
    assert out.decision is not None
    assert out.decision.verdict == baseline.decision.verdict
    assert out.family == baseline.family
    assert out.mode == baseline.mode
    assert out.error_reason is None


# ── 8. call-time fail-closed on unknown keys ─────────────────────────────
def test_unknown_adapter_fails_closed_for_mutating_only() -> None:
    out = _guard("git_push", adapter="bogus_adapter", ctx=_ctx_bound())
    assert out.mode == "enforce"
    assert out.proceed is False

    # allow is allow regardless of mode.
    out_ro = _guard("read_file", adapter="bogus_adapter", ctx=_ctx_bound())
    assert out_ro.mode == "enforce"
    assert out_ro.proceed is True

    # Rule 3 (family outside vocab) is unreachable via the dispatch
    # helper — kernel families are always in-vocab — cover it directly.
    assert resolve_mode("chat", "bogus_family") == "enforce"


# ── 9. startup validation is loud ────────────────────────────────────────
@pytest.mark.parametrize(
    "bad",
    [
        "chat:bogus_family=enforce",
        "bogus:read_only=shadow",
        "chat:code_write=weird",
        "chat:__unknown_deny__=enforce",
        "chatcode_write=enforce",
    ],
)
def test_malformed_matrix_entries_raise_value_error(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("OMNISIGHT_ACTION_GUARD_MODE", bad)
    with pytest.raises(ValueError):
        reload_mode_matrix_for_tests()


def test_valid_matrix_entry_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_matrix(monkeypatch, "chat:memory_write=enforce,runner_sdk:deploy=enforce")
    assert resolve_mode("chat", "memory_write") == "enforce"
    assert resolve_mode("runner_sdk", "deploy") == "enforce"
    assert resolve_mode("chat", "code_write") == "shadow"


# ── 10. metrics lockstep regression ──────────────────────────────────────
def test_metrics_reset_rebinds_both_guard_counters() -> None:
    metrics.reset_for_tests()
    assert hasattr(metrics, "action_guard_decision_total")
    assert hasattr(metrics, "action_guard_fail_closed_total")
    metrics.action_guard_decision_total.labels(
        adapter="chat",
        family="read_only",
        verdict="allow",
        authorization_source="chat",
        mode="shadow",
    ).inc()
    metrics.action_guard_fail_closed_total.labels(
        adapter="chat", reason="unknown_tool"
    ).inc()


# ── 11. guard callers are a declared, closed set ─────────────────────────
def test_action_guard_callers_are_declared() -> None:
    """U6-0 T7a retarget: the guard's callers are a CLOSED, declared set.

    T7a wired the nodes.py adapters (chat ``_run_tool_rounds`` + specialist
    ``tool_executor_node`` + the dormant A2A ``external_agent_node``); T7b
    will add ``tool_dispatcher.py``. Any OTHER caller must extend this
    allowlist in a reviewed change — the guard is the single kernel
    chokepoint T11 flips, so an undeclared caller is a wiring smell.
    """
    backend_root = pathlib.Path(__file__).resolve().parents[1]
    allowed = {
        backend_root / "agents" / "action_guard.py",
        backend_root / "agents" / "nodes.py",
        # T7b wired ToolDispatcher.execute (the runner-SDK chokepoint).
        backend_root / "agents" / "tool_dispatcher.py",
        backend_root / "tests" / "test_nodes_action_guard.py",
        # T7b test file names guard_tool_dispatch as a monkeypatch target.
        backend_root / "tests" / "test_tool_dispatcher_action_guard.py",
        # G0b plumbing tests record the guard/dispatcher whole-value kwargs.
        backend_root / "tests" / "test_provenance_chat_plumbing.py",
        backend_root / "tests" / "test_provenance_runner_plumbing.py",
        pathlib.Path(__file__).resolve(),
    }
    pattern = re.compile(r"\bguard_tool_dispatch\b")
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
        f"guard callers must be the declared set. Unexpected references: "
        f"{sorted(offenders)}"
    )
