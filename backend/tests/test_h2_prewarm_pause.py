"""H2 row 1515 — sandbox_prewarm pauses new warm pool under host pressure.

Covers the pause-on-pressure invariant:
  * High CPU% blocks new warm-pool creation (reason ``host_cpu_high``)
  * High mem% blocks new warm-pool creation (reason ``host_mem_high``)
  * High container count blocks new warm-pool creation (reason ``container_cap``)
  * High loadavg ratio (WSL2 auxiliary) blocks (reason ``host_loadavg_high``)
  * Already-warmed slots are PRESERVED — not stopped, still consumable
  * Already-warmed slots matching candidates are returned even when paused
  * Clean host launches new containers as before
  * No snapshot (cold start) launches as before (fail-open)
  * Reason precedence: CPU → mem → container → loadavg
  * Pause emits ``sandbox.prewarm_paused`` SSE + bumps prewarm_paused_total
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from backend import host_metrics as hm
from backend import sandbox_prewarm as pw
from backend.dag_schema import DAG, Task


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures + helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    pw._reset_for_tests()
    hm._reset_for_tests()
    # Pin policy to "shared" so tenant_id=None lands in a predictable bucket.
    monkeypatch.setattr(
        "backend.config.settings.prewarm_policy", "shared", raising=False,
    )
    yield
    pw._reset_for_tests()
    hm._reset_for_tests()


def _t(task_id: str, *, depends_on=None) -> Task:
    return Task(
        task_id=task_id,
        description=f"t {task_id}",
        required_tier="t1",
        toolchain="cmake",
        inputs=[],
        expected_output=f"build/{task_id}.bin",
        depends_on=depends_on or [],
    )


def _dag(tasks: list[Task], dag_id: str = "REQ-pw-pause") -> DAG:
    return DAG(dag_id=dag_id, tasks=tasks)


@dataclass
class _FakeInfo:
    agent_id: str
    container_id: str = "cid-fake"


def _make_starter(store: dict[str, _FakeInfo]):
    async def starter(agent_id, workspace_path):
        info = _FakeInfo(agent_id=agent_id)
        store[agent_id] = info
        return info
    return starter


def _install_snapshot(*, cpu: float = 10.0, mem: float = 20.0,
                      containers: int = 2, loadavg_1m: float = 1.0
                      ) -> hm.HostSnapshot:
    """Install a single HostSnapshot in the host_metrics ring buffer."""
    now = time.time()
    host = hm.HostSample(
        cpu_percent=cpu,
        mem_percent=mem,
        mem_used_gb=mem * 0.64,
        mem_total_gb=64.0,
        disk_percent=10.0,
        disk_used_gb=51.2,
        disk_total_gb=512.0,
        loadavg_1m=loadavg_1m,
        loadavg_5m=loadavg_1m,
        loadavg_15m=loadavg_1m,
        sampled_at=now,
    )
    docker = hm.DockerSample(
        container_count=containers,
        total_mem_reservation_bytes=0,
        source="sdk",
        sampled_at=now,
    )
    snap = hm.HostSnapshot(host=host, docker=docker, sampled_at=now)
    hm._host_history.clear()
    hm._host_history.append(snap)
    return snap


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pressure detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_check_pressure_returns_none_on_clean_host():
    _install_snapshot(cpu=10.0, mem=10.0, containers=1, loadavg_1m=0.5)
    assert pw._check_host_pressure() is None


def test_check_pressure_returns_none_on_cold_start():
    """No snapshot in ring → fail-open."""
    assert pw._check_host_pressure() is None


def test_check_pressure_detects_cpu():
    _install_snapshot(cpu=90.0)
    assert pw._check_host_pressure() == pw.PREWARM_PAUSE_REASON_CPU


def test_check_pressure_detects_mem():
    _install_snapshot(cpu=10.0, mem=92.0)
    assert pw._check_host_pressure() == pw.PREWARM_PAUSE_REASON_MEM


def test_check_pressure_detects_container_cap(monkeypatch):
    monkeypatch.setattr("backend.decision_engine.H2_CONTAINER_CAP", 4)
    _install_snapshot(cpu=10.0, mem=10.0, containers=64)
    assert pw._check_host_pressure() == pw.PREWARM_PAUSE_REASON_CONTAINER


def test_check_pressure_detects_loadavg():
    """psutil clean but loadavg saturated (WSL2 case) — still high pressure."""
    # Baseline cpu_cores=16, ratio threshold 0.9 → loadavg_1m must exceed 14.4.
    _install_snapshot(cpu=10.0, mem=10.0, containers=2, loadavg_1m=15.0)
    assert pw._check_host_pressure() == pw.PREWARM_PAUSE_REASON_LOADAVG


def test_reason_precedence_cpu_beats_mem(monkeypatch):
    """CPU is checked before mem so a host hot on both reports CPU."""
    _install_snapshot(cpu=90.0, mem=92.0)
    assert pw._check_host_pressure() == pw.PREWARM_PAUSE_REASON_CPU


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  prewarm_for — pause behaviour
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_prewarm_skips_new_starts_under_pressure(tmp_path):
    """High pressure must mean zero starter invocations."""
    started: dict = {}
    _install_snapshot(cpu=95.0)
    slots = await pw.prewarm_for(
        _dag([_t("A"), _t("B")]),
        tmp_path, depth=2, starter=_make_starter(started),
    )
    assert started == {}, "starter must not be called under high pressure"
    assert slots == []
    assert pw.snapshot() == {}, "registry must remain empty"


@pytest.mark.asyncio
async def test_prewarm_preserves_already_warmed_slots(tmp_path):
    """Slots warmed BEFORE pressure rose stay in the registry, are
    consumable, and are returned by the pressure-paused call."""
    started: dict = {}
    # Warm A under clean host.
    _install_snapshot(cpu=10.0, mem=10.0, containers=1)
    pre = await pw.prewarm_for(
        _dag([_t("A")]),
        tmp_path, depth=1, starter=_make_starter(started),
    )
    assert len(pre) == 1
    assert "A" in pw.snapshot()

    # Pressure rises; subsequent prewarm_for must not start B but must
    # still return the existing A slot for the same DAG.
    _install_snapshot(cpu=92.0)
    started_after: dict = {}
    paused = await pw.prewarm_for(
        _dag([_t("A"), _t("B")]),
        tmp_path, depth=2, starter=_make_starter(started_after),
    )
    assert started_after == {}, "no new starts under pressure"
    # A still warm; B never started.
    assert {s.task_id for s in paused} == {"A"}
    assert "A" in pw.snapshot()
    assert "B" not in pw.snapshot()


@pytest.mark.asyncio
async def test_already_warmed_still_consumable_under_pressure(tmp_path):
    """The pause must NOT stop already-warmed containers — consume()
    still hits."""
    started: dict = {}
    _install_snapshot(cpu=10.0)
    await pw.prewarm_for(
        _dag([_t("A")]),
        tmp_path, depth=1, starter=_make_starter(started),
    )
    # Pressure rises.
    _install_snapshot(cpu=99.0)
    # consume() is independent of prewarm_for — must still return slot A.
    slot = await pw.consume("A")
    assert slot is not None
    assert slot.task_id == "A"


@pytest.mark.asyncio
async def test_clean_host_still_launches(tmp_path):
    """Smoke: a healthy snapshot doesn't break the existing path."""
    started: dict = {}
    _install_snapshot(cpu=20.0, mem=30.0, containers=2, loadavg_1m=1.0)
    slots = await pw.prewarm_for(
        _dag([_t("A"), _t("B")]),
        tmp_path, depth=2, starter=_make_starter(started),
    )
    assert {s.task_id for s in slots} == {"A", "B"}
    assert len(started) == 2


