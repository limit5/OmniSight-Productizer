"""W4 #278 — Cloudflare Pages adapter tests (respx-mocked)."""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from backend.cloudflare_client import CF_API_BASE
from backend.deploy import BuildArtifact
from backend.deploy.base import (
    DeployConflictError,
    InvalidDeployTokenError,
    MissingDeployScopeError,
    DeployRateLimitError,
    RollbackUnavailableError,
)
from backend.deploy.cloudflare_pages import CloudflarePagesAdapter

CF = CF_API_BASE
ACCT = "acc_ABCD"


def _cf_ok(result=None, status=200):
    return httpx.Response(
        status,
        json={"success": True, "errors": [], "result": result if result is not None else {}},
    )


def _cf_err(status, msg="err"):
    return httpx.Response(status, json={"success": False, "errors": [{"code": 0, "message": msg}]})


@pytest.fixture
def build_site(tmp_path):
    (tmp_path / "index.html").write_text("<html/>")
    (tmp_path / "_headers").write_text("/*\n  X-Robots-Tag: noindex\n")
    return tmp_path


def _mk(**kw):
    return CloudflarePagesAdapter(
        token="cf_pages_token_0123456789ABCD",
        project_name="demo-app",
        account_id=ACCT,
        **kw,
    )


class TestProvision:

    def test_rejects_missing_account_id(self):
        with pytest.raises(ValueError):
            CloudflarePagesAdapter(
                token="t", project_name="p", account_id="",
            )

    @respx.mock
    async def test_creates_project_when_absent_and_patches_env(self):
        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=_cf_err(404, "not found"),
        )
        respx.post(f"{CF}/accounts/{ACCT}/pages/projects").mock(
            return_value=_cf_ok({"id": "cfp_1", "name": "demo-app", "subdomain": "demo-app.pages.dev"}),
        )
        patch = respx.patch(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=_cf_ok({}),
        )
        r = await _mk().provision(env={"API_URL": "https://api.example.com"})
        assert r.created is True
        assert r.project_id == "cfp_1"
        assert r.env_vars_set == ["API_URL"]
        assert r.url == "https://demo-app.pages.dev"
        assert patch.called
        body = patch.calls.last.request.read()
        assert b'"deployment_configs"' in body
        assert b'"API_URL"' in body

    @respx.mock
    async def test_reuses_existing_project(self):
        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=_cf_ok({"id": "existing", "name": "demo-app", "subdomain": "demo-app.pages.dev"}),
        )
        r = await _mk().provision()
        assert r.created is False
        assert r.project_id == "existing"

    @respx.mock
    async def test_401_403_429_error_mapping(self):
        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=_cf_err(401, "bad token"),
        )
        with pytest.raises(InvalidDeployTokenError):
            await _mk().provision()

        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=_cf_err(403, "scope"),
        )
        with pytest.raises(MissingDeployScopeError):
            await _mk().provision()

        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=httpx.Response(
                429,
                headers={"Retry-After": "15"},
                json={"success": False, "errors": [{"message": "rate"}]},
            ),
        )
        with pytest.raises(DeployRateLimitError) as ei:
            await _mk().provision()
        assert ei.value.retry_after == 15

    @respx.mock
    async def test_create_project_with_git_source(self):
        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=_cf_err(404, "not found"),
        )
        route = respx.post(f"{CF}/accounts/{ACCT}/pages/projects").mock(
            return_value=_cf_ok({"id": "cfp_git", "name": "demo-app"}),
        )
        source = {"type": "github", "config": {"owner": "me", "repo_name": "site"}}
        await _mk().provision(source=source)
        body = route.calls.last.request.read()
        assert b'"github"' in body


