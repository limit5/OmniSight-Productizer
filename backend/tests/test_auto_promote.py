"""OP-877 D5 — daily develop -> main auto-promote contract.

Five cases (per the ticket test plan):

1. ``test_acceptance_green_fast_forwards_main`` — milestone_ready in
   event log + clean FF on develop -> push succeeds, audit row =
   ``promoted``.
2. ``test_acceptance_not_green_noops`` — milestone_blocked in event log,
   exit 0, no push, audit row = ``milestone_not_accepted``.
3. ``test_ff_impossible_when_diverged`` — main has commits absent from
   develop, exit != 0, audit row = ``ff_not_possible``.
4. ``test_push_rejected_records_audit_and_exits_nonzero`` — push fails
   (broken remote), audit row = ``push_rejected``, exit != 0.
5. ``test_alembic_0207_creates_release_audit_table`` — running the
   ``0207_release_audit`` migration on a fresh SQLite DB creates the
   table with the documented columns + outcome CHECK constraint.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "auto_promote_develop_to_main.sh"
SERVICE = REPO_ROOT / "deploy" / "systemd" / "auto-promote-develop.service"
TIMER = REPO_ROOT / "deploy" / "systemd" / "auto-promote-develop.timer"
MIGRATION_0207 = (
    REPO_ROOT / "backend" / "alembic" / "versions" / "0207_release_audit.py"
)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit_file(repo: Path, name: str, body: str) -> str:
    path = repo / name
    path.write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"commit {name}")
    return _git(repo, "rev-parse", "HEAD")


def _init_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.name", "OP-877 Test")
    _git(repo, "config", "user.email", "op-877@example.test")
    _git(repo, "remote", "add", "gerrit", str(remote))
    _commit_file(repo, "base.txt", "base\n")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "gerrit", "main:main")
    _git(repo, "checkout", "-b", "develop")
    _git(repo, "push", "gerrit", "develop:develop")
    return repo, remote


def _init_release_audit_db(tmp_path: Path) -> Path:
    db_file = tmp_path / "release_audit.sqlite"
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE release_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                outcome TEXT NOT NULL CHECK (outcome IN (
                    'promoted','noop','milestone_not_accepted',
                    'ff_not_possible','push_rejected'
                )),
                fix_version TEXT,
                develop_sha TEXT NOT NULL DEFAULT '',
                main_sha TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
    return db_file


def _read_audit_rows(db_file: Path) -> list[tuple]:
    with sqlite3.connect(db_file) as conn:
        return list(
            conn.execute(
                "SELECT outcome, fix_version, develop_sha, main_sha, detail "
                "FROM release_audit ORDER BY id"
            )
        )


def _write_event_log(tmp_path: Path, *records: str) -> Path:
    path = tmp_path / "milestone.log"
    path.write_text("\n".join(records) + "\n", encoding="utf-8")
    return path


def _run_script(
    *,
    repo: Path,
    event_log: Path,
    audit_db: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        OP877_REPO=str(repo),
        OP877_REMOTE="gerrit",
        OP877_SOURCE_BRANCH="develop",
        OP877_TARGET_BRANCH="main",
        OP877_EVENT_LOG=str(event_log),
        OP877_AUDIT_DB=str(audit_db),
        OP877_AUDIT_DB_KIND="sqlite",
        OP877_NOW="2026-05-11T07:00:00Z",
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ─────────────────────────────────────────────────────────────────────
# AC #2 + #3: acceptance green => fast-forward + push (success path)
# ─────────────────────────────────────────────────────────────────────


def test_acceptance_green_fast_forwards_main(tmp_path: Path) -> None:
    repo, remote = _init_repo_with_remote(tmp_path)
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    audit_db = _init_release_audit_db(tmp_path)
    event_log = _write_event_log(
        tmp_path,
        '{"event": "milestone_ready", "fixVersion": "v9.99.0"}',
    )

    result = _run_script(repo=repo, event_log=event_log, audit_db=audit_db)

    assert result.returncode == 0, result.stderr + result.stdout
    assert _git(remote, "rev-parse", "main") == develop_tip
    rows = _read_audit_rows(audit_db)
    assert len(rows) == 1
    outcome, fix_version, audit_dev_sha, _audit_main_sha, _detail = rows[0]
    assert outcome == "promoted"
    assert fix_version == "v9.99.0"
    assert audit_dev_sha == develop_tip


# ─────────────────────────────────────────────────────────────────────
# AC #4: acceptance NOT green => log + no-op (cron exit 0)
# ─────────────────────────────────────────────────────────────────────


def test_acceptance_not_green_noops(tmp_path: Path) -> None:
    repo, remote = _init_repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "feature\n")
    main_before_run = _git(remote, "rev-parse", "main")
    audit_db = _init_release_audit_db(tmp_path)
    event_log = _write_event_log(
        tmp_path,
        '{"event": "milestone_blocked", "fixVersion": "v9.99.0", "reasons": [{"gate": "smoke_suite"}]}',
    )

    result = _run_script(repo=repo, event_log=event_log, audit_db=audit_db)

    assert result.returncode == 0, result.stderr + result.stdout
    # Remote main must NOT have advanced.
    assert _git(remote, "rev-parse", "main") == main_before_run
    rows = _read_audit_rows(audit_db)
    assert len(rows) == 1
    assert rows[0][0] == "milestone_not_accepted"
    # fix_version retained from the blocked record.
    assert rows[0][1] == "v9.99.0"
    assert "MilestoneNotAccepted" in result.stdout


# ─────────────────────────────────────────────────────────────────────
# Error catalog: GitFFNotPossible (develop/main diverged)
# ─────────────────────────────────────────────────────────────────────


def test_ff_impossible_when_diverged(tmp_path: Path) -> None:
    repo, remote = _init_repo_with_remote(tmp_path)
    # develop adds a feature commit
    _commit_file(repo, "feature.txt", "feature\n")
    # main forks off with a hotfix commit (so develop..main is non-empty)
    _git(repo, "checkout", "main")
    _commit_file(repo, "hotfix.txt", "hotfix\n")
    main_before_run = _git(remote, "rev-parse", "main")
    audit_db = _init_release_audit_db(tmp_path)
    event_log = _write_event_log(
        tmp_path,
        '{"event": "milestone_ready", "fixVersion": "v9.99.0"}',
    )

    result = _run_script(repo=repo, event_log=event_log, audit_db=audit_db)

    assert result.returncode == 2, result.stderr + result.stdout
    # Remote main must NOT have moved.
    assert _git(remote, "rev-parse", "main") == main_before_run
    rows = _read_audit_rows(audit_db)
    assert len(rows) == 1
    assert rows[0][0] == "ff_not_possible"
    assert "GitFFNotPossible" in result.stdout


# ─────────────────────────────────────────────────────────────────────
# Error catalog: MainPushRejected (retry next day)
# ─────────────────────────────────────────────────────────────────────


def test_push_rejected_records_audit_and_exits_nonzero(tmp_path: Path) -> None:
    repo, _remote = _init_repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ready\n")
    # Point the remote at a non-existent bare repo so the push will fail.
    _git(repo, "remote", "remove", "gerrit")
    _git(repo, "remote", "add", "gerrit", str(tmp_path / "does-not-exist.git"))

    audit_db = _init_release_audit_db(tmp_path)
    event_log = _write_event_log(
        tmp_path,
        '{"event": "milestone_ready", "fixVersion": "v9.99.0"}',
    )

    result = _run_script(repo=repo, event_log=event_log, audit_db=audit_db)

    assert result.returncode == 3, result.stderr + result.stdout
    rows = _read_audit_rows(audit_db)
    assert len(rows) == 1
    assert rows[0][0] == "push_rejected"
    assert "MainPushRejected" in result.stdout


# ─────────────────────────────────────────────────────────────────────
# AC #5 + alembic apply: `release_audit` schema lands cleanly
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def m0207():
    return _load_module(MIGRATION_0207, "_alembic_test_0207")


def test_alembic_0207_creates_release_audit_table(m0207) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            m0207.upgrade()

        columns = {
            row[1]: row
            for row in conn.exec_driver_sql("PRAGMA table_info(release_audit)")
        }
        expected = {
            "id",
            "ts",
            "outcome",
            "fix_version",
            "develop_sha",
            "main_sha",
            "detail",
        }
        assert expected.issubset(columns), f"missing: {expected - set(columns)}"
        # outcome is NOT NULL
        assert columns["outcome"][3] == 1
        # fix_version is nullable (milestone_not_accepted rows lack a version)
        assert columns["fix_version"][3] == 0

        # CHECK constraint rejects unknown outcomes
        with pytest.raises(sa.exc.IntegrityError):
            conn.exec_driver_sql(
                "INSERT INTO release_audit (outcome) VALUES ('typo_outcome')"
            )

        # Known outcome inserts cleanly and ts default fires
        conn.exec_driver_sql(
            "INSERT INTO release_audit (outcome, fix_version) "
            "VALUES ('promoted', 'v9.99.0')"
        )
        row = conn.exec_driver_sql(
            "SELECT outcome, fix_version, ts FROM release_audit ORDER BY id DESC LIMIT 1"
        ).first()
        assert row.outcome == "promoted"
        assert row.fix_version == "v9.99.0"
        assert row.ts is not None

        # Indexes exist for the documented query shapes
        idx_names = {
            r[1]
            for r in conn.exec_driver_sql("PRAGMA index_list(release_audit)")
        }
        assert "idx_release_audit_ts" in idx_names
        assert "idx_release_audit_outcome_ts" in idx_names

        # Round-trip the downgrade path
        with Operations.context(ctx):
            m0207.downgrade()
        names = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        assert "release_audit" not in names
        assert "idx_release_audit_ts" not in names
        assert "idx_release_audit_outcome_ts" not in names


# ─────────────────────────────────────────────────────────────────────
# systemd contract: daily 07:00 UTC trigger; service tails the cron
# ─────────────────────────────────────────────────────────────────────


def test_systemd_timer_fires_daily_at_07_utc() -> None:
    text = TIMER.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 07:00:00 UTC" in text
    assert "Persistent=true" in text
    assert "Unit=auto-promote-develop.service" in text


def test_systemd_service_invokes_the_promote_script() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    assert "scripts/auto_promote_develop_to_main.sh" in text
    assert "Type=oneshot" in text
