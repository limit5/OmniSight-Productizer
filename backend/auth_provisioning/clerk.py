"""FS.2.1 -- Clerk Backend API inbound auth provisioning adapter."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from backend.auth_provisioning.base import (
    AuthProviderSetupResult,
    AuthProvisionAdapter,
    AuthProvisionConflictError,
    AuthProvisionError,
    AuthProvisionRateLimitError,
    InvalidAuthProvisionTokenError,
    MissingAuthProvisionScopeError,
)

logger = logging.getLogger(__name__)

CLERK_API_BASE = "https://api.clerk.com/v1"


def _raise_for_clerk(resp: httpx.Response, provider: str = "clerk") -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    errors = body.get("errors") if isinstance(body, dict) else None
    msg = ""
    if errors and isinstance(errors, list):
        first = errors[0] if errors else {}
        msg = first.get("message") or first.get("long_message") or ""
    msg = msg or body.get("message") or body.get("error") or resp.text or "unknown error"
    if resp.status_code == 401:
        raise InvalidAuthProvisionTokenError(msg, status=401, provider=provider)
    if resp.status_code == 403:
        raise MissingAuthProvisionScopeError(msg, status=403, provider=provider)
    if resp.status_code in (409, 422):
        raise AuthProvisionConflictError(msg, status=resp.status_code, provider=provider)
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", "60"))
        raise AuthProvisionRateLimitError(msg, retry_after=retry, status=429, provider=provider)
    raise AuthProvisionError(msg, status=resp.status_code, provider=provider)


class ClerkAuthProvisionAdapter(AuthProvisionAdapter):
    """Clerk Backend API adapter (``provider='clerk'``)."""

    provider = "clerk"

    def _configure(
        self,
        *,
        created_by: str,
        issuer_url: Optional[str] = None,
        publishable_key: Optional[str] = None,
        api_base: str = CLERK_API_BASE,
        **_: Any,
    ) -> None:
        if not created_by:
            raise ValueError("ClerkAuthProvisionAdapter requires created_by")
        self._created_by = created_by
        self._issuer_url = issuer_url
        self._publishable_key = publishable_key
        self._api_base = api_base.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict:
        url = f"{self._api_base}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.request(
                method,
                url,
                headers=self._headers(),
                json=json,
                params=params,
            )
        _raise_for_clerk(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {}

    async def _find_organization(self, slug: Optional[str]) -> Optional[dict]:
        data = await self._request(
            "GET",
            "/organizations",
            params={"query": slug or self._application_name, "limit": 10},
        )
        organizations = data.get("data") if isinstance(data, dict) else []
        for organization in organizations or []:
            if organization.get("slug") == slug or organization.get("name") == self._application_name:
                return organization
        return None

    async def _create_organization(
        self,
        *,
        slug: Optional[str],
        max_allowed_memberships: Optional[int],
        public_metadata: Optional[dict[str, Any]],
        private_metadata: Optional[dict[str, Any]],
    ) -> dict:
        body: dict[str, Any] = {
            "name": self._application_name,
            "created_by": self._created_by,
        }
        if slug:
            body["slug"] = slug
        if max_allowed_memberships is not None:
            body["max_allowed_memberships"] = max_allowed_memberships
        if public_metadata:
            body["public_metadata"] = public_metadata
        if private_metadata:
            body["private_metadata"] = private_metadata
        return await self._request("POST", "/organizations", json=body)

    async def setup_application(
        self,
        *,
        slug: Optional[str] = None,
        max_allowed_memberships: Optional[int] = None,
        redirect_uris: tuple[str, ...] = (),
        allowed_origins: tuple[str, ...] = (),
        public_metadata: Optional[dict[str, Any]] = None,
        private_metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> AuthProviderSetupResult:
        existing = await self._find_organization(slug)
        created = False
        if existing:
            organization = existing
        else:
            private = dict(private_metadata or {})
            private.setdefault("redirect_uris", list(redirect_uris))
            private.setdefault("allowed_origins", list(allowed_origins))
            organization = await self._create_organization(
                slug=slug,
                max_allowed_memberships=max_allowed_memberships,
                public_metadata=public_metadata,
                private_metadata=private,
            )
            created = True
        application_id = organization.get("id") or ""
        logger.info(
            "clerk.auth_provision application=%s id=%s created=%s fp=%s",
            self._application_name, application_id, created, self.token_fp(),
        )
        result = AuthProviderSetupResult(
            provider=self.provider,
            application_id=application_id,
            application_name=organization.get("name") or self._application_name,
            client_id=self._publishable_key,
            issuer_url=self._issuer_url,
            redirect_uris=tuple(redirect_uris),
            allowed_origins=tuple(allowed_origins),
            status="ready",
            created=created,
            raw=organization,
        )
        self._cached_result = result
        return result
