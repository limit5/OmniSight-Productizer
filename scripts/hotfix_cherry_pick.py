#!/usr/bin/env python3
"""OP-950 — Cherry-pick a Gerrit change onto a hotfix release branch."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch  # noqa: E402


PUSH_CHANGE_RE = re.compile(r"(https://\S+/c/[^\s]+/\+/(\d+))")


class CherryPickConflict(RuntimeError):
    """The source patch did not apply cleanly to the target branch."""


class TargetBranchMissing(RuntimeError):
    """The requested target branch is not available locally or remotely."""


def _run(
    args: list[str],
    *,
    repo: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def _gerrit_env(agent_class: str) -> dict[str, str]:
    _, ssh_key = jira_dispatch._gerrit_auth_for_instance(agent_class)
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key}"
    return env


def _gerrit_ssh_cmd(agent_class: str, *remote_args: str) -> list[str]:
    user, ssh_key = jira_dispatch._gerrit_auth_for_instance(agent_class)
    return [
        "ssh",
        "-i",
        str(ssh_key),
        "-p",
        str(jira_dispatch.GERRIT_SSH_PORT),
        f"{user}@{jira_dispatch.GERRIT_SSH_HOST}",
        *remote_args,
    ]


def _query_change(repo: Path, change_number: str, agent_class: str) -> dict[str, Any]:
    result = _run(
        _gerrit_ssh_cmd(
            agent_class,
            "gerrit",
            "query",
            "--format=JSON",
            "--current-patch-set",
            f"change:{change_number}",
        ),
        repo=repo,
    )
    for line in result.stdout.splitlines():
        payload = json.loads(line)
        if "project" in payload:
            return payload
    raise RuntimeError(f"Gerrit change {change_number} not found")


def _ensure_target_branch(repo: Path, target: str, env: dict[str, str]) -> None:
    local = _run(["git", "rev-parse", "--verify", f"{target}^{{commit}}"], repo=repo, check=False)
    if local.returncode == 0:
        return
    remote = _run(
        ["git", "ls-remote", "--heads", "origin", target],
        repo=repo,
        env=env,
        check=False,
    )
    if remote.returncode != 0 or not remote.stdout.strip():
        raise TargetBranchMissing(f"target branch not found: {target}")
    _run(["git", "fetch", "origin", f"{target}:{target}"], repo=repo, env=env)


def cherry_pick_change(
    *,
    repo: Path,
    change_number: str,
    target: str,
    agent_class: str,
) -> dict[str, Any]:
    env = _gerrit_env(agent_class)
    original_ref = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo=repo).stdout.strip()
    change = _query_change(repo, change_number, agent_class)
    patchset = change.get("currentPatchSet") or {}
    ref = patchset.get("ref")
    revision = patchset.get("revision")
    if not ref or not revision:
        raise RuntimeError(f"Gerrit change {change_number} has no current patch set")

    _ensure_target_branch(repo, target, env)
    target_head = ""
    try:
        _run(["git", "fetch", jira_dispatch._gerrit_ssh_url(agent_class), ref], repo=repo, env=env)
        _run(["git", "checkout", target], repo=repo)
        target_head = _run(["git", "rev-parse", "HEAD"], repo=repo).stdout.strip()
        picked = _run(["git", "cherry-pick", "FETCH_HEAD"], repo=repo, check=False)
        if picked.returncode != 0:
            _run(["git", "cherry-pick", "--abort"], repo=repo, check=False)
            detail = (picked.stderr or picked.stdout or "").strip()
            raise CherryPickConflict(detail or f"change {change_number} conflicts on {target}")
        push = _run(
            ["git", "push", jira_dispatch._gerrit_ssh_url(agent_class), f"HEAD:refs/for/{target}"],
            repo=repo,
            env=env,
            check=False,
        )
        blob = (push.stderr + "\n" + push.stdout).strip()
        if push.returncode != 0:
            raise RuntimeError(f"git push failed: {blob[-1000:]}")
        match = PUSH_CHANGE_RE.search(blob)
        if not match:
            raise RuntimeError(
                f"push succeeded but Gerrit change URL was not parsed: {blob[-1000:]}"
            )
        return {
            "source_change": str(change_number),
            "target": target,
            "source_revision": revision,
            "cherry_picked_change": match.group(2),
            "cherry_picked_url": match.group(1),
        }
    except Exception:
        _run(["git", "cherry-pick", "--abort"], repo=repo, check=False)
        if target_head:
            _run(["git", "reset", "--hard", target_head], repo=repo, check=False)
        raise
    finally:
        if original_ref and original_ref != "HEAD":
            _run(["git", "checkout", original_ref], repo=repo, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-change", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--agent-class", default="subscription-codex")
    args = parser.parse_args(argv)

    try:
        result = cherry_pick_change(
            repo=Path(args.repo).resolve(),
            change_number=args.from_change,
            target=args.target,
            agent_class=args.agent_class,
        )
    except (CherryPickConflict, TargetBranchMissing) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI reports diagnostic for handler
        print(f"HotfixCherryPickFailed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
