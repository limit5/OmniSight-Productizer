"""W14.2 — `backend/routers/web_sandbox.py` endpoint contract tests.

Pins the REST surface for the live web-preview launcher:

* Auth: ``POST`` / ``DELETE`` / ``touch`` / ``ready`` require operator,
  ``GET`` requires viewer.
* ``POST /web-sandbox/preview`` resolves ``workspace_id`` → path via
  :func:`backend.workspace.get_workspace`, falls back to
  ``workspace_path`` from the request body, returns 404 when neither
  is available.
* Idempotent: re-POSTing for the same workspace_id while running
  returns the existing instance.
* Lifecycle: ``POST`` (launch) → ``POST /touch`` (bump) →
  ``POST /ready`` (transition installing → running) →
  ``DELETE`` (stop+remove with ``reason``).
* Validation: bad workspace_id / bad container_port / non-absolute
  workspace_path return 400.

Tests use a stubbed :class:`backend.web_sandbox.WebSandboxManager`
backed by the in-memory FakeDockerClient from
``test_web_sandbox.py`` — no real docker daemon involvement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth as _au
from backend import web_sandbox as ws
from backend import web_sandbox_pep as _wsp
from backend import workspace as _ws
from backend.routers import web_sandbox as web_sandbox_router
from backend.tests.test_web_sandbox import (
    FakeClock,
    FakeDockerClient,
    RecordingEventCallback,
)
from backend.web_sandbox import (
    WebSandboxConfig,
    WebSandboxManager,
    WebSandboxStatus,
)


# ── Test app + auth bypass ─────────────────────────────────────────


def _operator() -> _au.User:
    return _au.User(
        id="u-operator",
        email="op@example.com",
        name="Op",
        role="operator",
    )


def _viewer() -> _au.User:
    return _au.User(
        id="u-viewer",
        email="viewer@example.com",
        name="V",
        role="viewer",
    )


@pytest.fixture
def manager(tmp_path: Path) -> WebSandboxManager:
    """Fresh manager backed by an in-memory FakeDockerClient — every
    test gets a clean slate so workspace_id reuse doesn't leak."""

    return WebSandboxManager(
        docker_client=FakeDockerClient(),
        manifest=None,
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
    )


@pytest.fixture
def client(manager: WebSandboxManager, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """FastAPI TestClient with auth dependencies stubbed out and the
    web_sandbox manager replaced with the fixture's in-memory one."""

    app = FastAPI()
    app.include_router(web_sandbox_router.router)
    # Auth bypass — every request runs as operator unless overridden.
    app.dependency_overrides[_au.require_operator] = _operator
    app.dependency_overrides[_au.require_viewer] = _viewer
    # Manager injection — share one in-memory manager across the
    # whole TestClient lifetime so POST-then-GET sees the same state.
    app.dependency_overrides[web_sandbox_router.get_manager] = lambda: manager
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_workspace_registry() -> Any:
    """Y6 #282 workspace registry is a module-global dict — reset it
    between tests so a fixture-registered workspace doesn't leak into
    the next test."""

    saved = dict(_ws._workspaces)
    _ws._workspaces.clear()
    yield
    _ws._workspaces.clear()
    _ws._workspaces.update(saved)


@pytest.fixture(autouse=True)
def stub_pep_evaluator() -> Any:
    """W14.8 — every test runs with the PEP HOLD auto-approved by
    default so the lifecycle / validation / SSO tests don't have to
    plumb a propose_fn into every call site. Tests that explicitly
    want to assert PEP behaviour install a richer recorder via
    :func:`web_sandbox_router.set_pep_evaluator_for_tests`.

    The fixture also resets the evaluator after each test so a custom
    recorder doesn't leak into the next test's run.
    """

    async def _auto_approve(**_kwargs: Any) -> _wsp.WebPreviewPepResult:
        return _wsp.WebPreviewPepResult(
            action="approved",
            reason="auto-approved by stub_pep_evaluator fixture",
            decision_id="stub-dec",
            rule="tier_unlisted",
        )

    web_sandbox_router.set_pep_evaluator_for_tests(_auto_approve)
    yield
    web_sandbox_router.set_pep_evaluator_for_tests(None)


# ── Helpers ────────────────────────────────────────────────────────


def _register_workspace(workspace_id: str, path: Path) -> None:
    """Stub a workspace registration so the launcher can resolve
    ``workspace_id`` → ``path`` without a real Y6 provision."""

    _ws._workspaces[workspace_id] = _ws.WorkspaceInfo(
        agent_id=workspace_id,
        task_id="t-1",
        branch="main",
        path=path,
        repo_source=str(path),
    )


# ── POST /web-sandbox/preview — happy path ─────────────────────────


def test_post_preview_with_explicit_path(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-42",
            "workspace_path": str(tmp_path),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workspace_id"] == "ws-42"
    assert body["status"] == WebSandboxStatus.installing.value
    assert body["sandbox_id"].startswith("ws-")
    assert body["container_name"].startswith("omnisight-web-preview-")
    assert body["host_port"] is not None
    assert body["preview_url"].startswith("http://")


def test_post_preview_resolves_via_workspace_registry(
    client: TestClient, tmp_path: Path
) -> None:
    _register_workspace("ws-42", tmp_path)
    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-42"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == WebSandboxStatus.installing.value


def test_post_preview_unknown_workspace_id_returns_404(client: TestClient) -> None:
    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-unknown"},
    )
    assert resp.status_code == 404
    assert "not registered" in resp.json()["detail"]


def test_post_preview_idempotent_returns_existing(
    client: TestClient, tmp_path: Path
) -> None:
    body = {"workspace_id": "ws-42", "workspace_path": str(tmp_path)}
    a = client.post("/web-sandbox/preview", json=body)
    b = client.post("/web-sandbox/preview", json=body)
    assert a.status_code == 200
    assert b.status_code == 200
    assert a.json()["sandbox_id"] == b.json()["sandbox_id"]


def test_post_preview_with_git_ref(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-42",
            "workspace_path": str(tmp_path),
            "git_ref": "feature/foo",
        },
    )
    assert resp.status_code == 200, resp.text
    cmd = resp.json()["config"]["dev_command"]
    assert cmd == ["pnpm", "dev", "--host", "0.0.0.0"]


