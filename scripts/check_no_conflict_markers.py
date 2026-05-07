"""OP-698 — reject unresolved git conflict markers in source files.

Background
----------
Two 2026-05-07 incidents (changes #61 and #70) surfaced that Gerrit's
``Submit with conflicts`` UI button can produce two silent-failure modes:

  * INCIDENT A (#61) — conflict markers landed in develop's
    ``preferences.py`` (broken Python ``SyntaxError``); only caught by
    the next backend ticket touching the file.
  * INCIDENT B (#70) — Gerrit reported the change as MERGED but
    develop's git ref never advanced; the conflicted merge commit was
    reachable only from the feature branch. Feature was missing in
    production.

Both modes are catastrophic and operator-invisible. This script is
the CI drift guard (action 3 of OP-698): scan the working tree for
unresolved conflict markers and fail the build if any are found.

Scope
-----
Only the canonical 7-character ``<<<<<<<`` and ``>>>>>>>`` markers at
the start of a line are matched. The middle separator ``=======`` is
not matched standalone because Markdown horizontal rules use it
legitimately.

Files scanned: every text file under the repo root *except*::

  * .git/, node_modules/, .next/, dist/, build/, __pycache__/,
    .venv/, .pytest_cache/, .mypy_cache/, .ruff_cache/
  * binary files (detected via NUL byte in first 8 KiB)
  * the script itself + any test fixture that documents the markers
    explicitly (must contain the literal allow-marker token defined
    below).

Allow-marker
------------
A test or doc that intentionally contains conflict-marker text must
include the literal token ``op-698-allow-conflict-marker`` somewhere
in the file (case-insensitive). The token is short and specific
enough that no real source line will collide.

Usage
-----
::

  python3 scripts/check_no_conflict_markers.py [--verbose]

Returns exit code 0 if clean, 1 if any markers found.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".git", "node_modules", ".next", "dist", "build", "__pycache__",
    ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "coverage", ".coverage", "playwright-report", "test-results",
    ".turbo", ".cache", ".vite",
}

# Files with these names anywhere in the path are skipped wholesale.
SKIP_BASENAMES = {
    ".DS_Store", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "uv.lock", "Cargo.lock",
}

# Only scan text-ish extensions to keep the walk fast. Empty extension
# (e.g. shell scripts named without .sh) is also scanned — guarded by
# the binary-content check.
TEXT_EXT_ALLOWLIST = {
    ".py", ".pyx", ".pyi",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".md", ".mdx", ".rst", ".txt",
    ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg",
    ".sh", ".bash", ".zsh", ".fish",
    ".html", ".css", ".scss", ".sass",
    ".sql", ".prisma",
    ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php",
    ".c", ".cc", ".cpp", ".h", ".hh", ".hpp",
    ".dockerfile", ".makefile", ".mk",
    "",  # extensionless (will be screened by binary check below)
}

ALLOW_MARKER_TOKEN = "op-698-allow-conflict-marker"


def _is_binary(path: Path) -> bool:
    """Return True iff the first 8 KiB of ``path`` contain a NUL byte."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return True


def _scan_file(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, line)] for every line that starts with a conflict marker."""
    hits: list[tuple[int, str]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return hits

    if ALLOW_MARKER_TOKEN in content.lower():
        return hits

    for n, line in enumerate(content.splitlines(), start=1):
        # Match the canonical 7-char Git conflict markers at start of
        # line. Allow trailing space-or-text per Git's format
        # (``<<<<<<< HEAD`` / ``>>>>>>> branch-name``).
        if line.startswith("<<<<<<< ") or line.startswith(">>>>>>> "):
            hits.append((n, line.rstrip()))
        # Also match exact-7 with newline only (degenerate rebase form).
        elif line == "<<<<<<<" or line == ">>>>>>>":
            hits.append((n, line))
    return hits


def main(argv: list[str]) -> int:
    verbose = "--verbose" in argv or "-v" in argv

    self_path = Path(__file__).resolve()
    total_hits: list[tuple[Path, int, str]] = []
    files_scanned = 0

    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        # Prune skip dirs in-place so os.walk doesn't recurse into them.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for fname in filenames:
            if fname in SKIP_BASENAMES:
                continue
            fpath = Path(dirpath) / fname

            # Skip the script itself (it documents the markers in
            # docstrings).
            if fpath.resolve() == self_path:
                continue

            ext = fpath.suffix.lower()
            if ext not in TEXT_EXT_ALLOWLIST:
                continue

            if _is_binary(fpath):
                continue

            files_scanned += 1
            for lineno, line in _scan_file(fpath):
                rel = fpath.relative_to(REPO_ROOT)
                total_hits.append((rel, lineno, line))

    if verbose:
        print(f"check_no_conflict_markers: scanned {files_scanned} files")

    if not total_hits:
        if verbose:
            print("OK — no unresolved conflict markers found.")
        return 0

    print("FAIL — unresolved conflict markers detected:", file=sys.stderr)
    for rel, lineno, line in total_hits:
        # Truncate long lines for readability.
        snippet = line if len(line) <= 120 else line[:117] + "..."
        print(f"  {rel}:{lineno}: {snippet}", file=sys.stderr)
    print(
        f"\nTotal: {len(total_hits)} marker line(s) across "
        f"{len({h[0] for h in total_hits})} file(s).",
        file=sys.stderr,
    )
    print(
        "\nIf this is a deliberate test fixture or documentation example, "
        f"add the literal token '{ALLOW_MARKER_TOKEN}' anywhere in the "
        "file (case-insensitive) to suppress the check for that file.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
