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
