"""W4 #278 — Netlify adapter tests (respx-mocked)."""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from backend.deploy import BuildArtifact
from backend.deploy.base import (
    DeployConflictError,
    DeployError,
    InvalidDeployTokenError,
    MissingDeployScopeError,
    DeployRateLimitError,
    RollbackUnavailableError,
)
from backend.deploy.netlify import NETLIFY_API_BASE, NetlifyAdapter

N = NETLIFY_API_BASE


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _err(status, msg="err"):
    return httpx.Response(status, json={"message": msg})


@pytest.fixture
def build_site(tmp_path):
    (tmp_path / "index.html").write_text("<html/>")
    (tmp_path / "app.css").write_text("body{}")
    return tmp_path


def _mk_adapter(**kw):
    return NetlifyAdapter(token="nfp_ABCDEF0123456789", project_name="demo-site", **kw)


class TestProvision:

    @respx.mock
    async def test_creates_site_when_absent_and_patches_env(self):
        respx.get(f"{N}/sites").mock(return_value=_ok([]))
        respx.post(f"{N}/sites").mock(
            return_value=_ok({
                "id": "site_123",
                "name": "demo-site",
                "ssl_url": "https://demo-site.netlify.app",
            }),
        )
        patch = respx.patch(f"{N}/sites/site_123").mock(return_value=_ok({}))
        adapter = _mk_adapter()
        r = await adapter.provision(env={"API_URL": "https://api.example.com"})
        assert r.created is True
        assert r.project_id == "site_123"
        assert r.env_vars_set == ["API_URL"]
        assert r.url == "https://demo-site.netlify.app"
        assert patch.called
        # env goes under build_settings.env
        body = patch.calls.last.request.read()
        assert b'"build_settings"' in body
        assert b'"API_URL"' in body

    @respx.mock
    async def test_reuses_existing_site_by_name(self):
        respx.get(f"{N}/sites").mock(
            return_value=_ok([
                {"id": "site_other", "name": "another"},
                {"id": "site_main", "name": "demo-site", "ssl_url": "https://demo-site.netlify.app"},
            ]),
        )
        r = await _mk_adapter().provision()
        assert r.created is False
        assert r.project_id == "site_main"

    @respx.mock
    async def test_create_site_uses_account_scope_when_provided(self):
        respx.get(f"{N}/sites").mock(return_value=_ok([]))
        route = respx.post(f"{N}/teamX/sites").mock(
            return_value=_ok({"id": "s", "name": "demo-site"}),
        )
        await _mk_adapter(account_slug="teamX").provision()
        assert route.called

    @respx.mock
    async def test_401_and_403_map_correctly(self):
        respx.get(f"{N}/sites").mock(return_value=_err(401, "bad"))
        with pytest.raises(InvalidDeployTokenError):
            await _mk_adapter().provision()
        respx.get(f"{N}/sites").mock(return_value=_err(403, "scope"))
        with pytest.raises(MissingDeployScopeError):
            await _mk_adapter().provision()

    @respx.mock
    async def test_422_maps_to_conflict(self):
        respx.get(f"{N}/sites").mock(return_value=_ok([]))
        respx.post(f"{N}/sites").mock(return_value=_err(422, "taken"))
        with pytest.raises(DeployConflictError):
            await _mk_adapter().provision()

    @respx.mock
    async def test_429_is_rate_limit(self):
        respx.get(f"{N}/sites").mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "7"}, json={"message": "slow down"},
            ),
        )
        with pytest.raises(DeployRateLimitError) as ei:
            await _mk_adapter().provision()
        assert ei.value.retry_after == 7


