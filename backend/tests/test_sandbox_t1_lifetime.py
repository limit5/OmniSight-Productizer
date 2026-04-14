"""Phase 64-A S4 — sandbox lifetime killswitch.

Exercises `_lifetime_killswitch` in isolation (no real docker run) and
verifies the integration points: container removed, registry popped,
metric incremented, audit row written, status flipped to
`killed_lifetime`. Also covers the cancel path so a normal stop_container
doesn't trip the killer.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from backend import container as ct


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
async def db_for_audit(monkeypatch):
    """audit.log writes to the DB; give it an empty temp DB."""
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


@pytest.fixture(autouse=True)
def _reset_registry():
    ct._containers.clear()
    yield
    ct._containers.clear()


def _seed_info(agent_id: str = "a-life") -> ct.ContainerInfo:
    info = ct.ContainerInfo(
        agent_id=agent_id,
        container_id="abc123",
        container_name=f"omnisight-agent-{agent_id}",
        workspace_path=__import__("pathlib").Path("/tmp"),
        image="omnisight-agent:test",
    )
    ct._containers[agent_id] = info
    return info


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Killswitch fires
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_lifetime_kills_container_after_cap(db_for_audit, monkeypatch):
    info = _seed_info("a-fired")
    rm_calls: list[str] = []

    async def fake_run(cmd, timeout=10):
        if "docker rm" in cmd:
            rm_calls.append(cmd)
        return (0, "", "")
    monkeypatch.setattr(ct, "_run", fake_run)

    await ct._lifetime_killswitch(
        "a-fired", info.container_name, lifetime_s=0.05, tier="t1",
    )

    assert any(info.container_name in c for c in rm_calls)
    assert "a-fired" not in ct._containers
    assert info.status == "killed_lifetime"

    from backend import audit as _a
    rows = await _a.query(actor="system:lifetime-watchdog", limit=10)
    assert any(r.get("action") == "sandbox_killed" for r in rows)


@pytest.mark.asyncio
async def test_lifetime_cancellation_is_quiet(monkeypatch):
    """If the agent finishes naturally, stop_container cancels the
    watchdog — it must NOT then go on to nuke the (now-recycled)
    container of the same name."""
    info = _seed_info("a-cancelled")
    rm_calls: list[str] = []

    async def fake_run(cmd, timeout=10):
        rm_calls.append(cmd)
        return (0, "", "")
    monkeypatch.setattr(ct, "_run", fake_run)

    task = asyncio.create_task(
        ct._lifetime_killswitch("a-cancelled", info.container_name, 30.0)
    )
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # No docker rm should have fired.
    assert not any("docker rm" in c for c in rm_calls)
    # Registry untouched.
    assert "a-cancelled" in ct._containers
    assert info.status == "running"


@pytest.mark.asyncio
async def test_killswitch_skips_when_container_already_replaced(monkeypatch):
    info = _seed_info("a-replaced")
    # Replace the registered container_name behind the watchdog's back.
    info.container_name = "different-name"

    rm_calls: list[str] = []
    async def fake_run(cmd, timeout=10):
        rm_calls.append(cmd)
        return (0, "", "")
    monkeypatch.setattr(ct, "_run", fake_run)

    await ct._lifetime_killswitch(
        "a-replaced", "omnisight-agent-a-replaced", lifetime_s=0.01,
    )
    assert not rm_calls  # skipped: name mismatch


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Settings / disable path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_default_lifetime_is_2700_seconds():
    from backend.config import Settings
    assert Settings().sandbox_lifetime_s == 2700


def test_container_info_has_lifetime_task_slot():
    """The dataclass must carry the watchdog handle so stop_container
    can cancel it. Pin the contract."""
    info = ct.ContainerInfo(
        agent_id="x", container_id="y",
        container_name="z", workspace_path=__import__("pathlib").Path("/tmp"),
        image="i",
    )
    assert hasattr(info, "lifetime_task")
    assert info.lifetime_task is None  # default


@pytest.mark.asyncio
async def test_stop_container_cancels_lifetime_task(monkeypatch):
    """Verify stop_container cancels the watchdog so it doesn't fire
    against a recycled name."""
    info = _seed_info("a-stop")

    async def fake_run(cmd, timeout=15):
        return (0, "", "")
    monkeypatch.setattr(ct, "_run", fake_run)

    cancelled = {"flag": False}

    async def long_sleep():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled["flag"] = True
            raise

    info.lifetime_task = asyncio.create_task(long_sleep())
    await asyncio.sleep(0.01)
    await ct.stop_container("a-stop")
    await asyncio.sleep(0.01)
    assert cancelled["flag"] is True
    assert "a-stop" not in ct._containers
