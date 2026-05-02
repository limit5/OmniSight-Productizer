"""FS.3.1 -- Cloudflare R2 object storage provisioning adapter."""

from __future__ import annotations

from typing import Any, Optional

from backend.storage_provisioning.s3 import S3CompatibleStorageProvisionAdapter


class R2StorageProvisionAdapter(S3CompatibleStorageProvisionAdapter):
    """Cloudflare R2 S3-compatible adapter (``provider='r2'``)."""

    provider = "r2"

    def _configure(
        self,
        *,
        access_key_id: str,
        account_id: str,
        region: str = "auto",
        endpoint_url: Optional[str] = None,
        public_url: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        if not account_id:
            raise ValueError("R2StorageProvisionAdapter requires account_id")
        super()._configure(
            access_key_id=access_key_id,
            region=region,
            endpoint_url=endpoint_url or f"https://{account_id}.r2.cloudflarestorage.com",
            public_url=public_url,
            **kwargs,
        )
        self._account_id = account_id


__all__ = ["R2StorageProvisionAdapter"]
