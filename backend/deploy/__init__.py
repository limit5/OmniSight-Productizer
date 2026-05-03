"""W4 #278 — Web deploy adapters package.

Exposes the unified ``WebDeployAdapter`` interface and a
``get_adapter(provider)`` factory so routers / HMI forms / orchestrator
can select a target provider by its canonical string id
(``vercel`` / ``netlify`` / ``cloudflare-pages`` / ``docker-nginx``).

Example:

    from backend.deploy import get_adapter, BuildArtifact

    adapter = get_adapter("vercel").from_encrypted_token(
        ciphertext, project_name="my-app",
    )
    await adapter.provision(env={"API_URL": "https://api.example.com"})
    await adapter.deploy(BuildArtifact(path=Path("./.vercel/output")))
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.deploy.base import (
    BuildArtifact,
    ContainerVulnerabilityBlockError,
    DeployArtifactError,
    DeployConflictError,
    DeployError,
    DeployRateLimitError,
    DeployResult,
    InvalidDeployTokenError,
    MissingDeployScopeError,
    ProvisionResult,
    RollbackUnavailableError,
    WebDeployAdapter,
    token_fingerprint,
)
from backend.deploy.instant_preview import (
    INSTANT_PREVIEW_MODES,
    MODE_DOCKER_RUN,
    MODE_VERCEL_PREVIEW,
    InstantPreviewError,
    InstantPreviewResult,
    InstantPreviewUnavailable,
    create_docker_run_preview,
    create_instant_preview,
    create_vercel_preview,
)

if TYPE_CHECKING:
    pass


def list_providers() -> list[str]:
    """Return the canonical id for every shipped adapter."""
    return ["vercel", "netlify", "cloudflare-pages", "docker-nginx"]


def get_adapter(provider: str) -> type[WebDeployAdapter]:
    """Look up an adapter class by its canonical provider string.

    Imports lazily so a broken/missing optional dependency in one
    adapter does not cascade to the others.
    """
    key = provider.strip().lower().replace("_", "-")
    if key == "vercel":
        from backend.deploy.vercel import VercelAdapter
        return VercelAdapter
    if key == "netlify":
        from backend.deploy.netlify import NetlifyAdapter
        return NetlifyAdapter
    if key in ("cloudflare-pages", "cloudflare", "cf-pages"):
        from backend.deploy.cloudflare_pages import CloudflarePagesAdapter
        return CloudflarePagesAdapter
    if key in ("docker-nginx", "docker", "nginx"):
        from backend.deploy.docker_nginx import DockerNginxAdapter
        return DockerNginxAdapter
    raise ValueError(
        f"Unknown deploy provider '{provider}'. "
        f"Expected one of: {', '.join(list_providers())}"
    )


__all__ = [
    "BuildArtifact",
    "ContainerVulnerabilityBlockError",
    "DeployArtifactError",
    "DeployConflictError",
    "DeployError",
    "DeployRateLimitError",
    "DeployResult",
    "INSTANT_PREVIEW_MODES",
    "InvalidDeployTokenError",
    "InstantPreviewError",
    "InstantPreviewResult",
    "InstantPreviewUnavailable",
    "MODE_DOCKER_RUN",
    "MODE_VERCEL_PREVIEW",
    "MissingDeployScopeError",
    "ProvisionResult",
    "RollbackUnavailableError",
    "WebDeployAdapter",
    "create_docker_run_preview",
    "create_instant_preview",
    "create_vercel_preview",
    "get_adapter",
    "list_providers",
    "token_fingerprint",
]