def test_post_preview_with_custom_dev_command(
    client: TestClient, tmp_path: Path
) -> None:
    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-42",
            "workspace_path": str(tmp_path),
            "dev_command": ["bun", "--bun", "nuxt", "dev"],
            "container_port": 3000,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["config"]["dev_command"] == ["bun", "--bun", "nuxt", "dev"]
    assert body["config"]["container_port"] == 3000


def test_post_preview_with_env(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-42",
            "workspace_path": str(tmp_path),
            "env": {"NUXT_PUBLIC_API_URL": "https://api.example.com"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert (
        resp.json()["config"]["env"]["NUXT_PUBLIC_API_URL"]
        == "https://api.example.com"
    )


# ── POST /web-sandbox/preview — validation ────────────────────────


def test_post_preview_relative_path_returns_400(
    client: TestClient,
) -> None:
    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-42", "workspace_path": "relative/path"},
    )
    assert resp.status_code == 400


def test_post_preview_missing_path_in_request_returns_404(
    client: TestClient,
) -> None:
    # No registry entry, no body workspace_path → 404.
    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-orphan"},
    )
    assert resp.status_code == 404


def test_post_preview_bad_workspace_id_returns_422(
    client: TestClient, tmp_path: Path
) -> None:
    # Pydantic enforces the min_length=1 on workspace_id at the schema
    # layer before our launcher sees it.
    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "", "workspace_path": str(tmp_path)},
    )
    assert resp.status_code == 422


def test_post_preview_workspace_id_charset_validation_returns_400(
    client: TestClient, tmp_path: Path
) -> None:
    # Charset rejection happens inside the WebSandboxConfig
    # constructor — surfaces as 400.
    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws/42", "workspace_path": str(tmp_path)},
    )
    assert resp.status_code == 400
    assert "workspace_id" in resp.json()["detail"]


def test_post_preview_bad_container_port_returns_422(
    client: TestClient, tmp_path: Path
) -> None:
    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-42",
            "workspace_path": str(tmp_path),
            "container_port": 0,
        },
    )
    assert resp.status_code == 422


# ── GET /web-sandbox/preview/{id} ─────────────────────────────────


def test_get_preview_404_when_unknown(client: TestClient) -> None:
    resp = client.get("/web-sandbox/preview/ws-nope")
    assert resp.status_code == 404


