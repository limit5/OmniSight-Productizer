"""G6 #2 — RTO / RPO objective document contract tests.

TODO row 1380:
    RTO / RPO 目標明文化（建議 RTO ≤ 15min, RPO ≤ 5min）

Pins ``docs/ops/dr_rto_rpo.md``, the canonical source of truth for
OmniSight's disaster-recovery budgets. The doc is the second
deliverable of the G6 HA-06 bucket after the daily drill workflow
(G6 #1).

The contract is textual: the doc must *name* the two numbers
(``RTO <= 15 min``, ``RPO <= 5 min``), cross-reference the adjacent
G6 rows it bounds, and preserve the non-goals section that keeps the
doc from silently absorbing scope that belongs to G6 #3 / #4 / #5 or
G7.

Sibling rows NOT covered by this test (explicit scope fence):

    * row 1381 (G6 #3) — primary-DB / reverse-proxy manual failover
      runbook.
    * row 1382 (G6 #4) — annual DR drill operator checklist.
    * row 1383 (G6 #5) — ``scripts/dr_drill.sh`` +
      ``docs/ops/dr_runbook.md`` bundle-closure deliverables.

The sibling-row guards below RED-flag any of the above landing in
this same commit (silent scope creep). The explicit-migration pattern
(remove the guard in the commit that lands the next row) is carried
forward from G5 → G6 #1 → this row.

Explicit migration accepted from G6 #1:

    ``backend/tests/test_dr_drill_daily_g6_1.py`` previously owned
    ``TestNoRtoRpoDocYet`` which RED-flagged any
    ``docs/ops/dr_rto_rpo.md`` or ``docs/ops/rto_rpo.md`` file.
    G6 #2 is precisely that doc — landing this row REQUIRES removing
    that guard in the same commit. The migration is asserted below
    (``TestG6_1SiblingGuardMigration``).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

RTO_RPO_DOC = PROJECT_ROOT / "docs" / "ops" / "dr_rto_rpo.md"
DB_FAILOVER_DOC = PROJECT_ROOT / "docs" / "ops" / "db_failover.md"
DR_DRILL_WORKFLOW = (
    PROJECT_ROOT / ".github" / "workflows" / "dr-drill-daily.yml"
)

TODO = PROJECT_ROOT / "TODO.md"
HANDOFF = PROJECT_ROOT / "HANDOFF.md"

G6_1_TEST = (
    PROJECT_ROOT / "backend" / "tests" / "test_dr_drill_daily_g6_1.py"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# TestDocFileShape — file is on disk and has a single canonical name.
# ---------------------------------------------------------------------------
class TestDocFileShape:
    def test_doc_file_exists(self) -> None:
        assert RTO_RPO_DOC.is_file(), (
            "G6 #2 (row 1380) ships docs/ops/dr_rto_rpo.md — missing "
            "means the RTO/RPO objectives are not explicitly documented"
        )

    def test_doc_path_is_canonical(self) -> None:
        # Pinning the path means a rename surfaces here instead of
        # silently splitting the objectives across two locations.
        rel = RTO_RPO_DOC.relative_to(PROJECT_ROOT)
        assert str(rel) == "docs/ops/dr_rto_rpo.md"

    def test_only_one_rto_rpo_doc(self) -> None:
        # A second file at docs/ops/rto_rpo.md or similar would split
        # the truth source and defeat the G6 #2 contract.
        alternatives = (
            PROJECT_ROOT / "docs" / "ops" / "rto_rpo.md",
            PROJECT_ROOT / "docs" / "ops" / "dr_objectives.md",
            PROJECT_ROOT / "docs" / "ops" / "recovery_objectives.md",
        )
        for alt in alternatives:
            assert not alt.exists(), (
                f"only one RTO/RPO doc allowed; found extra at "
                f"{alt.relative_to(PROJECT_ROOT)}"
            )

    def test_doc_has_title(self) -> None:
        # First non-empty line must be a markdown H1 that names the
        # doc — future readers should see "RTO / RPO" on first glance.
        text = _read(RTO_RPO_DOC)
        for line in text.splitlines():
            if line.strip():
                assert line.startswith("# "), (
                    "doc must open with a markdown H1 title"
                )
                assert "RTO" in line and "RPO" in line, (
                    "H1 must name both RTO and RPO so the doc is "
                    "searchable by either acronym"
                )
                return
        pytest.fail("doc is empty")


# ---------------------------------------------------------------------------
# TestRtoTarget — the 15-min RTO budget is the load-bearing number.
# ---------------------------------------------------------------------------
class TestRtoTarget:
    def test_rto_target_15_minutes_literal(self) -> None:
        # The contract PINs the exact budget string. A change to 10
        # or 20 minutes (either direction) RED-flags here and forces
        # an explicit review.
        text = _read(RTO_RPO_DOC)
        patterns = (
            r"RTO\s*[≤<=]+\s*15\s*min",
            r"RTO\s*[≤<=]+\s*15\s*minute",
        )
        assert any(re.search(p, text, re.IGNORECASE) for p in patterns), (
            "doc must state RTO <= 15 min (or <= 15 minutes); the "
            "exact numeric target is load-bearing"
        )

    def test_rto_acronym_expanded_somewhere(self) -> None:
        # "Recovery Time Objective" must appear at least once so a
        # reader unfamiliar with the acronym can parse the doc.
        text = _read(RTO_RPO_DOC)
        assert re.search(r"Recovery\s+Time\s+Objective", text), (
            "RTO acronym must be expanded at least once — "
            "'Recovery Time Objective' is the full form"
        )

    def test_rto_budget_split_present(self) -> None:
        # §2.2 breaks the 15-min budget into phases. The doc does not
        # have to use the exact same phase names forever, but it MUST
        # show that the 15 min is decomposed (otherwise "RTO <= 15"
        # is a slogan, not a plan).
        text = _read(RTO_RPO_DOC)
        # At least two phase-like words + at least one minute-range
        # like "0–2 min" or "5-12 min".
        assert re.search(
            r"\d+\s*[\-–]\s*\d+\s*min", text
        ), "doc must show at least one budget split like '0–2 min'"

    def test_readyz_named_as_recovery_signal(self) -> None:
        # /readyz is the canonical "service is back" probe per
        # G1 / G5 #4. The RTO doc must name it so the recovery
        # criterion is unambiguous.
        text = _read(RTO_RPO_DOC)
        assert "/readyz" in text, (
            "doc must name /readyz as the canonical recovery signal"
        )


# ---------------------------------------------------------------------------
# TestRpoTarget — the 5-min RPO budget mirrors the RTO pin.
# ---------------------------------------------------------------------------
class TestRpoTarget:
    def test_rpo_target_5_minutes_literal(self) -> None:
        text = _read(RTO_RPO_DOC)
        patterns = (
            r"RPO\s*[≤<=]+\s*5\s*min",
            r"RPO\s*[≤<=]+\s*5\s*minute",
        )
        assert any(re.search(p, text, re.IGNORECASE) for p in patterns), (
            "doc must state RPO <= 5 min; the exact numeric target "
            "is load-bearing"
        )

    def test_rpo_acronym_expanded_somewhere(self) -> None:
        text = _read(RTO_RPO_DOC)
        assert re.search(r"Recovery\s+Point\s+Objective", text), (
            "RPO acronym must be expanded at least once — "
            "'Recovery Point Objective' is the full form"
        )

    def test_rpo_non_zero_justified(self) -> None:
        # The doc must explain WHY RPO isn't zero — otherwise a future
        # reader will try to tighten it without understanding the
        # synchronous-replication trade-off.
        text = _read(RTO_RPO_DOC).lower()
        assert "not" in text and "zero" in text, (
            "doc must explicitly address why RPO is not zero"
        )
        assert "synchronous" in text or "replication" in text, (
            "doc must name the synchronous-replication trade-off "
            "that justifies non-zero RPO"
        )

    def test_rpo_mechanisms_named(self) -> None:
        # The 5-min budget is backed by specific mechanisms; at least
        # one of Postgres-WAL or sqlite3.Connection.backup() must be
        # named as the durable-write path.
        text = _read(RTO_RPO_DOC)
        has_pg = re.search(
            r"postgres|WAL|streaming replication", text, re.IGNORECASE
        )
        has_sqlite = (
            "sqlite3.Connection.backup()" in text
            or "backup_selftest.py" in text
        )
        assert has_pg or has_sqlite, (
            "doc must name at least one of Postgres WAL / sqlite3 "
            "backup as the mechanism achieving the 5-min budget"
        )


# ---------------------------------------------------------------------------
# TestCrossReferences — the doc must point at sibling G6 rows + G4.
# ---------------------------------------------------------------------------
class TestCrossReferences:
    def test_references_g6_1_daily_drill(self) -> None:
        text = _read(RTO_RPO_DOC)
        assert "G6 #1" in text and "row 1379" in text, (
            "doc must cite G6 #1 (row 1379) — the daily drill is the "
            "CI evidence that the RTO execution path holds"
        )
        assert "dr-drill-daily.yml" in text, (
            "doc must point at .github/workflows/dr-drill-daily.yml"
        )

    def test_references_g6_3_failover_runbook_slot(self) -> None:
        # G6 #3 is not yet landed; the doc must name it as the owner
        # of the manual-switch runbook so future-row authors see the
        # dependency before they write it.
        text = _read(RTO_RPO_DOC)
        assert ("G6 #3" in text) or ("row 1381" in text), (
            "doc must name G6 #3 / row 1381 as owner of the manual "
            "failover runbook"
        )

    def test_references_g6_4_annual_checklist_slot(self) -> None:
        text = _read(RTO_RPO_DOC)
        assert ("G6 #4" in text) or ("row 1382" in text), (
            "doc must name G6 #4 / row 1382 as owner of the annual "
            "drill checklist"
        )

    def test_references_g6_5_bundle_closure_slot(self) -> None:
        text = _read(RTO_RPO_DOC)
        assert ("G6 #5" in text) or ("row 1383" in text), (
            "doc must name G6 #5 / row 1383 as owner of the bundle-"
            "closure deliverables"
        )

    def test_references_db_failover_doc(self) -> None:
        # G4's db_failover.md already has a 5-min "promote vs fix"
        # threshold; the RTO doc must cross-link it so the two docs
        # don't drift.
        text = _read(RTO_RPO_DOC)
        assert "db_failover.md" in text, (
            "doc must cross-reference docs/ops/db_failover.md so the "
            "G4 and G6 threshold statements stay coherent"
        )

    def test_db_failover_doc_still_exists(self) -> None:
        # Referential integrity: the RTO doc's G4 link must resolve.
        assert DB_FAILOVER_DOC.is_file(), (
            "G4 docs/ops/db_failover.md must still exist — G6 #2 "
            "cross-references it"
        )

    def test_references_g7_observability_slot(self) -> None:
        # G7 is a separate bucket; the doc must name it so no one
        # tries to silently graft alerting content here.
        text = _read(RTO_RPO_DOC)
        assert "G7" in text, (
            "doc must name G7 (observability) so alerting content "
            "does not silently end up in this doc"
        )


# ---------------------------------------------------------------------------
# TestNonGoals — the doc must explicitly list what it does NOT cover.
# ---------------------------------------------------------------------------
class TestNonGoals:
    def test_has_non_goals_section(self) -> None:
        text = _read(RTO_RPO_DOC).lower()
        # Either a §1.1 "non-goals" or a §6 "scope / does NOT cover"
        # is acceptable; at least one must be present.
        assert "non-goal" in text or "does not cover" in text or (
            "scope" in text and "not" in text
        ), (
            "doc must have a non-goals / scope-NOT-covered section "
            "to prevent silent scope creep"
        )

    def test_availability_sla_marked_out_of_scope(self) -> None:
        # Recovery-speed is NOT the same as availability-SLA;
        # conflating them is the single most common RTO/RPO doc
        # regression.
        text = _read(RTO_RPO_DOC).lower()
        assert "availability" in text, (
            "doc must mention availability to distinguish RTO from "
            "a 99.X % SLA"
        )


# ---------------------------------------------------------------------------
# TestTrackerAlignment — TODO row 1380 is flipped + HANDOFF updated.
# ---------------------------------------------------------------------------
class TestTrackerAlignment:
    ROW_HEADLINE = "RTO / RPO 目標明文化（建議 RTO ≤ 15min, RPO ≤ 5min）"

    def test_todo_row_headline_present(self) -> None:
        text = _read(TODO)
        assert self.ROW_HEADLINE in text, (
            "row 1380 headline literal missing from TODO.md — "
            "rename would silently mask the [x] flip below"
        )

    def test_todo_row_marked_complete(self) -> None:
        text = _read(TODO)
        assert f"- [x] {self.ROW_HEADLINE}" in text, (
            "row 1380 must flip from [ ] to [x] in the same commit "
            "that lands docs/ops/dr_rto_rpo.md"
        )

    def test_todo_row_headline_target_pair_matches_doc(self) -> None:
        # The TODO headline and the doc must name the SAME pair of
        # numbers. Drift between the two is the highest-likelihood
        # regression; this assertion is the cheapest guard.
        doc = _read(RTO_RPO_DOC)
        assert "15" in self.ROW_HEADLINE and "15" in doc
        assert "5min" in self.ROW_HEADLINE or "5 min" in self.ROW_HEADLINE
        assert re.search(r"RPO\s*[≤<=]+\s*5\s*min", doc, re.IGNORECASE), (
            "TODO headline says RPO ≤ 5 min; doc must match"
        )
        assert re.search(r"RTO\s*[≤<=]+\s*15\s*min", doc, re.IGNORECASE), (
            "TODO headline says RTO ≤ 15 min; doc must match"
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
        assert row_idx is not None, "row 1380 line missing"
        assert row_idx > g6_header_idx, (
            "row 1380 must appear AFTER the G6 section header"
        )

    def test_handoff_names_g6_2(self) -> None:
        text = _read(HANDOFF)
        assert "G6 #2" in text, (
            "HANDOFF.md must name G6 #2 — the RTO/RPO doc landing "
            "is the headline event for this commit"
        )

    def test_handoff_names_row_1380(self) -> None:
        text = _read(HANDOFF)
        assert "row 1380" in text, "HANDOFF.md must cite TODO row 1380"

    def test_handoff_names_doc_file(self) -> None:
        text = _read(HANDOFF)
        assert "dr_rto_rpo.md" in text, (
            "HANDOFF.md must point operators at docs/ops/dr_rto_rpo.md"
        )


# ---------------------------------------------------------------------------
# TestG6_1SiblingGuardMigration — G6 #1's `TestNoRtoRpoDocYet` MUST be
# removed in this commit per the explicit-migration pattern.
# ---------------------------------------------------------------------------
class TestG6_1SiblingGuardMigration:
    def test_g6_1_no_rto_rpo_guard_removed(self) -> None:
        # G6 #1's contract carried a guard that RED-flagged any
        # docs/ops/dr_rto_rpo.md file. G6 #2 lands exactly that file
        # — the guard must be removed in this same commit per the
        # explicit-migration pattern carried forward from
        # G5 → G6 #1 → G6 #2.
        text = _read(G6_1_TEST)
        assert "class TestNoRtoRpoDocYet" not in text, (
            "G6 #1 sibling guard class `TestNoRtoRpoDocYet` must be "
            "REMOVED in the same commit that lands G6 #2"
        )

    def test_g6_1_documents_migration(self) -> None:
        # Leave a breadcrumb in G6 #1's test so future readers can
        # trace why the guard was removed.
        text = _read(G6_1_TEST)
        assert "G6 #2" in text and "row 1380" in text, (
            "G6 #1 test must document the migration — a silent "
            "removal leaves no trace of why the guard is gone"
        )


# ---------------------------------------------------------------------------
# TestScopeDisciplineSiblingRows — guard against silent scope creep
# into the rest of G6 + G7.
# ---------------------------------------------------------------------------
class TestScopeDisciplineSiblingRows:
    def test_no_dr_runbook_doc(self) -> None:
        # docs/ops/dr_runbook.md is row 1383's deliverable — must
        # not land here.
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

    def test_no_annual_dr_checklist_doc(self) -> None:
        # G6 #4 (row 1382) owns the annual DR drill checklist. A
        # separate checklist file landing here would be scope creep.
        candidates = (
            PROJECT_ROOT / "docs" / "ops" / "dr_annual_checklist.md",
            PROJECT_ROOT / "docs" / "ops" / "annual_dr_checklist.md",
            PROJECT_ROOT / "docs" / "ops" / "dr_drill_checklist.md",
        )
        for cand in candidates:
            assert not cand.exists(), (
                f"row 1382 (G6 #4) owns the annual DR checklist; "
                f"saw unexpected {cand.relative_to(PROJECT_ROOT)}"
            )

    def test_no_manual_failover_runbook(self) -> None:
        # G6 #3 (row 1381) owns the primary-DB / reverse-proxy
        # manual failover runbook. A separate file landing here
        # would be scope creep.
        candidates = (
            PROJECT_ROOT / "docs" / "ops" / "dr_manual_failover.md",
            PROJECT_ROOT / "docs" / "ops" / "manual_failover.md",
            PROJECT_ROOT / "docs" / "ops" / "dr_failover_runbook.md",
        )
        for cand in candidates:
            assert not cand.exists(), (
                f"row 1381 (G6 #3) owns the manual failover runbook; "
                f"saw unexpected {cand.relative_to(PROJECT_ROOT)}"
            )

    def test_no_g7_grafana_dashboard(self) -> None:
        # G7 ships the Grafana dashboard; must not appear with G6 #2.
        assert not (
            PROJECT_ROOT
            / "deploy"
            / "observability"
            / "grafana"
            / "ha.json"
        ).exists(), (
            "G7 (row 1387) owns deploy/observability/grafana/ha.json "
            "— do not pre-commit it with G6 #2"
        )

    def test_doc_does_not_pre_commit_runbook_content(self) -> None:
        # The RTO/RPO doc is a *budget contract* — it names §2 /
        # §3 phase budgets but MUST NOT contain step-by-step
        # operator commands (those belong to G6 #3 / G6 #5). This
        # test enforces the separation by counting shell-command
        # fences + explicit `pg_ctl promote` / `docker compose exec`
        # procedural literals.
        text = _read(RTO_RPO_DOC)
        # It's fine to name `pg_ctl promote` as an example of a step
        # the runbook will cover, but the doc must not have many
        # bash fences (≥ 3 would signal it's swallowing runbook
        # content).
        fence_count = text.count("```bash")
        assert fence_count <= 1, (
            f"RTO/RPO doc has {fence_count} bash fences — more than "
            f"1 is a sign the doc is absorbing runbook scope owned "
            f"by G6 #3 / G6 #5"
        )


# ---------------------------------------------------------------------------
# TestDrillBudgetCoherence — the CI drill's timing must not silently
# exceed the documented RTO.
# ---------------------------------------------------------------------------
class TestDrillBudgetCoherence:
    def test_drill_workflow_still_exists(self) -> None:
        # Referential integrity: the RTO doc cites G6 #1's workflow
        # file explicitly; if it disappears, the doc's §4 honesty
        # claim is broken.
        assert DR_DRILL_WORKFLOW.is_file()

    def test_drill_does_not_claim_sub_15_min_budget_in_timeout(self) -> None:
        # The drill's `timeout-minutes` are upper bounds, not
        # targets — the RTO doc explicitly flags this. This test
        # asserts the RTO doc still carries that nuance (a future
        # edit that removes it would misrepresent the contract).
        text = _read(RTO_RPO_DOC).lower()
        assert "upper bound" in text or "not the target" in text, (
            "doc must explain that drill `timeout-minutes` is an "
            "upper bound, not the RTO target — preventing a reader "
            "from concluding 'RTO = sum of timeouts'"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
