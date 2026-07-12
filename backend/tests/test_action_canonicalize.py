"""OP-2630 prepared-action canonicalization hardening tests (offline)."""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import sys
from collections.abc import Iterator, Mapping

import pytest

from backend.agents import action_canonicalize
from backend.agents.action_canonicalize import (
    CanonicalizationContext,
    CanonicalizationError,
    PreparedAction,
    _registry_restore,
    _registry_snapshot,
    canonicalize,
    register_canonicalizer,
)
from backend.agents.tool_registry import OperationDescriptor, resolve


_TOOL_NAME = "write_file"
_CONTEXT = CanonicalizationContext(
    workspace_id="workspace-1",
    workspace_root="/workspace",
    adapter_namespace="builtin",
)


@pytest.fixture(autouse=True)
def restore_canonicalizer_registry() -> Iterator[None]:
    """Prevent registrations in one test from leaking into another."""
    snapshot = _registry_snapshot()
    try:
        yield
    finally:
        _registry_restore(snapshot)


def _prepared_action(
    *,
    operation_descriptor: OperationDescriptor | None = None,
    executable_args: Mapping[str, object] | None = None,
    human_rendering: Mapping[str, object] | None = None,
) -> PreparedAction:
    return PreparedAction(
        operation_descriptor=operation_descriptor or resolve(_TOOL_NAME),
        canonical_target="/workspace/input.txt",
        executable_args=(
            executable_args
            if executable_args is not None
            else {"path": "/workspace/input.txt", "encoding": "utf-8"}
        ),
        human_rendering=(
            human_rendering
            if human_rendering is not None
            else {"summary": "Write /workspace/input.txt"}
        ),
    )


def test_canonicalize_dispatches_and_returns_prepared_action() -> None:
    raw_args: dict[str, object] = {"path": "input.txt"}

    def fake_canonicalizer(
        context: CanonicalizationContext,
        args: Mapping[str, object],
    ) -> PreparedAction:
        assert context is _CONTEXT
        assert args is raw_args
        return _prepared_action()

    register_canonicalizer("builtin", _TOOL_NAME, "v1", fake_canonicalizer)

    result = canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", raw_args)

    assert result.operation_descriptor == resolve(_TOOL_NAME)
    assert result.operation_descriptor.effect == "mutating"
    assert result.operation_descriptor.family == "code_write"
    assert result.executable_args == {
        "path": "/workspace/input.txt",
        "encoding": "utf-8",
    }
    assert result.executable_args is not raw_args
    assert raw_args == {"path": "input.txt"}


def test_canonicalize_unregistered_key_fails_closed() -> None:
    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", "unknown", "v1", {})

    assert caught.value.reason.startswith("no_canonicalizer:")
    assert caught.value.reason == "no_canonicalizer:builtin/unknown@v1"


def test_canonicalize_wraps_canonicalizer_exception_with_cause() -> None:
    class RawCanonicalizerError(Exception):
        pass

    def failing_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        raise RawCanonicalizerError("sensitive raw failure")

    register_canonicalizer("builtin", _TOOL_NAME, "v1", failing_canonicalizer)

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "canonicalizer_failed:builtin/write_file@v1"
    assert isinstance(caught.value.__cause__, RawCanonicalizerError)


def test_canonicalize_propagates_canonicalization_error() -> None:
    def failing_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        raise CanonicalizationError("leaf_rejected")

    register_canonicalizer("builtin", _TOOL_NAME, "v1", failing_canonicalizer)

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "leaf_rejected"


def test_canonicalize_requires_exact_registry_key() -> None:
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: _prepared_action(),
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v2", {})

    assert caught.value.reason == "no_canonicalizer:builtin/write_file@v2"


def test_register_canonicalizer_rejects_duplicate_key() -> None:
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: _prepared_action(),
    )

    with pytest.raises(ValueError, match="duplicate canonicalizer"):
        register_canonicalizer(
            "builtin",
            _TOOL_NAME,
            "v1",
            lambda _context, _args: _prepared_action(),
        )


def test_prepared_action_is_frozen() -> None:
    prepared = _prepared_action()

    with pytest.raises(dataclasses.FrozenInstanceError):
        prepared.canonical_target = "/workspace/other.txt"  # type: ignore[misc]


def test_canonicalization_context_is_frozen_and_requires_workspace() -> None:
    assert _CONTEXT.require_workspace() == "/workspace"

    with pytest.raises(dataclasses.FrozenInstanceError):
        _CONTEXT.workspace_root = "/other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("workspace_id", "workspace_root"),
    [("", "/workspace"), ("workspace-1", "")],
)
def test_canonicalization_context_require_workspace_fails_closed(
    workspace_id: str,
    workspace_root: str,
) -> None:
    context = CanonicalizationContext(
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        adapter_namespace="builtin",
    )

    with pytest.raises(CanonicalizationError) as caught:
        context.require_workspace()

    assert caught.value.reason == "no_workspace_context"


def test_canonicalize_deep_freezes_json_fields() -> None:
    lines = ["first"]
    rendering_parts = ["Write", "input.txt"]
    raw_args: dict[str, object] = {
        "path": "/workspace/input.txt",
        "lines": lines,
        "position": (1, 2),
    }
    rendering: dict[str, object] = {"parts": rendering_parts}

    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, args: _prepared_action(
            executable_args=args,
            human_rendering=rendering,
        ),
    )

    result = canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", raw_args)
    raw_args["path"] = "/workspace/other.txt"
    lines.append("second")
    rendering_parts.append("changed")

    assert result.executable_args == {
        "path": "/workspace/input.txt",
        "lines": ["first"],
        "position": [1, 2],
    }
    assert result.human_rendering == {"parts": ["Write", "input.txt"]}


@pytest.mark.parametrize("field_name", ["executable_args", "human_rendering"])
def test_canonicalize_rejects_non_serializable_json_fields(field_name: str) -> None:
    kwargs = {field_name: {"invalid": {"not-json"}}}
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: _prepared_action(**kwargs),  # type: ignore[arg-type]
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "non_serializable_args"


def test_canonicalize_rejects_descriptor_tool_mismatch() -> None:
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: _prepared_action(
            operation_descriptor=resolve("read_file")
        ),
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "descriptor_tool_mismatch"


def test_canonicalize_rejects_unregistered_descriptor_refinement() -> None:
    refined_descriptor = dataclasses.replace(
        resolve(_TOOL_NAME),
        effect="read_only",
        family="read_only",
    )
    register_canonicalizer(
        "builtin",
        _TOOL_NAME,
        "v1",
        lambda _context, _args: _prepared_action(
            operation_descriptor=refined_descriptor
        ),
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "descriptor_refinement_unregistered"


def test_canonicalize_rejects_non_prepared_action_result() -> None:
    def invalid_canonicalizer(
        _context: CanonicalizationContext,
        _args: Mapping[str, object],
    ) -> PreparedAction:
        return object()  # type: ignore[return-value]

    register_canonicalizer("builtin", _TOOL_NAME, "v1", invalid_canonicalizer)

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(_CONTEXT, "builtin", _TOOL_NAME, "v1", {})

    assert caught.value.reason == "canonicalizer_failed:builtin/write_file@v1"
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