def test_get_preview_returns_state(client: TestClient, tmp_path: Path) -> None:
    client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
    )
    resp = client.get("/web-sandbox/preview/ws-42")
    assert resp.status_code == 200
    assert resp.json()["workspace_id"] == "ws-42"


# ── GET /web-sandbox/preview (list) ────────────────────────────────


def test_list_preview_empty(client: TestClient) -> None:
    resp = client.get("/web-sandbox/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["sandboxes"] == []


def test_list_preview_two_workspaces(client: TestClient, tmp_path: Path) -> None:
    client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-a", "workspace_path": str(tmp_path)},
    )
    client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-b", "workspace_path": str(tmp_path)},
    )
    resp = client.get("/web-sandbox/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2


# ── POST /touch ───────────────────────────────────────────────────


def test_post_touch_bumps_last_request_at(
    client: TestClient, tmp_path: Path
) -> None:
    client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
    )
    before = client.get("/web-sandbox/preview/ws-42").json()["last_request_at"]
    after = client.post("/web-sandbox/preview/ws-42/touch").json()["last_request_at"]
    assert after > before


def test_post_touch_404_when_unknown(client: TestClient) -> None:
    resp = client.post("/web-sandbox/preview/ws-nope/touch")
    assert resp.status_code == 404


# ── POST /ready ───────────────────────────────────────────────────


def test_post_ready_transitions(client: TestClient, tmp_path: Path) -> None:
    client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
    )
    resp = client.post("/web-sandbox/preview/ws-42/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == WebSandboxStatus.running.value
    assert body["ready_at"] is not None


def test_post_ready_404_when_unknown(client: TestClient) -> None:
    resp = client.post("/web-sandbox/preview/ws-nope/ready")
    assert resp.status_code == 404


# ── DELETE /web-sandbox/preview/{id} ──────────────────────────────


def test_delete_preview(client: TestClient, tmp_path: Path) -> None:
    client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
    )
    resp = client.delete("/web-sandbox/preview/ws-42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == WebSandboxStatus.stopped.value
    assert body["stopped_at"] is not None


def test_delete_preview_with_reason(client: TestClient, tmp_path: Path) -> None:
    client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
    )
    resp = client.delete("/web-sandbox/preview/ws-42?reason=idle_timeout")
    assert resp.status_code == 200
    assert resp.json()["killed_reason"] == "idle_timeout"


def test_delete_preview_404_when_unknown(client: TestClient) -> None:
    resp = client.delete("/web-sandbox/preview/ws-nope")
    assert resp.status_code == 404


# ── Lifecycle integration: launch → touch → ready → delete ────────


def test_full_lifecycle(client: TestClient, tmp_path: Path) -> None:
    body = {"workspace_id": "ws-42", "workspace_path": str(tmp_path)}
    launch = client.post("/web-sandbox/preview", json=body).json()
    assert launch["status"] == WebSandboxStatus.installing.value
    touched = client.post("/web-sandbox/preview/ws-42/touch").json()
    assert touched["status"] == WebSandboxStatus.installing.value
    ready = client.post("/web-sandbox/preview/ws-42/ready").json()
    assert ready["status"] == WebSandboxStatus.running.value
    deleted = client.delete("/web-sandbox/preview/ws-42").json()
    assert deleted["status"] == WebSandboxStatus.stopped.value


# ── Schema-version pin ────────────────────────────────────────────


def test_response_carries_schema_version(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
    )
    assert resp.json()["schema_version"] == ws.WEB_SANDBOX_SCHEMA_VERSION


# ── Router prefix + paths ────────────────────────────────────────


def test_router_prefix() -> None:
    assert web_sandbox_router.router.prefix == "/web-sandbox"


def test_router_exposes_expected_paths() -> None:
    # Ensure operators / dashboards know what to expect.
    pairs: set[tuple[str, str]] = set()
    for r in web_sandbox_router.router.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None) or ()
        if path is None:
            continue
        for m in methods:
            pairs.add((path, m))
    assert ("/web-sandbox/preview", "POST") in pairs
    assert ("/web-sandbox/preview", "GET") in pairs
    assert ("/web-sandbox/preview/{workspace_id}", "GET") in pairs
    assert ("/web-sandbox/preview/{workspace_id}", "DELETE") in pairs
    assert ("/web-sandbox/preview/{workspace_id}/touch", "POST") in pairs
    assert ("/web-sandbox/preview/{workspace_id}/ready", "POST") in pairs


