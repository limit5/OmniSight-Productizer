"""FS.3.1 -- Object storage provisioning adapters package."""

from __future__ import annotations

from backend.storage_provisioning.base import (
    InvalidStorageProvisionTokenError,
    MissingStorageProvisionScopeError,
    PresignedStorageUrl,
    StorageCorsConfig,
    StorageCorsResult,
    StorageProvisionAdapter,
    StorageProvisionConflictError,
    StorageProvisionError,
    StorageProvisionRateLimitError,
    StorageProvisionResult,
)


def list_providers() -> list[str]:
    """Return the canonical id for every shipped storage provisioning adapter."""
    return ["s3", "r2", "supabase-storage"]


def get_adapter(provider: str) -> type[StorageProvisionAdapter]:
    """Look up an adapter class by canonical provider string."""
    key = provider.strip().lower().replace("_", "-")
    if key == "s3":
        from backend.storage_provisioning.s3 import S3StorageProvisionAdapter
        return S3StorageProvisionAdapter
    if key == "r2":
        from backend.storage_provisioning.r2 import R2StorageProvisionAdapter
        return R2StorageProvisionAdapter
    if key in ("supabase", "supabase-storage"):
        from backend.storage_provisioning.supabase import SupabaseStorageProvisionAdapter
        return SupabaseStorageProvisionAdapter
    raise ValueError(
        f"Unknown storage provisioning provider '{provider}'. "
        f"Expected one of: {', '.join(list_providers())}"
    )


__all__ = [
    "StorageProvisionAdapter",
    "PresignedStorageUrl",
    "StorageCorsConfig",
    "StorageCorsResult",
    "StorageProvisionResult",
    "StorageProvisionConflictError",
    "StorageProvisionError",
    "StorageProvisionRateLimitError",
    "InvalidStorageProvisionTokenError",
    "MissingStorageProvisionScopeError",
    "get_adapter",
    "list_providers",
]
