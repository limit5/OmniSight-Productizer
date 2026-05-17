"""Generate Gerrit TDD dual-patchset chains.

BP.G.1 creates the implementation helper for the TDD review pattern:

* Patchset A contains only test files.
* Patchset B contains only implementation files and carries a
  ``Depends-On: <Patchset-A Change-Id>`` trailer.

The module intentionally keeps Git/Gerrit side effects behind explicit
function calls. Pure helpers such as :func:`split_paths` are used by
tests and by the CLI dry-run path without touching the working tree.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_TARGET_BRANCH = "develop"
DEFAULT_AGENT_CLASS = "subscription-codex"

TEST_PATH_PREFIXES = (
    "backend/tests/",
    "tests/",
)
TEST_PATH_NAMES = {
    "backend/pytest.ini",
    "pytest.ini",
}
TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test)\.py$")
CHANGE_ID_RE = re.compile(r"^Change-Id:\s*(I[0-9a-fA-F]+)\s*$", re.MULTILINE)
DEPENDS_ON_RE = re.compile(r"^Depends-On:\s*I[0-9a-fA-F]+\s*$", re.MULTILINE)


class GerritTddError(RuntimeError):
    """Raised when a TDD patchset chain cannot be generated safely."""


@dataclass(frozen=True)
class PathSplit:
    """Changed paths partitioned into Patchset A and Patchset B."""

    tests: tuple[str, ...]
    implementation: tuple[str, ...]


@dataclass(frozen=True)
class GeneratedPatchsets:
    """Result of creating the local two-commit TDD chain."""

    test_commit: str
    test_change_id: str
    implementation_commit: str
    implementation_change_id: str
    split: PathSplit


@dataclass(frozen=True)
class GerritTddResult:
    """Result of local generation plus optional Gerrit push."""

    patchsets: GeneratedPatchsets
    change_url: str | None = None
    detail: str = ""


def normalise_repo_path(path: str | Path) -> str:
    """Return a stable POSIX repo-relative path string."""

    text = str(path).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def is_test_path(path: str | Path) -> bool:
    """Return True if *path* belongs in Patchset A."""

    normalised = normalise_repo_path(path)
    if normalised in TEST_PATH_NAMES:
        return True
    if normalised.startswith(TEST_PATH_PREFIXES):
        return True
    return bool(TEST_FILE_RE.search(normalised))


def split_paths(paths: Iterable[str | Path]) -> PathSplit:
    """Partition changed paths into test and implementation groups."""

    tests: list[str] = []
    implementation: list[str] = []
    for raw_path in paths:
        path = normalise_repo_path(raw_path)
        if not path:
            continue
        if is_test_path(path):
            tests.append(path)
        else:
            implementation.append(path)

    return PathSplit(
        tests=tuple(sorted(set(tests))),
        implementation=tuple(sorted(set(implementation))),
    )


def validate_split(split: PathSplit) -> None:
    """Reject plans that cannot form the two required pure patchsets."""

    if not split.tests:
        raise GerritTddError("Patchset A would be empty; no test paths found")
    if not split.implementation:
        raise GerritTddError(
            "Patchset B would be empty; no implementation paths found"
        )


def build_test_commit_message(ticket_key: str, subject: str = "") -> str:
    """Build the Patchset A commit message."""

    summary = subject.strip() or "TDD tests"
    return "\n".join(
        [
            f"[{ticket_key}] {summary} tests",
            "",
            "Patchset A: tests only.",
        ]
    )


def build_impl_commit_message(
    ticket_key: str,
    test_change_id: str,
    subject: str = "",
) -> str:
    """Build the Patchset B commit message with the dependency trailer."""

    change_id = test_change_id.strip()
    if not CHANGE_ID_RE.match(f"Change-Id: {change_id}"):
        raise GerritTddError(f"invalid Patchset A Change-Id: {test_change_id!r}")

    summary = subject.strip() or "TDD implementation"
    return "\n".join(
        [
            f"[{ticket_key}] {summary} implementation",
            "",
            "Patchset B: implementation only.",
            "",
            f"Depends-On: {change_id}",
        ]
    )


def _git(
    repo: Path,
    args: Sequence[str],
    *,
    check: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _git_stdout(repo: Path, args: Sequence[str], *, timeout: int = 60) -> str:
    return _git(repo, args, timeout=timeout).stdout.strip()


def changed_worktree_paths(repo: Path) -> tuple[str, ...]:
    """Return paths with staged, unstaged, or untracked changes."""

    result = _git(repo, ["status", "--porcelain=v1", "-z"])
    entries = result.stdout.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        if status[0] in {"R", "C"}:
            # Porcelain -z emits "XY new\0old\0" for renames/copies.
            if index < len(entries):
                index += 1
            paths.append(path)
            continue
        paths.append(path)
    return tuple(sorted(set(normalise_repo_path(path) for path in paths if path)))


def commit_paths(repo: Path, paths: Sequence[str], message: str) -> str:
    """Create one commit containing exactly *paths* from the worktree."""

    if not paths:
        raise GerritTddError("refusing to create an empty commit")
    _git(repo, ["add", "--", *paths])
    _git(repo, ["commit", "-m", message])
    return _git_stdout(repo, ["rev-parse", "HEAD"])


def commit_change_id(repo: Path, commit: str = "HEAD") -> str:
    """Return the Gerrit Change-Id trailer for *commit*."""

    body = _git_stdout(repo, ["log", "-1", "--format=%B", commit])
    match = CHANGE_ID_RE.search(body)
    if not match:
        raise GerritTddError(
            f"commit {commit} has no Change-Id; install Gerrit's commit-msg hook"
        )
    return match.group(1)


def changed_paths_in_commit(repo: Path, commit: str) -> tuple[str, ...]:
    """Return files changed by a commit."""

    out = _git_stdout(
        repo,
        ["diff-tree", "--no-commit-id", "--name-only", "-r", commit],
    )
    if not out:
        return ()
    return tuple(normalise_repo_path(line) for line in out.splitlines() if line)


def assert_commit_purity(repo: Path, test_commit: str, impl_commit: str) -> None:
    """Verify Patchset A is test-only and Patchset B is implementation-only."""

    test_paths = changed_paths_in_commit(repo, test_commit)
    impl_paths = changed_paths_in_commit(repo, impl_commit)
    bad_tests = [path for path in test_paths if not is_test_path(path)]
    bad_impl = [path for path in impl_paths if is_test_path(path)]
    if bad_tests:
        raise GerritTddError(
            "Patchset A contains non-test paths: " + ", ".join(bad_tests)
        )
    if bad_impl:
        raise GerritTddError(
            "Patchset B contains test paths: " + ", ".join(bad_impl)
        )


def assert_depends_on(repo: Path, impl_commit: str, test_change_id: str) -> None:
    """Verify Patchset B carries the expected ``Depends-On`` trailer."""

    body = _git_stdout(repo, ["log", "-1", "--format=%B", impl_commit])
    expected = f"Depends-On: {test_change_id}"
    if expected not in body or not DEPENDS_ON_RE.search(body):
        raise GerritTddError(
            f"Patchset B is missing dependency trailer {expected!r}"
        )


def generate_patchsets(
    repo: Path,
    *,
    ticket_key: str,
    subject: str = "",
    install_hook: bool = True,
) -> GeneratedPatchsets:
    """Commit current worktree changes as Patchset A then Patchset B."""

    repo = repo.resolve()
    split = split_paths(changed_worktree_paths(repo))
    validate_split(split)

    if install_hook:
        from backend.agents import jira_dispatch

        jira_dispatch.install_commit_msg_hook(repo)

    test_commit = commit_paths(
        repo,
        split.tests,
        build_test_commit_message(ticket_key, subject),
    )
    test_change_id = commit_change_id(repo, test_commit)

    impl_commit = commit_paths(
        repo,
        split.implementation,
        build_impl_commit_message(ticket_key, test_change_id, subject),
    )
    impl_change_id = commit_change_id(repo, impl_commit)

    assert_commit_purity(repo, test_commit, impl_commit)
    assert_depends_on(repo, impl_commit, test_change_id)
    return GeneratedPatchsets(
        test_commit=test_commit,
        test_change_id=test_change_id,
        implementation_commit=impl_commit,
        implementation_change_id=impl_change_id,
        split=split,
    )


def generate_and_maybe_push(
    repo: Path,
    *,
    ticket_key: str,
    subject: str = "",
    target: str = DEFAULT_TARGET_BRANCH,
    agent_class: str = DEFAULT_AGENT_CLASS,
    push: bool = False,
) -> GerritTddResult:
    """Generate the local chain and optionally push ``HEAD`` to Gerrit."""

    patchsets = generate_patchsets(repo, ticket_key=ticket_key, subject=subject)
    if not push:
        return GerritTddResult(
            patchsets=patchsets,
            detail="generated local Patchset A/B commit chain",
        )

    from backend.agents import jira_dispatch

    pushed = jira_dispatch.push_to_gerrit_for_review(
        repo.resolve(),
        agent_class,
        target=target,
    )
    if not pushed.success:
        raise GerritTddError(f"Gerrit push failed: {pushed.detail}")
    return GerritTddResult(
        patchsets=patchsets,
        change_url=pushed.change_url,
        detail=pushed.detail,
    )


def plan_from_worktree(repo: Path) -> PathSplit:
    """Return the dry-run split plan for current worktree changes."""

    return split_paths(changed_worktree_paths(repo.resolve()))


def _print_plan(split: PathSplit) -> None:
    print("Patchset A (tests):")
    for path in split.tests:
        print(f"  {path}")
    print("Patchset B (implementation):")
    for path in split.implementation:
        print(f"  {path}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate Gerrit TDD Patchset A (tests) and Patchset B (implementation)."
    )
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--ticket", required=True, help="JIRA ticket key, e.g. OP-266")
    parser.add_argument("--subject", default="", help="commit subject suffix")
    parser.add_argument("--target", default=DEFAULT_TARGET_BRANCH, help="Gerrit target branch")
    parser.add_argument("--agent-class", default=DEFAULT_AGENT_CLASS)
    parser.add_argument(
        "--push",
        action="store_true",
        help="push the generated chain to Gerrit refs/for/<target>",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the split plan without committing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    try:
        if args.dry_run:
            split = plan_from_worktree(repo)
            validate_split(split)
            _print_plan(split)
            return 0

        result = generate_and_maybe_push(
            repo,
            ticket_key=args.ticket,
            subject=args.subject,
            target=args.target,
            agent_class=args.agent_class,
            push=args.push,
        )
    except (GerritTddError, subprocess.CalledProcessError) as exc:
        print(f"gerrit_tdd: {exc}", file=sys.stderr)
        return 1

    patchsets = result.patchsets
    print(f"Patchset A commit: {patchsets.test_commit}")
    print(f"Patchset A Change-Id: {patchsets.test_change_id}")
    print(f"Patchset B commit: {patchsets.implementation_commit}")
    print(f"Patchset B Change-Id: {patchsets.implementation_change_id}")
    if result.change_url:
        print(f"Gerrit change: {result.change_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
