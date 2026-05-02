"""FS.3.1 -- Supabase Storage bucket provisioning adapter."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from backend.storage_provisioning.base import (
    InvalidStorageProvisionTokenError,
    MissingStorageProvisionScopeError,
    StorageProvisionAdapter,
    StorageProvisionConflictError,
    StorageProvisionError,
    StorageProvisionRateLimitError,
    StorageProvisionResult,
)

logger = logging.getLogger(__name__)

SUPABASE_STORAGE_API_BASE = "https://api.supabase.com"


def _raise_for_supabase_storage(
    resp: httpx.Response,
    provider: str = "supabase-storage",
) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    msg = body.get("message") or body.get("error") or resp.text or "unknown error"
    if resp.status_code == 401:
        raise InvalidStorageProvisionTokenError(msg, status=401, provider=provider)
    if resp.status_code == 403:
        raise MissingStorageProvisionScopeError(msg, status=403, provider=provider)
    if resp.status_code in (409, 422):
        raise StorageProvisionConflictError(msg, status=resp.status_code, provider=provider)
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", "60"))
        raise StorageProvisionRateLimitError(msg, retry_after=retry, status=429, provider=provider)
    raise StorageProvisionError(msg, status=resp.status_code, provider=provider)


class SupabaseStorageProvisionAdapter(StorageProvisionAdapter):
    """Supabase Storage API adapter (``provider='supabase-storage'``)."""

    provider = "supabase-storage"

    def _configure(
        self,
        *,
        project_ref: str,
        public: bool = False,
        file_size_limit: Optional[int] = None,
        allowed_mime_types: Optional[list[str]] = None,
        api_base: str = SUPABASE_STORAGE_API_BASE,
        **_: Any,
    ) -> None:
        if not project_ref:
            raise ValueError("SupabaseStorageProvisionAdapter requires project_ref")
        self._project_ref = project_ref
        self._public = public
        self._file_size_limit = file_size_limit
        self._allowed_mime_types = tuple(allowed_mime_types or ())
        self._api_base = api_base.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _storage_base(self) -> str:
        return f"{self._api_base}/v1/projects/{self._project_ref}/storage"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
    ) -> dict:
        url = f"{self._storage_base()}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.request(method, url, headers=self._headers(), json=json)
        if method == "GET" and resp.status_code == 404:
            return {}
        _raise_for_supabase_storage(resp, self.provider)
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    async def _get_bucket(self) -> Optional[dict]:
        data = await self._request("GET", f"/buckets/{self._bucket_name}")
        return data or None

    async def _create_bucket(self) -> dict:
        body: dict[str, Any] = {
            "id": self._bucket_name,
            "name": self._bucket_name,
            "public": self._public,
        }
        if self._file_size_limit is not None:
            body["file_size_limit"] = self._file_size_limit
        if self._allowed_mime_types:
            body["allowed_mime_types"] = list(self._allowed_mime_types)
        return await self._request("POST", "/buckets", json=body)

    async def provision_bucket(self, **kwargs: Any) -> StorageProvisionResult:
        existing = await self._get_bucket()
        created = False
        if existing:
            bucket = existing
        else:
            bucket = await self._create_bucket()
            created = True
        bucket_id = bucket.get("id") or bucket.get("name") or self._bucket_name
        public_url = (
            f"https://{self._project_ref}.supabase.co/storage/v1/object/public/"
            f"{self._bucket_name}"
            if self._public
            else None
        )
        logger.info(
            "supabase_storage.storage_provision bucket=%s id=%s created=%s fp=%s",
            self._bucket_name, bucket_id, created, self.token_fp(),
        )
        result = StorageProvisionResult(
            provider=self.provider,
            bucket_name=self._bucket_name,
            bucket_id=bucket_id,
            endpoint_url=self._storage_base(),
            public_url=public_url,
            status=bucket.get("status") or "ready",
            created=created,
            region=bucket.get("region"),
            raw=bucket,
        )
        self._cached_result = result
        return result


__all__ = ["SUPABASE_STORAGE_API_BASE", "SupabaseStorageProvisionAdapter"]
