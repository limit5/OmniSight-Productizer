"""OP-2659 end-to-end shadow classification isolation tests (offline)."""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend import metrics
from backend.agents import (
    action_canonicalize,
    action_guard,
    authorization_kernel,
    canonicalize_code_write_file,
    shadow_context_resolver,
    shadow_root_registry,
    tool_registry,
)
from backend.agents.action_guard import GuardOutcome, guard_tool_dispatch
from backend.agents.execution_context import ExecutionContext, for_human
from backend.auth import User


@pytest.fixture(autouse=True)
def _isolate_shadow_state() -> Iterator[None]:
    """Restore every process-local registry and gate changed by these tests."""
    canonical_state = action_canonicalize._snapshot_state_for_tests()
    bootstrap_ok = action_guard._CANONICAL_BOOTSTRAP_OK
    metrics.reset_for_tests()
    action_canonicalize.reset_for_tests()
    shadow_root_registry.reset_for_tests()
    action_guard._CANONICAL_BOOTSTRAP_OK = True
    try:
        yield
    finally:
        shadow_root_registry.reset_for_tests()
        action_canonicalize._restore_state_for_tests(canonical_state)
        action_guard._CANONICAL_BOOTSTRAP_OK = bootstrap_ok


def _bound_context() -> ExecutionContext:
    return for_human(
        user=User(id="u1", email="u@x", name="U", role="operator"),
        tenant_id="t-default",
        session_id="s1",
        request_id="r1",
        message_id="m1",
        authorization_source="chat",
    )


def _guard(
    adapter_namespace: str,
    tool_name: str,
    raw_args: dict,
) -> GuardOutcome:
    return guard_tool_dispatch(
        adapter_namespace=adapter_namespace,
        tool_name=tool_name,
        raw_args=raw_args,
        execution_context=_bound_context(),
    )


def _outcome_signature(outcome: GuardOutcome) -> tuple[object, ...]:
    return (
        outcome.proceed,
        outcome.decision.verdict if outcome.decision is not None else None,
        outcome.family,
        outcome.mode,
        outcome.blocked_reason,
    )


def _metric_value(counter, **labels: str) -> float:
    samples = counter.collect()[0].samples
    return sum(
        sample.value
        for sample in samples
        if sample.name.endswith("_total") and sample.labels == labels
    )


def _all_metric_values(counter) -> float:
    return sum(
        sample.value
        for sample in counter.collect()[0].samples
        if sample.name.endswith("_total")
    )


def _enable_prepared_shadow(root: Path, *tool_names: str) -> None:
    canonicalize_code_write_file.register_code_write_file_canonicalizers()
    action_canonicalize.freeze_registry()
    for tool_name in tool_names:
        shadow_root_registry.register_shadow_root(
            "runner_sdk",
            tool_name,
            "v1",
            workspace_root=str(root),
        )
    shadow_root_registry.freeze_registry()


def test_metrics_reset_rebinds_shadow_counter() -> None:
    prior = metrics.action_guard_shadow_classification_total

    metrics.reset_for_tests()

    assert metrics.action_guard_shadow_classification_total is not prior


_FAULT_TARGETS = (
    (shadow_context_resolver, "resolve_shadow_context"),
    (action_canonicalize, "canonicalize_if_registered"),
    (authorization_kernel, "classify_for_telemetry"),
    (action_canonicalize, "classify_refinement"),
    (tool_registry, "resolve"),
    (action_guard, "_inc_shadow"),
)


@pytest.mark.parametrize(
    ("source_module", "attribute"),
    _FAULT_TARGETS,
    ids=(
        "resolve-shadow-context",
        "canonicalize",
        "classify-for-telemetry",
        "classify-refinement",
        "resolve-name",
        "increment-counter",
    ),
)
def test_each_shadow_failure_leaves_live_outcome_identical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_module: object,
    attribute: str,
) -> None:
    _enable_prepared_shadow(tmp_path, "Write")
    raw_args = {"file_path": str(tmp_path / "x.py"), "content": "x = 1\n"}
    working = _guard("runner_sdk", "Write", raw_args)

    def fail_shadow(*_args, **_kwargs):
        raise RuntimeError(f"shadow failure: {attribute}")

    monkeypatch.setattr(source_module, attribute, fail_shadow)

    failed = _guard("runner_sdk", "Write", raw_args)

    assert failed == working
    assert _outcome_signature(failed) == _outcome_signature(working)


