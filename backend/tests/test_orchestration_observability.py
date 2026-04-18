"""O9 (#272) — orchestration observability layer tests.

Covers:
  * Awaiting-human-+2 registry: register / clear / list, idempotent
    registration, gauge mirroring.
  * Snapshot accessor: queue depth, lock entries, merger rates,
    awaiting-human composition.
  * SSE emit: every ``orchestration.*`` publisher pushes through the bus.
  * Prometheus exporter: every O1 / O2 / O6 metric name appears in the
    rendered exposition (the "unified /metrics outlet" guarantee).
  * Alert rules YAML: each documented alert exists with the right
    severity label.
  * HTTP surface: snapshot + awaiting-human + queue-tick endpoints.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ──────────────────────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_orch_obs():
    """Wipe the awaiting-human registry between tests."""
    from backend import orchestration_observability as obs
    obs.reset_awaiting_for_tests()
    yield
    obs.reset_awaiting_for_tests()


# ──────────────────────────────────────────────────────────────
#  1. Awaiting-human registry
# ──────────────────────────────────────────────────────────────


class TestAwaitingHumanRegistry:
    def test_register_inserts_entry(self):
        from backend import orchestration_observability as obs
        e = obs.register_awaiting_human(
            change_id="I1234",
            project="omnisight",
            file_path="backend/foo.py",
            merger_confidence=0.92,
            push_sha="deadbeef",
        )
        assert e.change_id == "I1234"
        items = obs.list_awaiting_human()
        assert len(items) == 1
        assert items[0].change_id == "I1234"

    def test_register_is_idempotent_and_keeps_clock(self):
        """Re-firing register on the same change_id must NOT reset the wait
        clock — that would mask aging changes from the alert rule."""
        from backend import orchestration_observability as obs
        first = obs.register_awaiting_human(
            change_id="I9", project="p", file_path="a.py",
            merger_confidence=0.91,
        )
        original_since = first.awaiting_since
        # Re-register with a different confidence — must not re-zero clock.
        second = obs.register_awaiting_human(
            change_id="I9", project="p", file_path="a.py",
            merger_confidence=0.99,
        )
        assert obs.list_awaiting_human() == [second]
        assert second.awaiting_since == original_since
        assert second.merger_confidence == 0.99

    def test_clear_removes_entry(self):
        from backend import orchestration_observability as obs
        obs.register_awaiting_human(
            change_id="I7", project="p", file_path="a.py",
            merger_confidence=0.91,
        )
        assert obs.clear_awaiting_human("I7") is True
        assert obs.list_awaiting_human() == []
        # Idempotent: second clear is a noop.
        assert obs.clear_awaiting_human("I7") is False

    def test_register_requires_change_id(self):
        from backend import orchestration_observability as obs
        with pytest.raises(ValueError):
            obs.register_awaiting_human(
                change_id="", project="p", file_path="a.py",
                merger_confidence=0.9,
            )

    def test_list_sorted_by_oldest_first(self):
        from backend import orchestration_observability as obs
        obs.register_awaiting_human(
            change_id="I_new", project="p", file_path="a.py",
            merger_confidence=0.9, awaiting_since=2_000_000_000.0,
        )
        obs.register_awaiting_human(
            change_id="I_old", project="p", file_path="b.py",
            merger_confidence=0.9, awaiting_since=1_000_000_000.0,
        )
        items = obs.list_awaiting_human()
        assert [i.change_id for i in items] == ["I_old", "I_new"]

    def test_age_seconds_monotonic(self):
        import time
        from backend import orchestration_observability as obs
        e = obs.register_awaiting_human(
            change_id="I1", project="p", file_path="a.py",
            merger_confidence=0.9, awaiting_since=time.time() - 60,
        )
        assert e.age_seconds >= 60

    def test_gauge_mirrors_count(self):
        """awaiting_human_pending gauge tracks registry size."""
        from backend import metrics as m, orchestration_observability as obs
        m.reset_for_tests()
        obs.reset_awaiting_for_tests()
        obs.register_awaiting_human(
            change_id="I1", project="p", file_path="a.py",
            merger_confidence=0.9,
        )
        obs.register_awaiting_human(
            change_id="I2", project="p", file_path="b.py",
            merger_confidence=0.9,
        )
        if m.is_available():
            samples = list(m.awaiting_human_pending.collect()[0].samples)
            assert any(s.value == 2 for s in samples)
        obs.clear_awaiting_human("I1")
        if m.is_available():
            samples = list(m.awaiting_human_pending.collect()[0].samples)
            assert any(s.value == 1 for s in samples)


# ──────────────────────────────────────────────────────────────
#  2. Snapshot accessor
# ──────────────────────────────────────────────────────────────


class TestSnapshot:
    def test_snapshot_shape(self):
        from backend import orchestration_observability as obs
        snap = obs.snapshot_orchestration()
        assert "queue" in snap and "by_priority" in snap["queue"]
        assert "locks" in snap and "by_task" in snap["locks"]
        assert "merger" in snap
        assert "workers" in snap
        assert "awaiting_human_plus_two" in snap
        assert "awaiting_human_warn_hours" in snap

    def test_snapshot_includes_awaiting_entry(self):
        from backend import orchestration_observability as obs
        obs.register_awaiting_human(
            change_id="Iabcd", project="omni", file_path="x.py",
            merger_confidence=0.9, push_sha="abc",
        )
        snap = obs.snapshot_orchestration()
        ids = [e["change_id"] for e in snap["awaiting_human_plus_two"]]
        assert "Iabcd" in ids

    def test_snapshot_queue_uses_live_backend(self):
        """Push something onto the in-memory queue, then verify snapshot."""
        from backend import queue_backend as qb, orchestration_observability as obs
        qb.set_backend_for_tests(qb.InMemoryQueueBackend())
        try:
            qb.push(_dummy_task_card(), priority=qb.PriorityLevel.P1)
            snap = obs.snapshot_orchestration()
            assert snap["queue"]["by_priority"]["P1"] == 1
            assert snap["queue"]["total"] >= 1
        finally:
            qb.set_backend_for_tests(None)

    def test_snapshot_merger_rates_sum_to_one(self):
        """If we push a +2 and an abstain, rates should sum to exactly 1.0."""
        from backend import metrics as m, orchestration_observability as obs
        m.reset_for_tests()
        if not m.is_available():
            pytest.skip("prometheus_client not installed")
        m.merger_plus_two_total.inc()
        m.merger_abstain_total.labels(reason="abstained_low_confidence").inc()
        snap = obs.snapshot_orchestration()
        s = (
            snap["merger"]["plus_two_rate"]
            + snap["merger"]["abstain_rate"]
            + snap["merger"]["security_refusal_rate"]
        )
        assert abs(s - 1.0) < 1e-9
        assert snap["merger"]["total_votes"] == 2.0


def _dummy_task_card():
    """Minimal TaskCard built the same way `test_queue_backend.py` does."""
    from backend.catc import TaskCard
    return TaskCard.from_dict({
        "jira_ticket": "PROJ-272",
        "acceptance_criteria": "criteria",
        "navigation": {
            "entry_point": "src/main.c",
            "impact_scope": {
                "allowed": ["src/main.c"],
                "forbidden": [],
            },
        },
    })


# ──────────────────────────────────────────────────────────────
#  3. SSE emission
# ──────────────────────────────────────────────────────────────


class TestSseEmission:
    def test_emit_queue_tick_publishes(self, monkeypatch):
        from backend import orchestration_observability as obs
        emitted: list[tuple[str, dict]] = []

        def _capture(self, event, data, **kw):  # noqa: ANN001
            emitted.append((event, data))

        from backend.events import bus
        monkeypatch.setattr(bus.__class__, "publish", _capture)
        obs.emit_queue_tick()
        assert any(ev == "orchestration.queue.tick" for ev, _ in emitted)

    def test_emit_lock_acquired_carries_paths(self, monkeypatch):
        from backend import orchestration_observability as obs
        emitted: list[tuple[str, dict]] = []
        from backend.events import bus
        monkeypatch.setattr(bus.__class__, "publish",
                            lambda self, ev, data, **kw: emitted.append((ev, data)))
        obs.emit_lock_acquired(
            task_id="t1", paths=["a", "b"], priority=10,
            wait_seconds=0.5, expires_at=999.0,
        )
        ev, data = next((e, d) for e, d in emitted if e == "orchestration.lock.acquired")
        assert data["task_id"] == "t1"
        assert data["paths"] == ["a", "b"]
        assert data["priority"] == 10

    def test_emit_lock_released_count(self, monkeypatch):
        from backend import orchestration_observability as obs
        emitted: list[tuple[str, dict]] = []
        from backend.events import bus
        monkeypatch.setattr(bus.__class__, "publish",
                            lambda self, ev, data, **kw: emitted.append((ev, data)))
        obs.emit_lock_released(task_id="t9", released_count=3)
        ev, data = next((e, d) for e, d in emitted
                        if e == "orchestration.lock.released")
        assert data["task_id"] == "t9"
        assert data["released_count"] == 3

    def test_emit_merger_voted(self, monkeypatch):
        from backend import orchestration_observability as obs
        emitted: list[tuple[str, dict]] = []
        from backend.events import bus
        monkeypatch.setattr(bus.__class__, "publish",
                            lambda self, ev, data, **kw: emitted.append((ev, data)))
        obs.emit_merger_voted(
            change_id="Ix", file_path="x.py", reason="plus_two_voted",
            voted_score=2, confidence=0.93,
        )
        ev, data = next((e, d) for e, d in emitted
                        if e == "orchestration.merger.voted")
        assert data["voted_score"] == 2
        assert data["confidence"] == 0.93

    def test_emit_change_awaiting_human(self, monkeypatch):
        from backend import orchestration_observability as obs
        emitted: list[tuple[str, dict]] = []
        from backend.events import bus
        monkeypatch.setattr(bus.__class__, "publish",
                            lambda self, ev, data, **kw: emitted.append((ev, data)))
        obs.emit_change_awaiting_human(
            change_id="Iy", project="omni", file_path="z.py",
            merger_confidence=0.92, awaiting_since=1_700_000_000.0,
        )
        ev, data = next((e, d) for e, d in emitted
                        if e == "orchestration.change.awaiting_human_plus_two")
        assert data["change_id"] == "Iy"
        assert data["awaiting_since"] == 1_700_000_000.0

    def test_event_type_registry_complete(self):
        """The frontend SSE registry pins this list — keep it stable."""
        from backend import orchestration_observability as obs
        assert set(obs.ORCHESTRATION_EVENT_TYPES) == {
            "orchestration.queue.tick",
            "orchestration.lock.acquired",
            "orchestration.lock.released",
            "orchestration.merger.voted",
            "orchestration.change.awaiting_human_plus_two",
        }


# ──────────────────────────────────────────────────────────────
#  4. Prometheus exposition unification
# ──────────────────────────────────────────────────────────────


class TestPrometheusExporterUnified:
    """All O1 / O2 / O6 + O9 metric *names* must show up in /metrics."""

    REQUIRED_METRIC_NAMES = (
        # O1
        "omnisight_dist_lock_wait_seconds",
        "omnisight_dist_lock_held_total",
        "omnisight_dist_lock_deadlock_kills_total",
        # O2
        "omnisight_queue_depth",
        "omnisight_queue_claim_duration_seconds",
        # O3
        "omnisight_worker_active",
        "omnisight_worker_inflight",
        "omnisight_worker_task_total",
        # O6
        "omnisight_merger_agent_plus_two_total",
        "omnisight_merger_agent_abstain_total",
        "omnisight_merger_agent_security_refusal_total",
        "omnisight_merger_agent_confidence",
        # O9
        "omnisight_awaiting_human_plus_two_pending",
        "omnisight_awaiting_human_plus_two_age_seconds",
        "omnisight_worker_pool_capacity",
    )

    def test_render_exposition_contains_all_metrics(self):
        from backend import metrics as m
        if not m.is_available():
            pytest.skip("prometheus_client not installed")
        m.reset_for_tests()
        # Bump every Counter / Histogram to materialise it in the
        # exposition. Gauges show up immediately on creation.
        m.dist_lock_wait_seconds.labels(outcome="acquired").observe(0.1)
        m.dist_lock_held_total.labels(outcome="acquired").inc()
        m.dist_lock_deadlock_kills_total.labels(reason="cycle").inc()
        m.queue_claim_duration_seconds.labels(outcome="hit").observe(0.001)
        m.worker_task_total.labels(outcome="acked").inc()
        m.merger_plus_two_total.inc()
        m.merger_abstain_total.labels(reason="abstained_low_confidence").inc()
        m.merger_security_refusal_total.inc()
        m.merger_confidence.observe(0.95)
        body, ctype = m.render_exposition()
        assert "text/plain" in ctype
        text = body.decode("utf-8", errors="ignore")
        for name in self.REQUIRED_METRIC_NAMES:
            assert name in text, f"{name} missing from /metrics output"


# ──────────────────────────────────────────────────────────────
#  5. Alert rules YAML
# ──────────────────────────────────────────────────────────────


class TestAlertRulesYaml:
    RULES_PATH = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "prometheus"
        / "orchestration_alerts.rules.yml"
    )

    REQUIRED_ALERTS = {
        # name                                  -> expected severity label
        "OmniSightQueueDepthHigh":               "warning",
        "OmniSightQueueP0Backlog":               "critical",
        "OmniSightDistLockWaitP99High":          "warning",
        "OmniSightDistLockDeadlockKills":        "critical",
        "OmniSightMergerPlusTwoRateHigh":        "warning",
        "OmniSightMergerSecurityRefusalSpike":   "warning",
        "OmniSightDualSignPendingTooLong":       "warning",
        "OmniSightDualSignBacklog":              "info",
    }

    def test_rules_file_exists(self):
        assert self.RULES_PATH.exists(), (
            f"{self.RULES_PATH} missing — operators load it via "
            "rule_files in prometheus.yml"
        )

    def test_rules_parse_with_yaml(self):
        try:
            import yaml  # type: ignore
        except ImportError:
            pytest.skip("pyyaml not installed")
        data = yaml.safe_load(self.RULES_PATH.read_text())
        assert "groups" in data
        assert isinstance(data["groups"], list)

    def test_every_required_alert_present(self):
        try:
            import yaml  # type: ignore
        except ImportError:
            pytest.skip("pyyaml not installed")
        data = yaml.safe_load(self.RULES_PATH.read_text())
        all_alerts = {}
        for grp in data["groups"]:
            for rule in grp.get("rules", []):
                if "alert" in rule:
                    all_alerts[rule["alert"]] = rule.get("labels", {})
        for name, sev in self.REQUIRED_ALERTS.items():
            assert name in all_alerts, f"missing alert: {name}"
            assert all_alerts[name].get("severity") == sev, (
                f"alert {name}: severity={all_alerts[name].get('severity')} "
                f"!= expected {sev}"
            )

    def test_dual_sign_pending_uses_age_metric(self):
        """The 24h dual-sign alert must reference the awaiting-human age
        gauge — that's the metric the registry mirrors. A drift here means
        the alert won't fire."""
        body = self.RULES_PATH.read_text()
        assert "omnisight_awaiting_human_plus_two_age_seconds" in body
        assert "86400" in body                               # 24h in seconds


