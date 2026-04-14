"""Phase 64-B — Tier 2 networked sandbox."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from backend import container as ct, sandbox_net as sn


@pytest.fixture(autouse=True)
def _reset():
    sn._reset_dns_cache_for_tests()
    ct._containers.clear()
    ct._reset_runtime_cache_for_tests()
    yield
    ct._containers.clear()


@pytest.fixture()
async def db_for_audit(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as cfg
        cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        try:
            yield db
        finally:
            await db.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  sandbox_net helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_t2_network_name_distinct_from_t1():
    assert sn.T2_NETWORK_NAME != sn.T1_NETWORK_NAME
    assert sn.T2_NETWORK_NAME == "omnisight-egress-t2"


@pytest.mark.asyncio
async def test_ensure_t2_network_creates_when_missing():
    calls: list[str] = []
    async def fake_runner(cmd, timeout=10):
        calls.append(cmd)
        if "network ls" in cmd:
            return (0, "", "")
        if "network create" in cmd:
            return (0, "ok", "")
        return (0, "", "")
    name = await sn.ensure_t2_network(runner=fake_runner)
    assert name == sn.T2_NETWORK_NAME
    assert any("network create" in c and sn.T2_NETWORK_NAME in c for c in calls)


@pytest.mark.asyncio
async def test_ensure_t2_network_skips_when_present():
    async def fake_runner(cmd, timeout=10):
        if "network ls" in cmd:
            return (0, sn.T2_NETWORK_NAME, "")
        if "network create" in cmd:
            pytest.fail("create called when bridge already exists")
        return (0, "", "")
    await sn.ensure_t2_network(runner=fake_runner)


@pytest.mark.asyncio
async def test_resolve_t2_network_arg_returns_bridge():
    async def fake_runner(cmd, timeout=10):
        if "network ls" in cmd:
            return (0, sn.T2_NETWORK_NAME, "")
        return (0, "", "")
    arg = await sn.resolve_t2_network_arg(runner=fake_runner)
    assert arg == f"--network {sn.T2_NETWORK_NAME}"


@pytest.mark.asyncio
async def test_resolve_t2_raises_when_bridge_unavailable():
    async def fake_runner(cmd, timeout=10):
        if "network ls" in cmd:
            return (0, "", "")
        if "network create" in cmd:
            return (1, "", "permission denied")
        return (0, "", "")
    with pytest.raises(RuntimeError):
        await sn.resolve_t2_network_arg(runner=fake_runner)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  start_container(tier="networked") integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _install_docker_stubs(monkeypatch):
    seen: dict[str, str] = {"docker_run_cmd": ""}

    async def fake_run(cmd, timeout=60):
        if "docker info" in cmd:
            return (0, json.dumps({"runc": {"path": "runc"}}), "")
        if "network ls" in cmd:
            return (0, sn.T2_NETWORK_NAME, "")
        if "image inspect" in cmd:
            return (0, "sha256:" + "a" * 64, "")
        if "docker run" in cmd:
            seen["docker_run_cmd"] = cmd
            return (0, "abcdef123456", "")
        if "docker exec" in cmd:
            return (0, "", "")
        if "docker rm" in cmd or "docker stop" in cmd:
            return (0, "", "")
        return (0, "", "")

    async def fake_ensure_image():
        return True

    monkeypatch.setattr(ct, "_run", fake_run)
    monkeypatch.setattr(ct, "ensure_image", fake_ensure_image)
    return seen


@pytest.mark.asyncio
async def test_start_networked_container_uses_t2_bridge(monkeypatch):
    seen = _install_docker_stubs(monkeypatch)
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests", "", raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.sandbox_lifetime_s", 0, raising=False,
    )
    info = await ct.start_networked_container("a-net", Path("/tmp"))
    try:
        assert info.container_id == "abcdef123456"
        assert "--network omnisight-egress-t2" in seen["docker_run_cmd"]
        assert "--network none" not in seen["docker_run_cmd"]
    finally:
        ct._containers.pop("a-net", None)


@pytest.mark.asyncio
async def test_t1_default_still_uses_none(monkeypatch):
    seen = _install_docker_stubs(monkeypatch)
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests", "", raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.sandbox_lifetime_s", 0, raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.t1_allow_egress", False, raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.t1_egress_allow_hosts", "", raising=False,
    )
    info = await ct.start_container("a-default", Path("/tmp"))
    try:
        assert "--network none" in seen["docker_run_cmd"]
        assert "omnisight-egress-t2" not in seen["docker_run_cmd"]
    finally:
        ct._containers.pop("a-default", None)


@pytest.mark.asyncio
async def test_networked_launch_metric_carries_tier_t2(db_for_audit, monkeypatch):
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()

    _install_docker_stubs(monkeypatch)
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests", "", raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.sandbox_lifetime_s", 0, raising=False,
    )

    await ct.start_networked_container("a-net-metric", Path("/tmp"))
    ct._containers.pop("a-net-metric", None)

    samples = list(m.sandbox_launch_total.collect()[0].samples)
    t2_success = [
        s for s in samples
        if s.labels.get("tier") == "networked"
        and s.labels.get("result") == "success"
        and s.name.endswith("_total")
    ]
    assert t2_success and t2_success[0].value >= 1


@pytest.mark.asyncio
async def test_networked_launch_audit_records_tier(db_for_audit, monkeypatch):
    _install_docker_stubs(monkeypatch)
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests", "", raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.sandbox_lifetime_s", 0, raising=False,
    )

    await ct.start_networked_container("a-audit", Path("/tmp"))
    ct._containers.pop("a-audit", None)

    from backend import audit as _a
    rows = await _a.query(actor="agent:a-audit", limit=10)
    launched = [r for r in rows if r.get("action") == "sandbox_launched"]
    assert launched
    after = launched[0].get("after")
    if isinstance(after, str):
        import json
        after = json.loads(after)
    assert after.get("tier") == "networked"
    assert after.get("network") == sn.T2_NETWORK_NAME
