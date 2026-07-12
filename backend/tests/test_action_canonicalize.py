"""OP-2627 prepared-action canonicalization framework tests (offline)."""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import sys
from collections.abc import Iterator, Mapping

import pytest

from backend.agents import action_canonicalize
from backend.agents.action_canonicalize import (
    CanonicalizationError,
    PreparedAction,
    _registry_restore,
    _registry_snapshot,
    canonicalize,
    register_canonicalizer,
)
from backend.agents.tool_registry import OperationDescriptor


@pytest.fixture(autouse=True)
def restore_canonicalizer_registry() -> Iterator[None]:
    """Prevent registrations in one test from leaking into another."""
    snapshot = _registry_snapshot()
    try:
        yield
    finally:
        _registry_restore(snapshot)


def _prepared_action() -> PreparedAction:
    return PreparedAction(
        operation_descriptor=OperationDescriptor(
            tool_name="run_bash",
            effect="read_only",
            family="read_only",
        ),
        canonical_target="/workspace/input.txt",
        executable_args={"path": "/workspace/input.txt", "encoding": "utf-8"},
        human_rendering={"summary": "Read /workspace/input.txt"},
    )


def test_canonicalize_dispatches_and_returns_refined_prepared_action() -> None:
    raw_args: dict[str, object] = {"path": "input.txt"}

    def fake_canonicalizer(args: Mapping[str, object]) -> PreparedAction:
        assert args is raw_args
        return _prepared_action()

    register_canonicalizer("builtin", "run_bash", "v1", fake_canonicalizer)

    result = canonicalize("builtin", "run_bash", "v1", raw_args)

    assert result.operation_descriptor.effect == "read_only"
    assert result.operation_descriptor.family == "read_only"
    assert result.executable_args == {
        "path": "/workspace/input.txt",
        "encoding": "utf-8",
    }
    assert result.executable_args is not raw_args
    assert raw_args == {"path": "input.txt"}


def test_canonicalize_unregistered_key_fails_closed() -> None:
    with pytest.raises(CanonicalizationError) as caught:
        canonicalize("builtin", "unknown", "v1", {})

    assert caught.value.reason.startswith("no_canonicalizer:")
    assert caught.value.reason == "no_canonicalizer:builtin/unknown@v1"


def test_canonicalize_wraps_canonicalizer_exception_with_cause() -> None:
    class RawCanonicalizerError(Exception):
        pass

    def failing_canonicalizer(_args: Mapping[str, object]) -> PreparedAction:
        raise RawCanonicalizerError("sensitive raw failure")

    register_canonicalizer("builtin", "run_bash", "v1", failing_canonicalizer)

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize("builtin", "run_bash", "v1", {})

    assert caught.value.reason == "canonicalizer_failed:builtin/run_bash@v1"
    assert isinstance(caught.value.__cause__, RawCanonicalizerError)


def test_canonicalize_requires_exact_registry_key() -> None:
    register_canonicalizer(
        "builtin",
        "run_bash",
        "v1",
        lambda _args: _prepared_action(),
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize("builtin", "run_bash", "v2", {})

    assert caught.value.reason == "no_canonicalizer:builtin/run_bash@v2"


def test_register_canonicalizer_rejects_duplicate_key() -> None:
    register_canonicalizer(
        "builtin",
        "run_bash",
        "v1",
        lambda _args: _prepared_action(),
    )

    with pytest.raises(ValueError, match="duplicate canonicalizer"):
        register_canonicalizer(
            "builtin",
            "run_bash",
            "v1",
            lambda _args: _prepared_action(),
        )


def test_prepared_action_is_frozen() -> None:
    prepared = _prepared_action()

    with pytest.raises(dataclasses.FrozenInstanceError):
        prepared.canonical_target = "/workspace/other.txt"


def test_canonicalize_rejects_non_prepared_action_result() -> None:
    def invalid_canonicalizer(_args: Mapping[str, object]) -> PreparedAction:
        return object()  # type: ignore[return-value]

    register_canonicalizer("builtin", "run_bash", "v1", invalid_canonicalizer)

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize("builtin", "run_bash", "v1", {})

    assert caught.value.reason == "canonicalizer_failed:builtin/run_bash@v1"
    assert caught.value.__cause__ is None


def test_action_canonicalize_import_direction_is_stdlib_plus_tool_registry() -> None:
    source = pathlib.Path(action_canonicalize.__file__).read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    for module in imported:
        if module == "backend.agents.tool_registry":
            continue
        root_module = module.partition(".")[0]
        assert root_module in sys.stdlib_module_names, f"non-leaf import: {module}"
