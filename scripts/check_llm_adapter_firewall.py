"""N4 — LangChain/LangGraph firewall enforcement gate.

Walks `backend/` and fails (non-zero exit) if any `.py` file other
than `backend/llm_adapter.py` contains a `langchain*` or `langgraph*`
import.  The adapter module is the sole approved bridge between the
project and those libraries; keeping every other call site behind the
adapter means upgrading LangChain is a one-file change.

Usage:
    python3 scripts/check_llm_adapter_firewall.py [--root PATH]

Exit codes:
    0 — no violations
    1 — one or more violating imports detected
    2 — script/environment error (e.g. root path missing)

Detection:
    Regex over raw source — matches both `from langchain*.x import Y`
    and `import langchain*`.  Comments and strings are ignored via
    ``ast`` parsing to avoid false positives in docstrings and
    regex-like strings (this file itself is a good example).
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


ADAPTER_REL_PATH = Path("backend") / "llm_adapter.py"

# The adapter's own test suite is the only other file allowed to
# import directly from `langchain*` / `langgraph*`. It needs those
# imports to verify re-export identity (e.g. `adapter.HumanMessage is
# langchain_core.messages.HumanMessage`). Every other file must go
# through the adapter.
ADAPTER_TEST_REL_PATH = Path("backend") / "tests" / "test_llm_adapter.py"


def _is_forbidden_import(module: str) -> bool:
    """True if `module` is a langchain* / langgraph* import path."""
    if not module:
        return False
    head = module.split(".", 1)[0]
    return head.startswith("langchain") or head.startswith("langgraph")


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return list of (lineno, statement) violations inside *path*."""
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"::warning::cannot read {path}: {exc}", file=sys.stderr)
        return []

    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        print(f"::warning::cannot parse {path}: {exc}", file=sys.stderr)
        return []

    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_import(alias.name):
                    offenders.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            # node.module is None for `from . import X`; ignore those.
            if node.module and _is_forbidden_import(node.module):
                imported = ", ".join(a.name for a in node.names)
                offenders.append(
                    (node.lineno, f"from {node.module} import {imported}")
                )
    return offenders


def check(root: Path) -> int:
    backend_dir = root / "backend"
    if not backend_dir.is_dir():
        print(f"::error::backend directory not found at {backend_dir}", file=sys.stderr)
        return 2

    adapter_path = root / ADAPTER_REL_PATH
    if not adapter_path.is_file():
        print(
            f"::error::{ADAPTER_REL_PATH} is missing — the firewall "
            "adapter MUST exist. Did someone delete it?",
            file=sys.stderr,
        )
        return 2

    adapter_test_path = root / ADAPTER_TEST_REL_PATH  # May not exist yet

    total_violations = 0
    any_violation_file = False

    # Directories under backend/ that are NOT first-party source and
    # therefore aren't subject to the firewall.
    _SKIP_DIR_PARTS = {
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "site-packages",
    }

    for py_file in sorted(backend_dir.rglob("*.py")):
        # The adapter itself is the only legal place for these imports.
        if py_file.resolve() == adapter_path.resolve():
            continue
        # The adapter's own test file is the only allowlisted caller —
        # it verifies re-export identity against the real classes.
        if adapter_test_path.is_file() and py_file.resolve() == adapter_test_path.resolve():
            continue
        # Skip vendored / cache / venv paths — those are not our source.
        if any(part in _SKIP_DIR_PARTS for part in py_file.parts):
            continue

        violations = _scan_file(py_file)
        if not violations:
            continue
        any_violation_file = True
        rel = py_file.relative_to(root)
        for lineno, stmt in violations:
            total_violations += 1
            # GitHub Actions annotation format — surfaces as inline error.
            print(
                f"::error file={rel},line={lineno}::"
                f"N4 firewall violation: `{stmt}`. "
                "Import from `backend.llm_adapter` instead."
            )

    if any_violation_file:
        print(
            f"\n[N4] {total_violations} forbidden import(s) detected. "
            "All langchain*/langgraph* imports must go through "
            "`backend/llm_adapter.py`.",
            file=sys.stderr,
        )
        return 1

    print("[N4] OK — no langchain*/langgraph* imports outside the adapter.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root (default: auto-detected from script location)",
    )
    args = parser.parse_args()
    return check(args.root.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
