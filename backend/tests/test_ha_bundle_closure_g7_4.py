"""G7 HA-07 #4 — observability bundle closure (TODO row 1389).

The last deliverable of the G7 bucket. Ships two artefacts:

    * ``docs/ops/observability_runbook.md`` — single-entry
                                              aggregator the
                                              on-call operator
                                              reads when paged.
                                              Decision tree (§1),
                                              contract pins (§2),
                                              alert → panel map
                                              (§3), artefact
                                              relationship (§4),
                                              common failure modes
                                              (§5), scope fence (§6).
    * ``deploy/observability/README.md``      — deploy-side
                                              quickstart for an
                                              SRE loading the
                                              dashboard + alert
                                              rules into a fresh
                                              Grafana / Prometheus
                                              pair.

Both ship together: the runbook is the incident path, the README
is the deploy path. Contract tests in this file pin the shape of
both so drift between them (or between them and the G7 #1/#2/#3
artefacts they aggregate) surfaces on CI.

Explicit-migration pattern, 12th continuation — carried forward
from G5 #3 → #4 → #5 → #6 → G6 #1 → #2 → #3 → #4 → G6 #5 → G7 #2
→ G7 #3 → **G7 #4**. The G7 #3 test previously owned
``TestScopeDisciplineSiblingRows::test_no_g7_bundle_closure_file``
which RED-flagged the three candidate bundle-closure paths. G7 #4
lands two of those paths — the guard MUST be removed in the same
commit per the pattern. The removal is asserted in
``TestSiblingGuardMigration``.

Principles (Step 4 SOP):
    * Scientific: each assertion checks ONE invariant.
    * Minimal: no Grafana / Prometheus server — pure file reads.
    * Fast: whole file runs under a second.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The two G7 #4 artefacts.
RUNBOOK = PROJECT_ROOT / "docs" / "ops" / "observability_runbook.md"
DEPLOY_README = PROJECT_ROOT / "deploy" / "observability" / "README.md"

# Sibling artefacts the bundle aggregates.
METRICS_PY = PROJECT_ROOT / "backend" / "metrics.py"
HA_OBSERVABILITY_PY = PROJECT_ROOT / "backend" / "ha_observability.py"
DASHBOARD = PROJECT_ROOT / "deploy" / "observability" / "grafana" / "ha.json"
ALERTS_YML = (
    PROJECT_ROOT / "deploy" / "observability" / "prometheus" / "alerts.yml"
)

# G7 metric names that must appear in both docs at least once.
EXPECTED_METRICS = (
    "omnisight_backend_instance_up",
    "omnisight_rolling_deploy_5xx_rate",
    "omnisight_replica_lag_seconds",
    "omnisight_readyz_latency_seconds",
)

# The three alert names.
EXPECTED_ALERTS = (
    "OmniSightReplicaLagHigh",
    "OmniSightRollingDeploy5xxRateHigh",
    "OmniSightBackendInstanceDown",
)

TODO_MD = PROJECT_ROOT / "TODO.md"
HANDOFF_MD = PROJECT_ROOT / "HANDOFF.md"

G7_3_TEST = (
    PROJECT_ROOT / "backend" / "tests" / "test_ha_alert_rules_g7_3.py"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  A — runbook file shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRunbookFileShape:
    def test_runbook_exists(self) -> None:
        assert RUNBOOK.is_file(), (
            "G7 #4 (row 1389) ships docs/ops/observability_runbook.md — "
            "missing means the bundle aggregator has no source of truth"
        )

    def test_runbook_path_is_canonical(self) -> None:
        rel = RUNBOOK.relative_to(PROJECT_ROOT)
        assert str(rel) == "docs/ops/observability_runbook.md"

    def test_runbook_in_docs_ops(self) -> None:
        assert RUNBOOK.parent == PROJECT_ROOT / "docs" / "ops", (
            "runbook must live under docs/ops/ alongside the other "
            "operator-facing runbooks"
        )

    def test_runbook_has_h1_title(self) -> None:
        text = _read(RUNBOOK)
        for line in text.splitlines():
            if line.strip():
                assert line.startswith("# "), (
                    "runbook must open with a markdown H1 title"
                )
                lower = line.lower()
                assert (
                    "observability" in lower
                    or "ha-07" in lower
                    or "ha 07" in lower
                ), "H1 must name the bucket (observability or HA-07)"
                return
        pytest.fail("runbook is empty")

    def test_only_one_observability_runbook(self) -> None:
        # A second file at an alternative path would defeat the
        # aggregator role.
        alternatives = (
            PROJECT_ROOT / "docs" / "ops" / "observability-runbook.md",
            PROJECT_ROOT / "docs" / "ops" / "ha_observability.md",
            PROJECT_ROOT / "docs" / "OBSERVABILITY.md",
            PROJECT_ROOT / "OBSERVABILITY_RUNBOOK.md",
        )
        for alt in alternatives:
            assert not alt.exists(), (
                f"only one observability aggregator allowed; found "
                f"extra at {alt.relative_to(PROJECT_ROOT)}"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  B — deploy-side README file shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDeployReadmeFileShape:
    def test_readme_exists(self) -> None:
        assert DEPLOY_README.is_file(), (
            "G7 #4 ships deploy/observability/README.md — missing "
            "means there is no deploy-path quickstart for the bundle"
        )

    def test_readme_path_is_canonical(self) -> None:
        rel = DEPLOY_README.relative_to(PROJECT_ROOT)
        assert str(rel) == "deploy/observability/README.md"

    def test_readme_sits_beside_grafana_and_prometheus_dirs(self) -> None:
        # Canonical placement — the README must be the entry point
        # that GitHub renders when a user navigates to
        # deploy/observability/. Anywhere else is a split surface.
        parent = DEPLOY_README.parent
        assert (parent / "grafana").is_dir(), (
            "README must sit next to deploy/observability/grafana/"
        )
        assert (parent / "prometheus").is_dir(), (
            "README must sit next to deploy/observability/prometheus/"
        )

    def test_readme_has_h1_title(self) -> None:
        text = _read(DEPLOY_README)
        for line in text.splitlines():
            if line.strip():
                assert line.startswith("# "), (
                    "deploy README must open with a markdown H1 title"
                )
                lower = line.lower()
                assert "observability" in lower or "ha-07" in lower, (
                    "deploy README H1 must name the bundle"
                )
                return
        pytest.fail("deploy README is empty")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  C — runbook aggregator shape (decision tree + every sibling cited)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRunbookAggregatorShape:
    def test_runbook_has_decision_tree(self) -> None:
        text = _read(RUNBOOK).lower()
        assert "decision tree" in text, (
            "runbook must contain a decision-tree section — the "
            "operator paged at 3am must not have to read end-to-end "
            "to pick the right failure mode"
        )
        # Alert-driven framing — a page fires and the tree maps to a
        # dashboard panel. The phrase 'alert fired' is the G7 analogue
        # of G6's 'page fired'.
        assert "alert fired" in text or "paged" in text, (
            "decision tree must open with an alert-fire framing"
        )

    def test_runbook_references_g7_1_metrics_module(self) -> None:
        text = _read(RUNBOOK)
        assert "backend/metrics.py" in text or "backend/ha_observability.py" in text, (
            "runbook must reference G7 #1 metrics modules"
        )

    def test_runbook_references_g7_2_dashboard_file(self) -> None:
        text = _read(RUNBOOK)
        assert "deploy/observability/grafana/ha.json" in text, (
            "runbook must reference the G7 #2 Grafana dashboard file "
            "— half of the aggregator's job is pointing operators at it"
        )

    def test_runbook_references_g7_3_alerts_file(self) -> None:
        text = _read(RUNBOOK)
        assert "deploy/observability/prometheus/alerts.yml" in text, (
            "runbook must reference the G7 #3 Prometheus alerts file "
            "— the decision tree branches on alert names from there"
        )

    def test_runbook_references_deploy_readme(self) -> None:
        text = _read(RUNBOOK)
        assert "deploy/observability/README.md" in text, (
            "runbook must reference the companion deploy README — "
            "the two G7 #4 artefacts ship as a pair"
        )

    @pytest.mark.parametrize("alert_name", EXPECTED_ALERTS)
    def test_runbook_names_each_alert(self, alert_name: str) -> None:
        text = _read(RUNBOOK)
        assert alert_name in text, (
            f"runbook must name alert {alert_name!r} — the decision "
            f"tree is keyed on alert names, so a missing one means "
            f"the operator has no entry point for that page"
        )

    @pytest.mark.parametrize("metric_name", EXPECTED_METRICS)
    def test_runbook_names_each_metric(self, metric_name: str) -> None:
        text = _read(RUNBOOK)
        assert metric_name in text, (
            f"runbook must name metric {metric_name!r} — the §4 "
            f"relationship map and §2 contract pins depend on the "
            f"full metric set being enumerated"
        )

    @pytest.mark.parametrize("pin", ("G7 #1", "G7 #2", "G7 #3", "G7 #4"))
    def test_runbook_enumerates_every_g7_row(self, pin: str) -> None:
        text = _read(RUNBOOK)
        assert pin in text, (
            f"runbook must name {pin} — the §2 contract-pin index "
            f"must be complete for the aggregator to be honest"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  D — runbook cites, does not redefine (single source of truth per fact)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRunbookCitesNotRedefines:
    """G7 #3 owns the alert PromQL; G7 #2 owns the panel JSON; G7 #1
    owns the metric definitions. The runbook may CITE any of these
    (the numbers, the names) but MUST NOT redefine them — otherwise
    the first edit to the real artefact leaves this runbook stale."""

    def test_runbook_cites_10s_replica_threshold(self) -> None:
        text = _read(RUNBOOK)
        # The literal threshold must appear at least once so the
        # operator sees the number that pages them.
        assert re.search(r"\b10\s*s\b|\b10\s*second", text, re.IGNORECASE), (
            "runbook must cite the 10-second replica-lag threshold"
        )

    def test_runbook_cites_1pct_5xx_threshold(self) -> None:
        text = _read(RUNBOOK)
        # Accept "1%" or "0.01" — both are the same fact.
        assert "1%" in text or "0.01" in text, (
            "runbook must cite the 5xx-rate threshold (1% / 0.01)"
        )

    def test_runbook_does_not_inline_alert_yaml(self) -> None:
        # A fenced YAML block containing `- alert: OmniSight...`
        # would duplicate the alert definition. The runbook is
        # allowed to NAME the alert; it must not declare it.
        text = _read(RUNBOOK)
        pattern = re.compile(
            r"```ya?ml[^`]*-\s*alert:\s*OmniSight",
            re.IGNORECASE | re.DOTALL,
        )
        assert pattern.search(text) is None, (
            "runbook must NOT inline a `- alert: OmniSight...` YAML "
            "block — that belongs in "
            "deploy/observability/prometheus/alerts.yml"
        )

    def test_runbook_does_not_inline_panel_json(self) -> None:
        # A fenced JSON block containing a full panel definition
        # (has `"targets"` + `"fieldConfig"`) would duplicate the
        # dashboard surface.
        text = _read(RUNBOOK)
        pattern = re.compile(
            r"```json[^`]*\"targets\"[^`]*\"fieldConfig\"",
            re.IGNORECASE | re.DOTALL,
        )
        assert pattern.search(text) is None, (
            "runbook must NOT inline a Grafana panel JSON block — "
            "that belongs in deploy/observability/grafana/ha.json"
        )

    def test_runbook_does_not_formally_redefine_metrics(self) -> None:
        # G7 #1's backend/metrics.py owns the Gauge/Counter/Histogram
        # definitions. The runbook may describe what each metric
        # MEANS, but must not redefine them as Python code.
        text = _read(RUNBOOK)
        pattern = re.compile(
            r"```(?:python|py)?\s*[^`]*Gauge\s*\(\s*['\"]omnisight_",
            re.IGNORECASE | re.DOTALL,
        )
        assert pattern.search(text) is None, (
            "runbook must NOT declare Prometheus client Gauge() / "
            "Counter() / Histogram() — G7 #1 backend/metrics.py owns "
            "those definitions"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  E — deploy README quickstart shape (load + validate + import)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDeployReadmeQuickstart:
    def test_readme_references_dashboard_file(self) -> None:
        text = _read(DEPLOY_README)
        # The README lives in deploy/observability/; links can be
        # relative (`grafana/ha.json`) or repo-rooted.
        assert "grafana/ha.json" in text, (
            "deploy README must reference the dashboard JSON — the "
            "whole point of the quickstart is loading it"
        )

    def test_readme_references_alerts_file(self) -> None:
        text = _read(DEPLOY_README)
        assert "prometheus/alerts.yml" in text, (
            "deploy README must reference the alert rules YAML"
        )

    def test_readme_references_runbook(self) -> None:
        text = _read(DEPLOY_README)
        assert "observability_runbook.md" in text, (
            "deploy README must reference the companion runbook — "
            "the pair ships together"
        )

    def test_readme_mentions_prometheus_scrape(self) -> None:
        text = _read(DEPLOY_README).lower()
        assert "scrape" in text, (
            "deploy README must cover Prometheus scraping — the "
            "metrics don't land in Prometheus by themselves"
        )

    def test_readme_mentions_grafana_import(self) -> None:
        text = _read(DEPLOY_README).lower()
        assert "import" in text, (
            "deploy README must cover Grafana import — the "
            "dashboard JSON is an as-code artefact, not magic"
        )

    def test_readme_mentions_promtool_or_validate(self) -> None:
        # SRE reading this README must see how to sanity-check the
        # rules YAML before reloading Prometheus. Either promtool or
        # the word "validate" surfacing the check is acceptable.
        text = _read(DEPLOY_README).lower()
        assert "promtool" in text or "validate" in text, (
            "deploy README must tell the SRE how to validate the "
            "alert rules before reloading Prometheus — silent syntax "
            "errors are the worst observability failure"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  F — cross-references: every path the runbook/README names
#      must actually exist on disk (dangling links = silent rot)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCrossReferencesExistOnDisk:
    @pytest.mark.parametrize(
        "path",
        (
            METRICS_PY,
            HA_OBSERVABILITY_PY,
            DASHBOARD,
            ALERTS_YML,
        ),
    )
    def test_referenced_path_exists(self, path: Path) -> None:
        assert path.exists(), (
            f"G7 #4 bundle references {path.relative_to(PROJECT_ROOT)} "
            f"but it does not exist on disk — the runbook + README "
            f"would be dangling links in production"
        )

    def test_alerts_yaml_defines_every_alert_the_runbook_names(
        self,
    ) -> None:
        # If the runbook names an alert that the alert YAML does not
        # define, the decision tree has a dead branch.
        doc = yaml.safe_load(_read(ALERTS_YML))
        defined = set()
        for group in doc.get("groups", []):
            for rule in group.get("rules", []):
                if "alert" in rule:
                    defined.add(rule["alert"])
        for name in EXPECTED_ALERTS:
            assert name in defined, (
                f"alert {name!r} named by the runbook is not defined "
                f"in {ALERTS_YML.relative_to(PROJECT_ROOT)}"
            )

    def test_dashboard_exposes_metrics_the_readme_documents(self) -> None:
        # Symmetric: if the README documents a metric that the
        # dashboard doesn't render, the README is overselling the
        # bundle.
        dashboard = json.loads(_read(DASHBOARD))
        rendered_exprs = []
        for panel in dashboard.get("panels", []):
            if panel.get("type") == "row":
                continue
            for t in panel.get("targets", []):
                rendered_exprs.append(t.get("expr", ""))
        corpus = " ".join(rendered_exprs)
        for metric in EXPECTED_METRICS:
            assert metric in corpus, (
                f"metric {metric!r} documented by the deploy README "
                f"is not rendered by any panel in "
                f"{DASHBOARD.relative_to(PROJECT_ROOT)}"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  G — tracker alignment (TODO row 1389 flipped, HANDOFF names the pair)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTrackerAlignment:
    ROW_HEADLINE = "交付：dashboard + alert rules"

    def test_todo_row_headline_present(self) -> None:
        text = _read(TODO_MD)
        assert self.ROW_HEADLINE in text, (
            "row 1389 headline literal missing from TODO.md — "
            "a rename would silently mask the [x] flip below"
        )

    def test_todo_row_marked_complete(self) -> None:
        text = _read(TODO_MD)
        assert f"- [x] {self.ROW_HEADLINE}" in text, (
            "row 1389 must flip from [ ] to [x] in the same commit "
            "that lands the G7 #4 bundle artefacts"
        )

    def test_row_under_g7_section(self) -> None:
        text = _read(TODO_MD)
        g7_idx = text.find("### G7. HA-07 Observability for HA signals")
        row_idx = text.find(self.ROW_HEADLINE)
        assert g7_idx >= 0, "G7 section header missing from TODO.md"
        assert row_idx > g7_idx, (
            "row 1389 must appear AFTER the G7 section header"
        )

    def test_handoff_names_g7_4(self) -> None:
        text = _read(HANDOFF_MD)
        assert "G7 #4" in text, (
            "HANDOFF.md must name G7 #4 — bundle closure is the "
            "headline event for this commit"
        )

    def test_handoff_names_row_1389(self) -> None:
        text = _read(HANDOFF_MD)
        assert "row 1389" in text, "HANDOFF.md must cite TODO row 1389"

    def test_handoff_names_runbook_path(self) -> None:
        text = _read(HANDOFF_MD)
        assert "docs/ops/observability_runbook.md" in text, (
            "HANDOFF.md must name the runbook path so operators can "
            "grep for it from the handoff"
        )

    def test_handoff_names_deploy_readme_path(self) -> None:
        text = _read(HANDOFF_MD)
        assert "deploy/observability/README.md" in text, (
            "HANDOFF.md must name the deploy README path"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H — G7 #3 sibling guard migration (explicit-migration, 12th continuation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSiblingGuardMigration:
    """The G7 #3 test previously owned
    ``TestScopeDisciplineSiblingRows::test_no_g7_bundle_closure_file``
    which RED-flagged all three candidate bundle paths. G7 #4 lands
    two of those paths; the guard MUST be removed in the SAME commit
    per the explicit-migration pattern carried forward from
    G5 #3 → #4 → #5 → #6 → G6 #1 → #2 → #3 → #4 → G6 #5 → G7 #2 →
    G7 #3 → G7 #4 (12th continuation). A silent removal leaves no
    trace, so a breadcrumb must be written into G7 #3's test in
    place of the deleted guard."""

    def test_g7_3_no_bundle_closure_guard_removed(self) -> None:
        text = _read(G7_3_TEST)
        assert "def test_no_g7_bundle_closure_file" not in text, (
            "G7 #3 sibling guard `test_no_g7_bundle_closure_file` "
            "must be REMOVED in the same commit that lands G7 #4"
        )

    def test_g7_3_breadcrumb_points_at_successor(self) -> None:
        text = _read(G7_3_TEST)
        assert "G7 #4" in text, (
            "G7 #3 test must name G7 #4 in its migration breadcrumb"
        )
        assert "row 1389" in text, (
            "G7 #3 test must cite row 1389 in its migration breadcrumb"
        )
        assert "test_ha_bundle_closure_g7_4.py" in text, (
            "G7 #3 test must name the successor contract test file"
        )
        assert "12th continuation" in text, (
            "G7 #3 test must mark this as the 12th continuation of "
            "the explicit-migration pattern — the chain length is "
            "load-bearing context for readers"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  I — scope fence: G7 is closed by this row; nothing beyond HA-07
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestScopeFence:
    def test_runbook_does_not_inline_promql_fenced_block_with_for(
        self,
    ) -> None:
        # Full alert definitions land in alerts.yml. A fenced code
        # block that combines a PromQL expr with a `for:` duration
        # is the shape of a full alert rule and must not appear in
        # the runbook.
        text = _read(RUNBOOK)
        pattern = re.compile(
            r"```(?:promql|yaml|yml)?[^`]*omnisight_[a-z_]+\s*>\s*[0-9.]+[^`]*for:\s*[0-9]+m",
            re.IGNORECASE | re.DOTALL,
        )
        assert pattern.search(text) is None, (
            "runbook must NOT inline a PromQL + for-duration pair — "
            "that's a full alert rule; it belongs in alerts.yml"
        )

    def test_readme_forbids_recording_rules_wording(self) -> None:
        # Symmetric with G7 #3's TestScopeDisciplineSiblingRows
        # contract that the alert YAML carries no `record:` rules.
        # The deploy README must document that forbidden-ness so
        # the SRE reading it doesn't add recording rules "for
        # convenience".
        text = _read(DEPLOY_README).lower()
        assert "recording rule" in text, (
            "deploy README must mention recording rules (even just "
            "to forbid them) so an SRE does not silently add them "
            "and split the metric truth source"
        )

    def test_bundle_does_not_ship_yaml_manifest(self) -> None:
        # The third candidate path from the G7 #3 guard was
        # `deploy/observability/bundle.yml`. G7 #4 deliberately ships
        # only the two docs; a bundle YAML would be ceremonial
        # (nothing consumes it). If we later decide to ship it, it
        # lands in a new row with its own contract test.
        bundle_yml = (
            PROJECT_ROOT / "deploy" / "observability" / "bundle.yml"
        )
        assert not bundle_yml.exists(), (
            "deploy/observability/bundle.yml was a candidate path "
            "guarded by G7 #3 but is deliberately NOT shipped by "
            "G7 #4 — it would be ceremonial. If it appears, it "
            "needs its own row + contract test"
        )
