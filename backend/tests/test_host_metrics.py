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

import os
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
