"""FS.2.1 -- Unified inbound auth provisioning adapter interface.

Clerk / Auth0 / WorkOS expose management APIs that can prepare the
provider-side resources a generated app needs before FS.2.2+ renders
framework-specific auth code. This module mirrors
``backend.db_provisioning.base``: callers construct a provider adapter
from an encrypted or plaintext token, call ``setup_application()``, then
hand the returned client metadata to the generated app scaffold.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
This module defines immutable classes/functions only. No module-level
cache, singleton, or mutable registry is read or written; provider
factory functions in ``backend.auth_provisioning`` materialize fresh
lists per call, so uvicorn workers do not share runtime state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

from backend import secret_store
from backend.deploy.base import token_fingerprint


DEFAULT_OIDC_SCOPES: tuple[str, ...] = ("openid", "email", "profile")


class AuthProvisionError(Exception):
    """Base for all inbound auth provisioning adapter errors."""

    def __init__(self, message: str, status: int = 0, provider: str = ""):
        super().__init__(message)
        self.status = status
        self.provider = provider


class InvalidAuthProvisionTokenError(AuthProvisionError):
    """401 -- management token invalid / revoked."""


class MissingAuthProvisionScopeError(AuthProvisionError):
    """403 -- management token lacks required permission."""


class AuthProvisionConflictError(AuthProvisionError):
    """409 / 422 -- application or organization already exists."""


class AuthProvisionRateLimitError(AuthProvisionError):
    """429 -- provider rate limit hit."""

    def __init__(self, message: str, retry_after: int = 60, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


@dataclass
class AuthProviderSetupResult:
    """Outcome of ``adapter.setup_application(...)``."""

    provider: str
    application_id: str
    application_name: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    issuer_url: Optional[str] = None
    redirect_uris: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    scopes: tuple[str, ...] = DEFAULT_OIDC_SCOPES
    require_mfa: bool = False
    status: str = "ready"
    created: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "application_id": self.application_id,
            "application_name": self.application_name,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "issuer_url": self.issuer_url,
            "redirect_uris": list(self.redirect_uris),
            "allowed_origins": list(self.allowed_origins),
            "scopes": list(self.scopes),
            "require_mfa": self.require_mfa,
            "status": self.status,
            "created": self.created,
        }


class AuthProvisionAdapter(ABC):
    """Abstract base for every inbound auth provisioning provider adapter."""

    provider: ClassVar[str] = ""

    def __init__(
        self,
        *,
        token: str,
        application_name: str,
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        if not self.provider:
            raise ValueError(f"{type(self).__name__} must set classvar 'provider'")
        if not application_name:
            raise ValueError("application_name is required")
        self._token = token
        self._application_name = application_name
        self._timeout = timeout
        self._cached_result: Optional[AuthProviderSetupResult] = None
        self._configure(**kwargs)

    @classmethod
    def from_encrypted_token(
        cls,
        ciphertext: str,
        *,
        application_name: str,
        **kwargs: Any,
    ) -> "AuthProvisionAdapter":
        """Decrypt via ``backend.secret_store`` and build an adapter."""
        token = secret_store.decrypt(ciphertext)
        return cls(token=token, application_name=application_name, **kwargs)

    @classmethod
    def from_plaintext_token(
        cls,
        token: str,
        *,
        application_name: str,
        **kwargs: Any,
    ) -> "AuthProvisionAdapter":
        """Build an adapter from a plaintext token for tests / CLI paths."""
        return cls(token=token, application_name=application_name, **kwargs)

    def _configure(self, **kwargs: Any) -> None:
        """Override for provider-specific setup."""
        pass

    def token_fp(self) -> str:
        return token_fingerprint(self._token)

    @property
    def application_name(self) -> str:
        return self._application_name

    @abstractmethod
    async def setup_application(self, **kwargs: Any) -> AuthProviderSetupResult:
        """Create or reuse the provider-side inbound auth resource."""

    def get_client_config(self) -> Optional[dict[str, Any]]:
        """Return scaffold-facing OAuth/OIDC config from the last setup call."""
        if self._cached_result is None:
            return None
        return self._cached_result.to_dict()


__all__ = [
    "DEFAULT_OIDC_SCOPES",
    "AuthProvisionAdapter",
    "AuthProviderSetupResult",
    "AuthProvisionConflictError",
    "AuthProvisionError",
    "AuthProvisionRateLimitError",
    "InvalidAuthProvisionTokenError",
    "MissingAuthProvisionScopeError",
]
