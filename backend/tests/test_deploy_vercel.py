"""W4 #278 — Vercel adapter tests (respx-mocked)."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
import respx

from backend.deploy import BuildArtifact
from backend.deploy.base import (
    DeployConflictError,
    InvalidDeployTokenError,
    MissingDeployScopeError,
    DeployRateLimitError,
    RollbackUnavailableError,
)
from backend.deploy.vercel import VERCEL_API_BASE, VercelAdapter

V = VERCEL_API_BASE


def _ok(result=None, status=200):
    return httpx.Response(status, json=result if result is not None else {})


def _err(status, code="", msg="err"):
    body = {"error": {"code": code, "message": msg}}
    return httpx.Response(status, json=body)


@pytest.fixture
def build_site(tmp_path):
    (tmp_path / "index.html").write_text("<html><body><h1>hi</h1></body></html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('ok');\n")
    return tmp_path


def _mk_adapter(**kw):
    return VercelAdapter(token="vrc_test_token_ABCD1234", project_name="demo-app", **kw)


class TestProvision:

    @respx.mock
    async def test_creates_project_when_absent_and_sets_env(self):
        respx.get(f"{V}/v9/projects/demo-app").mock(
            return_value=_err(404, code="not_found", msg="Not Found"),
        )
        respx.post(f"{V}/v9/projects").mock(
            return_value=_ok({"id": "prj_123", "name": "demo-app"}),
        )
        env_post = respx.post(re.compile(rf"{re.escape(V)}/v10/projects/prj_123/env.*")).mock(
            return_value=_ok({"created": True}),
        )
        adapter = _mk_adapter()
        result = await adapter.provision(env={"API_URL": "https://api.example.com"})
        assert result.created is True
        assert result.project_id == "prj_123"
        assert result.env_vars_set == ["API_URL"]
        assert result.url == "https://demo-app.vercel.app"
        assert env_post.called

    @respx.mock
    async def test_reuses_existing_project_is_idempotent(self):
        respx.get(f"{V}/v9/projects/demo-app").mock(
            return_value=_ok({"id": "prj_existing", "name": "demo-app"}),
        )
        adapter = _mk_adapter()
        r = await adapter.provision()
        assert r.created is False
        assert r.project_id == "prj_existing"

    @respx.mock
    async def test_env_upsert_handles_conflict_by_delete_then_recreate(self):
        respx.get(f"{V}/v9/projects/demo-app").mock(return_value=_ok({"id": "prj_conflict"}))
        # First post returns 409 "exists", then delete 200, then create 200.
        post_route = respx.post(re.compile(rf"{re.escape(V)}/v10/projects/prj_conflict/env.*")).mock(
            side_effect=[
                _err(409, code="conflict", msg="exists"),
                _ok({"created": True}),
            ],
        )
        del_route = respx.delete(f"{V}/v10/projects/prj_conflict/env/API_URL").mock(
            return_value=_ok({"deleted": True}),
        )
        adapter = _mk_adapter()
        r = await adapter.provision(env={"API_URL": "https://api.example.com"})
        assert r.env_vars_set == ["API_URL"]
        assert del_route.called
        assert post_route.call_count == 2

    @respx.mock
    async def test_401_maps_to_invalid_token(self):
        respx.get(f"{V}/v9/projects/demo-app").mock(
            return_value=_err(401, msg="Invalid token"),
        )
        adapter = _mk_adapter()
        with pytest.raises(InvalidDeployTokenError):
            await adapter.provision()

    @respx.mock
    async def test_403_maps_to_missing_scope(self):
        respx.get(f"{V}/v9/projects/demo-app").mock(
            return_value=_err(403, msg="Forbidden"),
        )
        with pytest.raises(MissingDeployScopeError):
            await _mk_adapter().provision()

    @respx.mock
    async def test_429_maps_to_rate_limit(self):
        respx.get(f"{V}/v9/projects/demo-app").mock(
            return_value=httpx.Response(
                429,
                headers={"Retry-After": "42"},
                json={"error": {"message": "rate limited"}},
            ),
        )
        with pytest.raises(DeployRateLimitError) as ei:
            await _mk_adapter().provision()
        assert ei.value.retry_after == 42

    @respx.mock
    async def test_team_id_appended_as_query_param(self):
        route = respx.get(f"{V}/v9/projects/demo-app").mock(
            return_value=_ok({"id": "prj_team"}),
        )
        adapter = _mk_adapter(team_id="team_abc")
        await adapter.provision()
        assert route.called
        req = route.calls.last.request
        assert "teamId=team_abc" in str(req.url)


class TestDeploy:

    @respx.mock
    async def test_uploads_files_then_creates_deployment(self, build_site):
        respx.get(f"{V}/v9/projects/demo-app").mock(return_value=_ok({"id": "prj_xyz"}))
        # Any file upload endpoint → accept
        upload_route = respx.post(f"{V}/v2/files").mock(return_value=_ok({}))
        deploy_route = respx.post(f"{V}/v13/deployments").mock(
            return_value=_ok({
                "id": "dpl_abc",
                "url": "demo-app-abc.vercel.app",
                "readyState": "READY",
            }),
        )
        adapter = _mk_adapter()
        await adapter.provision()
        artifact = BuildArtifact(path=build_site, commit_sha="cafebabe", framework="nextjs")
        result = await adapter.deploy(artifact)
        assert upload_route.called
        assert deploy_route.called
        assert result.deployment_id == "dpl_abc"
        assert result.url == "https://demo-app-abc.vercel.app"
        assert result.status == "ready"
        assert adapter.get_url() == "https://demo-app-abc.vercel.app"
        # Verify deployment body includes manifest
        body = deploy_route.calls.last.request.read()
        assert b'"files"' in body
        assert b'"sha"' in body
        assert b'"cafebabe"' in body

    @respx.mock
    async def test_deploy_raises_on_empty_artifact(self, tmp_path):
        respx.get(f"{V}/v9/projects/demo-app").mock(return_value=_ok({"id": "prj_empty"}))
        adapter = _mk_adapter()
        await adapter.provision()
        from backend.deploy.base import DeployArtifactError
        with pytest.raises(DeployArtifactError):
            await adapter.deploy(BuildArtifact(path=tmp_path))

    @respx.mock
    async def test_deploys_without_prior_provision_lookup(self, build_site):
        respx.get(f"{V}/v9/projects/demo-app").mock(return_value=_ok({"id": "prj_lookup"}))
        respx.post(f"{V}/v2/files").mock(return_value=_ok({}))
        route = respx.post(f"{V}/v13/deployments").mock(
            return_value=_ok({"id": "dpl_x", "url": "x.vercel.app", "readyState": "READY"}),
        )
        adapter = _mk_adapter()
        # deploy() must resolve the project lazily.
        await adapter.deploy(BuildArtifact(path=build_site))
        assert route.called

    @respx.mock
    async def test_deploy_dedupes_upload_of_identical_files(self, tmp_path):
        # Same bytes → single upload.
        (tmp_path / "a.txt").write_text("same")
        (tmp_path / "b.txt").write_text("same")
        respx.get(f"{V}/v9/projects/demo-app").mock(return_value=_ok({"id": "p"}))
        upload = respx.post(f"{V}/v2/files").mock(return_value=_ok({}))
        respx.post(f"{V}/v13/deployments").mock(
            return_value=_ok({"id": "d", "url": "demo.vercel.app", "readyState": "READY"}),
        )
        adapter = _mk_adapter()
        await adapter.provision()
        await adapter.deploy(BuildArtifact(path=tmp_path))
        assert upload.call_count == 1  # only one unique digest


class TestRollback:

    @respx.mock
    async def test_rollback_to_previous_ready_deployment(self, build_site):
        respx.get(f"{V}/v9/projects/demo-app").mock(return_value=_ok({"id": "prj_r"}))
        respx.post(f"{V}/v2/files").mock(return_value=_ok({}))
        respx.post(f"{V}/v13/deployments").mock(
            return_value=_ok({"id": "dpl_curr", "url": "curr.vercel.app", "readyState": "READY"}),
        )
        respx.get(f"{V}/v6/deployments").mock(
            return_value=_ok({
                "deployments": [
                    {"uid": "dpl_curr", "state": "READY"},
                    {"uid": "dpl_prev", "state": "READY"},
                ],
            }),
        )
        promote = respx.post(re.compile(rf"{re.escape(V)}/v13/deployments/dpl_prev/promote")).mock(
            return_value=_ok({"url": "prev.vercel.app"}),
        )
        adapter = _mk_adapter()
        await adapter.provision()
        await adapter.deploy(BuildArtifact(path=build_site))
        result = await adapter.rollback()
        assert promote.called
        assert result.deployment_id == "dpl_prev"
        assert result.status == "rolled-back"
        assert result.previous_deployment_id == "dpl_curr"

    @respx.mock
    async def test_rollback_raises_when_no_history(self):
        respx.get(f"{V}/v9/projects/demo-app").mock(return_value=_ok({"id": "prj"}))
        respx.get(f"{V}/v6/deployments").mock(return_value=_ok({"deployments": []}))
        adapter = _mk_adapter()
        await adapter.provision()
        with pytest.raises(RollbackUnavailableError):
            await adapter.rollback()

    @respx.mock
    async def test_rollback_to_explicit_deployment_id(self):
        respx.get(f"{V}/v9/projects/demo-app").mock(return_value=_ok({"id": "prj"}))
        promote = respx.post(re.compile(rf"{re.escape(V)}/v13/deployments/dpl_manual/promote")).mock(
            return_value=_ok({"url": "manual.vercel.app"}),
        )
        adapter = _mk_adapter()
        await adapter.provision()
        r = await adapter.rollback(deployment_id="dpl_manual")
        assert promote.called
        assert r.deployment_id == "dpl_manual"


class TestGetUrl:

    def test_get_url_before_provision_is_none(self):
        assert _mk_adapter().get_url() is None

    @respx.mock
    async def test_get_url_cached_after_provision(self):
        respx.get(f"{V}/v9/projects/demo-app").mock(return_value=_ok({"id": "prj"}))
        adapter = _mk_adapter()
        await adapter.provision()
        assert adapter.get_url() == "https://demo-app.vercel.app"
