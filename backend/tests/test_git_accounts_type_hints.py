"""OP-1229 — guard rail: every signature in ``backend.routers.git_accounts``
keeps a return annotation and fully-annotated parameters.

The router was missing return annotations on six endpoint coroutines and
two helpers carried a bare ``dict`` (instead of ``dict[str, Any]``).
This test pins that down so a future drive-by edit cannot quietly drop
annotations again — it walks the module AST and asserts every
``def`` / ``async def`` in the file has a return annotation and every
non-``self``/``cls`` parameter is annotated.
"""

from __future__ import annotations

import ast
import inspect

from backend.routers import git_accounts as _ga


def _module_source_tree() -> ast.Module:
    src = inspect.getsource(_ga)
    return ast.parse(src)


def test_every_function_has_return_annotation() -> None:
    tree = _module_source_tree()
    missing: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is None:
                missing.append(f"{node.name} (line {node.lineno})")
    assert not missing, (
        "git_accounts.py functions missing return annotations: "
        + ", ".join(missing)
    )


def test_every_parameter_is_annotated() -> None:
    tree = _module_source_tree()
    missing: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args:
                if arg.arg in {"self", "cls"}:
                    continue
                if arg.annotation is None:
                    missing.append(
                        f"{node.name}::{arg.arg} (line {node.lineno})"
                    )
    assert not missing, (
        "git_accounts.py parameters missing annotations: "
        + ", ".join(missing)
    )


def test_helpers_use_parameterised_dict() -> None:
    """Bare ``dict`` is technically valid but loses information.

    The two helpers (``_probe_token_for``, ``_probe_jira_token``) and the
    ``_classify_match`` parameter were tightened to ``dict[str, Any]``;
    this asserts no one regresses them back to bare ``dict``.
    """
    src = inspect.getsource(_ga)
    # Look for any "-> dict:" or "-> dict\n" return that isn't subscripted.
    # ast.parse + AST walk is more reliable than regex here.
    tree = ast.parse(src)
    bare: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            r = node.returns
            if isinstance(r, ast.Name) and r.id == "dict":
                bare.append(f"{node.name} return (line {node.lineno})")
            for arg in node.args.args:
                a = arg.annotation
                if isinstance(a, ast.Name) and a.id == "dict":
                    bare.append(
                        f"{node.name}::{arg.arg} (line {node.lineno})"
                    )
    assert not bare, (
        "bare ``dict`` annotations regressed in git_accounts.py: "
        + ", ".join(bare)
    )
