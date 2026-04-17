"""G6 #3 — Manual failover runbook contract tests.

TODO row 1381:
    Runbook：資料庫 primary 掛掉的手動切換步驟、反向代理故障的 fallback

Pins ``docs/ops/dr_manual_failover.md``, the operator-facing
step-by-step for the two failure modes the G6 #2 RTO budget is sized
against:

    1. Database primary host dies → promote the standby and re-point
       the application within the 15-min RTO.
    2. Reverse proxy (Caddy) fails → fall back to a direct-to-backend
       or replacement-edge path while keeping the RTO budget intact.

The runbook is the third deliverable of the G6 HA-06 bucket after the
daily drill workflow (G6 #1) and the RTO/RPO objective doc (G6 #2).

Sibling rows NOT covered by this test (explicit scope fence):

    * row 1383 (G6 #5) — ``scripts/dr_drill.sh`` +
      ``docs/ops/dr_runbook.md`` bundle-closure deliverables.

Previously row 1382 (G6 #4, annual DR drill checklist) was also
guarded here via ``test_no_annual_dr_checklist_doc``. G6 #4 landed
as ``docs/ops/dr_annual_drill_checklist.md`` with contract
``backend/tests/test_dr_annual_drill_checklist_g6_4.py`` — the
guard was removed in that commit per the explicit-migration pattern.

The sibling-row guards below RED-flag any of the above landing in
this same commit (silent scope creep). The explicit-migration pattern
(remove the guard in the commit that lands the next row) is carried
forward from G5 → G6 #1 → G6 #2 → this row → G6 #4.

Explicit migration accepted from G6 #2:

    ``backend/tests/test_dr_rto_rpo_g6_2.py`` previously owned
    ``TestScopeDisciplineSiblingRows::test_no_manual_failover_runbook``
    which RED-flagged any of the candidate paths
    ``docs/ops/dr_manual_failover.md`` /
    ``docs/ops/manual_failover.md`` /
    ``docs/ops/dr_failover_runbook.md``. G6 #3 lands precisely the
    first of these — the guard MUST be removed in the same commit.
    The migration is asserted below
    (``TestG6_2SiblingGuardMigration``).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

RUNBOOK = PROJECT_ROOT / "docs" / "ops" / "dr_manual_failover.md"
RTO_RPO_DOC = PROJECT_ROOT / "docs" / "ops" / "dr_rto_rpo.md"
DB_FAILOVER_DOC = PROJECT_ROOT / "docs" / "ops" / "db_failover.md"
BLUE_GREEN_DOC = PROJECT_ROOT / "docs" / "ops" / "blue_green_runbook.md"
DR_DRILL_WORKFLOW = (
    PROJECT_ROOT / ".github" / "workflows" / "dr-drill-daily.yml"
)
CADDYFILE = PROJECT_ROOT / "deploy" / "reverse-proxy" / "Caddyfile"

TODO = PROJECT_ROOT / "TODO.md"
HANDOFF = PROJECT_ROOT / "HANDOFF.md"

G6_2_TEST = (
    PROJECT_ROOT / "backend" / "tests" / "test_dr_rto_rpo_g6_2.py"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# TestRunbookFileShape — file is on disk and has a single canonical name.
# ---------------------------------------------------------------------------
class TestRunbookFileShape:
    def test_runbook_file_exists(self) -> None:
        assert RUNBOOK.is_file(), (
            "G6 #3 (row 1381) ships docs/ops/dr_manual_failover.md — "
            "missing means operators paged at 3am have no manual-switch "
            "playbook"
        )

    def test_runbook_path_is_canonical(self) -> None:
        # Pinning the path means a rename surfaces here instead of
        # silently splitting the playbook across two locations.
        rel = RUNBOOK.relative_to(PROJECT_ROOT)
        assert str(rel) == "docs/ops/dr_manual_failover.md"

    def test_only_one_manual_failover_doc(self) -> None:
        # A second file at one of the candidate paths would split the
        # truth source and defeat the G6 #3 contract.
        alternatives = (
            PROJECT_ROOT / "docs" / "ops" / "manual_failover.md",
            PROJECT_ROOT / "docs" / "ops" / "dr_failover_runbook.md",
            PROJECT_ROOT / "docs" / "ops" / "primary_failover.md",
            PROJECT_ROOT / "docs" / "ops" / "proxy_failover.md",
        )
        for alt in alternatives:
            assert not alt.exists(), (
                f"only one manual failover runbook allowed; found "
                f"extra at {alt.relative_to(PROJECT_ROOT)}"
            )

    def test_runbook_has_title(self) -> None:
        # First non-empty line must be a markdown H1 that names the
        # doc — future readers should see "Manual Failover" on first
        # glance.
        text = _read(RUNBOOK)
        for line in text.splitlines():
            if line.strip():
                assert line.startswith("# "), (
                    "runbook must open with a markdown H1 title"
                )
                lower = line.lower()
                assert "failover" in lower, (
                    "H1 must name 'failover' so the doc is searchable"
                )
                return
        pytest.fail("runbook is empty")

    def test_runbook_in_docs_ops(self) -> None:
        # Other ops docs link by the exact path; moving it would
        # silently break those references.
        assert RUNBOOK.parent == PROJECT_ROOT / "docs" / "ops", (
            "runbook must live at docs/ops/dr_manual_failover.md"
        )


# ---------------------------------------------------------------------------
# TestRunbookCoversBothFailureModes — the row text names two modes;
# the runbook MUST address both or it fails the row's contract.
# ---------------------------------------------------------------------------
class TestRunbookCoversBothFailureModes:
    def test_db_primary_failover_section_present(self) -> None:
        text = _read(RUNBOOK).lower()
        # "primary" must appear in a header context discussing DB
        # failover. We assert a header-like line names primary.
        has_primary_section = re.search(
            r"^#+\s+.*primary.*failover", text, re.MULTILINE
        )
        assert has_primary_section, (
            "runbook must have a section header naming the database "
            "primary failover path (row 1381 names it explicitly)"
        )

    def test_reverse_proxy_section_present(self) -> None:
        text = _read(RUNBOOK).lower()
        # "reverse proxy" or "caddy" in a header — the row text says
        # "反向代理故障的 fallback" which is reverse-proxy fallback.
        has_proxy_section = (
            re.search(r"^#+\s+.*(reverse proxy|caddy)", text, re.MULTILINE)
            is not None
        )
        assert has_proxy_section, (
            "runbook must have a section header naming the reverse "
            "proxy / Caddy fallback path (row 1381 names it explicitly)"
        )

    def test_pg_ctl_promote_named(self) -> None:
        # The DB-primary failover step is `pg_ctl promote` — naming
        # the literal command is the cheapest way to confirm the
        # runbook is operationally specific.
        text = _read(RUNBOOK)
        assert "pg_ctl promote" in text, (
            "runbook must name `pg_ctl promote` — the load-bearing "
            "command for the manual primary-down switch"
        )

    def test_caddy_reload_or_restart_named(self) -> None:
        # The reverse-proxy fallback must name at least one of the
        # canonical Caddy operator commands.
        text = _read(RUNBOOK)
        assert (
            "caddy reload" in text
            or "caddy validate" in text
            or "restart caddy" in text
        ), (
            "runbook must name at least one canonical Caddy operator "
            "command (caddy reload / caddy validate / restart caddy)"
        )

    def test_readyz_named_as_recovery_signal(self) -> None:
        # /readyz is the canonical "service is back" probe per
        # G6 #2 §2.4. The runbook must name it so the recovery
        # criterion is unambiguous.
        text = _read(RUNBOOK)
        assert "/readyz" in text, (
            "runbook must name /readyz as the canonical recovery "
            "signal — every numbered step's exit criterion must "
            "reduce to /readyz 200"
        )

    def test_decision_tree_present(self) -> None:
        # Operators paged at 3am need an entry-point decision tree;
        # this is the cheapest assertion that one exists.
        text = _read(RUNBOOK).lower()
        assert "decision tree" in text or "what is broken" in text, (
            "runbook must open with an operator-facing decision tree "
            "so the oncall does not have to read the whole doc to "
            "decide which section applies"
        )


# ---------------------------------------------------------------------------
# TestRtoBudgetAlignment — the runbook's step budgets must sum to the
# G6 #2 15-min RTO contract.
# ---------------------------------------------------------------------------
class TestRtoBudgetAlignment:
    def test_runbook_cites_15_min_rto(self) -> None:
        # The runbook is sized against G6 #2's RTO; naming the literal
        # number ensures drift is visible (G6 #2 doc and TODO already
        # pin the 15 min — this is the third pin).
        text = _read(RUNBOOK)
        assert re.search(r"15[\s\-]*min", text, re.IGNORECASE), (
            "runbook must cite the 15-min RTO budget so operators "
            "see the time pressure inline"
        )

    def test_runbook_uses_phase_split(self) -> None:
        # G6 #2 §2.2 splits 15 min into 0-2 / 2-5 / 5-12 / 12-15.
        # The runbook should adopt the same split (or at least show
        # numbered minute ranges) so a future reader can map step to
        # budget.
        text = _read(RUNBOOK)
        # At least two budget ranges like "0-2 min", "5-12 min".
        ranges = re.findall(r"\d+\s*[\-–]\s*\d+\s*min", text)
        assert len(ranges) >= 2, (
            "runbook must show at least two minute-range budgets so "
            "the steps map to G6 #2's 15-min phase split"
        )

    def test_runbook_names_rpo_5_min(self) -> None:
        # The DB primary failover path is bounded by RPO too — under
        # async replication the standby may be missing the last
        # `replay_lag` of writes. Naming the 5-min RPO ensures the
        # operator knows to check `replay_lag` against the budget.
        text = _read(RUNBOOK)
        assert re.search(r"5[\s\-]*min", text), (
            "runbook must name the 5-min RPO so the operator checks "
            "`replay_lag` against the budget at promote time"
        )


# ---------------------------------------------------------------------------
# TestCrossReferences — the runbook must point at sibling G6 / G4 / G3
# docs and the script-backed primitives.
# ---------------------------------------------------------------------------
class TestCrossReferences:
    def test_references_g6_2_rto_rpo_doc(self) -> None:
        text = _read(RUNBOOK)
        assert "dr_rto_rpo.md" in text, (
            "runbook must cross-reference docs/ops/dr_rto_rpo.md — "
            "the budget it is sized against"
        )

    def test_references_g4_db_failover_doc(self) -> None:
        text = _read(RUNBOOK)
        assert "db_failover.md" in text, (
            "runbook must cross-reference docs/ops/db_failover.md — "
            "the canonical PG HA + cutover playbook this one extracts"
        )

    def test_references_g6_1_daily_drill(self) -> None:
        text = _read(RUNBOOK)
        # Either name the workflow file or the row marker.
        assert "dr-drill-daily.yml" in text or "G6 #1" in text, (
            "runbook must cite G6 #1 (the daily drill) so operators "
            "know to re-run the drill after a manual failover"
        )

    def test_references_g6_4_annual_checklist_slot(self) -> None:
        # G6 #4 is not yet landed; the runbook must name it as the
        # owner so future-row authors see the dependency before they
        # write the checklist.
        text = _read(RUNBOOK)
        assert ("G6 #4" in text) or ("row 1382" in text), (
            "runbook must name G6 #4 / row 1382 as owner of the "
            "annual drill checklist — recovery telemetry from this "
            "runbook feeds that doc"
        )

    def test_references_g6_5_bundle_closure_slot(self) -> None:
        text = _read(RUNBOOK)
        assert ("G6 #5" in text) or ("row 1383" in text), (
            "runbook must name G6 #5 / row 1383 as owner of the "
            "bundle-closure deliverables (dr_drill.sh + dr_runbook.md)"
        )

    def test_references_g7_observability_slot(self) -> None:
        # G7 is a separate bucket; the runbook must name it so no one
        # tries to silently graft alerting content here.
        text = _read(RUNBOOK)
        assert "G7" in text, (
            "runbook must name G7 so observability content does not "
            "silently end up in this doc"
        )

    def test_caddyfile_path_referenced(self) -> None:
        # The reverse-proxy section refers to the canonical Caddyfile
        # path; if the path moves, the runbook is silently wrong.
        text = _read(RUNBOOK)
        assert "deploy/reverse-proxy/Caddyfile" in text, (
            "runbook must name the canonical Caddyfile path so the "
            "operator looks at the right file"
        )

    def test_referenced_files_exist(self) -> None:
        # Referential integrity: every cross-ref must resolve.
        for path in (
            RTO_RPO_DOC,
            DB_FAILOVER_DOC,
            BLUE_GREEN_DOC,
            DR_DRILL_WORKFLOW,
            CADDYFILE,
        ):
            assert path.exists(), (
                f"runbook cross-references {path.relative_to(PROJECT_ROOT)} "
                f"but it does not exist on disk"
            )


# ---------------------------------------------------------------------------
# TestNonGoals — the runbook must explicitly list what it does NOT cover.
# ---------------------------------------------------------------------------
class TestNonGoals:
    def test_has_non_goals_section(self) -> None:
        text = _read(RUNBOOK).lower()
        assert "does not cover" in text or "not cover" in text or (
            "scope" in text and "not" in text
        ), (
            "runbook must have a scope-NOT-covered section to prevent "
            "silent scope creep into G6 #4 / G6 #5 / G7"
        )

    def test_pitr_marked_out_of_scope(self) -> None:
        # PITR / WAL archival is explicitly out of scope per G4
        # `db_failover.md` §1.2 and is a frequent scope-creep target
        # for failover docs.
        text = _read(RUNBOOK).lower()
        assert "pitr" in text or "wal archival" in text, (
            "runbook must explicitly mention PITR / WAL archival as "
            "out of scope (mirrors G4 db_failover.md §1.2)"
        )


# ---------------------------------------------------------------------------
# TestTrackerAlignment — TODO row 1381 is flipped + HANDOFF updated.
# ---------------------------------------------------------------------------
class TestTrackerAlignment:
    ROW_HEADLINE = (
        "Runbook：資料庫 primary 掛掉的手動切換步驟、反向代理故障的 fallback"
    )

    def test_todo_row_headline_present(self) -> None:
        text = _read(TODO)
        assert self.ROW_HEADLINE in text, (
            "row 1381 headline literal missing from TODO.md — rename "
            "would silently mask the [x] flip below"
        )

    def test_todo_row_marked_complete(self) -> None:
        text = _read(TODO)
        assert f"- [x] {self.ROW_HEADLINE}" in text, (
            "row 1381 must flip from [ ] to [x] in the same commit "
            "that lands docs/ops/dr_manual_failover.md"
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
        assert row_idx is not None, "row 1381 line missing"
        assert row_idx > g6_header_idx, (
            "row 1381 must appear AFTER the G6 section header"
        )

    def test_handoff_names_g6_3(self) -> None:
        text = _read(HANDOFF)
        assert "G6 #3" in text, (
            "HANDOFF.md must name G6 #3 — the manual failover runbook "
            "landing is the headline event for this commit"
        )

    def test_handoff_names_row_1381(self) -> None:
        text = _read(HANDOFF)
        assert "row 1381" in text, "HANDOFF.md must cite TODO row 1381"

    def test_handoff_names_doc_file(self) -> None:
        text = _read(HANDOFF)
        assert "dr_manual_failover.md" in text, (
            "HANDOFF.md must point operators at "
            "docs/ops/dr_manual_failover.md"
        )


# ---------------------------------------------------------------------------
# TestG6_2SiblingGuardMigration — G6 #2's `test_no_manual_failover_runbook`
# MUST be removed in this commit per the explicit-migration pattern.
# ---------------------------------------------------------------------------
class TestG6_2SiblingGuardMigration:
    def test_g6_2_no_manual_failover_guard_removed(self) -> None:
        # G6 #2's contract carried a guard that RED-flagged any of
        # the candidate paths (incl. dr_manual_failover.md). G6 #3
        # lands exactly that file — the guard must be removed in
        # this same commit per the explicit-migration pattern carried
        # forward from G5 → G6 #1 → G6 #2 → G6 #3.
        text = _read(G6_2_TEST)
        assert "def test_no_manual_failover_runbook" not in text, (
            "G6 #2 sibling guard `test_no_manual_failover_runbook` "
            "must be REMOVED in the same commit that lands G6 #3"
        )

    def test_g6_2_documents_migration(self) -> None:
        # Leave a breadcrumb in G6 #2's test so future readers can
        # trace why the guard was removed.
        text = _read(G6_2_TEST)
        assert (
            "G6 #3" in text
            and "row 1381" in text
            and "dr_manual_failover.md" in text
        ), (
            "G6 #2 test must document the migration — a silent "
            "removal leaves no trace of why the guard is gone"
        )


# ---------------------------------------------------------------------------
# TestScopeDisciplineSiblingRows — guard against silent scope creep
# into the rest of G6 + G7. Explicit-migration pattern continued.
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

    # NOTE: `test_no_annual_dr_checklist_doc` was removed when G6 #4
    # (row 1382) landed as `docs/ops/dr_annual_drill_checklist.md`
    # (contract: `backend/tests/test_dr_annual_drill_checklist_g6_4.py`).
    # Explicit-migration pattern carried forward from
    # G5 #3→#4→#5→#6 → G6 #1 → G6 #2 → G6 #3 → G6 #4 (step 8).

    def test_no_g7_grafana_dashboard(self) -> None:
        # G7 ships the Grafana dashboard; must not appear with G6 #3.
        assert not (
            PROJECT_ROOT
            / "deploy"
            / "observability"
            / "grafana"
            / "ha.json"
        ).exists(), (
            "G7 (row 1387) owns deploy/observability/grafana/ha.json "
            "— do not pre-commit it with G6 #3"
        )

    def test_no_g7_alert_rules_yaml(self) -> None:
        # G7 (row 1388) ships Prometheus alert rules. A separate
        # alert-rules YAML landing here would be scope creep.
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

    def test_doc_does_not_pre_commit_dr_drill_sh(self) -> None:
        # The runbook may name `dr_drill.sh` as a future deliverable
        # (cross-reference) but MUST NOT inline a candidate
        # implementation. Counting the literal as a code-fence
        # (e.g. ```bash starting with `dr_drill.sh`) catches the
        # most likely scope-creep shape.
        text = _read(RUNBOOK)
        # Allow naming the script in prose; forbid an inlined
        # implementation block that would pre-commit G6 #5.
        assert "#!/usr/bin/env bash" not in text, (
            "runbook must not inline a bash script — that's G6 #5's "
            "scripts/dr_drill.sh deliverable"
        )

    def test_doc_does_not_define_rto_rpo_targets(self) -> None:
        # G6 #2 owns the *definition* of RTO ≤ 15 min / RPO ≤ 5 min.
        # This runbook may *cite* the numbers but MUST NOT contain
        # the formal "Recovery Time Objective" / "Recovery Point
        # Objective" definitions that would split the truth source.
        text = _read(RUNBOOK)
        # We allow the acronyms (RTO / RPO) and the numbers, but
        # forbid the expanded definitions which belong to G6 #2.
        bad = re.search(
            r"Recovery\s+Time\s+Objective\s+\(RTO\)\s+is\s+the",
            text,
            re.IGNORECASE,
        )
        assert bad is None, (
            "runbook must not define RTO formally — that's G6 #2's "
            "docs/ops/dr_rto_rpo.md scope; cite the number, do not "
            "redefine it"
        )


# ---------------------------------------------------------------------------
# TestRunbookOperationalConcreteness — the runbook must be backed by
# real script primitives, not abstract advice.
# ---------------------------------------------------------------------------
class TestRunbookOperationalConcreteness:
    def test_caddyfile_exists(self) -> None:
        # Referential integrity: the runbook tells operators to look
        # at the Caddyfile; if it disappears the runbook is wrong.
        assert CADDYFILE.is_file(), (
            "deploy/reverse-proxy/Caddyfile must still exist — G6 #3 "
            "runbook references it as the source of truth for the "
            ":443 listener"
        )

    def test_runbook_has_curl_readyz_examples(self) -> None:
        # The verify steps lean on `curl /readyz` — naming the
        # canonical command form ensures the runbook is operationally
        # specific.
        text = _read(RUNBOOK)
        assert "curl" in text and "/readyz" in text, (
            "runbook must show concrete `curl … /readyz` checks so "
            "operators don't have to invent the verification command"
        )

    def test_runbook_has_docker_compose_examples(self) -> None:
        # The OmniSight HA pair runs under docker compose; the
        # runbook must use the same primitives.
        text = _read(RUNBOOK)
        assert "docker compose" in text, (
            "runbook must use `docker compose` primitives consistent "
            "with the rest of the OmniSight ops surface"
        )

    def test_runbook_acknowledges_async_replication_data_loss(self) -> None:
        # Under async replication, an unplanned promote loses the
        # un-shipped WAL tail. Naming this trade-off ensures the
        # operator checks `replay_lag` and files data-loss accounting
        # post-incident.
        text = _read(RUNBOOK).lower()
        assert "async" in text or "replay_lag" in text, (
            "runbook must acknowledge the async-replication data-loss "
            "window so the operator checks replay_lag at promote time"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
