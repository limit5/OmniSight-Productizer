"""W4 #278 — Netlify REST API deploy adapter.

Uses Netlify's v1 REST endpoints:

    GET    /api/v1/sites?name=<name>              find-by-name (list)
    POST   /api/v1/sites                          create site
    PATCH  /api/v1/sites/:site_id                 update env / build settings
    POST   /api/v1/sites/:site_id/deploys         create digest-based deploy
    PUT    /api/v1/deploys/:deploy_id/files/:path file upload (raw bytes)
    POST   /api/v1/sites/:site_id/rollback        rollback production
    POST   /api/v1/deploys/:deploy_id/restore     restore a specific deploy

Auth is a single personal access token (``NETLIFY_AUTH_TOKEN``). Team
scoping is implicit in the token.

Digest-based deploy model
-------------------------
Netlify wants a SHA1 manifest ``{path: sha1, ...}`` in the create-deploy
POST. It responds with ``required`` — the subset of SHA1s the server
does not already have cached. We then upload only those file bytes via
PUT. Saves upload time and lines up with the provider's recommended
workflow (same pattern the netlify CLI uses internally).

Env vars
--------
Netlify exposes env as a site-level ``build_settings.env`` dict. We set
them via PATCH /api/v1/sites/:site_id with ``{"build_settings": {"env":
{...}}}``. Newer sites may also live under the Account Env API
(``/accounts/:account_slug/env``) but the site-scoped variant is
sufficient for the one-click provisioner.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from backend.deploy.base import (
    BuildArtifact,
    DeployArtifactError,
    DeployConflictError,
    DeployError,
    DeployRateLimitError,
    DeployResult,
    InvalidDeployTokenError,
    MissingDeployScopeError,
    ProvisionResult,
    RollbackUnavailableError,
    WebDeployAdapter,
)

logger = logging.getLogger(__name__)

NETLIFY_API_BASE = "https://api.netlify.com/api/v1"


def _raise_for_netlify(resp: httpx.Response, provider: str = "netlify") -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    msg = body.get("message") or body.get("error") or resp.text or "unknown error"
    if resp.status_code == 401:
        raise InvalidDeployTokenError(msg, status=401, provider=provider)
    if resp.status_code == 403:
        raise MissingDeployScopeError(msg, status=403, provider=provider)
    if resp.status_code == 409 or resp.status_code == 422:
        raise DeployConflictError(msg, status=resp.status_code, provider=provider)
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", "60"))
        raise DeployRateLimitError(msg, retry_after=retry, status=429, provider=provider)
    raise DeployError(msg, status=resp.status_code, provider=provider)


class NetlifyAdapter(WebDeployAdapter):
    """Netlify REST API adapter (``provider='netlify'``)."""

    provider = "netlify"

    def _configure(
        self,
        *,
        account_slug: Optional[str] = None,
        api_base: str = NETLIFY_API_BASE,
        **_: Any,
    ) -> None:
        self._account_slug = account_slug
        self._api_base = api_base.rstrip("/")

    # ── HTTP plumbing ──

    def _headers(self, content_type: str = "application/json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": content_type,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict] = None,
        content: Optional[bytes] = None,
        content_type: str = "application/json",
    ) -> dict | list:
        url = f"{self._api_base}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.request(
                method,
                url,
                headers=self._headers(content_type),
                json=json,
                params=params,
                content=content,
            )
        _raise_for_netlify(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {}

    # ── Site lifecycle ──

    async def _find_site(self) -> Optional[dict]:
        data = await self._request(
            "GET", "/sites", params={"name": self._project_name, "filter": "all"},
        )
        if not isinstance(data, list):
            return None
        for site in data:
            if site.get("name") == self._project_name:
                return site
        return None

    async def _create_site(self) -> dict:
        body: dict[str, Any] = {"name": self._project_name}
        path = "/sites"
        if self._account_slug:
            path = f"/{self._account_slug}/sites"
        data = await self._request("POST", path, json=body)
        return data if isinstance(data, dict) else {}

    async def _patch_env(self, site_id: str, env: dict[str, str]) -> None:
        body = {"build_settings": {"env": dict(env)}}
        await self._request("PATCH", f"/sites/{site_id}", json=body)

    async def provision(
        self,
        *,
        env: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> ProvisionResult:
        existing = await self._find_site()
        created = False
        if existing:
            site = existing
        else:
            site = await self._create_site()
            created = True
        site_id = site.get("id") or site.get("site_id") or ""
        self._project_id = site_id
        env_keys: list[str] = []
        if env and site_id:
            await self._patch_env(site_id, env)
            env_keys = list(env.keys())
        url = site.get("ssl_url") or site.get("url") or (
            f"https://{self._project_name}.netlify.app"
        )
        self._cached_url = url
        logger.info(
            "netlify.provision site=%s id=%s created=%s env=%d fp=%s",
            self._project_name, site_id, created, len(env_keys), self.token_fp(),
        )
        return ProvisionResult(
            provider=self.provider,
            project_id=site_id,
            project_name=self._project_name,
            url=url,
            created=created,
            env_vars_set=env_keys,
            raw=site if isinstance(site, dict) else {},
        )

    # ── Digest deploy ──

    def _build_digest(self, root: Path) -> tuple[dict[str, str], dict[str, bytes]]:
        """Return ``(manifest, file_bytes_by_path)``.

        * ``manifest``: ``{"/index.html": "<sha1>", ...}`` — keys use a
          leading slash per Netlify API contract.
        * ``file_bytes_by_path``: lookup for the upload step.
        """
        manifest: dict[str, str] = {}
        bytes_by_path: dict[str, bytes] = {}
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            data = p.read_bytes()
            rel = "/" + p.relative_to(root).as_posix()
            sha1 = hashlib.sha1(data).hexdigest()
            manifest[rel] = sha1
            bytes_by_path[rel] = data
        return manifest, bytes_by_path

    async def deploy(self, build_artifact: BuildArtifact) -> DeployResult:
        build_artifact.validate()
        if not self._project_id:
            existing = await self._find_site()
            if existing:
                self._project_id = existing.get("id") or None
        if not self._project_id:
            raise DeployError(
                "Netlify site_id not resolved — call provision() first.",
                provider=self.provider,
            )

        manifest, bytes_by_path = self._build_digest(build_artifact.path)
        if not manifest:
            raise DeployArtifactError(
                f"No files found under {build_artifact.path}"
            )

        deploy_body: dict[str, Any] = {"files": manifest, "draft": False}
        if build_artifact.commit_sha:
            deploy_body["commit_ref"] = build_artifact.commit_sha
        if build_artifact.branch:
            deploy_body["branch"] = build_artifact.branch

        resp = await self._request(
            "POST", f"/sites/{self._project_id}/deploys", json=deploy_body,
        )
        if not isinstance(resp, dict):
            raise DeployError("Unexpected deploy response shape", provider=self.provider)
        deploy_id = resp.get("id") or ""
        required: list[str] = resp.get("required", []) or []

        for sha1 in required:
            # Find the (first) path that maps to this sha1 and PUT its bytes.
            path = next((k for k, v in manifest.items() if v == sha1), None)
            if path is None:
                continue
            url = f"/deploys/{deploy_id}/files{path}"
            await self._request(
                "PUT", url, content=bytes_by_path[path],
                content_type="application/octet-stream",
            )

        deploy_url = resp.get("deploy_ssl_url") or resp.get("deploy_url") or ""
        prod_url = resp.get("ssl_url") or resp.get("url") or self._cached_url or ""
        final_url = prod_url or deploy_url
        self._cached_url = final_url or self._cached_url
        self._last_deployment_id = deploy_id
        logger.info(
            "netlify.deploy site=%s deploy=%s files=%d uploaded=%d",
            self._project_name, deploy_id, len(manifest), len(required),
        )
        return DeployResult(
            provider=self.provider,
            deployment_id=deploy_id,
            url=final_url,
            status=str(resp.get("state") or "ready").lower(),
            logs_url=resp.get("admin_url"),
            commit_sha=build_artifact.commit_sha,
            raw=resp,
        )

    # ── Rollback ──

    async def rollback(
        self,
        *,
        deployment_id: Optional[str] = None,
    ) -> DeployResult:
        if not self._project_id:
            existing = await self._find_site()
            self._project_id = existing.get("id") if existing else None
        if not self._project_id:
            raise RollbackUnavailableError(
                "No site_id resolved for rollback.", provider=self.provider,
            )
        previous_id = self._last_deployment_id

        if deployment_id:
            # Restore a specific deploy.
            resp = await self._request(
                "POST", f"/deploys/{deployment_id}/restore", json={},
            )
            target_id = deployment_id
        else:
            # Site-level rollback → previous production deploy.
            try:
                resp = await self._request(
                    "POST", f"/sites/{self._project_id}/rollback", json={},
                )
            except DeployError as e:
                if e.status in (404, 422):
                    raise RollbackUnavailableError(
                        "No previous deploy available for rollback.",
                        provider=self.provider,
                    ) from e
                raise
            target_id = ""
            if isinstance(resp, dict):
                target_id = resp.get("id") or resp.get("deploy_id") or ""

        url = ""
        if isinstance(resp, dict):
            url = (
                resp.get("ssl_url") or resp.get("url") or
                resp.get("deploy_ssl_url") or self._cached_url or ""
            )
        self._cached_url = url or self._cached_url
        self._last_deployment_id = target_id or previous_id
        logger.info(
            "netlify.rollback site=%s target=%s previous=%s",
            self._project_name, target_id, previous_id,
        )
        return DeployResult(
            provider=self.provider,
            deployment_id=target_id or "",
            url=url,
            status="rolled-back",
            previous_deployment_id=previous_id,
            raw=resp if isinstance(resp, dict) else {},
        )

    def get_url(self) -> Optional[str]:
        return self._cached_url


__all__ = ["NetlifyAdapter", "NETLIFY_API_BASE"]
