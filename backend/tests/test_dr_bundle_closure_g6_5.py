"""G6 #5 — DR bundle-closure contract tests.

TODO row 1383:
    交付：`scripts/dr_drill.sh`、`docs/ops/dr_runbook.md`

Pins the two bundle-closure deliverables that close the G6 HA-06
bucket:

    * ``scripts/dr_drill.sh``     — operator-runnable local-host
                                    twin of the G6 #1 daily CI
                                    workflow. Exercises the same
                                    four-stage flow (backup →
                                    restore → selftest → smoke-
                                    subset) from the operator's
                                    shell so a CI red can be
                                    reproduced without network
                                    access to github.com, and so
                                    the annual drill (G6 #4
                                    Scenario C §5) has a scripted
                                    invocation it can call.

    * ``docs/ops/dr_runbook.md``  — single-entry aggregator that
                                    points the on-call operator at
                                    every other G6 artefact in the
                                    order it needs to be read
                                    during an incident. Decision
                                    tree (§1), contract-pin index
                                    (§2), script usage (§3),
                                    artefact map (§4), scope fence
                                    (§6).

G6 #5 is the **fifth and final** deliverable of the G6 bucket.
Rows 1379 / 1380 / 1381 / 1382 / 1383 are all flipped ``[x]`` in
the commit that lands this test; G6 is closed.

Explicit migration accepted from G6 #4:

    ``backend/tests/test_dr_annual_drill_checklist_g6_4.py``
    previously owned
    ``TestScopeDisciplineSiblingRows::test_no_dr_runbook_doc`` and
    ``test_no_dr_drill_shell_script`` which RED-flagged the two
    bundle-closure paths ``docs/ops/dr_runbook.md`` and
    ``scripts/dr_drill.sh``. G6 #5 lands both of those paths —
    both guards MUST be removed in the same commit per the
    explicit-migration pattern carried forward from G5 #3 → #4 →
    #5 → #6 → G6 #1 → #2 → #3 → #4 → this row (9th continuation).
    The migration is asserted below
    (``TestG6_4SiblingGuardMigration``).

Sibling rows NOT covered by this test (explicit scope fence):

    * G7 (rows 1385–1388) — Prometheus + Grafana dashboards +
      alert rules. Independent bucket; must not be pre-committed
      with this row.
"""
from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

RUNBOOK = PROJECT_ROOT / "docs" / "ops" / "dr_runbook.md"
DRILL_SCRIPT = PROJECT_ROOT / "scripts" / "dr_drill.sh"

RTO_RPO_DOC = PROJECT_ROOT / "docs" / "ops" / "dr_rto_rpo.md"
MANUAL_FAILOVER_DOC = (
    PROJECT_ROOT / "docs" / "ops" / "dr_manual_failover.md"
)
ANNUAL_CHECKLIST_DOC = (
    PROJECT_ROOT / "docs" / "ops" / "dr_annual_drill_checklist.md"
)
DR_DRILL_WORKFLOW = (
    PROJECT_ROOT / ".github" / "workflows" / "dr-drill-daily.yml"
)
BACKUP_SELFTEST_SCRIPT = (
    PROJECT_ROOT / "scripts" / "backup_selftest.py"
)
SMOKE_SUBSET_TEST = (
    PROJECT_ROOT / "backend" / "tests" / "test_prod_smoke_test_subset_cli.py"
)

TODO = PROJECT_ROOT / "TODO.md"
HANDOFF = PROJECT_ROOT / "HANDOFF.md"

