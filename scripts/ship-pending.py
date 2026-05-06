#!/usr/bin/env python3
"""Ship local pending commits to Gerrit for operator review.

This is the manual escape hatch for commits created directly in the
operator/main repository, outside the JIRA runner worktree flow.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch  # noqa: E402

CHANGE_ID_RE = re.compile(r"^Change-Id:\s*(I[0-9a-fA-F]{40})\s*$", re.MULTILINE)
CHANGE_URL_RE = re.compile(r"(https://\S+/c/[^\s]+/\+/(\d+))")


@dataclass(frozen=True)
class PendingCommit:
    sha: str
    short_sha: str
    subject: str
    change_id: str | None
    parent_count: int
    gerrit_url: str | None = None

    @property
    def has_change_id(self) -> bool:
        return self.change_id is not None

    @property
    def already_in_gerrit(self) -> bool:
        return self.gerrit_url is not None

    @property
    def is_merge(self) -> bool:
        return self.parent_count > 1

    @property
    def selectable(self) -> bool:
        return not self.already_in_gerrit and not self.is_merge


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=cwd, check=check)


def _require_git_repo(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    try:
        _git(path, "rev-parse", "--show-toplevel")
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"{label} is not a git repository: {path}\n{exc.stderr.strip()}") from exc
    return path


def _fetch_source_base(source_repo: Path, base_ref: str) -> None:
    if not base_ref.startswith("gerrit/"):
        return
    remote_branch = base_ref.split("/", 1)[1]
    remote_check = _git(source_repo, "remote", "get-url", "gerrit", check=False)
    if remote_check.returncode != 0:
        print(f"[ship-pending] warning: no 'gerrit' remote in {source_repo}; using existing {base_ref}")
        return
    refspec = f"{remote_branch}:refs/remotes/gerrit/{remote_branch}"
    _git(source_repo, "fetch", "gerrit", refspec)


def _extract_pending_commits(source_repo: Path, base_ref: str) -> list[PendingCommit]:
    revs = _git(source_repo, "rev-list", "--reverse", f"{base_ref}..HEAD").stdout.splitlines()
    commits: list[PendingCommit] = []
    for sha in revs:
        short_sha = _git(source_repo, "show", "-s", "--format=%h", sha).stdout.strip()
        subject = _git(source_repo, "show", "-s", "--format=%s", sha).stdout.strip()
        body = _git(source_repo, "show", "-s", "--format=%B", sha).stdout
        parents = _git(source_repo, "show", "-s", "--format=%P", sha).stdout.split()
        match = CHANGE_ID_RE.search(body)
        commits.append(PendingCommit(sha, short_sha, subject, match.group(1) if match else None, len(parents)))
    return commits


def _gerrit_auth(agent_class: str) -> tuple[str, Path]:
    auth = jira_dispatch._GERRIT_AUTH_BY_CLASS.get(agent_class)
    if auth is None:
        valid = ", ".join(sorted(jira_dispatch._GERRIT_AUTH_BY_CLASS))
        raise SystemExit(f"unknown --agent-class {agent_class!r}; expected one of: {valid}")
    user, key = auth
    if not key.exists():
        raise SystemExit(f"Gerrit SSH key not found for {agent_class}: {key}")
    return user, key


def _gerrit_ssh_cmd(agent_class: str, *remote_args: str) -> list[str]:
    user, key = _gerrit_auth(agent_class)
    return [
        "ssh",
        "-i",
        str(key),
        "-p",
        str(jira_dispatch.GERRIT_SSH_PORT),
        f"{user}@{jira_dispatch.GERRIT_SSH_HOST}",
        *remote_args,
    ]


def _query_change_url(agent_class: str, change_id: str) -> str | None:
    result = subprocess.run(
        _gerrit_ssh_cmd(agent_class, "gerrit", "query", "--format=JSON", f"change:{change_id}"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        blob = (result.stderr + "\n" + result.stdout).strip()
        raise RuntimeError(f"Gerrit query failed for {change_id}:\n{blob[-1000:]}")
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "rowCount" in item or item.get("type") == "stats":
            continue
        number = item.get("number")
        if number:
            return f"https://{jira_dispatch.GERRIT_SSH_HOST}/c/{jira_dispatch.GERRIT_PROJECT_PATH}/+/{number}"
    return None


def _with_gerrit_status(commits: list[PendingCommit], agent_class: str) -> list[PendingCommit]:
    checked: list[PendingCommit] = []
    for commit in commits:
        url = _query_change_url(agent_class, commit.change_id) if commit.change_id else None
        checked.append(
            PendingCommit(
                sha=commit.sha,
                short_sha=commit.short_sha,
                subject=commit.subject,
                change_id=commit.change_id,
                parent_count=commit.parent_count,
                gerrit_url=url,
            )
        )
    return checked


def _print_commit_summary(index: int, commit: PendingCommit) -> None:
    if commit.already_in_gerrit:
        status = f"already in Gerrit: {commit.gerrit_url}"
    elif commit.is_merge:
        status = "merge commit; not selectable for cherry-pick shipping"
    elif commit.change_id:
        status = f"Change-Id present: {commit.change_id}"
    else:
        status = "missing Change-Id; hook will be installed before cherry-pick/amend"
    print(f"[{index}] {commit.short_sha} {commit.subject}")
    print(f"    {status}")


def _confirm(commit: PendingCommit, assume_yes: bool) -> bool:
    if not commit.selectable:
        return False
    if assume_yes:
        return True
    while True:
        answer = input(f"Ship {commit.short_sha} to Gerrit? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        print("Please answer y or n.")


def _ensure_clean_worktree(worktree_path: Path) -> None:
    status = _git(worktree_path, "status", "--porcelain").stdout.strip()
    if status:
        raise SystemExit(
            f"ship worktree is dirty; refusing to reset/switch it:\n{worktree_path}\n\n{status}"
        )


def _push_to_gerrit(worktree_path: Path, agent_class: str) -> tuple[list[str], str]:
    _, key = _gerrit_auth(agent_class)
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {key}"
    result = _run(
        ["git", "push", jira_dispatch._gerrit_ssh_url(agent_class), "HEAD:refs/for/develop"],
        cwd=worktree_path,
        env=env,
        check=False,
        timeout=120,
    )
    blob = (result.stderr + "\n" + result.stdout).strip()
    if result.returncode != 0:
        raise RuntimeError(f"Gerrit push failed:\n{blob[-2000:]}")
    urls = [match.group(1) for match in CHANGE_URL_RE.finditer(blob)]
    return urls, blob


def _query_current_patchset_revision(agent_class: str, change_number: str) -> str:
    result = subprocess.run(
        _gerrit_ssh_cmd(agent_class, "gerrit", "query", "--format=JSON", "--current-patch-set", change_number),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        blob = (result.stderr + "\n" + result.stdout).strip()
        raise RuntimeError(f"Gerrit patch-set query failed for {change_number}:\n{blob[-1000:]}")
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        patch_set = item.get("currentPatchSet") or {}
        revision = patch_set.get("revision")
        if revision:
            return revision
    raise RuntimeError(f"Could not resolve current patch-set revision for change {change_number}")


def _other_agent_class(agent_class: str) -> str:
    if agent_class in ("subscription-codex", "api-openai"):
        return "subscription-claude"
    return "subscription-codex"


def _auto_cross_review(push_agent_class: str, change_urls: list[str]) -> None:
    review_agent_class = _other_agent_class(push_agent_class)
    for url in change_urls:
        change_number = url.rstrip("/").rsplit("/", 1)[-1]
        revision = _query_current_patchset_revision(push_agent_class, change_number)
        result = subprocess.run(
            _gerrit_ssh_cmd(
                review_agent_class,
                "gerrit",
                "review",
                "--project",
                jira_dispatch.GERRIT_PROJECT_PATH,
                "--code-review=+1",
                "--message=ship-pending-cross-bot-review",
                revision,
            ),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            blob = (result.stderr + "\n" + result.stdout).strip()
            raise RuntimeError(f"cross-bot review failed for {url}:\n{blob[-1000:]}")
        print(f"[ship-pending] cross-bot review posted by {review_agent_class}: {url}")


def _default_worktree(agent_class: str) -> Path:
    env_name = "OMNISIGHT_CODEX_WORKTREE" if agent_class in ("subscription-codex", "api-openai") else "OMNISIGHT_CLAUDE_WORKTREE"
    if os.environ.get(env_name):
        return Path(os.environ[env_name]).expanduser()
    candidate = REPO_ROOT if "codex-worktree" in REPO_ROOT.name else REPO_ROOT.parent / "OmniSight-codex-worktree"
    return candidate


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-repo",
        type=Path,
        default=Path.cwd(),
        help="Repository containing pending local commits; default: current directory.",
    )
    parser.add_argument(
        "--worktree",
        type=Path,
        default=None,
        help="Codex/Claude worktree used for the fresh ship branch.",
    )
    parser.add_argument(
        "--agent-class",
        default="subscription-codex",
        choices=sorted(jira_dispatch._GERRIT_AUTH_BY_CLASS),
        help="Bot identity/key used to push to Gerrit.",
    )
    parser.add_argument("--base", default="gerrit/develop", help="Base ref for pending scan; default: gerrit/develop.")
    parser.add_argument("--dry-run", action="store_true", help="List and confirm candidates without changing the ship worktree.")
    parser.add_argument("--yes", action="store_true", help="Do not prompt; ship every non-idempotent candidate.")
    parser.add_argument(
        "--auto-cross-review",
        action="store_true",
        help="After a successful push, ask the other bot to post Code-Review +1.",
    )
    parser.add_argument("--no-fetch", action="store_true", help="Do not refresh source gerrit/develop before scanning.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_repo = _require_git_repo(args.source_repo, "--source-repo")
    worktree_path = _require_git_repo(args.worktree or _default_worktree(args.agent_class), "--worktree")

    if not args.no_fetch:
        _fetch_source_base(source_repo, args.base)

    commits = _extract_pending_commits(source_repo, args.base)
    if not commits:
        print(f"[ship-pending] no pending commits in {source_repo}: {args.base}..HEAD")
        return 0

    commits = _with_gerrit_status(commits, args.agent_class)
    print(f"[ship-pending] pending scan: {source_repo} ({args.base}..HEAD)")
    print(f"[ship-pending] ship worktree: {worktree_path}")

    selected: list[PendingCommit] = []
    for index, commit in enumerate(commits, start=1):
        _print_commit_summary(index, commit)
        if _confirm(commit, args.yes):
            selected.append(commit)

    if not selected:
        print("[ship-pending] nothing selected to ship.")
        return 0

    if args.dry_run:
        print("[ship-pending] dry-run: would ship these commits:")
        for commit in selected:
            print(f"  - {commit.short_sha} {commit.subject}")
        return 0

    _ensure_clean_worktree(worktree_path)
    jira_dispatch.install_commit_msg_hook(worktree_path)
    jira_dispatch.set_bot_identity_in_worktree(worktree_path, args.agent_class)

    ticket_key = f"OP-686-ship-pending-{int(time.time())}"
    sync = jira_dispatch.sync_to_gerrit_develop(worktree_path, args.agent_class, ticket_key)
    print(f"[ship-pending] {sync.detail}")

    for commit in selected:
        print(f"[ship-pending] cherry-pick {commit.short_sha}: {commit.subject}")
        _git(worktree_path, "cherry-pick", commit.sha)

    if any(not commit.has_change_id for commit in selected):
        print("[ship-pending] adding missing Change-Id footers via Gerrit commit-msg hook")
        jira_dispatch.ensure_change_ids(worktree_path, base_ref=sync.develop_sha)

    change_urls, push_detail = _push_to_gerrit(worktree_path, args.agent_class)
    if not change_urls:
        print("[ship-pending] push succeeded but no Change URL was parsed; raw tail follows:")
        print(push_detail[-1500:])
    else:
        print("[ship-pending] Gerrit changes:")
        for url in change_urls:
            print(f"  {url}")

    if args.auto_cross_review and change_urls:
        _auto_cross_review(args.agent_class, change_urls)

    print("[ship-pending] Operator: cross-bot review +1 is required, then human Code-Review +2 and Submit in Gerrit.")
    print("[ship-pending] Suggested manual trigger: run this at the end of /loop when direct main-repo commits were made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
