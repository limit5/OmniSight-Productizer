"""FS.3.1 -- Unified object storage provisioning adapter interface.

S3 / Cloudflare R2 / Supabase Storage expose management APIs that can
prepare a tenant-owned object bucket before later FS.3 rows add database
persistence and CORS automation. This module mirrors
``backend.db_provisioning.base``: callers construct a provider adapter
from an encrypted or plaintext token, call ``provision_bucket()``, then
hand the returned bucket metadata to downstream storage setup.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable classes/functions only. No module-level
cache, singleton, or mutable registry is read or written; provider
factory functions in ``backend.storage_provisioning`` materialize fresh
lists per call, so uvicorn workers do not share runtime state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

from backend import secret_store
from backend.deploy.base import token_fingerprint


class StorageProvisionError(Exception):
    """Base for all object storage provisioning adapter errors."""

    def __init__(self, message: str, status: int = 0, provider: str = ""):
        super().__init__(message)
        self.status = status
        self.provider = provider


class InvalidStorageProvisionTokenError(StorageProvisionError):
    """401 -- management token invalid / revoked."""


class MissingStorageProvisionScopeError(StorageProvisionError):
    """403 -- management token lacks required permission."""


class StorageProvisionConflictError(StorageProvisionError):
    """409 / 422 -- bucket already exists or is globally unavailable."""


class StorageProvisionRateLimitError(StorageProvisionError):
    """429 -- provider rate limit hit."""

    def __init__(self, message: str, retry_after: int = 60, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


@dataclass
class StorageProvisionResult:
    """Outcome of ``adapter.provision_bucket(...)``."""

    provider: str
    bucket_name: str
    bucket_id: str
    endpoint_url: Optional[str] = None
    public_url: Optional[str] = None
    status: str = "ready"
    created: bool = False
    region: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "bucket_name": self.bucket_name,
            "bucket_id": self.bucket_id,
            "endpoint_url": self.endpoint_url,
            "public_url": self.public_url,
            "status": self.status,
            "created": self.created,
            "region": self.region,
        }


@dataclass
class PresignedStorageUrl:
    """Outcome of ``adapter.generate_presigned_url(...)``."""

    provider: str
    bucket_name: str
    object_key: str
    url: str
    method: str = "GET"
    expires_in: int = 3600
    headers: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "bucket_name": self.bucket_name,
            "object_key": self.object_key,
            "url": self.url,
            "method": self.method,
            "expires_in": self.expires_in,
            "headers": dict(self.headers),
        }


class StorageProvisionAdapter(ABC):
    """Abstract base for every tenant object storage provider adapter."""

    provider: ClassVar[str] = ""

    def __init__(
        self,
        *,
        token: str,
        bucket_name: str,
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        if not self.provider:
            raise ValueError(f"{type(self).__name__} must set classvar 'provider'")
        if not bucket_name:
            raise ValueError("bucket_name is required")
        self._token = token
        self._bucket_name = bucket_name
        self._timeout = timeout
        self._cached_result: Optional[StorageProvisionResult] = None
        self._configure(**kwargs)

    @classmethod
    def from_encrypted_token(
        cls,
        ciphertext: str,
        *,
        bucket_name: str,
        **kwargs: Any,
    ) -> "StorageProvisionAdapter":
        """Decrypt via ``backend.secret_store`` and build an adapter."""
        token = secret_store.decrypt(ciphertext)
        return cls(token=token, bucket_name=bucket_name, **kwargs)

    @classmethod
    def from_plaintext_token(
        cls,
        token: str,
        *,
        bucket_name: str,
        **kwargs: Any,
    ) -> "StorageProvisionAdapter":
        """Build an adapter from a plaintext token for tests / CLI paths."""
        return cls(token=token, bucket_name=bucket_name, **kwargs)

    def _configure(self, **kwargs: Any) -> None:
        """Override for provider-specific setup."""
        pass

    def token_fp(self) -> str:
        return token_fingerprint(self._token)

    @property
    def bucket_name(self) -> str:
        return self._bucket_name

    @abstractmethod
    async def provision_bucket(self, **kwargs: Any) -> StorageProvisionResult:
        """Create or reuse the provider-side object storage bucket."""

    @abstractmethod
    async def generate_presigned_url(
        self,
        object_key: str,
        *,
        method: str = "GET",
        expires_in: int = 3600,
        **kwargs: Any,
    ) -> PresignedStorageUrl:
        """Return a short-lived object URL for generated app upload/download."""

    def get_bucket_config(self) -> Optional[dict[str, Any]]:
        """Return scaffold-facing bucket config from the last provision call."""
        if self._cached_result is None:
            return None
        return self._cached_result.to_dict()


__all__ = [
    "StorageProvisionAdapter",
    "PresignedStorageUrl",
    "StorageProvisionResult",
    "StorageProvisionConflictError",
    "StorageProvisionError",
    "StorageProvisionRateLimitError",
    "InvalidStorageProvisionTokenError",
    "MissingStorageProvisionScopeError",
]