class TestDeploy:

    @respx.mock
    async def test_creates_deployment_with_sha256_manifest(self, build_site):
        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=_cf_ok({"id": "cfp_1", "name": "demo-app", "subdomain": "demo-app.pages.dev"}),
        )
        route = respx.post(f"{CF}/accounts/{ACCT}/pages/projects/demo-app/deployments").mock(
            return_value=_cf_ok({
                "id": "dep_1",
                "url": "https://abc123.demo-app.pages.dev",
                "latest_stage": {"status": "success"},
            }),
        )
        adapter = _mk()
        await adapter.provision()
        r = await adapter.deploy(BuildArtifact(path=build_site, commit_sha="cafebabe"))
        assert route.called
        body = route.calls.last.request.read()
        assert b'"manifest"' in body
        assert b'"commit_hash"' in body
        # SHA256 hex is 64 chars, SHA1 is 40. Deployment body should contain SHA256.
        sha256_hex = re.compile(rb'"hash"\s*:\s*"[0-9a-f]{64}"')
        assert sha256_hex.search(body), "manifest hash must be SHA256"
        assert r.deployment_id == "dep_1"
        assert r.url == "https://abc123.demo-app.pages.dev"

    @respx.mock
    async def test_deploy_empty_artifact_raises(self, tmp_path):
        from backend.deploy.base import DeployArtifactError
        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=_cf_ok({"id": "cfp_1", "name": "demo-app"}),
        )
        adapter = _mk()
        await adapter.provision()
        with pytest.raises(DeployArtifactError):
            await adapter.deploy(BuildArtifact(path=tmp_path))


class TestRollback:

    @respx.mock
    async def test_rollback_to_previous_success_deployment(self, build_site):
        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=_cf_ok({"id": "cfp_1", "name": "demo-app", "subdomain": "demo-app.pages.dev"}),
        )
        respx.post(f"{CF}/accounts/{ACCT}/pages/projects/demo-app/deployments").mock(
            return_value=_cf_ok({"id": "dep_curr", "latest_stage": {"status": "success"}, "url": "https://curr.pages.dev"}),
        )
        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app/deployments").mock(
            return_value=_cf_ok([
                {"id": "dep_curr", "latest_stage": {"status": "success"}},
                {"id": "dep_prev", "latest_stage": {"status": "success"}},
            ]),
        )
        retry = respx.post(f"{CF}/accounts/{ACCT}/pages/projects/demo-app/deployments/dep_prev/retry").mock(
            return_value=_cf_ok({"id": "dep_new_from_prev", "url": "https://prev.pages.dev"}),
        )
        adapter = _mk()
        await adapter.provision()
        await adapter.deploy(BuildArtifact(path=build_site))
        r = await adapter.rollback()
        assert retry.called
        assert r.previous_deployment_id == "dep_curr"
        assert r.status == "rolled-back"

    @respx.mock
    async def test_rollback_unavailable_when_no_history(self):
        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=_cf_ok({"id": "cfp_1", "name": "demo-app"}),
        )
        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app/deployments").mock(
            return_value=_cf_ok([]),
        )
        adapter = _mk()
        await adapter.provision()
        with pytest.raises(RollbackUnavailableError):
            await adapter.rollback()

    @respx.mock
    async def test_rollback_by_explicit_id(self):
        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=_cf_ok({"id": "cfp_1", "name": "demo-app"}),
        )
        route = respx.post(f"{CF}/accounts/{ACCT}/pages/projects/demo-app/deployments/dep_manual/retry").mock(
            return_value=_cf_ok({"id": "dep_new", "url": "https://m.pages.dev"}),
        )
        adapter = _mk()
        await adapter.provision()
        r = await adapter.rollback(deployment_id="dep_manual")
        assert route.called
        assert r.previous_deployment_id is None


class TestGetUrl:

    @respx.mock
    async def test_cached_url_after_provision(self):
        respx.get(f"{CF}/accounts/{ACCT}/pages/projects/demo-app").mock(
            return_value=_cf_ok({"id": "p", "name": "demo-app", "subdomain": "demo-app.pages.dev"}),
        )
        adapter = _mk()
        await adapter.provision()
        assert adapter.get_url() == "https://demo-app.pages.dev"
