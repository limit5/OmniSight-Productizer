"""FS.2.1 -- Inbound auth provisioning adapters package."""

from __future__ import annotations

from backend.auth_provisioning.base import (
    DEFAULT_OIDC_SCOPES,
    AuthProviderSetupResult,
    AuthProvisionAdapter,
    AuthProvisionConflictError,
    AuthProvisionError,
    AuthProvisionRateLimitError,
    InvalidAuthProvisionTokenError,
    MissingAuthProvisionScopeError,
)


def list_providers() -> list[str]:
    """Return the canonical id for every shipped auth provisioning adapter."""
    return ["clerk", "auth0", "workos"]


def get_adapter(provider: str) -> type[AuthProvisionAdapter]:
    """Look up an adapter class by canonical provider string."""
    key = provider.strip().lower().replace("_", "-")
    if key == "clerk":
        from backend.auth_provisioning.clerk import ClerkAuthProvisionAdapter
        return ClerkAuthProvisionAdapter
    if key == "auth0":
        from backend.auth_provisioning.auth0 import Auth0AuthProvisionAdapter
        return Auth0AuthProvisionAdapter
    if key == "workos":
        from backend.auth_provisioning.workos import WorkOSAuthProvisionAdapter
        return WorkOSAuthProvisionAdapter
    raise ValueError(
        f"Unknown auth provisioning provider '{provider}'. "
        f"Expected one of: {', '.join(list_providers())}"
    )


__all__ = [
    "DEFAULT_OIDC_SCOPES",
    "AuthProvisionAdapter",
    "AuthProviderSetupResult",
    "AuthProvisionConflictError",
    "AuthProvisionError",
    "AuthProvisionRateLimitError",
    "InvalidAuthProvisionTokenError",
    "MissingAuthProvisionScopeError",
    "get_adapter",
    "list_providers",
]