class TestDeploy:

    @respx.mock
    async def test_digest_deploy_uploads_required_files(self, build_site):
        respx.get(f"{N}/sites").mock(
            return_value=_ok([{"id": "site_x", "name": "demo-site"}]),
        )
        # Deploy manifest → server demands only one upload (index.html's sha)
        import hashlib
        index_sha = hashlib.sha1(b"<html/>").hexdigest()
        respx.post(f"{N}/sites/site_x/deploys").mock(
            return_value=_ok({
                "id": "dep_1",
                "required": [index_sha],
                "ssl_url": "https://demo-site.netlify.app",
                "admin_url": "https://app.netlify.com/sites/demo-site",
                "state": "ready",
            }),
        )
        put_route = respx.put(re.compile(rf"{re.escape(N)}/deploys/dep_1/files/.*")).mock(
            return_value=_ok({}),
        )
        adapter = _mk_adapter()
        await adapter.provision()
        r = await adapter.deploy(BuildArtifact(path=build_site, commit_sha="deadbeef", branch="main"))
        assert put_route.called
        assert put_route.call_count == 1  # only the required sha gets uploaded
        assert r.deployment_id == "dep_1"
        assert r.url == "https://demo-site.netlify.app"
        assert r.status == "ready"

    @respx.mock
    async def test_deploy_without_required_uploads_nothing(self, build_site):
        respx.get(f"{N}/sites").mock(
            return_value=_ok([{"id": "site_x", "name": "demo-site"}]),
        )
        respx.post(f"{N}/sites/site_x/deploys").mock(
            return_value=_ok({
                "id": "dep_empty",
                "required": [],
                "ssl_url": "https://demo-site.netlify.app",
                "state": "ready",
            }),
        )
        put_route = respx.put(re.compile(rf"{re.escape(N)}/deploys/dep_empty/files/.*"))
        adapter = _mk_adapter()
        await adapter.provision()
        await adapter.deploy(BuildArtifact(path=build_site))
        assert put_route.call_count == 0

    @respx.mock
    async def test_deploy_before_provision_lazy_lookup(self, build_site):
        respx.get(f"{N}/sites").mock(
            return_value=_ok([{"id": "site_lazy", "name": "demo-site"}]),
        )
        respx.post(f"{N}/sites/site_lazy/deploys").mock(
            return_value=_ok({"id": "d", "required": [], "ssl_url": "https://x.netlify.app"}),
        )
        r = await _mk_adapter().deploy(BuildArtifact(path=build_site))
        assert r.deployment_id == "d"

    @respx.mock
    async def test_deploy_empty_artifact_raises(self, tmp_path):
        respx.get(f"{N}/sites").mock(
            return_value=_ok([{"id": "site_x", "name": "demo-site"}]),
        )
        from backend.deploy.base import DeployArtifactError
        adapter = _mk_adapter()
        await adapter.provision()
        with pytest.raises(DeployArtifactError):
            await adapter.deploy(BuildArtifact(path=tmp_path))


class TestRollback:

    @respx.mock
    async def test_rollback_without_id_calls_site_rollback(self):
        respx.get(f"{N}/sites").mock(
            return_value=_ok([{"id": "site_x", "name": "demo-site"}]),
        )
        route = respx.post(f"{N}/sites/site_x/rollback").mock(
            return_value=_ok({
                "id": "dep_prev", "ssl_url": "https://demo-site.netlify.app",
            }),
        )
        adapter = _mk_adapter()
        await adapter.provision()
        r = await adapter.rollback()
        assert route.called
        assert r.deployment_id == "dep_prev"
        assert r.status == "rolled-back"

    @respx.mock
    async def test_rollback_by_id_calls_restore(self):
        respx.get(f"{N}/sites").mock(
            return_value=_ok([{"id": "site_x", "name": "demo-site"}]),
        )
        route = respx.post(f"{N}/deploys/dep_explicit/restore").mock(
            return_value=_ok({"id": "dep_explicit", "ssl_url": "https://demo.netlify.app"}),
        )
        adapter = _mk_adapter()
        await adapter.provision()
        r = await adapter.rollback(deployment_id="dep_explicit")
        assert route.called
        assert r.deployment_id == "dep_explicit"

    @respx.mock
    async def test_rollback_maps_404_to_unavailable(self):
        respx.get(f"{N}/sites").mock(
            return_value=_ok([{"id": "site_x", "name": "demo-site"}]),
        )
        respx.post(f"{N}/sites/site_x/rollback").mock(return_value=_err(404, "no prior"))
        adapter = _mk_adapter()
        await adapter.provision()
        with pytest.raises(RollbackUnavailableError):
            await adapter.rollback()


class TestGetUrl:

    @respx.mock
    async def test_url_cached_after_provision(self):
        respx.get(f"{N}/sites").mock(
            return_value=_ok([{"id": "s", "name": "demo-site", "ssl_url": "https://demo-site.netlify.app"}]),
        )
        adapter = _mk_adapter()
        await adapter.provision()
        assert adapter.get_url() == "https://demo-site.netlify.app"
