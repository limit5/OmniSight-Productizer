"""FS.3.1 -- AWS S3 object storage provisioning adapter."""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlparse

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

S3_API_BASE = "https://s3.amazonaws.com"
S3_SERVICE = "s3"


def _raise_for_s3(resp: httpx.Response, provider: str) -> None:
    if resp.status_code < 400:
        return
    msg = resp.text or resp.reason_phrase or "unknown error"
    if resp.status_code == 401:
        raise InvalidStorageProvisionTokenError(msg, status=401, provider=provider)
    if resp.status_code == 403:
        raise MissingStorageProvisionScopeError(msg, status=403, provider=provider)
    if resp.status_code in (409, 422):
        raise StorageProvisionConflictError(msg, status=resp.status_code, provider=provider)
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", "60"))
        raise StorageProvisionRateLimitError(
            msg,
            retry_after=retry,
            status=429,
            provider=provider,
        )
    raise StorageProvisionError(msg, status=resp.status_code, provider=provider)


def _signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    key_date = hmac.new(f"AWS4{secret}".encode(), datestamp.encode(), hashlib.sha256).digest()
    key_region = hmac.new(key_date, region.encode(), hashlib.sha256).digest()
    key_service = hmac.new(key_region, service.encode(), hashlib.sha256).digest()
    return hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()


class S3CompatibleStorageProvisionAdapter(StorageProvisionAdapter):
    """Shared S3-compatible bucket provisioning implementation."""

    service = S3_SERVICE

    def _configure(
        self,
        *,
        access_key_id: str,
        region: str = "us-east-1",
        endpoint_url: str = S3_API_BASE,
        public_url: Optional[str] = None,
        **_: Any,
    ) -> None:
        if not access_key_id:
            raise ValueError(f"{type(self).__name__} requires access_key_id")
        self._access_key_id = access_key_id
        self._region = region
        self._endpoint_url = endpoint_url.rstrip("/")
        self._public_url = public_url.rstrip("/") if public_url else None

    def _bucket_url(self) -> str:
        return f"{self._endpoint_url}/{quote(self._bucket_name, safe='')}"

    def _payload(self) -> bytes:
        if self._region == "us-east-1":
            return b""
        return (
            b"<CreateBucketConfiguration xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
            b"<LocationConstraint>"
            + self._region.encode()
            + b"</LocationConstraint></CreateBucketConfiguration>"
        )

    def _headers(self, method: str, url: str, payload: bytes) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()
        parsed = urlparse(url)
        host = parsed.netloc
        canonical_uri = parsed.path or "/"
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{key}:{headers[key]}\n" for key in sorted(headers))
        canonical_request = "\n".join([
            method,
            canonical_uri,
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        scope = f"{datestamp}/{self._region}/{self.service}/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])
        signature = hmac.new(
            _signing_key(self._token, datestamp, self._region, self.service),
            string_to_sign.encode(),
            hashlib.sha256,
        ).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    async def _request(self, method: str, url: str, payload: bytes = b"") -> httpx.Response:
        headers = self._headers(method, url, payload)
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            return await c.request(method, url, headers=headers, content=payload)

    async def _bucket_exists(self) -> bool:
        resp = await self._request("HEAD", self._bucket_url())
        if resp.status_code == 404:
            return False
        _raise_for_s3(resp, self.provider)
        return True

    async def provision_bucket(self, **kwargs: Any) -> StorageProvisionResult:
        created = False
        if not await self._bucket_exists():
            payload = self._payload()
            resp = await self._request("PUT", self._bucket_url(), payload)
            _raise_for_s3(resp, self.provider)
            created = True
        logger.info(
            "%s.storage_provision bucket=%s created=%s fp=%s",
            self.provider, self._bucket_name, created, self.token_fp(),
        )
        result = StorageProvisionResult(
            provider=self.provider,
            bucket_name=self._bucket_name,
            bucket_id=self._bucket_name,
            endpoint_url=self._endpoint_url,
            public_url=self._public_url,
            status="ready",
            created=created,
            region=self._region,
            raw={"bucket_url": self._bucket_url()},
        )
        self._cached_result = result
        return result


class S3StorageProvisionAdapter(S3CompatibleStorageProvisionAdapter):
    """AWS S3 REST API adapter (``provider='s3'``)."""

    provider = "s3"


__all__ = [
    "S3_API_BASE",
    "S3StorageProvisionAdapter",
    "S3CompatibleStorageProvisionAdapter",
]
