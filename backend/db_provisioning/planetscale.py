"""FS.1.1 — PlanetScale API DB provisioning adapter."""

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
from backend.db_provisioning.backup import plan_backup_schedule
from backend.db_provisioning.encryption import plan_encryption_at_rest
from backend.db_provisioning.pep_hold import plan_pep_hold

logger = logging.getLogger(__name__)

PLANETSCALE_API_BASE = "https://api.planetscale.com/v1"


def _raise_for_planetscale(resp: httpx.Response, provider: str = "planetscale") -> None:
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


class PlanetScaleDBProvisionAdapter(DBProvisionAdapter):
    """PlanetScale API adapter (``provider='planetscale'``)."""

    provider = "planetscale"

    def _configure(
        self,
        *,
        organization: str,
        region: str = "us-east",
        branch: str = "main",
        provider_tier: str = "scaler-pro",
        api_base: str = PLANETSCALE_API_BASE,
        **_: Any,
    ) -> None:
        if not organization:
            raise ValueError("PlanetScaleDBProvisionAdapter requires organization")
        self._organization = organization
        self._region = region
        self._branch = branch
        self._encryption_at_rest = plan_encryption_at_rest(self.provider, provider_tier)
        self._backup_schedule = plan_backup_schedule(self.provider, provider_tier)
        self._pep_hold = plan_pep_hold(self.provider, provider_tier)
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
        _raise_for_planetscale(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {}

    async def _get_database(self) -> Optional[dict]:
        try:
            data = await self._request(
                "GET",
                f"/organizations/{self._organization}/databases/{self._database_name}",
            )
        except DBProvisionError as exc:
            if exc.status == 404:
                return None
            raise
        return data if isinstance(data, dict) else {}

    async def _create_database(self) -> dict:
        body = {"name": self._database_name, "region": self._region}
        data = await self._request(
            "POST",
            f"/organizations/{self._organization}/databases",
            json=body,
        )
        return data if isinstance(data, dict) else {}

    async def _create_password(
        self,
        *,
        password_name: str,
        role: str,
        cidrs: Optional[list[str]],
    ) -> dict:
        body: dict[str, Any] = {"name": password_name, "role": role}
        if cidrs:
            body["cidrs"] = cidrs
        data = await self._request(
            "POST",
            f"/organizations/{self._organization}/databases/"
            f"{self._database_name}/branches/{self._branch}/passwords",
            json=body,
        )
        return data if isinstance(data, dict) else {}

    def _connection_url(self, password: dict) -> Optional[str]:
        user = password.get("username")
        plaintext = password.get("plain_text")
        host = password.get("access_host_url")
        if not user or not plaintext or not host:
            return None
        return (
            f"mysql://{quote(user, safe='')}:{quote(plaintext, safe='')}@{host}/"
            f"{quote(self._database_name, safe='')}?sslaccept=strict"
        )

    async def provision_database(
        self,
        *,
        password_name: str = "omnisight",
        role: str = "admin",
        cidrs: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> DatabaseProvisionResult:
        existing = await self._get_database()
        created = False
        if existing:
            database = existing
        else:
            database = await self._create_database()
            created = True
        password = await self._create_password(
            password_name=password_name,
            role=role,
            cidrs=cidrs,
        )
        connection_url = self._connection_url(password)
        self._cached_connection_url = connection_url
        database_id = database.get("id") or database.get("name") or self._database_name
        logger.info(
            "planetscale.db_provision database=%s id=%s created=%s fp=%s",
            self._database_name, database_id, created, self.token_fp(),
        )
        return DatabaseProvisionResult(
            provider=self.provider,
            database_id=database_id,
            database_name=self._database_name,
            connection_url=connection_url,
            status=database.get("state") or database.get("status") or "ready",
            created=created,
            region=database.get("region", {}).get("slug") or self._region,
            encryption_at_rest=self._encryption_at_rest,
            backup_schedule=self._backup_schedule,
            pep_hold=self._pep_hold,
            raw={"database": database, "password": password},
        )

    def get_connection_url(self) -> Optional[str]:
        return self._cached_connection_url
