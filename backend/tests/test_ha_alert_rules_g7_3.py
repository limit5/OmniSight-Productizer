"""G7 HA-07 #3 — Prometheus alert rules ``deploy/observability/prometheus/alerts.yml``.

Three alerts pinned in the TODO headline (row 1388):
  * replica_lag > 10 s
  * 5xx rate > 1% for 2 min
  * instance down

Threshold values are intentionally aligned with the G7 #2 Grafana
dashboard panel thresholds — any drift between numbers here and the
panel steps in ``deploy/observability/grafana/ha.json`` means the
operator opens the dashboard after a page and sees one line saying
"OK" while the alert says "RED". That cognitive dissonance doubles
the time-to-diagnose; so this file pins both sides.

Principles (Step 4 SOP):
  * Scientific: each assertion checks ONE invariant.
  * Minimal: no Prometheus server — YAML + semantic rules only.
  * Fast: whole file runs under a second.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALERTS_YML = (
    PROJECT_ROOT / "deploy" / "observability" / "prometheus" / "alerts.yml"
)
DASHBOARD = PROJECT_ROOT / "deploy" / "observability" / "grafana" / "ha.json"
TODO_MD = PROJECT_ROOT / "TODO.md"
HANDOFF_MD = PROJECT_ROOT / "HANDOFF.md"

# The three alert names this file pins. Any rename must land here and in
# Alertmanager routing at the same time.
EXPECTED_ALERTS = (
    "OmniSightReplicaLagHigh",
    "OmniSightRollingDeploy5xxRateHigh",
    "OmniSightBackendInstanceDown",
)


@pytest.fixture(scope="module")
def rules_doc() -> dict:
    return yaml.safe_load(ALERTS_YML.read_text(encoding="utf-8"))


def _all_rules(doc: dict) -> list[dict]:
    out: list[dict] = []
    for group in doc.get("groups", []):
        for rule in group.get("rules", []):
            if "alert" in rule:
                out.append(rule)
    return out


def _rule_by_name(doc: dict, name: str) -> dict:
    for r in _all_rules(doc):
        if r.get("alert") == name:
            return r
    raise AssertionError(f"alert {name!r} not found in {ALERTS_YML}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  A — file shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAlertsFileShape:
    def test_alerts_file_exists(self) -> None:
        assert ALERTS_YML.exists(), (
            "deploy/observability/prometheus/alerts.yml must exist — "
            "G7 #3 deliverable"
        )

    def test_alerts_path_is_canonical(self) -> None:
        assert ALERTS_YML.relative_to(PROJECT_ROOT) == Path(
            "deploy/observability/prometheus/alerts.yml"
        )

    def test_alerts_file_is_valid_yaml(self) -> None:
        yaml.safe_load(ALERTS_YML.read_text(encoding="utf-8"))

    def test_no_sibling_candidate_paths_exist(self) -> None:
        # Prior guards forbade three candidate paths; now that the
        # canonical path is chosen, the OTHER two must stay unoccupied
        # so there's exactly one source of truth.
        siblings = (
            PROJECT_ROOT / "deploy" / "observability" / "alerts.yml",
            PROJECT_ROOT / "deploy" / "prometheus" / "ha-alerts.yml",
        )
        for s in siblings:
            assert not s.exists(), (
                f"{s.relative_to(PROJECT_ROOT)} must NOT exist — "
                f"the canonical path is deploy/observability/prometheus/"
                f"alerts.yml"
            )

    def test_top_level_is_groups_list(self, rules_doc: dict) -> None:
        assert isinstance(rules_doc.get("groups"), list)
        assert rules_doc["groups"], "groups must be non-empty"

    def test_group_has_name_and_rules(self, rules_doc: dict) -> None:
        for group in rules_doc["groups"]:
            assert group.get("name"), "group must have a name"
            assert isinstance(group.get("rules"), list)
            assert group["rules"], "group must have rules"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  B — three required alerts present (TODO headline pin)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRequiredAlertsPresent:
    @pytest.mark.parametrize("name", EXPECTED_ALERTS)
    def test_alert_is_defined(self, rules_doc: dict, name: str) -> None:
        names = [r.get("alert") for r in _all_rules(rules_doc)]
        assert name in names, (
            f"alert {name!r} must be defined; got {names!r}"
        )

    def test_exactly_three_alerts(self, rules_doc: dict) -> None:
        # The TODO headline pins exactly three alerts. Extras would be
        # scope creep; a fourth alert belongs in a separate row.
        alerts = _all_rules(rules_doc)
        assert len(alerts) == 3, (
            f"exactly 3 alerts pinned in TODO row 1388; got "
            f"{len(alerts)}: {[r.get('alert') for r in alerts]}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  C — threshold alignment with TODO headline + G7 #2 dashboard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestThresholdsMatchHeadline:
    """The TODO headline fixes the numbers. This section pins the
    exact values so that a silent edit to the YAML can't drift from
    the operator's mental model."""

    def test_replica_lag_threshold_is_ten_seconds(
        self, rules_doc: dict
    ) -> None:
        rule = _rule_by_name(rules_doc, "OmniSightReplicaLagHigh")
        expr = rule["expr"]
        assert "omnisight_replica_lag_seconds" in expr
        # Accept `> 10`, `>  10`, `>= 10` patterns (strict threshold).
        assert re.search(r">\s*10\b", expr), (
            f"replica lag alert must fire at > 10 s; got expr={expr!r}"
        )

    def test_5xx_rate_threshold_is_one_percent(self, rules_doc: dict) -> None:
        rule = _rule_by_name(
            rules_doc, "OmniSightRollingDeploy5xxRateHigh"
        )
        expr = rule["expr"]
        assert "omnisight_rolling_deploy_5xx_rate" in expr
        # 1% is 0.01 against the in-process ring-buffer gauge
        # (G7 #1 backend/ha_observability.py — already a ratio 0..1).
        assert re.search(r">\s*0\.01\b", expr), (
            f"5xx alert must fire at > 0.01 (1%); got expr={expr!r}"
        )

    def test_5xx_rate_has_two_minute_for_duration(
        self, rules_doc: dict
    ) -> None:
        rule = _rule_by_name(
            rules_doc, "OmniSightRollingDeploy5xxRateHigh"
        )
        # Prometheus accepts `2m`, `120s` — pin the canonical `2m` form
        # used by the TODO headline so edits are traceable.
        assert rule.get("for") == "2m", (
            f"5xx alert must use for: 2m; got {rule.get('for')!r}"
        )

    def test_instance_down_alert_targets_instance_up_metric(
        self, rules_doc: dict
    ) -> None:
        rule = _rule_by_name(rules_doc, "OmniSightBackendInstanceDown")
        expr = rule["expr"]
        assert "omnisight_backend_instance_up" in expr
        # `== 0` is the operational shape — `< 1` would also match
        # a half-drain, but the G7 #1 metric is a binary 0/1 gauge.
        assert re.search(r"==\s*0\b", expr), (
            f"instance-down alert must check == 0; got expr={expr!r}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  D — rule hygiene (labels, annotations, documentation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRuleHygiene:
    @pytest.mark.parametrize("name", EXPECTED_ALERTS)
    def test_rule_has_severity_label(
        self, rules_doc: dict, name: str
    ) -> None:
        # Alertmanager routes on `severity` — missing it means the
        # alert will fire but route to the default receiver silently.
        rule = _rule_by_name(rules_doc, name)
        labels = rule.get("labels", {})
        assert labels.get("severity") in {
            "info", "warning", "critical",
        }, (
            f"alert {name} needs a severity label "
            f"(info/warning/critical); got {labels!r}"
        )

    @pytest.mark.parametrize("name", EXPECTED_ALERTS)
    def test_rule_has_subsystem_label(
        self, rules_doc: dict, name: str
    ) -> None:
        # Subsystem lets on-call filter by HA failure mode; consistent
        # with deploy/prometheus/orchestration_alerts.rules.yml style.
        rule = _rule_by_name(rules_doc, name)
        labels = rule.get("labels", {})
        assert labels.get("subsystem", "").startswith("ha_"), (
            f"alert {name} needs a subsystem label prefixed 'ha_'; "
            f"got {labels!r}"
        )

    @pytest.mark.parametrize("name", EXPECTED_ALERTS)
    def test_rule_has_for_duration(
        self, rules_doc: dict, name: str
    ) -> None:
        # Every alert must have a non-zero `for:` to ride out single
        # scrape blips — otherwise oncall gets paged on noise.
        rule = _rule_by_name(rules_doc, name)
        assert rule.get("for"), (
            f"alert {name} missing `for:` — would page on single scrape"
        )

    @pytest.mark.parametrize("name", EXPECTED_ALERTS)
    def test_rule_has_summary_and_description(
        self, rules_doc: dict, name: str
    ) -> None:
        rule = _rule_by_name(rules_doc, name)
        ann = rule.get("annotations", {})
        assert ann.get("summary"), f"alert {name} missing summary"
        desc = ann.get("description", "")
        assert len(desc) >= 40, (
            f"alert {name} description too short ({len(desc)} chars); "
            f"oncall needs context, not just a metric name"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  E — cross-reference with G7 #2 dashboard thresholds
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDashboardThresholdAlignment:
    """G7 #2 panel thresholds were pinned to 10 (replica lag) and 0.01
    (5xx). If the alert rules drift from those numbers, the dashboard
    and the alert become two sources of truth."""

    @pytest.fixture(scope="class")
    def dashboard(self) -> dict:
        return json.loads(DASHBOARD.read_text(encoding="utf-8"))

    def test_dashboard_10s_replica_threshold_matches_alert(
        self, dashboard: dict, rules_doc: dict
    ) -> None:
        # Dashboard side (G7 #2) has a threshold step at value=10.
        panel_has_ten = False
        for p in dashboard.get("panels", []):
            if p.get("type") == "row":
                continue
            exprs = [t.get("expr", "") for t in p.get("targets", [])]
            if not any("replica_lag_seconds" in e for e in exprs):
                continue
            steps = (
                p.get("fieldConfig", {})
                .get("defaults", {})
                .get("thresholds", {})
                .get("steps", [])
            )
            if any(s.get("value") == 10 for s in steps):
                panel_has_ten = True
        # Alert side (this file) uses the same 10.
        rule = _rule_by_name(rules_doc, "OmniSightReplicaLagHigh")
        alert_has_ten = bool(re.search(r">\s*10\b", rule["expr"]))
        assert panel_has_ten and alert_has_ten, (
            "replica lag threshold (10 s) must be pinned on BOTH the "
            "Grafana panel (G7 #2) and the alert rule (this file)"
        )

    def test_dashboard_1pct_5xx_threshold_matches_alert(
        self, dashboard: dict, rules_doc: dict
    ) -> None:
        panel_has_one_pct = False
        for p in dashboard.get("panels", []):
            if p.get("type") == "row":
                continue
            exprs = [t.get("expr", "") for t in p.get("targets", [])]
            if not any("rolling_deploy_5xx_rate" in e for e in exprs):
                continue
            steps = (
                p.get("fieldConfig", {})
                .get("defaults", {})
                .get("thresholds", {})
                .get("steps", [])
            )
            if any(s.get("value") == 0.01 for s in steps):
                panel_has_one_pct = True
        rule = _rule_by_name(
            rules_doc, "OmniSightRollingDeploy5xxRateHigh"
        )
        alert_has_one_pct = bool(re.search(r">\s*0\.01\b", rule["expr"]))
        assert panel_has_one_pct and alert_has_one_pct, (
            "5xx rate threshold (0.01) must be pinned on BOTH the "
            "Grafana panel (G7 #2) and the alert rule (this file)"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  F — scope discipline (no G7 #4 bundle artefact pre-committed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestScopeDisciplineSiblingRows:
    # NOTE: `test_no_g7_bundle_closure_file` was removed in the commit
    # that landed G7 #4 (TODO row 1389) — `docs/ops/observability_runbook.md`
    # and `deploy/observability/README.md` now own the G7 bundle
    # closure surface. The G7 #4-side contract pinning lives in
    # `backend/tests/test_ha_bundle_closure_g7_4.py`. Explicit-
    # migration pattern, 12th continuation:
    # G5 #3 → #4 → #5 → #6 → G6 #1 → #2 → #3 → #4 → G6 #5 → G7 #2
    # → G7 #3 → G7 #4. (The third candidate path
    # `deploy/observability/bundle.yml` was deliberately NOT shipped;
    # G7 #4's `TestScopeFence::test_bundle_does_not_ship_yaml_manifest`
    # re-pins that absence under the successor contract.)

    def test_alert_file_does_not_redefine_metrics(
        self, rules_doc: dict
    ) -> None:
        # Prometheus rule files CAN also hold `record:` recording rules.
        # The G7 #1 metrics are the source of truth; a recording rule
        # here would split that truth.
        for group in rules_doc.get("groups", []):
            for rule in group.get("rules", []):
                assert "record" not in rule, (
                    f"rule {rule!r} uses `record:` — G7 #1 owns metric "
                    f"definitions; this file is for `alert:` rules only"
                )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  G — tracker alignment (TODO + HANDOFF)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTrackerAlignment:
    def test_todo_row_1388_is_flipped(self) -> None:
        text = TODO_MD.read_text(encoding="utf-8")
        assert re.search(
            r"^- \[x\] Alert rules：replica lag > 10s / 5xx rate > 1% for 2min / instance down",
            text,
            re.MULTILINE,
        ), "TODO row 1388 must be [x] once alert rules land"

    def test_todo_row_1388_stays_under_g7_section(self) -> None:
        text = TODO_MD.read_text(encoding="utf-8")
        g7_idx = text.find("### G7. HA-07 Observability for HA signals")
        row_idx = text.find(
            "Alert rules：replica lag > 10s / 5xx rate > 1% for 2min"
        )
        assert g7_idx >= 0 and row_idx > g7_idx, (
            "row 1388 must live beneath the G7 section header"
        )

    def test_handoff_names_g7_3_row_and_file(self) -> None:
        text = HANDOFF_MD.read_text(encoding="utf-8")
        assert "G7 #3" in text
        assert "row 1388" in text
        assert "deploy/observability/prometheus/alerts.yml" in text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H — G7 #2 sibling guard migration (explicit-migration pattern)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSiblingGuardMigration:
    """Four earlier files (G6 #3 / G6 #4 / G6 #5 / G7 #2) each held a
    guard asserting NO Prometheus alert-rules YAML existed at the
    three candidate paths. The explicit-migration pattern (11th
    continuation: G5 #3 → #4 → #5 → #6 → G6 #1 → #2 → #3 → #4 → G6
    #5 → G7 #2 → G7 #3) requires those guards be removed in the SAME
    commit that lands this file, with an inline NOTE breadcrumb so
    readers find the successor contract."""

    GUARD_FILES = (
        "test_dr_manual_failover_g6_3.py",
        "test_dr_annual_drill_checklist_g6_4.py",
        "test_dr_bundle_closure_g6_5.py",
        "test_ha_grafana_dashboard_g7_2.py",
    )

    @pytest.mark.parametrize("guard_file", GUARD_FILES)
    def test_prior_sibling_guard_is_removed(
        self, guard_file: str
    ) -> None:
        path = PROJECT_ROOT / "backend" / "tests" / guard_file
        text = path.read_text(encoding="utf-8")
        assert "def test_no_g7_alert_rules_yaml" not in text, (
            f"{guard_file} still defines test_no_g7_alert_rules_yaml "
            f"— must be removed as part of G7 #3 landing"
        )

    @pytest.mark.parametrize("guard_file", GUARD_FILES)
    def test_breadcrumb_points_at_successor(
        self, guard_file: str
    ) -> None:
        path = PROJECT_ROOT / "backend" / "tests" / guard_file
        text = path.read_text(encoding="utf-8")
        assert "G7 #3" in text, f"{guard_file} missing G7 #3 breadcrumb"
        assert "row 1388" in text, f"{guard_file} missing row 1388 breadcrumb"
        assert "test_ha_alert_rules_g7_3.py" in text, (
            f"{guard_file} must name the successor contract test"
        )
