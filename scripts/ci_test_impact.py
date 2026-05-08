#!/usr/bin/env python3
"""OP-742 — Test impact analysis for the selective CI worker.

Maps a list of changed file paths (relative to repo root) to the set of
pytest test files that should run for the patchset. The goal is to keep
typical backend PSes inside a 5-15 minute test budget rather than the
60-180 minute full-suite cost (memory: feedback_test_strategy.md).

Three layers, applied in order:

1. **Direct map**: ``backend/foo.py`` → ``backend/tests/test_foo.py``
   (file-name convention).

2. **Import graph**: build a reverse-import map by parsing top-level
   ``import``/``from`` statements in every ``.py`` file under
   ``backend/``. If ``bar.py`` imports ``foo``, then test files that
   exercise ``bar`` are also pulled in.

3. **Conservative fallback**: if any change touches a high-fan-out path
   (e.g. ``backend/db.py``, ``backend/agents/scheduler.py``) the
   selection collapses to the full backend suite. The fan-out list is
   maintained explicitly below so that a careless graph walk can't
   silently skip safety-critical coverage.

The module is import-safe (no side effects at import time) and is
exercised by ``backend/tests/test_ci_test_impact.py``.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

# ── Public constants ──────────────────────────────────────────────

# Area → test policy. Mirrors the spec table in OP-742. Keys are the
# normalised ``area:`` label suffix (so ``area:backend`` → ``"backend"``).
AREA_TO_TEST_POLICY: dict[str, str] = {
    "docs":     "skip",
    "frontend": "frontend-only",
    "backend":  "affected",
    "devops":   "lint-only",
    "tests":    "affected",
    "tooling":  "lint-only",
    "security": "full",
    "db":       "full",         # migrations always run full suite
    "embedded": "lint-only",    # firmware can't run in CI runner
}

# High-fan-out modules: any change here forces the full backend suite.
# Selected because they are imported by >50 modules in the tree, or
# because their behaviour is checked indirectly by tests that don't name
# them in their imports (DB pool, scheduler tick, agent state machine).
HIGH_FAN_OUT_PATHS: frozenset[str] = frozenset({
    "backend/db.py",
    "backend/db_pool.py",
    "backend/db_context.py",
    "backend/auth.py",
    "backend/tenant_secrets.py",
    "backend/audit.py",
    "backend/agents/scheduler.py",
    "backend/agents/state.py",
    "backend/agents/jira_dispatch.py",
    "backend/conftest.py",
    "backend/tests/conftest.py",
    "pyproject.toml",
    "ruff.toml",
})

DOCS_PREFIXES: tuple[str, ...] = ("docs/",)
FRONTEND_PREFIXES: tuple[str, ...] = (
    "app/", "components/", "hooks/", "lib/", "i18n/", "messages/",
    "styles/", "public/", "e2e/",
)
FRONTEND_SUFFIXES: tuple[str, ...] = (".tsx", ".ts", ".jsx", ".css", ".scss")
DEVOPS_PREFIXES: tuple[str, ...] = (
    "deploy/", "docker-compose", "Dockerfile", ".github/",
)
TOOLING_PREFIXES: tuple[str, ...] = (
    "scripts/", "tools/", "auto-runner",
)


@dataclass(frozen=True)
class TestImpactResult:
    """Selection produced by the impact analyser.

    ``policy`` drives the CI worker; ``test_files`` is non-empty only
    when policy is ``"affected"`` (and is the explicit list passed to
    pytest). ``reason`` is a one-line explanation suitable for a
    Verified -1 / +1 comment.
    """

    # Tell pytest not to collect this dataclass as a test class — the
    # leading "Test" prefix is matched by ``python_classes = Test*``.
    __test__ = False

    policy: str
    test_files: list[str]
    reason: str


# ── Path classification helpers ───────────────────────────────────


def _is_docs(path: str) -> bool:
    return path.startswith(DOCS_PREFIXES) or path.endswith(".md")


def _is_frontend(path: str) -> bool:
    if path.startswith(FRONTEND_PREFIXES):
        return True
    # Top-level config files dedicated to the frontend pipeline
    if path in {
        "next.config.mjs", "tsconfig.json", "package.json",
        "pnpm-lock.yaml", "vitest.config.ts", "playwright.config.ts",
        "eslint.config.mjs", "postcss.config.mjs", "components.json",
        "middleware.ts",
    }:
        return True
    return False


def _is_backend(path: str) -> bool:
    return path.startswith("backend/") and path.endswith(".py")


def _is_test_file(path: str) -> bool:
    p = Path(path)
    return p.name.startswith("test_") and p.suffix == ".py"


def _classify(path: str) -> str:
    """Coarse path → bucket mapping. Used as a sanity layer when the
    JIRA ticket area labels are missing or stale."""
    if _is_docs(path):
        return "docs"
    if _is_frontend(path):
        return "frontend"
    if path.startswith(DEVOPS_PREFIXES):
        return "devops"
    if path.startswith(TOOLING_PREFIXES):
        return "tooling"
    if _is_backend(path):
        return "backend"
    return "other"


# ── Direct map ────────────────────────────────────────────────────


def direct_test_files(changed: str, repo_root: Path) -> list[str]:
    """``backend/foo/bar.py`` → existing test files matching
    ``test_bar.py`` under any ``tests/`` directory in the project.

    Returns an empty list if the file isn't a Python source file or no
    matching test file exists. Test files map to themselves so that a
    PS that only edits a test file still runs that test.
    """

    p = Path(changed)
    if p.suffix != ".py":
        return []

    if _is_test_file(changed):
        if (repo_root / changed).exists():
            return [changed]
        return []

    stem = p.stem
    candidate_names = {f"test_{stem}.py"}
    matches: list[str] = []

    for tests_dir in (
        repo_root / "backend" / "tests",
        repo_root / "tests",
    ):
        if not tests_dir.is_dir():
            continue
        for name in candidate_names:
            for hit in tests_dir.rglob(name):
                rel = hit.relative_to(repo_root).as_posix()
                if rel not in matches:
                    matches.append(rel)
    return matches


# ── Import graph ──────────────────────────────────────────────────


def _module_name_for_file(rel_path: str) -> str | None:
    """``backend/agents/foo.py`` → ``backend.agents.foo``. Returns None
    for non-Python files or files outside an importable tree."""
    p = Path(rel_path)
    if p.suffix != ".py":
        return None
    parts = list(p.with_suffix("").parts)
    if not parts:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def _imports_in_file(file_path: Path) -> set[str]:
    """Return the set of dotted module names this file imports."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (OSError, SyntaxError):
        return set()

    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module)
    return out


