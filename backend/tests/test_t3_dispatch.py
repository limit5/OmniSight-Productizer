"""Phase 64-C-LOCAL S2 — dispatch_t3 routing.

We don't spin up real Docker here (CI image doesn't have the
privileged runtime available); we mock out start_t3_local_container
and just verify the dispatcher:
  * consults the resolver
  * bumps the metric
  * calls start_t3_local_container ONLY on a LOCAL match
  * returns (None, kind) for non-local kinds
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import container as _ct
from backend import t3_resolver as _r


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture(autouse=True)
def _host_x86_64_linux(monkeypatch):
    """Pin host → x86_64/linux for every test so the outcome isn't
    coupled to whatever arch CI happens to run on."""
    monkeypatch.setattr(_r, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(_r, "host_os", lambda: "linux")


@pytest.fixture
def fake_info():
    """Minimal ContainerInfo stub returned by the patched starter."""
    return _ct.ContainerInfo(
        agent_id="a-test",
        container_id="abc123abc123",
        container_name="omnisight-agent-a-test",
        workspace_path=Path("/tmp/fake-ws"),
        image="omnisight-agent:test",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  dispatch_t3 — routing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_local_dispatch_calls_local_starter(monkeypatch, fake_info):
    called = {"n": 0}

    async def fake_starter(agent_id, workspace_path, **kwargs):
        called["n"] += 1
        called["agent_id"] = agent_id
        called["workspace_path"] = workspace_path
        called["kwargs"] = kwargs
        return fake_info

    monkeypatch.setattr(_ct, "start_t3_local_container", fake_starter)

    info, kind = await _ct.dispatch_t3(
        "a-test", Path("/tmp/ws"), target_arch="x86_64", target_os="linux",
    )
    assert kind == _r.T3RunnerKind.LOCAL
    assert info is fake_info
    assert called["n"] == 1
    assert called["agent_id"] == "a-test"


@pytest.mark.asyncio
async def test_bundle_dispatch_returns_none(monkeypatch, fake_info):
    """aarch64 target on an x86_64 host → BUNDLE, no container start."""
    called = {"n": 0}

    async def never(agent_id, workspace_path, **kwargs):
        called["n"] += 1
        return fake_info

    monkeypatch.setattr(_ct, "start_t3_local_container", never)

    info, kind = await _ct.dispatch_t3(
        "a-test", Path("/tmp/ws"), target_arch="aarch64", target_os="linux",
    )
    assert kind == _r.T3RunnerKind.BUNDLE
    assert info is None
    assert called["n"] == 0, "Must not start a container when resolver says BUNDLE"


@pytest.mark.asyncio
async def test_empty_target_bundles_instead_of_local(monkeypatch, fake_info):
    """An under-specified DAG must never silently get LOCAL just
    because nobody said otherwise."""
    called = {"n": 0}

    async def never(agent_id, workspace_path, **kwargs):
        called["n"] += 1
        return fake_info

    monkeypatch.setattr(_ct, "start_t3_local_container", never)

    info, kind = await _ct.dispatch_t3("a-test", Path("/tmp/ws"))
    assert kind == _r.T3RunnerKind.BUNDLE
    assert info is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_kill_switch_forces_bundle(monkeypatch, fake_info):
    """OMNISIGHT_T3_LOCAL_ENABLED=false must override a matching host."""
    monkeypatch.setenv("OMNISIGHT_T3_LOCAL_ENABLED", "false")
    called = {"n": 0}

    async def never(agent_id, workspace_path, **kwargs):
        called["n"] += 1
        return fake_info

    monkeypatch.setattr(_ct, "start_t3_local_container", never)

    info, kind = await _ct.dispatch_t3(
        "a-test", Path("/tmp/ws"), target_arch="x86_64", target_os="linux",
    )
    assert kind == _r.T3RunnerKind.BUNDLE
    assert info is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_dispatch_bumps_metric(monkeypatch, fake_info):
    """Every dispatch (LOCAL or BUNDLE) must record exactly one metric bump."""
    bumped: list[str] = []

    def fake_record(kind):
        bumped.append(kind.value)

    monkeypatch.setattr(_r, "record_dispatch", fake_record)

    async def fake_starter(agent_id, workspace_path, **kwargs):
        return fake_info
    monkeypatch.setattr(_ct, "start_t3_local_container", fake_starter)

    await _ct.dispatch_t3("a1", Path("/tmp/ws"), target_arch="x86_64", target_os="linux")
    await _ct.dispatch_t3("a2", Path("/tmp/ws"), target_arch="aarch64", target_os="linux")
    assert bumped == ["local", "bundle"]
