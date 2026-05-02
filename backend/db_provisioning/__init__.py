"""FS.1.1 — Database provisioning adapters package."""

from __future__ import annotations

from backend.db_provisioning.base import (
    DBProvisionAdapter,
    DatabaseProvisionResult,
    DBProvisionConflictError,
    DBProvisionError,
    DBProvisionRateLimitError,
    InvalidDBProvisionTokenError,
    MissingDBProvisionScopeError,
)


def list_providers() -> list[str]:
    """Return the canonical id for every shipped DB provisioning adapter."""
    return ["supabase", "neon", "planetscale"]


def get_adapter(provider: str) -> type[DBProvisionAdapter]:
    """Look up an adapter class by canonical provider string."""
    key = provider.strip().lower().replace("_", "-")
    if key == "supabase":
        from backend.db_provisioning.supabase import SupabaseDBProvisionAdapter
        return SupabaseDBProvisionAdapter
    if key == "neon":
        from backend.db_provisioning.neon import NeonDBProvisionAdapter
        return NeonDBProvisionAdapter
    if key in ("planetscale", "planet-scale"):
        from backend.db_provisioning.planetscale import PlanetScaleDBProvisionAdapter
        return PlanetScaleDBProvisionAdapter
    raise ValueError(
        f"Unknown DB provisioning provider '{provider}'. "
        f"Expected one of: {', '.join(list_providers())}"
    )


__all__ = [
    "DBProvisionAdapter",
    "DatabaseProvisionResult",
    "DBProvisionConflictError",
    "DBProvisionError",
    "DBProvisionRateLimitError",
    "InvalidDBProvisionTokenError",
    "MissingDBProvisionScopeError",
    "get_adapter",
    "list_providers",
]
