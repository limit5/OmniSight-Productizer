"""WP.8 — Runbook router contracts (OP-1502).

Locks the request / response shape the FE Block context-menu "Save as
Runbook" and the dashboard runbook surface call against. Mirrors the
shape of :mod:`backend.tests.test_routers_shareable_objects`.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend import auth
from backend.routers import runbooks as router_module


def _operator(tenant_id: str = "t-default", user_id: str = "u-op") -> auth.User:
    return auth.User(
        id=user_id,
        email="operator@example.com",
        name="op",
        role="operator",
        tenant_id=tenant_id,
    )


def test_router_is_mounted_on_versioned_app() -> None:
    """The router is registered alongside the other versioned routers."""
    from backend import main as backend_main

    src = inspect.getsource(backend_main)
    assert "from backend.routers import runbooks" in src
    assert "_runbooks_router.router" in src


def test_router_prefix_and_role() -> None:
    """Prefix matches the spec; endpoints are operator-gated."""
    assert router_module.router.prefix == "/runbooks"
    save_src = inspect.getsource(router_module.save_from_block)
    exec_src = inspect.getsource(router_module.execute_runbook)
    list_src = inspect.getsource(router_module.list_effective_runbooks)
    for src in (save_src, exec_src, list_src):
        assert "Depends(_au.require_operator)" in src


# ─── /save-from-block ────────────────────────────────────────────


def _save_request(**overrides) -> router_module.SaveFromBlockRequest:
    block_kwargs: dict = dict(
        block_id="blk-abc123def456",
        tenant_id="t-default",
        user_id="u-op",
        kind="command",
        title="Probe USB {{ target_soc }}",
        payload={"command": "lsusb | grep {{ target_soc }}"},
    )
    block_kwargs.update(overrides.pop("block", {}))
    return router_module.SaveFromBlockRequest(
        block=router_module.BlockInput(**block_kwargs),
        **overrides,
    )


@pytest.mark.asyncio
async def test_save_from_block_writes_yaml_to_project_scope(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(router_module, "_project_root", lambda: tmp_path)
    req = _save_request()

    res = await router_module.save_from_block(req, _user=_operator())

    assert res["runbook"]["source_url"] == "omnisight://block/blk-abc123def456"
    # Inferred param from ``{{ target_soc }}`` placeholder.
    assert [p["name"] for p in res["runbook"]["params"]] == ["target_soc"]
    # File landed in project-scope dir, immediately discoverable.
    written = Path(res["path"])
    assert written.exists()
    assert written.parent == tmp_path / ".omnisight" / "runbooks"
    # YAML body included for UI confirmation preview.
    assert "name:" in res["yaml"]
    assert "lsusb" in res["yaml"]


@pytest.mark.asyncio
async def test_save_from_block_refuses_duplicate_without_overwrite(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(router_module, "_project_root", lambda: tmp_path)
    req = _save_request()

    await router_module.save_from_block(req, _user=_operator())
    with pytest.raises(HTTPException) as excinfo:
        await router_module.save_from_block(req, _user=_operator())
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_save_from_block_overwrite_succeeds(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(router_module, "_project_root", lambda: tmp_path)
    req = _save_request()
    await router_module.save_from_block(req, _user=_operator())

    req2 = _save_request(overwrite=True, description="updated desc")
    res = await router_module.save_from_block(req2, _user=_operator())
    assert "updated desc" in res["yaml"]


# ─── /effective ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_effective_includes_saved_runbook(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(router_module, "_project_root", lambda: tmp_path)
    await router_module.save_from_block(_save_request(), _user=_operator())

    res = await router_module.list_effective_runbooks(_user=_operator())
    assert res["count"] >= 1
    names = [r["name"] for r in res["items"]]
    assert any("probe-usb" in n for n in names)
    # Effective entries carry the scope label for the dashboard.
    saved = next(r for r in res["items"] if "probe-usb" in r["name"])
    assert saved["scope"] == "project"


# ─── /{name}/execute ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_runbook_returns_block_chain(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(router_module, "_project_root", lambda: tmp_path)
    saved = await router_module.save_from_block(_save_request(), _user=_operator())
    name = saved["runbook"]["name"]

    res = await router_module.execute_runbook(
        name,
        router_module.ExecuteRunbookRequest(
            params={"target_soc": "imx8mp"},
            tenant_id="t-default",
            user_id="u-op",
            parent_block_id="blk-abc123def456",
        ),
        _user=_operator(),
    )
    blocks = res["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["parent_id"] == "blk-abc123def456"
    assert blocks[0]["payload"]["command"] == "lsusb | grep imx8mp"
    assert blocks[0]["payload"]["runbook"]["params"] == {"target_soc": "imx8mp"}


@pytest.mark.asyncio
async def test_execute_runbook_404_when_unknown(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(router_module, "_project_root", lambda: tmp_path)
    with pytest.raises(HTTPException) as excinfo:
        await router_module.execute_runbook(
            "no-such-runbook",
            router_module.ExecuteRunbookRequest(
                params={}, tenant_id="t-default",
            ),
            _user=_operator(),
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_execute_runbook_422_lists_missing_params(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(router_module, "_project_root", lambda: tmp_path)
    saved = await router_module.save_from_block(_save_request(), _user=_operator())
    name = saved["runbook"]["name"]

    with pytest.raises(HTTPException) as excinfo:
        await router_module.execute_runbook(
            name,
            router_module.ExecuteRunbookRequest(
                params={},
                tenant_id="t-default",
            ),
            _user=_operator(),
        )
    assert excinfo.value.status_code == 422
    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert "target_soc" in detail["missing_params"]
