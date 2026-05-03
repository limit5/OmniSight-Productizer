"""FX.7.1 — drift guard for ``from backend import X`` resolution.

Background
----------
The 2026-05-03 deep audit (`docs/audit/2026-05-03-deep-audit.md`, B18-B20
and DT36-DT45) reported "25+ dead imports from non-existent submodules in
`backend/__init__.py:15-25`". On investigation that file does not exist:
``backend`` is a PEP 420 namespace package and never had an ``__init__``.
The "25" was the count of *existing* ``__init__.py`` files under
``backend/``, mis-labeled by the auditor.

But a real instance of the bug class did exist (and had survived because
its ImportError was swallowed by ``except Exception``):

    backend/payment_compliance.py:1153
        from backend import audit_log as _al      # module never existed
        await _al.append(event_type=..., payload=...)

Symptom: every PCI-DSS / SOX / HIPAA / GDPR gate evaluation logged a
DEBUG line and silently dropped the audit row that compliance evidence
actually depends on. FX.7.1 fixes the call site to use ``backend.audit``
and adds this test so a future "from backend import X" with a typo
(or referencing a deleted module) fails CI red instead of silently
erasing a feature.

What the guard checks
---------------------
For every ``.py`` file in the repo (excluding venvs, site-packages,
node_modules, and the dotenv cache), find every

    from backend import X[, Y as Z, ...]

statement at AST level (only top-level ``backend`` package — sub-package
imports like ``from backend.routers import x`` are validated by Python
itself at import time). Assert that every ``X`` resolves to an
importable ``backend.X`` module.

This is a static AST + ``importlib`` check, no runtime fixture needed.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# Modules referenced by `from backend import X` that are intentionally
# optional / runtime-injected and not expected to exist as files.
# Each entry must have a one-line reason so a future reader can re-audit.
_INTENTIONALLY_DYNAMIC: dict[str, str] = {
    # (intentionally empty — every current `from backend import X` should
    #  resolve to a real module. Add an entry here only if a future
    #  feature legitimately needs a runtime-injected stub, with a comment
    #  pointing at the injection site.)
}

# Directories to skip when walking the repo.
_SKIP_DIR_FRAGMENTS = (
    "/.venv/",
    "/venv/",
    "/__pycache__/",
    "/node_modules/",
    "/site-packages/",
    "/.git/",
    "/.pytest_cache/",
    "/.mypy_cache/",
    "/.ruff_cache/",
    "/dist/",
    "/build/",
)


def _iter_py_files() -> list[Path]:
    out: list[Path] = []
    for p in REPO_ROOT.rglob("*.py"):
        s = str(p) + "/"
        if any(frag in s for frag in _SKIP_DIR_FRAGMENTS):
            continue
        out.append(p)
    return sorted(out)


def _collect_top_level_backend_imports() -> list[tuple[Path, int, str]]:
    """Return (file, lineno, symbol) for every `from backend import X`.

    Only matches the top-level ``backend`` package — `from backend.foo
    import bar` is left to Python's own import machinery to validate.
    """
    hits: list[tuple[Path, int, str]] = []
    for p in _iter_py_files():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            # Some scripts intentionally contain non-Python heredocs in
            # tests or docs — skip rather than fail the guard.
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level != 0 or node.module != "backend":
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                hits.append((p, node.lineno, alias.name))
    return hits


def test_top_level_backend_imports_resolve() -> None:
    """Every `from backend import X` must resolve to a real `backend.X`.

    Failure mode this catches: a typo or a deleted-but-still-referenced
    module silently breaks the import; the call site's ``except
    ImportError`` swallows it; the feature dies in production with a
    DEBUG line nobody reads. See module docstring for the
    payment_compliance.py incident this test was born from.
    """
    hits = _collect_top_level_backend_imports()
    # Sanity: we should find a non-trivial number of hits, otherwise the
    # walker is broken (e.g. wrong root) and the test would silently
    # always pass.
    assert len(hits) > 50, (
        f"Walker only found {len(hits)} `from backend import X` sites; "
        f"expected >50 across the repo. The walker root or skip filter "
        f"is probably wrong."
    )

    distinct_symbols = sorted({sym for _, _, sym in hits})
    failures: list[tuple[str, str, str]] = []  # (symbol, exc_type, msg)
    for sym in distinct_symbols:
        if sym in _INTENTIONALLY_DYNAMIC:
            continue
        full = f"backend.{sym}"
        try:
            importlib.import_module(full)
        except Exception as exc:
            failures.append((sym, type(exc).__name__, str(exc)[:200]))

    if failures:
        # Re-walk to surface the file:line for each broken symbol so the
        # CI failure message tells the operator exactly where to look.
        by_symbol: dict[str, list[str]] = {}
        for path, lineno, sym in hits:
            if any(sym == f[0] for f in failures):
                by_symbol.setdefault(sym, []).append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}"
                )
        lines = ["Dead `from backend import X` references:"]
        for sym, exc_type, msg in failures:
            sites = by_symbol.get(sym, ["<no sites — walker bug>"])
            lines.append(f"  - backend.{sym}  ({exc_type}: {msg})")
            for site in sites:
                lines.append(f"      at {site}")
        lines.append("")
        lines.append(
            "Fix: either (a) the module exists under a different name — "
            "update the call sites, or (b) it was deleted — remove the "
            "dead call sites entirely. Do NOT add to "
            "_INTENTIONALLY_DYNAMIC unless the symbol is genuinely "
            "runtime-injected; that is an escape hatch, not a workaround."
        )
        pytest.fail("\n".join(lines))
