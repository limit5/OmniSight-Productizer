"""FX.7.2 — drift guard for the top-20 hoisted local imports.

Background
----------
The 2026-05-03 deep audit (`docs/audit/2026-05-03-deep-audit.md`,
DT36-DT45) reported "1768x local imports (function-內) 廣泛 circular
avoidance". FX.7.2 sampled the 20 most-repeated `(file, import-line)`
combinations and hoisted them to module level — collapsing 216 local-
import lines into 18 module-level imports without introducing any
import cycle (verified by clean import of all 18 modified modules).

Why a drift guard
-----------------
Function-internal imports are seductive: when a developer fights an
ImportError they reach for "just import it inside the function" as the
shortest path. That choice is sometimes correct (real circular
dependency) but most often it papers over a structural issue and
re-introduces the maintenance debt FX.7.2 just paid down.

This test pins the 20 hoisted imports: if anyone re-introduces the same
`from X import Y` as a local import inside any function in the listed
file, CI fails red with a pointer to FX.7.2 and the file:lineno of the
offending re-introduction. The author can either:

  (a) keep the import at module level (the hoisted form already exists
      — they don't need to add it again), and delete the local copy;
  (b) document a real circular-dep reason in the SKIP_REASONS dict
      below, with a one-line justification a future reader can audit.

The guard is intentionally narrow: it only checks the 20 (file, line)
pairs FX.7.2 touched. The broader 1500+ remaining local imports are
left alone — many are genuine circular-avoidance, some are heavy/
optional-dep guards, and a few are dead. Sampling 20 with a guard is
the FX.7.2 design (see `TODO.md` line for FX.7.2).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# (file, import-line) pairs that FX.7.2 hoisted out of function bodies.
# If any of these reappears as a function-internal import, the test
# fails — point the author at this list.
HOISTED_PAIRS: list[tuple[str, str]] = [
    ("backend/auth.py", "from backend.db_pool import get_pool"),
    ("backend/mfa.py", "from backend.db_pool import get_pool"),
    ("backend/routers/tenant_projects.py", "from backend.db_pool import get_pool"),
    ("backend/routers/auth.py", "from backend import audit as _audit"),
    ("backend/routers/system.py", "from backend import db"),
    ("backend/api_keys.py", "from backend.db_pool import get_pool"),
    ("backend/prompt_registry.py", "from backend.db_pool import get_pool"),
    ("backend/routers/tenant_projects.py", "from backend import audit as _audit"),
    ("backend/routers/catalog.py", "from backend.db_pool import get_pool"),
    ("backend/routers/installer.py", "from backend.db_pool import get_pool"),
    ("backend/workflow.py", "from backend.db_pool import get_pool"),
    ("backend/container.py", "from backend import metrics as _m"),
    ("backend/routers/admin_tenants.py", "from backend.db_pool import get_pool"),
    ("backend/dag_storage.py", "from backend.db_pool import get_pool"),
    ("backend/tenant_egress.py", "from backend.db_pool import get_pool"),
    ("backend/routers/auth.py", "from backend.security import auth_event as _aevent"),
    ("backend/routers/integration.py", "import json"),
    ("backend/routers/mfa.py", "from backend import audit as _audit"),
    ("backend/decision_engine.py", "from backend import audit as _audit"),
    ("backend/agents/tools.py", "from backend.db_pool import get_pool"),
]


# Escape hatch: if a (file, line) ever genuinely re-introduces a circular
# dependency that forces the local import back, document the reason here.
# Empty by design — adding an entry without a justification comment is
# itself a regression.
SKIP_REASONS: dict[tuple[str, str], str] = {}


def _import_node_matches(node: ast.AST, snippet: str) -> bool:
    """Return True if AST import node renders to the same text as snippet.

    We compare normalized textual form (whitespace-collapsed) so that
    `from backend.db_pool   import   get_pool` matches
    `from backend.db_pool import get_pool`. Aliases must match exactly
    (a different alias is a different intent and is allowed).
    """
    if not isinstance(node, (ast.Import, ast.ImportFrom)):
        return False
    rendered = ast.unparse(node)  # py3.9+
    return " ".join(rendered.split()) == " ".join(snippet.split())


def _function_local_imports(file_path: Path) -> list[tuple[int, ast.AST]]:
    """Return (lineno, import_node) for every function-internal import."""
    src = file_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: list[tuple[int, ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # walk only inside this function body, descending through if/try/
        # with/loop bodies but stopping at nested function defs.
        stack: list[ast.AST] = list(node.body)
        while stack:
            stmt = stack.pop()
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                out.append((stmt.lineno, stmt))
                continue
            for attr in ("body", "orelse", "finalbody"):
                v = getattr(stmt, attr, None)
                if isinstance(v, list):
                    stack.extend(v)
            handlers = getattr(stmt, "handlers", None)
            if handlers:
                for h in handlers:
                    stack.extend(getattr(h, "body", []) or [])
    return out


def test_hoisted_imports_remain_at_module_level() -> None:
    """The 20 FX.7.2-hoisted imports must not reappear as local imports."""
    regressions: list[str] = []
    for rel_path, snippet in HOISTED_PAIRS:
        if (rel_path, snippet) in SKIP_REASONS:
            continue
        file_path = REPO_ROOT / rel_path
        assert file_path.exists(), f"Hoisted file vanished: {rel_path}"
        for lineno, node in _function_local_imports(file_path):
            if _import_node_matches(node, snippet):
                regressions.append(
                    f"{rel_path}:{lineno}  re-introduces local "
                    f"`{snippet}` (FX.7.2 hoisted this to module level — "
                    f"delete the local copy or add a justified entry to "
                    f"SKIP_REASONS in this test file)"
                )

    if regressions:
        msg_lines = [
            "FX.7.2 local-import regression detected:",
            "",
            *regressions,
            "",
            "See backend/tests/test_local_import_drift_guard.py docstring "
            "for the rationale and the proper fix.",
        ]
        pytest.fail("\n".join(msg_lines))


def test_hoisted_imports_present_at_module_level() -> None:
    """The hoisted form must remain visible at module top level.

    This is the symmetric guard: if someone deletes the module-level
    import (thinking it's "unused" because grep didn't show callsites
    that all use the alias) we fail before the next FX.7.2 sample
    re-introduces the same pattern of local imports.
    """
    missing: list[str] = []
    for rel_path, snippet in HOISTED_PAIRS:
        file_path = REPO_ROOT / rel_path
        src = file_path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = False
        for node in tree.body:  # only top-level statements
            if _import_node_matches(node, snippet):
                found = True
                break
        if not found:
            missing.append(f"{rel_path}: missing module-level `{snippet}`")

    if missing:
        pytest.fail(
            "FX.7.2 hoisted import disappeared:\n  " + "\n  ".join(missing)
        )
