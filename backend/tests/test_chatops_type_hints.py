"""OP-1275 — guard rail: every signature in ``backend.routers.chatops``
keeps a return annotation and fully-annotated parameters.

The router was missing parameter annotations on the three operator-protected
endpoint coroutines (``chatops_inject`` / ``chatops_send`` / ``pep_decision``);
their ``_user=Depends(_au.require_operator)`` arg carried no type. This test
pins that down so a future drive-by edit cannot quietly drop annotations
again — it walks the module AST and asserts every ``def`` / ``async def`` in
the file has a return annotation and every non-``self``/``cls`` parameter is
annotated. A third check guards against any helper regressing to a bare
``dict`` (the OP-1229 sibling-router pattern).
"""

from __future__ import annotations

import ast
import inspect

from backend.routers import chatops as _co


def _module_source_tree() -> ast.Module:
    src = inspect.getsource(_co)
    return ast.parse(src)


def test_every_function_has_return_annotation() -> None:
    tree = _module_source_tree()
    missing: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is None:
                missing.append(f"{node.name} (line {node.lineno})")
    assert not missing, (
        "chatops.py functions missing return annotations: "
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
        "chatops.py parameters missing annotations: "
        + ", ".join(missing)
    )


def test_no_bare_dict_in_signatures() -> None:
    """Bare ``dict`` is technically valid but loses information.

    The three operator-protected endpoints + SendRequest fields were
    tightened so no signature uses a bare ``dict``; this asserts no one
    regresses them back. We only walk function args / returns here — the
    Pydantic class-attribute annotations live in ``ast.AnnAssign`` and
    are checked by ``test_send_request_fields_parameterised`` below.
    """
    tree = _module_source_tree()
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
        "bare ``dict`` annotations regressed in chatops.py: "
        + ", ".join(bare)
    )


def test_send_request_fields_parameterised() -> None:
    """``SendRequest.buttons`` and ``SendRequest.meta`` were promoted from
    bare ``list[dict]`` / ``dict`` to fully parameterised forms — guard
    against regression on those Pydantic field annotations specifically.
    """
    tree = _module_source_tree()
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SendRequest":
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign):
                    continue
                if not isinstance(stmt.target, ast.Name):
                    continue
                name = stmt.target.id
                ann = stmt.annotation
                # Reject a bare ``dict`` annotation (e.g. ``meta: dict``).
                if isinstance(ann, ast.Name) and ann.id == "dict":
                    bad.append(f"SendRequest.{name} -> bare dict")
                # Reject ``list[dict]`` (inner type must be parameterised).
                if (
                    isinstance(ann, ast.Subscript)
                    and isinstance(ann.value, ast.Name)
                    and ann.value.id == "list"
                    and isinstance(ann.slice, ast.Name)
                    and ann.slice.id == "dict"
                ):
                    bad.append(f"SendRequest.{name} -> list[dict]")
    assert not bad, (
        "SendRequest fields regressed to bare dict: " + ", ".join(bad)
    )
