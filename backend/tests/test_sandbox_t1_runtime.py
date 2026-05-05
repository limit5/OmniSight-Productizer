"""Phase 64-A S1 — runtime probe + selection.

These tests exercise `resolve_runtime` without launching real docker —
`_run` is monkey-patched to return whatever `docker info` output we
want for the case under test.
"""

from __future__ import annotations

import json

import pytest

from backend import container as ct


@pytest.fixture(autouse=True)
def _reset():
    ct._reset_runtime_cache_for_tests()
    yield
    ct._reset_runtime_cache_for_tests()


def _patch_docker_info(monkeypatch, runtimes_payload: str | None, rc: int = 0):
    async def fake_run(cmd: str, timeout: int = 60):
        if "docker info" in cmd:
            return (rc, runtimes_payload or "", "")
        # Default for any other docker call in the path.
        return (0, "ok", "")
    monkeypatch.setattr(ct, "_run", fake_run)


@pytest.mark.asyncio
async def test_uses_runsc_when_available(monkeypatch):
    monkeypatch.setattr(ct, "_run", None)  # ensure stub takes over
    _patch_docker_info(monkeypatch, json.dumps({
        "runc": {"path": "runc"}, "runsc": {"path": "runsc"},
    }))
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runsc", raising=False,
    )
    rt = await ct.resolve_runtime()
    assert rt == "runsc"


@pytest.mark.asyncio
async def test_falls_back_to_runc_when_runsc_missing(monkeypatch, caplog):
    import logging
    _patch_docker_info(monkeypatch, json.dumps({"runc": {"path": "runc"}}))
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runsc", raising=False,
    )
    caplog.set_level(logging.WARNING, logger="backend.container")
    rt = await ct.resolve_runtime()
    assert rt == "runc"
    assert any("falling back to runc" in rec.message for rec in caplog.records), \
        "expected user-visible downgrade warning"


@pytest.mark.asyncio
async def test_runc_preference_short_circuits_probe(monkeypatch):
    _patch_docker_info(monkeypatch, json.dumps({
        "runc": {"path": "runc"}, "runsc": {"path": "runsc"},
    }))
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runc", raising=False,
    )
    rt = await ct.resolve_runtime()
    assert rt == "runc"


@pytest.mark.asyncio
async def test_unknown_runtime_value_warns_and_uses_runc(monkeypatch, caplog):
    import logging
    _patch_docker_info(monkeypatch, json.dumps({"runc": {"path": "runc"}}))
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "kata-containers", raising=False,
    )
    caplog.set_level(logging.WARNING, logger="backend.container")
    rt = await ct.resolve_runtime()
    assert rt == "runc"
    assert any("not in {runsc,runc}" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_docker_info_failure_defaults_to_runc(monkeypatch):
    # rc != 0 → probe gives up and assumes only runc is available.
    _patch_docker_info(monkeypatch, "", rc=1)
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runsc", raising=False,
    )
    rt = await ct.resolve_runtime()
    assert rt == "runc"


@pytest.mark.asyncio
async def test_production_requires_runsc_preference(monkeypatch):
    _patch_docker_info(monkeypatch, json.dumps({
        "runc": {"path": "runc"}, "runsc": {"path": "runsc"},
    }))
    monkeypatch.setattr("backend.config.settings.env", "production", raising=False)
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runc", raising=False,
    )

    with pytest.raises(RuntimeError, match="requires OMNISIGHT_DOCKER_RUNTIME=runsc"):
        await ct.resolve_runtime()


@pytest.mark.asyncio
async def test_production_requires_runsc_registered(monkeypatch):
    _patch_docker_info(monkeypatch, json.dumps({"runc": {"path": "runc"}}))
    monkeypatch.setattr("backend.config.settings.env", "production", raising=False)
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runsc", raising=False,
    )

    with pytest.raises(RuntimeError, match="requires gVisor runsc registered"):
        await ct.resolve_runtime()


@pytest.mark.asyncio
async def test_production_rejects_cached_runc(monkeypatch):
    _patch_docker_info(monkeypatch, json.dumps({"runc": {"path": "runc"}}))
    monkeypatch.setattr("backend.config.settings.docker_runtime", "runc", raising=False)
    assert await ct.resolve_runtime() == "runc"

    monkeypatch.setattr("backend.config.settings.env", "production", raising=False)
    with pytest.raises(RuntimeError, match="cached sandbox runtime"):
        await ct.resolve_runtime()


@pytest.mark.asyncio
async def test_resolve_runtime_is_cached(monkeypatch):
    calls = {"n": 0}
    async def counting_run(cmd: str, timeout: int = 60):
        if "docker info" in cmd:
            calls["n"] += 1
        return (0, json.dumps({"runc": {"path": "runc"}, "runsc": {"path": "runsc"}}), "")
    monkeypatch.setattr(ct, "_run", counting_run)
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runsc", raising=False,
    )
    await ct.resolve_runtime()
    await ct.resolve_runtime()
    await ct.resolve_runtime()
    assert calls["n"] == 1, "expected docker info to be probed exactly once"


@pytest.mark.asyncio
async def test_force_redetect_reprobes(monkeypatch):
    calls = {"n": 0}
    async def counting_run(cmd: str, timeout: int = 60):
        if "docker info" in cmd:
            calls["n"] += 1
        return (0, json.dumps({"runc": {"path": "runc"}}), "")
    monkeypatch.setattr(ct, "_run", counting_run)
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runsc", raising=False,
    )
    await ct.resolve_runtime()
    await ct.resolve_runtime(force_redetect=True)
    assert calls["n"] == 2
