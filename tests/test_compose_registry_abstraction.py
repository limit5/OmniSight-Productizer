"""OP-1487 - compose registry abstraction for digest-pinned images."""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
STAGING_COMPOSE = REPO_ROOT / "docker-compose.staging.yml"

BACKEND_DIGEST = "sha256:" + ("0" * 64)
FRONTEND_DIGEST = "sha256:" + ("1" * 64)
BRIDGE_DIGEST = "sha256:" + ("2" * 64)


@contextmanager
def _empty_env_files() -> Iterator[None]:
    """Compose config requires service-level env_file paths to exist."""
    touched: list[Path] = []
    for path in (REPO_ROOT / ".env", REPO_ROOT / ".env.staging"):
        if not path.exists():
            path.write_text("", encoding="utf-8")
            touched.append(path)
    try:
        yield
    finally:
        for path in touched:
            path.unlink(missing_ok=True)


def _compose_env(registry: str | None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "OMNISIGHT_BACKEND_DIGEST": BACKEND_DIGEST,
            "OMNISIGHT_FRONTEND_DIGEST": FRONTEND_DIGEST,
            "OMNISIGHT_BRIDGE_DIGEST": BRIDGE_DIGEST,
            "OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN": "dummy-token",
        }
    )
    env.pop("OMNISIGHT_REGISTRY", None)
    env.pop("OMNISIGHT_GHCR_NAMESPACE", None)
    if registry is not None:
        env["OMNISIGHT_REGISTRY"] = registry
    return env


def _compose_config(compose_file: Path, registry: str | None) -> dict:
    cmd = ["docker", "compose", "-f", str(compose_file), "config", "--format", "json"]
    with _empty_env_files():
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=_compose_env(registry),
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("compose_file", [PROD_COMPOSE, STAGING_COMPOSE])
@pytest.mark.parametrize(
    "registry",
    ["ghcr.io/your-org", "sora.services:49154/omnisight"],
)
def test_compose_config_accepts_ghcr_and_gitlab_registry_paths(
    compose_file: Path, registry: str
) -> None:
    cmd = ["docker", "compose", "-f", str(compose_file), "config", "--quiet"]
    with _empty_env_files():
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=_compose_env(registry),
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("compose_file", [PROD_COMPOSE, STAGING_COMPOSE])
def test_omnisight_registry_switches_only_the_image_prefix(compose_file: Path) -> None:
    ghcr = _compose_config(compose_file, "ghcr.io/your-org")["services"]
    gitlab = _compose_config(compose_file, "sora.services:49154/omnisight")["services"]

    expected = {
        "backend-a": ("omnisight-backend", BACKEND_DIGEST),
        "backend-b": ("omnisight-backend", BACKEND_DIGEST),
        "frontend": ("omnisight-frontend", FRONTEND_DIGEST),
    }
    for service, (image_name, digest) in expected.items():
        assert ghcr[service]["image"] == f"ghcr.io/your-org/{image_name}@{digest}"
        assert (
            gitlab[service]["image"]
            == f"sora.services:49154/omnisight/{image_name}@{digest}"
        )


def test_registry_default_preserves_ghcr_namespace_backcompat() -> None:
    services = _compose_config(PROD_COMPOSE, None)["services"]

    assert services["backend-a"]["image"] == (
        f"ghcr.io/your-org/omnisight-backend@{BACKEND_DIGEST}"
    )
    assert services["backend-b"]["image"] == (
        f"ghcr.io/your-org/omnisight-backend@{BACKEND_DIGEST}"
    )
    assert services["frontend"]["image"] == (
        f"ghcr.io/your-org/omnisight-frontend@{FRONTEND_DIGEST}"
    )
