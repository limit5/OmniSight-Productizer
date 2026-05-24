"""WP.1.4 -- ``POST /shareable-objects`` permalink endpoint contracts.

These tests pin the request / response shape consumed by the WP.1
``<Block />`` share dialog (``components/omnisight/block.tsx``) so the
durable ``shareable_objects`` row carries the operator's selected
regions + redaction mask before WP.9.5 enforcement reads them back.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth
from backend import shareable_objects as so_module
from backend.routers import shareable_objects as router_module


class _FakeAcquire:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


class _FakeConn:
    """asyncpg-shaped fake recording each ``fetchrow`` insert call."""

    def __init__(self, rows) -> None:
        self._rows = list(rows)
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *params):
        self.calls.append((sql, params))
        if not self._rows:
            return None
        return self._rows.pop(0)


def _viewer(tenant_id: str = "t-default", user_id: str = "u-1") -> auth.User:
    return auth.User(
        id=user_id,
        email="viewer@example.com",
        name="viewer",
        role="viewer",
        tenant_id=tenant_id,
    )


def _stub_share_row(
    *,
    share_id: str = "sh-stub_slug_____________",
    object_kind: str = "block",
    object_id: str = "blk-1",
    tenant_id: str = "t-default",
    owner_user_id: str = "u-1",
    visibility: str = "private",
    redaction_applied: dict | None = None,
) -> dict:
    return {
        "share_id": share_id,
        "object_kind": object_kind,
        "object_id": object_id,
        "tenant_id": tenant_id,
        "owner_user_id": owner_user_id,
        "visibility": visibility,
        "expires_at": None,
        "redaction_applied": redaction_applied or {},
        "created_at": "2026-05-19 00:00:00",
    }


def _stub_block_row(
    *,
    block_id: str = "blk-1",
    tenant_id: str = "t-default",
    payload: dict | None = None,
    metadata: dict | None = None,
) -> dict:
    return {
        "block_id": block_id,
        "parent_id": None,
        "tenant_id": tenant_id,
        "user_id": "u-1",
        "project_id": "p-1",
        "session_id": "s-1",
        "kind": "command",
        "status": "completed",
        "title": "Probe target",
        "payload": json.dumps(payload or {"command": "uname -a"}),
        "metadata": json.dumps(metadata or {}),
        "started_at": None,
        "completed_at": None,
        "created_at": "2026-05-19 00:00:00",
    }


def _client(monkeypatch, user: auth.User, conn) -> TestClient:
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[auth.require_viewer] = lambda: user
    monkeypatch.setattr(router_module, "get_pool", lambda: _FakePool(conn))
    return TestClient(app)


def test_router_is_mounted_on_versioned_app() -> None:
    """The router is registered alongside the other versioned routers."""
    from backend import main as backend_main

    src = inspect.getsource(backend_main)
    assert "from backend.routers import shareable_objects" in src
    assert "_shareable_objects_router.router" in src


def test_router_prefix_and_role() -> None:
    """The router uses the documented prefix and viewer-level gate."""
    assert router_module.router.prefix == "/shareable-objects"
    create_src = inspect.getsource(router_module.create_shareable_object)
    resolve_src = inspect.getsource(router_module.resolve_shareable_object)
    assert "Depends(auth.require_viewer)" in create_src
    assert "Depends(auth.require_viewer)" in resolve_src


def test_get_team_share_denies_non_permitted_caller_over_http(monkeypatch) -> None:
    conn = _FakeConn([
        _stub_share_row(owner_user_id="u-owner", visibility="team"),
        {"role": "viewer", "status": "active"},
    ])
    client = _client(monkeypatch, _viewer(user_id="u-other"), conn)

    res = client.get("/shareable-objects/sh-stub_slug_____________")

    assert res.status_code == 404
    assert conn.calls[1] == (
        so_module._FETCH_USER_TENANT_MEMBERSHIP_SQL,
        ("u-other", "t-default"),
    )


def test_get_block_share_masks_redacted_region_on_wire(monkeypatch) -> None:
    conn = _FakeConn([
        _stub_share_row(
            redaction_applied={"mask": {"payload.command": "secret"}},
        ),
        _stub_block_row(payload={
            "command": "curl https://internal.example",
            "stdout": "public output",
        }),
    ])
    client = _client(monkeypatch, _viewer(), conn)

    res = client.get("/shareable-objects/sh-stub_slug_____________")
    body = res.json()

    assert res.status_code == 200
    assert body["share_id"] == "sh-stub_slug_____________"
    assert body["object_kind"] == "block"
    assert body["payload"] == {
        "command": "[REDACTED:secret]",
        "stdout": "public output",
    }
    assert body["metadata"]["block_id"] == "blk-1"


def test_get_expired_share_returns_404_over_http(monkeypatch) -> None:
    conn = _FakeConn([])
    client = _client(monkeypatch, _viewer(), conn)

    res = client.get("/shareable-objects/sh-stub_slug_____________")

    assert res.status_code == 404
    assert "expires_at > now()" in conn.calls[0][0]


def test_get_runbook_share_serves_loaded_runbook_over_http(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runbook_dir = tmp_path / ".omnisight" / "runbooks"
    runbook_dir.mkdir(parents=True)
    (runbook_dir / "demo.yaml").write_text(
        "\n".join([
            "name: demo",
            "description: Shared runbook",
            "tags: [bringup]",
            "steps:",
            "  - kind: command",
            "    title: Probe",
            "    command: uname -a",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(router_module, "_project_root", lambda: tmp_path)
    conn = _FakeConn([
        _stub_share_row(
            object_kind="runbook",
            object_id="demo",
            redaction_applied={"mask": {"payload.steps.0.payload.command": "secret"}},
        ),
    ])
    client = _client(monkeypatch, _viewer(), conn)

    res = client.get("/shareable-objects/sh-stub_slug_____________")
    body = res.json()

    assert res.status_code == 200
    assert body["object_kind"] == "runbook"
    assert body["object_id"] == "demo"
    assert body["payload"]["name"] == "demo"
    assert body["payload"]["steps"][0]["payload"]["command"] == (
        "[REDACTED:secret]"
    )


@pytest.mark.asyncio
async def test_create_block_share_writes_regions_and_mask(monkeypatch) -> None:
    conn = _FakeConn([_stub_share_row()])
    monkeypatch.setattr(router_module, "get_pool", lambda: _FakePool(conn))

    req = router_module.CreateShareableObjectRequest(
        object_kind="block",
        object_id="blk-1",
        visibility="private",
        regions=["command", "output"],
        redaction_mask={
            "payload.command": "secret",
            "metadata.customer_ip": "customer_ip",
            "payload.stdout": ["secret", "pii"],
        },
        base_url="https://omnisight.local",
    )

    res = await router_module.create_shareable_object(req, actor=_viewer())
    body = json.loads(res.body)

    assert res.status_code == 201
    assert body["share_id"] == "sh-stub_slug_____________"
    assert body["object_kind"] == "block"
    assert body["object_id"] == "blk-1"
    assert body["visibility"] == "private"
    assert body["permalink_url"] == (
        "https://omnisight.local/share/sh-stub_slug_____________"
    )

    assert len(conn.calls) == 1
    _, params = conn.calls[0]
    assert params[1] == "block"            # object_kind
    assert params[2] == "blk-1"            # object_id
    assert params[3] == "t-default"        # tenant_id (defaulted from actor)
    assert params[4] == "u-1"              # owner_user_id (from actor)
    assert params[5] == "private"          # visibility

    stored_redaction = json.loads(params[6])
    assert stored_redaction == {
        "regions": ["command", "output"],
        "mask": {
            "payload.command": "secret",
            "metadata.customer_ip": "customer_ip",
            "payload.stdout": ["secret", "pii"],
        },
    }


@pytest.mark.asyncio
async def test_block_share_defaults_to_all_share_regions(monkeypatch) -> None:
    conn = _FakeConn([_stub_share_row()])
    monkeypatch.setattr(router_module, "get_pool", lambda: _FakePool(conn))

    req = router_module.CreateShareableObjectRequest(
        object_kind="block",
        object_id="blk-1",
        base_url="https://omnisight.local",
    )

    await router_module.create_shareable_object(req, actor=_viewer())

    stored_redaction = json.loads(conn.calls[0][1][6])
    assert stored_redaction == {
        "regions": ["command", "output", "metadata", "screenshots"],
        "mask": {},
    }


@pytest.mark.asyncio
async def test_response_omits_permalink_when_base_url_missing(monkeypatch) -> None:
    conn = _FakeConn([_stub_share_row()])
    monkeypatch.setattr(router_module, "get_pool", lambda: _FakePool(conn))

    req = router_module.CreateShareableObjectRequest(
        object_kind="block",
        object_id="blk-1",
    )
    res = await router_module.create_shareable_object(req, actor=_viewer())
    body = json.loads(res.body)

    assert "permalink_url" not in body
    assert body["share_id"] == "sh-stub_slug_____________"


@pytest.mark.asyncio
async def test_explicit_tenant_overrides_caller_default(monkeypatch) -> None:
    conn = _FakeConn([
        _stub_share_row(tenant_id="t-explicit"),
    ])
    monkeypatch.setattr(router_module, "get_pool", lambda: _FakePool(conn))

    req = router_module.CreateShareableObjectRequest(
        object_kind="block",
        object_id="blk-1",
        tenant_id="t-explicit",
    )
    await router_module.create_shareable_object(
        req, actor=_viewer(tenant_id="t-default"),
    )

    assert conn.calls[0][1][3] == "t-explicit"


@pytest.mark.asyncio
async def test_visibility_team_round_trips(monkeypatch) -> None:
    conn = _FakeConn([_stub_share_row(visibility="team")])
    monkeypatch.setattr(router_module, "get_pool", lambda: _FakePool(conn))

    req = router_module.CreateShareableObjectRequest(
        object_kind="block",
        object_id="blk-1",
        visibility="team",
    )
    res = await router_module.create_shareable_object(req, actor=_viewer())
    body = json.loads(res.body)

    assert conn.calls[0][1][5] == "team"
    assert body["visibility"] == "team"


@pytest.mark.asyncio
async def test_rejects_unknown_region(monkeypatch) -> None:
    monkeypatch.setattr(
        router_module, "get_pool", lambda: _FakePool(_FakeConn([])),
    )

    req = router_module.CreateShareableObjectRequest(
        object_kind="block",
        object_id="blk-1",
        regions=["command", "definitely-not-a-region"],
    )
    with pytest.raises(HTTPException) as exc:
        await router_module.create_shareable_object(req, actor=_viewer())
    assert exc.value.status_code == 400
    assert "unknown values" in exc.value.detail


@pytest.mark.asyncio
async def test_rejects_unknown_redaction_reason(monkeypatch) -> None:
    monkeypatch.setattr(
        router_module, "get_pool", lambda: _FakePool(_FakeConn([])),
    )
    req = router_module.CreateShareableObjectRequest(
        object_kind="block",
        object_id="blk-1",
        redaction_mask={"payload.command": "totally-bogus"},
    )
    with pytest.raises(HTTPException) as exc:
        await router_module.create_shareable_object(req, actor=_viewer())
    assert exc.value.status_code == 400
    assert "not recognised" in exc.value.detail


@pytest.mark.asyncio
async def test_rejects_invalid_base_url(monkeypatch) -> None:
    monkeypatch.setattr(
        router_module, "get_pool", lambda: _FakePool(_FakeConn([])),
    )
    req = router_module.CreateShareableObjectRequest(
        object_kind="block",
        object_id="blk-1",
        base_url="ftp://nope.example.com",
    )
    with pytest.raises(HTTPException) as exc:
        await router_module.create_shareable_object(req, actor=_viewer())
    assert exc.value.status_code == 400
    assert "absolute http(s) URL" in exc.value.detail


@pytest.mark.asyncio
async def test_rejects_regions_for_non_block_kind(monkeypatch) -> None:
    monkeypatch.setattr(
        router_module, "get_pool", lambda: _FakePool(_FakeConn([])),
    )
    req = router_module.CreateShareableObjectRequest(
        object_kind="runbook",
        object_id="rb-1",
        regions=["command"],
    )
    with pytest.raises(HTTPException) as exc:
        await router_module.create_shareable_object(req, actor=_viewer())
    assert exc.value.status_code == 400
    assert "object_kind='block'" in exc.value.detail


@pytest.mark.asyncio
async def test_slug_collision_returns_503(monkeypatch) -> None:
    from backend import shareable_objects as so_module

    async def _always_collide(*args, **kwargs):
        raise so_module.ShareSlugCollisionError("budget exhausted")

    monkeypatch.setattr(
        router_module, "get_pool", lambda: _FakePool(_FakeConn([])),
    )
    monkeypatch.setattr(
        router_module._so, "create_shareable_object", _always_collide,
    )

    req = router_module.CreateShareableObjectRequest(
        object_kind="block",
        object_id="blk-1",
    )
    with pytest.raises(HTTPException) as exc:
        await router_module.create_shareable_object(req, actor=_viewer())
    assert exc.value.status_code == 503
    assert "retry" in exc.value.detail


@pytest.mark.asyncio
async def test_non_block_kind_drops_regions_in_redaction_applied(monkeypatch) -> None:
    conn = _FakeConn([
        {
            **_stub_share_row(),
            "object_kind": "runbook",
            "object_id": "rb-1",
        },
    ])
    monkeypatch.setattr(router_module, "get_pool", lambda: _FakePool(conn))

    req = router_module.CreateShareableObjectRequest(
        object_kind="runbook",
        object_id="rb-1",
        redaction_mask={"payload.command": "secret"},
    )
    await router_module.create_shareable_object(req, actor=_viewer())

    stored_redaction = json.loads(conn.calls[0][1][6])
    assert stored_redaction == {"mask": {"payload.command": "secret"}}
