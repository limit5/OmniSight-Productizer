"""OP-836 — Runner workspace safety: prevent CLI from writing to main repo.

L1 root-cause prevention for the OP-811/813/832/835 wedge family. Repeatedly
observed: the launched CLI (claude or codex) completes implementation, posts
AC verification, then makes commits — but **into the main-repo cwd
(``/home/user/work/sora/OmniSight-Productizer``) instead of the assigned
worktree (``/home/user/work/sora/OmniSight-claude-worktree``)**. The worktree
branch ends with 0 commits → ``ensure_change_ids`` raises
``NoCommitsOnBranchError`` (post-OP-827) or ``git push`` returns
``(no new changes)`` (pre-OP-827); the actual work piles up as untracked /
locally-committed garbage on ``main``.

OP-827 (typed exceptions) and OP-832 (area validation) are both *L2 recovery*
— they catch the wedge AFTER it happens. This module is *L1 prevention*: refuse
to launch the CLI in a workspace state where the wedge is reachable, and
detect post-CLI tamper before any subsequent runner step trusts the worktree.

The two checks are independent and both are required:

* ``assert_main_repo_unwritable_for_cli`` — pre-launch. Verifies that the main
  repo's ``.git`` and worktree root are NOT writable to the user that is about
  to launch the CLI (only the worktree itself is writable). Production
  deployments that want this guarantee will configure the user / repo perms
  accordingly; this function asserts the configuration. In dev environments
  where the assertion would always fail (operator runs as the same user that
  owns the main repo), the function is a no-op when ``OMNISIGHT_RUNNER_CWD_
  ENFORCE`` is unset, and active otherwise.

* ``write_workspace_sentinel`` / ``verify_workspace_sentinel`` — pre + post.
  Stamps the worktree with the current HEAD SHA + ticket key + timestamp
  before launch. After CLI returns, asserts the sentinel still exists and the
  HEAD SHA matches. A CLI that ``git reset``ed the worktree, blew away the
  sentinel, or pulled an unrelated branch will fail this check.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


SENTINEL_FILENAME = ".runner-cwd-sentinel"
"""Per-worktree marker file — committed-to-disk only, never committed-to-git
(the runner's worktree-prep step adds it to ``.git/info/exclude``)."""


ENV_ENFORCE = "OMNISIGHT_RUNNER_CWD_ENFORCE"
"""When set to a truthy value (1/true/yes/on), ``assert_main_repo_unwritable
_for_cli`` becomes a hard guard. Default off so dev environments where the
operator owns the main repo aren't broken."""


class MainRepoWritableInLaunchEnvError(RuntimeError):
    """Raised when the main repo is writable by the user about to launch the
    CLI — the workspace lacks the perm-isolation that would prevent the CLI
    from making commits into the main repo's working tree.

    Diagnostic carries: which path failed the check (main repo vs git common
    dir vs worktree), and the writable mode bits, so the operator can fix
    perms with a clear pointer.
    """

    def __init__(self, *, main_repo: Path, worktree: Path, common_dir: Path,
                 writable_path: Path) -> None:
        self.main_repo = main_repo
        self.worktree = worktree
        self.common_dir = common_dir
        self.writable_path = writable_path
        super().__init__(
            f"main repo path {writable_path} is writable to the launching "
            f"user; CLI could commit into the wrong tree (main_repo="
            f"{main_repo}, worktree={worktree}, common_dir={common_dir}). "
            f"Either: chmod the main repo to read-only for the runner user, "
            f"or unset {ENV_ENFORCE} to disable this guard for dev runs."
        )


class WorkspaceTamperedError(RuntimeError):
    """Raised when the post-CLI sentinel verification fails — CLI either
    deleted the sentinel, moved the worktree HEAD to an unexpected SHA, or
    swapped the worktree to a different branch entirely.

    The runner trusts the worktree's HEAD as the basis for ``ensure_change
    _ids`` and the eventual Gerrit push; tamper invalidates that trust.
    """

    def __init__(self, *, sentinel_path: Path, expected_sha: str | None,
                 observed_sha: str | None, reason: str) -> None:
        self.sentinel_path = sentinel_path
        self.expected_sha = expected_sha
        self.observed_sha = observed_sha
        self.reason = reason
        super().__init__(
            f"workspace sentinel {sentinel_path} {reason} "
            f"(expected_sha={expected_sha}, observed_sha={observed_sha})"
        )


def _git_common_dir(worktree_path: Path) -> Path:
    """Return ``git rev-parse --git-common-dir`` resolved from ``worktree_path``."""
    out = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=worktree_path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    common = Path(out)
    if not common.is_absolute():
        common = (worktree_path / common).resolve()
    return common


def _git_show_toplevel(path: Path) -> Path:
    """Return ``git rev-parse --show-toplevel`` from ``path``."""
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    return Path(out).resolve()


def _git_head_sha(worktree_path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_path, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _enforce_enabled() -> bool:
    val = os.environ.get(ENV_ENFORCE, "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def assert_main_repo_unwritable_for_cli(worktree_path: Path) -> None:
    """Verify the main-repo working tree is NOT writable by the launching user.

    Raises ``MainRepoWritableInLaunchEnvError`` when:

    * ``worktree_path`` and the main repo share the same git common dir
      (i.e. they are sibling worktrees of one repo), AND
    * The main repo working tree is writable to the user (mode bit ``0o200``
      on the directory itself OR on its ``.git`` reference).

    No-op when ``OMNISIGHT_RUNNER_CWD_ENFORCE`` is unset (dev mode). Production
    deployments that want this guarantee set the env var + chmod the main repo
    so only the worktree directory is writable.

    The check is *layered* with the sentinel verification: this function
    refuses to launch when the perm-isolation is missing; the sentinel detects
    when CLI tampered with the worktree state regardless of perms.
    """
    if not _enforce_enabled():
        return

    common_dir = _git_common_dir(worktree_path)
    # main_repo = the worktree whose .git is the common dir (i.e. NOT a linked
    # worktree). We can derive it from the common dir: it's the .git's parent.
    if common_dir.name == ".git":
        main_repo = common_dir.parent
    else:
        # Bare-ish layout — fall back to the common dir's parent.
        main_repo = common_dir.parent

    # Sanity: the worktree must be a separate path. If they're the same, we're
    # being launched IN the main repo cwd — refuse loudly.
    worktree = worktree_path.resolve()
    if worktree == main_repo:
        raise MainRepoWritableInLaunchEnvError(
            main_repo=main_repo, worktree=worktree, common_dir=common_dir,
            writable_path=worktree,
        )

    # Check perms on main repo working tree + its .git.
    candidates = [main_repo]
    git_ref = main_repo / ".git"
    if git_ref.exists():
        candidates.append(git_ref)
    for candidate in candidates:
        try:
            mode = candidate.stat().st_mode
        except OSError:
            continue
        if mode & 0o200:  # writable bit for owner
            raise MainRepoWritableInLaunchEnvError(
                main_repo=main_repo, worktree=worktree, common_dir=common_dir,
                writable_path=candidate,
            )


def write_workspace_sentinel(worktree_path: Path, ticket_key: str) -> Path:
    """Write a sentinel file at the worktree root marking the pre-launch state.

    Returns the absolute path of the sentinel for later verification. The
    sentinel records the worktree's HEAD SHA, the ticket key, and a timestamp.
    """
    sentinel = worktree_path / SENTINEL_FILENAME
    sha = _git_head_sha(worktree_path)
    payload = {
        "ticket_key": ticket_key,
        "head_sha": sha,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "worktree_path": str(worktree_path.resolve()),
    }
    sentinel.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return sentinel


def verify_workspace_sentinel(sentinel_path: Path, worktree_path: Path) -> dict:
    """Verify the sentinel survived the CLI invocation and the worktree HEAD
    didn't drift to an unexpected SHA.

    A worktree HEAD that ADVANCED relative to the sentinel SHA is OK — that's
    exactly what we want, the CLI made commits. A HEAD that REGRESSED, swapped
    branches, or detached is suspect — raise ``WorkspaceTamperedError``.

    Returns the parsed sentinel payload for the caller's audit log.
    """
    if not sentinel_path.exists():
        raise WorkspaceTamperedError(
            sentinel_path=sentinel_path, expected_sha=None,
            observed_sha=None, reason="was deleted by CLI",
        )
    try:
        payload = json.loads(sentinel_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise WorkspaceTamperedError(
            sentinel_path=sentinel_path, expected_sha=None,
            observed_sha=None, reason=f"is corrupt: {e}",
        ) from e

    pre_sha = payload.get("head_sha")
    post_sha = _git_head_sha(worktree_path)

    if pre_sha == post_sha:
        return payload  # No commits made — caller (ensure_change_ids) handles.

    # Post-SHA must be a descendant of pre-SHA (CLI added commits on top).
    # If it's not a descendant, the CLI did something destructive — reset to
    # a different commit, swapped branch, etc.
    is_descendant = subprocess.run(
        ["git", "merge-base", "--is-ancestor", pre_sha, post_sha],
        cwd=worktree_path, capture_output=True,
    )
    if is_descendant.returncode != 0:
        raise WorkspaceTamperedError(
            sentinel_path=sentinel_path, expected_sha=pre_sha,
            observed_sha=post_sha,
            reason="HEAD moved to a non-descendant SHA (reset/branch-swap)",
        )

    return payload