# ── W14.3 — CFIngressManager wiring ───────────────────────────────


def test_w14_3_build_cf_ingress_manager_returns_none_when_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the four W14.3 env knobs are absent, get_manager() must
    fall back to the W14.2 path with ``cf_ingress_manager=None``."""

    monkeypatch.setenv("OMNISIGHT_TUNNEL_HOST", "")
    monkeypatch.setenv("OMNISIGHT_CF_API_TOKEN", "")
    monkeypatch.setenv("OMNISIGHT_CF_ACCOUNT_ID", "")
    monkeypatch.setenv("OMNISIGHT_CF_TUNNEL_ID", "")

    cf = web_sandbox_router._build_cf_ingress_manager()
    assert cf is None


def test_w14_3_build_cf_ingress_manager_returns_manager_when_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All four knobs set + valid → returns a CFIngressManager."""

    monkeypatch.setenv("OMNISIGHT_TUNNEL_HOST", "ai.sora-dev.app")
    monkeypatch.setenv("OMNISIGHT_CF_API_TOKEN", "deadbeefdeadbeefdeadbeefdeadbeef")
    monkeypatch.setenv("OMNISIGHT_CF_ACCOUNT_ID", "0" * 32)
    monkeypatch.setenv("OMNISIGHT_CF_TUNNEL_ID", "1" * 32)

    cf = web_sandbox_router._build_cf_ingress_manager()
    from backend.cf_ingress import CFIngressManager

    assert isinstance(cf, CFIngressManager)
    assert cf.config.tunnel_host == "ai.sora-dev.app"
    assert cf.config.account_id == "0" * 32


def test_w14_3_build_cf_ingress_manager_returns_none_when_token_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed value (token set but tunnel_id is not 32-hex) ⇒
    construct_cf_ingress_manager logs a warning and returns None."""

    monkeypatch.setenv("OMNISIGHT_TUNNEL_HOST", "ai.sora-dev.app")
    monkeypatch.setenv("OMNISIGHT_CF_API_TOKEN", "deadbeefdeadbeefdeadbeefdeadbeef")
    monkeypatch.setenv("OMNISIGHT_CF_ACCOUNT_ID", "0" * 32)
    monkeypatch.setenv("OMNISIGHT_CF_TUNNEL_ID", "not-a-uuid")

    cf = web_sandbox_router._build_cf_ingress_manager()
    assert cf is None


def test_w14_3_post_response_carries_ingress_url_field(
    client: TestClient, tmp_path: Path
) -> None:
    """The response body must include the ``ingress_url`` field
    (W14.2 schema), even when CF wiring is absent (value is null)."""

    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "ingress_url" in body
    assert body["ingress_url"] is None  # No CF wiring on this fixture


# ── W14.4 — CFAccessManager wiring ────────────────────────────────


def test_w14_4_build_cf_access_manager_returns_none_when_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the four W14.4 env knobs are absent, get_manager() must
    fall back to the W14.3 path with ``cf_access_manager=None``."""

    monkeypatch.setenv("OMNISIGHT_TUNNEL_HOST", "")
    monkeypatch.setenv("OMNISIGHT_CF_API_TOKEN", "")
    monkeypatch.setenv("OMNISIGHT_CF_ACCOUNT_ID", "")
    monkeypatch.setenv("OMNISIGHT_CF_ACCESS_TEAM_DOMAIN", "")

    cf = web_sandbox_router._build_cf_access_manager()
    assert cf is None


