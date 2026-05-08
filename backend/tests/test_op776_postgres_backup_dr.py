"""OP-776 — Postgres backup + disaster-recovery contract tests.

Pins the scheduled Postgres backup workflow and the operator runbook
for Sprint D D15:

    * hourly backup / WAL proof cron
    * daily pg_basebackup snapshot cron
    * offsite copy verified by SHA
    * weekly restore-test to ephemeral Postgres plus smoke
    * RTO/RPO targets of 1h
    * runbook at docs/operations/disaster-recovery.md

The tests are structural. CI does not contact production Postgres or
S3; the workflow exercises an ephemeral Postgres service instead.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "postgres-backup-dr.yml"
RUNBOOK = PROJECT_ROOT / "docs" / "operations" / "disaster-recovery.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _yaml(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(_read(path))
    assert isinstance(doc, Mapping), f"{path.name}: top-level YAML must be a mapping"
    return dict(doc)


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    node = doc.get("on")
    if node is None:
        node = doc.get(True)
    assert isinstance(node, Mapping), "workflow trigger block must be a mapping"
    return dict(node)


def _jobs(doc: dict[str, Any]) -> dict[str, Any]:
    jobs = doc.get("jobs")
    assert isinstance(jobs, Mapping), "workflow jobs block must be a mapping"
    return dict(jobs)


class TestWorkflowShape:
    def test_workflow_exists_at_canonical_path(self) -> None:
        assert WORKFLOW.is_file()
        assert str(WORKFLOW.relative_to(PROJECT_ROOT)) == (
            ".github/workflows/postgres-backup-dr.yml"
        )

    def test_workflow_yaml_parses(self) -> None:
        doc = _yaml(WORKFLOW)
        assert "Postgres backup" in doc["name"]
        assert "jobs" in doc

    def test_workflow_has_manual_dispatch(self) -> None:
        assert "workflow_dispatch" in _on(_yaml(WORKFLOW))

    def test_job_timeout_pins_rto_one_hour(self) -> None:
        job = _jobs(_yaml(WORKFLOW))["postgres-backup-and-restore"]
        assert job["timeout-minutes"] == 60
        text = _read(WORKFLOW)
        assert 'RTO_TARGET_MINUTES: "60"' in text
        assert 'RPO_TARGET_MINUTES: "60"' in text


class TestBackupCronSchedule:
    def test_hourly_daily_and_weekly_crons_present(self) -> None:
        schedule = _on(_yaml(WORKFLOW))["schedule"]
        crons = {entry["cron"] for entry in schedule}
        assert "7 * * * *" in crons
        assert "17 2 * * *" in crons
        assert "37 3 * * 0" in crons

    def test_schedule_classifier_routes_hourly_daily_weekly_modes(self) -> None:
        text = _read(WORKFLOW)
        assert '"7 * * * *") mode="hourly-backup"' in text
        assert '"17 2 * * *") mode="daily-snapshot"' in text
        assert '"37 3 * * 0") mode="weekly-restore-test"' in text

    def test_push_paths_cover_workflow_runbook_and_test(self) -> None:
        paths = _on(_yaml(WORKFLOW))["push"]["paths"]
        assert ".github/workflows/postgres-backup-dr.yml" in paths
        assert "docs/operations/disaster-recovery.md" in paths
        assert "backend/tests/test_op776_postgres_backup_dr.py" in paths


class TestBackupPrimitives:
    def test_pg_basebackup_used_for_daily_snapshot(self) -> None:
        text = _read(WORKFLOW)
        assert "pg_basebackup" in text
        assert "--wal-method=stream" in text
        assert "--format=tar" in text
        assert "--gzip" in text

    def test_hourly_wal_archive_proof_switches_wal(self) -> None:
        text = _read(WORKFLOW)
        assert "pg_switch_wal()" in text
        assert "pg_current_wal_lsn()" in text
        assert "wal-hourly/SHA256SUMS" in text

    def test_offsite_copy_is_sha_verified(self) -> None:
        text = _read(WORKFLOW)
        assert "OFFSITE_ROOT" in text
        assert "cp \"$BACKUP_ROOT/basebackup/SHA256SUMS\"" in text
        assert "sha256sum --check SHA256SUMS" in text


class TestWeeklyRestoreTest:
    def test_restore_runs_ephemeral_postgres(self) -> None:
        text = _read(WORKFLOW)
        assert "docker run -d --rm" in text
        assert "--name op776-restore-postgres" in text
        assert "postgres:16-alpine" in text

    def test_restore_smoke_checks_ready_and_data(self) -> None:
        text = _read(WORKFLOW)
        assert "pg_isready -h 127.0.0.1 -p \"$RESTORE_PORT\"" in text
        assert "SELECT count(*) FROM op776_smoke" in text
        assert "test \"$restored_count\" = \"1\"" in text

    def test_restore_enforces_one_hour_rto(self) -> None:
        text = _read(WORKFLOW)
        assert "restore_elapsed_seconds" in text
        assert "test \"$elapsed\" -le \"$(( RTO_TARGET_MINUTES * 60 ))\"" in text


class TestRunbook:
    def test_runbook_exists_at_required_path(self) -> None:
        assert RUNBOOK.is_file()
        assert str(RUNBOOK.relative_to(PROJECT_ROOT)) == (
            "docs/operations/disaster-recovery.md"
        )

    def test_runbook_documents_rto_rpo_targets(self) -> None:
        text = _read(RUNBOOK)
        assert re.search(r"RTO\s*≤\s*1h", text)
        assert re.search(r"RPO\s*≤\s*1h", text)
        assert "Recovery Time Objective" in text
        assert "Recovery Point Objective" in text

    def test_runbook_documents_retention_policy(self) -> None:
        text = _read(RUNBOOK)
        assert "7 days" in text
        assert "30 days" in text
        assert "12 months" in text

    def test_runbook_documents_full_restore_steps_and_smoke(self) -> None:
        text = _read(RUNBOOK)
        assert "Full Restore During Production DB Loss" in text
        assert "Apply available WAL" in text
        assert "/readyz" in text
        assert "SELECT 1;" in text

    def test_runbook_documents_encryption_at_rest_and_sha(self) -> None:
        text = _read(RUNBOOK)
        assert "encryption at rest" in text.lower()
        assert "--sse AES256" in text
        assert "sha256sum --check SHA256SUMS" in text
