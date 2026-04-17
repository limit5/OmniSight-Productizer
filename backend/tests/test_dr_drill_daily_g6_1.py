"""G6 #1 — Daily DR drill workflow contract tests.

TODO row 1379:
    每日排程：備份 → 另一主機執行 `restore` → 跑 `backup_selftest.py`
    + smoke 子集 → 報告

Pins the daily DR drill workflow
``.github/workflows/dr-drill-daily.yml`` that is G6 bucket's first row
and the first OmniSight workflow that runs the backup-restore round-
trip on a schedule.

The workflow is the contract target. We do NOT boot CI locally — these
are pure text + YAML shape assertions, consistent with the pattern the
G5 contract tests established (G5 #6 pinned the k8s-helm-smoke workflow
the same way).

Sibling rows not covered by this test (explicit scope fence):

    * row 1380 — RTO / RPO objective doc. Landed as G6 #2 at
      ``docs/ops/dr_rto_rpo.md``; now pinned in
      ``backend/tests/test_dr_rto_rpo_g6_2.py`` (the
      ``TestNoRtoRpoDocYet`` guard was migrated out in that commit).
    * row 1381 — DB-primary / reverse-proxy manual failover runbook.
    * row 1382 — annual DR drill operator checklist.
    * row 1383 — ``scripts/dr_drill.sh`` + ``docs/ops/dr_runbook.md``
                 bundle-closure deliverables.

The sibling-row guards below RED-flag any of the above landing in
this same commit (silent scope creep). The explicit-migration pattern
(remove the guard in the commit that lands the next row) is carried
forward from G5.

Explicit migration accepted from G5 #6:

    ``backend/tests/test_ci_k8s_helm_smoke_g5_6.py`` previously owned
    ``TestScopeDisciplineSiblingRows::test_no_g6_dr_drill_workflow``
    which RED-flagged any workflow that mentioned ``dr_drill`` or
    ``backup_selftest``. G6 #1 is precisely that workflow — landing
    this row REQUIRES removing that guard in the same commit. The
    migration is asserted below (``TestG5SiblingGuardMigration``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "dr-drill-daily.yml"
WORKFLOWS_DIR = PROJECT_ROOT / ".github" / "workflows"

BACKUP_SELFTEST = PROJECT_ROOT / "scripts" / "backup_selftest.py"
PROD_SMOKE = PROJECT_ROOT / "scripts" / "prod_smoke_test.py"

SMOKE_SUBSET_TEST = (
    PROJECT_ROOT / "backend" / "tests" / "test_prod_smoke_test_subset_cli.py"
)

TODO = PROJECT_ROOT / "TODO.md"
HANDOFF = PROJECT_ROOT / "HANDOFF.md"

G5_6_TEST = (
    PROJECT_ROOT / "backend" / "tests" / "test_ci_k8s_helm_smoke_g5_6.py"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _yaml(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(_read(path))
    assert isinstance(doc, Mapping), f"{path.name}: top-level YAML must be a mapping"
    return dict(doc)


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML parses bare ``on:`` as Python True (the boolean trigger
    # key). Accept either the literal string "on" or True.
    node = doc.get("on")
    if node is None:
        node = doc.get(True)
    assert node is not None, "workflow missing trigger block"
    assert isinstance(node, Mapping), "trigger block must be a mapping"
    return dict(node)


# ---------------------------------------------------------------------------
# TestWorkflowFileShape — file is on disk, parses, has known top-level keys.
# ---------------------------------------------------------------------------
class TestWorkflowFileShape:
    def test_workflow_file_exists(self) -> None:
        assert WORKFLOW.is_file(), (
            "G6 #1 (row 1379) ships .github/workflows/dr-drill-daily.yml "
            "— missing means the daily DR drill is not landed"
        )

    def test_workflow_path_is_known(self) -> None:
        # Pinning the path means any rename surfaces here instead of
        # silently dropping the daily schedule.
        rel = WORKFLOW.relative_to(PROJECT_ROOT)
        assert str(rel) == ".github/workflows/dr-drill-daily.yml"

    def test_workflow_yaml_parses(self) -> None:
        doc = _yaml(WORKFLOW)
        assert "name" in doc
        assert ("on" in doc) or (True in doc)
        assert "jobs" in doc

    def test_workflow_name_marks_dr_drill(self) -> None:
        doc = _yaml(WORKFLOW)
        name = doc["name"]
        assert "DR" in name or "dr " in name.lower(), (
            f"workflow name should mark this as a DR drill; got {name!r}"
        )

    def test_workflow_header_cross_references_todo_row(self) -> None:
        # Header comment must name G6 #1 + row 1379 so a future
        # reader can trace the workflow back to the tracker entry
        # without grepping.
        text = _read(WORKFLOW)
        assert "G6 #1" in text
        assert "row 1379" in text


# ---------------------------------------------------------------------------
# TestDailyCronSchedule — the load-bearing feature of row 1379.
# ---------------------------------------------------------------------------
class TestDailyCronSchedule:
    def test_schedule_trigger_present(self) -> None:
        doc = _yaml(WORKFLOW)
        on = _on(doc)
        assert "schedule" in on, (
            "row 1379 is a daily schedule — schedule trigger is mandatory"
        )

    def test_schedule_entries_use_cron(self) -> None:
        doc = _yaml(WORKFLOW)
        schedule = _on(doc)["schedule"]
        assert isinstance(schedule, list) and schedule, (
            "schedule block must be a non-empty list of cron entries"
        )
        for entry in schedule:
            assert "cron" in entry, "every schedule entry needs a `cron:` key"

    def test_cron_fires_once_daily(self) -> None:
        doc = _yaml(WORKFLOW)
        crons = [entry["cron"] for entry in _on(doc)["schedule"]]
        # A daily schedule has day-of-month "*", month "*", day-of-
        # week "*" and both minute + hour as concrete values. Anything
        # more frequent (e.g. "*/N" in the hour slot) is NOT "daily".
        assert len(crons) >= 1
        for expr in crons:
            parts = expr.split()
            assert len(parts) == 5, f"invalid cron {expr!r}: expected 5 fields"
            minute, hour, dom, month, dow = parts
            assert minute.isdigit(), (
                f"daily cron minute must be fixed; got {minute!r} in {expr!r}"
            )
            assert hour.isdigit(), (
                f"daily cron hour must be fixed; got {hour!r} in {expr!r}"
            )
            assert dom == "*" and month == "*" and dow == "*", (
                f"daily cron must leave DoM/Month/DoW as '*'; got {expr!r}"
            )

    def test_workflow_dispatch_trigger_present(self) -> None:
        doc = _yaml(WORKFLOW)
        on = _on(doc)
        # Manual trigger so operators can re-run after a fix; matches
        # the pattern of every OmniSight scheduled workflow.
        assert "workflow_dispatch" in on

    def test_push_trigger_paths_cover_drill_inputs(self) -> None:
        doc = _yaml(WORKFLOW)
        on = _on(doc)
        assert "push" in on
        paths = on["push"].get("paths", [])
        assert any("dr-drill-daily" in p for p in paths), (
            "push paths must self-include the workflow file"
        )
        assert any("backup_selftest.py" in p for p in paths), (
            "push paths must cover scripts/backup_selftest.py so a "
            "change to it triggers a re-drill immediately"
        )


# ---------------------------------------------------------------------------
# TestWorkflowJobsContract — the four jobs the drill needs.
# ---------------------------------------------------------------------------
class TestWorkflowJobsContract:
    REQUIRED_JOBS = (
        "primary-backup",
        "secondary-restore",
        "smoke-subset",
        "report",
    )

    def _jobs(self) -> dict[str, Any]:
        doc = _yaml(WORKFLOW)
        jobs = doc.get("jobs")
        assert isinstance(jobs, Mapping)
        return dict(jobs)

    def test_all_required_jobs_present(self) -> None:
        jobs = self._jobs()
        for name in self.REQUIRED_JOBS:
            assert name in jobs, (
                f"job {name!r} missing — row 1379 needs primary-backup "
                f"+ secondary-restore + smoke-subset + report"
            )

    def test_jobs_have_timeouts(self) -> None:
        for name, job in self._jobs().items():
            assert "timeout-minutes" in job, (
                f"job {name!r} missing timeout-minutes — required by "
                f"OmniSight workflow conventions"
            )

    def test_runs_on_ubuntu(self) -> None:
        for name, job in self._jobs().items():
            runs_on = job.get("runs-on")
            assert runs_on == "ubuntu-latest" or (
                isinstance(runs_on, list) and "ubuntu-latest" in runs_on
            ), f"job {name!r} must run on ubuntu-latest"

    def test_restore_depends_on_backup(self) -> None:
        # The cross-host round-trip: restore runs on a *separate*
        # runner VM but must wait for the backup artefact to upload.
        restore = self._jobs()["secondary-restore"]
        needs = restore.get("needs")
        if isinstance(needs, str):
            needs = [needs]
        assert needs is not None and "primary-backup" in needs, (
            "secondary-restore must declare needs: primary-backup so "
            "the artefact handoff is deterministic"
        )

    def test_smoke_depends_on_restore(self) -> None:
        smoke = self._jobs()["smoke-subset"]
        needs = smoke.get("needs")
        if isinstance(needs, str):
            needs = [needs]
        assert needs is not None and "secondary-restore" in needs, (
            "smoke-subset must declare needs: secondary-restore so the "
            "smoke runs only against a verified-restore DB"
        )

    def test_report_always_runs(self) -> None:
        # The report must run even when earlier jobs red — that's the
        # "→ 報告" in the row text.
        report = self._jobs()["report"]
        assert report.get("if") == "always()", (
            "report job must have `if: always()` so operators get a "
            "report when the drill reds"
        )

    def test_report_depends_on_every_earlier_job(self) -> None:
        report = self._jobs()["report"]
        needs = report.get("needs")
        if isinstance(needs, str):
            needs = [needs]
        assert needs is not None
        assert "primary-backup" in needs
        assert "secondary-restore" in needs
        assert "smoke-subset" in needs


# ---------------------------------------------------------------------------
# TestCrossHostRoundTrip — the "另一主機執行 restore" part of the row text.
# ---------------------------------------------------------------------------
class TestCrossHostRoundTrip:
    def test_backup_uploads_artifact(self) -> None:
        # The artefact IS the cross-host proxy: separate jobs get
        # separate VMs on GitHub-hosted runners, so the only way data
        # crosses is through upload-artifact / download-artifact.
        text = _read(WORKFLOW)
        assert "actions/upload-artifact@v4" in text, (
            "primary-backup must upload the backup artefact — that's "
            "the only cross-host channel"
        )

    def test_restore_downloads_artifact(self) -> None:
        text = _read(WORKFLOW)
        assert "actions/download-artifact@v4" in text, (
            "secondary-restore must download the backup artefact"
        )

    def test_backup_artifact_name_shared_env(self) -> None:
        # The backup artefact name is a single literal env var so
        # rename regressions surface here, not as a silent mismatch.
        doc = _yaml(WORKFLOW)
        env = doc.get("env", {})
        assert env.get("BACKUP_ARTIFACT"), (
            "env.BACKUP_ARTIFACT must declare the shared artefact "
            "name so upload + download stay in sync"
        )

    def test_backup_artifact_env_referenced_by_jobs(self) -> None:
        text = _read(WORKFLOW)
        # Counting the literal appears ≥ 3 (declaration + upload in
        # primary-backup + download in secondary-restore) is the
        # cheapest way to ensure no job uses a hardcoded name.
        assert text.count("${{ env.BACKUP_ARTIFACT }}") >= 3, (
            "BACKUP_ARTIFACT env var must be referenced by upload + "
            "both download steps — a hardcoded literal is a regression"
        )


# ---------------------------------------------------------------------------
# TestBackupSelftestInvocation — the row literally names this script.
# ---------------------------------------------------------------------------
class TestBackupSelftestInvocation:
    def test_backup_selftest_script_exists(self) -> None:
        assert BACKUP_SELFTEST.is_file(), (
            "scripts/backup_selftest.py is the precondition of row "
            "1379 — missing means the drill has nothing to run"
        )

    def test_workflow_invokes_backup_selftest(self) -> None:
        text = _read(WORKFLOW)
        assert "scripts/backup_selftest.py" in text, (
            "workflow must invoke scripts/backup_selftest.py by name "
            "— row 1379 text calls it out explicitly"
        )

    def test_backup_selftest_runs_against_restored_db(self) -> None:
        # The DB_PATH env var pins the canonical DB location; the
        # selftest invocation must pass it (or the default) so the
        # restored DB is what gets checked — not whatever bytes the
        # checkout happens to ship.
        text = _read(WORKFLOW)
        assert 'python scripts/backup_selftest.py "$DB_PATH"' in text, (
            "backup_selftest.py must be invoked against the DB_PATH "
            "env var (the restored DB), not the checkout default"
        )


# ---------------------------------------------------------------------------
# TestSmokeSubsetInvocation — "smoke 子集" in the row text.
# ---------------------------------------------------------------------------
class TestSmokeSubsetInvocation:
    def test_smoke_subset_test_exists(self) -> None:
        assert SMOKE_SUBSET_TEST.is_file(), (
            "smoke subset test must exist on disk before the workflow "
            "references it"
        )

    def test_workflow_runs_smoke_subset_test(self) -> None:
        text = _read(WORKFLOW)
        # The smoke subset is the `prod_smoke_test` --subset CLI
        # contract test. Pinning the literal means a rename surfaces
        # here rather than as a silent "no tests ran" green.
        assert "backend/tests/test_prod_smoke_test_subset_cli.py" in text, (
            "workflow must run the prod_smoke_test subset CLI test "
            "as the smoke subset"
        )

    def test_prod_smoke_test_script_exists(self) -> None:
        # The subset CLI test boots the prod_smoke_test script at
        # import time; keeping the script alive is a precondition.
        assert PROD_SMOKE.is_file()


# ---------------------------------------------------------------------------
# TestReportStep — the "→ 報告" part of the row text.
# ---------------------------------------------------------------------------
class TestReportStep:
    def test_report_writes_step_summary(self) -> None:
        text = _read(WORKFLOW)
        assert "GITHUB_STEP_SUMMARY" in text, (
            "report job must write to $GITHUB_STEP_SUMMARY so the "
            "drill surfaces in the Actions UI without a download"
        )

    def test_report_uploads_markdown_artefact(self) -> None:
        text = _read(WORKFLOW)
        assert "dr-drill-report.md" in text, (
            "report job must ship a dr-drill-report.md artefact — "
            "archival proof the drill ran"
        )

    def test_report_retention_at_least_7_days(self) -> None:
        # The report is the ONLY long-lived artefact; the backup
        # artefact intentionally expires fast. A 1-day retention
        # defeats the purpose of having a report.
        doc = _yaml(WORKFLOW)
        jobs = doc["jobs"]
        report_steps = jobs["report"]["steps"]
        report_upload = None
        for step in report_steps:
            if step.get("uses", "").startswith("actions/upload-artifact"):
                if step.get("with", {}).get("name") == "omnisight-dr-drill-report":
                    report_upload = step
                    break
        assert report_upload is not None, "report artefact upload step missing"
        retention = report_upload["with"].get("retention-days", 0)
        assert retention >= 7, (
            f"report retention must be >= 7 days; got {retention!r}"
        )


# ---------------------------------------------------------------------------
# TestConcurrencyAndPermissions — workflow-convention alignment.
# ---------------------------------------------------------------------------
class TestConcurrencyAndPermissions:
    def test_permissions_least_privilege(self) -> None:
        # Drill only needs checkout + artefact up/down. Broader
        # scopes (issues: write etc.) belong to a later G6 row that
        # adopts auto-ticket filing.
        doc = _yaml(WORKFLOW)
        perms = doc.get("permissions", {})
        assert perms == {"contents": "read"}, (
            f"least-privilege: only contents: read needed; got {perms!r}"
        )

    def test_concurrency_group_per_ref(self) -> None:
        doc = _yaml(WORKFLOW)
        conc = doc.get("concurrency", {})
        assert "group" in conc
        assert "${{ github.ref }}" in conc["group"]

    def test_concurrency_does_not_cancel_in_progress(self) -> None:
        # Scheduled drills must not cancel each other — every daily
        # run is a separate archival record. This is different from
        # k8s-helm-smoke where cancel-in-progress is on because PR
        # runs are ephemeral.
        doc = _yaml(WORKFLOW)
        conc = doc.get("concurrency", {})
        assert conc.get("cancel-in-progress") is False, (
            "daily drill runs must NOT cancel each other — each run "
            "is a separate archival record"
        )


# ---------------------------------------------------------------------------
# TestG5SiblingGuardMigration — the explicit-migration pattern the
# G5 series carried forward at every row boundary. G5 #6's
# `test_no_g6_dr_drill_workflow` MUST be removed in this commit.
# ---------------------------------------------------------------------------
class TestG5SiblingGuardMigration:
    def test_g5_6_dr_drill_guard_removed(self) -> None:
        # G5 #6's contract file carried a guard that RED-flagged any
        # workflow that mentioned `dr_drill` or `backup_selftest`.
        # G6 #1 lands exactly such a workflow — the guard must be
        # removed in this same commit per the explicit-migration
        # pattern carried forward from G5 #3 → #4 → #5 → #6 → G6 #1.
        text = _read(G5_6_TEST)
        assert "def test_no_g6_dr_drill_workflow" not in text, (
            "G5 #6 sibling guard `test_no_g6_dr_drill_workflow` must "
            "be REMOVED in the same commit that lands G6 #1"
        )

    def test_g5_6_test_documents_migration(self) -> None:
        # Leave a breadcrumb in G5 #6's test so future readers can
        # trace why the guard was removed.
        text = _read(G5_6_TEST)
        assert "G6 #1" in text and "row 1379" in text, (
            "G5 #6 test must document the migration — a silent "
            "removal leaves no trace of why the guard is gone"
        )


# ---------------------------------------------------------------------------
# TestTodoRowMarker — tracker hygiene: row 1379 flipped + headline literal.
# ---------------------------------------------------------------------------
class TestTodoRowMarker:
    ROW_HEADLINE = (
        "每日排程：備份 → 另一主機執行 `restore` → "
        "跑 `backup_selftest.py` + smoke 子集 → 報告"
    )

    def test_row_headline_present(self) -> None:
        text = _read(TODO)
        assert self.ROW_HEADLINE in text, (
            "row 1379 headline literal missing — TODO row may have "
            "been renamed (would silently mask the [x] flip below)"
        )

    def test_row_marked_complete(self) -> None:
        text = _read(TODO)
        assert f"- [x] {self.ROW_HEADLINE}" in text, (
            "row 1379 must flip from [ ] to [x] in the same commit "
            "that lands the workflow"
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
        assert row_idx is not None, "row 1379 line missing"
        assert row_idx > g6_header_idx, (
            "row 1379 must appear AFTER the G6 section header — file "
            "ordering regression check"
        )


# ---------------------------------------------------------------------------
# TestHandoffAlignment — HANDOFF.md must record that row 1379 landed.
# ---------------------------------------------------------------------------
class TestHandoffAlignment:
    def test_handoff_names_g6_1(self) -> None:
        text = _read(HANDOFF)
        assert "G6 #1" in text, (
            "HANDOFF.md must name G6 #1 — the daily DR drill landing "
            "is the headline event for the commit"
        )

    def test_handoff_names_row_1379(self) -> None:
        text = _read(HANDOFF)
        assert "row 1379" in text, "HANDOFF.md must cite TODO row 1379"

    def test_handoff_names_workflow_file(self) -> None:
        text = _read(HANDOFF)
        assert "dr-drill-daily.yml" in text, (
            "HANDOFF.md must point operators at the workflow file"
        )


# ---------------------------------------------------------------------------
# TestScopeDisciplineSiblingRows — guard against silent scope creep
# into the rest of G6 + G7. Explicit-migration pattern continued.
# ---------------------------------------------------------------------------
class TestScopeDisciplineSiblingRows:
    def test_no_dr_drill_shell_script(self) -> None:
        # `scripts/dr_drill.sh` is row 1383's deliverable (G6 bundle
        # closure). Landing it here would pre-commit a later row.
        assert not (PROJECT_ROOT / "scripts" / "dr_drill.sh").exists(), (
            "row 1383 (G6 #5) owns scripts/dr_drill.sh — do not land "
            "it in this commit"
        )

    def test_no_dr_runbook_doc(self) -> None:
        # `docs/ops/dr_runbook.md` is row 1383's companion doc.
        assert not (PROJECT_ROOT / "docs" / "ops" / "dr_runbook.md").exists(), (
            "row 1383 (G6 #5) owns docs/ops/dr_runbook.md — do not "
            "land it in this commit"
        )

    def test_no_g7_grafana_dashboard(self) -> None:
        # G7 ships the Grafana dashboard; must not appear with G6 #1.
        assert not (
            PROJECT_ROOT / "deploy" / "observability" / "grafana" / "ha.json"
        ).exists(), (
            "G7 (row 1387) owns deploy/observability/grafana/ha.json "
            "— do not pre-commit it with G6 #1"
        )

    def test_workflow_does_not_reference_other_g_buckets(self) -> None:
        # Body of the workflow must not name G5 / G7 — those are
        # separate buckets; any reference is a copy-paste regression.
        text = _read(WORKFLOW)
        # The workflow legitimately mentions G6 (its own bucket) and
        # row numbers 1379–1383 (its own + sibling scope fences). We
        # explicitly forbid G5 and G7.
        assert "G5 " not in text and "G5#" not in text, (
            "G6 #1 workflow should not reference G5 — separate bucket"
        )
        assert "G7" not in text, "G6 #1 workflow must not reference G7"


# ---------------------------------------------------------------------------
# NOTE: `TestNoRtoRpoDocYet` was removed in the commit that landed
# G6 #2 (TODO row 1380) — `docs/ops/dr_rto_rpo.md` now owns the
# RTO / RPO objective doc (RTO ≤ 15 min, RPO ≤ 5 min), which is the
# exact literal this guard used to forbid. The G6 #2-side contract
# pinning lives in `backend/tests/test_dr_rto_rpo_g6_2.py` —
# explicit-migration pattern, carried forward from G5 → G6 #1 → G6 #2.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestScriptReferentialIntegrity — scripts the workflow invokes must
# exist AND be executable as python entry points.
# ---------------------------------------------------------------------------
class TestScriptReferentialIntegrity:
    def test_backup_selftest_is_python_script(self) -> None:
        text = _read(BACKUP_SELFTEST)
        assert text.startswith("#!/usr/bin/env python"), (
            "backup_selftest.py must have a Python shebang — the "
            "workflow invokes it via `python ...`; keeping the shebang "
            "lets operators also run it directly"
        )

    def test_backup_selftest_has_main(self) -> None:
        text = _read(BACKUP_SELFTEST)
        assert "__main__" in text, (
            "backup_selftest.py must have an `if __name__ == '__main__'` "
            "guard — the workflow runs it as a script"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
