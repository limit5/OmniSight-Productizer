"""OP-879 D7 -- ``scripts/smoke_baseline_compare.py`` contract.

Five required cases per the OP-879 test plan:
  1. pass within threshold
  2. warn within +/-20%
  3. abort beyond +/-50% (triggers SSE + rollback)
  4. baseline missing -> bootstrap path
  5. metric fetch timeout -> partial-result event, no rollback

Plus a re-snapshot test (AC #5) and a tiny percentile sanity check so
the comparator's math is locked down independently of the orchestrator.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest


# ── Load the script as a module ─────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "smoke_baseline_compare.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_smoke_baseline_compare_under_test", _SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_smoke_baseline_compare_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def smoke():
    return _load_module()


# ── In-memory DB matching the alembic 0223 schema ───────────────


def _make_db() -> sqlite3.Connection:
    """Return an in-memory SQLite conn with the metric_baselines table.

    We mirror the alembic 0223 SQLite DDL inline so the test doesn't
    have to spin alembic up. ``test_alembic_0223_metric_baselines``
    (a sibling test, would be added under a separate contract test)
    is the authoritative schema check; here we just need the table.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE metric_baselines (
            endpoint        TEXT PRIMARY KEY,
            p50_ms          REAL NOT NULL CHECK (p50_ms >= 0),
            p95_ms          REAL NOT NULL CHECK (p95_ms >= 0),
            p99_ms          REAL NOT NULL CHECK (p99_ms >= 0),
            sample_count    INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
            deployment_tag  TEXT NOT NULL DEFAULT '',
            captured_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn


# ── Helpers ─────────────────────────────────────────────────────


def _seed_baselines(conn: sqlite3.Connection, smoke, p95_ms: float = 100.0) -> None:
    """Seed every DEFAULT_ROUTES endpoint with the same p95 baseline."""
    for endpoint in smoke.DEFAULT_ROUTES:
        conn.execute(
            "INSERT INTO metric_baselines "
            "(endpoint, p50_ms, p95_ms, p99_ms, sample_count, deployment_tag) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (endpoint.path, p95_ms * 0.5, p95_ms, p95_ms * 1.2, 25, "seed-v0"),
        )
    conn.commit()


def _flat_sampler(latency_ms: float, smoke):
    """Sampler that returns a constant latency for every request.

    Constant latency keeps p50/p95/p99 identical so the warn/abort
    logic is exercised on a clean signal. The sampler signature must
    match :data:`smoke.Sampler` -- ``(endpoint, n, timeout) -> list``.
    """
    def _sample(endpoint, n, timeout_s):  # noqa: ARG001
        return [latency_ms] * n
    return _sample


class _RecordingSink:
    """Spy ``EventSink`` -- collects ``(event, payload)`` tuples."""
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, payload) -> None:
        self.events.append((event, dict(payload)))


class _RollbackSpy:
    """Spy ``RollbackFn`` -- records call count + canned return."""
    def __init__(self, succeed: bool = True) -> None:
        self.succeed = succeed
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.succeed


# ── Tests ───────────────────────────────────────────────────────


def test_percentile_math_basic(smoke) -> None:
    assert smoke.percentile([10.0], 0.95) == 10.0
    # p50 of [10..100] (10 samples) -> ~55 with linear interp
    s = [float(i) for i in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)]
    assert smoke.percentile(s, 0.50) == pytest.approx(55.0, rel=1e-6)
    assert smoke.percentile(s, 0.95) == pytest.approx(95.5, rel=1e-6)
    assert smoke.percentile([], 0.95) == 0.0


def test_default_routes_has_ten_fixed_endpoints(smoke) -> None:
    """AC #2: 10 fixed routes (health, changes, 8 MCP)."""
    assert len(smoke.DEFAULT_ROUTES) == 10
    paths = {e.path for e in smoke.DEFAULT_ROUTES}
    assert "/health" in paths
    assert "/api/v1/changes" in paths
    mcp_count = sum(1 for p in paths if "/mcp" in p)
    assert mcp_count == 8


def test_pass_within_threshold(smoke) -> None:
    """Case 1: measurement within +/-20% of baseline -> overall pass."""
    conn = _make_db()
    _seed_baselines(conn, smoke, p95_ms=100.0)
    sink = _RecordingSink()
    rollback = _RollbackSpy()

    # 110ms vs 100ms = +10% -> pass
    report = smoke.run_smoke_compare(
        conn,
        _flat_sampler(110.0, smoke),
        sample_count=10,
        event_sink=sink,
        rollback_fn=rollback,
    )

    assert report.decision == smoke.DECISION_PASS
    assert report.rollback_invoked is False
    assert rollback.calls == 0
    assert all(row["decision"] == smoke.DECISION_PASS for row in report.per_endpoint)
    assert any(ev == smoke.EVENT_SMOKE_PASS for ev, _ in sink.events)


