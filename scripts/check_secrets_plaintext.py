#!/usr/bin/env python3
"""OP-869 CI gate for plaintext fields in SOPS encrypted env secrets."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET_PATH_RE = re.compile(r"^deploy/env/(dev|staging|prod)/secrets\.encrypted\.yaml$")
SECRET_KEY_RE = re.compile(
    r"^\s*(?!sops:)([A-Za-z0-9_.-]*(?:secret|token|password|api[_-]?key|private[_-]?key|credential)[A-Za-z0-9_.-]*)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
SAFE_VALUE_RE = re.compile(r"^(ENC\[|null$|~$|[{}]$)", re.IGNORECASE)


def changed_secret_paths() -> list[Path]:
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--", "deploy/env"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git diff failed")
    return [
        REPO_ROOT / line
        for line in proc.stdout.splitlines()
        if SECRET_PATH_RE.match(line)
    ]


def plaintext_hits(path: Path) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = SECRET_KEY_RE.match(line)
        if match is None:
            continue
        value = match.group(2).strip().strip("'\"")
        if not SAFE_VALUE_RE.match(value):
            hits.append((lineno, match.group(1)))
    return hits


def check_paths(paths: list[Path]) -> list[tuple[Path, int, str]]:
    failures: list[tuple[Path, int, str]] = []
    for path in paths:
        for lineno, key in plaintext_hits(path):
            failures.append((path, lineno, key))
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Secret files to scan; defaults to staged env secrets.")
    args = parser.parse_args(argv)

    paths = [Path(p) for p in args.paths] if args.paths else changed_secret_paths()
    paths = [p if p.is_absolute() else REPO_ROOT / p for p in paths]
    failures = check_paths(paths)
    if not failures:
        print(f"OK: scanned {len(paths)} encrypted secret file(s)")
        return 0

    print("FAIL: plaintext-looking secret fields found:", file=sys.stderr)
    for path, lineno, key in failures:
        rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        print(f"  {rel}:{lineno}: {key}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