def build_reverse_import_graph(
    repo_root: Path, roots: tuple[str, ...] = ("backend",)
) -> dict[str, set[str]]:
    """Return ``{imported_module: {importing_module, ...}}``.

    Keys/values are dotted module names (matching ``_module_name_for_file``).
    Only files under ``roots`` are scanned, so imports of stdlib modules
    are not retained in the graph values (they only appear as keys with
    empty value sets, which is fine for the consumer).
    """

    graph: dict[str, set[str]] = {}
    for root in roots:
        base = repo_root / root
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            rel = py.relative_to(repo_root).as_posix()
            mod = _module_name_for_file(rel)
            if mod is None:
                continue
            for imported in _imports_in_file(py):
                graph.setdefault(imported, set()).add(mod)
                # Walk parent packages so ``from backend.agents.foo``
                # reverse-links to ``backend.agents.foo`` *and*
                # ``backend.agents`` *and* ``backend``.
                parts = imported.split(".")
                for i in range(1, len(parts)):
                    graph.setdefault(".".join(parts[:i]), set()).add(mod)
    return graph


def transitive_importers(
    target_module: str, graph: dict[str, set[str]], max_depth: int = 6
) -> set[str]:
    """Walk the reverse-import graph, returning every module that
    transitively imports ``target_module`` within ``max_depth`` hops.
    """
    seen: set[str] = set()
    frontier: set[str] = {target_module}
    for _ in range(max_depth):
        nxt: set[str] = set()
        for node in frontier:
            for importer in graph.get(node, ()):
                if importer not in seen:
                    seen.add(importer)
                    nxt.add(importer)
        if not nxt:
            break
        frontier = nxt
    return seen


