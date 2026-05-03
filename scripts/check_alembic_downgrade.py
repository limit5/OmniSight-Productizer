"""FX.7.6 — reject silent ``downgrade(): pass`` in Alembic migrations.

Background
----------
Phase-3 PG cutover and the 2026-05-03 deep audit both surfaced the same
pattern: a migration ships with ``def downgrade(): pass`` (no rollback
SQL, no marker, no explanation). Operationally this means *the
migration cannot be rolled back* — a ``alembic downgrade -1`` advances
the version pointer but leaves the schema unchanged, silently
corrupting the relationship between the recorded revision and the
actual schema state. The 2026-05-03 audit found 7 such files in the
versions tree (BLOCKER-class for any rollback drill).

Two ways to legitimately ship a no-op rollback:

1. The author wrote real ``DROP TABLE`` / ``DROP COLUMN`` SQL and the
   downgrade is non-empty. This is the default and what the audit asks
   for.
2. The author has decided rollback is genuinely unsafe (would lose
   forensic data, leave orphaned references, etc.) and has written a
   one-line marker comment inside the function body documenting *why*::

       def downgrade() -> None:
           # alembic-allow-noop-downgrade: dropping the DLQ entries
           # would lose retry-exhaustion forensic history; require
           # hand-rolled migration to roll back.
           pass

   The marker token is machine-readable so this script can grep for
   it; the reason text is mandatory and must be at least 20 chars
   (forces the author to articulate the reason rather than rubber-stamp
   the marker).

Anything else — empty downgrade, ``pass``-only body, ``...`` ellipsis
body, missing ``downgrade`` function — is rejected.

Usage
-----
::

    # check every migration (manual / CI)
    python3 scripts/check_alembic_downgrade.py

    # pre-commit pass-through (only changed files)
    python3 scripts/check_alembic_downgrade.py backend/alembic/versions/0042_x.py

Stdlib-only by design (no external deps); same self-defense rationale
as ``check_migration_syntax.py`` — the linter has to remain runnable
even when a dependency upgrade has broken the migrations it lints.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = REPO_ROOT / "backend" / "alembic" / "versions"

MARKER_RE = re.compile(
    r"#\s*alembic-allow-noop-downgrade\s*:\s*(?P<reason>\S.*?)\s*$",
    re.MULTILINE,
)
MIN_REASON_CHARS = 20


def _is_noop_body(body: list[ast.stmt]) -> bool:
    """A downgrade body is a no-op if it consists only of:

    * ``pass`` statements
    * Bare string-literal expressions (docstring or inline string)
    * ``...`` (Ellipsis) expressions
    """
    if not body:
        return True
    for node in body:
        if isinstance(node, ast.Pass):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            value = node.value.value
            if isinstance(value, str) or value is Ellipsis:
                continue
        return False
    return True


def _find_downgrade(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "downgrade":
            return node
    return None


def _slice_function_source(source: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    lines = source.splitlines()
    start = fn.lineno - 1
    end = (fn.end_lineno or fn.lineno)
    return "\n".join(lines[start:end])


def _display_path(path: Path) -> Path:
    """Return a repo-relative path when possible; otherwise the input."""
    if not path.is_absolute():
        return path
    try:
        return path.relative_to(REPO_ROOT)
    except ValueError:
        return path


def check_file(path: Path) -> list[str]:
    """Return a list of violation strings (empty if file is clean)."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: SyntaxError ({exc.msg}) — cannot lint"]

    fn = _find_downgrade(tree)
    rel = _display_path(path)

    if fn is None:
        return [
            f"{rel}: missing top-level `def downgrade()` — every Alembic "
            "migration must define one (write real rollback SQL, or use "
            "the `# alembic-allow-noop-downgrade: <reason>` marker for a "
            "documented no-op)."
        ]

    if not _is_noop_body(fn.body):
        return []

    fn_source = _slice_function_source(source, fn)
    match = MARKER_RE.search(fn_source)
    if match is None:
        return [
            f"{rel}:{fn.lineno}: `def downgrade()` body is empty / `pass` "
            "/ docstring-only with no rollback SQL. Either implement the "
            "rollback, or add a marker comment inside the function body:\n"
            "    # alembic-allow-noop-downgrade: <one-line reason ≥ "
            f"{MIN_REASON_CHARS} chars>\n"
            "  See FX.7.6 in TODO.md."
        ]

    reason = match.group("reason").strip()
    if len(reason) < MIN_REASON_CHARS:
        return [
            f"{rel}:{fn.lineno}: `# alembic-allow-noop-downgrade:` marker "
            f"reason is too short ({len(reason)} chars; need ≥ "
            f"{MIN_REASON_CHARS}). The reason must articulate *why* "
            "rollback is unsafe (dropping data / orphaning rows / "
            "breaking in-flight clients / etc.) — a future operator "
            "reading this needs to know whether to attempt manual rollback."
        ]
    return []


def _candidate_files(args_files: list[str]) -> list[Path]:
    if args_files:
        # Pre-commit already filters by `files:` regex, and tests may
        # pass tmp_path fixtures that live outside the repo. Accept any
        # `.py` file the caller passed; only skip `__init__.py`.
        out: list[Path] = []
        for raw in args_files:
            p = Path(raw)
            if not p.is_absolute():
                p = (REPO_ROOT / p).resolve()
            if p.suffix != ".py" or p.name == "__init__.py":
                continue
            out.append(p)
        return out

    return sorted(
        p
        for p in VERSIONS_DIR.glob("*.py")
        if p.name != "__init__.py"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="Migration files to check (default: all).")
    args = parser.parse_args(argv)

    paths = _candidate_files(args.files)
    if not paths:
        return 0

    violations: list[str] = []
    for path in paths:
        violations.extend(check_file(path))

    if violations:
        sys.stderr.write(
            "\nFX.7.6 — alembic downgrade enforcement: found "
            f"{len(violations)} violation(s):\n\n"
        )
        for v in violations:
            sys.stderr.write(f"  - {v}\n")
        sys.stderr.write(
            "\nSee scripts/check_alembic_downgrade.py docstring or "
            "TODO.md FX.7.6 for the marker contract.\n"
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
