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
from backend.db_provisioning.encryption import (
    EncryptionAtRestPolicy,
    EncryptionAtRestUnsupportedTierError,
    encryption_supported_tiers,
    normalize_provider_tier,
    plan_encryption_at_rest,
)
from backend.db_provisioning.migrations import (
    DBMigrationCommandError,
    DBMigrationError,
    DBMigrationResult,
    UnsupportedDBMigrationToolError,
    build_migration_command,
    run_tenant_migrations,
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
    "EncryptionAtRestPolicy",
    "EncryptionAtRestUnsupportedTierError",
    "InvalidDBProvisionTokenError",
    "MissingDBProvisionScopeError",
    "DBMigrationCommandError",
    "DBMigrationError",
    "DBMigrationResult",
    "UnsupportedDBMigrationToolError",
    "build_migration_command",
    "encryption_supported_tiers",
    "get_adapter",
    "list_providers",
    "normalize_provider_tier",
    "plan_encryption_at_rest",
    "run_tenant_migrations",
]