def test_w14_4_build_cf_access_manager_returns_manager_when_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All four knobs set + valid → returns a CFAccessManager."""

    monkeypatch.setenv("OMNISIGHT_TUNNEL_HOST", "ai.sora-dev.app")
    monkeypatch.setenv("OMNISIGHT_CF_API_TOKEN", "deadbeefdeadbeefdeadbeefdeadbeef")
    monkeypatch.setenv("OMNISIGHT_CF_ACCOUNT_ID", "0" * 32)
    monkeypatch.setenv(
        "OMNISIGHT_CF_ACCESS_TEAM_DOMAIN", "acme.cloudflareaccess.com"
    )
    monkeypatch.setenv(
        "OMNISIGHT_CF_ACCESS_DEFAULT_EMAILS", "admin@example.com,oncall@example.com"
    )

    cf = web_sandbox_router._build_cf_access_manager()
    from backend.cf_access import CFAccessManager

    assert isinstance(cf, CFAccessManager)
    assert cf.config.tunnel_host == "ai.sora-dev.app"
    assert cf.config.team_domain == "acme.cloudflareaccess.com"
    assert "admin@example.com" in cf.config.default_emails
    assert "oncall@example.com" in cf.config.default_emails


def test_w14_4_build_cf_access_manager_returns_none_when_team_domain_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed value (team_domain has the wrong suffix) ⇒
    _build_cf_access_manager logs and returns None."""

    monkeypatch.setenv("OMNISIGHT_TUNNEL_HOST", "ai.sora-dev.app")
    monkeypatch.setenv("OMNISIGHT_CF_API_TOKEN", "deadbeefdeadbeefdeadbeefdeadbeef")
    monkeypatch.setenv("OMNISIGHT_CF_ACCOUNT_ID", "0" * 32)
    monkeypatch.setenv(
        "OMNISIGHT_CF_ACCESS_TEAM_DOMAIN", "acme.example.com"  # wrong suffix
    )

    cf = web_sandbox_router._build_cf_access_manager()
    assert cf is None


def test_w14_4_post_response_carries_access_app_id_field(
    client: TestClient, tmp_path: Path
) -> None:
    """The response body must include the ``access_app_id`` field
    (W14.4 schema), even when CF Access wiring is absent (value is
    null)."""

    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_app_id" in body
    assert body["access_app_id"] is None  # No CF Access wiring on this fixture


def test_w14_4_post_response_carries_allowed_emails_in_config(
    client: TestClient, tmp_path: Path
) -> None:
    """The router auto-prepends the operator's email to the config's
    ``allowed_emails`` so the manager can build the CF Access policy."""

    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-42",
            "workspace_path": str(tmp_path),
            "allowed_emails": ["second@example.com"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # Operator email (from the fixture's _operator() factory) is at index 0,
    # caller-supplied emails follow.
    emails = body["config"]["allowed_emails"]
    assert "op@example.com" in emails
    assert "second@example.com" in emails


def test_w14_4_post_response_default_allowed_emails_is_just_operator(
    client: TestClient, tmp_path: Path
) -> None:
    """When no allowed_emails are supplied, the router still records
    the operator's email so the launcher has at least one identity to
    let through CF Access."""

    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["config"]["allowed_emails"] == ["op@example.com"]


def test_w14_4_post_with_allowed_emails_validates_at_config_layer(
    client: TestClient, tmp_path: Path
) -> None:
    """Passing a non-string entry trips the WebSandboxConfig validator
    → 400."""

    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-42",
            "workspace_path": str(tmp_path),
            "allowed_emails": [123],  # invalid
        },
    )
    # FastAPI/Pydantic rejects at the schema layer before we hit the
    # WebSandboxConfig validator.
    assert resp.status_code == 422


# ─────────────── W14.8 — PEP HOLD before first preview ───────────────
#
# The router calls ``backend.web_sandbox_pep.evaluate_first_preview_hold``
# on cold launches (no live instance). Tests inject a fake evaluator
# via ``set_pep_evaluator_for_tests`` so we can assert all four
# branches (approved → 200 + decision_id, rejected → 403, gateway
# error → 503, idempotent re-launch → no PEP call) deterministically.


class _PepEvaluatorRecorder:
    """Records every ``(workspace_id, kwargs)`` call and returns the
    canned :class:`WebPreviewPepResult` queued by the test."""

    def __init__(self, results: list[_wsp.WebPreviewPepResult]):
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> _wsp.WebPreviewPepResult:
        self.calls.append(kwargs)
        if not self._results:
            raise AssertionError("PepEvaluatorRecorder ran out of canned results")
        return self._results.pop(0)


