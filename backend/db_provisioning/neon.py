"""FS.1.1 — Neon API DB provisioning adapter."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from backend.db_provisioning.base import (
    DBProvisionAdapter,
    DatabaseProvisionResult,
    DBProvisionConflictError,
    DBProvisionError,
    DBProvisionRateLimitError,
    InvalidDBProvisionTokenError,
    MissingDBProvisionScopeError,
)

logger = logging.getLogger(__name__)

NEON_API_BASE = "https://console.neon.tech/api/v2"


def _raise_for_neon(resp: httpx.Response, provider: str = "neon") -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    msg = body.get("message") or body.get("error") or resp.text or "unknown error"
    if resp.status_code == 401:
        raise InvalidDBProvisionTokenError(msg, status=401, provider=provider)
    if resp.status_code == 403:
        raise MissingDBProvisionScopeError(msg, status=403, provider=provider)
    if resp.status_code in (409, 422):
        raise DBProvisionConflictError(msg, status=resp.status_code, provider=provider)
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", "60"))
        raise DBProvisionRateLimitError(msg, retry_after=retry, status=429, provider=provider)
    raise DBProvisionError(msg, status=resp.status_code, provider=provider)


class NeonDBProvisionAdapter(DBProvisionAdapter):
    """Neon API adapter (``provider='neon'``)."""

    provider = "neon"

    def _configure(
        self,
        *,
        region_id: str = "aws-us-east-1",
        api_base: str = NEON_API_BASE,
        **_: Any,
    ) -> None:
        self._region_id = region_id
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
    ) -> dict:
        url = f"{self._api_base}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.request(method, url, headers=self._headers(), json=json)
        _raise_for_neon(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {}

    async def _find_project(self) -> Optional[dict]:
        data = await self._request("GET", "/projects")
        projects = data.get("projects") if isinstance(data, dict) else []
        for project in projects or []:
            if project.get("name") == self._database_name:
                return project
        return None

    async def _create_project(self, *, pg_version: Optional[int] = None) -> dict:
        project_body: dict[str, Any] = {
            "name": self._database_name,
            "region_id": self._region_id,
        }
        if pg_version is not None:
            project_body["pg_version"] = pg_version
        return await self._request("POST", "/projects", json={"project": project_body})

    def _extract_connection_url(self, data: dict) -> Optional[str]:
        if data.get("connection_uri"):
            return data["connection_uri"]
        for item in data.get("connection_uris") or []:
            if item.get("connection_uri"):
                return item["connection_uri"]
        return None

    async def provision_database(
        self,
        *,
        pg_version: Optional[int] = None,
        **kwargs: Any,
    ) -> DatabaseProvisionResult:
        existing = await self._find_project()
        created = False
        raw: dict[str, Any]
        if existing:
            project = existing
            raw = {"project": project}
        else:
            raw = await self._create_project(pg_version=pg_version)
            project = raw.get("project") or {}
            created = True
        connection_url = self._extract_connection_url(raw)
        self._cached_connection_url = connection_url
        logger.info(
            "neon.db_provision project=%s id=%s created=%s fp=%s",
            self._database_name, project.get("id", ""), created, self.token_fp(),
        )
        return DatabaseProvisionResult(
            provider=self.provider,
            database_id=project.get("id") or "",
            database_name=self._database_name,
            connection_url=connection_url,
            status=project.get("provisioner") or project.get("status") or "ready",
            created=created,
            region=project.get("region_id") or self._region_id,
            raw=raw,
        )

    def get_connection_url(self) -> Optional[str]:
        return self._cached_connection_url
