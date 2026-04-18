"""G7 HA-07 #1 — Prometheus metrics + helpers contract lock.

The HA-observability layer must expose exactly the four signals the
charter commits to:

    * ``omnisight_backend_instance_up``
    * ``omnisight_rolling_deploy_5xx_rate``
      (+ its source-of-truth counter ``omnisight_rolling_deploy_responses_total``)
    * ``omnisight_replica_lag_seconds``
    * ``omnisight_readyz_latency_seconds``

This file is the contract lock: it asserts the metric names, the
declared label sets, the NoOp-fallback contract for prometheus-less
environments, and the end-to-end behaviour of the helper module
(status classification, rolling-window rate, replica-lag clamp,
readyz histogram, /readyz wiring, and middleware recording).

Principles (Step 4 SOP):
  * Scientific: each assertion checks ONE invariant.
  * Minimal: no network, no sleeping. Time is injected.
  * Isolated: rolling-window state is reset between tests.
  * Fast: whole file runs under a second.
"""

from __future__ import annotations

import importlib

import pytest

from backend import metrics as m


prom_only = pytest.mark.skipif(
    not m.is_available(), reason="prometheus_client not installed"
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  A — metric registration + label shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@prom_only
def test_reset_for_tests_rebinds_all_four_g7_metrics():
    """After reset_for_tests(), every G7 HA-07 metric attr must re-bind
    to a live collector (not stay pointed at the previous registry)."""
    prev = {
        "backend_instance_up": m.backend_instance_up,
        "rolling_deploy_responses_total": m.rolling_deploy_responses_total,
        "rolling_deploy_5xx_rate": m.rolling_deploy_5xx_rate,
        "replica_lag_seconds": m.replica_lag_seconds,
        "readyz_latency_seconds": m.readyz_latency_seconds,
    }
    m.reset_for_tests()
    for name, old in prev.items():
        new = getattr(m, name)
        assert new is not None, f"{name} unbound after reset"
        assert new is not old, f"{name} still pointing at stale collector"


@prom_only
@pytest.mark.parametrize("name,labels,op", [
    ("backend_instance_up", {"instance_id": "pod-0"}, "set"),
    ("rolling_deploy_responses_total", {"status_class": "5xx"}, "inc"),
    ("replica_lag_seconds", {"replica": "standby_1"}, "set"),
    ("readyz_latency_seconds", {"outcome": "ready"}, "observe"),
])
def test_labelled_g7_metric_accepts_declared_labels(name, labels, op):
    """Each labelled G7 metric must accept exactly its declared label set."""
    m.reset_for_tests()
    child = getattr(m, name).labels(**labels)
    if op == "set":
        child.set(1)
    elif op == "inc":
        child.inc()
    elif op == "observe":
        child.observe(0.05)


@prom_only
def test_rolling_deploy_5xx_rate_is_unlabelled_gauge():
    """The convenience rate is an unlabelled scalar — Grafana alert
    rules that fire off `rolling_deploy_5xx_rate > 0.01` would break
    if we silently added a label dim."""
    m.reset_for_tests()
    m.rolling_deploy_5xx_rate.set(0.42)
    samples = list(m.rolling_deploy_5xx_rate.collect()[0].samples)
    assert any(abs(s.value - 0.42) < 1e-9 for s in samples)


@prom_only
@pytest.mark.parametrize("name,bogus_label", [
    ("backend_instance_up", {"pod": "p-0"}),              # expects instance_id
    ("rolling_deploy_responses_total", {"status": "5xx"}),  # expects status_class
    ("replica_lag_seconds", {"host": "s1"}),              # expects replica
    ("readyz_latency_seconds", {"verdict": "ready"}),     # expects outcome
])
def test_g7_labelled_metric_rejects_unknown_label(name, bogus_label):
    m.reset_for_tests()
    with pytest.raises(Exception):  # ValueError from prometheus_client
        getattr(m, name).labels(**bogus_label)


@prom_only
def test_registry_collects_all_four_g7_metric_families():
    """`/metrics` exposition must list all four HA-07 family names."""
    m.reset_for_tests()
    # Register at least one labelled sample so the family actually
    # appears in collect() output.
    m.backend_instance_up.labels(instance_id="p").set(1)
    m.rolling_deploy_responses_total.labels(status_class="2xx").inc()
    m.replica_lag_seconds.labels(replica="s1").set(0)
    m.readyz_latency_seconds.labels(outcome="ready").observe(0.01)
    body, _ = m.render_exposition()
    text = body.decode()
    for name in (
        "omnisight_backend_instance_up",
        "omnisight_rolling_deploy_responses_total",
        "omnisight_rolling_deploy_5xx_rate",
        "omnisight_replica_lag_seconds",
        "omnisight_readyz_latency_seconds",
    ):
        assert name in text, f"{name} missing from /metrics exposition"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  B — NoOp fallback contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_noop_stubs_support_labels_and_ops():
    """Even when prometheus_client is unavailable, every G7 metric
    must still chain ``.labels(...).inc|set|observe(...)`` without
    raising — call sites are NOT expected to guard every bump."""
    mod = importlib.import_module("backend.metrics")
    _NoOp = None
    for name in dir(mod):
        obj = getattr(mod, name)
        if obj.__class__.__name__ == "_NoOp":
            _NoOp = obj.__class__
            break
    if _NoOp is None:
        if m.is_available():
            pytest.skip("prom installed → _NoOp stub not under test")
        pytest.fail("_NoOp class not found even though prom unavailable")
    stub = _NoOp()
    # Mirror every call shape the G7 helpers use:
    stub.labels(instance_id="p-0").set(1)
    stub.labels(status_class="5xx").inc()
    stub.labels(replica="s1").set(1.5)
    stub.labels(outcome="ready").observe(0.01)
    stub.set(0.5)  # rolling_deploy_5xx_rate convenience gauge
    stub.inc()
    stub.dec()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  C — instance identity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_get_instance_id_prefers_env_over_hostname(monkeypatch):
    from backend import ha_observability as h
    monkeypatch.setenv("OMNISIGHT_INSTANCE_ID", "override-id-7")
    monkeypatch.setenv("HOSTNAME", "should-be-ignored")
    assert h.get_instance_id() == "override-id-7"


def test_get_instance_id_falls_back_to_hostname_env(monkeypatch):
    from backend import ha_observability as h
    monkeypatch.delenv("OMNISIGHT_INSTANCE_ID", raising=False)
    monkeypatch.setenv("HOSTNAME", "pod-backend-3")
    assert h.get_instance_id() == "pod-backend-3"


def test_get_instance_id_last_resort_socket_gethostname(monkeypatch):
    import socket
    from backend import ha_observability as h
    monkeypatch.delenv("OMNISIGHT_INSTANCE_ID", raising=False)
    monkeypatch.delenv("HOSTNAME", raising=False)
    got = h.get_instance_id()
    assert got == (socket.gethostname() or "unknown")


@prom_only
def test_mark_instance_up_sets_gauge_to_one(monkeypatch):
    m.reset_for_tests()
    from backend import ha_observability as h
    monkeypatch.setenv("OMNISIGHT_INSTANCE_ID", "test-up")
    h.mark_instance_up()
    samples = list(m.backend_instance_up.collect()[0].samples)
    hit = [s for s in samples if s.labels.get("instance_id") == "test-up"]
    assert hit and hit[0].value == 1.0


@prom_only
def test_mark_instance_down_sets_gauge_to_zero(monkeypatch):
    m.reset_for_tests()
    from backend import ha_observability as h
    monkeypatch.setenv("OMNISIGHT_INSTANCE_ID", "test-down")
    h.mark_instance_up()
    h.mark_instance_down()
    samples = list(m.backend_instance_up.collect()[0].samples)
    hit = [s for s in samples if s.labels.get("instance_id") == "test-down"]
    assert hit and hit[0].value == 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  D — rolling 5xx tracker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture(autouse=True)
def _reset_rolling_window():
    """Isolate each test from previous window state."""
    m.reset_for_tests()
    from backend import ha_observability as h
    h.reset_rolling_window()
    yield
    h.reset_rolling_window()


@pytest.mark.parametrize("status,cls", [
    (200, "2xx"), (204, "2xx"), (299, "2xx"),
    (301, "3xx"), (304, "3xx"),
    (400, "4xx"), (404, "4xx"), (499, "4xx"),
    (500, "5xx"), (502, "5xx"), (599, "5xx"),
    (0, "5xx"),     # out-of-range bucketed into 5xx
    (700, "5xx"),   # out-of-range bucketed into 5xx
])
def test_status_class_boundaries(status, cls):
    from backend.ha_observability import _status_class
    assert _status_class(status) == cls


@prom_only
def test_record_http_response_increments_counter():
    from backend import ha_observability as h
    h.record_http_response(200)
    h.record_http_response(500)
    h.record_http_response(500)
    samples = {
        s.labels["status_class"]: s.value
        for s in m.rolling_deploy_responses_total.collect()[0].samples
        if s.name.endswith("_total")
    }
    assert samples.get("2xx") == 1
    assert samples.get("5xx") == 2


def test_current_5xx_rate_zero_when_no_traffic():
    from backend import ha_observability as h
    assert h.current_5xx_rate(now=1000.0) == 0.0


def test_current_5xx_rate_one_when_all_5xx():
    from backend import ha_observability as h
    for _ in range(5):
        h.record_http_response(503, now=1000.0)
    assert h.current_5xx_rate(now=1000.5) == 1.0


def test_current_5xx_rate_half_when_half_5xx():
    from backend import ha_observability as h
    h.record_http_response(200, now=1000.0)
    h.record_http_response(500, now=1000.0)
    h.record_http_response(200, now=1000.0)
    h.record_http_response(500, now=1000.0)
    assert h.current_5xx_rate(now=1000.5) == 0.5


def test_rolling_window_prunes_samples_older_than_60s():
    """Samples outside the 60 s window must not count."""
    from backend import ha_observability as h
    # t=1000: 10 × 5xx (will fall out of window)
    for _ in range(10):
        h.record_http_response(500, now=1000.0)
    # Jump forward 90s: 1 × 2xx (fresh)
    h.record_http_response(200, now=1090.0)
    rate = h.current_5xx_rate(now=1090.0)
    assert rate == 0.0, f"expected expired 5xx samples to be pruned, got {rate}"


def test_rolling_window_keeps_samples_within_60s():
    """Samples still inside the window must still be counted."""
    from backend import ha_observability as h
    h.record_http_response(500, now=1000.0)
    h.record_http_response(200, now=1030.0)  # 30s later — still in window
    rate = h.current_5xx_rate(now=1030.0)
    assert rate == 0.5


@prom_only
def test_record_http_response_updates_rate_gauge():
    from backend import ha_observability as h
    h.record_http_response(200, now=1000.0)
    h.record_http_response(500, now=1000.0)
    samples = list(m.rolling_deploy_5xx_rate.collect()[0].samples)
    # Only one sample on an unlabelled gauge
    assert len(samples) == 1
    assert abs(samples[0].value - 0.5) < 1e-9


def test_reset_rolling_window_clears_state():
    from backend import ha_observability as h
    h.record_http_response(500, now=1000.0)
    h.record_http_response(500, now=1000.0)
    assert h.current_5xx_rate(now=1000.0) == 1.0
    h.reset_rolling_window()
    assert h.current_5xx_rate(now=1000.0) == 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  E — replica lag
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@prom_only
def test_update_replica_lag_sets_gauge_per_replica():
    from backend import ha_observability as h
    h.update_replica_lag("standby_a", 2.5)
    h.update_replica_lag("standby_b", 0.1)
    samples = {
        s.labels["replica"]: s.value
        for s in m.replica_lag_seconds.collect()[0].samples
    }
    assert samples["standby_a"] == 2.5
    assert samples["standby_b"] == 0.1


@prom_only
def test_update_replica_lag_clamps_negative_to_zero():
    """A negative lag from a mis-configured sampler must not show up
    as a negative gauge (PromQL rate() would lie) — clamp to 0."""
    from backend import ha_observability as h
    h.update_replica_lag("standby_weird", -3.0)
    samples = {
        s.labels["replica"]: s.value
        for s in m.replica_lag_seconds.collect()[0].samples
    }
    assert samples["standby_weird"] == 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  F — /readyz latency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@prom_only
def test_observe_readyz_latency_emits_histogram_sample():
    from backend import ha_observability as h
    with h.observe_readyz_latency(outcome="ready"):
        pass  # instant block — histogram bucket still ticks
    count = 0
    for s in m.readyz_latency_seconds.collect()[0].samples:
        if s.name.endswith("_count") and s.labels.get("outcome") == "ready":
            count = int(s.value)
    assert count == 1


@prom_only
def test_observe_readyz_latency_draining_outcome_label():
    from backend import ha_observability as h
    with h.observe_readyz_latency(outcome="draining"):
        pass
    drained = [
        s for s in m.readyz_latency_seconds.collect()[0].samples
        if s.name.endswith("_count") and s.labels.get("outcome") == "draining"
    ]
    assert drained and int(drained[0].value) == 1


@prom_only
def test_readyz_latency_histogram_has_sub_second_buckets():
    """The probe is expected to complete in <1s on a healthy replica;
    buckets below 1s must exist so p95/p99 is meaningful."""
    m.reset_for_tests()
    from backend import ha_observability as h
    # Force at least one sample so _bucket lines appear.
    with h.observe_readyz_latency(outcome="ready"):
        pass
    bucket_values = [
        float(s.labels.get("le"))
        for s in m.readyz_latency_seconds.collect()[0].samples
        if s.name.endswith("_bucket") and s.labels.get("le", "+Inf") != "+Inf"
    ]
    assert any(b <= 0.05 for b in bucket_values), (
        f"no sub-50ms bucket: {sorted(set(bucket_values))}"
    )
    assert any(b >= 1.0 for b in bucket_values), (
        f"no ≥1s bucket: {sorted(set(bucket_values))}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  G — HTTP middleware behaviour
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@prom_only
def test_register_middleware_records_status_classes_end_to_end():
    """Build a minimal FastAPI app, register our middleware, hit it
    with a TestClient, and confirm the 5xx counter moved."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from backend import ha_observability as h

    app = FastAPI()

    @app.get("/ok")
    async def _ok():
        return {"ok": True}

    @app.get("/boom")
    async def _boom():
        return JSONResponse(status_code=503, content={"err": "boom"})

    h.register_middleware(app)
    client = TestClient(app)

    assert client.get("/ok").status_code == 200
    assert client.get("/boom").status_code == 503
    assert client.get("/boom").status_code == 503

    samples = {
        s.labels["status_class"]: s.value
        for s in m.rolling_deploy_responses_total.collect()[0].samples
        if s.name.endswith("_total")
    }
    assert samples.get("2xx") == 1
    assert samples.get("5xx") == 2


@prom_only
def test_middleware_records_unhandled_exception_as_5xx():
    """Routes that raise must still show up in the 5xx counter —
    Starlette's exception handler fires AFTER our middleware records."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend import ha_observability as h

    app = FastAPI()

    @app.get("/raise")
    async def _raise():
        raise RuntimeError("kaboom")

    h.register_middleware(app)
    # raise_server_exceptions=False so TestClient lets the 500 reach us
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/raise")
    assert resp.status_code == 500

    samples = {
        s.labels["status_class"]: s.value
        for s in m.rolling_deploy_responses_total.collect()[0].samples
        if s.name.endswith("_total")
    }
    assert samples.get("5xx") == 1