def _module_to_test_file(module: str, repo_root: Path) -> str | None:
    """``backend.agents.foo`` → ``backend/tests/test_foo.py`` if present."""
    leaf = module.rsplit(".", 1)[-1]
    candidate = repo_root / "backend" / "tests" / f"test_{leaf}.py"
    if candidate.is_file():
        return candidate.relative_to(repo_root).as_posix()
    return None


# ── Top-level entry point ─────────────────────────────────────────


def classify_changes(
    changed_files: list[str],
    repo_root: Path,
    *,
    declared_areas: list[str] | None = None,
    import_graph: dict[str, set[str]] | None = None,
) -> TestImpactResult:
    """Combine the three layers into a single decision.

    Decision order (first match wins):

    1. Empty change list → ``skip``.
    2. All files match a single short-circuit policy (``skip``,
       ``frontend-only``, ``lint-only``) → that policy.
    3. Any change touches a high-fan-out path → ``full``.
    4. ``security`` declared in ``declared_areas`` → ``full``.
    5. Otherwise → ``affected``: union of direct map + import graph.
       If no test files surface (nothing maps), fall back to ``full``
       so we never silently approve untested code.
    """

    if not changed_files:
        return TestImpactResult("skip", [], "no files changed")

    buckets = {_classify(p) for p in changed_files}

    if buckets == {"docs"}:
        return TestImpactResult("skip", [], "docs-only PS")

    declared = set(declared_areas or [])
    if "security" in declared:
        return TestImpactResult(
            "full", [], "security area declared — full suite required"
        )

    if buckets <= {"frontend", "docs"} and "frontend" in buckets:
        return TestImpactResult(
            "frontend-only", [],
            "frontend (+docs) only — handled by pnpm test pipeline",
        )

    if buckets <= {"devops", "tooling", "docs"} and (
        "devops" in buckets or "tooling" in buckets
    ):
        return TestImpactResult(
            "lint-only", [],
            "devops / tooling only — shellcheck + yamllint, no integration",
        )

    fan_out_hit = sorted(set(changed_files) & HIGH_FAN_OUT_PATHS)
    if fan_out_hit:
        return TestImpactResult(
            "full", [],
            f"high-fan-out path touched ({fan_out_hit[0]}) — full suite",
        )

    if import_graph is None:
        import_graph = build_reverse_import_graph(repo_root)

    selected: set[str] = set()
    backend_change_seen = False

    for path in changed_files:
        if not _is_backend(path):
            continue
        backend_change_seen = True
        for t in direct_test_files(path, repo_root):
            selected.add(t)
        mod = _module_name_for_file(path)
        if mod is None:
            continue
        for importer in transitive_importers(mod, import_graph):
            t = _module_to_test_file(importer, repo_root)
            if t:
                selected.add(t)
        # Map the changed module itself
        own = _module_to_test_file(mod, repo_root)
        if own:
            selected.add(own)

    if not backend_change_seen:
        # Mixed change with no backend code touched at all — defer to
        # frontend / lint pipeline.
        if buckets <= {"frontend", "docs"}:
            return TestImpactResult(
                "frontend-only", [],
                "no backend files in change set",
            )
        return TestImpactResult(
            "lint-only", [], "no backend files in change set"
        )

    if not selected:
        return TestImpactResult(
            "full", [],
            "backend change with no test mapping — falling back to full suite",
        )

    return TestImpactResult(
        "affected",
        sorted(selected),
        f"{len(selected)} affected test file(s) selected via direct + import graph",
    )


# ── CLI entry point ───────────────────────────────────────────────


def _parse_args(argv: list[str]) -> tuple[list[str], list[str], Path]:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", default=".", help="Repository root (default: cwd).",
    )
    parser.add_argument(
        "--area", action="append", default=[],
        help="Declared area label (repeatable).",
    )
    parser.add_argument(
        "files", nargs="*", help="Changed file paths (relative to repo root).",
    )
    args = parser.parse_args(argv)
    return args.files, args.area, Path(args.repo_root).resolve()


def main(argv: list[str] | None = None) -> int:
    import sys

    files, areas, repo_root = _parse_args(argv if argv is not None else sys.argv[1:])
    if not files and not sys.stdin.isatty():
        files = [line.strip() for line in sys.stdin if line.strip()]

    result = classify_changes(files, repo_root, declared_areas=areas)
    print(json.dumps({
        "policy": result.policy,
        "test_files": result.test_files,
        "reason": result.reason,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
