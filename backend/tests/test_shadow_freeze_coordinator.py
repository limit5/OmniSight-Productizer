"""OP-2658 launcher shadow-root freeze-ordering tests (offline)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHERS = (
    "auto-runner-sdk.py",
    "scripts/run_s1_via_anthropic_sdk.py",
)


def _launcher_tree(relative_path: str) -> ast.Module:
    path = REPO_ROOT / relative_path
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports_aliased_shadow_freeze(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "backend.agents.shadow_root_registry"
        and any(
            alias.name == "freeze_registry"
            and alias.asname == "_freeze_shadow_roots"
            for alias in node.names
        )
        for node in ast.walk(tree)
    )


def _call_lines(tree: ast.Module, function_name: str) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == function_name
    ]


@pytest.mark.parametrize("relative_path", _LAUNCHERS)
def test_launcher_imports_freeze_alias_and_freezes_after_registration(
    relative_path: str,
) -> None:
    tree = _launcher_tree(relative_path)

    register_lines = _call_lines(tree, "register_runner_sdk_shadow_roots")
    freeze_lines = _call_lines(tree, "_freeze_shadow_roots")

    assert _imports_aliased_shadow_freeze(tree)
    assert len(register_lines) == 1
    assert len(freeze_lines) == 1
    assert register_lines[0] < freeze_lines[0]
