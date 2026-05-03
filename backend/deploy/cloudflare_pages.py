"""W4 #278 — Cloudflare Pages deploy adapter.

Cloudflare Pages exposes a REST API under the main CF v4 surface:

    GET    /accounts/:acct/pages/projects                list projects
    POST   /accounts/:acct/pages/projects                create project
    GET    /accounts/:acct/pages/projects/:name          get project
    POST   /accounts/:acct/pages/projects/:name/deployments   create deploy
    GET    /accounts/:acct/pages/projects/:name/deployments   list deploys
    POST   /accounts/:acct/pages/projects/:name/deployments/:id/retry  rollback

Env vars are set via PATCH on the project (``deployment_configs.production``
/ ``.preview``); this adapter reuses the ``CloudflareClient`` wrapper
from B12 where possible so token-scope errors / 429 handling stay in
one place. Where the Pages endpoints are not wrapped by B12's client,
we talk to the CF v4 surface directly with the same httpx pattern.

File upload shortcut
--------------------
Creating a Pages deployment over the REST API with file uploads uses
the direct-upload JWT flow: first call POST /deployments to get a
``jwt``, then upload manifests to ``https://api.cloudflare.com/client/v4/
accounts/:acct/pages/assets/upload``. For the unit-testable first cut we
expose the manifest-upload method and treat the JWT as opaque. Larger
deployments that ship via git-integration should set
``source.type=github`` at project creation — that path is also
supported via ``provision(..., source={...})``.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from backend.cloudflare_client import (
    CF_API_BASE,
    CloudflareAPIError,
    ConflictError,
    InvalidTokenError,
    MissingScopeError,
    RateLimitError,
)
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


def _translate_cf_error(err: CloudflareAPIError, provider: str) -> DeployError:
    """Map B12 CloudflareClient exceptions into the W4 deploy taxonomy."""
    if isinstance(err, InvalidTokenError):
        return InvalidDeployTokenError(str(err), status=err.status, provider=provider)
    if isinstance(err, MissingScopeError):
        return MissingDeployScopeError(str(err), status=err.status, provider=provider)
    if isinstance(err, ConflictError):
        return DeployConflictError(str(err), status=err.status, provider=provider)
    if isinstance(err, RateLimitError):
        return DeployRateLimitError(
            str(err), retry_after=err.retry_after, status=err.status, provider=provider,
        )
    return DeployError(str(err), status=err.status, provider=provider)


class CloudflarePagesAdapter(WebDeployAdapter):
    """Cloudflare Pages deploy adapter (``provider='cloudflare-pages'``).

    Requires an ``account_id`` — every CF Pages endpoint is scoped to
    an account. The token must have ``Account › Cloudflare Pages ›
    Edit`` scope.
    """

    provider = "cloudflare-pages"

    def _configure(
        self,
        *,
        account_id: str,
        api_base: str = CF_API_BASE,
        production_branch: str = "main",
        **_: Any,
    ) -> None:
        if not account_id:
            raise ValueError("CloudflarePagesAdapter requires account_id")
        self._account_id = account_id
        self._api_base = api_base.rstrip("/")
        self._production_branch = production_branch

    # ── HTTP plumbing ──

    def _headers(self, extra: Optional[dict] = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict] = None,
    ) -> dict:
        url = f"{self._api_base}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.request(method, url, headers=self._headers(), json=json, params=params)
        self._raise_for_cf(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {}

    def _raise_for_cf(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            body = resp.json()
        except Exception:
            body = {}
        errors = body.get("errors") or []
        msg = errors[0].get("message", resp.text) if errors else (resp.text or "cf error")
        if resp.status_code == 401:
            raise InvalidDeployTokenError(msg, status=401, provider=self.provider)
        if resp.status_code == 403:
            raise MissingDeployScopeError(msg, status=403, provider=self.provider)
        if resp.status_code == 409:
            raise DeployConflictError(msg, status=409, provider=self.provider)
        if resp.status_code == 429:
            retry = int(resp.headers.get("Retry-After", "60"))
            raise DeployRateLimitError(msg, retry_after=retry, status=429, provider=self.provider)
        raise DeployError(msg, status=resp.status_code, provider=self.provider)

    # ── Project lifecycle ──

    async def _get_project(self) -> Optional[dict]:
        try:
            data = await self._request(
                "GET",
                f"/accounts/{self._account_id}/pages/projects/{self._project_name}",
            )
        except DeployError as e:
            if e.status == 404:
                return None
            raise
        return data.get("result") or {}

    async def _create_project(self, source: Optional[dict] = None) -> dict:
        body: dict[str, Any] = {
            "name": self._project_name,
            "production_branch": self._production_branch,
        }
        if source:
            body["source"] = source
        data = await self._request(
            "POST", f"/accounts/{self._account_id}/pages/projects", json=body,
        )
        return data.get("result") or {}

    async def _patch_env(self, env: dict[str, str]) -> None:
        env_map = {k: {"value": v, "type": "plain_text"} for k, v in env.items()}
        body = {
            "deployment_configs": {
                "production": {"env_vars": env_map},
                "preview": {"env_vars": env_map},
            }
        }
        await self._request(
            "PATCH",
            f"/accounts/{self._account_id}/pages/projects/{self._project_name}",
            json=body,
        )

    async def provision(
        self,
        *,
        env: Optional[dict[str, str]] = None,
        source: Optional[dict] = None,
        **kwargs: Any,
    ) -> ProvisionResult:
        existing = await self._get_project()
        created = False
        if existing:
            project = existing
        else:
            project = await self._create_project(source=source)
            created = True
        project_id = project.get("id") or project.get("name") or self._project_name
        self._project_id = project_id
        env_keys: list[str] = []
        if env:
            await self._patch_env(env)
            env_keys = list(env.keys())
        subdomain = project.get("subdomain") or f"{self._project_name}.pages.dev"
        url = f"https://{subdomain}"
        self._cached_url = url
        logger.info(
            "cf_pages.provision project=%s created=%s env=%d fp=%s",
            self._project_name, created, len(env_keys), self.token_fp(),
        )
        return ProvisionResult(
            provider=self.provider,
            project_id=project_id,
            project_name=self._project_name,
            url=url,
            created=created,
            env_vars_set=env_keys,
            raw=project,
        )

    # ── Deploy (direct upload) ──

    def _collect_manifest(self, root: Path) -> dict[str, dict]:
        """Return ``{rel_path: {"hash": sha256, "size": len}}`` per CF Pages spec.

        CF Pages uses SHA256 for the direct-upload manifest (distinct
        from Vercel's SHA1).
        """
        out: dict[str, dict] = {}
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            data = p.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            rel = p.relative_to(root).as_posix()
            out[rel] = {"hash": digest, "size": len(data)}
        return out

    async def deploy(self, build_artifact: BuildArtifact) -> DeployResult:
        build_artifact.validate()
        self._enforce_container_vulnerability_gate(build_artifact)
        manifest = self._collect_manifest(build_artifact.path)
        if not manifest:
            raise DeployArtifactError(
                f"No files found under {build_artifact.path}"
            )
        body: dict[str, Any] = {
            "manifest": manifest,
            "branch": self._production_branch,
        }
        if build_artifact.commit_sha:
            body["commit_hash"] = build_artifact.commit_sha
        resp = await self._request(
            "POST",
            f"/accounts/{self._account_id}/pages/projects/{self._project_name}/deployments",
            json=body,
        )
        result = resp.get("result") or {}
        deployment_id = result.get("id") or ""
        url = result.get("url") or self._cached_url or f"https://{self._project_name}.pages.dev"
        self._cached_url = url
        self._last_deployment_id = deployment_id
        logger.info(
            "cf_pages.deploy project=%s deployment=%s files=%d",
            self._project_name, deployment_id, len(manifest),
        )
        return DeployResult(
            provider=self.provider,
            deployment_id=deployment_id,
            url=url,
            status=str(result.get("latest_stage", {}).get("status") or "ready").lower(),
            logs_url=None,
            commit_sha=build_artifact.commit_sha,
            raw=result,
        )

    # ── Rollback ──

    async def _list_deployments(self, limit: int = 10) -> list[dict]:
        data = await self._request(
            "GET",
            f"/accounts/{self._account_id}/pages/projects/{self._project_name}/deployments",
            params={"per_page": str(limit)},
        )
        result = data.get("result")
        return result if isinstance(result, list) else []

    async def rollback(
        self,
        *,
        deployment_id: Optional[str] = None,
    ) -> DeployResult:
        previous_id = self._last_deployment_id
        target_id = deployment_id
        if not target_id:
            deployments = await self._list_deployments(limit=10)
            ready = [d for d in deployments if str(d.get("latest_stage", {}).get("status", "")).lower() == "success"]
            if len(ready) < 2:
                raise RollbackUnavailableError(
                    "No previous successful deployment to roll back to.",
                    provider=self.provider,
                )
            target_id = ready[1].get("id") or ""
        if not target_id:
            raise RollbackUnavailableError(
                "No deployment id resolved for rollback.", provider=self.provider,
            )
        resp = await self._request(
            "POST",
            f"/accounts/{self._account_id}/pages/projects/{self._project_name}/deployments/{target_id}/retry",
            json={},
        )
        result = resp.get("result") or {}
        url = result.get("url") or self._cached_url or ""
        self._cached_url = url or self._cached_url
        self._last_deployment_id = result.get("id") or target_id
        logger.info(
            "cf_pages.rollback project=%s target=%s previous=%s",
            self._project_name, target_id, previous_id,
        )
        return DeployResult(
            provider=self.provider,
            deployment_id=result.get("id") or target_id,
            url=url,
            status="rolled-back",
            previous_deployment_id=previous_id,
            raw=result,
        )

    def get_url(self) -> Optional[str]:
        return self._cached_url


__all__ = ["CloudflarePagesAdapter"]
