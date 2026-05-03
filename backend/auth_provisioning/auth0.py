"""FS.2.1 -- Auth0 Management API inbound auth provisioning adapter."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

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

logger = logging.getLogger(__name__)


def _raise_for_auth0(resp: httpx.Response, provider: str = "auth0") -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    msg = (
        body.get("message")
        or body.get("error_description")
        or body.get("error")
        or resp.text
        or "unknown error"
    )
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


class Auth0AuthProvisionAdapter(AuthProvisionAdapter):
    """Auth0 Management API adapter (``provider='auth0'``)."""

    provider = "auth0"

    def _configure(
        self,
        *,
        tenant_domain: str,
        app_type: str = "regular_web",
        api_base: Optional[str] = None,
        **_: Any,
    ) -> None:
        if not tenant_domain:
            raise ValueError("Auth0AuthProvisionAdapter requires tenant_domain")
        self._tenant_domain = tenant_domain.rstrip("/")
        self._app_type = app_type
        self._api_base = (api_base or f"https://{self._tenant_domain}/api/v2").rstrip("/")
        self._issuer_url = f"https://{self._tenant_domain}/"

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
    ) -> dict | list:
        url = f"{self._api_base}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.request(
                method,
                url,
                headers=self._headers(),
                json=json,
                params=params,
            )
        _raise_for_auth0(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {}

    async def _find_client(self) -> Optional[dict]:
        data = await self._request(
            "GET",
            "/clients",
            params={
                "fields": "client_id,name,app_type,callbacks,allowed_logout_urls,web_origins",
                "include_fields": "true",
            },
        )
        clients = data if isinstance(data, list) else []
        for client in clients:
            if client.get("name") == self._application_name:
                return client
        return None

    async def _create_client(
        self,
        *,
        redirect_uris: tuple[str, ...],
        allowed_logout_urls: tuple[str, ...],
        allowed_origins: tuple[str, ...],
        grant_types: tuple[str, ...],
    ) -> dict:
        body = {
            "name": self._application_name,
            "app_type": self._app_type,
            "callbacks": list(redirect_uris),
            "allowed_logout_urls": list(allowed_logout_urls),
            "web_origins": list(allowed_origins),
            "oidc_conformant": True,
            "grant_types": list(grant_types),
        }
        data = await self._request("POST", "/clients", json=body)
        return data if isinstance(data, dict) else {}

    async def setup_application(
        self,
        *,
        redirect_uris: tuple[str, ...],
        allowed_logout_urls: tuple[str, ...] = (),
        allowed_origins: tuple[str, ...] = (),
        grant_types: tuple[str, ...] = ("authorization_code", "refresh_token"),
        scopes: tuple[str, ...] = DEFAULT_OIDC_SCOPES,
        **kwargs: Any,
    ) -> AuthProviderSetupResult:
        existing = await self._find_client()
        created = False
        if existing:
            client = existing
        else:
            client = await self._create_client(
                redirect_uris=redirect_uris,
                allowed_logout_urls=allowed_logout_urls,
                allowed_origins=allowed_origins,
                grant_types=grant_types,
            )
            created = True
        logger.info(
            "auth0.auth_provision application=%s id=%s created=%s fp=%s",
            self._application_name, client.get("client_id", ""), created, self.token_fp(),
        )
        result = AuthProviderSetupResult(
            provider=self.provider,
            application_id=client.get("client_id") or "",
            application_name=client.get("name") or self._application_name,
            client_id=client.get("client_id"),
            client_secret=client.get("client_secret"),
            issuer_url=self._issuer_url,
            redirect_uris=tuple(client.get("callbacks") or redirect_uris),
            allowed_origins=tuple(client.get("web_origins") or allowed_origins),
            scopes=tuple(scopes),
            status=client.get("app_type") or self._app_type,
            created=created,
            raw=client,
        )
        self._cached_result = result
        return result