@pytest.mark.asyncio
async def test_cold_start_no_snapshot_launches(tmp_path):
    """No snapshot in ring buffer → fail-open, launches as usual."""
    started: dict = {}
    # Ensure ring is empty — _reset fixture already cleared it, but be explicit.
    hm._host_history.clear()
    slots = await pw.prewarm_for(
        _dag([_t("A")]),
        tmp_path, depth=1, starter=_make_starter(started),
    )
    assert len(slots) == 1
    assert len(started) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit / SSE / metric emit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_pause_emits_sse_event(tmp_path):
    from backend.events import bus
    captured: list[tuple[str, dict]] = []

    original_publish = bus.publish

    def _spy(event, data, **kw):
        captured.append((event, dict(data)))
        return original_publish(event, data, **kw)

    bus.publish = _spy  # type: ignore[assignment]
    try:
        _install_snapshot(cpu=95.0)
        await pw.prewarm_for(
            _dag([_t("A")]),
            tmp_path, depth=1, starter=_make_starter({}),
        )
    finally:
        bus.publish = original_publish  # type: ignore[assignment]

    paused_events = [d for ev, d in captured if ev == "sandbox.prewarm_paused"]
    assert len(paused_events) == 1
    assert paused_events[0]["reason"] == pw.PREWARM_PAUSE_REASON_CPU
    assert paused_events[0]["dag_id"] == "REQ-pw-pause"
    assert paused_events[0]["candidate_count"] == 1


@pytest.mark.asyncio
async def test_pause_increments_prom_metric(tmp_path):
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()

    _install_snapshot(cpu=95.0)
    await pw.prewarm_for(
        _dag([_t("A"), _t("B")]),
        tmp_path, depth=2, starter=_make_starter({}),
    )

    samples = {s.labels.get("reason"): s.value
               for s in m.prewarm_paused_total.collect()[0].samples
               if s.name.endswith("_total")}
    assert samples.get(pw.PREWARM_PAUSE_REASON_CPU) == 1


@pytest.mark.asyncio
async def test_pause_does_not_bump_started_metric(tmp_path):
    """A paused call must not pretend it started something."""
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()

    _install_snapshot(cpu=99.0)
    await pw.prewarm_for(
        _dag([_t("A")]),
        tmp_path, depth=1, starter=_make_starter({}),
    )
    started_total = sum(
        s.value for s in m.prewarm_started_total.collect()[0].samples
        if s.name.endswith("_total")
    )
    assert started_total == 0
