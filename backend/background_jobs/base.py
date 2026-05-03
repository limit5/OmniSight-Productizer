"""FS.5.1 -- Unified background job adapter interface.

Inngest / Trigger.dev / Vercel Cron expose different primitives for
background execution. This module mirrors ``backend.email_delivery.base``:
callers construct a provider adapter from an encrypted or plaintext
token, call ``dispatch_job()``, then hand the returned provider run id to
the caller-owned workflow.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable classes/functions only. No module-level
cache, singleton, or mutable registry is read or written; provider
factory functions in ``backend.background_jobs`` materialize fresh lists
per call, so uvicorn workers do not share runtime state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from backend import secret_store
from backend.deploy.base import token_fingerprint


class BackgroundJobError(Exception):
    """Base for all background job adapter errors."""

    def __init__(self, message: str, status: int = 0, provider: str = ""):
        super().__init__(message)
        self.status = status
        self.provider = provider


class InvalidBackgroundJobTokenError(BackgroundJobError):
    """401 -- API token invalid / revoked."""


class MissingBackgroundJobScopeError(BackgroundJobError):
    """403 -- API token lacks required permission."""


class BackgroundJobConflictError(BackgroundJobError):
    """409 / 422 -- provider rejected job identity or payload shape."""


class BackgroundJobRateLimitError(BackgroundJobError):
    """429 -- provider rate limit hit."""

    def __init__(self, message: str, retry_after: int = 60, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


@dataclass
class BackgroundJobRequest:
    """Provider-neutral request to dispatch one background job."""

    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    cron: str | None = None
    endpoint_path: str | None = None

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("job name is required")
        if self.idempotency_key is not None:
            self.idempotency_key = self.idempotency_key.strip() or None
        if self.cron is not None:
            self.cron = self.cron.strip() or None
        if self.endpoint_path is not None:
            path = self.endpoint_path.strip()
            self.endpoint_path = path if path.startswith("/") else f"/{path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "payload": dict(self.payload),
            "idempotency_key": self.idempotency_key,
            "cron": self.cron,
            "endpoint_path": self.endpoint_path,
        }


@dataclass
class BackgroundJobResult:
    """Outcome of ``adapter.dispatch_job(...)``."""

    provider: str
    job_id: str
    status: str = "queued"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "job_id": self.job_id,
            "status": self.status,
        }


@dataclass
class CronDescriptor:
    """Provider-specific representation of a cron-backed job."""

    provider: str
    name: str
    schedule: str
    target: str
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "name": self.name,
            "schedule": self.schedule,
            "target": self.target,
        }


class BackgroundJobAdapter(ABC):
    """Abstract base for every background job provider adapter."""

    provider: ClassVar[str] = ""

    def __init__(
        self,
        *,
        token: str,
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        if not self.provider:
            raise ValueError(f"{type(self).__name__} must set classvar 'provider'")
        self._token = token
        self._timeout = timeout
        self._configure(**kwargs)

    @classmethod
    def from_encrypted_token(
        cls,
        ciphertext: str,
        **kwargs: Any,
    ) -> "BackgroundJobAdapter":
        """Decrypt via ``backend.secret_store`` and build an adapter."""
        token = secret_store.decrypt(ciphertext)
        return cls(token=token, **kwargs)

    @classmethod
    def from_plaintext_token(
        cls,
        token: str,
        **kwargs: Any,
    ) -> "BackgroundJobAdapter":
        """Build an adapter from a plaintext token for tests / CLI paths."""
        return cls(token=token, **kwargs)

    def _configure(self, **kwargs: Any) -> None:
        """Override for provider-specific setup."""
        pass

    def token_fp(self) -> str:
        return token_fingerprint(self._token)

    @abstractmethod
    async def dispatch_job(
        self,
        request: BackgroundJobRequest,
        **kwargs: Any,
    ) -> BackgroundJobResult:
        """Dispatch one background job via the provider API."""

    @abstractmethod
    def cron_descriptor(self, request: BackgroundJobRequest) -> CronDescriptor:
        """Return the provider-specific cron descriptor for this job."""


__all__ = [
    "BackgroundJobAdapter",
    "BackgroundJobConflictError",
    "BackgroundJobError",
    "BackgroundJobRateLimitError",
    "BackgroundJobRequest",
    "BackgroundJobResult",
    "CronDescriptor",
    "InvalidBackgroundJobTokenError",
    "MissingBackgroundJobScopeError",
]
