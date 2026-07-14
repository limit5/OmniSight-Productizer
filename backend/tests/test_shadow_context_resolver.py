"""OP-2658 dormant shadow-context resolver contract tests (offline)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.agents import tools
from backend.agents.action_canonicalize import CanonicalizationContext
from backend.agents.shadow_context_resolver import resolve_shadow_context
from backend.agents.shadow_root_registry import (
    ShadowRootRegistryError,
    freeze_registry,
    register_shadow_root,
    reset_for_tests,
)


_RUNNER_KEY = ("runner_sdk", "Write", "v1")


@pytest.fixture(autouse=True)
def isolate_shadow_context_state() -> Iterator[None]:
    """Prevent registry or active-workspace state from leaking between tests."""
    reset_for_tests()
    tools.set_active_workspace(None)
    yield
    tools.set_active_workspace(None)
    reset_for_tests()


def test_runner_sdk_frozen_root_resolves_dispatch_bound_context() -> None:
    register_shadow_root(*_RUNNER_KEY, workspace_root="/abs/a")
    freeze_registry()

    context = resolve_shadow_context(*_RUNNER_KEY)

    assert isinstance(context, CanonicalizationContext)
    assert context.workspace_root == "/abs/a"
    assert context.adapter_namespace == "runner_sdk"
    assert context.tool_name == "Write"
    assert context.schema_version == "v1"
    assert context.workspace_id.startswith("shadow:")


def test_runner_sdk_unfrozen_root_returns_none() -> None:
    register_shadow_root(*_RUNNER_KEY, workspace_root="/abs/a")

    assert resolve_shadow_context(*_RUNNER_KEY) is None


def test_runner_sdk_unregistered_and_poisoned_roots_return_none() -> None:
    freeze_registry()
    assert resolve_shadow_context(*_RUNNER_KEY) is None

    reset_for_tests()
    register_shadow_root(*_RUNNER_KEY, workspace_root="/abs/a")
    with pytest.raises(
        ShadowRootRegistryError,
        match=r"^conflicting_shadow_root:runner_sdk/Write@v1$",
    ):
        register_shadow_root(*_RUNNER_KEY, workspace_root="/abs/b")
    freeze_registry()

    assert resolve_shadow_context(*_RUNNER_KEY) is None


def test_specialist_active_workspace_resolves_dispatch_bound_context() -> None:
    tools.set_active_workspace(Path("/abs/ws"))

    context = resolve_shadow_context("specialist", "write_file", "v1")

    assert isinstance(context, CanonicalizationContext)
    assert context.workspace_root == "/abs/ws"
    assert context.adapter_namespace == "specialist"
    assert context.tool_name == "write_file"
    assert context.schema_version == "v1"
    assert context.workspace_id.startswith("shadow:")


def test_specialist_without_active_workspace_mirrors_live_fallback() -> None:
    tools.set_active_workspace(None)

    context = resolve_shadow_context("specialist", "write_file", "v1")

    assert isinstance(context, CanonicalizationContext)
    assert context.workspace_root == str(tools.WORKSPACE_ROOT)


@pytest.mark.parametrize("adapter_namespace", ["chat", "a2a", "unknown"])
def test_adapter_without_file_canonicalizer_returns_none(
    adapter_namespace: str,
) -> None:
    assert resolve_shadow_context(adapter_namespace, "tool", "v1") is None


@pytest.mark.parametrize(
    "dispatch_key",
    [
        (None, "Write", "v1"),
        ("runner_sdk", None, "v1"),
        ("runner_sdk", "Write", None),
        (1, "Write", "v1"),
    ],
)
def test_non_str_dispatch_key_returns_none(
    dispatch_key: tuple[object, object, object],
) -> None:
    assert resolve_shadow_context(*dispatch_key) is None  # type: ignore[arg-type]


def test_specialist_workspace_resolution_swallows_ordinary_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_workspace_resolution() -> Path:
        raise RuntimeError("workspace unavailable")

    monkeypatch.setattr(tools, "get_active_workspace", fail_workspace_resolution)

    assert resolve_shadow_context("specialist", "write_file", "v1") is None