def test_hostile_shadow_gate_leaves_live_outcome_identical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _enable_prepared_shadow(tmp_path, "Write")
    raw_args = {"file_path": str(tmp_path / "x.py"), "content": "x = 1\n"}
    working = _guard("runner_sdk", "Write", raw_args)

    class HostileGate:
        def __bool__(self) -> bool:
            raise RuntimeError("hostile gate")

    monkeypatch.setattr(action_guard, "_CANONICAL_BOOTSTRAP_OK", HostileGate())

    failed = _guard("runner_sdk", "Write", raw_args)

    assert failed == working
    assert _outcome_signature(failed) == _outcome_signature(working)


def test_shadow_failure_logging_cannot_change_live_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _enable_prepared_shadow(tmp_path, "Write")
    raw_args = {"file_path": str(tmp_path / "x.py"), "content": "x = 1\n"}
    working = _guard("runner_sdk", "Write", raw_args)

    def fail(*_args, **_kwargs):
        raise RuntimeError("shadow and logging failure")

    monkeypatch.setattr(
        shadow_context_resolver,
        "resolve_shadow_context",
        fail,
    )
    monkeypatch.setattr(action_guard.logger, "debug", fail)

    assert _guard("runner_sdk", "Write", raw_args) == working


def test_stage_two_metric_still_fires_when_stage_three_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _enable_prepared_shadow(tmp_path, "Write")

    def fail_resolution(*_args, **_kwargs):
        raise RuntimeError("shadow resolution failed")

    monkeypatch.setattr(
        shadow_context_resolver,
        "resolve_shadow_context",
        fail_resolution,
    )

    _guard(
        "runner_sdk",
        "Write",
        {"file_path": str(tmp_path / "x.py"), "content": "x = 1\n"},
    )

    assert _metric_value(
        metrics.action_guard_decision_total,
        adapter="runner_sdk",
        family="code_write",
        verdict="requires_grant",
        authorization_source="chat",
        mode="shadow",
    ) == 1


def test_prepared_shadow_metric_reports_same_requires_grant(
    tmp_path: Path,
) -> None:
    _enable_prepared_shadow(tmp_path, "Write")
    target = tmp_path / "prepared.py"

    _guard(
        "runner_sdk",
        "Write",
        {"file_path": str(target), "content": "x = 1\n"},
    )

    assert _metric_value(
        metrics.action_guard_shadow_classification_total,
        adapter="runner_sdk",
        coverage="prepared",
        refinement="same",
        would_verdict="requires_grant",
    ) == 1
    assert not target.exists()


def test_str_replace_view_reports_family_changed_allow(tmp_path: Path) -> None:
    _enable_prepared_shadow(tmp_path, "str_replace_based_edit_tool")

    _guard(
        "runner_sdk",
        "str_replace_based_edit_tool",
        {"command": "view", "path": str(tmp_path / "view.py")},
    )

    assert _metric_value(
        metrics.action_guard_shadow_classification_total,
        adapter="runner_sdk",
        coverage="prepared",
        refinement="family_changed",
        would_verdict="allow",
    ) == 1


@pytest.mark.parametrize(
    ("adapter_namespace", "tool_name"),
    (
        ("runner_sdk", "Write"),
        ("chat", "read_file"),
        ("a2a", "external_agent:peer"),
    ),
)
def test_no_context_is_reported_for_unready_or_unsupported_adapters(
    adapter_namespace: str,
    tool_name: str,
) -> None:
    _guard(adapter_namespace, tool_name, {})

    assert _metric_value(
        metrics.action_guard_shadow_classification_total,
        adapter=adapter_namespace,
        coverage="no_context",
        refinement="n/a",
        would_verdict="n/a",
    ) == 1


