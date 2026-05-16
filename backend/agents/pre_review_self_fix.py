"""Pre-review Gerrit mergeability self-fix.

AUDIT-29g-1 / OP-1038: before the runner asks an operator for +1/+2, verify
the just-pushed Gerrit change is mergeable. If Gerrit reports
``mergeable=false``, rebase the local runner branch onto the current target
branch and force-push a replacement patchset. The loop is intentionally capped
so a persistent conflict cannot wedge the runner.
"""
from __future__ import annotations

import base64
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


GERRIT_XSSI_PREFIX = ")]}'"
DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class SelfFixResult:
    """Outcome of the pre-review self-fix loop."""

    mergeable: bool
    attempts: int = 0
    rebased: bool = False
    force_pushed: bool = False
    cap_exhausted: bool = False
    skipped: bool = False
    detail: str = ""


def self_fix_mergeability(
    *,
    worktree_path: Path,
    change_number: int,
    gerrit_ssh_url: str,
    rest_base_url: str,
    username: str,
    http_password: str | None,
    target: str = "develop",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
    timeout: int = 60,
) -> SelfFixResult:
    """Rebase + force-push while Gerrit reports ``mergeable=false``.

    Returns ``skipped=True`` only when HTTP credentials are unavailable; that
    is still a non-mergeable terminal result so the caller can avoid forwarding
    an unverified patchset to review.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if not http_password:
        return SelfFixResult(
            mergeable=False,
            skipped=True,
            detail="missing Gerrit HTTP password; pre-review self-fix skipped",
        )

    for attempt in range(0, max_attempts + 1):
        mergeable = _fetch_mergeable(
            rest_base_url=rest_base_url,
            change_number=change_number,
            username=username,
            http_password=http_password,
            urlopen=urlopen,
            timeout=timeout,
        )
        if mergeable is True:
            return SelfFixResult(
                mergeable=True,
                attempts=attempt,
                rebased=attempt > 0,
                force_pushed=attempt > 0,
                detail=(
                    "Gerrit mergeability is true"
                    if attempt == 0
                    else f"Gerrit mergeability restored after {attempt} self-fix attempt(s)"
                ),
            )
        if mergeable is None:
            return SelfFixResult(
                mergeable=False,
                attempts=attempt,
                detail="Gerrit mergeability check failed",
            )
        if attempt == max_attempts:
            return SelfFixResult(
                mergeable=False,
                attempts=attempt,
                rebased=attempt > 0,
                force_pushed=attempt > 0,
                cap_exhausted=True,
                detail=f"mergeable=false after {max_attempts} self-fix attempt(s)",
            )

        rebase = _rebase_onto_target(
            worktree_path=worktree_path,
            gerrit_ssh_url=gerrit_ssh_url,
            target=target,
            run_command=run_command,
            timeout=timeout,
        )
        if rebase.returncode != 0:
            _abort_rebase(worktree_path, run_command)
            return SelfFixResult(
                mergeable=False,
                attempts=attempt + 1,
                detail=f"git rebase failed: {(rebase.stderr or rebase.stdout)[-500:]}",
            )

        push = run_command(
            [
                "git",
                "push",
                "--force",
                "--no-thin",
                gerrit_ssh_url,
                f"HEAD:refs/for/{target}",
            ],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if push.returncode != 0:
            return SelfFixResult(
                mergeable=False,
                attempts=attempt + 1,
                detail=f"git force-push failed: {(push.stderr or push.stdout)[-500:]}",
            )

    return SelfFixResult(
        mergeable=False,
        attempts=max_attempts,
        cap_exhausted=True,
        detail=f"mergeable=false after {max_attempts} self-fix attempt(s)",
    )


def _fetch_mergeable(
    *,
    rest_base_url: str,
    change_number: int,
    username: str,
    http_password: str,
    urlopen: Callable[..., Any],
    timeout: int,
) -> bool | None:
    change = urllib.parse.quote(str(change_number), safe="")
    url = f"{rest_base_url.rstrip('/')}/a/changes/{change}/revisions/current/mergeable"
    token = base64.b64encode(f"{username}:{http_password}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except (OSError, urllib.error.URLError, TimeoutError):
        return None
    if raw.startswith(GERRIT_XSSI_PREFIX):
        raw = raw.split("\n", 1)[1] if "\n" in raw else ""
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    value = payload.get("mergeable")
    return value if isinstance(value, bool) else None


def _rebase_onto_target(
    *,
    worktree_path: Path,
    gerrit_ssh_url: str,
    target: str,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    fetch = run_command(
        ["git", "fetch", gerrit_ssh_url, target],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if fetch.returncode != 0:
        return fetch
    return run_command(
        ["git", "rebase", "FETCH_HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _abort_rebase(
    worktree_path: Path,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    run_command(
        ["git", "rebase", "--abort"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