def test_w14_8_first_preview_holds_via_pep_gateway(
    client: TestClient, tmp_path: Path,
) -> None:
    """Cold launch goes through the PEP HOLD; on approval the router
    proceeds with manager.launch and surfaces the decision_id on the
    response."""

    recorder = _PepEvaluatorRecorder([
        _wsp.WebPreviewPepResult(
            action="approved",
            decision_id="dec-w14-8-approved",
            rule="tier_unlisted",
        ),
    ])
    web_sandbox_router.set_pep_evaluator_for_tests(recorder)

    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-fresh", "workspace_path": str(tmp_path)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pep_decision_id"] == "dec-w14-8-approved"
    assert body["status"] == WebSandboxStatus.installing.value
    # Recorder saw the workspace + image + actor email forwarded.
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["workspace_id"] == "ws-fresh"
    assert call["workspace_path"] == str(tmp_path)
    assert call["image_tag"] == ws.DEFAULT_IMAGE_TAG
    # actor_email forwarded from the dependency-overridden operator.
    assert call["actor_email"] == "op@example.com"


def test_w14_8_first_preview_rejected_returns_403(
    client: TestClient, tmp_path: Path,
) -> None:
    """PEP rejection surfaces as HTTP 403 — frontend renders the
    operator's reject reason, no docker work happens."""

    recorder = _PepEvaluatorRecorder([
        _wsp.WebPreviewPepResult(
            action="rejected",
            reason="operator rejected",
            decision_id="dec-rejected",
            rule="tier_unlisted",
        ),
    ])
    web_sandbox_router.set_pep_evaluator_for_tests(recorder)

    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-blocked", "workspace_path": str(tmp_path)},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "pep_first_preview_rejected"
    assert detail["decision_id"] == "dec-rejected"
    assert detail["reason"] == "operator rejected"
    assert detail["rule"] == "tier_unlisted"


def test_w14_8_gateway_error_returns_503(
    client: TestClient, tmp_path: Path,
) -> None:
    """A gateway-error result (raised propose / fastapi-internal)
    surfaces as 503 so the operator can retry."""

    recorder = _PepEvaluatorRecorder([
        _wsp.WebPreviewPepResult(
            action="gateway_error",
            reason="pep_gateway_error:RuntimeError",
        ),
    ])
    web_sandbox_router.set_pep_evaluator_for_tests(recorder)

    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-broken", "workspace_path": str(tmp_path)},
    )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["code"] == "pep_gateway_unavailable"
    assert "RuntimeError" in detail["reason"]


def test_w14_8_idempotent_relaunch_skips_pep(
    client: TestClient, tmp_path: Path,
) -> None:
    """Once a sandbox is live, re-POSTing the same workspace_id is an
    idempotent recovery — no second PEP HOLD."""

    recorder = _PepEvaluatorRecorder([
        _wsp.WebPreviewPepResult(action="approved", decision_id="dec-1"),
        # Second result should never be consumed — recorder asserts on
        # exhaustion if it is.
        _wsp.WebPreviewPepResult(action="approved", decision_id="dec-2"),
    ])
    web_sandbox_router.set_pep_evaluator_for_tests(recorder)

    body = {"workspace_id": "ws-stable", "workspace_path": str(tmp_path)}
    a = client.post("/web-sandbox/preview", json=body)
    b = client.post("/web-sandbox/preview", json=body)
    assert a.status_code == 200
    assert b.status_code == 200
    # Cold launch consumed exactly one PEP evaluation.
    assert len(recorder.calls) == 1
    # First call carried decision_id; idempotent second call does not.
    assert a.json()["pep_decision_id"] == "dec-1"
    assert b.json().get("pep_decision_id") is None


def test_w14_8_set_pep_evaluator_for_tests_resets_with_none(
    client: TestClient, tmp_path: Path,
) -> None:
    """``set_pep_evaluator_for_tests(None)`` reverts to the production
    evaluator (drift guard for the test seam)."""

    rec = _PepEvaluatorRecorder([
        _wsp.WebPreviewPepResult(action="approved", decision_id="dec-1"),
    ])
    web_sandbox_router.set_pep_evaluator_for_tests(rec)
    assert web_sandbox_router.get_pep_evaluator() is rec

    web_sandbox_router.set_pep_evaluator_for_tests(None)
    assert web_sandbox_router.get_pep_evaluator() is _wsp.evaluate_first_preview_hold


def test_w14_8_default_evaluator_is_module_function(
    # reset handled by autouse stub_pep_evaluator
) -> None:
    """Drift guard — the router must default to the public evaluator
    function so a future rename in :mod:`backend.web_sandbox_pep`
    breaks compile-time, not at first request."""

    web_sandbox_router.set_pep_evaluator_for_tests(None)
    assert web_sandbox_router.get_pep_evaluator() is _wsp.evaluate_first_preview_hold


