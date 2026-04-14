"""Phase 63-A — IIS signal-layer tests.

Pure-function tests at the IntelligenceWindow level + lightweight
publisher checks. No mitigation / Decision Engine wiring asserted
here (that's 63-B).
"""

from __future__ import annotations

import pytest

from backend import intelligence as iis


@pytest.fixture(autouse=True)
def _reset():
    iis.reset_for_tests()
    yield
    iis.reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tokeniser + Jaccard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_jaccard_identical_strings_is_one():
    assert iis.jaccard("hello world foo", "hello world foo") == 1.0


def test_jaccard_disjoint_is_zero():
    assert iis.jaccard("alpha beta", "gamma delta") == 0.0


def test_jaccard_empty_both_is_one_not_zero():
    """Avoid spurious "regression" alerts when both sides have no
    extractable tokens (e.g. just punctuation)."""
    assert iis.jaccard("", "") == 1.0
    assert iis.jaccard("..!", "??") == 1.0


def test_jaccard_one_empty_is_zero():
    assert iis.jaccard("hello world", "") == 0.0


def test_jaccard_partial_overlap():
    # 2 common (hello, world) / 4 union (hello, world, foo, bar) = 0.5
    assert iis.jaccard("hello world foo", "hello world bar") == 0.5


def test_tokenise_drops_numbers_and_punct():
    tokens = iis._tokenise("Hello, 42 _world! foo123_bar")
    assert tokens == {"hello", "_world", "foo123_bar"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  IntelligenceWindow.record + score
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_empty_window_returns_none_for_each_dim():
    w = iis.IntelligenceWindow(agent_id="a1")
    s = w.score()
    assert s == {"code_pass": None, "compliance": None,
                 "consistency": None, "entropy": None}


def test_record_with_all_none_is_noop():
    w = iis.IntelligenceWindow(agent_id="a1")
    w.record()  # all defaults None
    assert len(w._entries) == 0


def test_code_pass_rate_basic():
    w = iis.IntelligenceWindow(agent_id="a1")
    for ok in [True, True, False, True, False]:
        w.record(code_pass=ok)
    assert w.code_pass_rate() == pytest.approx(0.6)


def test_code_pass_rate_ignores_unrelated_records():
    """Recording only response_tokens shouldn't pollute pass-rate."""
    w = iis.IntelligenceWindow(agent_id="a1")
    w.record(response_tokens=100)
    w.record(code_pass=True)
    w.record(code_pass=False)
    assert w.code_pass_rate() == pytest.approx(0.5)


def test_compliance_rate_average():
    w = iis.IntelligenceWindow(agent_id="a1")
    w.record(compliance=True)
    w.record(compliance=True)
    w.record(compliance=False)
    w.record(compliance=False)
    assert w.compliance_rate() == pytest.approx(0.5)


def test_logic_consistency_average():
    w = iis.IntelligenceWindow(agent_id="a1")
    w.record(consistency=0.2)
    w.record(consistency=0.8)
    assert w.logic_consistency() == pytest.approx(0.5)


def test_token_entropy_z_needs_three_samples():
    w = iis.IntelligenceWindow(agent_id="a1")
    w.record(response_tokens=100)
    w.record(response_tokens=110)
    assert w.token_entropy_z() is None
    w.record(response_tokens=120)
    z = w.token_entropy_z()
    assert z is not None  # now defined


def test_token_entropy_z_constant_prior_returns_zero():
    w = iis.IntelligenceWindow(agent_id="a1")
    for _ in range(5):
        w.record(response_tokens=100)
    assert w.token_entropy_z() == 0.0


def test_token_entropy_z_detects_outlier_short():
    w = iis.IntelligenceWindow(agent_id="a1")
    for n in [100, 110, 105, 115, 95]:
        w.record(response_tokens=n)
    w.record(response_tokens=20)  # huge negative outlier
    z = w.token_entropy_z()
    assert z is not None
    assert z < -2.0  # well below warning threshold


def test_window_size_bounds_history():
    w = iis.IntelligenceWindow(agent_id="a1", size=3)
    for ok in [True, True, True, False]:
        w.record(code_pass=ok)
    # last 3: T, T, F → 2/3
    assert w.code_pass_rate() == pytest.approx(2 / 3)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Alerts (signal-only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_no_alerts_when_window_empty():
    w = iis.IntelligenceWindow(agent_id="a1")
    assert w.alerts() == []


def test_no_alerts_when_all_signals_healthy():
    w = iis.IntelligenceWindow(agent_id="a1")
    for _ in range(5):
        w.record(code_pass=True, compliance=True, consistency=0.8,
                 response_tokens=100)
    assert w.alerts() == []


def test_warning_alert_when_pass_rate_under_60_pct():
    w = iis.IntelligenceWindow(agent_id="a1")
    for ok in [True, False, False, False, True]:  # 40%
        w.record(code_pass=ok)
    levels = {(lvl, dim) for lvl, dim, _ in w.alerts()}
    assert ("warning", "code_pass") in levels


def test_critical_alert_when_pass_rate_under_30_pct():
    w = iis.IntelligenceWindow(agent_id="a1")
    for ok in [True, False, False, False, False]:  # 20%
        w.record(code_pass=ok)
    levels = [(lvl, dim) for lvl, dim, _ in w.alerts()]
    assert ("critical", "code_pass") in levels
    # We do NOT also emit "warning" for the same dim — escalation only.
    assert ("warning", "code_pass") not in levels


def test_compliance_warning_when_under_70_pct():
    w = iis.IntelligenceWindow(agent_id="a1")
    for ok in [True, False, False]:  # 33%
        w.record(compliance=ok)
    levels = {(lvl, dim) for lvl, dim, _ in w.alerts()}
    assert ("warning", "compliance") in levels


def test_consistency_warning_when_below_threshold():
    w = iis.IntelligenceWindow(agent_id="a1")
    for c in [0.1, 0.2, 0.15]:  # avg ≈ 0.15
        w.record(consistency=c)
    levels = {(lvl, dim) for lvl, dim, _ in w.alerts()}
    assert ("warning", "consistency") in levels


def test_entropy_warning_on_outlier():
    w = iis.IntelligenceWindow(agent_id="a1")
    for n in [100, 105, 95, 100, 102]:
        w.record(response_tokens=n)
    w.record(response_tokens=10)  # huge drop
    levels = {(lvl, dim) for lvl, dim, _ in w.alerts()}
    assert ("warning", "entropy") in levels


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-agent registry + record_and_publish
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_get_window_returns_singleton_per_agent():
    a = iis.get_window("a1")
    b = iis.get_window("a1")
    c = iis.get_window("a2")
    assert a is b
    assert a is not c


def test_record_and_publish_returns_score_and_alerts():
    iis.record_and_publish("a1", code_pass=True, compliance=True,
                           response_tokens=100)
    iis.record_and_publish("a1", code_pass=False, response_tokens=110)
    score, alerts = iis.record_and_publish(
        "a1", code_pass=False, response_tokens=120,
    )
    assert score["code_pass"] is not None
    assert isinstance(alerts, list)


def test_record_and_publish_increments_metric():
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()
    # Force a critical alert.
    for _ in range(5):
        iis.record_and_publish("a-metric-test", code_pass=False)
    samples = list(m.intelligence_alert_total.collect()[0].samples)
    crit = [
        s for s in samples
        if s.labels.get("agent_id") == "a-metric-test"
        and s.labels.get("dim") == "code_pass"
        and s.labels.get("level") == "critical"
        and s.name.endswith("_total")
    ]
    assert crit and crit[0].value >= 1


def test_record_and_publish_publishes_score_gauge():
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()
    for ok in [True, True, False, True]:
        iis.record_and_publish("a-gauge-test", code_pass=ok)
    samples = list(m.intelligence_score.collect()[0].samples)
    gauge = [
        s for s in samples
        if s.labels.get("agent_id") == "a-gauge-test"
        and s.labels.get("dim") == "code_pass"
    ]
    assert gauge and gauge[0].value == pytest.approx(0.75)
