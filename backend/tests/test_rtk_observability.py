"""BP.R.6 contract tests for RTK Prometheus observability."""

from __future__ import annotations

import subprocess

import pytest

from backend import metrics as m
from backend import rtk_observability as rtk_obs


@pytest.mark.skipif(not m.is_available(), reason="prometheus_client not installed")
def test_probe_install_status_sets_success_gauge() -> None:
    m.reset_for_tests()

    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["rtk", "--version"],
            returncode=0,
            stdout="rtk 1.0.0",
            stderr="",
        )

    assert rtk_obs.probe_install_status(runner=_run) is True

    from prometheus_client import generate_latest

    text = generate_latest(m.REGISTRY).decode()
    assert "omnisight_rtk_install_status 1.0" in text


@pytest.mark.skipif(not m.is_available(), reason="prometheus_client not installed")
def test_probe_install_status_sets_failure_gauge() -> None:
    m.reset_for_tests()

    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["rtk", "--version"],
            returncode=127,
            stdout="",
            stderr="rtk: not found",
        )

    assert rtk_obs.probe_install_status(runner=_run) is False

    from prometheus_client import generate_latest

    text = generate_latest(m.REGISTRY).decode()
    assert "omnisight_rtk_install_status 0.0" in text


@pytest.mark.skipif(not m.is_available(), reason="prometheus_client not installed")
def test_probe_install_status_handles_missing_binary() -> None:
    m.reset_for_tests()

    def _run(*_args, **_kwargs):
        raise FileNotFoundError("rtk")

    assert rtk_obs.probe_install_status(runner=_run) is False

    from prometheus_client import generate_latest

    text = generate_latest(m.REGISTRY).decode()
    assert "omnisight_rtk_install_status 0.0" in text