def test_w14_8_terminal_instance_re_holds_on_relaunch(
    client: TestClient, tmp_path: Path, manager: WebSandboxManager,
    # reset handled by autouse stub_pep_evaluator
) -> None:
    """Operator stops the sandbox → next launch is again 'first' and
    re-pays the PEP HOLD. The W14.5 idle reaper / W14.10 PG audit will
    eventually persist this transition; today the per-worker manager
    enforces it."""

    recorder = _PepEvaluatorRecorder([
        _wsp.WebPreviewPepResult(action="approved", decision_id="dec-cold-1"),
        _wsp.WebPreviewPepResult(action="approved", decision_id="dec-cold-2"),
    ])
    web_sandbox_router.set_pep_evaluator_for_tests(recorder)

    body = {"workspace_id": "ws-cycle", "workspace_path": str(tmp_path)}

    # First cold launch.
    a = client.post("/web-sandbox/preview", json=body)
    assert a.status_code == 200
    assert a.json()["pep_decision_id"] == "dec-cold-1"

    # Stop it (operator action).
    d = client.delete("/web-sandbox/preview/ws-cycle")
    assert d.status_code == 200
    assert d.json()["status"] == WebSandboxStatus.stopped.value

    # Second launch — manager.get returns the terminal instance →
    # requires_first_preview_hold returns True → PEP HOLD again.
    b = client.post("/web-sandbox/preview", json=body)
    # The launcher's idempotent re-launch path returns the terminal
    # instance unchanged (it doesn't auto-restart on terminal). What
    # matters for W14.8 contract is that the PEP gate fired again.
    assert b.status_code == 200
    assert len(recorder.calls) == 2


def test_w14_8_response_schema_includes_pep_decision_id_field(
    client: TestClient, tmp_path: Path,
) -> None:
    """Response carries pep_decision_id on cold launches (and a None
    placeholder on idempotent re-launches when the test client decodes
    via the typed response model)."""

    recorder = _PepEvaluatorRecorder([
        _wsp.WebPreviewPepResult(action="approved", decision_id="dec-shape"),
    ])
    web_sandbox_router.set_pep_evaluator_for_tests(recorder)

    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-shape", "workspace_path": str(tmp_path)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "pep_decision_id" in body
    assert body["pep_decision_id"] == "dec-shape"


def test_w14_8_evaluator_receives_image_tag_default(
    client: TestClient, tmp_path: Path,
) -> None:
    """When the caller doesn't override image_tag, the router
    forwards :data:`backend.web_sandbox.DEFAULT_IMAGE_TAG` so the
    coaching card renders the production image, not an empty string."""

    recorder = _PepEvaluatorRecorder([
        _wsp.WebPreviewPepResult(action="approved", decision_id="dec-img"),
    ])
    web_sandbox_router.set_pep_evaluator_for_tests(recorder)

    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-img", "workspace_path": str(tmp_path)},
    )
    assert resp.status_code == 200
    assert recorder.calls[0]["image_tag"] == ws.DEFAULT_IMAGE_TAG


def test_w14_8_evaluator_forwards_git_ref_and_container_port(
    client: TestClient, tmp_path: Path,
) -> None:
    """git_ref + container_port land in the evaluator kwargs so the
    coaching card / audit row see exactly what the operator clicked."""

    recorder = _PepEvaluatorRecorder([
        _wsp.WebPreviewPepResult(action="approved", decision_id="dec-fwd"),
    ])
    web_sandbox_router.set_pep_evaluator_for_tests(recorder)

    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-fwd",
            "workspace_path": str(tmp_path),
            "git_ref": "feature/pretty",
            "container_port": 3000,
        },
    )
    assert resp.status_code == 200
    call = recorder.calls[0]
    assert call["git_ref"] == "feature/pretty"
    assert call["container_port"] == 3000


# ─────────────── W14.9 — Resource limit cgroup wiring ───────────────


