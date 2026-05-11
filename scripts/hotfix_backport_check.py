#!/usr/bin/env python3
"""OP-886 — CI gate that verifies a hotfix was cherry-picked to develop.

The gate compares patch identity, not raw SHA, because the required
follow-up is a cherry-pick and therefore normally has a different
commit id on ``develop``.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def run_git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=check,
    )


def verify_ref(repo: Path, ref: str) -> str:
    proc = run_git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    return proc.stdout.strip()


def cherry_status(repo: Path, *, develop_ref: str, hotfix_ref: str, base_ref: str) -> list[str]:
    verify_ref(repo, develop_ref)
    verify_ref(repo, hotfix_ref)
    verify_ref(repo, base_ref)
    proc = run_git(repo, ["cherry", develop_ref, hotfix_ref, base_ref])
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def missing_backports(lines: list[str]) -> list[str]:
    """Return hotfix commits whose patch-id is not present on develop."""
    return [line[2:] for line in lines if line.startswith("+ ")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--hotfix-ref", required=True)
    parser.add_argument("--develop-ref", default="develop")
    parser.add_argument("--base-ref", default="main")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    try:
        lines = cherry_status(
            repo,
            develop_ref=args.develop_ref,
            hotfix_ref=args.hotfix_ref,
            base_ref=args.base_ref,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"HotfixBackportCheckError: {detail}", file=sys.stderr)
        return 2

    missing = missing_backports(lines)
    if missing:
        print(
            "HotfixBackportMissing: cherry-pick these commits back to "
            f"{args.develop_ref}: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    print(
        "HotfixBackportOK: all hotfix patches are present on "
        f"{args.develop_ref}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
