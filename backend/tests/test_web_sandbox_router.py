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
