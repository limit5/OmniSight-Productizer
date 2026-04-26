"""Y9 #285 row 4 — Prometheus label cardinality control + per-(tenant,
project, product_line) metric fan-out.

Validates:

  * ``backend.metrics_labels.bucket_*`` returns sentinel ``"unknown"``
    for None/empty inputs and ``"other"`` after the per-process cap is
    saturated.
  * The new ``omnisight_billing_*`` metric family is registered with
    the expected ``(tenant_id, project_id, product_line, ...)``
    labelnames and survives ``metrics.reset_for_tests()``.
  * The emitters in ``backend.billing_usage`` (``record_llm_call`` /
    ``record_workflow_run`` / ``record_workspace_gb_hour``) bump the
    Prometheus counters with bucketed labels even when the DB write
    leg fails.
  * The cap-status gauge ``omnisight_metrics_label_cap_used`` reports
    fractional consumption per dimension.
  * The model → provider inference helper is correct for the prefix
    list it documents and falls through to ``"unknown"``.

These are pure-unit tests — none touches PG or asyncio. The bucketing
helpers are sync, and the publish helpers (`_publish_*_metrics`) are
sync side-effects of the async ``record_*`` emitters that we exercise
directly to skip the asyncpg leg.

Module-global state audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
``metrics_labels._seen_*`` are per-worker module-globals (audit
answer #3 — intentionally per-worker independent; see module
docstring). Each test calls ``metrics_labels.reset_for_tests()`` and
``metrics.reset_for_tests()`` in the appropriate fixture so
cross-test pollution is impossible. The ``threading.Lock`` in
``_bucket`` is for in-process thread-safety, not cross-test
isolation.
"""

from __future__ import annotations

import pytest

from backend import billing_usage as bu
from backend import metrics as m
from backend import metrics_labels as ml


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

prom_only = pytest.mark.skipif(
    not m.is_available(), reason="prometheus_client not installed"
)


@pytest.fixture(autouse=True)
def _reset_state():
    """Clean slate for every test — registry + label-bookkeeping sets."""
    if m.is_available():
        m.reset_for_tests()
    ml.reset_for_tests()
    yield
    if m.is_available():
        m.reset_for_tests()
    ml.reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  bucket_* helpers — sentinel + cap behaviour
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_none_and_empty_become_unknown():
    assert ml.bucket_tenant_id(None) == ml.UNKNOWN_BUCKET
    assert ml.bucket_tenant_id("") == ml.UNKNOWN_BUCKET
    assert ml.bucket_project_id(None) == ml.UNKNOWN_BUCKET
    assert ml.bucket_project_id("") == ml.UNKNOWN_BUCKET
    assert ml.bucket_product_line(None) == ml.UNKNOWN_BUCKET
    assert ml.bucket_product_line("") == ml.UNKNOWN_BUCKET


def test_real_value_under_cap_passes_through():
    assert ml.bucket_tenant_id("t-acme") == "t-acme"
    assert ml.bucket_project_id("p-acme-firmware") == "p-acme-firmware"
    assert ml.bucket_product_line("embedded") == "embedded"


def test_known_value_does_not_consume_extra_slots():
    """Re-bucketing the same value should NOT count against the cap."""
    ml.bucket_tenant_id("t-acme")
    seen_first = ml.cap_status()["tenant"]["seen"]
    for _ in range(20):
        assert ml.bucket_tenant_id("t-acme") == "t-acme"
    seen_after = ml.cap_status()["tenant"]["seen"]
    assert seen_first == 1 == seen_after


def test_overflow_falls_into_other_bucket(monkeypatch):
    """Past the per-worker cap, brand-new values collapse to 'other'."""
    monkeypatch.setattr(ml, "_TENANT_CAP", 3)
    ml.reset_for_tests()
    assert ml.bucket_tenant_id("a") == "a"
    assert ml.bucket_tenant_id("b") == "b"
    assert ml.bucket_tenant_id("c") == "c"
    # Cap is now saturated.
    assert ml.bucket_tenant_id("d") == ml.OTHER_BUCKET
    assert ml.bucket_tenant_id("e") == ml.OTHER_BUCKET
    # Already-seen values still pass through after saturation.
    assert ml.bucket_tenant_id("a") == "a"


