"""OP-2661 dormant authoritative-context resolver contract tests (offline)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.agents import authoritative_context_resolver, tools
from backend.agents.execution_context import (
    ExecutionContext,
    for_machine,
    for_service,
    for_unbound,
)
from backend.agents.shadow_root_registry import (
    freeze_registry,
    register_shadow_root,
    reset_for_tests,
)


_RUNNER_KEY = ("runner_sdk", "Write", "v1")
_SERVER_ID_RE = re.compile(r"^ws:[^:]+:(?:runner_sdk|specialist):[0-9a-f]{64}$")


@pytest.fixture(autouse=True)
def isolate_authoritative_context_state() -> Iterator[None]:
    """Prevent registry or active-workspace state from leaking between tests."""
    reset_for_tests()
    tools.set_active_workspace(None)
    yield
    tools.set_active_workspace(None)
    reset_for_tests()


def _bound_context(tenant_id: str = "t-1") -> ExecutionContext:
    return for_service(
        service_name="runner",
        tenant_id=tenant_id,
        request_id="r-1",
        roles=["operator"],
        authorization_source="runner_sdk",
    )


def _assert_server_workspace(result: tuple[str, str] | None) -> tuple[str, str]:
    assert result is not None
    workspace_id, workspace_root = result
    assert _SERVER_ID_RE.fullmatch(workspace_id)
    assert not workspace_id.startswith("shadow:")
    return workspace_id, workspace_root


def _expected_workspace_id(tenant: str, adapter: str, root: str) -> str:
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()
    return f"ws:{tenant}:{adapter}:{digest}"


def _resolve_frozen_runner_root(root: str) -> tuple[str, str]:
    reset_for_tests()
    register_shadow_root(*_RUNNER_KEY, workspace_root=root)
    freeze_registry()
    return _assert_server_workspace(
        authoritative_context_resolver.resolve_authoritative_workspace(
            _bound_context(),
            *_RUNNER_KEY,
        )
    )


def test_runner_sdk_returns_server_id_and_resolved_root(tmp_path: Path) -> None:
    registered_root = str(tmp_path / "base" / ".." / "workspace")
    register_shadow_root(*_RUNNER_KEY, workspace_root=registered_root)
    freeze_registry()

    workspace_id, workspace_root = _assert_server_workspace(
        authoritative_context_resolver.resolve_authoritative_workspace(
            _bound_context(),
            *_RUNNER_KEY,
        )
    )

    expected_root = str(Path(registered_root).resolve())
    assert workspace_id == _expected_workspace_id(
        "t-1", "runner_sdk", expected_root
    )
    assert workspace_root == expected_root


def test_workspace_id_is_deterministic_and_changes_with_resolved_root(
    tmp_path: Path,
) -> None:
    first_id, first_root = _resolve_frozen_runner_root(str(tmp_path / "first"))
    repeated_id, repeated_root = _resolve_frozen_runner_root(
        str(tmp_path / "first")
    )
    different_id, different_root = _resolve_frozen_runner_root(
        str(tmp_path / "second")
    )

    assert (first_id, first_root) == (repeated_id, repeated_root)
    assert different_id != first_id
    assert different_root != first_root


def test_runner_sdk_unfrozen_and_unregistered_roots_return_none(
    tmp_path: Path,
) -> None:
    register_shadow_root(*_RUNNER_KEY, workspace_root=str(tmp_path))
    assert (
        authoritative_context_resolver.resolve_authoritative_workspace(
            _bound_context(),
            *_RUNNER_KEY,
        )
        is None
    )

    reset_for_tests()
    freeze_registry()
    assert (
        authoritative_context_resolver.resolve_authoritative_workspace(
            _bound_context(),
            *_RUNNER_KEY,
        )
        is None
    )


def test_specialist_returns_server_id_and_resolved_active_workspace(
    tmp_path: Path,
) -> None:
    active_workspace = tmp_path / "specialist" / ".." / "workspace"
    tools.set_active_workspace(active_workspace)

    workspace_id, workspace_root = _assert_server_workspace(
        authoritative_context_resolver.resolve_authoritative_workspace(
            _bound_context(),
            "specialist",
            "write_file",
            "v1",
        )
    )

    expected_root = str(active_workspace.resolve())
    assert workspace_id == _expected_workspace_id(
        "t-1", "specialist", expected_root
    )
    assert workspace_root == expected_root


@pytest.mark.parametrize(
    "dispatch_key",
    [
        ("", "Write", "v1"),
        ("runner_sdk", "", "v1"),
        ("runner_sdk", "Write", ""),
        ("specialist", "", "v1"),
        ("specialist", "write_file", ""),
    ],
)
def test_empty_dispatch_component_returns_none(
    dispatch_key: tuple[str, str, str],
) -> None:
    assert (
        authoritative_context_resolver.resolve_authoritative_workspace(
            _bound_context(),
            *dispatch_key,
        )
        is None
    )


@pytest.mark.parametrize("adapter_namespace", ["chat", "a2a", "unknown"])
def test_adapter_without_file_canonicalizer_returns_none(
    adapter_namespace: str,
) -> None:
    assert (
        authoritative_context_resolver.resolve_authoritative_workspace(
            _bound_context(),
            adapter_namespace,
            "tool",
            "v1",
        )
        is None
    )


@pytest.mark.parametrize(
    "execution_context",
    [
        for_unbound(),
        for_machine(service_name="runner", request_id="r-2"),
        _bound_context(tenant_id=""),
    ],
)
def test_unbound_or_empty_tenant_returns_none(
    execution_context: object,
    tmp_path: Path,
) -> None:
    register_shadow_root(*_RUNNER_KEY, workspace_root=str(tmp_path))
    freeze_registry()

    assert (
        authoritative_context_resolver.resolve_authoritative_workspace(
            execution_context,
            *_RUNNER_KEY,
        )
        is None
    )


def test_shadow_resolution_exception_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_shadow_resolution(*_args: str) -> None:
        raise RuntimeError("shadow resolution unavailable")

    monkeypatch.setattr(
        authoritative_context_resolver,
        "resolve_shadow_context",
        fail_shadow_resolution,
    )

    assert (
        authoritative_context_resolver.resolve_authoritative_workspace(
            _bound_context(),
            *_RUNNER_KEY,
        )
        is None
    )


@pytest.mark.parametrize(
    "dispatch_key",
    [
        (None, "Write", "v1"),
        ("runner_sdk", None, "v1"),
        ("runner_sdk", "Write", None),
        (1, "Write", "v1"),
    ],
)
def test_non_str_dispatch_component_returns_none(
    dispatch_key: tuple[object, object, object],
) -> None:
    assert (
        authoritative_context_resolver.resolve_authoritative_workspace(
            _bound_context(),
            *dispatch_key,
        )
        is None
    )


def test_shadow_workspace_id_is_never_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ShadowWithUnreadableId:
        workspace_root = str(tmp_path)

        @property
        def workspace_id(self) -> str:
            raise AssertionError("shadow workspace_id must not be read")

    monkeypatch.setattr(
        authoritative_context_resolver,
        "resolve_shadow_context",
        lambda *_args: ShadowWithUnreadableId(),
    )

    workspace_id, workspace_root = _assert_server_workspace(
        authoritative_context_resolver.resolve_authoritative_workspace(
            _bound_context(),
            *_RUNNER_KEY,
        )
    )

    expected_root = str(tmp_path.resolve())
    assert workspace_id == _expected_workspace_id(
        "t-1", "runner_sdk", expected_root
    )
    assert workspace_root == expected_root
