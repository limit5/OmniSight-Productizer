#!/usr/bin/env python3
"""BP.W3.14 — CI detector for stale production frontend bundles.

Compares the current master/main HEAD with the last recorded production
frontend deploy commit. If more than the allowed number of commits that
touch frontend-owned files have landed since that deploy, the script
exits non-zero and optionally sends a Slack webhook notification.

Module-global state audit: immutable path/threshold constants only; all
state is derived from git/env inputs per invocation.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib import request as urlrequest

SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

FRONTEND_PATHS = (
    "app",
    "components",
    "lib",
    "messages",
    "middleware.ts",
    "next.config.mjs",
    "package.json",
    "pnpm-lock.yaml",
    "postcss.config.mjs",
    "public",
    "styles",
    "test",
    "tsconfig.json",
    "vitest.config.ts",
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def _clean_sha(value: str | None) -> str:
    candidate = (value or "").strip().lower()
    return candidate if SHA_RE.match(candidate) else ""


def _read_deploy_commit(record: Path | None) -> str:
    if record is None or not record.exists():
        return ""
    data = json.loads(record.read_text(encoding="utf-8"))
    return _clean_sha(str(data.get("frontend_build_commit") or ""))


def _frontend_commits(repo: Path, deploy_commit: str, head: str) -> list[str]:
    output = _git(
        repo,
        "log",
        "--format=%H",
        f"{deploy_commit}..{head}",
        "--",
        *FRONTEND_PATHS,
    )
    return [line for line in output.splitlines() if line.strip()]


def _slack_alert(webhook: str, *, count: int, threshold: int, deploy: str, head: str) -> None:
    if not webhook:
        return
    payload = {
        "text": (
            "OmniSight frontend stale-bundle detector failed: "
            f"{count} frontend commits since prod deploy "
            f"(threshold {threshold}). prod={deploy[:12] or 'missing'} "
            f"head={head[:12]}"
        )
    }
    req = urlrequest.Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=10) as resp:  # nosec B310 - caller supplies webhook
        resp.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--head-ref", default="HEAD")
    parser.add_argument("--deploy-commit", default=os.getenv("FRONTEND_DEPLOY_COMMIT", ""))
    parser.add_argument("--deploy-record", type=Path, default=None)
    parser.add_argument("--threshold", type=int, default=5)
    parser.add_argument("--slack-webhook", default=os.getenv("SLACK_WEBHOOK_URL", ""))
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    head = _clean_sha(_git(repo, "rev-parse", args.head_ref))
    deploy_commit = _clean_sha(args.deploy_commit) or _read_deploy_commit(args.deploy_record)

    if not deploy_commit:
        print("::error::missing frontend deploy commit record", file=sys.stderr)
        try:
            _slack_alert(
                args.slack_webhook,
                count=args.threshold + 1,
                threshold=args.threshold,
                deploy="",
                head=head,
            )
        except Exception as exc:
            print(f"::warning::Slack alert failed: {exc}", file=sys.stderr)
        return 2

    commits = _frontend_commits(repo, deploy_commit, head)
    count = len(commits)
    print(f"frontend deploy commit: {deploy_commit}")
    print(f"master head: {head}")
    print(f"frontend commits since deploy: {count}")

    if count > args.threshold:
        print(
            f"::error::{count} frontend commits landed since prod frontend deploy "
            f"(threshold {args.threshold})",
            file=sys.stderr,
        )
        try:
            _slack_alert(
                args.slack_webhook,
                count=count,
                threshold=args.threshold,
                deploy=deploy_commit,
                head=head,
            )
        except Exception as exc:
            print(f"::warning::Slack alert failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
