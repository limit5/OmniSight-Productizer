"""FS.1.1 — Supabase Management API DB provisioning adapter."""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

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
from backend.db_provisioning.encryption import plan_encryption_at_rest

logger = logging.getLogger(__name__)

SUPABASE_API_BASE = "https://api.supabase.com/v1"


def _raise_for_supabase(resp: httpx.Response, provider: str = "supabase") -> None:
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


class SupabaseDBProvisionAdapter(DBProvisionAdapter):
    """Supabase Management API adapter (``provider='supabase'``)."""

    provider = "supabase"

    def _configure(
        self,
        *,
        organization_id: str,
        region: str = "us-east-1",
        provider_tier: str = "free",
        api_base: str = SUPABASE_API_BASE,
        **_: Any,
    ) -> None:
        if not organization_id:
            raise ValueError("SupabaseDBProvisionAdapter requires organization_id")
        self._organization_id = organization_id
        self._region = region
        self._encryption_at_rest = plan_encryption_at_rest(self.provider, provider_tier)
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
    ) -> dict | list:
        url = f"{self._api_base}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.request(method, url, headers=self._headers(), json=json)
        _raise_for_supabase(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {}

    async def _find_project(self) -> Optional[dict]:
        data = await self._request("GET", "/projects")
        if not isinstance(data, list):
            return None
        for project in data:
            if (
                project.get("name") == self._database_name
                and project.get("organization_id") == self._organization_id
            ):
                return project
        return None

    async def _create_project(
        self,
        *,
        db_pass: str,
        region: Optional[str] = None,
    ) -> dict:
        body = {
            "name": self._database_name,
            "organization_id": self._organization_id,
            "db_pass": db_pass,
            "region": region or self._region,
        }
        data = await self._request("POST", "/projects", json=body)
        return data if isinstance(data, dict) else {}

    def _connection_url(self, project: dict, db_pass: Optional[str]) -> Optional[str]:
        database = project.get("database") or {}
        host = database.get("host")
        ref = project.get("ref")
        if not host and ref:
            host = f"db.{ref}.supabase.co"
        if not host or not db_pass:
            return None
        user = f"postgres.{ref}" if ref else "postgres"
        return (
            f"postgresql://{quote(user, safe='')}:{quote(db_pass, safe='')}"
            f"@{host}:5432/postgres"
        )

    async def provision_database(
        self,
        *,
        db_pass: Optional[str] = None,
        region: Optional[str] = None,
        **kwargs: Any,
    ) -> DatabaseProvisionResult:
        existing = await self._find_project()
        created = False
        if existing:
            project = existing
        else:
            if not db_pass:
                raise ValueError("db_pass is required when creating a Supabase project")
            project = await self._create_project(db_pass=db_pass, region=region)
            created = True
        database_id = project.get("ref") or project.get("id") or ""
        connection_url = self._connection_url(project, db_pass)
        self._cached_connection_url = connection_url
        logger.info(
            "supabase.db_provision project=%s id=%s created=%s fp=%s",
            self._database_name, database_id, created, self.token_fp(),
        )
        return DatabaseProvisionResult(
            provider=self.provider,
            database_id=database_id,
            database_name=self._database_name,
            connection_url=connection_url,
            status=project.get("status") or "ready",
            created=created,
            region=project.get("region") or region or self._region,
            encryption_at_rest=self._encryption_at_rest,
            raw=project,
        )

    def get_connection_url(self) -> Optional[str]:
        return self._cached_connection_url
