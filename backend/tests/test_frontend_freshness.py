"""BP.W3.14 — frontend build freshness helper tests."""

from __future__ import annotations

import subprocess

import pytest

from backend import frontend_freshness as ff
from backend import metrics as m


def _runner_for(outputs: dict[tuple[str, ...], str]):
    def _run(args, **_kwargs):
        key = tuple(args[1:])
        out = outputs.get(key, "")
        return subprocess.CompletedProcess(args=args, returncode=0 if out else 1, stdout=out, stderr="")

    return _run


def test_freshness_uses_env_and_git_lag() -> None:
    runner = _runner_for({
        ("rev-list", "--count", "abc1234..def5678"): "12\n",
    })

    status = ff.get_frontend_freshness(
        env={
            ff.ENV_FRONTEND_BUILD_COMMIT: "abc1234",
            ff.ENV_MASTER_HEAD_COMMIT: "def5678",
        },
        runner=runner,
    )

    assert status.prod_build_commit == "abc1234"
    assert status.master_head_commit == "def5678"
    assert status.lag_commits == 12
    assert status.status == "stale"


def test_freshness_falls_back_to_explicit_lag_when_git_unavailable() -> None:
    status = ff.get_frontend_freshness(
        env={
            ff.ENV_FRONTEND_BUILD_COMMIT: "abc1234",
            ff.ENV_MASTER_HEAD_COMMIT: "def5678",
            ff.ENV_FRONTEND_BUILD_LAG_COMMITS: "3",
        },
        runner=_runner_for({}),
    )

    assert status.lag_commits == 3
    assert status.status == "fresh"


def test_missing_build_commit_is_unknown() -> None:
    status = ff.get_frontend_freshness(
        env={ff.ENV_MASTER_HEAD_COMMIT: "def5678"},
        runner=_runner_for({}),
    )

    assert status.status == "unknown"
    assert status.prod_build_commit == ""


@pytest.mark.skipif(not m.is_available(), reason="prometheus_client not installed")
def test_publish_frontend_lag_metric() -> None:
    m.reset_for_tests()
    status = ff.FrontendFreshness(
        prod_build_commit="abc1234",
        master_head_commit="def5678",
        lag_commits=10,
        status="stale",
        detail="behind",
    )

    assert ff.publish_frontend_build_lag(status) == 10

    from prometheus_client import generate_latest

    text = generate_latest(m.REGISTRY).decode()
    assert "omnisight_frontend_build_lag_commits 10.0" in text