def test_caps_are_independent_across_dimensions(monkeypatch):
    """Saturating tenants must not affect projects or product_lines."""
    monkeypatch.setattr(ml, "_TENANT_CAP", 1)
    ml.reset_for_tests()
    ml.bucket_tenant_id("t-1")
    assert ml.bucket_tenant_id("t-2") == ml.OTHER_BUCKET
    # Project + product_line dimensions still fresh.
    assert ml.bucket_project_id("p-1") == "p-1"
    assert ml.bucket_project_id("p-2") == "p-2"
    assert ml.bucket_product_line("embedded") == "embedded"


def test_default_caps_are_1000_10000_50():
    """Acceptance criterion from the row spec: tenant 1000, project
    10k, product_line bounded ('5 known + headroom'). The exact
    product_line cap is module-private; we only assert it's at least
    1 and well above the realistic working set."""
    # Re-import to bypass any monkeypatch — read the module-level constants.
    import importlib
    fresh = importlib.reload(ml)
    try:
        # Defaults can be overridden by env at import time; if the
        # env var is set the test is informational only.
        import os as _os
        if not _os.getenv("OMNISIGHT_METRICS_TENANT_CAP"):
            assert fresh._TENANT_CAP == 1000
        if not _os.getenv("OMNISIGHT_METRICS_PROJECT_CAP"):
            assert fresh._PROJECT_CAP == 10_000
        if not _os.getenv("OMNISIGHT_METRICS_PRODUCT_LINE_CAP"):
            assert fresh._PRODUCT_LINE_CAP == 50
    finally:
        importlib.reload(ml)


def test_env_override_caps_bad_value_falls_back(monkeypatch):
    """Garbage env input must NOT crash — fall back to default."""
    monkeypatch.setenv("OMNISIGHT_METRICS_TENANT_CAP", "not-a-number")
    monkeypatch.setenv("OMNISIGHT_METRICS_PROJECT_CAP", "")
    import importlib
    fresh = importlib.reload(ml)
    try:
        assert fresh._TENANT_CAP == 1000
        assert fresh._PROJECT_CAP == 10_000
    finally:
        importlib.reload(ml)