G6_4_TEST = (
    PROJECT_ROOT
    / "backend"
    / "tests"
    / "test_dr_annual_drill_checklist_g6_4.py"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# TestRunbookFileShape — runbook is on disk, single canonical name, H1.
# ---------------------------------------------------------------------------
class TestRunbookFileShape:
    def test_runbook_exists(self) -> None:
        assert RUNBOOK.is_file(), (
            "G6 #5 (row 1383) ships docs/ops/dr_runbook.md — missing "
            "means the bundle aggregator has no source of truth"
        )

    def test_runbook_path_is_canonical(self) -> None:
        # Pinning the path means a rename surfaces here instead of
        # silently splitting the aggregator across two locations.
        rel = RUNBOOK.relative_to(PROJECT_ROOT)
        assert str(rel) == "docs/ops/dr_runbook.md"

    def test_runbook_in_docs_ops(self) -> None:
        assert RUNBOOK.parent == PROJECT_ROOT / "docs" / "ops", (
            "runbook must live under docs/ops/"
        )

    def test_runbook_has_title(self) -> None:
        # First non-empty line must be a markdown H1 that names the
        # doc. Aggregator's title must include 'runbook' + 'disaster'
        # (or 'DR') so the file is searchable.
        text = _read(RUNBOOK)
        for line in text.splitlines():
            if line.strip():
                assert line.startswith("# "), (
                    "runbook must open with a markdown H1 title"
                )
                lower = line.lower()
                assert "runbook" in lower or "disaster" in lower or "dr" in lower
                return
        pytest.fail("runbook is empty")

    def test_only_one_dr_runbook_doc(self) -> None:
        # A second file at one of the alternative paths would split
        # the truth source and defeat the aggregator role.
        alternatives = (
            PROJECT_ROOT / "docs" / "ops" / "dr-runbook.md",
            PROJECT_ROOT / "docs" / "ops" / "disaster_recovery.md",
            PROJECT_ROOT / "docs" / "ops" / "disaster_recovery_runbook.md",
            PROJECT_ROOT / "docs" / "DR_RUNBOOK.md",
            PROJECT_ROOT / "DR_RUNBOOK.md",
        )
        for alt in alternatives:
            assert not alt.exists(), (
                f"only one DR aggregator allowed; found extra at "
                f"{alt.relative_to(PROJECT_ROOT)}"
            )


# ---------------------------------------------------------------------------
# TestRunbookAggregatorShape — the runbook is an AGGREGATOR, not a
# duplicator. It must link to every sibling artefact and contain a
# decision tree.
# ---------------------------------------------------------------------------
class TestRunbookAggregatorShape:
    def test_runbook_has_decision_tree(self) -> None:
        text = _read(RUNBOOK).lower()
        # Aggregator without a decision tree is just a reading list.
        # A "Page fired" opener + either a fenced block with arrows
        # or an explicit "decision tree" phrase is the minimum shape.
        assert "decision tree" in text, (
            "runbook must contain a decision-tree section — the "
            "operator paged at 3am should not have to read the doc "
            "end-to-end to pick the right failure mode"
        )
        assert "page fired" in text or "paged" in text, (
            "decision tree must open with a page-fire framing so the "
            "operator immediately sees 'this is the incident path'"
        )

    def test_runbook_references_g6_1_workflow(self) -> None:
        text = _read(RUNBOOK)
        assert "dr-drill-daily.yml" in text, (
            "runbook must reference .github/workflows/dr-drill-daily.yml "
            "— the daily CI drill is half the G6 #1 → G6 #5 relationship"
        )

    def test_runbook_references_g6_2_rto_rpo_doc(self) -> None:
        text = _read(RUNBOOK)
        assert "dr_rto_rpo.md" in text, (
            "runbook must reference docs/ops/dr_rto_rpo.md — the "
            "RTO/RPO budget is the contract the aggregator indexes"
        )

    def test_runbook_references_g6_3_manual_failover_doc(self) -> None:
        text = _read(RUNBOOK)
        assert "dr_manual_failover.md" in text, (
            "runbook must reference docs/ops/dr_manual_failover.md "
            "— the decision tree's failure-mode branches terminate "
            "in that doc"
        )

    def test_runbook_references_g6_4_annual_checklist(self) -> None:
        text = _read(RUNBOOK)
        assert "dr_annual_drill_checklist.md" in text, (
            "runbook must reference docs/ops/dr_annual_drill_checklist.md "
            "— the annual cadence is part of the bundle promise"
        )

    def test_runbook_references_dr_drill_script(self) -> None:
        text = _read(RUNBOOK)
        assert "scripts/dr_drill.sh" in text, (
            "runbook must reference scripts/dr_drill.sh — the two "
            "artefacts ship as a pair under G6 #5"
        )

    def test_runbook_references_backup_selftest(self) -> None:
        text = _read(RUNBOOK)
        assert "backup_selftest.py" in text, (
            "runbook must reference scripts/backup_selftest.py — the "
            "selftest is stage 3 of dr_drill.sh and the data-plane "
            "branch of the decision tree"
        )

    def test_runbook_names_g7_next_bucket(self) -> None:
        text = _read(RUNBOOK)
        assert "G7" in text, (
            "runbook must name G7 as the next bucket — bundle closure "
            "is explicit about what comes after"
        )

    def test_runbook_names_every_g6_row(self) -> None:
        text = _read(RUNBOOK)
        # The contract-pin index table in §2 must enumerate all five
        # G6 rows by their numeric identifier.
        for pin in ("G6 #1", "G6 #2", "G6 #3", "G6 #4", "G6 #5"):
            assert pin in text, (
                f"runbook must name {pin} — the pin index must be "
                f"complete for the bundle-closure to be honest"
            )


# ---------------------------------------------------------------------------
# TestRunbookCitesNotRedefines — RTO/RPO numbers and load-bearing commands
# must be CITED, not redefined. A redefinition splits the truth source.
# ---------------------------------------------------------------------------
class TestRunbookCitesNotRedefines:
    def test_runbook_cites_15_min_rto(self) -> None:
        text = _read(RUNBOOK)
        assert re.search(r"15[\s\-]*min", text, re.IGNORECASE), (
            "runbook must cite the 15-min RTO so readers see the "
            "bucket promise in the aggregator"
        )

    def test_runbook_cites_5_min_rpo(self) -> None:
        text = _read(RUNBOOK)
        assert re.search(r"5[\s\-]*min", text, re.IGNORECASE), (
            "runbook must cite the 5-min RPO so readers see the "
            "bucket promise in the aggregator"
        )

    def test_runbook_does_not_define_rto_rpo_formally(self) -> None:
        # G6 #2 owns the formal "Recovery Time Objective is the..."
        # definition. The aggregator MUST NOT redefine it, or the
        # numbers will drift between two docs the first time someone
        # tightens the budget.
        text = _read(RUNBOOK)
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
            "runbook must NOT formally define RTO / RPO — that's "
            "G6 #2's docs/ops/dr_rto_rpo.md scope; cite the numbers, "
            "do not redefine them"
        )

    def test_runbook_does_not_inline_pg_ctl_promote_step(self) -> None:
        # G6 #3 owns the step-by-step. The aggregator may NAME the
        # command ("...load-bearing `pg_ctl promote`...") but MUST
        # NOT inline a fenced bash block containing the command —
        # that would duplicate the runbook surface.
        text = _read(RUNBOOK)
        # Allow the name to appear (as a reference), but no code
        # block that actually runs pg_ctl promote.
        pattern = re.compile(
            r"```bash[^`]*pg_ctl\s+promote",
            re.IGNORECASE | re.DOTALL,
        )
        assert pattern.search(text) is None, (
            "runbook must NOT inline a pg_ctl promote bash block — "
            "that belongs in docs/ops/dr_manual_failover.md §2.2 Step 3"
        )

    def test_runbook_does_not_inline_caddy_reload_step(self) -> None:
        # Symmetric: caddy reload / caddy validate commands live in
        # the manual failover runbook, not the aggregator.
        text = _read(RUNBOOK)
        pattern = re.compile(
            r"```bash[^`]*caddy\s+(reload|validate|reverse-proxy)",
            re.IGNORECASE | re.DOTALL,
        )
        assert pattern.search(text) is None, (
            "runbook must NOT inline a caddy reload/validate/"
            "reverse-proxy bash block — that belongs in "
            "docs/ops/dr_manual_failover.md §3"
        )


