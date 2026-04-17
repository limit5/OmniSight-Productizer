"""G6 #4 — Annual DR drill checklist contract tests.

TODO row 1382:
    年度 DR 演練 checklist

Pins ``docs/ops/dr_annual_drill_checklist.md``, the operator-led,
human-in-the-loop annual rehearsal that complements the automated
daily drill (G6 #1), reconciles observed RTO / RPO against the
G6 #2 budget, and exercises the G6 #3 manual failover runbook end
to end with a cold operator.

The checklist is the fourth deliverable of the G6 HA-06 bucket after
the daily drill workflow (G6 #1), the RTO/RPO objective doc (G6 #2),
and the manual failover runbook (G6 #3).

Sibling rows NOT covered by this test (explicit scope fence):

    * row 1383 (G6 #5) — ``scripts/dr_drill.sh`` +
      ``docs/ops/dr_runbook.md`` bundle-closure deliverables.

The sibling-row guards below RED-flag the above landing in this
same commit (silent scope creep). The explicit-migration pattern
(remove the guard in the commit that lands the next row) is carried
forward from G5 → G6 #1 → G6 #2 → G6 #3 → this row.

Explicit migration accepted from G6 #3:

    ``backend/tests/test_dr_manual_failover_g6_3.py`` previously
    owned ``TestScopeDisciplineSiblingRows::test_no_annual_dr_checklist_doc``
    which RED-flagged any of the candidate paths
    ``docs/ops/dr_annual_checklist.md`` /
    ``docs/ops/annual_dr_checklist.md`` /
    ``docs/ops/dr_drill_checklist.md``. G6 #4 lands a sibling
    canonical path ``docs/ops/dr_annual_drill_checklist.md`` — the
    guard MUST be removed in the same commit per the
    explicit-migration pattern. The migration is asserted below
    (``TestG6_3SiblingGuardMigration``).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CHECKLIST = (
    PROJECT_ROOT / "docs" / "ops" / "dr_annual_drill_checklist.md"
)
RTO_RPO_DOC = PROJECT_ROOT / "docs" / "ops" / "dr_rto_rpo.md"
MANUAL_FAILOVER_DOC = (
    PROJECT_ROOT / "docs" / "ops" / "dr_manual_failover.md"
)
DB_FAILOVER_DOC = PROJECT_ROOT / "docs" / "ops" / "db_failover.md"
DR_DRILL_WORKFLOW = (
    PROJECT_ROOT / ".github" / "workflows" / "dr-drill-daily.yml"
)
BACKUP_SELFTEST_SCRIPT = (
    PROJECT_ROOT / "scripts" / "backup_selftest.py"
)

TODO = PROJECT_ROOT / "TODO.md"
HANDOFF = PROJECT_ROOT / "HANDOFF.md"

G6_3_TEST = (
    PROJECT_ROOT / "backend" / "tests" / "test_dr_manual_failover_g6_3.py"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# TestChecklistFileShape — file is on disk and has a single canonical name.
# ---------------------------------------------------------------------------
class TestChecklistFileShape:
    def test_checklist_file_exists(self) -> None:
        assert CHECKLIST.is_file(), (
            "G6 #4 (row 1382) ships docs/ops/dr_annual_drill_checklist.md — "
            "missing means the annual human rehearsal has no source of truth"
        )

    def test_checklist_path_is_canonical(self) -> None:
        # Pinning the path means a rename surfaces here instead of
        # silently splitting the checklist across two locations.
        rel = CHECKLIST.relative_to(PROJECT_ROOT)
        assert str(rel) == "docs/ops/dr_annual_drill_checklist.md"

    def test_only_one_annual_checklist_doc(self) -> None:
        # A second file at one of the candidate paths would split the
        # truth source and defeat the G6 #4 contract.
        alternatives = (
            PROJECT_ROOT / "docs" / "ops" / "dr_annual_checklist.md",
            PROJECT_ROOT / "docs" / "ops" / "annual_dr_checklist.md",
            PROJECT_ROOT / "docs" / "ops" / "dr_drill_checklist.md",
            PROJECT_ROOT / "docs" / "ops" / "annual_drill_checklist.md",
        )
        for alt in alternatives:
            assert not alt.exists(), (
                f"only one annual DR drill checklist allowed; found "
                f"extra at {alt.relative_to(PROJECT_ROOT)}"
            )

    def test_checklist_has_title(self) -> None:
        # First non-empty line must be a markdown H1 that names the
        # doc — future readers should see "Annual DR Drill" on first
        # glance.
        text = _read(CHECKLIST)
        for line in text.splitlines():
            if line.strip():
                assert line.startswith("# "), (
                    "checklist must open with a markdown H1 title"
                )
                lower = line.lower()
                assert "annual" in lower and (
                    "drill" in lower or "dr" in lower
                ), (
                    "H1 must name 'annual' + 'drill' so the doc is "
                    "searchable"
                )
                return
        pytest.fail("checklist is empty")

    def test_checklist_in_docs_ops(self) -> None:
        # Other ops docs link by the exact path; moving it would
        # silently break those references.
        assert CHECKLIST.parent == PROJECT_ROOT / "docs" / "ops", (
            "checklist must live at docs/ops/dr_annual_drill_checklist.md"
        )


# ---------------------------------------------------------------------------
# TestChecklistStructure — the checklist must be annual-cadence, human-led,
# and contain actionable checkbox items for the two failure modes + data
# plane.
# ---------------------------------------------------------------------------
class TestChecklistStructure:
    def test_annual_cadence_named(self) -> None:
        text = _read(CHECKLIST).lower()
        # "annual" literal must appear — this is not a monthly or
        # quarterly drill, and the rationale for the cadence is part
        # of the G6 #4 deliverable.
        assert "annual" in text, (
            "checklist must name the 'annual' cadence — row 1382 "
            "headline is literally '年度 DR 演練' = annual DR drill"
        )

    def test_has_actionable_checkboxes(self) -> None:
        # A "checklist" without actionable `- [ ]` items is just prose.
        # The annual drill contract is that an operator can walk the
        # doc top-to-bottom and tick boxes.
        text = _read(CHECKLIST)
        checkbox_count = len(re.findall(r"^\s*-\s*\[\s\]", text, re.MULTILINE))
        assert checkbox_count >= 20, (
            f"checklist must have at least 20 actionable `- [ ]` "
            f"items (got {checkbox_count}) — pre-drill + three "
            f"scenarios + post-drill each need their own actionable "
            f"lines"
        )

    def test_db_primary_scenario_present(self) -> None:
        text = _read(CHECKLIST).lower()
        # The annual drill MUST exercise the G6 #3 DB-primary path.
        has_db_section = re.search(
            r"^#+\s+.*(primary|database|db).*(failover|drill|scenario)",
            text,
            re.MULTILINE,
        )
        assert has_db_section, (
            "checklist must have a scenario section exercising the "
            "DB-primary failover path (the G6 #3 runbook's §2)"
        )

    def test_reverse_proxy_scenario_present(self) -> None:
        text = _read(CHECKLIST).lower()
        has_proxy_section = re.search(
            r"^#+\s+.*(reverse proxy|caddy|proxy).*(fallback|scenario|drill)",
            text,
            re.MULTILINE,
        )
        assert has_proxy_section, (
            "checklist must have a scenario section exercising the "
            "reverse-proxy fallback path (the G6 #3 runbook's §3)"
        )

    def test_backup_restore_scenario_present(self) -> None:
        text = _read(CHECKLIST).lower()
        # Scenario C exercises the data-plane — backup chain + RPO
        # reconciliation. Named by either "backup" or "restore"
        # appearing in a section header.
        has_data_section = re.search(
            r"^#+\s+.*(backup|restore|rpo|data[\s\-]*plane)",
            text,
            re.MULTILINE,
        )
        assert has_data_section, (
            "checklist must have a scenario section for the "
            "backup/restore chain + RPO reconciliation"
        )

    def test_has_report_template(self) -> None:
        text = _read(CHECKLIST).lower()
        # The annual drill's durable output is a filled-in report
        # template. A report section keyword + a markdown table are
        # the cheapest assertion that a template exists.
        assert "report" in text, (
            "checklist must have a post-drill report section"
        )
        assert "|" in _read(CHECKLIST), (
            "checklist must have at least one markdown table (likely "
            "the report template)"
        )


# ---------------------------------------------------------------------------
# TestRtoRpoReconciliation — the checklist MUST cite the G6 #2 budgets
# (15 min / 5 min) and instruct the operator to reconcile observed vs
# budget. It must NOT re-define the numbers.
# ---------------------------------------------------------------------------
class TestRtoRpoReconciliation:
    def test_checklist_cites_15_min_rto(self) -> None:
        text = _read(CHECKLIST)
        assert re.search(r"15[\s\-]*min", text, re.IGNORECASE), (
            "checklist must cite the 15-min RTO budget so the "
            "operator can measure observed vs budget"
        )

    def test_checklist_cites_5_min_rpo(self) -> None:
        text = _read(CHECKLIST)
        assert re.search(r"5[\s\-]*min", text, re.IGNORECASE), (
            "checklist must cite the 5-min RPO budget so the "
            "operator can measure observed vs budget"
        )

    def test_checklist_names_reconciliation_action(self) -> None:
        text = _read(CHECKLIST).lower()
        # "Observed vs budget" (or "reconcile" + "budget") must appear
        # so the drill's purpose is explicit, not implicit.
        has_reconciliation = (
            ("observed" in text and "budget" in text)
            or ("reconcil" in text and "budget" in text)
        )
        assert has_reconciliation, (
            "checklist must name the observed-vs-budget reconciliation "
            "action — drilling without the comparison step is just "
            "theatre"
        )

    def test_checklist_does_not_define_rto_rpo_targets(self) -> None:
        # G6 #2 owns the *definition* of RTO ≤ 15 min / RPO ≤ 5 min.
        # This checklist may *cite* the numbers but MUST NOT contain
        # the formal "Recovery Time Objective is the..." definitions
        # that would split the truth source.
        text = _read(CHECKLIST)
        bad_rto = re.search(
            r"Recovery\s+Time\s+Objective\s+\(RTO\)\s+is\s+the",
            text,
            re.IGNORECASE,
        )
        bad_rpo = re.search(
            r"Recovery\s+Point\s+Objective\s+\(RPO\)\s+is\s+the",
            text,
            re.IGNORECASE,
        )
        assert bad_rto is None and bad_rpo is None, (
            "checklist must not define RTO / RPO formally — that's "
            "G6 #2's docs/ops/dr_rto_rpo.md scope; cite the numbers, "
            "do not redefine them"
        )


# ---------------------------------------------------------------------------
# TestCrossReferences — the checklist must point at sibling G6 / G4 docs
# and the backing scripts / workflows.
# ---------------------------------------------------------------------------
class TestCrossReferences:
    def test_references_g6_1_daily_drill(self) -> None:
        text = _read(CHECKLIST)
        assert (
            "dr-drill-daily.yml" in text or "G6 #1" in text
        ), (
            "checklist must cross-reference G6 #1 — the automated "
            "daily drill it complements (Scenario C reads the daily "
            "drill's green/red signal)"
        )

    def test_references_g6_2_rto_rpo_doc(self) -> None:
        text = _read(CHECKLIST)
        assert "dr_rto_rpo.md" in text, (
            "checklist must cross-reference docs/ops/dr_rto_rpo.md — "
            "the budget it reconciles observed values against"
        )

    def test_references_g6_3_manual_failover_doc(self) -> None:
        text = _read(CHECKLIST)
        assert "dr_manual_failover.md" in text, (
            "checklist must cross-reference docs/ops/dr_manual_failover.md "
            "— the runbook Scenarios A + B exercise end-to-end"
        )

    def test_references_g6_5_bundle_closure_slot(self) -> None:
        text = _read(CHECKLIST)
        assert ("G6 #5" in text) or ("row 1383" in text), (
            "checklist must name G6 #5 / row 1383 as owner of the "
            "bundle-closure deliverables so the aggregator slot is "
            "explicit"
        )

    def test_references_g7_observability_slot(self) -> None:
        text = _read(CHECKLIST)
        assert "G7" in text, (
            "checklist must name G7 so observability / alerting "
            "content does not silently end up in this doc"
        )

    def test_references_g4_db_failover_doc(self) -> None:
        text = _read(CHECKLIST)
        assert "db_failover.md" in text, (
            "checklist must cross-reference docs/ops/db_failover.md "
            "— §7 is the rebuild-standby procedure used in Scenario "
            "A clean-up"
        )

    def test_references_backup_selftest_script(self) -> None:
        text = _read(CHECKLIST)
        assert "backup_selftest.py" in text, (
            "checklist must cross-reference scripts/backup_selftest.py "
            "— Scenario C runs it against the latest backup artefact"
        )

    def test_referenced_files_exist(self) -> None:
        # Referential integrity: every cross-ref must resolve.
        for path in (
            RTO_RPO_DOC,
            MANUAL_FAILOVER_DOC,
            DB_FAILOVER_DOC,
            DR_DRILL_WORKFLOW,
            BACKUP_SELFTEST_SCRIPT,
        ):
            assert path.exists(), (
                f"checklist cross-references {path.relative_to(PROJECT_ROOT)} "
                f"but it does not exist on disk"
            )


# ---------------------------------------------------------------------------
# TestNonGoals — the checklist must explicitly list what it does NOT cover.
# ---------------------------------------------------------------------------
class TestNonGoals:
    def test_has_non_goals_section(self) -> None:
        text = _read(CHECKLIST).lower()
        assert (
            "does not cover" in text
            or "not cover" in text
            or ("scope" in text and "not" in text)
        ), (
            "checklist must have a scope-NOT-covered section to "
            "prevent silent scope creep into G6 #5 / G7"
        )

    def test_g6_5_marked_out_of_scope(self) -> None:
        # G6 #5 is explicitly the next row — the checklist must name
        # it in the NOT-covered section so future authors don't graft
        # bundle-closure content here.
        text = _read(CHECKLIST)
        assert "G6 #5" in text or "row 1383" in text, (
            "checklist must name G6 #5 / row 1383 as out-of-scope"
        )


# ---------------------------------------------------------------------------
# TestTrackerAlignment — TODO row 1382 is flipped + HANDOFF updated.
# ---------------------------------------------------------------------------
class TestTrackerAlignment:
    ROW_HEADLINE = "年度 DR 演練 checklist"

    def test_todo_row_headline_present(self) -> None:
        text = _read(TODO)
        assert self.ROW_HEADLINE in text, (
            "row 1382 headline literal missing from TODO.md — rename "
            "would silently mask the [x] flip below"
        )

    def test_todo_row_marked_complete(self) -> None:
        text = _read(TODO)
        assert f"- [x] {self.ROW_HEADLINE}" in text, (
            "row 1382 must flip from [ ] to [x] in the same commit "
            "that lands docs/ops/dr_annual_drill_checklist.md"
        )

    def test_row_under_g6_section(self) -> None:
        text = _read(TODO)
        lines = text.splitlines()
        g6_header_idx = None
        row_idx = None
        for i, line in enumerate(lines):
            if "### G6." in line and g6_header_idx is None:
                g6_header_idx = i
            if self.ROW_HEADLINE in line:
                row_idx = i
        assert g6_header_idx is not None, "G6 section header missing"
        assert row_idx is not None, "row 1382 line missing"
        assert row_idx > g6_header_idx, (
            "row 1382 must appear AFTER the G6 section header"
        )

    def test_handoff_names_g6_4(self) -> None:
        text = _read(HANDOFF)
        assert "G6 #4" in text, (
            "HANDOFF.md must name G6 #4 — the annual DR drill "
            "checklist landing is the headline event for this commit"
        )

    def test_handoff_names_row_1382(self) -> None:
        text = _read(HANDOFF)
        assert "row 1382" in text, "HANDOFF.md must cite TODO row 1382"

    def test_handoff_names_doc_file(self) -> None:
        text = _read(HANDOFF)
        assert "dr_annual_drill_checklist.md" in text, (
            "HANDOFF.md must point operators at "
            "docs/ops/dr_annual_drill_checklist.md"
        )


# ---------------------------------------------------------------------------
# TestG6_3SiblingGuardMigration — G6 #3's `test_no_annual_dr_checklist_doc`
# MUST be removed in this commit per the explicit-migration pattern.
# ---------------------------------------------------------------------------
class TestG6_3SiblingGuardMigration:
    def test_g6_3_no_annual_checklist_guard_removed(self) -> None:
        # G6 #3's contract carried a guard that RED-flagged any of
        # three candidate annual-checklist paths. G6 #4 lands a
        # canonical sibling path — the guard must be removed in
        # this same commit per the explicit-migration pattern carried
        # forward from G5 → G6 #1 → G6 #2 → G6 #3 → G6 #4.
        text = _read(G6_3_TEST)
        assert "def test_no_annual_dr_checklist_doc" not in text, (
            "G6 #3 sibling guard `test_no_annual_dr_checklist_doc` "
            "must be REMOVED in the same commit that lands G6 #4"
        )

    def test_g6_3_documents_migration(self) -> None:
        # Leave a breadcrumb in G6 #3's test so future readers can
        # trace why the guard was removed.
        text = _read(G6_3_TEST)
        assert (
            "G6 #4" in text
            and "row 1382" in text
            and "dr_annual_drill_checklist.md" in text
        ), (
            "G6 #3 test must document the migration — a silent "
            "removal leaves no trace of why the guard is gone"
        )


# ---------------------------------------------------------------------------
# TestScopeDisciplineSiblingRows — guard against silent scope creep
# into the remaining G6 row + G7. Explicit-migration pattern continued.
# ---------------------------------------------------------------------------
class TestScopeDisciplineSiblingRows:
    def test_no_dr_runbook_doc(self) -> None:
        # docs/ops/dr_runbook.md is row 1383's deliverable (G6 #5
        # bundle closure) — must not land here.
        assert not (
            PROJECT_ROOT / "docs" / "ops" / "dr_runbook.md"
        ).exists(), (
            "row 1383 (G6 #5) owns docs/ops/dr_runbook.md — do not "
            "land it in this commit"
        )

    def test_no_dr_drill_shell_script(self) -> None:
        # scripts/dr_drill.sh is row 1383's shell-script deliverable.
        assert not (PROJECT_ROOT / "scripts" / "dr_drill.sh").exists(), (
            "row 1383 (G6 #5) owns scripts/dr_drill.sh — do not "
            "land it in this commit"
        )

    def test_no_g7_grafana_dashboard(self) -> None:
        # G7 ships the Grafana dashboard; must not appear with G6 #4.
        assert not (
            PROJECT_ROOT
            / "deploy"
            / "observability"
            / "grafana"
            / "ha.json"
        ).exists(), (
            "G7 (row 1387) owns deploy/observability/grafana/ha.json "
            "— do not pre-commit it with G6 #4"
        )

    def test_no_g7_alert_rules_yaml(self) -> None:
        # G7 (row 1388) ships Prometheus alert rules.
        candidates = (
            PROJECT_ROOT / "deploy" / "observability" / "prometheus"
            / "alerts.yml",
            PROJECT_ROOT / "deploy" / "observability" / "alerts.yml",
            PROJECT_ROOT / "deploy" / "prometheus" / "ha-alerts.yml",
        )
        for cand in candidates:
            assert not cand.exists(), (
                f"G7 (row 1388) owns alert rules; saw unexpected "
                f"{cand.relative_to(PROJECT_ROOT)}"
            )

    def test_doc_does_not_inline_bash_script(self) -> None:
        # G6 #5 owns scripts/dr_drill.sh. The checklist may NAME
        # commands but MUST NOT ship a `#!/usr/bin/env bash` inline
        # script — that would pre-commit G6 #5.
        text = _read(CHECKLIST)
        assert "#!/usr/bin/env bash" not in text, (
            "checklist must not inline a bash script shebang — "
            "that's G6 #5's scripts/dr_drill.sh deliverable"
        )

    def test_doc_does_not_duplicate_runbook_commands(self) -> None:
        # G6 #3 owns the step-by-step command literals. The checklist
        # may *reference* steps ("Step 3 — Execute: `pg_ctl promote`")
        # but MUST NOT re-ship the full runbook inline — that would
        # split the truth source. Cheapest assertion: the checklist
        # has fewer bash code fences than the runbook.
        checklist_text = _read(CHECKLIST)
        runbook_text = _read(MANUAL_FAILOVER_DOC)
        checklist_bash_fences = len(
            re.findall(r"^```bash", checklist_text, re.MULTILINE)
        )
        runbook_bash_fences = len(
            re.findall(r"^```bash", runbook_text, re.MULTILINE)
        )
        assert checklist_bash_fences < runbook_bash_fences, (
            f"checklist bash fence count ({checklist_bash_fences}) "
            f"must be less than runbook's ({runbook_bash_fences}) — "
            f"if the checklist duplicates the runbook's commands, "
            f"the truth source is split between two docs"
        )


# ---------------------------------------------------------------------------
# TestChecklistOperationalConcreteness — the checklist must be backed by
# concrete operator actions, not abstract advice.
# ---------------------------------------------------------------------------
class TestChecklistOperationalConcreteness:
    def test_checklist_names_readyz_as_stopwatch_criterion(self) -> None:
        # /readyz 200 is the canonical "service is back" signal per
        # G6 #2 §2.4. The checklist must name it so the stopwatch-
        # stop criterion is unambiguous.
        text = _read(CHECKLIST)
        assert "/readyz" in text, (
            "checklist must name /readyz — the canonical stopwatch-"
            "stop criterion for every scenario's observed RTO"
        )

    def test_checklist_has_pre_and_post_drill_sections(self) -> None:
        # Annual drill is not just the scenarios — pre-drill prep
        # (scheduling, operator selection, staging warmup) and
        # post-drill report (reconciliation, drift findings) are
        # where half the value lives.
        text = _read(CHECKLIST).lower()
        assert (
            "pre-drill" in text
            or "pre drill" in text
            or "preparation" in text
        ), (
            "checklist must have a pre-drill preparation section"
        )
        assert (
            "post-drill" in text
            or "post drill" in text
            or "report" in text
        ), (
            "checklist must have a post-drill report / reconciliation "
            "section"
        )

    def test_checklist_names_staging_vs_production(self) -> None:
        # A drill on production would be an incident, not a drill.
        # Naming "staging" explicitly ensures the operator drills
        # against the right environment.
        text = _read(CHECKLIST).lower()
        assert "staging" in text, (
            "checklist must name 'staging' as the drill target — "
            "drilling on production would be an incident, not a "
            "drill"
        )

    def test_checklist_names_pg_ctl_promote_as_rehearsed_command(self) -> None:
        # Scenario A exercises the G6 #3 runbook whose load-bearing
        # command is `pg_ctl promote`. The checklist must cite it so
        # readers see what the drill actually rehearses.
        text = _read(CHECKLIST)
        assert "pg_ctl promote" in text, (
            "checklist must name `pg_ctl promote` — Scenario A's "
            "load-bearing command is the whole reason for the drill"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