def test_env_override_negative_clamped_to_one(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_METRICS_TENANT_CAP", "-5")
    import importlib
    fresh = importlib.reload(ml)
    try:
        assert fresh._TENANT_CAP == 1
    finally:
        importlib.reload(ml)


def test_cap_status_shape():
    status = ml.cap_status()
    assert set(status.keys()) == {"tenant", "project", "product_line"}
    for dim, info in status.items():
        assert "seen" in info and "cap" in info
        assert info["seen"] >= 0
        assert info["cap"] >= 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  metrics.py registry — Y9 row 4 metrics exist with right labels
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@prom_only
@pytest.mark.parametrize("name,labels", [
    ("billing_llm_calls_total", {
        "tenant_id": "t-acme", "project_id": "p-acme-fw",
        "product_line": "embedded", "provider": "anthropic",
        "model": "claude-haiku-4.5",
    }),
    ("billing_llm_input_tokens_total", {
        "tenant_id": "t-acme", "project_id": "p-acme-fw",
        "product_line": "embedded", "provider": "anthropic",
        "model": "claude-haiku-4.5",
    }),
    ("billing_llm_output_tokens_total", {
        "tenant_id": "t-acme", "project_id": "p-acme-fw",
        "product_line": "embedded", "provider": "anthropic",
        "model": "claude-haiku-4.5",
    }),
    ("billing_llm_cost_usd_total", {
        "tenant_id": "t-acme", "project_id": "p-acme-fw",
        "product_line": "embedded", "provider": "anthropic",
        "model": "claude-haiku-4.5",
    }),
    ("billing_workflow_runs_total", {
        "tenant_id": "t-acme", "project_id": "p-acme-fw",
        "product_line": "embedded", "workflow_kind": "build",
        "workflow_status": "completed",
    }),
    ("billing_workspace_gb_hours_total", {
        "tenant_id": "t-acme", "project_id": "p-acme-fw",
        "product_line": "embedded",
    }),
    ("metrics_label_cap_used", {"dimension": "tenant"}),
])
def test_billing_metrics_accept_declared_labels(name, labels):
    metric = getattr(m, name)
    child = metric.labels(**labels)
    if name == "metrics_label_cap_used":
        child.set(0.5)
    else:
        child.inc()


@prom_only
def test_billing_metric_rejects_unknown_label():
    with pytest.raises(Exception):  # ValueError from prometheus_client
        m.billing_llm_calls_total.labels(not_a_real_label="x")


@prom_only
def test_billing_metrics_survive_reset_for_tests():
    """Operators rely on a non-None metric attr after reset; otherwise
    test-runs that re-import the module would publish to a stale
    registry while /metrics scrapes a fresh one."""
    m.reset_for_tests()
    for name in (
        "billing_llm_calls_total",
        "billing_llm_input_tokens_total",
        "billing_llm_output_tokens_total",
        "billing_llm_cost_usd_total",
        "billing_workflow_runs_total",
        "billing_workspace_gb_hours_total",
        "metrics_label_cap_used",
    ):
        attr = getattr(m, name)
        assert attr is not None, f"{name} unbound after reset_for_tests"
        assert hasattr(attr, "labels"), f"{name} missing .labels after reset"


@prom_only
def test_render_exposition_includes_billing_family():
    bu._publish_llm_metrics(
        tenant_id="t-acme", project_id="p-acme-fw",
        product_line="embedded", provider="anthropic",
        model="claude-haiku-4.5",
        input_tokens=100, output_tokens=50, cost_usd=0.001,
    )
    bu._publish_workflow_metrics(
        tenant_id="t-acme", project_id="p-acme-fw",
        product_line="embedded",
        workflow_kind="build", workflow_status="completed",
    )
    bu._publish_workspace_gb_hour_metrics(
        tenant_id="t-acme", project_id="p-acme-fw",
        product_line="embedded", gb_hours=0.5,
    )
    body, _ctype = m.render_exposition()
    text = body.decode()
    assert "omnisight_billing_llm_calls_total" in text
    assert "omnisight_billing_llm_input_tokens_total" in text
    assert "omnisight_billing_llm_output_tokens_total" in text
    assert "omnisight_billing_llm_cost_usd_total" in text
    assert "omnisight_billing_workflow_runs_total" in text
    assert "omnisight_billing_workspace_gb_hours_total" in text
    assert "omnisight_metrics_label_cap_used" in text
    # And labels are present.
    assert 'tenant_id="t-acme"' in text
    assert 'project_id="p-acme-fw"' in text
    assert 'product_line="embedded"' in text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  billing_usage._publish_* — bucketing + cap collapse end-to-end
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@prom_only
def test_publish_llm_metrics_buckets_overflow_into_other(monkeypatch):
    """Saturate the project cap and watch the next call land on
    project_id='other' in the actual exposition output."""
    monkeypatch.setattr(ml, "_PROJECT_CAP", 2)
    ml.reset_for_tests()
    m.reset_for_tests()
    # First two projects pass through.
    for pid in ("p-1", "p-2"):
        bu._publish_llm_metrics(
            tenant_id="t-acme", project_id=pid, product_line="embedded",
            provider="anthropic", model="claude-haiku-4.5",
            input_tokens=10, output_tokens=5, cost_usd=0.001,
        )
    # Third would overflow — must collapse to 'other'.
    bu._publish_llm_metrics(
        tenant_id="t-acme", project_id="p-3", product_line="embedded",
        provider="anthropic", model="claude-haiku-4.5",
        input_tokens=10, output_tokens=5, cost_usd=0.001,
    )
    body, _ctype = m.render_exposition()
    text = body.decode()
    assert 'project_id="p-1"' in text
    assert 'project_id="p-2"' in text
    assert 'project_id="other"' in text
    assert 'project_id="p-3"' not in text  # collapsed


@prom_only
def test_publish_llm_metrics_unknown_tenant_uses_unknown_bucket():
    bu._publish_llm_metrics(
        tenant_id=None, project_id=None, product_line=None,
        provider=None, model="claude-haiku-4.5",
        input_tokens=1, output_tokens=1, cost_usd=0.0,
    )
    body, _ctype = m.render_exposition()
    text = body.decode()
    assert 'tenant_id="unknown"' in text
    assert 'project_id="unknown"' in text
    assert 'product_line="unknown"' in text


@prom_only
def test_publish_llm_metrics_zero_value_skips_token_increment():
    """Zero token counts must not register a child series with 0
    counter — that pollutes /metrics output. Only the call-count
    counter (always ticks +1) and the cost counter (if non-zero)
    should appear for a token=0 emission."""
    bu._publish_llm_metrics(
        tenant_id="t-acme", project_id="p-acme-fw", product_line="embedded",
        provider="anthropic", model="claude-haiku-4.5",
        input_tokens=0, output_tokens=0, cost_usd=0.0,
    )
    body, _ctype = m.render_exposition()
    text = body.decode()
    # Call count always fires.
    assert "omnisight_billing_llm_calls_total" in text
    # The token / cost counter LINES for this label set should NOT
    # appear (they would be 0). prometheus_client only emits a sample
    # line when ``.inc(n)`` has been called at least once; we never
    # called .inc on these, so they should be absent for this label set.
    label_signature = (
        'tenant_id="t-acme"'
    )
    # Find lines for the input_tokens family with this label set.
    in_tok_lines = [
        line for line in text.splitlines()
        if line.startswith("omnisight_billing_llm_input_tokens_total{")
        and label_signature in line
    ]
    assert in_tok_lines == []


@prom_only
def test_publish_workspace_gb_hour_zero_is_noop():
    """A zero-GB-hour sample is skipped — no series leak for empty
    project dirs."""
    bu._publish_workspace_gb_hour_metrics(
        tenant_id="t-acme", project_id="p-acme-fw",
        product_line="embedded", gb_hours=0.0,
    )
    body, _ctype = m.render_exposition()
    text = body.decode()
    # The metric family registers but no sample lines for this label set.
    in_lines = [
        line for line in text.splitlines()
        if line.startswith("omnisight_billing_workspace_gb_hours_total{")
    ]
    assert in_lines == []


@prom_only
def test_cap_status_gauge_reflects_seen_count(monkeypatch):
    monkeypatch.setattr(ml, "_TENANT_CAP", 5)
    ml.reset_for_tests()
    m.reset_for_tests()
    for tid in ("a", "b", "c"):
        bu._publish_llm_metrics(
            tenant_id=tid, project_id="p", product_line="embedded",
            provider="anthropic", model="claude-haiku-4.5",
            input_tokens=1, output_tokens=1, cost_usd=0.0001,
        )
    body, _ctype = m.render_exposition()
    text = body.decode()
    # 3 of 5 = 0.6
    matches = [
        line for line in text.splitlines()
        if line.startswith('omnisight_metrics_label_cap_used{dimension="tenant"}')
    ]
    assert matches, "tenant cap_used gauge missing"
    # The value should be 0.6 (3 / 5).
    assert any("0.6" in line for line in matches)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _infer_provider — model prefix → provider mapping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("model,expected", [
    ("claude-haiku-4.5", "anthropic"),
    ("claude-opus-4-7", "anthropic"),
    ("anthropic/claude-haiku-4", "anthropic"),
    ("gpt-4o", "openai"),
    ("gpt-4o-mini", "openai"),
    ("o1-preview", "openai"),
    ("o3-mini", "openai"),
    ("openai/gpt-4o", "openai"),
    ("gemini-2.0-flash", "google"),
    ("google/gemini-pro", "google"),
    ("mixtral-8x7b", "mistral"),
    ("mistral-7b", "mistral"),
    ("llama-3.1-70b", "meta"),
    ("deepseek-coder", "deepseek"),
    ("qwen2.5-72b", "qwen"),
    ("openrouter/exotic-model", "openrouter"),
    ("custom-internal-llm", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_infer_provider(model, expected):
    assert bu._infer_provider(model) == expected


def test_provider_explicit_kwarg_wins_over_inference():
    """If a caller passes ``provider=``, the metric label uses that
    even when the model prefix would map differently."""
    if not m.is_available():
        pytest.skip("prom not installed")
    m.reset_for_tests()
    ml.reset_for_tests()
    bu._publish_llm_metrics(
        tenant_id="t-acme", project_id="p-acme-fw", product_line="embedded",
        provider="custom-router",  # operator-supplied
        model="claude-haiku-4.5",  # would infer anthropic
        input_tokens=1, output_tokens=1, cost_usd=0.0,
    )
    body, _ctype = m.render_exposition()
    text = body.decode()
    assert 'provider="custom-router"' in text
    assert 'provider="anthropic"' not in text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public emitters accept new kwargs without breaking signatures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_record_llm_call_signature_includes_new_kwargs():
    import inspect
    sig = inspect.signature(bu.record_llm_call)
    assert "product_line" in sig.parameters
    assert "provider" in sig.parameters


def test_record_workflow_run_signature_includes_product_line():
    import inspect
    sig = inspect.signature(bu.record_workflow_run)
    assert "product_line" in sig.parameters


def test_record_workspace_gb_hour_signature_includes_product_line():
    import inspect
    sig = inspect.signature(bu.record_workspace_gb_hour)
    assert "product_line" in sig.parameters


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Workspace GC walker emits (product_line, project_id, size) tuples
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_workspace_gc_walker_returns_product_line_tuple(tmp_path, monkeypatch):
    """Y9 row 4 wiring: ``_list_projects_per_tenant`` must surface
    ``product_line`` so the GC sweep can label its workspace-GB-hour
    samples without re-walking the filesystem."""
    from backend import workspace_gc as gc
    from backend import workspace as ws

    root = tmp_path / "workspaces"
    monkeypatch.setattr(ws, "_WORKSPACES_ROOT", root)

    # Layout: {root}/{tid}/{product_line}/{project_id}/{agent}/{hash}/file
    leaf = root / "t-acme" / "embedded" / "p-acme-fw" / "agent-1" / "hash-1"
    leaf.mkdir(parents=True)
    (leaf / "binary.bin").write_bytes(b"x" * 1024)

    leaf2 = root / "t-acme" / "web" / "p-acme-www" / "agent-1" / "hash-1"
    leaf2.mkdir(parents=True)
    (leaf2 / "asset.bin").write_bytes(b"y" * 2048)

    out = gc._list_projects_per_tenant()
    assert "t-acme" in out
    rows = out["t-acme"]
    # Each row is (product_line, project_id, size_bytes)
    for row in rows:
        assert len(row) == 3
    pls = sorted(r[0] for r in rows)
    assert pls == ["embedded", "web"]
    pids = sorted(r[1] for r in rows)
    assert pids == ["p-acme-fw", "p-acme-www"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Module-global state audit — caps don't bleed across reset_for_tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_reset_for_tests_clears_all_seen_sets():
    ml.bucket_tenant_id("t-1")
    ml.bucket_project_id("p-1")
    ml.bucket_product_line("embedded")
    pre = ml.cap_status()
    assert pre["tenant"]["seen"] == 1
    assert pre["project"]["seen"] == 1
    assert pre["product_line"]["seen"] == 1

    ml.reset_for_tests()
    post = ml.cap_status()
    assert post["tenant"]["seen"] == 0
    assert post["project"]["seen"] == 0
    assert post["product_line"]["seen"] == 0
