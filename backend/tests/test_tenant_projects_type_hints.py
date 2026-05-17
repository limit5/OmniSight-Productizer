"""OP-1258 — guard rail: every signature in
``backend.routers.tenant_projects`` keeps a return annotation and fully-
annotated parameters.

The router was missing parameter annotations on the three ``_row_to_*_dict``
helpers (``row``) and on ``gc_archived_projects::now_utc``; those three
helpers also carried bare ``dict`` returns instead of ``dict[str, Any]``.
This test pins that down so a future drive-by edit cannot quietly drop
annotations again — it walks the module AST and asserts every
``def`` / ``async def`` in the file has a return annotation, every
non-``self``/``cls`` parameter is annotated, and no signature regresses
back to a bare ``dict``.
"""

from __future__ import annotations

import ast
import inspect

from backend.routers import tenant_projects as _tp


def _module_source_tree() -> ast.Module:
    src = inspect.getsource(_tp)
    return ast.parse(src)


def test_every_function_has_return_annotation() -> None:
    tree = _module_source_tree()
    missing: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is None:
                missing.append(f"{node.name} (line {node.lineno})")
    assert not missing, (
        "tenant_projects.py functions missing return annotations: "
        + ", ".join(missing)
    )


def test_every_parameter_is_annotated() -> None:
    tree = _module_source_tree()
    missing: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.kwonlyargs:
                if arg.arg in {"self", "cls"}:
                    continue
                if arg.annotation is None:
                    missing.append(
                        f"{node.name}::{arg.arg} (line {node.lineno})"
                    )
    assert not missing, (
        "tenant_projects.py parameters missing annotations: "
        + ", ".join(missing)
    )


def test_helpers_use_parameterised_dict() -> None:
    """Bare ``dict`` is technically valid but loses information.

    The three ``_row_to_*_dict`` helpers were tightened to
    ``dict[str, Any]``; this asserts no one regresses them back to bare
    ``dict``.
    """
    tree = _module_source_tree()
    bare: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            r = node.returns
            if isinstance(r, ast.Name) and r.id == "dict":
                bare.append(f"{node.name} return (line {node.lineno})")
            for arg in node.args.args + node.args.kwonlyargs:
                a = arg.annotation
                if isinstance(a, ast.Name) and a.id == "dict":
                    bare.append(
                        f"{node.name}::{arg.arg} (line {node.lineno})"
                    )
    assert not bare, (
        "bare ``dict`` annotations regressed in tenant_projects.py: "
        + ", ".join(bare)
    )