# ---------------------------------------------------------------------------
# TestDrillScriptFileShape — scripts/dr_drill.sh is on disk, has a bash
# shebang, is executable, syntax-checks clean.
# ---------------------------------------------------------------------------
class TestDrillScriptFileShape:
    def test_drill_script_exists(self) -> None:
        assert DRILL_SCRIPT.is_file(), (
            "G6 #5 (row 1383) ships scripts/dr_drill.sh — missing "
            "means operators cannot reproduce the daily drill locally"
        )

    def test_drill_script_path_is_canonical(self) -> None:
        rel = DRILL_SCRIPT.relative_to(PROJECT_ROOT)
        assert str(rel) == "scripts/dr_drill.sh"

    def test_drill_script_has_bash_shebang(self) -> None:
        first_line = _read(DRILL_SCRIPT).splitlines()[0]
        assert first_line == "#!/usr/bin/env bash", (
            "scripts/dr_drill.sh must start with #!/usr/bin/env bash "
            "so it runs on Linux + macOS operators without a shell "
            "hardcode"
        )

    def test_drill_script_is_executable(self) -> None:
        mode = DRILL_SCRIPT.stat().st_mode
        assert mode & stat.S_IXUSR, (
            "scripts/dr_drill.sh must be executable (chmod +x) so "
            "operators can invoke it directly, not via `bash`"
        )

    def test_drill_script_bash_syntax_clean(self) -> None:
        # `bash -n` parses without executing — catches syntax errors
        # that would silent-fail until the drill is invoked.
        result = subprocess.run(
            ["bash", "-n", str(DRILL_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"scripts/dr_drill.sh has bash syntax errors:\n"
            f"{result.stderr}"
        )

    def test_drill_script_uses_strict_mode(self) -> None:
        # `set -euo pipefail` is the standard strict-mode posture for
        # every OmniSight shell script (see scripts/deploy.sh +
        # scripts/bluegreen_switch.sh). A drill that silently
        # continues after a stage failure is worse than no drill.
        text = _read(DRILL_SCRIPT)
        assert re.search(r"^\s*set\s+-[eu]+o?\s*pipefail", text, re.MULTILINE) or (
            "set -euo pipefail" in text or "set -eu" in text
        ), (
            "scripts/dr_drill.sh must use `set -euo pipefail` (or at "
            "minimum `set -eu`) — strict mode is required for drill "
            "scripts"
        )


# ---------------------------------------------------------------------------
# TestDrillScriptContract — the script must expose the documented flags,
# exit codes, and reference the sibling artefacts.
# ---------------------------------------------------------------------------
class TestDrillScriptContract:
    def test_help_flag_is_supported(self) -> None:
        # --help must not fail. Running the actual script is safe
        # because --help short-circuits before any stage.
        result = subprocess.run(
            [str(DRILL_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"scripts/dr_drill.sh --help must exit 0; got "
            f"{result.returncode}\nstderr:\n{result.stderr}"
        )
        assert "Usage" in result.stdout or "usage" in result.stdout.lower(), (
            "scripts/dr_drill.sh --help must print a Usage: block"
        )

    def test_help_names_all_flags(self) -> None:
        result = subprocess.run(
            [str(DRILL_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        help_text = result.stdout
        for flag in ("--db", "--out", "--no-smoke", "--seed", "--help"):
            assert flag in help_text, (
                f"scripts/dr_drill.sh --help must document {flag} — "
                f"an undocumented flag is the fastest way for an "
                f"operator to trip over a stage they did not intend"
            )

    def test_help_documents_exit_codes(self) -> None:
        result = subprocess.run(
            [str(DRILL_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        help_text = result.stdout.lower()
        assert "exit" in help_text, (
            "scripts/dr_drill.sh --help must document exit codes — "
            "operators need to know what '3' means without reading "
            "the script"
        )

    def test_script_references_backup_selftest(self) -> None:
        text = _read(DRILL_SCRIPT)
        # The drill's stage 3 invokes the selftest — naming it in
        # the script itself means the dependency is explicit.
        assert "backup_selftest.py" in text, (
            "scripts/dr_drill.sh must invoke scripts/backup_selftest.py "
            "— stage 3 of the four-stage round-trip is the selftest"
        )

    def test_script_references_smoke_subset_test(self) -> None:
        text = _read(DRILL_SCRIPT)
        assert "test_prod_smoke_test_subset_cli" in text, (
            "scripts/dr_drill.sh must invoke the smoke-subset pytest "
            "file that G6 #1's CI workflow runs"
        )

    def test_script_points_at_runbook(self) -> None:
        # The script's module-level docstring / comments should name
        # the runbook so a `head` / `less` on the script leads the
        # reader to the aggregator.
        text = _read(DRILL_SCRIPT)
        assert "dr_runbook.md" in text, (
            "scripts/dr_drill.sh must name docs/ops/dr_runbook.md in "
            "its header — the script + runbook ship as a pair"
        )

    def test_script_uses_same_backup_api_as_selftest(self) -> None:
        # The whole point of the local drill is that its bytes are
        # identical to what the selftest expects. Using the same
        # Python `sqlite3.Connection.backup()` API is the contract.
        text = _read(DRILL_SCRIPT)
        assert ".backup(" in text or "Connection.backup" in text, (
            "scripts/dr_drill.sh must call sqlite3.Connection.backup() "
            "— same API the selftest uses, one source of truth for "
            "'what does a backup look like'"
        )


# ---------------------------------------------------------------------------
# TestDrillScriptEndToEnd — actually run the drill against a seeded
# synthetic DB. This is the load-bearing contract: if the script
# regresses, the annual drill and the developer loop both break.
# ---------------------------------------------------------------------------
class TestDrillScriptEndToEnd:
    def test_drill_greens_on_seeded_db(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        out = tmp_path / "artefacts"
        result = subprocess.run(
            [
                str(DRILL_SCRIPT),
                "--db",
                str(db),
                "--out",
                str(out),
                "--seed",
                "--no-smoke",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"scripts/dr_drill.sh --seed --no-smoke against a fresh "
            f"tmp dir must exit 0 (this is the happy-path contract).\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        assert (out / "backup.db").exists(), (
            "stage 1 must produce <out>/backup.db"
        )
        assert (out / "restored.db").exists(), (
            "stage 2 must produce <out>/restored.db"
        )
        assert (out / "dr-drill-report.md").exists(), (
            "report stage must always write <out>/dr-drill-report.md "
            "— the durable artefact the annual drill attaches to its "
            "ticket"
        )

    def test_drill_exits_1_on_missing_source_db(self, tmp_path: Path) -> None:
        # No --seed, no source DB — must fail with usage-class exit 1.
        missing = tmp_path / "does-not-exist.db"
        out = tmp_path / "artefacts"
        result = subprocess.run(
            [
                str(DRILL_SCRIPT),
                "--db",
                str(missing),
                "--out",
                str(out),
                "--no-smoke",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1, (
            f"missing source DB must exit 1 (usage); got "
            f"{result.returncode}"
        )

    def test_drill_writes_report_even_on_failure(self, tmp_path: Path) -> None:
        # Report must be written on the failure path too — the annual
        # drill and incident ticket both rely on the report artefact
        # being present.
        missing = tmp_path / "does-not-exist.db"
        out = tmp_path / "artefacts"
        subprocess.run(
            [
                str(DRILL_SCRIPT),
                "--db",
                str(missing),
                "--out",
                str(out),
                "--no-smoke",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (out / "dr-drill-report.md").exists(), (
            "report must be written even when a stage fails — "
            "operators attach the report to the incident ticket"
        )
        report = (out / "dr-drill-report.md").read_text()
        assert "failed" in report.lower(), (
            "failure report must mark the failed stage status"
        )


# ---------------------------------------------------------------------------
# TestCrossReferences — every path the runbook / script names must
# actually exist on disk. Dangling references would be silent rot.
# ---------------------------------------------------------------------------
class TestCrossReferences:
    def test_all_referenced_paths_exist(self) -> None:
        for path in (
            RTO_RPO_DOC,
            MANUAL_FAILOVER_DOC,
            ANNUAL_CHECKLIST_DOC,
            DR_DRILL_WORKFLOW,
            BACKUP_SELFTEST_SCRIPT,
            SMOKE_SUBSET_TEST,
        ):
            assert path.exists(), (
                f"G6 #5 references {path.relative_to(PROJECT_ROOT)} "
                f"but it does not exist on disk"
            )


# ---------------------------------------------------------------------------
# TestTrackerAlignment — TODO row 1383 flipped, HANDOFF updated.
# ---------------------------------------------------------------------------
class TestTrackerAlignment:
    ROW_HEADLINE = "交付：`scripts/dr_drill.sh`、`docs/ops/dr_runbook.md`"

    def test_todo_row_headline_present(self) -> None:
        text = _read(TODO)
        assert self.ROW_HEADLINE in text, (
            "row 1383 headline literal missing from TODO.md — "
            "rename would silently mask the [x] flip below"
        )

    def test_todo_row_marked_complete(self) -> None:
        text = _read(TODO)
        assert f"- [x] {self.ROW_HEADLINE}" in text, (
            "row 1383 must flip from [ ] to [x] in the same commit "
            "that lands scripts/dr_drill.sh + docs/ops/dr_runbook.md"
        )

    def test_row_under_g6_section(self) -> None:
        text = _read(TODO)
        lines = text.splitlines()
        g6_header_idx = None
        row_idx = None
        for i, line in enumerate(lines):
            if "### G6." in line and g6_header_idx is None:
                g6_header_idx = i
            if self.ROW_HEADLINE in line and row_idx is None:
                row_idx = i
        assert g6_header_idx is not None, "G6 section header missing"
        assert row_idx is not None, "row 1383 line missing"
        assert row_idx > g6_header_idx, (
            "row 1383 must appear AFTER the G6 section header"
        )

    def test_handoff_names_g6_5(self) -> None:
        text = _read(HANDOFF)
        assert "G6 #5" in text, (
            "HANDOFF.md must name G6 #5 — the bundle-closure landing "
            "is the headline event for this commit"
        )

    def test_handoff_names_row_1383(self) -> None:
        text = _read(HANDOFF)
        assert "row 1383" in text, "HANDOFF.md must cite TODO row 1383"

    def test_handoff_names_dr_drill_script(self) -> None:
        text = _read(HANDOFF)
        assert "scripts/dr_drill.sh" in text, (
            "HANDOFF.md must point operators at scripts/dr_drill.sh"
        )

    def test_handoff_names_dr_runbook(self) -> None:
        text = _read(HANDOFF)
        assert "dr_runbook.md" in text, (
            "HANDOFF.md must point operators at docs/ops/dr_runbook.md"
        )


# ---------------------------------------------------------------------------
# TestG6_4SiblingGuardMigration — the G6 #4 test's guards against
# docs/ops/dr_runbook.md + scripts/dr_drill.sh MUST be removed in this
# commit per the explicit-migration pattern.
# ---------------------------------------------------------------------------
class TestG6_4SiblingGuardMigration:
    def test_g6_4_no_dr_runbook_guard_removed(self) -> None:
        # G6 #4's contract carried a guard RED-flagging
        # docs/ops/dr_runbook.md. G6 #5 lands that doc — the guard
        # MUST be removed in this same commit per the explicit-
        # migration pattern carried forward from G5 → G6 #1 → #2 →
        # #3 → #4 → G6 #5.
        text = _read(G6_4_TEST)
        assert "def test_no_dr_runbook_doc" not in text, (
            "G6 #4 sibling guard `test_no_dr_runbook_doc` must be "
            "REMOVED in the same commit that lands G6 #5"
        )

    def test_g6_4_no_dr_drill_script_guard_removed(self) -> None:
        # Symmetric: G6 #4's guard against scripts/dr_drill.sh must
        # also be gone.
        text = _read(G6_4_TEST)
        assert "def test_no_dr_drill_shell_script" not in text, (
            "G6 #4 sibling guard `test_no_dr_drill_shell_script` "
            "must be REMOVED in the same commit that lands G6 #5"
        )

    def test_g6_4_documents_migration(self) -> None:
        # Leave a breadcrumb in G6 #4's test so future readers can
        # trace why the two guards were removed.
        text = _read(G6_4_TEST)
        assert (
            "G6 #5" in text
            and "row 1383" in text
            and "dr_runbook.md" in text
            and "dr_drill.sh" in text
        ), (
            "G6 #4 test must document the migration — a silent "
            "removal leaves no trace of why the guards are gone"
        )


# ---------------------------------------------------------------------------
# TestScopeDisciplineSiblingRows — guard against silent scope creep
# into G7. G6 is closed with this row; G7 is independent and MUST NOT
# be pre-committed here.
# ---------------------------------------------------------------------------
class TestScopeDisciplineSiblingRows:
    # NOTE: `test_no_g7_grafana_dashboard` was removed in the commit
    # that landed G7 #2 (TODO row 1387) —
    # `deploy/observability/grafana/ha.json` now owns the G7 HA-07
    # dashboard surface. The G7 #2-side contract pinning lives in
    # `backend/tests/test_ha_grafana_dashboard_g7_2.py`. Explicit-
    # migration pattern, carried forward from
    # G5 #3 → #4 → #5 → #6 → G6 #1 → #2 → #3 → #4 → G6 #5 → G7 #2
    # (10th continuation).

    # NOTE: `test_no_g7_alert_rules_yaml` was removed in the commit
    # that landed G7 #3 (TODO row 1388) —
    # `deploy/observability/prometheus/alerts.yml` now owns the
    # HA-07 Prometheus alert surface. The G7 #3-side contract
    # pinning lives in `backend/tests/test_ha_alert_rules_g7_3.py`.
    # Explicit-migration pattern, 11th continuation:
    # G5 #3 → #4 → #5 → #6 → G6 #1 → #2 → #3 → #4 → G6 #5 → G7 #2
    # → G7 #3.

    def test_runbook_does_not_ship_alert_thresholds(self) -> None:
        # G7 owns alert thresholds. The runbook may mention G7 by
        # name but MUST NOT inline Prometheus PromQL, alert durations
        # (`for: 2m`), or specific metric names — those would
        # pre-commit G7 content.
        text = _read(RUNBOOK)
        bad_patterns = (
            r"^\s*expr\s*:",   # Prometheus alert rule key
            r"^\s*for\s*:\s*\d+m",
            r"histogram_quantile\s*\(",
        )
        for pat in bad_patterns:
            assert re.search(pat, text, re.MULTILINE) is None, (
                f"runbook must NOT inline PromQL / alert-rule keys "
                f"(pattern: {pat}) — that's G7's scope"
            )

    def test_runbook_does_not_duplicate_runbook_commands(self) -> None:
        # Bundle aggregator must have STRICTLY FEWER bash code fences
        # than the manual failover runbook (G6 #3). Equality would
        # mean the aggregator is starting to own step-by-step content;
        # strictly less is the cheapest quantitative assertion that
        # the aggregator stays an aggregator.
        aggregator_text = _read(RUNBOOK)
        manual_text = _read(MANUAL_FAILOVER_DOC)
        aggregator_bash = len(
            re.findall(r"^```bash", aggregator_text, re.MULTILINE)
        )
        manual_bash = len(
            re.findall(r"^```bash", manual_text, re.MULTILINE)
        )
        assert aggregator_bash < manual_bash, (
            f"aggregator bash fence count ({aggregator_bash}) must "
            f"be strictly less than manual failover's ({manual_bash}) "
            f"— aggregator owning step-by-step is the most common "
            f"scope-creep shape for bundle-closure docs"
        )

    def test_script_does_not_do_failover(self) -> None:
        # scripts/dr_drill.sh is a DRILL wrapper, NOT a failover
        # script. It must not invoke pg_ctl promote, caddy reload, or
        # anything that would mutate production state.
        text = _read(DRILL_SCRIPT)
        for bad in ("pg_ctl promote", "caddy reload", "caddy validate"):
            # Allow names in comments/docs (they appear in the
            # exit-code interpretation), but NOT as a command being
            # executed. Cheapest shape check: the line starting with
            # one of these + no comment prefix.
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                assert not stripped.startswith(bad), (
                    f"scripts/dr_drill.sh must not execute `{bad}` — "
                    f"the drill is a backup round-trip, not a "
                    f"failover; that belongs in an operator-typed "
                    f"command from docs/ops/dr_manual_failover.md"
                )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