def test_warn_within_20_to_50_pct(smoke) -> None:
    """Case 2: drift beyond +/-20% but within +/-50% -> warn, no rollback."""
    conn = _make_db()
    _seed_baselines(conn, smoke, p95_ms=100.0)
    sink = _RecordingSink()
    rollback = _RollbackSpy()

    # 130ms vs 100ms = +30% -> warn
    report = smoke.run_smoke_compare(
        conn,
        _flat_sampler(130.0, smoke),
        sample_count=10,
        event_sink=sink,
        rollback_fn=rollback,
    )

    assert report.decision == smoke.DECISION_WARN
    assert report.rollback_invoked is False
    assert rollback.calls == 0
    assert all(
        row["decision"] == smoke.DECISION_WARN for row in report.per_endpoint
    )
    assert any(ev == smoke.EVENT_SMOKE_WARN for ev, _ in sink.events)
    # No abort event emitted on the warn path.
    assert all(ev != smoke.EVENT_SMOKE_FAILED for ev, _ in sink.events)


def test_abort_beyond_50_pct_triggers_sse_and_rollback(smoke) -> None:
    """Case 3 (DoD): drift beyond +/-50% -> abort + SSE + rollback."""
    conn = _make_db()
    _seed_baselines(conn, smoke, p95_ms=100.0)
    sink = _RecordingSink()
    rollback = _RollbackSpy(succeed=True)

    # 200ms vs 100ms = +100% -> abort
    report = smoke.run_smoke_compare(
        conn,
        _flat_sampler(200.0, smoke),
        sample_count=10,
        event_sink=sink,
        rollback_fn=rollback,
        deployment_tag="img-deadbeef",
    )

    assert report.decision == smoke.DECISION_ABORT
    assert report.rollback_invoked is True
    assert rollback.calls == 1
    # AC #4: SSE staging.smoke.failed emitted exactly once.
    failed = [pl for ev, pl in sink.events if ev == smoke.EVENT_SMOKE_FAILED]
    assert len(failed) == 1
    assert failed[0]["decision"] == smoke.DECISION_ABORT
    assert failed[0]["deployment_tag"] == "img-deadbeef"
    assert failed[0]["rollback_invoked"] is True
    # SmokeAbortTriggered surfaces in the summary.
    assert "SmokeAbortTriggered" in report.summary


def test_baseline_missing_bootstraps_and_warns(smoke) -> None:
    """Case 4: first deploy -> bootstrap baseline + warn."""
    conn = _make_db()  # empty -- no baselines seeded
    sink = _RecordingSink()
    rollback = _RollbackSpy()

    report = smoke.run_smoke_compare(
        conn,
        _flat_sampler(42.0, smoke),
        sample_count=10,
        event_sink=sink,
        rollback_fn=rollback,
        deployment_tag="img-bootstrap-1",
    )

    assert report.bootstrap is True
    assert report.decision == smoke.DECISION_WARN
    assert rollback.calls == 0
    # Every route now has a row -- the next run won't bootstrap again.
    row_count = conn.execute(
        "SELECT COUNT(*) FROM metric_baselines"
    ).fetchone()[0]
    assert row_count == len(smoke.DEFAULT_ROUTES)
    # The seeded p95 == measured p95 == 42ms.
    p95 = conn.execute(
        "SELECT p95_ms FROM metric_baselines WHERE endpoint = ?",
        ("/health",),
    ).fetchone()[0]
    assert p95 == pytest.approx(42.0)
    assert any(ev == smoke.EVENT_SMOKE_BOOTSTRAP for ev, _ in sink.events)
    assert "BaselineMissing" in report.summary


