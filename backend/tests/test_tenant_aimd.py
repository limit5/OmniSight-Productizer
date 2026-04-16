"""M4 — tests for backend/tenant_aimd.py.

Covers the AIMD decision table:
  * HOT + single-culprit   → only that tenant derates
  * HOT + no outlier       → flat derate every running tenant
  * COOL + derated         → additive-increase toward baseline
  * HOLD path (warm band)
  * multiplier floor + ceiling
  * current_multiplier accessor
  * Prometheus tenant_derate_total bump
"""

from __future__ import annotations

import pytest

from backend import host_metrics as hm
from backend import tenant_aimd as ta


@pytest.fixture(autouse=True)
def _reset():
    ta._reset_for_tests()
    hm._reset_for_tests()
    yield
    ta._reset_for_tests()
    hm._reset_for_tests()


def _usage(tid: str, cpu: float, mem: float = 0.0) -> hm.TenantUsage:
    return hm.TenantUsage(tenant_id=tid, cpu_percent=cpu, mem_used_gb=mem)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HOT — single culprit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestHotCulprit:
    def test_single_culprit_derates_only_that_tenant(self):
        usage = {"tA": _usage("tA", 500.0), "tB": _usage("tB", 20.0)}
        plan = ta.plan_derate(host_cpu_pct=95.0, usage_by_tenant=usage)
        assert plan.reason == ta.DerateReason.CULPRIT
        assert plan.culprit_tenant_id == "tA"
        assert plan.affected == {"tA": 0.5}
        assert ta.current_multiplier("tA") == 0.5
        assert ta.current_multiplier("tB") == 1.0

    def test_repeated_hot_halves_again(self):
        usage = {"tA": _usage("tA", 500.0), "tB": _usage("tB", 20.0)}
        ta.plan_derate(95.0, usage)
        plan = ta.plan_derate(95.0, usage)
        assert plan.reason == ta.DerateReason.CULPRIT
        assert ta.current_multiplier("tA") == 0.25

    def test_floor_respected(self):
        usage = {"tA": _usage("tA", 500.0), "tB": _usage("tB", 20.0)}
        for _ in range(20):
            ta.plan_derate(95.0, usage)
        assert ta.current_multiplier("tA") >= ta.AimdConfig().min_multiplier


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HOT — no clear outlier → flat derate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestHotFlat:
    def test_flat_derates_both_tenants(self):
        usage = {"tA": _usage("tA", 200.0), "tB": _usage("tB", 190.0)}
        plan = ta.plan_derate(95.0, usage)
        assert plan.reason == ta.DerateReason.FLAT
        assert plan.culprit_tenant_id is None
        assert set(plan.affected) == {"tA", "tB"}
        assert plan.affected["tA"] == 0.5
        assert plan.affected["tB"] == 0.5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  COOL — recover
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCool:
    def test_additive_increase_when_cool(self):
        # Derate once, then go cool.
        ta.plan_derate(95.0, {"tA": _usage("tA", 500.0), "tB": _usage("tB", 20.0)})
        assert ta.current_multiplier("tA") == 0.5
        plan = ta.plan_derate(40.0, {"tA": _usage("tA", 20.0)})
        assert plan.reason == ta.DerateReason.RECOVER
        assert ta.current_multiplier("tA") == pytest.approx(0.55)

    def test_recover_caps_at_baseline(self):
        ta.plan_derate(95.0, {"tA": _usage("tA", 500.0), "tB": _usage("tB", 20.0)})
        # Many recover cycles
        for _ in range(100):
            ta.plan_derate(40.0, {})
        assert ta.current_multiplier("tA") == 1.0

    def test_recover_tenants_absent_from_usage_still_climb(self):
        """Idle-but-derated tenants aren't frozen at their reduced multiplier
        just because they don't show up in the current sample."""
        ta.plan_derate(95.0, {"tA": _usage("tA", 500.0), "tB": _usage("tB", 20.0)})
        before = ta.current_multiplier("tA")
        plan = ta.plan_derate(40.0, {})  # empty snapshot this cycle
        assert plan.reason == ta.DerateReason.RECOVER
        assert ta.current_multiplier("tA") > before


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HOLD — warm band / no change
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestHold:
    def test_warm_band_noop(self):
        plan = ta.plan_derate(70.0, {"tA": _usage("tA", 50.0)})
        assert plan.reason == ta.DerateReason.HOLD
        assert plan.affected == {}

    def test_cool_but_all_at_baseline(self):
        """Cool + nobody is derated → nothing to recover → HOLD."""
        plan = ta.plan_derate(40.0, {})
        assert plan.reason == ta.DerateReason.HOLD


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  current_multiplier for unseen tenants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCurrentMultiplier:
    def test_unseen_tenant_defaults_to_baseline(self):
        assert ta.current_multiplier("never-seen") == 1.0

    def test_snapshot_lists_only_touched_tenants(self):
        ta.plan_derate(95.0, {"tA": _usage("tA", 500.0), "tB": _usage("tB", 20.0)})
        snap = {s.tenant_id: s for s in ta.snapshot()}
        assert "tA" in snap
        assert snap["tA"].multiplier == 0.5
        assert snap["tA"].last_reason == ta.DerateReason.CULPRIT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config overrides
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfigOverrides:
    def test_custom_md_factor(self):
        cfg = ta.AimdConfig(md_factor=0.25)
        usage = {"tA": _usage("tA", 500.0), "tB": _usage("tB", 20.0)}
        ta.plan_derate(95.0, usage, config=cfg)
        assert ta.current_multiplier("tA") == 0.25

    def test_hot_threshold_not_crossed(self):
        cfg = ta.AimdConfig(host_hot_cpu_pct=99.0)
        plan = ta.plan_derate(95.0, {"tA": _usage("tA", 500.0)}, config=cfg)
        assert plan.reason != ta.DerateReason.CULPRIT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Prometheus counter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPromCounter:
    def test_tenant_derate_total_fires(self):
        from backend import metrics as m
        if not m.is_available():
            pytest.skip("prometheus_client not installed")
        m.reset_for_tests()
        ta.plan_derate(95.0, {"tA": _usage("tA", 500.0), "tB": _usage("tB", 20.0)})
        from prometheus_client import generate_latest
        text = generate_latest(m.REGISTRY).decode()
        assert 'omnisight_tenant_derate_total{reason="culprit",tenant_id="tA"} 1.0' in text
