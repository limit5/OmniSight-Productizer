"""OP-1573 - deploy/staging compose fail-closed GitLab CR refs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STAGING_COMPOSE = REPO_ROOT / "deploy" / "staging" / "docker-compose.yml"
GITLAB_CR = "sora.services:49154/omnisight/OmniSight-Productizer"
IMAGE_TAG = "sha-op1573"
IMAGE_SERVICES = ("backend-a", "backend-b", "frontend", "bridge-daemon")


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _compose_env(*, include_tag: bool) -> dict[str, str]:
    env = os.environ.copy()
    env["POSTGRES_PASSWORD"] = "test-password"
    env.pop("OMNISIGHT_GHCR_NAMESPACE", None)
    env.pop("OMNISIGHT_REGISTRY", None)
    env.pop("OMNISIGHT_IMAGE_TAG", None)
    if include_tag:
        env["OMNISIGHT_IMAGE_TAG"] = IMAGE_TAG
    return env


def test_deploy_staging_compose_config_fails_when_image_tag_unset() -> None:
    if not _docker_available():
        pytest.skip("docker CLI not available")

    proc = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "bridge",
            "-f",
            str(STAGING_COMPOSE),
            "config",
            "--quiet",
        ],
        cwd=REPO_ROOT,
        env=_compose_env(include_tag=False),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode != 0
    assert "OMNISIGHT_IMAGE_TAG" in proc.stderr


def test_deploy_staging_compose_renders_gitlab_cr_without_latest_or_ghcr() -> None:
    if not _docker_available():
        pytest.skip("docker CLI not available")

    proc = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "bridge",
            "-f",
            str(STAGING_COMPOSE),
            "config",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        env=_compose_env(include_tag=True),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    rendered = proc.stdout
    assert "ghcr.io/" not in rendered
    assert ":latest" not in rendered

    services = json.loads(rendered)["services"]
    expected = {
        "backend-a": f"{GITLAB_CR}/backend:{IMAGE_TAG}",
        "backend-b": f"{GITLAB_CR}/backend:{IMAGE_TAG}",
        "frontend": f"{GITLAB_CR}/frontend:{IMAGE_TAG}",
        "bridge-daemon": f"{GITLAB_CR}/backend:{IMAGE_TAG}",
    }
    for service in IMAGE_SERVICES:
        assert services[service]["image"] == expected[service]
        assert services[service]["pull_policy"] == "always"
