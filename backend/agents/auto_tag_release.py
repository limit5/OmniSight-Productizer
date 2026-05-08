"""OP-769 staging_passed -> immutable release tag automation.

Consumes the ``staging_passed`` JSON records emitted by the Sprint D
staging gates. For each release version it creates one annotated tag on
``main`` HEAD, pushes that tag to Gerrit and GitLab, updates the matching
``release/vX.Y`` branch, and emits ``release_tagged`` for D9.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.agents.auto_promote_main import iter_new_records

EVENT_STAGING_PASSED = "staging_passed"
EVENT_RELEASE_TAGGED = "release_tagged"

DEFAULT_REPO = Path("/home/user/sora-bridge")
DEFAULT_EVENT_LOG = Path("/home/user/work/sora/logs/release-milestone/staging.log")
DEFAULT_CURSOR = Path("/home/user/work/sora/logs/release-milestone/auto-tag-release.cursor")
DEFAULT_TAG_REMOTES = ("gerrit", "gitlab")
DEFAULT_BRANCH_REMOTES = ("gerrit", "gitlab")
SEMVER_TAG_RE = re.compile(
    r"^v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$"
)

EventSink = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class ReleaseTagResult:
    """Single release tagging attempt outcome."""

    status: str
    version: str
    tag: str
    main_sha: str
    release_branch: str
    branch_head: str = ""
    detail: str = ""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit_event(event: str, payload: dict[str, Any]) -> None:
    record = {
        "timestamp": utc_now_iso(),
        "level": "INFO",
        "event": event,
        **payload,
    }
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)


def _git(
    repo: Path,
    *args: str,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _git_one(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.strip()


def _git_ok(repo: Path, *args: str) -> bool:
    return _git(repo, *args, check=False).returncode == 0


def _normalise_version(raw: object) -> str:
    version = str(raw or "").strip()
    if version and not version.startswith("v"):
        version = f"v{version}"
    if not SEMVER_TAG_RE.match(version):
        raise ValueError(f"release version must match vMAJOR.MINOR.PATCH: {version!r}")
    return version


def release_branch_for_tag(tag: str) -> str:
    match = SEMVER_TAG_RE.match(tag)
    if not match:
        raise ValueError(f"release tag must match vMAJOR.MINOR.PATCH: {tag!r}")
    return f"release/v{match.group('major')}.{match.group('minor')}"


def _release_notes_summary(event: dict[str, Any], tag: str) -> str:
    for key in ("release_notes_summary", "releaseNotesSummary", "summary"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    return f"Release {tag}"


def _ensure_immutable_tag(repo: Path, *, tag: str, main_sha: str, message: str) -> bool:
    """Create ``tag`` if absent.

    Returns ``True`` when a new tag was created. Existing tags are never
    moved; a tag pointing anywhere other than ``main_sha`` is a hard error.
    """
    if _git_ok(repo, "rev-parse", "--verify", f"refs/tags/{tag}"):
        existing = _git_one(repo, "rev-parse", f"{tag}^{{commit}}")
        if existing != main_sha:
            raise RuntimeError(
                f"immutable tag {tag} already points at {existing}, not main HEAD {main_sha}"
            )
        return False
    _git(repo, "tag", "-a", tag, main_sha, "-m", message)
    return True


def _branch_exists(repo: Path, branch: str) -> bool:
    return _git_ok(repo, "rev-parse", "--verify", f"refs/heads/{branch}")


def _checkout_release_branch(
    worktree: Path,
    *,
    source_repo: Path,
    branch: str,
    main_sha: str,
    remote: str,
) -> None:
    if _branch_exists(source_repo, branch):
        _git(worktree, "switch", branch)
        return

    fetch_ref = f"refs/heads/{branch}:refs/remotes/{remote}/{branch}"
    _git(source_repo, "fetch", remote, fetch_ref, check=False, timeout=120)
    if _git_ok(source_repo, "rev-parse", "--verify", f"refs/remotes/{remote}/{branch}"):
        _git(worktree, "switch", "-c", branch, "--track", f"{remote}/{branch}")
        return

    _git(worktree, "switch", "-c", branch, main_sha)


def _update_release_branch(
    repo: Path,
    *,
    branch: str,
    main_sha: str,
    remotes: tuple[str, ...],
) -> str:
    primary_remote = remotes[0]
    with tempfile.TemporaryDirectory(prefix="omnisight-release-branch-") as tmp:
        worktree = Path(tmp) / "worktree"
        _git(repo, "worktree", "add", "--detach", str(worktree), timeout=120)
        try:
            _checkout_release_branch(
                worktree,
                source_repo=repo,
                branch=branch,
                main_sha=main_sha,
                remote=primary_remote,
            )
            if not _git_ok(worktree, "merge-base", "--is-ancestor", main_sha, "HEAD"):
                _git(worktree, "cherry-pick", main_sha, timeout=120)
            branch_head = _git_one(worktree, "rev-parse", "HEAD")
            for remote in remotes:
                _git(worktree, "push", remote, f"HEAD:refs/heads/{branch}", timeout=120)
            return branch_head
        finally:
            _git(repo, "worktree", "remove", "--force", str(worktree), timeout=120, check=False)


def tag_release_on_staging_passed(
    event: dict[str, Any],
    *,
    repo: Path = DEFAULT_REPO,
    main_branch: str = "main",
    tag_remotes: tuple[str, ...] = DEFAULT_TAG_REMOTES,
    branch_remotes: tuple[str, ...] = DEFAULT_BRANCH_REMOTES,
    event_sink: EventSink = emit_event,
) -> ReleaseTagResult:
    """Handle one staging gate event."""
    if event.get("event") != EVENT_STAGING_PASSED:
        return ReleaseTagResult("ignored", "", "", "", "")

    tag = _normalise_version(event.get("fixVersion") or event.get("version") or event.get("tag"))
    branch = release_branch_for_tag(tag)
    main_sha = _git_one(repo, "rev-parse", main_branch)
    message = _release_notes_summary(event, tag)
    tag_created = _ensure_immutable_tag(repo, tag=tag, main_sha=main_sha, message=message)
    if not tag_created and _git_ok(repo, "merge-base", "--is-ancestor", main_sha, branch):
        return ReleaseTagResult(
            "already_tagged",
            tag,
            tag,
            main_sha,
            branch,
            _git_one(repo, "rev-parse", branch),
        )

    for remote in tag_remotes:
        _git(repo, "push", remote, f"refs/tags/{tag}:refs/tags/{tag}", timeout=120)

    branch_head = _update_release_branch(
        repo,
        branch=branch,
        main_sha=main_sha,
        remotes=branch_remotes,
    )
    payload = {
        "fixVersion": tag,
        "tag": tag,
        "main_branch": main_branch,
        "main_sha": main_sha,
        "release_branch": branch,
        "release_branch_head": branch_head,
        "tag_remotes": list(tag_remotes),
        "branch_remotes": list(branch_remotes),
    }
    event_sink(EVENT_RELEASE_TAGGED, payload)
    return ReleaseTagResult("tagged", tag, tag, main_sha, branch, branch_head)


def run_once(
    *,
    event_log: Path,
    cursor: Path,
    repo: Path,
    main_branch: str,
    tag_remotes: tuple[str, ...],
    branch_remotes: tuple[str, ...],
    event_sink: EventSink = emit_event,
) -> list[ReleaseTagResult]:
    results: list[ReleaseTagResult] = []
    for record in iter_new_records(event_log, cursor):
        result = tag_release_on_staging_passed(
            record,
            repo=repo,
            main_branch=main_branch,
            tag_remotes=tag_remotes,
            branch_remotes=branch_remotes,
            event_sink=event_sink,
        )
        if result.status != "ignored":
            results.append(result)
    return results


def follow(
    *,
    event_log: Path,
    cursor: Path,
    repo: Path,
    main_branch: str,
    tag_remotes: tuple[str, ...],
    branch_remotes: tuple[str, ...],
    poll_seconds: float,
) -> None:
    while True:
        run_once(
            event_log=event_log,
            cursor=cursor,
            repo=repo,
            main_branch=main_branch,
            tag_remotes=tag_remotes,
            branch_remotes=branch_remotes,
        )
        time.sleep(poll_seconds)


def _csv_tuple(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-log", type=Path, default=DEFAULT_EVENT_LOG)
    parser.add_argument("--cursor", type=Path, default=DEFAULT_CURSOR)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--main-branch", default="main")
    parser.add_argument("--tag-remotes", default=",".join(DEFAULT_TAG_REMOTES))
    parser.add_argument("--branch-remotes", default=",".join(DEFAULT_BRANCH_REMOTES))
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    tag_remotes = _csv_tuple(args.tag_remotes)
    branch_remotes = _csv_tuple(args.branch_remotes)
    if not tag_remotes:
        raise SystemExit("--tag-remotes must name at least one remote")
    if not branch_remotes:
        raise SystemExit("--branch-remotes must name at least one remote")

    if args.once:
        run_once(
            event_log=args.event_log,
            cursor=args.cursor,
            repo=args.repo,
            main_branch=args.main_branch,
            tag_remotes=tag_remotes,
            branch_remotes=branch_remotes,
        )
        return 0
    follow(
        event_log=args.event_log,
        cursor=args.cursor,
        repo=args.repo,
        main_branch=args.main_branch,
        tag_remotes=tag_remotes,
        branch_remotes=branch_remotes,
        poll_seconds=args.poll_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
