"""BP.W3.14 — frontend bundle freshness helpers.

Publishes the build-vs-master lag that caught the R15 stale-bundle
incident. Import has no side effects; callers invoke
``get_frontend_freshness()`` from request/startup surfaces and then
``publish_frontend_build_lag()`` to update Prometheus.

Module-global state audit: no mutable process state is stored here. Each
worker reads the same env/git sources independently and publishes its
own in-process Prometheus sample.

Read-after-write timing audit: N/A. The module only reads env vars and
local git metadata; there is no cross-request write/read dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import Callable

from backend import metrics as _metrics

RunFn = Callable[..., subprocess.CompletedProcess[str]]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

ENV_FRONTEND_BUILD_COMMIT = "OMNISIGHT_FRONTEND_BUILD_COMMIT"
ENV_MASTER_HEAD_COMMIT = "OMNISIGHT_MASTER_HEAD_COMMIT"
ENV_FRONTEND_BUILD_LAG_COMMITS = "OMNISIGHT_FRONTEND_BUILD_LAG_COMMITS"


@dataclass(frozen=True)
class FrontendFreshness:
    prod_build_commit: str
    master_head_commit: str
    lag_commits: int
    status: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "prod_build_commit": self.prod_build_commit,
            "master_head_commit": self.master_head_commit,
            "lag_commits": self.lag_commits,
            "status": self.status,
            "detail": self.detail,
        }


def _clean_sha(value: str | None) -> str:
    candidate = (value or "").strip().lower()
    if _SHA_RE.match(candidate):
        return candidate
    return ""


def _git_output(args: list[str], *, runner: RunFn) -> str:
    result = runner(
        ["git", *args],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _git_head(*, runner: RunFn) -> str:
    return _clean_sha(_git_output(["rev-parse", "HEAD"], runner=runner))


def _git_lag(build_commit: str, head_commit: str, *, runner: RunFn) -> int | None:
    if not build_commit or not head_commit:
        return None
    output = _git_output(
        ["rev-list", "--count", f"{build_commit}..{head_commit}"],
        runner=runner,
    )
    try:
        return max(0, int(output))
    except (TypeError, ValueError):
        return None


def get_frontend_freshness(
    *,
    env: dict[str, str] | None = None,
    runner: RunFn = subprocess.run,
) -> FrontendFreshness:
    """Return the prod frontend build commit vs master HEAD freshness.

    Production images do not carry ``.git`` (see ``.dockerignore``), so
    operators can provide ``OMNISIGHT_FRONTEND_BUILD_LAG_COMMITS`` when
    only env metadata is available. In dev/CI, the helper computes the
    lag from local git history.
    """

    source = env if env is not None else os.environ
    build_commit = _clean_sha(source.get(ENV_FRONTEND_BUILD_COMMIT))
    head_commit = _clean_sha(source.get(ENV_MASTER_HEAD_COMMIT)) or _git_head(
        runner=runner,
    )

    lag = _git_lag(build_commit, head_commit, runner=runner)
    if lag is None:
        try:
            lag = max(0, int((source.get(ENV_FRONTEND_BUILD_LAG_COMMITS) or "0").strip()))
        except ValueError:
            lag = 0

    if not build_commit:
        return FrontendFreshness(
            prod_build_commit="",
            master_head_commit=head_commit,
            lag_commits=lag,
            status="unknown",
            detail=f"{ENV_FRONTEND_BUILD_COMMIT} is not set",
        )
    if not head_commit:
        return FrontendFreshness(
            prod_build_commit=build_commit,
            master_head_commit="",
            lag_commits=lag,
            status="unknown",
            detail=f"{ENV_MASTER_HEAD_COMMIT} is not set and git HEAD is unavailable",
        )

    status = "stale" if lag >= 10 else "fresh"
    detail = (
        f"prod frontend build is {lag} commit(s) behind master HEAD"
        if lag
        else "prod frontend build matches master HEAD"
    )
    return FrontendFreshness(
        prod_build_commit=build_commit,
        master_head_commit=head_commit,
        lag_commits=lag,
        status=status,
        detail=detail,
    )


def publish_frontend_build_lag(freshness: FrontendFreshness) -> int:
    """Publish ``omnisight_frontend_build_lag_commits`` and return it."""

    _metrics.frontend_build_lag_commits.set(freshness.lag_commits)
    return freshness.lag_commits


__all__ = [
    "ENV_FRONTEND_BUILD_COMMIT",
    "ENV_FRONTEND_BUILD_LAG_COMMITS",
    "ENV_MASTER_HEAD_COMMIT",
    "FrontendFreshness",
    "get_frontend_freshness",
    "publish_frontend_build_lag",
]
