"""W4 #278 — Unified WebDeployAdapter interface.

Every web-vertical deploy provider (Vercel / Netlify / Cloudflare Pages /
docker-nginx / any future) implements this single abstract class so the
upstream automation (`agents`, `orchestrator`, HMI forms) can swap
targets without branching on provider strings.

The interface is intentionally small — four operations:

    provision()             Create/ensure the remote project + env vars
                            (idempotent; safe to re-run).
    deploy(build_artifact)  Upload the local build output and trigger a
                            new deployment. Returns the deployment URL.
    rollback(...)           Promote the previous successful deployment
                            back to production.
    get_url()               Return the currently live production URL
                            without hitting the network if cached.

Secret handling
---------------
API tokens enter through `from_encrypted_token()` (ciphertext decrypted
via ``backend.secret_store``) or `from_plaintext_token()` (test / CLI
path). The instance never logs the raw token — only ``token_fingerprint()``.

Error handling
--------------
All adapters raise ``DeployError`` (or subclasses); HTTP 401/403/409/429
map to typed subclasses so upstream routers can select HTTP status codes
without pattern-matching on strings.

Async vs sync
-------------
Network adapters (Vercel / Netlify / CF Pages) are async — they share
``httpx.AsyncClient`` to match the rest of the backend. The docker_nginx
adapter is pure filesystem + subprocess, so its implementation is
synchronous; to keep the base interface uniform we wrap sync methods in
``run_in_executor`` via small ``async def`` shims on the docker adapter.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Optional

from backend import secret_store

logger = logging.getLogger(__name__)


# ── Error hierarchy ──────────────────────────────────────────────

class DeployError(Exception):
    """Base for all deploy adapter errors."""

    def __init__(self, message: str, status: int = 0, provider: str = ""):
        super().__init__(message)
        self.status = status
        self.provider = provider


class InvalidDeployTokenError(DeployError):
    """401 — API token invalid / revoked."""


class MissingDeployScopeError(DeployError):
    """403 — token lacks required permission."""


class DeployConflictError(DeployError):
    """409 — resource already exists (project name, env var)."""


class DeployRateLimitError(DeployError):
    """429 — provider rate limit hit."""

    def __init__(self, message: str, retry_after: int = 60, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class DeployArtifactError(DeployError):
    """Local artifact (build output / Dockerfile dir) missing or malformed."""


class ContainerVulnerabilityBlockError(DeployArtifactError):
    """Container vulnerability scan failed before deploy side effects."""


class RollbackUnavailableError(DeployError):
    """No previous deployment to roll back to."""


# ── Data models ──────────────────────────────────────────────────

@dataclass
class BuildArtifact:
    """Immutable handle to a local build output ready for upload.

    ``path`` is the root dir of the build (e.g. ``./dist`` / ``.next`` /
    ``.vercel/output``). ``framework`` is a declarative hint
    (``next`` / ``nuxt`` / ``svelte`` / ``astro`` / ``static``) — adapters
    can use it to pick provider-specific deploy presets; passing ``None``
    is always valid (provider will auto-detect).
    """

    path: Path
    framework: Optional[str] = None
    commit_sha: Optional[str] = None
    branch: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.path = Path(self.path)

    def validate(self) -> None:
        if not self.path.exists():
            raise DeployArtifactError(
                f"Build artifact path does not exist: {self.path}"
            )
        if not self.path.is_dir():
            raise DeployArtifactError(
                f"Build artifact path is not a directory: {self.path}"
            )


@dataclass
class ProvisionResult:
    """Outcome of ``adapter.provision(...)``."""

    provider: str
    project_id: str
    project_name: str
    url: Optional[str] = None
    created: bool = False          # True if the call actually created a new remote project
    env_vars_set: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "url": self.url,
            "created": self.created,
            "env_vars_set": list(self.env_vars_set),
        }


@dataclass
class DeployResult:
    """Outcome of ``adapter.deploy(...)`` / ``adapter.rollback(...)``."""

    provider: str
    deployment_id: str
    url: str
    status: str = "ready"          # ready / queued / building / error / rolled-back
    logs_url: Optional[str] = None
    commit_sha: Optional[str] = None
    previous_deployment_id: Optional[str] = None  # for rollback
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "deployment_id": self.deployment_id,
            "url": self.url,
            "status": self.status,
            "logs_url": self.logs_url,
            "commit_sha": self.commit_sha,
            "previous_deployment_id": self.previous_deployment_id,
        }


# ── Token utilities ──────────────────────────────────────────────

def token_fingerprint(token: str) -> str:
    """Return a log-safe fingerprint — never the full token."""
    if not token or len(token) <= 8:
        return "****"
    return f"…{token[-4:]}"


# ── Interface ────────────────────────────────────────────────────

class WebDeployAdapter(ABC):
    """Abstract base for every web-vertical deploy provider adapter.

    Subclasses MUST set a ``provider`` classvar and implement the four
    abstract methods. They SHOULD NOT override ``__init__`` — instead,
    override ``_configure()`` for provider-specific init (e.g. building
    an httpx client, parsing a site ID).
    """

    provider: ClassVar[str] = ""

    def __init__(
        self,
        *,
        token: str,
        project_name: str,
        project_id: Optional[str] = None,
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        if not self.provider:
            raise ValueError(f"{type(self).__name__} must set classvar 'provider'")
        if not project_name:
            raise ValueError("project_name is required")
        self._token = token
        self._project_name = project_name
        self._project_id = project_id
        self._timeout = timeout
        self._cached_url: Optional[str] = None
        self._last_deployment_id: Optional[str] = None
        self._configure(**kwargs)

    # ── Construction helpers ──

    @classmethod
    def from_encrypted_token(
        cls,
        ciphertext: str,
        *,
        project_name: str,
        **kwargs: Any,
    ) -> "WebDeployAdapter":
        """Decrypt the ciphertext via ``backend.secret_store`` and build
        an adapter. This is the preferred entry point from routers — the
        plaintext only lives in memory and never appears in a log or
        dict dump."""
        token = secret_store.decrypt(ciphertext)
        return cls(token=token, project_name=project_name, **kwargs)

    @classmethod
    def from_plaintext_token(
        cls,
        token: str,
        *,
        project_name: str,
        **kwargs: Any,
    ) -> "WebDeployAdapter":
        """Build an adapter from a plaintext token. Only the CLI / tests
        should call this; production code paths go through
        ``from_encrypted_token``."""
        return cls(token=token, project_name=project_name, **kwargs)

    # ── Hooks ──

    def _configure(self, **kwargs: Any) -> None:
        """Override for provider-specific setup (client, site_id, etc.)."""
        # Default no-op — adapters that need no extra config use it as-is.
        pass

    # ── Public logging helper ──

    def token_fp(self) -> str:
        return token_fingerprint(self._token)

    def _enforce_container_vulnerability_gate(
        self,
        build_artifact: BuildArtifact,
    ) -> None:
        """Block deploys when SC.4 reports HIGH/CRITICAL artifact CVEs.

        Module-global state audit: this helper reads no mutable
        module-global state; every worker derives the gate decision from
        the passed ``BuildArtifact`` and the scanner report produced for
        that artifact.
        """
        from backend.security_scanning import scan_container_artifact

        report = scan_container_artifact(build_artifact)
        if report.passed:
            return

        blocking = report.blocking_findings
        if blocking:
            sample = ", ".join(
                f"{finding.vulnerability_id or finding.package}:{finding.severity}"
                for finding in blocking[:3]
            )
            suffix = f" ({sample})" if sample else ""
            raise ContainerVulnerabilityBlockError(
                "Container vulnerability scan blocked deploy: "
                f"{len(blocking)} HIGH/CRITICAL finding(s){suffix}",
                provider=self.provider,
            )

        raise ContainerVulnerabilityBlockError(
            "Container vulnerability scan failed before deploy: "
            f"{report.error or 'unknown scanner failure'}",
            provider=self.provider,
        )

    @property
    def project_name(self) -> str:
        return self._project_name

    @property
    def project_id(self) -> Optional[str]:
        return self._project_id

    # ── Abstract interface ──

    @abstractmethod
    async def provision(
        self,
        *,
        env: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> ProvisionResult:
        """Ensure the remote project exists and env vars are applied.

        Must be idempotent — callers run this every deploy to reconcile
        config drift. If the project already exists, the adapter
        returns the existing record with ``created=False``.
        """

    @abstractmethod
    async def deploy(self, build_artifact: BuildArtifact) -> DeployResult:
        """Upload the local build output and trigger a new deployment."""

    @abstractmethod
    async def rollback(
        self,
        *,
        deployment_id: Optional[str] = None,
    ) -> DeployResult:
        """Roll back to the previous successful deployment (or the one
        named by ``deployment_id``). Raises ``RollbackUnavailableError``
        when no prior deployment exists."""

    @abstractmethod
    def get_url(self) -> Optional[str]:
        """Return the current production URL, or ``None`` if the
        project has not been deployed yet. Synchronous — adapters cache
        the URL after ``provision()`` / ``deploy()``."""


__all__ = [
    "WebDeployAdapter",
    "BuildArtifact",
    "ProvisionResult",
    "DeployResult",
    "DeployError",
    "InvalidDeployTokenError",
    "MissingDeployScopeError",
    "DeployConflictError",
    "DeployRateLimitError",
    "DeployArtifactError",
    "ContainerVulnerabilityBlockError",
    "RollbackUnavailableError",
    "token_fingerprint",
]
