"""FS.1.1 — Unified database provisioning adapter interface.

Supabase / Neon / PlanetScale each expose a management API for creating
a tenant-owned backend database. This module keeps the OmniSight-facing
contract narrow and mirrors ``backend.deploy.base``: callers construct a
provider adapter from an encrypted or plaintext token, call
``provision_database()``, then hand the returned connection URL to later
FS.1 migration/storage rows.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
This module defines immutable classes/functions only. No module-level
cache, singleton, or mutable registry is read or written; provider
factory functions in ``backend.db_provisioning`` materialize fresh lists
per call, so uvicorn workers do not share runtime state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

from backend import secret_store
from backend.deploy.base import token_fingerprint
from backend.db_provisioning.encryption import EncryptionAtRestPolicy


class DBProvisionError(Exception):
    """Base for all database provisioning adapter errors."""

    def __init__(self, message: str, status: int = 0, provider: str = ""):
        super().__init__(message)
        self.status = status
        self.provider = provider


class InvalidDBProvisionTokenError(DBProvisionError):
    """401 — management token invalid / revoked."""


class MissingDBProvisionScopeError(DBProvisionError):
    """403 — management token lacks required permission."""


class DBProvisionConflictError(DBProvisionError):
    """409 / 422 — database or project already exists."""


class DBProvisionRateLimitError(DBProvisionError):
    """429 — provider rate limit hit."""

    def __init__(self, message: str, retry_after: int = 60, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


@dataclass
class DatabaseProvisionResult:
    """Outcome of ``adapter.provision_database(...)``."""

    provider: str
    database_id: str
    database_name: str
    connection_url: Optional[str] = None
    status: str = "ready"
    created: bool = False
    region: Optional[str] = None
    encryption_at_rest: Optional[EncryptionAtRestPolicy] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "database_id": self.database_id,
            "database_name": self.database_name,
            "connection_url": self.connection_url,
            "status": self.status,
            "created": self.created,
            "region": self.region,
            "encryption_at_rest": (
                self.encryption_at_rest.to_dict()
                if self.encryption_at_rest is not None
                else None
            ),
        }


class DBProvisionAdapter(ABC):
    """Abstract base for every tenant DB provisioning provider adapter."""

    provider: ClassVar[str] = ""

    def __init__(
        self,
        *,
        token: str,
        database_name: str,
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        if not self.provider:
            raise ValueError(f"{type(self).__name__} must set classvar 'provider'")
        if not database_name:
            raise ValueError("database_name is required")
        self._token = token
        self._database_name = database_name
        self._timeout = timeout
        self._cached_connection_url: Optional[str] = None
        self._configure(**kwargs)

    @classmethod
    def from_encrypted_token(
        cls,
        ciphertext: str,
        *,
        database_name: str,
        **kwargs: Any,
    ) -> "DBProvisionAdapter":
        """Decrypt via ``backend.secret_store`` and build an adapter."""
        token = secret_store.decrypt(ciphertext)
        return cls(token=token, database_name=database_name, **kwargs)

    @classmethod
    def from_plaintext_token(
        cls,
        token: str,
        *,
        database_name: str,
        **kwargs: Any,
    ) -> "DBProvisionAdapter":
        """Build an adapter from a plaintext token for tests / CLI paths."""
        return cls(token=token, database_name=database_name, **kwargs)

    def _configure(self, **kwargs: Any) -> None:
        """Override for provider-specific setup."""
        pass

    def token_fp(self) -> str:
        return token_fingerprint(self._token)

    @property
    def database_name(self) -> str:
        return self._database_name

    @abstractmethod
    async def provision_database(self, **kwargs: Any) -> DatabaseProvisionResult:
        """Create or reuse the provider-side database/project."""

    @abstractmethod
    def get_connection_url(self) -> Optional[str]:
        """Return the cached connection URL from the last provision call."""


__all__ = [
    "DBProvisionAdapter",
    "DatabaseProvisionResult",
    "DBProvisionConflictError",
    "DBProvisionError",
    "DBProvisionRateLimitError",
    "EncryptionAtRestPolicy",
    "InvalidDBProvisionTokenError",
    "MissingDBProvisionScopeError",
]
