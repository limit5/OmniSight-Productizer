"""W4 #278 — Vercel REST API deploy adapter.

Uses Vercel's v9/v10/v13 REST endpoints:

    v9  /v9/projects                      create/get project
    v10 /v10/projects/:id/env             env var CRUD
    v13 /v13/deployments                  create deployment (file upload)
    v13 /v13/deployments/:id              get/promote deployment
    v13 /v13/deployments/:id/promote      promote to production (rollback)

Auth is a single bearer token (``VERCEL_TOKEN``). Optional team scoping
via ``team_id`` (API scope prefix ``?teamId=...``).

The adapter intentionally does NOT shell out to the ``vercel`` CLI — we
keep everything inside the process so the caller controls secrets
through ``backend.secret_store`` without spawning a subprocess that
reads tokens from ~/.local/share/com.vercel.cli/auth.json.

File upload model
-----------------
Vercel requires a two-step upload: each file is POSTed to
``/v2/files`` (returning a SHA1 digest), then a manifest referencing
those digests is POSTed to ``/v13/deployments``. The reference
implementation here uploads sequentially (small static sites); larger
projects should batch-upload concurrently via ``asyncio.gather`` — left
as a future optimisation because sequential uploads keep the error
model simple for the first cut.
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

VERCEL_API_BASE = "https://api.vercel.com"


def _raise_for_vercel(resp: httpx.Response, provider: str = "vercel") -> None:
    """Map Vercel error responses to typed exceptions."""
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except Exception:
        body = {}
    err = body.get("error") or {}
    msg = err.get("message") or resp.text or "unknown error"
    code = err.get("code") or ""
    if resp.status_code == 401:
        raise InvalidDeployTokenError(msg, status=401, provider=provider)
    if resp.status_code == 403:
        raise MissingDeployScopeError(msg, status=403, provider=provider)
    if resp.status_code == 409 or code in ("project_already_exists", "conflict"):
        raise DeployConflictError(msg, status=resp.status_code, provider=provider)
    if resp.status_code == 429:
        retry = int(resp.headers.get("Retry-After", "60"))
        raise DeployRateLimitError(msg, retry_after=retry, status=429, provider=provider)
    raise DeployError(msg, status=resp.status_code, provider=provider)


class VercelAdapter(WebDeployAdapter):
    """Vercel REST API adapter (``provider='vercel'``)."""

    provider = "vercel"

    def _configure(
        self,
        *,
        team_id: Optional[str] = None,
        framework: Optional[str] = None,
        api_base: str = VERCEL_API_BASE,
        **_: Any,
    ) -> None:
        self._team_id = team_id
        self._framework = framework
        self._api_base = api_base.rstrip("/")

    # ── HTTP plumbing ──

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _team_param(self, params: Optional[dict] = None) -> dict:
        params = dict(params or {})
        if self._team_id:
            params["teamId"] = self._team_id
        return params

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict] = None,
        content: Optional[bytes] = None,
        extra_headers: Optional[dict] = None,
    ) -> dict:
        url = f"{self._api_base}{path}"
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.request(
                method,
                url,
                headers=headers,
                json=json,
                params=self._team_param(params),
                content=content,
            )
        _raise_for_vercel(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {}

    # ── Project lifecycle ──

    async def _get_project(self) -> Optional[dict]:
        try:
            data = await self._request("GET", f"/v9/projects/{self._project_name}")
            return data
        except DeployError as e:
            if e.status == 404:
                return None
            raise

    async def _create_project(self) -> dict:
        body: dict[str, Any] = {"name": self._project_name}
        if self._framework:
            body["framework"] = self._framework
        return await self._request("POST", "/v9/projects", json=body)

    async def _upsert_env_var(self, project_id: str, key: str, value: str) -> None:
        body = {
            "key": key,
            "value": value,
            "type": "encrypted",
            "target": ["production", "preview", "development"],
        }
        try:
            await self._request(
                "POST", f"/v10/projects/{project_id}/env", json=body,
                params={"upsert": "true"},
            )
        except DeployConflictError:
            # Older Vercel API without ?upsert=true — fall back to DELETE + POST.
            logger.debug("vercel env %s exists; deleting and re-creating", key)
            await self._request(
                "DELETE", f"/v10/projects/{project_id}/env/{key}",
            )
            await self._request(
                "POST", f"/v10/projects/{project_id}/env", json=body,
            )

    async def provision(
        self,
        *,
        env: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> ProvisionResult:
        existing = await self._get_project()
        created = False
        if existing:
            project = existing
        else:
            project = await self._create_project()
            created = True
        project_id = project.get("id") or ""
        self._project_id = project_id
        env_keys: list[str] = []
        if env:
            for k, v in env.items():
                await self._upsert_env_var(project_id, k, v)
                env_keys.append(k)
        url = None
        if project.get("alias"):
            url = f"https://{project['alias'][0]['domain']}" if project["alias"] else None
        if not url:
            url = f"https://{self._project_name}.vercel.app"
        self._cached_url = url
        logger.info(
            "vercel.provision project=%s id=%s created=%s env=%d fp=%s",
            self._project_name, project_id, created, len(env_keys), self.token_fp(),
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

    # ── File upload + deploy ──

    def _collect_files(self, root: Path) -> list[tuple[Path, str, bytes, str]]:
        """Walk ``root`` and return ``[(abs_path, rel_posix_path, data, sha1_hex), ...]``.

        SHA1 matches Vercel's content-addressed upload API contract.
        """
        out: list[tuple[Path, str, bytes, str]] = []
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            data = p.read_bytes()
            digest = hashlib.sha1(data).hexdigest()
            rel = p.relative_to(root).as_posix()
            out.append((p, rel, data, digest))
        return out

    async def _upload_file(self, data: bytes, sha1: str) -> None:
        headers = {
            "x-vercel-digest": sha1,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
        }
        await self._request("POST", "/v2/files", content=data, extra_headers=headers)

    async def deploy(self, build_artifact: BuildArtifact) -> DeployResult:
        build_artifact.validate()
        self._enforce_container_vulnerability_gate(build_artifact)
        if not self._project_id:
            # Caller skipped provision(); best-effort lookup.
            existing = await self._get_project()
            if existing:
                self._project_id = existing.get("id") or None

        files = self._collect_files(build_artifact.path)
        if not files:
            raise DeployArtifactError(
                f"No files found under {build_artifact.path}"
            )
        # Upload unique digests (dedupe identical bytes).
        seen: set[str] = set()
        for _, _, data, sha1 in files:
            if sha1 in seen:
                continue
            seen.add(sha1)
            await self._upload_file(data, sha1)

        manifest = [
            {"file": rel, "sha": sha1, "size": len(data)}
            for _, rel, data, sha1 in files
        ]
        body: dict[str, Any] = {
            "name": self._project_name,
            "target": "production",
            "files": manifest,
        }
        if self._project_id:
            body["project"] = self._project_id
        if build_artifact.commit_sha:
            body.setdefault("meta", {})["commitSha"] = build_artifact.commit_sha
        if build_artifact.framework or self._framework:
            body["projectSettings"] = {
                "framework": build_artifact.framework or self._framework,
            }

        resp = await self._request("POST", "/v13/deployments", json=body)
        deployment_id = resp.get("id") or resp.get("uid") or ""
        host = resp.get("url") or ""  # Vercel returns "my-app-abc123.vercel.app"
        url = f"https://{host}" if host else (self._cached_url or "")
        self._cached_url = url
        self._last_deployment_id = deployment_id
        logger.info(
            "vercel.deploy project=%s deployment=%s files=%d status=%s",
            self._project_name, deployment_id, len(files), resp.get("readyState", "QUEUED"),
        )
        return DeployResult(
            provider=self.provider,
            deployment_id=deployment_id,
            url=url,
            status=str(resp.get("readyState", "ready")).lower(),
            logs_url=f"https://vercel.com/_logs/{deployment_id}" if deployment_id else None,
            commit_sha=build_artifact.commit_sha,
            raw=resp,
        )

    # ── Rollback ──

    async def _list_deployments(self, limit: int = 10) -> list[dict]:
        params = {"projectId": self._project_id or "", "limit": str(limit)}
        data = await self._request("GET", "/v6/deployments", params=params)
        return data.get("deployments", [])

    async def rollback(
        self,
        *,
        deployment_id: Optional[str] = None,
    ) -> DeployResult:
        if not self._project_id:
            # Caller skipped provision()
            existing = await self._get_project()
            self._project_id = existing.get("id") if existing else None

        target_id = deployment_id
        previous_id = self._last_deployment_id
        if not target_id:
            deployments = await self._list_deployments(limit=10)
            # Skip the most recent (current) deployment; find next READY one.
            ready = [d for d in deployments if str(d.get("state", "")).upper() == "READY"]
            if len(ready) < 2:
                raise RollbackUnavailableError(
                    "No previous deployment available to roll back to.",
                    provider=self.provider,
                )
            target_id = ready[1].get("uid") or ready[1].get("id") or ""

        if not target_id:
            raise RollbackUnavailableError(
                "No deployment id resolved for rollback.", provider=self.provider,
            )
        resp = await self._request(
            "POST", f"/v13/deployments/{target_id}/promote",
            json={},
        )
        host = resp.get("url") or self._cached_url or ""
        url = f"https://{host}" if host and not host.startswith("http") else host or ""
        self._cached_url = url
        self._last_deployment_id = target_id
        logger.info(
            "vercel.rollback project=%s new_production=%s previous=%s",
            self._project_name, target_id, previous_id,
        )
        return DeployResult(
            provider=self.provider,
            deployment_id=target_id,
            url=url,
            status="rolled-back",
            previous_deployment_id=previous_id,
            raw=resp,
        )

    def get_url(self) -> Optional[str]:
        return self._cached_url


__all__ = ["VercelAdapter", "VERCEL_API_BASE"]
