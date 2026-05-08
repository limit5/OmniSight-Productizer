#!/usr/bin/env python3
"""[OP-775] Cherry-pick a single hotfix commit onto a release branch.

Usage:
    scripts/cherry_pick_hotfix.py <commit-sha> --to release/v1.2

By default the script checks out the target release branch, cherry-picks
the one commit, and creates the next patch tag for that major/minor line
(for example ``v1.2.0`` -> ``v1.2.1``). Use ``--dry-run`` to print the
plan without changing the working tree.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


RELEASE_BRANCH_RE = re.compile(r"^release/v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)$")
SEMVER_TAG_RE = re.compile(
    r"^v"
    r"(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)$"
)


@dataclass(frozen=True)
class HotfixPlan:
    commit_sha: str
    target_branch: str
    source_tag: str
    next_tag: str
    commands: tuple[tuple[str, ...], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "commit_sha": self.commit_sha,
            "target_branch": self.target_branch,
            "source_tag": self.source_tag,
            "next_tag": self.next_tag,
            "commands": [list(cmd) for cmd in self.commands],
        }


def run_git(args: list[str], *, repo: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


def validate_commit(repo: Path, commit_sha: str) -> str:
    proc = run_git(["rev-parse", "--verify", f"{commit_sha}^{{commit}}"], repo=repo)
    return proc.stdout.strip()


def release_major_minor(branch: str) -> tuple[int, int]:
    match = RELEASE_BRANCH_RE.match(branch)
    if not match:
        raise ValueError("--to must look like release/vX.Y")
    return int(match.group("major")), int(match.group("minor"))


def parse_semver_tag(tag: str) -> tuple[int, int, int] | None:
    match = SEMVER_TAG_RE.match(tag)
    if not match:
        return None
    return int(match.group("major")), int(match.group("minor")), int(match.group("patch"))


def latest_patch_tag(repo: Path, *, major: int, minor: int) -> str:
    proc = run_git(["tag", "--list", f"v{major}.{minor}.*"], repo=repo)
    candidates: list[tuple[int, str]] = []
    for raw in proc.stdout.splitlines():
        parsed = parse_semver_tag(raw.strip())
        if parsed is None:
            continue
        tag_major, tag_minor, patch = parsed
        if tag_major == major and tag_minor == minor:
            candidates.append((patch, raw.strip()))
    if not candidates:
        raise ValueError(f"no existing v{major}.{minor}.Z tag found")
    return max(candidates)[1]


def increment_patch_tag(tag: str) -> str:
    parsed = parse_semver_tag(tag)
    if parsed is None:
        raise ValueError(f"not a SemVer tag: {tag}")
    major, minor, patch = parsed
    return f"v{major}.{minor}.{patch + 1}"


def build_plan(repo: Path, *, commit_sha: str, target_branch: str) -> HotfixPlan:
    full_sha = validate_commit(repo, commit_sha)
    major, minor = release_major_minor(target_branch)
    source_tag = latest_patch_tag(repo, major=major, minor=minor)
    next_tag = increment_patch_tag(source_tag)
    return HotfixPlan(
        commit_sha=full_sha,
        target_branch=target_branch,
        source_tag=source_tag,
        next_tag=next_tag,
        commands=(
            ("git", "checkout", target_branch),
            ("git", "cherry-pick", full_sha),
            ("git", "tag", "-a", next_tag, "-m", f"Hotfix {next_tag}"),
        ),
    )


def apply_plan(repo: Path, plan: HotfixPlan) -> None:
    run_git(["checkout", plan.target_branch], repo=repo)
    run_git(["cherry-pick", plan.commit_sha], repo=repo)
    run_git(["tag", "-a", plan.next_tag, "-m", f"Hotfix {plan.next_tag}"], repo=repo)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit_sha")
    parser.add_argument("--to", required=True, dest="target_branch")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    try:
        plan = build_plan(repo, commit_sha=args.commit_sha, target_branch=args.target_branch)
        if not args.dry_run:
            apply_plan(repo, plan)
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(f"cherry_pick_hotfix: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(plan.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