def test_w14_9_build_resource_limits_returns_default_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the three OMNISIGHT_WEB_SANDBOX_* env knobs are absent,
    _build_resource_limits returns the row-spec default (2g/1/5g)."""

    monkeypatch.setenv("OMNISIGHT_WEB_SANDBOX_MEMORY_LIMIT", "")
    monkeypatch.setenv("OMNISIGHT_WEB_SANDBOX_CPU_LIMIT", "")
    monkeypatch.setenv("OMNISIGHT_WEB_SANDBOX_STORAGE_LIMIT", "")

    from backend.web_sandbox_resource_limits import WebPreviewResourceLimits

    limits = web_sandbox_router._build_resource_limits()
    assert limits == WebPreviewResourceLimits.default()


def test_w14_9_build_resource_limits_respects_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_WEB_SANDBOX_MEMORY_LIMIT", "4g")
    monkeypatch.setenv("OMNISIGHT_WEB_SANDBOX_CPU_LIMIT", "2")
    monkeypatch.setenv("OMNISIGHT_WEB_SANDBOX_STORAGE_LIMIT", "10g")

    limits = web_sandbox_router._build_resource_limits()
    assert limits.memory_limit_bytes == 4 * 1024**3
    assert limits.cpu_limit == 2.0
    assert limits.storage_limit_bytes == 10 * 1024**3


def test_w14_9_build_resource_limits_falls_back_on_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed env knob ⇒ logs + falls back to defaults rather than
    500'ing every launch — same gracefully-degrade pattern as W14.5
    idle reaper / W14.4 CF Access."""

    monkeypatch.setenv("OMNISIGHT_WEB_SANDBOX_MEMORY_LIMIT", "2x")  # bogus
    from backend.web_sandbox_resource_limits import WebPreviewResourceLimits

    limits = web_sandbox_router._build_resource_limits()
    assert limits == WebPreviewResourceLimits.default()


def test_w14_9_build_resource_limits_storage_disabled_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMNISIGHT_WEB_SANDBOX_STORAGE_LIMIT=off disables --storage-opt
    for operators on overlay2-on-ext4 hosts."""

    monkeypatch.setenv("OMNISIGHT_WEB_SANDBOX_STORAGE_LIMIT", "off")
    limits = web_sandbox_router._build_resource_limits()
    assert limits.storage_limit_bytes is None


def test_w14_9_post_response_carries_resource_limits_in_config(
    client: TestClient, tmp_path: Path,
) -> None:
    """When the request body specifies a resource override, it round-
    trips through to the response body's config block."""

    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-42",
            "workspace_path": str(tmp_path),
            "memory_limit": "3g",
            "cpu_limit": 0.5,
            "storage_limit": "8g",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rl = body["config"]["resource_limits"]
    assert rl is not None
    assert rl["memory_limit_bytes"] == 3 * 1024**3
    assert rl["cpu_limit"] == 0.5
    assert rl["storage_limit_bytes"] == 8 * 1024**3


def test_w14_9_post_response_resource_limits_none_by_default(
    client: TestClient, tmp_path: Path,
) -> None:
    """Without per-launch override, config.resource_limits is null
    (manager-wide policy applies; the manager's wiring is asserted
    in test_web_sandbox.py)."""

    resp = client.post(
        "/web-sandbox/preview",
        json={"workspace_id": "ws-42", "workspace_path": str(tmp_path)},
    )
    assert resp.status_code == 200
    assert resp.json()["config"]["resource_limits"] is None


def test_w14_9_post_rejects_malformed_memory_limit(
    client: TestClient, tmp_path: Path,
) -> None:
    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-42",
            "workspace_path": str(tmp_path),
            "memory_limit": "2x",
        },
    )
    assert resp.status_code == 400


def test_w14_9_post_rejects_malformed_cpu_limit(
    client: TestClient, tmp_path: Path,
) -> None:
    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-42",
            "workspace_path": str(tmp_path),
            "cpu_limit": "abc",
        },
    )
    assert resp.status_code == 400


def test_w14_9_post_storage_disabled_token(
    client: TestClient, tmp_path: Path,
) -> None:
    """Per-launch storage_limit='off' disables the disk cap on this
    one launch — useful when an operator on overlay2-on-ext4 launches
    a one-off sandbox and knows the host can't enforce."""

    resp = client.post(
        "/web-sandbox/preview",
        json={
            "workspace_id": "ws-42",
            "workspace_path": str(tmp_path),
            "storage_limit": "off",
        },
    )
    assert resp.status_code == 200
    rl = resp.json()["config"]["resource_limits"]
    assert rl["storage_limit_bytes"] is None
