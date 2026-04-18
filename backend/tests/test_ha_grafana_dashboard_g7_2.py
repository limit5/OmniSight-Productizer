"""G7 HA-07 #2 — Grafana dashboard ``deploy/observability/grafana/ha.json``.

The dashboard is the operator's read-only window on the four metric
families G7 #1 wired up. This file is the contract lock: it asserts
file shape, panel coverage of every metric name + label, PromQL
alignment with the G7 #3 alert thresholds (replica_lag > 10 s,
rolling_deploy_5xx_rate > 0.01), templating variable shape, and
TODO/HANDOFF alignment.

Principles (Step 4 SOP):
  * Scientific: each assertion checks ONE invariant.
  * Minimal: no Grafana server, no rendering — JSON-only contract.
  * Fast: whole file runs under a second.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = PROJECT_ROOT / "deploy" / "observability" / "grafana" / "ha.json"
TODO_MD = PROJECT_ROOT / "TODO.md"
HANDOFF_MD = PROJECT_ROOT / "HANDOFF.md"

# Metric names wired up by G7 #1 (source: backend/metrics.py §545-596).
G7_METRICS = (
    "omnisight_backend_instance_up",
    "omnisight_rolling_deploy_responses_total",
    "omnisight_rolling_deploy_5xx_rate",
    "omnisight_replica_lag_seconds",
    "omnisight_readyz_latency_seconds",
)


@pytest.fixture(scope="module")
def dashboard() -> dict:
    """Parse the dashboard once per run; fail loudly on malformed JSON."""
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _iter_panels(dash: dict):
    """Yield every non-row panel (rows are layout, not metric carriers)."""
    for p in dash.get("panels", []):
        if p.get("type") == "row":
            continue
        yield p


def _all_exprs(dash: dict) -> list[str]:
    out: list[str] = []
    for p in _iter_panels(dash):
        for t in p.get("targets", []):
            expr = t.get("expr")
            if isinstance(expr, str):
                out.append(expr)
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  A — file shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDashboardFileShape:
    def test_dashboard_file_exists(self) -> None:
        assert DASHBOARD.exists(), (
            "deploy/observability/grafana/ha.json must exist — "
            "G7 #2 deliverable"
        )

    def test_dashboard_path_is_canonical(self) -> None:
        assert DASHBOARD.relative_to(PROJECT_ROOT) == Path(
            "deploy/observability/grafana/ha.json"
        )

    def test_dashboard_is_valid_json(self) -> None:
        json.loads(DASHBOARD.read_text(encoding="utf-8"))

    def test_dashboard_has_stable_uid(self, dashboard: dict) -> None:
        # A stable UID is what makes Grafana treat re-imports as the
        # same dashboard; losing it breaks dashboard-as-code cadence.
        assert dashboard.get("uid") == "omnisight-ha-07"

    def test_dashboard_title_names_ha_07(self, dashboard: dict) -> None:
        title = dashboard.get("title", "")
        assert "HA" in title and "07" in title, (
            f"title must identify HA-07; got {title!r}"
        )

    def test_dashboard_tags_include_g7_and_ha(self, dashboard: dict) -> None:
        tags = set(dashboard.get("tags", []))
        # Tags drive Grafana folder/search; the deploy pipeline filters
        # by them, so pin the set.
        assert {"omnisight", "ha", "g7", "ha-07"}.issubset(tags)

    def test_dashboard_schema_version_is_grafana_10_compatible(
        self, dashboard: dict
    ) -> None:
        # Grafana 10.x uses schemaVersion in the high-30s; pin ≥ 37
        # (Grafana 9.4+) to rule out legacy Graph-panel definitions
        # that won't render on a modern deploy.
        assert int(dashboard.get("schemaVersion", 0)) >= 37


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  B — metric coverage (every G7 #1 metric appears in ≥ 1 panel)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestMetricCoverage:
    @pytest.mark.parametrize("metric", G7_METRICS)
    def test_every_g7_metric_is_queried_by_some_panel(
        self, dashboard: dict, metric: str
    ) -> None:
        exprs = _all_exprs(dashboard)
        hit = [e for e in exprs if metric in e]
        assert hit, (
            f"metric {metric} is defined in backend/metrics.py but no "
            f"dashboard panel queries it — every G7 #1 metric must be "
            f"visible on the dashboard"
        )

    def test_histogram_metric_uses_quantile_function(
        self, dashboard: dict
    ) -> None:
        # A histogram with only _count / _sum queries isn't exposing
        # latency — it's a request-rate chart with a misleading name.
        # At least one quantile call must be present.
        exprs = _all_exprs(dashboard)
        assert any(
            "histogram_quantile" in e
            and "omnisight_readyz_latency_seconds_bucket" in e
            for e in exprs
        ), "readyz latency histogram must be consumed via histogram_quantile()"

    def test_counter_metric_uses_rate_function(self, dashboard: dict) -> None:
        # Plotting a Counter as a raw value is monotonically rising;
        # it must go through rate() / increase() to be useful.
        exprs = _all_exprs(dashboard)
        total_hits = [
            e for e in exprs if "omnisight_rolling_deploy_responses_total" in e
        ]
        assert total_hits, "rolling_deploy_responses_total must appear"
        for e in total_hits:
            assert "rate(" in e or "increase(" in e, (
                f"Counter query should use rate()/increase(); got {e!r}"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  C — alert threshold alignment (G7 #3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAlertThresholdAlignment:
    """The dashboard must visualise the same thresholds the G7 #3
    alert rules will fire on, so an operator opening the dashboard
    after a page can see *why* the alert fired without consulting a
    second source. Rule set is pinned in the TODO headline:

        replica lag > 10 s  /  5xx rate > 1% for 2 min  /  instance down
    """

    def test_dashboard_visualises_10s_replica_lag_threshold(
        self, dashboard: dict
    ) -> None:
        found_ten = False
        for panel in _iter_panels(dashboard):
            # Accept threshold declared on any replica_lag panel.
            exprs = [t.get("expr", "") for t in panel.get("targets", [])]
            if not any("replica_lag_seconds" in e for e in exprs):
                continue
            steps = (
                panel.get("fieldConfig", {})
                .get("defaults", {})
                .get("thresholds", {})
                .get("steps", [])
            )
            for s in steps:
                if s.get("value") == 10:
                    found_ten = True
        assert found_ten, (
            "at least one replica_lag panel must carry a threshold "
            "step at value=10 (G7 #3 alert fires at > 10 s)"
        )

    def test_dashboard_visualises_one_percent_5xx_threshold(
        self, dashboard: dict
    ) -> None:
        found_one_pct = False
        for panel in _iter_panels(dashboard):
            exprs = [t.get("expr", "") for t in panel.get("targets", [])]
            if not any("rolling_deploy_5xx_rate" in e for e in exprs):
                continue
            steps = (
                panel.get("fieldConfig", {})
                .get("defaults", {})
                .get("thresholds", {})
                .get("steps", [])
            )
            for s in steps:
                if s.get("value") == 0.01:
                    found_one_pct = True
        assert found_one_pct, (
            "at least one rolling_deploy_5xx_rate panel must carry a "
            "threshold step at value=0.01 (G7 #3 alert fires at > 1%)"
        )

    def test_dashboard_surfaces_instance_down_as_distinct_panel(
        self, dashboard: dict
    ) -> None:
        # The instance-down alert (G7 #3) fires when any replica's
        # `instance_up` gauge is 0; the dashboard must have at least
        # one panel that scopes a query to the instance_up metric so
        # operators can see which replica dropped.
        panels_on_up = [
            p for p in _iter_panels(dashboard)
            if any(
                "omnisight_backend_instance_up" in t.get("expr", "")
                for t in p.get("targets", [])
            )
        ]
        assert len(panels_on_up) >= 2, (
            "need at least 2 panels on instance_up (count + per-replica) "
            "so operators see both scalar and breakdown"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  D — templating variables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTemplatingVariables:
    def test_datasource_variable_is_declared(self, dashboard: dict) -> None:
        names = [
            v.get("name")
            for v in dashboard.get("templating", {}).get("list", [])
        ]
        assert "datasource" in names, (
            "a `datasource` variable lets the dashboard follow the "
            "target Prometheus across environments without hard-coding"
        )

    def test_instance_id_variable_queries_correct_label(
        self, dashboard: dict
    ) -> None:
        var = next(
            v for v in dashboard["templating"]["list"]
            if v.get("name") == "instance_id"
        )
        query = var.get("query")
        # `query` can be a string or a dict (Grafana 9+ form) —
        # normalise for the regex.
        if isinstance(query, dict):
            query = query.get("query", "")
        assert "omnisight_backend_instance_up" in query
        assert "instance_id" in query

    def test_replica_variable_queries_correct_label(
        self, dashboard: dict
    ) -> None:
        var = next(
            v for v in dashboard["templating"]["list"]
            if v.get("name") == "replica"
        )
        query = var.get("query")
        if isinstance(query, dict):
            query = query.get("query", "")
        assert "omnisight_replica_lag_seconds" in query
        assert "replica" in query


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  E — panel structure
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPanelStructure:
    def test_every_panel_has_non_empty_targets(self, dashboard: dict) -> None:
        for p in _iter_panels(dashboard):
            assert p.get("targets"), (
                f"panel {p.get('title')!r} (id={p.get('id')}) has no "
                f"targets — dead panel in ops dashboard"
            )

    def test_every_panel_has_title(self, dashboard: dict) -> None:
        for p in _iter_panels(dashboard):
            assert p.get("title"), (
                f"panel id={p.get('id')} has no title — operators "
                f"will see a blank header"
            )

    def test_every_panel_has_description(self, dashboard: dict) -> None:
        # A read-only ops dashboard must explain what each panel is,
        # otherwise a 3am oncall guesses.
        for p in _iter_panels(dashboard):
            desc = p.get("description", "")
            assert desc and len(desc) >= 20, (
                f"panel {p.get('title')!r} needs a description "
                f"(≥ 20 chars); got {desc!r}"
            )

    def test_every_panel_uses_datasource_variable(
        self, dashboard: dict
    ) -> None:
        # Panels pointing at a hard-coded datasource UID break on
        # deploys that use a different Prometheus instance.
        for p in _iter_panels(dashboard):
            ds = p.get("datasource")
            if isinstance(ds, dict):
                uid = ds.get("uid", "")
                assert uid == "${datasource}", (
                    f"panel {p.get('title')!r} uses hard-coded "
                    f"datasource uid={uid!r}; must be ${{datasource}}"
                )

    def test_panel_ids_are_unique(self, dashboard: dict) -> None:
        ids = [p["id"] for p in dashboard.get("panels", []) if "id" in p]
        assert len(ids) == len(set(ids)), (
            f"duplicate panel ids — Grafana will behave unpredictably: {ids}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  F — tracker alignment (TODO + HANDOFF)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTrackerAlignment:
    def test_todo_row_1387_is_flipped(self) -> None:
        text = TODO_MD.read_text(encoding="utf-8")
        assert re.search(
            r"^- \[x\] Grafana dashboard `deploy/observability/grafana/ha\.json`",
            text,
            re.MULTILINE,
        ), "TODO row 1387 must be [x] once the dashboard lands"

    def test_todo_row_1387_stays_under_g7_section(self) -> None:
        text = TODO_MD.read_text(encoding="utf-8")
        g7_idx = text.find("### G7. HA-07 Observability for HA signals")
        row_idx = text.find(
            "Grafana dashboard `deploy/observability/grafana/ha.json`"
        )
        assert g7_idx >= 0 and row_idx > g7_idx, (
            "row 1387 must live beneath the G7 section header"
        )

    def test_handoff_names_g7_2_row_and_file(self) -> None:
        text = HANDOFF_MD.read_text(encoding="utf-8")
        assert "G7 #2" in text
        assert "row 1387" in text
        assert "deploy/observability/grafana/ha.json" in text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  G — G7 #1 sibling guard migration (explicit-migration pattern)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSiblingGuardMigration:
    """Six earlier commits (G5 #6 / G6 #1-#5) each held a guard
    asserting `deploy/observability/grafana/ha.json` did NOT exist.
    The explicit-migration pattern requires those guards be removed
    in the SAME commit that lands this file + replaced with a
    breadcrumb NOTE so readers find the successor contract."""

    @pytest.mark.parametrize("guard_file", [
        "test_ci_k8s_helm_smoke_g5_6.py",
        "test_dr_drill_daily_g6_1.py",
        "test_dr_rto_rpo_g6_2.py",
        "test_dr_manual_failover_g6_3.py",
        "test_dr_annual_drill_checklist_g6_4.py",
        "test_dr_bundle_closure_g6_5.py",
    ])
    def test_prior_sibling_guard_is_removed(self, guard_file: str) -> None:
        path = PROJECT_ROOT / "backend" / "tests" / guard_file
        text = path.read_text(encoding="utf-8")
        # The guard definition must be gone ...
        assert "def test_no_g7_grafana_dashboard" not in text, (
            f"{guard_file} still defines test_no_g7_grafana_dashboard "
            f"— must be removed as part of G7 #2 landing"
        )

    @pytest.mark.parametrize("guard_file", [
        "test_ci_k8s_helm_smoke_g5_6.py",
        "test_dr_drill_daily_g6_1.py",
        "test_dr_rto_rpo_g6_2.py",
        "test_dr_manual_failover_g6_3.py",
        "test_dr_annual_drill_checklist_g6_4.py",
        "test_dr_bundle_closure_g6_5.py",
    ])
    def test_breadcrumb_points_at_successor(self, guard_file: str) -> None:
        path = PROJECT_ROOT / "backend" / "tests" / guard_file
        text = path.read_text(encoding="utf-8")
        # ... and a NOTE must point future readers at the G7 #2
        # successor contract.
        assert "G7 #2" in text, f"{guard_file} missing G7 #2 breadcrumb"
        assert "row 1387" in text, f"{guard_file} missing row 1387 breadcrumb"
        assert "test_ha_grafana_dashboard_g7_2.py" in text, (
            f"{guard_file} must name the successor contract test"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H — scope discipline (sibling row 1388 not pre-committed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestScopeDisciplineSiblingRows:
    # NOTE: `test_no_g7_alert_rules_yaml` was removed in the commit
    # that landed G7 #3 (TODO row 1388) —
    # `deploy/observability/prometheus/alerts.yml` now owns the
    # HA-07 Prometheus alert surface. The G7 #3-side contract
    # pinning lives in `backend/tests/test_ha_alert_rules_g7_3.py`.
    # Explicit-migration pattern, 11th continuation:
    # G5 #3 → #4 → #5 → #6 → G6 #1 → #2 → #3 → #4 → G6 #5 → G7 #2
    # → G7 #3.

    def test_dashboard_does_not_inline_prometheus_alert_rule_shape(
        self, dashboard: dict
    ) -> None:
        """Grafana dashboards CAN carry alert rules, but the G7 bucket
        puts alert rules in Prometheus (row 1388) so Alertmanager owns
        routing. A dashboard that embeds its own alert rules under
        `panels[*].alert` would split the truth source."""
        for p in _iter_panels(dashboard):
            assert "alert" not in p, (
                f"panel {p.get('title')!r} declares an embedded alert "
                f"rule — G7 row 1388 owns alert rules in Prometheus, "
                f"not in this dashboard"
            )
