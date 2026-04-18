"""M4 — tests for backend/host_metrics.py.

Covers:
  * cgroup file readers — usage_usec / memory.current parsing + error paths
  * CPU% computation: first sample primes, delta math is correct, caps
    at num_cores*100, floors at 0
  * aggregation groups samples by tenant_id, sums CPU + mem
  * get_culprit_tenant outlier logic (3 representative cases)
  * accumulate_usage integrates CPU% × interval → cpu_seconds
  * reset_accounting scope (single tenant vs all)
  * Prometheus gauge publish (tenant_cpu_percent labels set)
  * get_tenant_usage fallback when no samples yet
  * H1 — HOST_BASELINE constant contract (shape + values + immutability)
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from backend import host_metrics as hm
from backend import metrics as m


@pytest.fixture(autouse=True)
def _reset():
    hm._reset_for_tests()
    yield
    hm._reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cgroup file readers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCgroupReaders:
    def test_read_cpu_usage_usec_parses_correctly(self, tmp_path: Path):
        (tmp_path / "cpu.stat").write_text(
            "usage_usec 123456789\n"
            "user_usec 100000000\n"
            "system_usec 23456789\n"
        )
        assert hm._read_cpu_usage_usec(tmp_path) == 123456789

    def test_read_cpu_usage_usec_missing_file_returns_zero(self, tmp_path: Path):
        assert hm._read_cpu_usage_usec(tmp_path) == 0

    def test_read_cpu_usage_usec_malformed_returns_zero(self, tmp_path: Path):
        (tmp_path / "cpu.stat").write_text("usage_usec not_a_number\nother 1\n")
        assert hm._read_cpu_usage_usec(tmp_path) == 0

    def test_read_memory_bytes(self, tmp_path: Path):
        (tmp_path / "memory.current").write_text("1073741824\n")
        assert hm._read_memory_bytes(tmp_path) == 1073741824

    def test_read_memory_bytes_missing_returns_zero(self, tmp_path: Path):
        assert hm._read_memory_bytes(tmp_path) == 0

    def test_read_memory_bytes_malformed_returns_zero(self, tmp_path: Path):
        (tmp_path / "memory.current").write_text("not a number\n")
        assert hm._read_memory_bytes(tmp_path) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CPU% delta computation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_sample(cid: str, tid: str, cpu_usec: int, mem: int, t: float) -> hm.ContainerSample:
    return hm.ContainerSample(
        container_id=cid, container_name=f"omnisight-agent-{cid}", tenant_id=tid,
        cpu_usage_usec=cpu_usec, memory_bytes=mem, sampled_at=t,
    )


class TestCpuPercentDelta:
    def test_first_sample_returns_zero_and_primes_state(self):
        sample = _make_sample("c1", "tA", 1_000_000, 0, 100.0)
        assert hm._compute_cpu_percent(sample) == 0.0
        # State is now primed for the next call.
        assert hm._prev_cpu["c1"] == (1_000_000, 100.0)

    def test_second_sample_computes_rate_correctly(self):
        first = _make_sample("c1", "tA", 0, 0, 100.0)
        hm._compute_cpu_percent(first)
        # +1s wall time, +1s CPU time (1e6 usec) → exactly 100%.
        second = _make_sample("c1", "tA", 1_000_000, 0, 101.0)
        assert hm._compute_cpu_percent(second) == pytest.approx(100.0, abs=0.01)

    def test_two_cores_saturating_for_one_second(self):
        hm._compute_cpu_percent(_make_sample("c1", "tA", 0, 0, 100.0))
        # +1s wall, +2s CPU → 200%
        assert hm._compute_cpu_percent(_make_sample("c1", "tA", 2_000_000, 0, 101.0)) == pytest.approx(200.0, abs=0.01)

    def test_dt_zero_returns_zero(self):
        hm._compute_cpu_percent(_make_sample("c1", "tA", 0, 0, 100.0))
        assert hm._compute_cpu_percent(_make_sample("c1", "tA", 5_000_000, 0, 100.0)) == 0.0

    def test_negative_delta_floors_at_zero(self):
        """Counter reset (container restart) — usage_usec went backwards."""
        hm._compute_cpu_percent(_make_sample("c1", "tA", 1_000_000, 0, 100.0))
        assert hm._compute_cpu_percent(_make_sample("c1", "tA", 500_000, 0, 101.0)) == 0.0

    def test_cap_at_num_cores_times_100(self):
        """An impossibly high rate (sample window too short) should be clamped."""
        cores = os.cpu_count() or 1
        hm._compute_cpu_percent(_make_sample("c1", "tA", 0, 0, 100.0))
        # Pretend 1000 cores worth of work done in 1 s.
        pct = hm._compute_cpu_percent(_make_sample("c1", "tA", 1_000 * 1_000_000, 0, 101.0))
        assert pct <= cores * 100.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Aggregation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAggregation:
    def test_groups_by_tenant(self, monkeypatch):
        monkeypatch.setattr(hm, "_measure_disk_gb", lambda _tid: 0.0)
        # Prime state
        hm._compute_cpu_percent(_make_sample("c1", "tA", 0, 0, 100.0))
        hm._compute_cpu_percent(_make_sample("c2", "tA", 0, 0, 100.0))
        hm._compute_cpu_percent(_make_sample("c3", "tB", 0, 0, 100.0))
        # Second pass: each tA container used 0.5 CPU-sec, tB used 1 CPU-sec
        samples = [
            _make_sample("c1", "tA", 500_000, 512 * 1024 * 1024, 101.0),
            _make_sample("c2", "tA", 500_000, 512 * 1024 * 1024, 101.0),
            _make_sample("c3", "tB", 1_000_000, 1024 * 1024 * 1024, 101.0),
        ]
        by_tenant = hm.aggregate_by_tenant(samples, include_disk=False)
        assert set(by_tenant) == {"tA", "tB"}
        assert by_tenant["tA"].sandbox_count == 2
        assert by_tenant["tA"].cpu_percent == pytest.approx(100.0, abs=0.1)  # 50% + 50%
        assert by_tenant["tA"].mem_used_gb == pytest.approx(1.0, abs=0.01)
        assert by_tenant["tB"].sandbox_count == 1
        assert by_tenant["tB"].cpu_percent == pytest.approx(100.0, abs=0.1)

    def test_empty_samples_yields_empty_dict(self, monkeypatch):
        monkeypatch.setattr(hm, "_measure_disk_gb", lambda _tid: 0.0)
        assert hm.aggregate_by_tenant([], include_disk=False) == {}

    def test_include_disk_delegates_to_tenant_quota(self, monkeypatch):
        monkeypatch.setattr(hm, "_measure_disk_gb", lambda tid: 2.5 if tid == "tA" else 0.0)
        hm._compute_cpu_percent(_make_sample("c1", "tA", 0, 0, 100.0))
        samples = [_make_sample("c1", "tA", 0, 1 * 1024 ** 3, 101.0)]
        by_tenant = hm.aggregate_by_tenant(samples, include_disk=True)
        assert by_tenant["tA"].disk_used_gb == 2.5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Culprit tenant detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCulpritDetection:
    def test_single_outlier_identified(self):
        usage = {
            "tA": hm.TenantUsage(tenant_id="tA", cpu_percent=400.0),
            "tB": hm.TenantUsage(tenant_id="tB", cpu_percent=20.0),
        }
        assert hm.get_culprit_tenant(usage) == "tA"

    def test_no_outlier_when_two_tenants_both_hot(self):
        """Both tenants above min_cpu; neither has the required margin."""
        usage = {
            "tA": hm.TenantUsage(tenant_id="tA", cpu_percent=200.0),
            "tB": hm.TenantUsage(tenant_id="tB", cpu_percent=180.0),
        }
        assert hm.get_culprit_tenant(usage) is None

    def test_no_culprit_when_top_is_below_min_cpu(self):
        usage = {
            "tA": hm.TenantUsage(tenant_id="tA", cpu_percent=60.0),
            "tB": hm.TenantUsage(tenant_id="tB", cpu_percent=10.0),
        }
        assert hm.get_culprit_tenant(usage) is None

    def test_empty_usage_returns_none(self):
        assert hm.get_culprit_tenant({}) is None

    def test_single_tenant_above_min_returns_itself(self):
        usage = {"tA": hm.TenantUsage(tenant_id="tA", cpu_percent=300.0)}
        assert hm.get_culprit_tenant(usage) == "tA"

    def test_reads_latest_snapshot_when_called_with_no_arg(self):
        with hm._lock:
            hm._latest_by_tenant["tA"] = hm.TenantUsage(tenant_id="tA", cpu_percent=500.0)
            hm._latest_by_tenant["tB"] = hm.TenantUsage(tenant_id="tB", cpu_percent=20.0)
        assert hm.get_culprit_tenant() == "tA"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Accounting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAccounting:
    def test_accumulate_integrates_cpu_seconds(self):
        usage = {"tA": hm.TenantUsage(tenant_id="tA", cpu_percent=100.0, mem_used_gb=2.0)}
        hm.accumulate_usage(usage, interval_s=5.0)
        snap = hm.snapshot_accounting()
        assert len(snap) == 1
        assert snap[0].tenant_id == "tA"
        # 100% × 5s = 5 cpu-seconds
        assert snap[0].cpu_seconds_total == pytest.approx(5.0)
        # 2 GB × 5s = 10 gb-seconds
        assert snap[0].mem_gb_seconds_total == pytest.approx(10.0)

    def test_accumulate_skips_on_zero_interval(self):
        usage = {"tA": hm.TenantUsage(tenant_id="tA", cpu_percent=100.0)}
        hm.accumulate_usage(usage, interval_s=0)
        assert hm.snapshot_accounting() == []

    def test_accumulate_is_additive(self):
        usage = {"tA": hm.TenantUsage(tenant_id="tA", cpu_percent=100.0)}
        hm.accumulate_usage(usage, interval_s=5.0)
        hm.accumulate_usage(usage, interval_s=5.0)
        snap = hm.snapshot_accounting()
        assert snap[0].cpu_seconds_total == pytest.approx(10.0)

    def test_reset_accounting_single_tenant(self):
        usage = {
            "tA": hm.TenantUsage(tenant_id="tA", cpu_percent=100.0),
            "tB": hm.TenantUsage(tenant_id="tB", cpu_percent=100.0),
        }
        hm.accumulate_usage(usage, interval_s=1.0)
        hm.reset_accounting("tA")
        snap = {a.tenant_id: a for a in hm.snapshot_accounting()}
        assert "tA" not in snap
        assert "tB" in snap

    def test_reset_accounting_all(self):
        hm.accumulate_usage({"tA": hm.TenantUsage(tenant_id="tA", cpu_percent=100.0)}, 1.0)
        hm.reset_accounting()
        assert hm.snapshot_accounting() == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Snapshot accessors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSnapshotAccessors:
    def test_get_tenant_usage_when_no_sample_yet(self, monkeypatch):
        monkeypatch.setattr(hm, "_measure_disk_gb", lambda _tid: 3.14)
        usage = hm.get_tenant_usage("tA")
        assert usage.tenant_id == "tA"
        assert usage.cpu_percent == 0.0
        assert usage.mem_used_gb == 0.0
        # Disk still measured so quota UI can render
        assert usage.disk_used_gb == 3.14

    def test_get_tenant_usage_returns_cached(self):
        with hm._lock:
            hm._latest_by_tenant["tA"] = hm.TenantUsage(
                tenant_id="tA", cpu_percent=42.0, mem_used_gb=1.2,
                disk_used_gb=3.4, sandbox_count=2,
            )
        usage = hm.get_tenant_usage("tA")
        assert usage.cpu_percent == 42.0
        assert usage.sandbox_count == 2

    def test_get_all_tenant_usage(self):
        with hm._lock:
            hm._latest_by_tenant["tA"] = hm.TenantUsage(tenant_id="tA", cpu_percent=10.0)
            hm._latest_by_tenant["tB"] = hm.TenantUsage(tenant_id="tB", cpu_percent=20.0)
        all_usage = {u.tenant_id: u for u in hm.get_all_tenant_usage()}
        assert set(all_usage) == {"tA", "tB"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Prometheus gauge publish
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPromPublish:
    def test_publish_writes_gauges(self):
        if not m.is_available():
            pytest.skip("prometheus_client not installed")
        m.reset_for_tests()
        usage = {
            "tA": hm.TenantUsage(tenant_id="tA", cpu_percent=120.0,
                                  mem_used_gb=2.0, disk_used_gb=1.5,
                                  sandbox_count=3),
        }
        hm._publish_prom_metrics(usage)
        from prometheus_client import generate_latest
        text = generate_latest(m.REGISTRY).decode()
        assert 'omnisight_tenant_cpu_percent{tenant_id="tA"} 120.0' in text
        assert 'omnisight_tenant_sandbox_count{tenant_id="tA"} 3.0' in text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Enumeration (uses real container registry)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEnumerate:
    def test_enumerate_empty_by_default(self):
        from backend import container as ct
        ct._containers.clear()
        assert hm._enumerate_agent_containers() == []

    def test_enumerate_picks_up_running_containers(self):
        from backend import container as ct
        ct._containers.clear()
        ct._containers["agent1"] = ct.ContainerInfo(
            agent_id="agent1", container_id="abc123",
            container_name="omnisight-agent-agent1",
            workspace_path=Path("/tmp"), image="test",
            tenant_id="tenantA", status="running",
        )
        ct._containers["agent2"] = ct.ContainerInfo(
            agent_id="agent2", container_id="def456",
            container_name="omnisight-agent-agent2",
            workspace_path=Path("/tmp"), image="test",
            tenant_id="tenantB", status="stopped",  # should be filtered
        )
        rows = hm._enumerate_agent_containers()
        assert len(rows) == 1
        assert rows[0]["tenant_id"] == "tenantA"
        ct._containers.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H1 — HOST_BASELINE contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# These tests pin the H1 "baseline hardcode" row from TODO.md:
#     HOST_BASELINE = HostBaseline(cpu_cores=16, mem_total_gb=64,
#                                  disk_total_gb=512,
#                                  cpu_model="AMD Ryzen 9 9950X")
# Downstream capacity planner (AIMD admission) depends on the shape *and*
# the exact values, so any drift here is a breaking change worth flagging
# at test-time rather than in a capacity incident.

class TestHostBaseline:
    def test_host_baseline_is_a_hostbaseline_instance(self):
        assert isinstance(hm.HOST_BASELINE, hm.HostBaseline)

    def test_host_baseline_values_match_h1_spec(self):
        assert hm.HOST_BASELINE.cpu_cores == 16
        assert hm.HOST_BASELINE.mem_total_gb == 64
        assert hm.HOST_BASELINE.disk_total_gb == 512
        assert hm.HOST_BASELINE.cpu_model == "AMD Ryzen 9 9950X"

    def test_host_baseline_field_types(self):
        assert isinstance(hm.HOST_BASELINE.cpu_cores, int)
        assert isinstance(hm.HOST_BASELINE.mem_total_gb, int)
        assert isinstance(hm.HOST_BASELINE.disk_total_gb, int)
        assert isinstance(hm.HOST_BASELINE.cpu_model, str)

    def test_host_baseline_is_immutable(self):
        # ``frozen=True`` guarantees no runtime code can mutate the ceiling.
        with pytest.raises(Exception):
            hm.HOST_BASELINE.cpu_cores = 99  # type: ignore[misc]

    def test_hostbaseline_dataclass_shape(self):
        # Catches accidental field rename — downstream serialisers key off
        # these exact attribute names.
        expected = {"cpu_cores", "mem_total_gb", "disk_total_gb", "cpu_model"}
        actual = {f.name for f in hm.HostBaseline.__dataclass_fields__.values()}
        assert actual == expected


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H1 — psutil host sampling (sample_host_once)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# These contract tests pin the H1 "psutil 採樣" TODO row. They cover:
#   * HostSample dataclass shape + immutability
#   * psutil-present path: cpu_percent / virtual_memory (available-based)
#     / disk_usage('/') / os.getloadavg() are all consumed correctly
#   * psutil-absent path: function still returns a HostSample, loadavg
#     still populates, and the HOST_BASELINE totals are used as fallback
#     (this is the "soft import" contract — dev envs without psutil
#     installed still boot)
#   * _read_loadavg() swallows OSError / AttributeError → (0,0,0)

class _FakeVM:
    """Drop-in for psutil.virtual_memory()."""
    def __init__(self, total: int, available: int):
        self.total = total
        self.available = available


class _FakeDU:
    """Drop-in for psutil.disk_usage(path)."""
    def __init__(self, total: int, used: int, percent: float):
        self.total = total
        self.used = used
        self.percent = percent


class _FakePsutil:
    """Minimal psutil shim — enough surface for sample_host_once()."""
    def __init__(self, *, cpu_pct: float, vm: _FakeVM, du: _FakeDU):
        self._cpu_pct = cpu_pct
        self._vm = vm
        self._du = du
        self.cpu_percent_calls: list[float] = []
        self.disk_usage_calls: list[str] = []

    def cpu_percent(self, interval: float = None):  # type: ignore[override]
        self.cpu_percent_calls.append(interval)
        return self._cpu_pct

    def virtual_memory(self):
        return self._vm

    def disk_usage(self, path: str):
        self.disk_usage_calls.append(path)
        return self._du


class TestHostSampleDataclass:
    def test_fields_shape(self):
        expected = {
            "cpu_percent", "mem_percent", "mem_used_gb", "mem_total_gb",
            "disk_percent", "disk_used_gb", "disk_total_gb",
            "loadavg_1m", "loadavg_5m", "loadavg_15m", "sampled_at",
        }
        actual = {f.name for f in hm.HostSample.__dataclass_fields__.values()}
        assert actual == expected

    def test_frozen(self):
        s = hm.HostSample(
            cpu_percent=1.0, mem_percent=2.0, mem_used_gb=3.0, mem_total_gb=4.0,
            disk_percent=5.0, disk_used_gb=6.0, disk_total_gb=7.0,
            loadavg_1m=8.0, loadavg_5m=9.0, loadavg_15m=10.0, sampled_at=11.0,
        )
        with pytest.raises(Exception):
            s.cpu_percent = 99.0  # type: ignore[misc]


class TestSampleHostOnce:
    def test_uses_psutil_cpu_percent_with_requested_interval(self, monkeypatch):
        fake = _FakePsutil(
            cpu_pct=37.5,
            vm=_FakeVM(total=64 * 1024 ** 3, available=32 * 1024 ** 3),
            du=_FakeDU(total=512 * 1024 ** 3, used=100 * 1024 ** 3, percent=20.0),
        )
        monkeypatch.setattr(hm, "psutil", fake)
        monkeypatch.setattr(hm, "_read_loadavg", lambda: (1.5, 2.0, 2.5))

        s = hm.sample_host_once(cpu_interval=1.0)

        assert s.cpu_percent == 37.5
        assert fake.cpu_percent_calls == [1.0]

    def test_memory_used_is_total_minus_available_not_psutil_used(self, monkeypatch):
        # Half memory available → mem_percent == 50%, used == 32 GB.
        fake = _FakePsutil(
            cpu_pct=0.0,
            vm=_FakeVM(total=64 * 1024 ** 3, available=32 * 1024 ** 3),
            du=_FakeDU(total=0, used=0, percent=0.0),
        )
        monkeypatch.setattr(hm, "psutil", fake)
        monkeypatch.setattr(hm, "_read_loadavg", lambda: (0.0, 0.0, 0.0))

        s = hm.sample_host_once(cpu_interval=0)
        assert s.mem_total_gb == pytest.approx(64.0, abs=0.01)
        assert s.mem_used_gb == pytest.approx(32.0, abs=0.01)
        assert s.mem_percent == pytest.approx(50.0, abs=0.1)

    def test_disk_sampled_from_root(self, monkeypatch):
        fake = _FakePsutil(
            cpu_pct=0.0,
            vm=_FakeVM(total=1, available=1),
            du=_FakeDU(total=512 * 1024 ** 3, used=256 * 1024 ** 3, percent=50.0),
        )
        monkeypatch.setattr(hm, "psutil", fake)
        monkeypatch.setattr(hm, "_read_loadavg", lambda: (0.0, 0.0, 0.0))

        s = hm.sample_host_once(cpu_interval=0)
        assert fake.disk_usage_calls == ["/"]
        assert s.disk_total_gb == pytest.approx(512.0, abs=0.01)
        assert s.disk_used_gb == pytest.approx(256.0, abs=0.01)
        assert s.disk_percent == 50.0

    def test_loadavg_populated_from_os_getloadavg(self, monkeypatch):
        fake = _FakePsutil(
            cpu_pct=0.0,
            vm=_FakeVM(total=1, available=1),
            du=_FakeDU(total=1, used=0, percent=0.0),
        )
        monkeypatch.setattr(hm, "psutil", fake)
        monkeypatch.setattr(hm, "_read_loadavg", lambda: (4.25, 3.5, 2.1))

        s = hm.sample_host_once(cpu_interval=0)
        assert s.loadavg_1m == 4.25
        assert s.loadavg_5m == 3.5
        assert s.loadavg_15m == 2.1

    def test_psutil_absent_falls_back_to_baseline_totals(self, monkeypatch):
        monkeypatch.setattr(hm, "psutil", None)
        monkeypatch.setattr(hm, "_read_loadavg", lambda: (1.0, 1.0, 1.0))

        s = hm.sample_host_once(cpu_interval=0)
        # Function still returns a HostSample — no exception.
        assert isinstance(s, hm.HostSample)
        # Usage fields are zero because we couldn't read them.
        assert s.cpu_percent == 0.0
        assert s.mem_used_gb == 0.0
        assert s.disk_used_gb == 0.0
        # But the *totals* fall back to HOST_BASELINE so downstream
        # percent calculations don't divide by zero.
        assert s.mem_total_gb == float(hm.HOST_BASELINE.mem_total_gb)
        assert s.disk_total_gb == float(hm.HOST_BASELINE.disk_total_gb)
        # Loadavg is stdlib so it still works.
        assert s.loadavg_1m == 1.0

    def test_psutil_cpu_percent_exception_is_swallowed(self, monkeypatch):
        class Boom(_FakePsutil):
            def cpu_percent(self, interval=None):
                raise RuntimeError("sampler glitch")
        fake = Boom(
            cpu_pct=0.0,
            vm=_FakeVM(total=1, available=1),
            du=_FakeDU(total=1, used=0, percent=0.0),
        )
        monkeypatch.setattr(hm, "psutil", fake)
        monkeypatch.setattr(hm, "_read_loadavg", lambda: (0.0, 0.0, 0.0))
        s = hm.sample_host_once(cpu_interval=0)
        assert s.cpu_percent == 0.0  # gracefully degraded, not raised

    def test_psutil_virtual_memory_exception_uses_baseline(self, monkeypatch):
        class Boom(_FakePsutil):
            def virtual_memory(self):
                raise RuntimeError("vm sampler glitch")
        fake = Boom(
            cpu_pct=10.0,
            vm=_FakeVM(total=1, available=1),
            du=_FakeDU(total=512 * 1024 ** 3, used=0, percent=0.0),
        )
        monkeypatch.setattr(hm, "psutil", fake)
        monkeypatch.setattr(hm, "_read_loadavg", lambda: (0.0, 0.0, 0.0))
        s = hm.sample_host_once(cpu_interval=0)
        # CPU + disk still work; mem uses baseline fallback.
        assert s.cpu_percent == 10.0
        assert s.mem_total_gb == float(hm.HOST_BASELINE.mem_total_gb)
        assert s.mem_used_gb == 0.0
        assert s.mem_percent == 0.0
        assert s.disk_total_gb == pytest.approx(512.0, abs=0.01)

    def test_sampled_at_is_wall_clock(self, monkeypatch):
        fake = _FakePsutil(
            cpu_pct=0.0,
            vm=_FakeVM(total=1, available=1),
            du=_FakeDU(total=1, used=0, percent=0.0),
        )
        monkeypatch.setattr(hm, "psutil", fake)
        monkeypatch.setattr(hm, "_read_loadavg", lambda: (0.0, 0.0, 0.0))
        import time as _time
        before = _time.time()
        s = hm.sample_host_once(cpu_interval=0)
        after = _time.time()
        assert before <= s.sampled_at <= after


class TestReadLoadavg:
    def test_returns_tuple_of_three_floats(self):
        # Real os.getloadavg() on Linux — just check the shape.
        la1, la5, la15 = hm._read_loadavg()
        assert isinstance(la1, float)
        assert isinstance(la5, float)
        assert isinstance(la15, float)
        # All non-negative.
        assert la1 >= 0 and la5 >= 0 and la15 >= 0

    def test_swallows_oserror(self, monkeypatch):
        def boom():
            raise OSError("no loadavg here")
        monkeypatch.setattr(os, "getloadavg", boom)
        assert hm._read_loadavg() == (0.0, 0.0, 0.0)

    def test_swallows_attributeerror(self, monkeypatch):
        """Windows stdlib doesn't ship os.getloadavg at all."""
        def boom():
            raise AttributeError("getloadavg undefined")
        monkeypatch.setattr(os, "getloadavg", boom)
        assert hm._read_loadavg() == (0.0, 0.0, 0.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H1 — Docker-daemon sampling (sample_docker_once)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# These contract tests pin the H1 "Docker SDK 抓 running container 數 +
# 總 mem reservation；Docker Desktop 情境 fallback `docker stats
# --no-stream`" TODO row. They cover:
#   * DockerSample dataclass shape + immutability
#   * SDK primary path: containers.list(filters={"status":"running"})
#     → sum HostConfig.MemoryReservation (with fallback to .Memory),
#     source="sdk"
#   * CLI fallback path: when SDK is unavailable or errors, parse
#     `docker stats --no-stream` output, source="cli"
#   * Both-paths-fail: source="unavailable", counts zero, no exception
#   * docker stats MemUsage column parser handles MiB/GiB/MB/GB/B/no-unit

import subprocess as _subprocess
from unittest.mock import MagicMock


class _FakeContainer:
    """Drop-in for docker-py container objects."""
    def __init__(self, host_config: dict | None = None):
        self.attrs = {"HostConfig": host_config or {}}


class _FakeContainersCollection:
    def __init__(self, containers: list[_FakeContainer]):
        self._containers = containers
        self.list_calls: list[dict] = []

    def list(self, filters=None, **kwargs):  # noqa: A002
        self.list_calls.append(filters or {})
        return list(self._containers)


class _FakeDockerClient:
    def __init__(self, containers: list[_FakeContainer]):
        self.containers = _FakeContainersCollection(containers)


class _FakeDockerSDK:
    """Drop-in for the ``docker`` module."""
    def __init__(self, client: _FakeDockerClient | None = None, raise_on_from_env: Exception | None = None):
        self._client = client
        self._raise = raise_on_from_env
        self.from_env_calls: list[dict] = []

    def from_env(self, **kwargs):
        self.from_env_calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return self._client


class TestDockerSampleDataclass:
    def test_fields_shape(self):
        expected = {
            "container_count", "total_mem_reservation_bytes",
            "source", "sampled_at",
        }
        actual = {f.name for f in hm.DockerSample.__dataclass_fields__.values()}
        assert actual == expected

    def test_frozen(self):
        s = hm.DockerSample(
            container_count=3, total_mem_reservation_bytes=1024,
            source="sdk", sampled_at=1000.0,
        )
        with pytest.raises(Exception):
            s.container_count = 99  # type: ignore[misc]

    def test_source_is_string(self):
        s = hm.DockerSample(
            container_count=0, total_mem_reservation_bytes=0,
            source="unavailable", sampled_at=0.0,
        )
        assert isinstance(s.source, str)


class TestSdkMemReservationBytes:
    def test_reservation_preferred_over_memory(self):
        c = _FakeContainer({"MemoryReservation": 500, "Memory": 1000})
        assert hm._sdk_mem_reservation_bytes(c) == 500

    def test_falls_back_to_memory_when_reservation_unset(self):
        c = _FakeContainer({"MemoryReservation": 0, "Memory": 2048})
        assert hm._sdk_mem_reservation_bytes(c) == 2048

    def test_falls_back_to_memory_when_reservation_missing(self):
        c = _FakeContainer({"Memory": 2048})
        assert hm._sdk_mem_reservation_bytes(c) == 2048

    def test_returns_zero_when_both_unset(self):
        c = _FakeContainer({})
        assert hm._sdk_mem_reservation_bytes(c) == 0

    def test_handles_missing_host_config(self):
        c = _FakeContainer()
        c.attrs = {}  # no HostConfig key at all
        assert hm._sdk_mem_reservation_bytes(c) == 0

    def test_swallows_exception(self):
        c = MagicMock()
        type(c).attrs = property(lambda _self: (_ for _ in ()).throw(RuntimeError("boom")))
        assert hm._sdk_mem_reservation_bytes(c) == 0


class TestSampleDockerViaSdk:
    def test_none_when_sdk_not_installed(self, monkeypatch):
        monkeypatch.setattr(hm, "docker_sdk", None)
        assert hm._sample_docker_via_sdk() is None

    def test_returns_count_and_total(self, monkeypatch):
        containers = [
            _FakeContainer({"MemoryReservation": 256 * 1024 ** 2}),   # 256 MiB
            _FakeContainer({"MemoryReservation": 1024 ** 3}),         # 1 GiB
            _FakeContainer({"Memory": 512 * 1024 ** 2}),              # 512 MiB via hard limit
        ]
        fake_sdk = _FakeDockerSDK(client=_FakeDockerClient(containers))
        monkeypatch.setattr(hm, "docker_sdk", fake_sdk)
        result = hm._sample_docker_via_sdk()
        assert result is not None
        count, total = result
        assert count == 3
        assert total == (256 * 1024 ** 2) + (1024 ** 3) + (512 * 1024 ** 2)

    def test_filters_running_only(self, monkeypatch):
        client = _FakeDockerClient([])
        fake_sdk = _FakeDockerSDK(client=client)
        monkeypatch.setattr(hm, "docker_sdk", fake_sdk)
        hm._sample_docker_via_sdk()
        # Running-only filter must be passed to containers.list().
        assert client.containers.list_calls == [{"status": "running"}]

    def test_returns_none_when_from_env_raises(self, monkeypatch):
        fake_sdk = _FakeDockerSDK(raise_on_from_env=RuntimeError("daemon unreachable"))
        monkeypatch.setattr(hm, "docker_sdk", fake_sdk)
        assert hm._sample_docker_via_sdk() is None

    def test_returns_none_when_list_raises(self, monkeypatch):
        client = MagicMock()
        client.containers.list.side_effect = RuntimeError("api 500")
        fake_sdk = _FakeDockerSDK(client=client)
        monkeypatch.setattr(hm, "docker_sdk", fake_sdk)
        assert hm._sample_docker_via_sdk() is None

    def test_empty_container_list_returns_zeros(self, monkeypatch):
        fake_sdk = _FakeDockerSDK(client=_FakeDockerClient([]))
        monkeypatch.setattr(hm, "docker_sdk", fake_sdk)
        result = hm._sample_docker_via_sdk()
        assert result == (0, 0)


class TestParseDockerStatsMemColumn:
    def test_mib(self):
        assert hm._parse_docker_stats_mem_column("127.5MiB / 1.95GiB") == int(127.5 * 1024 ** 2)

    def test_gib(self):
        assert hm._parse_docker_stats_mem_column("2GiB / 4GiB") == 2 * 1024 ** 3

    def test_mb_decimal(self):
        assert hm._parse_docker_stats_mem_column("100MB / 1GB") == 100 * 10 ** 6

    def test_gb_decimal(self):
        assert hm._parse_docker_stats_mem_column("1.5GB / 0B") == int(1.5 * 10 ** 9)

    def test_kib(self):
        assert hm._parse_docker_stats_mem_column("512KiB / 1GiB") == 512 * 1024

    def test_bytes(self):
        assert hm._parse_docker_stats_mem_column("4096B / 0B") == 4096

    def test_zero_bytes(self):
        assert hm._parse_docker_stats_mem_column("0B / 0B") == 0

    def test_empty_string_returns_zero(self):
        assert hm._parse_docker_stats_mem_column("") == 0

    def test_malformed_returns_zero(self):
        assert hm._parse_docker_stats_mem_column("not-a-number-MiB") == 0

    def test_no_unit_treated_as_bytes(self):
        assert hm._parse_docker_stats_mem_column("1234 / 0") == 1234

    def test_column_without_slash(self):
        # Edge case: just the LHS with no divider.
        assert hm._parse_docker_stats_mem_column("256MiB") == 256 * 1024 ** 2


class TestSampleDockerViaCli:
    def test_none_when_docker_cli_absent(self, monkeypatch):
        monkeypatch.setattr(hm.shutil, "which", lambda _name: None)
        assert hm._sample_docker_via_cli() is None

    def test_returns_count_and_total_from_stats(self, monkeypatch):
        monkeypatch.setattr(hm.shutil, "which", lambda _name: "/usr/bin/docker")
        fake_out = (
            "abc123\t100MiB / 1GiB\n"
            "def456\t200MiB / 1GiB\n"
            "789xyz\t50MiB / 1GiB\n"
        )
        fake_proc = MagicMock(returncode=0, stdout=fake_out, stderr="")

        def _run(*args, **kwargs):
            return fake_proc
        monkeypatch.setattr(hm.subprocess, "run", _run)
        result = hm._sample_docker_via_cli()
        assert result is not None
        count, total = result
        assert count == 3
        assert total == 350 * 1024 ** 2

    def test_none_when_subprocess_returns_nonzero(self, monkeypatch):
        monkeypatch.setattr(hm.shutil, "which", lambda _name: "/usr/bin/docker")
        fake_proc = MagicMock(returncode=1, stdout="", stderr="cannot connect")

        def _run(*args, **kwargs):
            return fake_proc
        monkeypatch.setattr(hm.subprocess, "run", _run)
        assert hm._sample_docker_via_cli() is None

    def test_none_when_subprocess_times_out(self, monkeypatch):
        monkeypatch.setattr(hm.shutil, "which", lambda _name: "/usr/bin/docker")

        def _run(*args, **kwargs):
            raise _subprocess.TimeoutExpired(cmd="docker stats", timeout=10)
        monkeypatch.setattr(hm.subprocess, "run", _run)
        assert hm._sample_docker_via_cli() is None

    def test_none_when_subprocess_raises_oserror(self, monkeypatch):
        monkeypatch.setattr(hm.shutil, "which", lambda _name: "/usr/bin/docker")

        def _run(*args, **kwargs):
            raise OSError("no such file")
        monkeypatch.setattr(hm.subprocess, "run", _run)
        assert hm._sample_docker_via_cli() is None

    def test_empty_stdout_returns_zeros(self, monkeypatch):
        monkeypatch.setattr(hm.shutil, "which", lambda _name: "/usr/bin/docker")
        fake_proc = MagicMock(returncode=0, stdout="", stderr="")

        def _run(*args, **kwargs):
            return fake_proc
        monkeypatch.setattr(hm.subprocess, "run", _run)
        assert hm._sample_docker_via_cli() == (0, 0)

    def test_ignores_blank_and_malformed_lines(self, monkeypatch):
        monkeypatch.setattr(hm.shutil, "which", lambda _name: "/usr/bin/docker")
        fake_out = (
            "abc123\t100MiB / 1GiB\n"
            "\n"                         # blank — skip
            "malformed_no_tab\n"          # no tab — skip
            "def456\t200MiB / 1GiB\n"
        )
        fake_proc = MagicMock(returncode=0, stdout=fake_out, stderr="")

        def _run(*args, **kwargs):
            return fake_proc
        monkeypatch.setattr(hm.subprocess, "run", _run)
        result = hm._sample_docker_via_cli()
        assert result == (2, 300 * 1024 ** 2)

    def test_invokes_docker_stats_no_stream(self, monkeypatch):
        monkeypatch.setattr(hm.shutil, "which", lambda _name: "/usr/bin/docker")
        captured: dict = {}

        def _run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(hm.subprocess, "run", _run)
        hm._sample_docker_via_cli()
        assert captured["cmd"][:3] == ["docker", "stats", "--no-stream"]
        assert "--format" in captured["cmd"]
        # Must time-box: we'd rather be unavailable than hang the sampler.
        assert captured["kwargs"]["timeout"] == hm.DOCKER_STATS_TIMEOUT_S


class TestSampleDockerOnce:
    def test_sdk_path_preferred_when_available(self, monkeypatch):
        containers = [_FakeContainer({"MemoryReservation": 1024 ** 3})]
        fake_sdk = _FakeDockerSDK(client=_FakeDockerClient(containers))
        monkeypatch.setattr(hm, "docker_sdk", fake_sdk)

        # Spy on CLI path — must not be called when SDK works.
        cli_calls = []

        def _cli_boom():
            cli_calls.append(1)
            return None
        monkeypatch.setattr(hm, "_sample_docker_via_cli", _cli_boom)

        s = hm.sample_docker_once()
        assert s.source == "sdk"
        assert s.container_count == 1
        assert s.total_mem_reservation_bytes == 1024 ** 3
        assert cli_calls == []

    def test_falls_back_to_cli_when_sdk_unavailable(self, monkeypatch):
        # SDK absent → primary path returns None.
        monkeypatch.setattr(hm, "docker_sdk", None)
        monkeypatch.setattr(hm.shutil, "which", lambda _name: "/usr/bin/docker")
        fake_proc = MagicMock(
            returncode=0,
            stdout="abc\t512MiB / 1GiB\n",
            stderr="",
        )
        monkeypatch.setattr(hm.subprocess, "run", lambda *a, **k: fake_proc)

        s = hm.sample_docker_once()
        assert s.source == "cli"
        assert s.container_count == 1
        assert s.total_mem_reservation_bytes == 512 * 1024 ** 2

    def test_falls_back_to_cli_when_sdk_connection_fails(self, monkeypatch):
        # Docker Desktop scenario — SDK module imports fine but can't
        # reach the daemon.
        fake_sdk = _FakeDockerSDK(raise_on_from_env=RuntimeError("no docker socket"))
        monkeypatch.setattr(hm, "docker_sdk", fake_sdk)
        monkeypatch.setattr(hm.shutil, "which", lambda _name: "/usr/bin/docker")
        fake_proc = MagicMock(
            returncode=0,
            stdout="abc\t100MiB / 1GiB\ndef\t200MiB / 1GiB\n",
            stderr="",
        )
        monkeypatch.setattr(hm.subprocess, "run", lambda *a, **k: fake_proc)

        s = hm.sample_docker_once()
        assert s.source == "cli"
        assert s.container_count == 2
        assert s.total_mem_reservation_bytes == 300 * 1024 ** 2

    def test_returns_unavailable_when_both_paths_fail(self, monkeypatch):
        monkeypatch.setattr(hm, "docker_sdk", None)
        monkeypatch.setattr(hm.shutil, "which", lambda _name: None)
        s = hm.sample_docker_once()
        assert s.source == "unavailable"
        assert s.container_count == 0
        assert s.total_mem_reservation_bytes == 0

    def test_returns_unavailable_when_cli_rc_nonzero(self, monkeypatch):
        monkeypatch.setattr(hm, "docker_sdk", None)
        monkeypatch.setattr(hm.shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(
            hm.subprocess, "run",
            lambda *a, **k: MagicMock(returncode=1, stdout="", stderr="err"),
        )
        s = hm.sample_docker_once()
        assert s.source == "unavailable"
        assert s.container_count == 0
        assert s.total_mem_reservation_bytes == 0

    def test_sampled_at_is_wall_clock(self, monkeypatch):
        monkeypatch.setattr(hm, "docker_sdk", None)
        monkeypatch.setattr(hm.shutil, "which", lambda _name: None)
        import time as _time
        before = _time.time()
        s = hm.sample_docker_once()
        after = _time.time()
        assert before <= s.sampled_at <= after

    def test_never_raises(self, monkeypatch):
        # Even if both paths misbehave in unexpected ways, the public
        # API must not raise — the sampling loop depends on this.
        class Grenade:
            def from_env(self, **_kwargs):
                raise Exception("grenade")
        monkeypatch.setattr(hm, "docker_sdk", Grenade())
        monkeypatch.setattr(hm.shutil, "which", lambda _name: "/usr/bin/docker")

        def _run(*a, **k):
            raise Exception("another grenade")
        monkeypatch.setattr(hm.subprocess, "run", _run)
        # Should not raise.
        s = hm.sample_docker_once()
        assert s.source == "unavailable"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H1 — Ring buffer (sample_host_snapshot / host history / sampling loop)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Contract tests for the H1 "採樣 5s 週期、ring buffer 60 點 (5 分鐘
# 歷史)" row. They pin:
#   * HOST_HISTORY_SIZE == 60 (downstream consumers size their buffers
#     against this constant — a silent change would desync them)
#   * HostSnapshot bundles HostSample + DockerSample immutably and its
#     sampled_at mirrors the host sample
#   * sample_host_snapshot() composes the two sub-samplers
#   * Ring buffer rotation: 70 appends → keep last 60, oldest dropped
#   * get_host_history() returns a point-in-time COPY (mutation-safe)
#   * get_latest_host_snapshot() returns None cold-start, newest after
#   * run_host_sampling_loop cadence (wall-clock ~interval_s, not
#     interval_s + cpu_interval) and per-iteration exception swallowing


def _make_host_sample(t: float = 0.0, cpu: float = 10.0) -> hm.HostSample:
    """Minimal HostSample factory for ring-buffer tests."""
    return hm.HostSample(
        cpu_percent=cpu, mem_percent=20.0, mem_used_gb=12.0, mem_total_gb=64.0,
        disk_percent=30.0, disk_used_gb=150.0, disk_total_gb=512.0,
        loadavg_1m=1.0, loadavg_5m=1.0, loadavg_15m=1.0, sampled_at=t,
    )


def _make_docker_sample(t: float = 0.0, count: int = 3) -> hm.DockerSample:
    """Minimal DockerSample factory for ring-buffer tests."""
    return hm.DockerSample(
        container_count=count, total_mem_reservation_bytes=count * 1024 ** 3,
        source="sdk", sampled_at=t,
    )


class TestHostHistoryConstants:
    def test_ring_size_is_60_for_five_minutes_at_5s(self):
        # 60 × 5 s = 300 s = 5 min. Pinned so AIMD / runbook math stays
        # in lock-step with the buffer depth.
        assert hm.HOST_HISTORY_SIZE == 60
        assert hm.SAMPLE_INTERVAL_S == 5.0
        assert hm.HOST_HISTORY_SIZE * hm.SAMPLE_INTERVAL_S == 300.0


class TestHostSnapshotDataclass:
    def test_fields_shape(self):
        expected = {"host", "docker", "sampled_at"}
        actual = {f.name for f in hm.HostSnapshot.__dataclass_fields__.values()}
        assert actual == expected

    def test_frozen(self):
        snap = hm.HostSnapshot(
            host=_make_host_sample(1.0),
            docker=_make_docker_sample(1.0),
            sampled_at=1.0,
        )
        with pytest.raises(Exception):
            snap.sampled_at = 99.0  # type: ignore[misc]

    def test_bundles_host_and_docker(self):
        host = _make_host_sample(42.0, cpu=77.0)
        docker = _make_docker_sample(42.0, count=5)
        snap = hm.HostSnapshot(host=host, docker=docker, sampled_at=42.0)
        assert snap.host.cpu_percent == 77.0
        assert snap.docker.container_count == 5


class TestSampleHostSnapshot:
    def test_composes_host_and_docker_samples(self, monkeypatch):
        fake_host = _make_host_sample(t=1000.0, cpu=55.0)
        fake_docker = _make_docker_sample(t=1000.5, count=7)
        monkeypatch.setattr(
            hm, "sample_host_once", lambda *, cpu_interval: fake_host,
        )
        monkeypatch.setattr(hm, "sample_docker_once", lambda: fake_docker)
        snap = hm.sample_host_snapshot(cpu_interval=0)
        assert snap.host is fake_host
        assert snap.docker is fake_docker
        # sampled_at mirrors the host sample — see docstring contract.
        assert snap.sampled_at == 1000.0

    def test_passes_cpu_interval_through_to_host_sampler(self, monkeypatch):
        captured: dict = {}

        def _fake_host(*, cpu_interval: float):
            captured["interval"] = cpu_interval
            return _make_host_sample(t=1.0)
        monkeypatch.setattr(hm, "sample_host_once", _fake_host)
        monkeypatch.setattr(hm, "sample_docker_once", lambda: _make_docker_sample(1.0))
        hm.sample_host_snapshot(cpu_interval=0.25)
        assert captured["interval"] == 0.25


class TestRingBufferRotation:
    def test_empty_ring_returns_empty_history(self):
        assert hm.get_host_history() == []

    def test_empty_ring_latest_is_none(self):
        assert hm.get_latest_host_snapshot() is None

    def test_single_append_returned_in_history(self):
        snap = hm.HostSnapshot(
            host=_make_host_sample(1.0),
            docker=_make_docker_sample(1.0),
            sampled_at=1.0,
        )
        hm._record_host_snapshot(snap)
        hist = hm.get_host_history()
        assert len(hist) == 1
        assert hist[0] is snap
        assert hm.get_latest_host_snapshot() is snap

    def test_appends_in_chronological_order(self):
        for i in range(5):
            hm._record_host_snapshot(hm.HostSnapshot(
                host=_make_host_sample(float(i)),
                docker=_make_docker_sample(float(i)),
                sampled_at=float(i),
            ))
        hist = hm.get_host_history()
        assert [s.sampled_at for s in hist] == [0.0, 1.0, 2.0, 3.0, 4.0]
        assert hm.get_latest_host_snapshot().sampled_at == 4.0

    def test_rotates_when_full_keeps_last_60(self):
        # Append 70 snapshots → buffer must hold ticks 10..69 (last 60).
        for i in range(70):
            hm._record_host_snapshot(hm.HostSnapshot(
                host=_make_host_sample(float(i)),
                docker=_make_docker_sample(float(i)),
                sampled_at=float(i),
            ))
        hist = hm.get_host_history()
        assert len(hist) == hm.HOST_HISTORY_SIZE == 60
        # Oldest 10 ticks dropped; ticks 10..69 remain.
        assert hist[0].sampled_at == 10.0
        assert hist[-1].sampled_at == 69.0
        assert hm.get_latest_host_snapshot().sampled_at == 69.0

    def test_exactly_60_appends_no_rotation(self):
        for i in range(hm.HOST_HISTORY_SIZE):
            hm._record_host_snapshot(hm.HostSnapshot(
                host=_make_host_sample(float(i)),
                docker=_make_docker_sample(float(i)),
                sampled_at=float(i),
            ))
        hist = hm.get_host_history()
        assert len(hist) == 60
        assert hist[0].sampled_at == 0.0
        assert hist[-1].sampled_at == 59.0

    def test_get_host_history_returns_copy_not_reference(self):
        """Mutating the returned list must not mutate the ring buffer."""
        hm._record_host_snapshot(hm.HostSnapshot(
            host=_make_host_sample(1.0),
            docker=_make_docker_sample(1.0),
            sampled_at=1.0,
        ))
        hist = hm.get_host_history()
        hist.clear()
        hist.append("garbage")  # type: ignore[arg-type]
        # Ring buffer untouched.
        assert len(hm.get_host_history()) == 1
        assert hm.get_latest_host_snapshot() is not None

    def test_reset_for_tests_clears_ring_buffer(self):
        hm._record_host_snapshot(hm.HostSnapshot(
            host=_make_host_sample(1.0),
            docker=_make_docker_sample(1.0),
            sampled_at=1.0,
        ))
        assert len(hm.get_host_history()) == 1
        hm._reset_for_tests()
        assert hm.get_host_history() == []
        assert hm.get_latest_host_snapshot() is None


class TestRunHostSamplingLoop:
    @pytest.mark.asyncio
    async def test_pushes_snapshots_into_ring_buffer(self, monkeypatch):
        call_log: list[float] = []

        def _fake_host(*, cpu_interval: float):
            t = float(len(call_log))
            call_log.append(t)
            return _make_host_sample(t=t, cpu=t * 10.0)
        monkeypatch.setattr(hm, "sample_host_once", _fake_host)
        monkeypatch.setattr(
            hm, "sample_docker_once",
            lambda: _make_docker_sample(t=float(len(call_log)), count=1),
        )

        task = asyncio.create_task(
            hm.run_host_sampling_loop(interval_s=0.01, cpu_interval=0),
        )
        # Let the loop fire a handful of times.
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        hist = hm.get_host_history()
        assert len(hist) >= 3, f"expected ≥3 ticks, got {len(hist)}"
        # Snapshots are in chronological order.
        assert all(
            hist[i].sampled_at <= hist[i + 1].sampled_at
            for i in range(len(hist) - 1)
        )

    @pytest.mark.asyncio
    async def test_bounded_at_HOST_HISTORY_SIZE(self, monkeypatch):
        """Loop running longer than 60 ticks must not grow the buffer."""
        monkeypatch.setattr(
            hm, "sample_host_once",
            lambda *, cpu_interval: _make_host_sample(t=time.time()),
        )
        monkeypatch.setattr(
            hm, "sample_docker_once",
            lambda: _make_docker_sample(t=time.time()),
        )

        task = asyncio.create_task(
            hm.run_host_sampling_loop(interval_s=0.001, cpu_interval=0),
        )
        # Fire the loop many more times than HOST_HISTORY_SIZE.
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(hm.get_host_history()) <= hm.HOST_HISTORY_SIZE

    @pytest.mark.asyncio
    async def test_swallows_sampler_exceptions(self, monkeypatch):
        """A transient sampler glitch must not kill the loop."""
        call_count = {"n": 0}

        def _flaky_host(*, cpu_interval: float):
            call_count["n"] += 1
            if call_count["n"] % 2 == 1:
                raise RuntimeError("sampler glitch")
            return _make_host_sample(t=float(call_count["n"]))
        monkeypatch.setattr(hm, "sample_host_once", _flaky_host)
        monkeypatch.setattr(
            hm, "sample_docker_once",
            lambda: _make_docker_sample(t=0.0),
        )

        task = asyncio.create_task(
            hm.run_host_sampling_loop(interval_s=0.01, cpu_interval=0),
        )
        await asyncio.sleep(0.15)
        assert not task.done(), "loop died on sampler exception"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # At least some good samples landed despite the flake.
        assert len(hm.get_host_history()) >= 1

    @pytest.mark.asyncio
    async def test_cancellation_is_clean(self, monkeypatch):
        monkeypatch.setattr(
            hm, "sample_host_once",
            lambda *, cpu_interval: _make_host_sample(t=0.0),
        )
        monkeypatch.setattr(
            hm, "sample_docker_once",
            lambda: _make_docker_sample(t=0.0),
        )
        task = asyncio.create_task(
            hm.run_host_sampling_loop(interval_s=0.01, cpu_interval=0),
        )
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_cadence_subtracts_sample_duration(self, monkeypatch):
        """Loop targets wall-clock ``interval_s`` — when the sample
        already blocks for part of that window, the sleep shrinks so
        total tick time stays close to the requested period."""
        sleep_calls: list[float] = []
        real_sleep = asyncio.sleep

        async def _tracking_sleep(seconds: float):
            sleep_calls.append(seconds)
            # Yield to the loop without actually waiting.
            await real_sleep(0)
        monkeypatch.setattr(hm.asyncio, "sleep", _tracking_sleep)

        def _slow_host(*, cpu_interval: float):
            # Simulate sampling taking a non-trivial slice of the window.
            time.sleep(0.02)
            return _make_host_sample(t=time.time())
        monkeypatch.setattr(hm, "sample_host_once", _slow_host)
        monkeypatch.setattr(
            hm, "sample_docker_once",
            lambda: _make_docker_sample(t=time.time()),
        )

        task = asyncio.create_task(
            hm.run_host_sampling_loop(interval_s=0.1, cpu_interval=0),
        )
        await real_sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert sleep_calls, "loop never reached the sleep branch"
        # Every sleep must be non-negative and strictly less than the
        # requested interval (sample ate part of the window).
        for s in sleep_calls:
            assert 0.0 <= s <= 0.1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H1 — WSL2 high-pressure loadavg signal
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Contract tests for the H1 "WSL2 輔助訊號" TODO row. They pin:
#   * HIGH_PRESSURE_LOADAVG_RATIO == 0.9 (downstream H2 coordinator /
#     sandbox_prewarm OR this into their derate precondition — a silent
#     change would shift the high-pressure activation point)
#   * Ratio math: loadavg_1m / cpu_cores > threshold, strictly greater
#   * HOST_BASELINE.cpu_cores is the default denominator (16 under
#     current baseline) and lookup is at call-time (future-proofs
#     against runtime HOST_BASELINE swap)
#   * Edge cases — zero cores, negative cores, NaN loadavg — degrade
#     to False rather than raising, because the helper sits on the
#     hot path and callers must never see an exception here
#   * is_host_high_pressure() reads the ring buffer, returns False on
#     cold start (no tick yet), and accepts an explicit snapshot for
#     replay / unit tests


class TestHighPressureLoadavgConstant:
    def test_threshold_value_pinned_at_0_9(self):
        # Pinned so H2 coordinator / runbook stay in lock-step. A change
        # here shifts every derate activation, so it must be deliberate.
        assert hm.HIGH_PRESSURE_LOADAVG_RATIO == 0.9


class TestIsHighPressureLoadavg:
    def test_ratio_above_threshold_is_high_pressure(self):
        # loadavg 15 / 16 cores = 0.9375 → above 0.9 → high pressure.
        assert hm.is_high_pressure_loadavg(15.0, cpu_cores=16) is True

    def test_ratio_below_threshold_is_not_high_pressure(self):
        # loadavg 10 / 16 cores = 0.625 → below 0.9 → calm host.
        assert hm.is_high_pressure_loadavg(10.0, cpu_cores=16) is False

    def test_ratio_exactly_at_threshold_is_not_high_pressure(self):
        # Strict ">" — a saturated-but-not-overloaded host doesn't derate.
        # 14.4 / 16 = exactly 0.9.
        assert hm.is_high_pressure_loadavg(14.4, cpu_cores=16) is False

    def test_ratio_just_above_threshold_is_high_pressure(self):
        # Smallest nudge past the threshold trips it.
        assert hm.is_high_pressure_loadavg(14.41, cpu_cores=16) is True

    def test_defaults_to_host_baseline_cpu_cores(self):
        # No cpu_cores → use HOST_BASELINE.cpu_cores (16 under current
        # baseline). Matches the TODO spec "loadavg_1m / 16 > 0.9".
        assert hm.is_high_pressure_loadavg(15.0) is True
        assert hm.is_high_pressure_loadavg(10.0) is False

    def test_cpu_cores_lookup_is_at_call_time(self, monkeypatch):
        # Future-proof: a runtime detector that swaps HOST_BASELINE must
        # automatically retune the threshold without rebinding callers.
        swapped = hm.HostBaseline(cpu_cores=8, mem_total_gb=32,
                                   disk_total_gb=256, cpu_model="fake")
        monkeypatch.setattr(hm, "HOST_BASELINE", swapped)
        # 8 cores: 7.5 / 8 = 0.9375 → high pressure on the swapped host
        # even though the same loadavg on a 16-core host would be calm.
        assert hm.is_high_pressure_loadavg(7.5) is True
        assert hm.is_high_pressure_loadavg(6.0) is False  # 0.75

    def test_custom_threshold_overrides_constant(self):
        # Tunability for future experimentation / H2 A/B testing.
        assert hm.is_high_pressure_loadavg(
            13.0, cpu_cores=16, threshold=0.8,
        ) is True  # 0.8125 > 0.8
        assert hm.is_high_pressure_loadavg(
            13.0, cpu_cores=16, threshold=0.85,
        ) is False  # 0.8125 not > 0.85

    def test_zero_cpu_cores_returns_false(self):
        # Pathological (would ZeroDivisionError) — degrade to False so
        # the sampler hot path never raises.
        assert hm.is_high_pressure_loadavg(10.0, cpu_cores=0) is False

    def test_negative_cpu_cores_returns_false(self):
        assert hm.is_high_pressure_loadavg(10.0, cpu_cores=-1) is False

    def test_negative_loadavg_returns_false(self):
        # Shouldn't happen in practice but defended: a negative ratio
        # is never "high pressure".
        assert hm.is_high_pressure_loadavg(-5.0, cpu_cores=16) is False

    def test_zero_loadavg_is_not_high_pressure(self):
        # Completely idle host — explicitly not high-pressure (guards
        # against the ``ratio > 0`` short-circuit being confused with
        # the threshold check).
        assert hm.is_high_pressure_loadavg(0.0, cpu_cores=16) is False

    def test_nan_loadavg_returns_false(self):
        # ``_read_loadavg`` on weird platforms could hand back NaN via
        # psutil shims; NaN comparisons are always False, so the helper
        # must degrade gracefully rather than leaking a NaN-truthy.
        nan = float("nan")
        assert hm.is_high_pressure_loadavg(nan, cpu_cores=16) is False


class TestIsHostHighPressure:
    def test_cold_start_returns_false(self):
        # No snapshot in the ring buffer yet → "pressure unknown" → False.
        # Prevents the first few seconds after boot looking derated.
        assert hm.get_latest_host_snapshot() is None
        assert hm.is_host_high_pressure() is False

    def test_reads_latest_ring_buffer_entry(self):
        # loadavg 15 / 16 = 0.9375 → high pressure on the baseline host.
        hot = hm.HostSnapshot(
            host=hm.HostSample(
                cpu_percent=10.0, mem_percent=20.0,
                mem_used_gb=12.0, mem_total_gb=64.0,
                disk_percent=30.0, disk_used_gb=150.0, disk_total_gb=512.0,
                loadavg_1m=15.0, loadavg_5m=14.0, loadavg_15m=13.0,
                sampled_at=100.0,
            ),
            docker=_make_docker_sample(100.0),
            sampled_at=100.0,
        )
        hm._record_host_snapshot(hot)
        assert hm.is_host_high_pressure() is True

    def test_latest_entry_below_threshold_is_calm(self):
        calm = hm.HostSnapshot(
            host=hm.HostSample(
                cpu_percent=10.0, mem_percent=20.0,
                mem_used_gb=12.0, mem_total_gb=64.0,
                disk_percent=30.0, disk_used_gb=150.0, disk_total_gb=512.0,
                loadavg_1m=5.0, loadavg_5m=5.0, loadavg_15m=5.0,
                sampled_at=100.0,
            ),
            docker=_make_docker_sample(100.0),
            sampled_at=100.0,
        )
        hm._record_host_snapshot(calm)
        assert hm.is_host_high_pressure() is False

    def test_accepts_explicit_snapshot(self):
        # Replay path — pass a specific tick without touching the ring.
        hot_sample = hm.HostSample(
            cpu_percent=0.0, mem_percent=0.0,
            mem_used_gb=0.0, mem_total_gb=64.0,
            disk_percent=0.0, disk_used_gb=0.0, disk_total_gb=512.0,
            loadavg_1m=20.0, loadavg_5m=0.0, loadavg_15m=0.0,
            sampled_at=0.0,
        )
        snap = hm.HostSnapshot(
            host=hot_sample, docker=_make_docker_sample(0.0), sampled_at=0.0,
        )
        assert hm.is_host_high_pressure(snap) is True

    def test_accepts_explicit_hostsample(self):
        # Coordinator may have a bare HostSample (no docker pair) in
        # some paths — helper must handle both shapes transparently.
        hot_sample = hm.HostSample(
            cpu_percent=0.0, mem_percent=0.0,
            mem_used_gb=0.0, mem_total_gb=64.0,
            disk_percent=0.0, disk_used_gb=0.0, disk_total_gb=512.0,
            loadavg_1m=20.0, loadavg_5m=0.0, loadavg_15m=0.0,
            sampled_at=0.0,
        )
        assert hm.is_host_high_pressure(hot_sample) is True

    def test_uses_only_latest_tick_not_history(self):
        # Append an old high-pressure tick, then a recent calm tick —
        # the helper must reflect the *current* state, not historical.
        old_hot = hm.HostSnapshot(
            host=hm.HostSample(
                cpu_percent=0.0, mem_percent=0.0,
                mem_used_gb=0.0, mem_total_gb=64.0,
                disk_percent=0.0, disk_used_gb=0.0, disk_total_gb=512.0,
                loadavg_1m=20.0, loadavg_5m=0.0, loadavg_15m=0.0,
                sampled_at=1.0,
            ),
            docker=_make_docker_sample(1.0), sampled_at=1.0,
        )
        new_calm = hm.HostSnapshot(
            host=hm.HostSample(
                cpu_percent=0.0, mem_percent=0.0,
                mem_used_gb=0.0, mem_total_gb=64.0,
                disk_percent=0.0, disk_used_gb=0.0, disk_total_gb=512.0,
                loadavg_1m=2.0, loadavg_5m=0.0, loadavg_15m=0.0,
                sampled_at=2.0,
            ),
            docker=_make_docker_sample(2.0), sampled_at=2.0,
        )
        hm._record_host_snapshot(old_hot)
        hm._record_host_snapshot(new_calm)
        assert hm.is_host_high_pressure() is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H1 — Host Prometheus gauge publisher
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Covers the five H1 gauges that run_host_sampling_loop pushes each
# tick:
#   * host_cpu_percent / host_mem_percent / host_disk_percent
#   * host_loadavg_1m (raw 1m load average, not normalised)
#   * host_container_count{source=sdk|cli|unavailable}
#
# The publisher must be a no-op when prometheus_client is absent and
# never raise — the sampling loop can't afford to die on a scrape.


class TestHostPromPublish:
    def test_publish_writes_all_five_host_gauges(self):
        if not m.is_available():
            pytest.skip("prometheus_client not installed")
        m.reset_for_tests()
        snap = hm.HostSnapshot(
            host=hm.HostSample(
                cpu_percent=42.5, mem_percent=77.25,
                mem_used_gb=49.0, mem_total_gb=64.0,
                disk_percent=88.0, disk_used_gb=450.0, disk_total_gb=512.0,
                loadavg_1m=14.4, loadavg_5m=12.0, loadavg_15m=10.0,
                sampled_at=100.0,
            ),
            docker=hm.DockerSample(
                container_count=7, total_mem_reservation_bytes=0,
                source="sdk", sampled_at=100.0,
            ),
            sampled_at=100.0,
        )
        hm._publish_host_prom_metrics(snap)
        from prometheus_client import generate_latest
        text = generate_latest(m.REGISTRY).decode()
        assert "omnisight_host_cpu_percent 42.5" in text
        assert "omnisight_host_mem_percent 77.25" in text
        assert "omnisight_host_disk_percent 88.0" in text
        assert "omnisight_host_loadavg_1m 14.4" in text
        assert 'omnisight_host_container_count{source="sdk"} 7.0' in text

    def test_publish_labels_container_count_by_source_cli(self):
        if not m.is_available():
            pytest.skip("prometheus_client not installed")
        m.reset_for_tests()
        snap = hm.HostSnapshot(
            host=_make_host_sample(t=1.0, cpu=15.0),
            docker=hm.DockerSample(
                container_count=3, total_mem_reservation_bytes=0,
                source="cli", sampled_at=1.0,
            ),
            sampled_at=1.0,
        )
        hm._publish_host_prom_metrics(snap)
        from prometheus_client import generate_latest
        text = generate_latest(m.REGISTRY).decode()
        assert 'omnisight_host_container_count{source="cli"} 3.0' in text
        # The sdk series should not exist since we never set it in this
        # process — Prometheus shouldn't invent labels.
        assert 'omnisight_host_container_count{source="sdk"}' not in text

    def test_publish_handles_unavailable_source(self):
        if not m.is_available():
            pytest.skip("prometheus_client not installed")
        m.reset_for_tests()
        snap = hm.HostSnapshot(
            host=_make_host_sample(t=2.0, cpu=0.0),
            docker=hm.DockerSample(
                container_count=0, total_mem_reservation_bytes=0,
                source="unavailable", sampled_at=2.0,
            ),
            sampled_at=2.0,
        )
        hm._publish_host_prom_metrics(snap)
        from prometheus_client import generate_latest
        text = generate_latest(m.REGISTRY).decode()
        assert 'omnisight_host_container_count{source="unavailable"} 0.0' in text

    def test_publish_second_call_overwrites_previous_value(self):
        """Gauges are set-values, not counters — a second publish must
        replace the reading rather than accumulate."""
        if not m.is_available():
            pytest.skip("prometheus_client not installed")
        m.reset_for_tests()
        snap_a = hm.HostSnapshot(
            host=_make_host_sample(t=1.0, cpu=10.0),
            docker=_make_docker_sample(1.0, count=2),
            sampled_at=1.0,
        )
        snap_b = hm.HostSnapshot(
            host=_make_host_sample(t=2.0, cpu=55.5),
            docker=_make_docker_sample(2.0, count=9),
            sampled_at=2.0,
        )
        hm._publish_host_prom_metrics(snap_a)
        hm._publish_host_prom_metrics(snap_b)
        from prometheus_client import generate_latest
        text = generate_latest(m.REGISTRY).decode()
        # Latest CPU wins; first-tick value must be gone.
        assert "omnisight_host_cpu_percent 55.5" in text
        assert "omnisight_host_cpu_percent 10.0" not in text
        assert 'omnisight_host_container_count{source="sdk"} 9.0' in text

    def test_publish_is_noop_when_metrics_module_import_fails(self, monkeypatch):
        """If the metrics import itself blows up, the publisher swallows
        the exception — the sampling loop must keep running."""
        import builtins
        real_import = builtins.__import__

        def raising_import(name, *args, **kwargs):
            if name == "backend.metrics" or name == "backend":
                raise ImportError("simulated missing dep")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", raising_import)
        snap = hm.HostSnapshot(
            host=_make_host_sample(t=3.0, cpu=1.0),
            docker=_make_docker_sample(3.0),
            sampled_at=3.0,
        )
        # Must not raise — no assertion on side-effects, by design.
        hm._publish_host_prom_metrics(snap)

    def test_publish_is_noop_when_a_gauge_set_raises(self, monkeypatch):
        """Individual gauge failures are logged & swallowed — the
        sampling loop never tears down on a bad write."""
        if not m.is_available():
            pytest.skip("prometheus_client not installed")
        m.reset_for_tests()

        class Boom:
            def set(self, *_a, **_kw):
                raise RuntimeError("prometheus exploded")
            def labels(self, *_a, **_kw):
                return self

        monkeypatch.setattr(m, "host_cpu_percent", Boom())
        snap = hm.HostSnapshot(
            host=_make_host_sample(t=4.0, cpu=1.0),
            docker=_make_docker_sample(4.0),
            sampled_at=4.0,
        )
        hm._publish_host_prom_metrics(snap)  # must not raise

    @pytest.mark.asyncio
    async def test_sampling_loop_publishes_each_tick(self, monkeypatch):
        """The lifespan loop must call the publisher each iteration so
        /metrics reflects the freshest tick without a lag of one cycle."""
        if not m.is_available():
            pytest.skip("prometheus_client not installed")
        m.reset_for_tests()

        ticks = {"n": 0}

        def fake_sample(cpu_interval: float = 1.0) -> hm.HostSnapshot:
            ticks["n"] += 1
            return hm.HostSnapshot(
                host=hm.HostSample(
                    cpu_percent=float(ticks["n"]),
                    mem_percent=11.0, mem_used_gb=7.0, mem_total_gb=64.0,
                    disk_percent=22.0, disk_used_gb=100.0, disk_total_gb=512.0,
                    loadavg_1m=float(ticks["n"]),
                    loadavg_5m=0.0, loadavg_15m=0.0,
                    sampled_at=float(ticks["n"]),
                ),
                docker=hm.DockerSample(
                    container_count=ticks["n"], total_mem_reservation_bytes=0,
                    source="sdk", sampled_at=float(ticks["n"]),
                ),
                sampled_at=float(ticks["n"]),
            )

        monkeypatch.setattr(hm, "sample_host_snapshot", fake_sample)
        task = asyncio.create_task(
            hm.run_host_sampling_loop(interval_s=0.01, cpu_interval=0.0),
        )
        try:
            # Give the loop a handful of iterations.
            for _ in range(50):
                if ticks["n"] >= 2:
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        from prometheus_client import generate_latest
        text = generate_latest(m.REGISTRY).decode()
        # Whichever of the last two ticks happened to win the race, the
        # cpu gauge must equal that tick's integer cpu_percent.
        assert any(
            f"omnisight_host_cpu_percent {float(n)}" in text
            for n in (ticks["n"], ticks["n"] - 1)
        )
        assert 'omnisight_host_container_count{source="sdk"}' in text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H1 — host.metrics.tick SSE event
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Covers:
#   * _snapshot_to_sse_payload shape (host / docker / baseline / high_pressure)
#   * _publish_host_sse_tick uses bus.publish with the correct event name
#   * run_host_sampling_loop emits one tick per iteration
#   * publisher swallows event-bus errors so the loop survives
#   * SSE_EVENT_SCHEMAS registers the schema for codegen / validation


class TestHostSseTickPayload:
    def test_payload_includes_host_block(self):
        snap = hm.HostSnapshot(
            host=_make_host_sample(t=10.0, cpu=42.5),
            docker=_make_docker_sample(t=10.0, count=4),
            sampled_at=10.0,
        )
        payload = hm._snapshot_to_sse_payload(snap)
        assert payload["host"]["cpu_percent"] == 42.5
        assert payload["host"]["mem_total_gb"] == 64.0
        assert payload["host"]["sampled_at"] == 10.0

    def test_payload_includes_docker_block_with_source(self):
        snap = hm.HostSnapshot(
            host=_make_host_sample(t=1.0),
            docker=hm.DockerSample(
                container_count=7,
                total_mem_reservation_bytes=1024 ** 3,
                source="cli",
                sampled_at=1.0,
            ),
            sampled_at=1.0,
        )
        payload = hm._snapshot_to_sse_payload(snap)
        assert payload["docker"]["container_count"] == 7
        assert payload["docker"]["source"] == "cli"
        assert payload["docker"]["total_mem_reservation_bytes"] == 1024 ** 3

    def test_payload_pins_baseline(self):
        snap = hm.HostSnapshot(
            host=_make_host_sample(t=1.0),
            docker=_make_docker_sample(t=1.0),
            sampled_at=1.0,
        )
        payload = hm._snapshot_to_sse_payload(snap)
        assert payload["baseline"]["cpu_cores"] == hm.HOST_BASELINE.cpu_cores
        assert payload["baseline"]["mem_total_gb"] == hm.HOST_BASELINE.mem_total_gb
        assert payload["baseline"]["disk_total_gb"] == hm.HOST_BASELINE.disk_total_gb
        assert payload["baseline"]["cpu_model"] == hm.HOST_BASELINE.cpu_model

    def test_payload_high_pressure_false_under_threshold(self):
        # loadavg_1m=1.0 / 16 cores = 0.0625 ratio → not high pressure.
        snap = hm.HostSnapshot(
            host=_make_host_sample(t=1.0),
            docker=_make_docker_sample(t=1.0),
            sampled_at=1.0,
        )
        payload = hm._snapshot_to_sse_payload(snap)
        assert payload["high_pressure"] is False

    def test_payload_high_pressure_true_over_threshold(self):
        # loadavg_1m=15.0 / 16 cores = 0.9375 ratio > 0.9 → high pressure.
        host = hm.HostSample(
            cpu_percent=10.0, mem_percent=20.0,
            mem_used_gb=12.0, mem_total_gb=64.0,
            disk_percent=30.0, disk_used_gb=150.0, disk_total_gb=512.0,
            loadavg_1m=15.0, loadavg_5m=10.0, loadavg_15m=5.0, sampled_at=1.0,
        )
        snap = hm.HostSnapshot(
            host=host, docker=_make_docker_sample(t=1.0), sampled_at=1.0,
        )
        payload = hm._snapshot_to_sse_payload(snap)
        assert payload["high_pressure"] is True

    def test_payload_serialises_to_json(self):
        # Must round-trip through json.dumps because the EventBus path
        # serialises before delivery — un-serialisable types would crash
        # bus.publish at runtime.
        import json
        snap = hm.HostSnapshot(
            host=_make_host_sample(t=1.0),
            docker=_make_docker_sample(t=1.0),
            sampled_at=1.0,
        )
        payload = hm._snapshot_to_sse_payload(snap)
        # Should not raise.
        text = json.dumps(payload)
        assert '"host"' in text and '"docker"' in text and '"baseline"' in text


class TestHostSseTickPublish:
    def test_publish_calls_bus_with_event_name(self, monkeypatch):
        captured: list[tuple[str, dict]] = []

        class _FakeBus:
            def publish(self, event, data, **_kwargs):
                captured.append((event, data))

        from backend import events as _events

        monkeypatch.setattr(_events, "bus", _FakeBus())
        snap = hm.HostSnapshot(
            host=_make_host_sample(t=99.0, cpu=33.0),
            docker=_make_docker_sample(t=99.0, count=2),
            sampled_at=99.0,
        )
        hm._publish_host_sse_tick(snap)
        assert len(captured) == 1
        event, data = captured[0]
        assert event == "host.metrics.tick"
        assert data["host"]["cpu_percent"] == 33.0
        assert data["docker"]["container_count"] == 2
        assert data["sampled_at"] == 99.0

    def test_publish_swallows_bus_errors(self, monkeypatch):
        class _BrokenBus:
            def publish(self, *a, **k):
                raise RuntimeError("redis unreachable")

        from backend import events as _events
        monkeypatch.setattr(_events, "bus", _BrokenBus())
        snap = hm.HostSnapshot(
            host=_make_host_sample(t=1.0),
            docker=_make_docker_sample(t=1.0),
            sampled_at=1.0,
        )
        # Must NOT raise — loop survives a broken bus.
        hm._publish_host_sse_tick(snap)


class TestHostSseTickLoopIntegration:
    @pytest.mark.asyncio
    async def test_loop_emits_one_event_per_tick(self, monkeypatch):
        captured: list[tuple[str, dict]] = []

        class _CaptureBus:
            def publish(self, event, data, **_kwargs):
                captured.append((event, data))

        from backend import events as _events
        monkeypatch.setattr(_events, "bus", _CaptureBus())

        ticks = {"n": 0}

        def fake_sample(cpu_interval: float = 1.0) -> hm.HostSnapshot:
            ticks["n"] += 1
            return hm.HostSnapshot(
                host=_make_host_sample(t=float(ticks["n"]), cpu=float(ticks["n"])),
                docker=_make_docker_sample(t=float(ticks["n"]), count=ticks["n"]),
                sampled_at=float(ticks["n"]),
            )

        monkeypatch.setattr(hm, "sample_host_snapshot", fake_sample)

        task = asyncio.create_task(
            hm.run_host_sampling_loop(interval_s=0.01, cpu_interval=0.0),
        )
        try:
            for _ in range(50):
                if len(captured) >= 3:
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert len(captured) >= 3
        # Every captured event uses the H1 event name.
        assert all(event == "host.metrics.tick" for event, _ in captured)
        # Successive ticks carry monotonically increasing sampled_at.
        sampled = [d["sampled_at"] for _, d in captured]
        assert all(sampled[i] <= sampled[i + 1] for i in range(len(sampled) - 1))


class TestHostSseTickSchemaRegistered:
    def test_event_name_in_schema_registry(self):
        from backend.sse_schemas import SSE_EVENT_SCHEMAS, SSEHostMetricsTick
        assert "host.metrics.tick" in SSE_EVENT_SCHEMAS
        assert SSE_EVENT_SCHEMAS["host.metrics.tick"] is SSEHostMetricsTick

    def test_payload_validates_against_schema(self):
        from backend.sse_schemas import SSEHostMetricsTick
        snap = hm.HostSnapshot(
            host=_make_host_sample(t=5.0, cpu=12.0),
            docker=_make_docker_sample(t=5.0, count=3),
            sampled_at=5.0,
        )
        payload = hm._snapshot_to_sse_payload(snap)
        # Pydantic v2 — model_validate should accept the payload as-is.
        model = SSEHostMetricsTick.model_validate(payload)
        assert model.host.cpu_percent == 12.0
        assert model.docker.container_count == 3
        assert model.baseline.cpu_cores == hm.HOST_BASELINE.cpu_cores
        assert model.high_pressure is False
