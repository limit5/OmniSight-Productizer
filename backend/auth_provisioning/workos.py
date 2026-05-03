"""FS.2.1 -- WorkOS Connect inbound auth provisioning adapter."""

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

WORKOS_API_BASE = "https://api.workos.com"


def _raise_for_workos(resp: httpx.Response, provider: str = "workos") -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    msg = body.get("message") or body.get("error") or resp.text or "unknown error"
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


class WorkOSAuthProvisionAdapter(AuthProvisionAdapter):
    """WorkOS Connect Applications API adapter (``provider='workos'``)."""

    provider = "workos"

    def _configure(
        self,
        *,
        organization_id: Optional[str] = None,
        api_base: str = WORKOS_API_BASE,
        **_: Any,
    ) -> None:
        self._organization_id = organization_id
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
        _raise_for_workos(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {}

    async def _find_application(self) -> Optional[dict]:
        params: dict[str, Any] = {}
        if self._organization_id:
            params["organization_id"] = self._organization_id
        data = await self._request("GET", "/connect/applications", params=params)
        applications = data.get("data") if isinstance(data, dict) else []
        for application in applications or []:
            if application.get("name") == self._application_name:
                return application
        return None

    async def _create_application(
        self,
        *,
        redirect_uris: tuple[str, ...],
        scopes: tuple[str, ...],
        uses_pkce: bool,
        is_first_party: bool,
        description: Optional[str],
    ) -> dict:
        body: dict[str, Any] = {
            "name": self._application_name,
            "application_type": "oauth",
            "redirect_uris": [
                {"uri": uri, "default": index == 0}
                for index, uri in enumerate(redirect_uris)
            ],
            "uses_pkce": uses_pkce,
            "is_first_party": is_first_party,
            "scopes": list(scopes),
        }
        if description:
            body["description"] = description
        if self._organization_id:
            body["organization_id"] = self._organization_id
        data = await self._request("POST", "/connect/applications", json=body)
        return data.get("connect_application") if "connect_application" in data else data

    async def setup_application(
        self,
        *,
        redirect_uris: tuple[str, ...],
        scopes: tuple[str, ...] = DEFAULT_OIDC_SCOPES,
        uses_pkce: bool = True,
        is_first_party: bool = True,
        description: Optional[str] = None,
        **kwargs: Any,
    ) -> AuthProviderSetupResult:
        existing = await self._find_application()
        created = False
        if existing:
            application = existing
        else:
            application = await self._create_application(
                redirect_uris=redirect_uris,
                scopes=scopes,
                uses_pkce=uses_pkce,
                is_first_party=is_first_party,
                description=description,
            )
            created = True
        result_redirects = tuple(
            item.get("uri")
            for item in application.get("redirect_uris") or []
            if item.get("uri")
        ) or tuple(redirect_uris)
        application_id = application.get("id") or ""
        logger.info(
            "workos.auth_provision application=%s id=%s created=%s fp=%s",
            self._application_name, application_id, created, self.token_fp(),
        )
        result = AuthProviderSetupResult(
            provider=self.provider,
            application_id=application_id,
            application_name=application.get("name") or self._application_name,
            client_id=application.get("client_id"),
            issuer_url=self._api_base,
            redirect_uris=result_redirects,
            scopes=tuple(application.get("scopes") or scopes),
            status=application.get("application_type") or "oauth",
            created=created,
            raw=application,
        )
        self._cached_result = result
        return result
