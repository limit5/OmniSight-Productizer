"""OP-2657 runner SDK shadow-root registration tests (offline)."""

from __future__ import annotations

import ast
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.agents import runner_handlers
from backend.agents.shadow_root_registry import (
    ShadowRootRegistryError,
    freeze_registry,
    register_shadow_root,
    reset_for_tests,
    resolve_shadow_root,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
_WRITE_KEY = ("runner_sdk", "Write", "v1")
_EDIT_KEY = ("runner_sdk", "Edit", "v1")
_STR_REPLACE_KEY = (
    "runner_sdk",
    "str_replace_based_edit_tool",
    "v1",
)


@pytest.fixture(autouse=True)
def isolate_shadow_root_registry() -> Iterator[None]:
    """Prevent roots, poison, or readiness from leaking between tests."""
    reset_for_tests()
    yield
    reset_for_tests()


def test_populate_freeze_resolves_all_runner_sdk_roots() -> None:
    runner_handlers.register_runner_sdk_shadow_roots(
        str_replace_root="/abs/wt"
    )

    freeze_registry()

    assert resolve_shadow_root(*_WRITE_KEY) == str(runner_handlers.BASE_DIR)
    assert resolve_shadow_root(*_EDIT_KEY) == str(runner_handlers.BASE_DIR)
    assert resolve_shadow_root(*_STR_REPLACE_KEY) == "/abs/wt"


def test_runner_sdk_roots_are_unreadable_before_freeze() -> None:
    runner_handlers.register_runner_sdk_shadow_roots(
        str_replace_root="/abs/wt"
    )

    assert resolve_shadow_root(*_WRITE_KEY) is None
    assert resolve_shadow_root(*_EDIT_KEY) is None
    assert resolve_shadow_root(*_STR_REPLACE_KEY) is None


def test_omitted_str_replace_registers_only_write_and_edit() -> None:
    runner_handlers.register_runner_sdk_shadow_roots()

    freeze_registry()

    assert resolve_shadow_root(*_WRITE_KEY) == str(runner_handlers.BASE_DIR)
    assert resolve_shadow_root(*_EDIT_KEY) == str(runner_handlers.BASE_DIR)
    assert resolve_shadow_root(*_STR_REPLACE_KEY) is None


def test_identical_runner_sdk_registration_is_idempotent() -> None:
    runner_handlers.register_runner_sdk_shadow_roots(
        str_replace_root="/abs/wt"
    )
    runner_handlers.register_runner_sdk_shadow_roots(
        str_replace_root="/abs/wt"
    )

    freeze_registry()

    assert resolve_shadow_root(*_WRITE_KEY) == str(runner_handlers.BASE_DIR)
    assert resolve_shadow_root(*_EDIT_KEY) == str(runner_handlers.BASE_DIR)
    assert resolve_shadow_root(*_STR_REPLACE_KEY) == "/abs/wt"


def test_poison_and_logging_failures_do_not_block_other_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_shadow_root(*_WRITE_KEY, workspace_root="/abs/a")
    with pytest.raises(
        ShadowRootRegistryError,
        match=r"^conflicting_shadow_root:runner_sdk/Write@v1$",
    ):
        register_shadow_root(*_WRITE_KEY, workspace_root="/abs/b")

    def fail_to_log(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("logging unavailable")

    monkeypatch.setattr(runner_handlers.logger, "warning", fail_to_log)
    runner_handlers.register_runner_sdk_shadow_roots(
        str_replace_root="/abs/wt"
    )
    freeze_registry()

    assert resolve_shadow_root(*_WRITE_KEY) is None
    assert resolve_shadow_root(*_EDIT_KEY) == str(runner_handlers.BASE_DIR)
    assert resolve_shadow_root(*_STR_REPLACE_KEY) == "/abs/wt"


def test_non_absolute_str_replace_root_is_swallowed() -> None:
    runner_handlers.register_runner_sdk_shadow_roots(str_replace_root=".")

    freeze_registry()

    assert resolve_shadow_root(*_WRITE_KEY) == str(runner_handlers.BASE_DIR)
    assert resolve_shadow_root(*_EDIT_KEY) == str(runner_handlers.BASE_DIR)
    assert resolve_shadow_root(*_STR_REPLACE_KEY) is None


def test_runner_handlers_base_dir_is_absolute() -> None:
    assert os.path.isabs(str(runner_handlers.BASE_DIR))


def _launcher_tree(relative_path: str) -> ast.Module:
    path = REPO_ROOT / relative_path
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports_shadow_root_helper(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "backend.agents.runner_handlers"
        and any(
            alias.name == "register_runner_sdk_shadow_roots"
            for alias in node.names
        )
        for node in ast.walk(tree)
    )


def _calls(tree: ast.Module, function_name: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == function_name
        for node in ast.walk(tree)
    )


def test_launcher_seams_import_call_and_capture_shadow_roots() -> None:
    auto_tree = _launcher_tree("auto-runner-sdk.py")
    run_s1_tree = _launcher_tree("scripts/run_s1_via_anthropic_sdk.py")

    for tree in (auto_tree, run_s1_tree):
        assert _imports_shadow_root_helper(tree)
        assert _calls(tree, "register_runner_sdk_shadow_roots")

    assert any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Tuple)
            and any(
                isinstance(element, ast.Name) and element.id == "text_editor"
                for element in target.elts
            )
            for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "bind_built_in_tools_with_static_analysis"
        for node in ast.walk(run_s1_tree)
    )