@pytest.mark.parametrize(
    "raw_args",
    (
        {"file_path": "x.py", "content": "x" * 262_144},
        {f"key-{index}": index for index in range(1025)},
    ),
    ids=("characters", "argument-count"),
)
def test_oversized_payload_skips_canonicalization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw_args: dict,
) -> None:
    _enable_prepared_shadow(tmp_path, "Write")
    canonicalize_calls = 0

    def record_canonicalize(*_args, **_kwargs):
        nonlocal canonicalize_calls
        canonicalize_calls += 1
        raise AssertionError("oversized payload reached canonicalization")

    monkeypatch.setattr(
        action_canonicalize,
        "canonicalize_if_registered",
        record_canonicalize,
    )

    _guard("runner_sdk", "Write", raw_args)

    assert canonicalize_calls == 0
    assert _metric_value(
        metrics.action_guard_shadow_classification_total,
        adapter="runner_sdk",
        coverage="oversized",
        refinement="n/a",
        would_verdict="n/a",
    ) == 1


def test_unknown_adapter_is_normalized_to_bounded_metric_label() -> None:
    _guard("arbitrary-adapter", "read_file", {})

    assert _metric_value(
        metrics.action_guard_shadow_classification_total,
        adapter="unknown",
        coverage="no_context",
        refinement="n/a",
        would_verdict="n/a",
    ) == 1


def test_unregistered_and_error_coverage_labels(tmp_path: Path) -> None:
    canonicalize_code_write_file.register_code_write_file_canonicalizers()
    action_canonicalize.freeze_registry()
    for tool_name in ("NoCanonicalizer", "Write"):
        shadow_root_registry.register_shadow_root(
            "runner_sdk",
            tool_name,
            "v1",
            workspace_root=str(tmp_path),
        )
    shadow_root_registry.freeze_registry()

    _guard("runner_sdk", "NoCanonicalizer", {})
    _guard("runner_sdk", "Write", {})

    assert _metric_value(
        metrics.action_guard_shadow_classification_total,
        adapter="runner_sdk",
        coverage="unregistered",
        refinement="n/a",
        would_verdict="n/a",
    ) == 1
    assert _metric_value(
        metrics.action_guard_shadow_classification_total,
        adapter="runner_sdk",
        coverage="error",
        refinement="n/a",
        would_verdict="n/a",
    ) == 1


def test_gated_off_shadow_emits_no_metric_and_never_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_calls = 0

    def record_resolution(*_args, **_kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return None

    monkeypatch.setattr(
        shadow_context_resolver,
        "resolve_shadow_context",
        record_resolution,
    )
    action_guard._CANONICAL_BOOTSTRAP_OK = False

    _guard("runner_sdk", "Write", {})

    assert resolve_calls == 0
    assert _all_metric_values(
        metrics.action_guard_shadow_classification_total
    ) == 0


def test_shadow_helpers_have_closed_call_allowlist_and_forbidden_tokens() -> None:
    functions = (
        action_guard._emit_shadow_classification,
        action_guard._inc_shadow,
        action_guard._shadow_payload_too_large,
    )
    sources = [inspect.getsource(function) for function in functions]
    calls: set[str] = set()
    for source in sources:
        tree = ast.parse(textwrap.dedent(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)

    allowed = {
        "resolve_shadow_context",
        "canonicalize_if_registered",
        "classify_refinement",
        "classify_for_telemetry",
        "resolve",
        "_inc_shadow",
        "_shadow_payload_too_large",
        "_shadow_adapter_label",
        "labels",
        "inc",
        "items",
        "debug",
        "len",
        "isinstance",
        "str",
    }
    forbidden = {
        "authorize_action",
        "OperationRequest",
        "AuthorizationDecision",
        "classify_operation",
        "_issue_from_name",
        "executable_args",
        "human_rendering",
        "prepared_action_digest",
        "put_prepared_action",
        "execution_service",
        "digest",
    }

    assert calls <= allowed
    combined_source = "\n".join(sources)
    assert not {token for token in forbidden if token in combined_source}