# ──────────────────────────────────────────────────────────────
#  6. HTTP surface
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestration_snapshot_endpoint(client):
    r = await client.get("/api/v1/orchestration/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert "queue" in body
    assert "locks" in body
    assert "merger" in body
    assert "workers" in body
    assert "awaiting_human_plus_two" in body


@pytest.mark.asyncio
async def test_orchestration_awaiting_human_endpoint(client):
    from backend import orchestration_observability as obs
    obs.register_awaiting_human(
        change_id="Ihttp", project="p", file_path="a.py",
        merger_confidence=0.91,
    )
    r = await client.get("/api/v1/orchestration/awaiting-human")
    assert r.status_code == 200
    body = r.json()
    assert any(item["change_id"] == "Ihttp" for item in body["items"])
    assert body["warn_hours"] == obs.AWAITING_HUMAN_WARN_HOURS


@pytest.mark.asyncio
async def test_orchestration_queue_tick_endpoint(client):
    r = await client.post("/api/v1/orchestration/queue-tick")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "snapshot" in body


@pytest.mark.asyncio
async def test_metrics_endpoint_includes_o9_metrics(client):
    """Confirm the unified /metrics outlet shows the new O9 series."""
    r = await client.get("/api/v1/metrics")
    assert r.status_code == 200
    body = r.text
    # NoOp stub keeps body minimal — only assert when prom client present.
    from backend import metrics as m
    if m.is_available():
        assert "omnisight_awaiting_human_plus_two_pending" in body
        assert "omnisight_worker_pool_capacity" in body