def test_metric_fetch_timeout_records_partial_no_rollback(smoke) -> None:
    """Case 5: per-endpoint timeout -> partial event, NO rollback.

    A fetch timeout is ambiguous (transient network blip vs. true
    regression) so the comparator records the partial-result event
    and lets the operator review -- it does NOT auto-rollback off a
    pure timeout signal.
    """
    conn = _make_db()
    _seed_baselines(conn, smoke, p95_ms=100.0)
    sink = _RecordingSink()
    rollback = _RollbackSpy()

    # Sampler raises MetricFetchTimeout on the first endpoint and
    # returns a healthy 100ms for everything else.
    first_path = smoke.DEFAULT_ROUTES[0].path

    def _sampler(endpoint, n, timeout_s):  # noqa: ARG001
        if endpoint.path == first_path:
            raise smoke.MetricFetchTimeout(
                f"{endpoint.path}: timed out after {timeout_s}s"
            )
        return [100.0] * n

    report = smoke.run_smoke_compare(
        conn,
        _sampler,
        sample_count=10,
        event_sink=sink,
        rollback_fn=rollback,
    )

    assert first_path in report.timed_out_endpoints
    assert rollback.calls == 0
    # Partial-result event fired exactly once.
    partial = [
        pl for ev, pl in sink.events if ev == smoke.EVENT_SMOKE_PARTIAL
    ]
    assert len(partial) == 1
    assert first_path in partial[0]["timed_out_endpoints"]
    # The non-timed-out endpoints still gate the overall decision --
    # 100ms vs 100ms baseline -> pass.
    assert report.decision == smoke.DECISION_PASS


# ── AC #5: operator-approved re-snapshot ────────────────────────


def test_snapshot_overwrites_baselines(smoke) -> None:
    """AC #5: snapshot replaces every baseline row idempotently."""
    conn = _make_db()
    _seed_baselines(conn, smoke, p95_ms=100.0)

    report = smoke.run_snapshot(
        conn,
        _flat_sampler(250.0, smoke),
        sample_count=8,
        deployment_tag="img-promote-2",
    )
    assert report.decision == smoke.DECISION_PASS  # snapshot never aborts
    # Every endpoint row p95 now reflects the fresh 250ms measurement.
    rows = conn.execute(
        "SELECT endpoint, p95_ms, deployment_tag FROM metric_baselines"
    ).fetchall()
    assert len(rows) == len(smoke.DEFAULT_ROUTES)
    for _path, p95, tag in rows:
        assert p95 == pytest.approx(250.0)
        assert tag == "img-promote-2"

    # Idempotency: a second snapshot call doesn't create duplicate rows
    # (PK on endpoint forces UPSERT).
    smoke.run_snapshot(
        conn,
        _flat_sampler(260.0, smoke),
        sample_count=8,
        deployment_tag="img-promote-3",
    )
    row_count = conn.execute(
        "SELECT COUNT(*) FROM metric_baselines"
    ).fetchone()[0]
    assert row_count == len(smoke.DEFAULT_ROUTES)


def test_snapshot_refuses_partial(smoke) -> None:
    """AC #5 corollary: snapshot must NOT commit a partial baseline."""
    conn = _make_db()
    _seed_baselines(conn, smoke, p95_ms=100.0)
    first_path = smoke.DEFAULT_ROUTES[0].path

    def _flaky(endpoint, n, timeout_s):  # noqa: ARG001
        if endpoint.path == first_path:
            raise smoke.MetricFetchTimeout(f"{endpoint.path}: timeout")
        return [200.0] * n

    with pytest.raises(smoke.MetricFetchTimeout):
        smoke.run_snapshot(conn, _flaky, sample_count=4)

    # Original baselines untouched (transactional contract).
    p95 = conn.execute(
        "SELECT p95_ms FROM metric_baselines WHERE endpoint = ?",
        ("/health",),
    ).fetchone()[0]
    assert p95 == pytest.approx(100.0)


# ── classify_delta sanity ───────────────────────────────────────


def test_classify_delta_boundaries(smoke) -> None:
    """The three-zone boundary check used by the compare path."""
    pass_, _ = smoke.classify_delta(
        baseline_p95=100.0, measured_p95=120.0,
        warn_pct=0.20, abort_pct=0.50,
    )
    assert pass_ == smoke.DECISION_PASS  # +20% is right at boundary (inclusive)

    warn_, _ = smoke.classify_delta(
        baseline_p95=100.0, measured_p95=130.0,
        warn_pct=0.20, abort_pct=0.50,
    )
    assert warn_ == smoke.DECISION_WARN

    abort_, _ = smoke.classify_delta(
        baseline_p95=100.0, measured_p95=200.0,
        warn_pct=0.20, abort_pct=0.50,
    )
    assert abort_ == smoke.DECISION_ABORT

    # Symmetric: regression in the *faster* direction also tripped.
    fast_abort, _ = smoke.classify_delta(
        baseline_p95=100.0, measured_p95=40.0,
        warn_pct=0.20, abort_pct=0.50,
    )
    assert fast_abort == smoke.DECISION_ABORT
