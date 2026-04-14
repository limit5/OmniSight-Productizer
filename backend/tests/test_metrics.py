"""Fix-D D5 — metrics.py registry integrity + no-op fallback contract.

Two shape-of-the-world tests:

  1. When prometheus_client is installed, every declared metric must
     actually register, accept its declared labels, and show up in
     `REGISTRY.collect()`.
  2. The no-op `_NoOp` class (used when prometheus_client is absent)
     must expose the same callable surface we rely on in hot paths:
     `labels()`, `inc()`, `dec()`, `set()`, `observe()`.

If the registry drifts from what the code expects, /metrics will
either 500 or silently omit a counter. Both are bad; both show up
here first.
"""

from __future__ import annotations

import pytest

from backend import metrics as m


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Real registry — gated on prometheus_client
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

prom_only = pytest.mark.skipif(
    not m.is_available(), reason="prometheus_client not installed"
)


@prom_only
def test_reset_for_tests_replaces_registry():
    prev = m.REGISTRY
    m.reset_for_tests()
    assert m.REGISTRY is not prev
    # All module-level metric attrs must be rebound to live collectors.
    for name in ("decision_total", "decision_resolve_seconds",
                 "pipeline_step_seconds", "provider_failure_total",
                 "provider_latency_seconds", "sse_subscribers",
                 "sse_dropped_total", "workflow_step_total",
                 "auth_login_total", "persist_failure_total",
                 "subprocess_orphan_total", "process_start_time"):
        assert getattr(m, name) is not None, f"{name} unbound after reset"


@prom_only
@pytest.mark.parametrize("name,labels", [
    ("decision_total", {"kind": "test/x", "severity": "risky", "status": "pending"}),
    ("decision_resolve_seconds",
     {"kind": "test/x", "severity": "risky", "resolver": "user"}),
    ("pipeline_step_seconds",
     {"phase": "plan", "step": "a", "outcome": "success"}),
    ("provider_failure_total", {"provider": "openai", "reason": "rate_limit"}),
    ("provider_latency_seconds", {"provider": "openai", "model": "gpt-4"}),
    ("workflow_step_total", {"kind": "build", "outcome": "success"}),
    ("auth_login_total", {"outcome": "success"}),
    ("persist_failure_total", {"module": "notifications"}),
    ("subprocess_orphan_total", {"target": "jenkins"}),
])
def test_labelled_metrics_accept_declared_labels(name, labels):
    m.reset_for_tests()
    metric = getattr(m, name)
    # labels(**x) returns a bound child; .inc / .observe must work.
    child = metric.labels(**labels)
    if name.endswith("_seconds"):
        child.observe(0.1)
    else:
        child.inc()


@prom_only
def test_labelled_metric_rejects_unknown_label():
    m.reset_for_tests()
    with pytest.raises(Exception):  # ValueError from prometheus_client
        m.decision_total.labels(not_a_real_label="x")


@prom_only
def test_unlabelled_gauge_ops():
    m.reset_for_tests()
    m.sse_subscribers.set(7)
    samples = list(m.sse_subscribers.collect()[0].samples)
    assert any(s.value == 7 for s in samples)


@prom_only
def test_render_exposition_returns_text_and_content_type():
    m.reset_for_tests()
    m.decision_total.labels(kind="k", severity="risky", status="pending").inc()
    body, ctype = m.render_exposition()
    assert b"omnisight_decision_total" in body
    assert "text/plain" in ctype or "openmetrics" in ctype


@prom_only
def test_registry_has_all_declared_metrics_by_name():
    m.reset_for_tests()
    collected_names = {
        mf.name for mf in m.REGISTRY.collect()
    }
    # Counter / Gauge / Histogram may strip `_total` / `_seconds` suffix
    # depending on client version, so use a prefix match.
    expected = {
        "omnisight_decision",
        "omnisight_pipeline_step",
        "omnisight_provider_failure",
        "omnisight_provider_latency",
        "omnisight_sse_subscribers",
        "omnisight_sse_dropped",
        "omnisight_workflow_step",
        "omnisight_auth_login",
        "omnisight_persist_failure",
        "omnisight_subprocess_orphan",
        "omnisight_process_start_time",
    }
    for prefix in expected:
        hit = any(n.startswith(prefix) for n in collected_names)
        assert hit, f"metric {prefix} missing from registry"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  No-op fallback contract — runs regardless of prometheus_client
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_noop_stub_exposes_required_methods():
    """Every call site assumes `.labels(...).inc()` / `.observe(...)` /
    `.set(...)` all work. Build a fresh _NoOp here and verify."""
    import importlib

    # We can't easily toggle `_AVAILABLE` at runtime (module already
    # imported), so sniff the stub via a fresh import of the symbol.
    mod = importlib.import_module("backend.metrics")
    _NoOp = None
    for name in dir(mod):
        obj = getattr(mod, name)
        if obj.__class__.__name__ == "_NoOp":
            _NoOp = obj.__class__
            break
    if _NoOp is None:
        if m.is_available():
            pytest.skip("prom installed → _NoOp stub not used")
        pytest.fail("_NoOp class not found but prom also unavailable")

    stub = _NoOp()
    # Chaining must not raise.
    stub.labels(anything="here").inc()
    stub.labels(x=1).observe(0.5)
    stub.set(10)
    stub.dec()
    stub.inc(3)


def test_render_exposition_returns_plain_text_when_prom_unavailable(monkeypatch):
    if m.is_available():
        pytest.skip("prom installed")
    body, ctype = m.render_exposition()
    assert b"not installed" in body.lower() or body == b"# prometheus_client not installed\n"
    assert "text/plain" in ctype
