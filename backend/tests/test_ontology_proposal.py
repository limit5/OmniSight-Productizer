"""OP-1264 — type-hint coverage audit for ``backend/agents/ontology_proposal.py``.

The audit performed under OP-1264 confirmed every function signature
and every dataclass field in ``ontology_proposal.py`` already carries
an explicit annotation. The tests here lock that result in so a
future edit cannot silently regress the coverage:

  * :func:`test_all_function_signatures_annotated` — AST sweep of every
    top-level and nested ``def`` / ``async def``; asserts each arg
    (except ``self``/``cls``) and the return value carry an annotation.
  * :func:`test_all_dataclass_fields_annotated` — AST sweep of the
    three module dataclasses; asserts every class-body assignment is
    an ``AnnAssign`` (so dataclass field discovery cannot fall back
    to ``Any``).

Behaviour-level coverage for the gate already exists in
``backend/tests/test_cognee_ingestion.py`` (cases 6 and 7); the
audit-tests here intentionally do NOT duplicate it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "agents" / "ontology_proposal.py"
)

DATACLASS_NAMES = {"EntityClassSpec", "ProposalDecision", "OntologyProposalGate"}


def _module_tree() -> ast.Module:
    return ast.parse(MODULE_PATH.read_text())


def _all_functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def test_module_path_exists() -> None:
    assert MODULE_PATH.is_file(), f"target module missing: {MODULE_PATH}"


def test_all_function_signatures_annotated() -> None:
    tree = _module_tree()
    offenders: list[str] = []
    for func in _all_functions(tree):
        if func.returns is None:
            offenders.append(f"{func.name}@L{func.lineno}: missing return annotation")
        for arg in (*func.args.args, *func.args.kwonlyargs, *func.args.posonlyargs):
            if arg.arg in {"self", "cls"}:
                continue
            if arg.annotation is None:
                offenders.append(
                    f"{func.name}@L{func.lineno}: arg '{arg.arg}' missing annotation"
                )
        if func.args.vararg is not None and func.args.vararg.annotation is None:
            offenders.append(
                f"{func.name}@L{func.lineno}: *{func.args.vararg.arg} missing annotation"
            )
        if func.args.kwarg is not None and func.args.kwarg.annotation is None:
            offenders.append(
                f"{func.name}@L{func.lineno}: **{func.args.kwarg.arg} missing annotation"
            )
    assert not offenders, "uncovered signatures: " + "; ".join(offenders)


def test_all_dataclass_fields_annotated() -> None:
    tree = _module_tree()
    offenders: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name not in DATACLASS_NAMES:
            continue
        seen.add(node.name)
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                targets = ", ".join(
                    t.id for t in stmt.targets if isinstance(t, ast.Name)
                )
                offenders.append(
                    f"{node.name}@L{stmt.lineno}: bare Assign for '{targets}' "
                    "(dataclass field needs an annotation)"
                )
    missing_classes = DATACLASS_NAMES - seen
    assert not missing_classes, f"expected dataclasses not found: {missing_classes}"
    assert not offenders, "unannotated dataclass fields: " + "; ".join(offenders)


@pytest.mark.parametrize(
    "func_name,expected_return",
    [
        ("to_yaml_row", "dict[str, Any]"),
        ("load_ontology", "dict[str, EntityClassSpec]"),
        ("write_ontology", "None"),
        ("reset_week", "None"),
        ("propose", "ProposalDecision"),
        ("digest", "list[ProposalDecision]"),
    ],
)
def test_public_return_types_pinned(func_name: str, expected_return: str) -> None:
    """Pin the audited return types so a future widening to ``Any`` shows up in review."""
    tree = _module_tree()
    matches = [f for f in _all_functions(tree) if f.name == func_name]
    assert matches, f"function {func_name} not found in module"
    func = matches[0]
    assert func.returns is not None, f"{func_name}: return annotation missing"
    assert ast.unparse(func.returns) == expected_return, (
        f"{func_name}: return annotation drifted "
        f"(expected {expected_return!r}, got {ast.unparse(func.returns)!r})"
    )
