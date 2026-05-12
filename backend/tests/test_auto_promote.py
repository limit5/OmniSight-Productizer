"""OP-877 D5 / OP-961 — daily develop -> main auto-promote contract.

History
-------
OP-877 shipped ``scripts/auto_promote_develop_to_main.sh`` doing its own
``git push develop:main`` + its own ``release_audit`` insert. OP-960
(AUDIT-13) rewrote ``backend.agents.auto_promote_main`` to advance ``main``
*through Gerrit Code Review* (``develop`` -> ``refs/for/main`` with the
``auto-promote`` + ``milestone:R3-fastforward`` hashtags / ``develop-to-main``
topic) and to own the release audit row. OP-961 rewrites the cron wrapper
to delegate to that module and map ``PromotionResult.status`` to a cron
exit code.

Script-contract cases (the 6 the OP-961 ticket asks for):

1. ``test_milestone_ready_creates_refs_for_main_review_change`` — a
   ``milestone_ready`` record + a clean FF on ``develop`` → the wrapper
   pushes ``develop:refs/for/main`` (with the documented hashtags +
   topic), leaves ``refs/heads/main`` untouched, exits 0.
2. ``test_milestone_blocked_noops_exit_zero`` — latest record is
   ``milestone_blocked`` → no push, exit 0.
3. ``test_no_milestone_record_noops_exit_zero`` — event log has no
   milestone record → no push, exit 0.
4. ``test_diverged_main_refused_exit_3`` — ``main`` has commit(s) absent
   from ``develop`` → no push, exit 3 (non-fast-forward refused).
5. ``test_push_rejected_exit_2`` — the ``refs/for/main`` push fails →
   exit 2 (Gerrit rejected).
6. ``test_batch_too_large_exit_4`` — ``develop`` is more commits ahead of
   ``main`` than ``receive.maxBatchChanges`` → no push, exit 4.

Plus:

* ``test_forwards_audit_kind_and_conductor_env`` — the wrapper exports
  ``OP877_AUDIT_DB_KIND`` and every ``RELEASE_CONDUCTOR_*`` var into the
  Python child (AC #2), verified against a stub ``auto_promote_main``.
* ``test_wrapper_does_not_write_release_audit_itself`` — the script no
  longer touches the ``release_audit`` table directly (AC #4).
* ``test_alembic_0207_creates_release_audit_table`` — unchanged: the
  ``0207_release_audit`` migration still lands the table the Python
  module's audit sink targets.
* ``test_systemd_*`` — unchanged systemd unit contract.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import sqlalchemy as sa


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "auto_promote_develop_to_main.sh"
SERVICE = REPO_ROOT / "deploy" / "systemd" / "auto-promote-develop.service"
TIMER = REPO_ROOT / "deploy" / "systemd" / "auto-promote-develop.timer"
SMOKE_SCRIPT = REPO_ROOT / "deploy" / "scripts" / "auto_promote_audit_db_smoke.sh"
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
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    # Gerrit advertises push-option support; a plain bare repo does not
    # unless told to. The module's refs/for/main push uses `git push -o
    # hashtag=… -o topic=…` push options, so the stand-in remote must
    # advertise them or every push is "the receiving end does not support
    # push options".
    subprocess.run(
        ["git", "-C", str(remote), "config", "receive.advertisePushOptions", "true"],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "OP-877 Test")
    _git(repo, "config", "user.email", "op-877@example.test")
    _git(repo, "remote", "add", "gerrit", str(remote))
    _commit_file(repo, "base.txt", "base\n")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "gerrit", "main:main")
    _git(repo, "checkout", "-b", "develop")
    _git(repo, "push", "gerrit", "develop:develop")
    return repo, remote


def _remote_refs(remote: Path) -> dict[str, str]:
    out = _git(remote, "for-each-ref", "--format=%(refname) %(objectname)")
    refs: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        ref, sha = line.split()
        refs[ref] = sha
    return refs


def _write_event_log(tmp_path: Path, *records: str) -> Path:
    path = tmp_path / "milestone.log"
    path.write_text(("\n".join(records) + "\n") if records else "", encoding="utf-8")
    return path


def _run_script(
    *,
    repo: Path,
    event_log: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        OP877_REPO=str(repo),
        OP877_REMOTE="gerrit",
        OP877_SOURCE_BRANCH="develop",
        OP877_TARGET_BRANCH="main",
        OP877_EVENT_LOG=str(event_log),
        OP877_NOW="2026-05-12T07:00:00Z",
    )
    # Make sure the wrapper resolves the real `backend` package.
    env.setdefault("OP961_PKG_ROOT", str(REPO_ROOT))
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
# 1. milestone_ready → develop:refs/for/main review change, main untouched
# ─────────────────────────────────────────────────────────────────────


def test_milestone_ready_creates_refs_for_main_review_change(tmp_path: Path) -> None:
    repo, remote = _init_repo_with_remote(tmp_path)
    develop_tip = _commit_file(repo, "feature.txt", "ready\n")
    main_before = _remote_refs(remote)["refs/heads/main"]
    event_log = _write_event_log(
        tmp_path, '{"event": "milestone_ready", "fixVersion": "v9.99.0"}'
    )

    result = _run_script(repo=repo, event_log=event_log)

    assert result.returncode == 0, result.stderr + result.stdout
    refs = _remote_refs(remote)
    # main advanced through review, not a direct push.
    assert refs["refs/heads/main"] == main_before
    assert refs.get("refs/for/main") == develop_tip
    # The module's telemetry event records the hashtags + topic.
    events = [
        json.loads(line[line.index("{"):])
        for line in result.stdout.splitlines()
        if '"event": "main_promote_change_created"' in line
    ]
    assert events, result.stdout
    assert events[0]["hashtags"] == ["auto-promote", "milestone:R3-fastforward"]
    assert events[0]["topic"] == "develop-to-main"
    assert "review change(s) created" in result.stdout


# ─────────────────────────────────────────────────────────────────────
# 1b. milestone_force_promoted (ADR-0019 / OP-967 AUDIT-18b) is a
#     green-equivalent: same refs/for/main review change, exit 0, plus a
#     loud OPERATOR FORCE-PROMOTE line in the journal.
# ─────────────────────────────────────────────────────────────────────


def test_milestone_force_promoted_treated_as_green(tmp_path: Path) -> None:
    repo, remote = _init_repo_with_remote(tmp_path)
    develop_tip = _commit_file(repo, "feature.txt", "forced\n")
    main_before = _remote_refs(remote)["refs/heads/main"]
    event_log = _write_event_log(
        tmp_path,
        # an earlier blocked record then the operator override on top
        '{"event": "milestone_blocked", "fixVersion": "v9.99.0", "reasons": [{"gate": "ci_canary"}]}',
        '{"event": "milestone_force_promoted", "fixVersion": "v9.99.0", '
        '"operator_override": true, "reasons": [{"gate": "ci_canary", "code": "status_not_green"}]}',
    )

    result = _run_script(repo=repo, event_log=event_log)

    assert result.returncode == 0, result.stderr + result.stdout
    refs = _remote_refs(remote)
    assert refs["refs/heads/main"] == main_before
    assert refs.get("refs/for/main") == develop_tip
    assert "review change(s) created" in result.stdout
    assert "OPERATOR FORCE-PROMOTE" in result.stdout
    decision = [
        json.loads(line.split("__OP961_RESULT__ ", 1)[1])
        for line in result.stdout.splitlines()
        if "__OP961_RESULT__" in line
    ][-1]
    assert decision["status"] == "change_created"
    assert decision["operator_override"] is True


def test_milestone_force_promoted_noop_when_main_current(tmp_path: Path) -> None:
    repo, remote = _init_repo_with_remote(tmp_path)
    # develop == main: nothing to promote, but the override event must
    # still be *recognised* (no MilestoneNotAccepted).
    before = _remote_refs(remote)
    event_log = _write_event_log(
        tmp_path,
        '{"event": "milestone_force_promoted", "fixVersion": "v9.99.0", '
        '"operator_override": true, "reasons": [{"gate": "smoke_suite"}]}',
    )

    result = _run_script(repo=repo, event_log=event_log)

    assert result.returncode == 0, result.stderr + result.stdout
    assert _remote_refs(remote) == before
    assert "MilestoneNotAccepted" not in result.stdout
    assert "Noop" in result.stdout


# ─────────────────────────────────────────────────────────────────────
# 2. + 3. not-ready / no record → no-op, exit 0
# ─────────────────────────────────────────────────────────────────────


def test_milestone_blocked_noops_exit_zero(tmp_path: Path) -> None:
    repo, remote = _init_repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "feature\n")
    before = _remote_refs(remote)
    event_log = _write_event_log(
        tmp_path,
        '{"event": "milestone_blocked", "fixVersion": "v9.99.0", "reasons": [{"gate": "smoke_suite"}]}',
    )

    result = _run_script(repo=repo, event_log=event_log)

    assert result.returncode == 0, result.stderr + result.stdout
    assert _remote_refs(remote) == before  # nothing pushed
    assert "MilestoneNotAccepted" in result.stdout


def test_no_milestone_record_noops_exit_zero(tmp_path: Path) -> None:
    repo, remote = _init_repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "feature\n")
    before = _remote_refs(remote)
    event_log = _write_event_log(tmp_path, "not json at all", '{"event": "something_else"}')

    result = _run_script(repo=repo, event_log=event_log)

    assert result.returncode == 0, result.stderr + result.stdout
    assert _remote_refs(remote) == before
    assert "MilestoneNotAccepted" in result.stdout


# ─────────────────────────────────────────────────────────────────────
# 4. main has commits absent from develop → non-FF refused, exit 3
# ─────────────────────────────────────────────────────────────────────


def test_diverged_main_refused_exit_3(tmp_path: Path) -> None:
    repo, remote = _init_repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "feature\n")  # develop-only commit
    _git(repo, "checkout", "main")
    _commit_file(repo, "hotfix.txt", "hotfix\n")  # main-only commit → diverged
    _git(repo, "checkout", "develop")
    before = _remote_refs(remote)
    event_log = _write_event_log(
        tmp_path, '{"event": "milestone_ready", "fixVersion": "v9.99.0"}'
    )

    result = _run_script(repo=repo, event_log=event_log)

    assert result.returncode == 3, result.stderr + result.stdout
    assert _remote_refs(remote) == before
    assert "GitFFNotPossible" in result.stdout


# ─────────────────────────────────────────────────────────────────────
# 5. refs/for/main push fails → exit 2
# ─────────────────────────────────────────────────────────────────────


def test_push_rejected_exit_2(tmp_path: Path) -> None:
    repo, _remote = _init_repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ready\n")
    # Point the remote at a non-existent bare repo so the push fails.
    _git(repo, "remote", "remove", "gerrit")
    _git(repo, "remote", "add", "gerrit", str(tmp_path / "does-not-exist.git"))
    event_log = _write_event_log(
        tmp_path, '{"event": "milestone_ready", "fixVersion": "v9.99.0"}'
    )

    result = _run_script(repo=repo, event_log=event_log)

    assert result.returncode == 2, result.stderr + result.stdout
    assert "MainPushRejected" in result.stdout


# ─────────────────────────────────────────────────────────────────────
# 6. develop too far ahead of main → batch_too_large, exit 4
# ─────────────────────────────────────────────────────────────────────


def test_batch_too_large_exit_4(tmp_path: Path) -> None:
    repo, remote = _init_repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ready\n")
    before = _remote_refs(remote)
    event_log = _write_event_log(
        tmp_path, '{"event": "milestone_ready", "fixVersion": "v9.99.0"}'
    )

    result = _run_script(
        repo=repo, event_log=event_log, extra_env={"OP877_MAX_PROMOTE_BATCH": "0"}
    )

    assert result.returncode == 4, result.stderr + result.stdout
    assert _remote_refs(remote) == before  # nothing pushed
    assert "BatchTooLarge" in result.stdout


# ─────────────────────────────────────────────────────────────────────
# AC #2 — OP877_AUDIT_DB_KIND + RELEASE_CONDUCTOR_* forwarded to Python
# ─────────────────────────────────────────────────────────────────────


def _stub_pkg_root(tmp_path: Path, env_dump: Path) -> Path:
    """A throw-away ``backend.agents.auto_promote_main`` that records its env."""
    root = tmp_path / "pkgstub"
    (root / "backend" / "agents").mkdir(parents=True)
    (root / "backend" / "__init__.py").write_text("", encoding="utf-8")
    (root / "backend" / "agents" / "__init__.py").write_text("", encoding="utf-8")
    (root / "backend" / "agents" / "auto_promote_main.py").write_text(
        textwrap.dedent(
            f"""
            import json, os
            from dataclasses import dataclass, field

            EVENT_MILESTONE_READY = "milestone_ready"
            PROMOTE_HASHTAGS = ("auto-promote", "milestone:R3-fastforward")
            PROMOTE_TOPIC = "develop-to-main"

            @dataclass
            class PromotionResult:
                status: str = "change_created"
                version: str = ""
                develop_only: tuple = ()
                main_only: tuple = ()
                detail: str = ""
                created_changes: tuple = ()

            def promote_on_milestone_ready(event, **kw):
                with open({str(env_dump)!r}, "w", encoding="utf-8") as fh:
                    json.dump(dict(os.environ), fh)
                return PromotionResult(version=str(event.get("fixVersion") or ""))
            """
        ),
        encoding="utf-8",
    )
    return root


def test_forwards_audit_kind_and_conductor_env(tmp_path: Path) -> None:
    repo, _remote = _init_repo_with_remote(tmp_path)
    event_log = _write_event_log(
        tmp_path, '{"event": "milestone_ready", "fixVersion": "v1.2.3"}'
    )
    env_dump = tmp_path / "child_env.json"
    pkg_root = _stub_pkg_root(tmp_path, env_dump)

    result = _run_script(
        repo=repo,
        event_log=event_log,
        extra_env={
            "OP961_PKG_ROOT": str(pkg_root),
            "OP877_AUDIT_DB_KIND": "postgres",
            "RELEASE_CONDUCTOR_AUDIT_LOG": "/tmp/conductor/audit.jsonl",
            "RELEASE_CONDUCTOR_STATE_DIR": "/tmp/conductor/state",
        },
    )

    assert result.returncode == 0, result.stderr + result.stdout
    child_env = json.loads(env_dump.read_text(encoding="utf-8"))
    assert child_env["OP877_AUDIT_DB_KIND"] == "postgres"
    assert child_env["RELEASE_CONDUCTOR_AUDIT_LOG"] == "/tmp/conductor/audit.jsonl"
    assert child_env["RELEASE_CONDUCTOR_STATE_DIR"] == "/tmp/conductor/state"


# ─────────────────────────────────────────────────────────────────────
# AC #4 — the wrapper itself no longer writes the release_audit table
# ─────────────────────────────────────────────────────────────────────


def test_wrapper_does_not_write_release_audit_itself(tmp_path: Path) -> None:
    repo, _remote = _init_repo_with_remote(tmp_path)
    _commit_file(repo, "feature.txt", "ready\n")
    event_log = _write_event_log(
        tmp_path, '{"event": "milestone_ready", "fixVersion": "v9.99.0"}'
    )
    # A sqlite DB that, if the script tried its old direct insert, would
    # gain a row. The script must not touch it (the audit write moved to
    # the Python module, which uses `backend.audit`, not this table).
    audit_db = tmp_path / "release_audit.sqlite"
    with sqlite3.connect(audit_db) as conn:
        conn.execute(
            "CREATE TABLE release_audit (id INTEGER PRIMARY KEY, outcome TEXT)"
        )

    result = _run_script(
        repo=repo,
        event_log=event_log,
        extra_env={
            "OP877_AUDIT_DB": f"sqlite:{audit_db}",
            "OP877_AUDIT_DB_KIND": "sqlite",
            "OMNISIGHT_DATABASE_URL": "",
        },
    )

    assert result.returncode == 0, result.stderr + result.stdout
    with sqlite3.connect(audit_db) as conn:
        rows = list(conn.execute("SELECT * FROM release_audit"))
    assert rows == []
    # And the script source carries no SQL INSERT anymore.
    assert "INSERT INTO release_audit" not in SCRIPT.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────
# alembic apply: `release_audit` schema still lands cleanly
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
        assert columns["outcome"][3] == 1  # NOT NULL
        assert columns["fix_version"][3] == 0  # nullable

        with pytest.raises(sa.exc.IntegrityError):
            conn.exec_driver_sql(
                "INSERT INTO release_audit (outcome) VALUES ('typo_outcome')"
            )

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

        idx_names = {
            r[1] for r in conn.exec_driver_sql("PRAGMA index_list(release_audit)")
        }
        assert "idx_release_audit_ts" in idx_names
        assert "idx_release_audit_outcome_ts" in idx_names

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
# systemd contract: daily 07:00 UTC trigger; service runs the cron once
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


# ─────────────────────────────────────────────────────────────────────
# OP-964 (AUDIT-16) — the cron unit must carry a usable DB env so the
# Python audit sink can reach the `release_audit` Postgres, plus a
# standalone smoke test for that connectivity.
# ─────────────────────────────────────────────────────────────────────


def test_systemd_service_loads_audit_db_env() -> None:
    """The unit pulls in OMNISIGHT_DATABASE_URL via an EnvironmentFile so
    backend.agents.auto_promote_main's audit sink doesn't degrade to the
    SQLite dev path and skip the durable release_audit row (OP-964)."""
    text = SERVICE.read_text(encoding="utf-8")
    env_file_lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip().startswith("EnvironmentFile=")
    ]
    assert env_file_lines, "auto-promote-develop.service must declare an EnvironmentFile= for the DB env"
    # Points at the canonical backend .env (same one omnisight-backend /
    # omnisight-slo-monitor load), and is "-"-prefixed so a missing file
    # is non-fatal on dev hosts / the test seam.
    assert any(
        "OmniSight-Productizer/.env" in ln and "EnvironmentFile=-" in ln
        for ln in env_file_lines
    ), env_file_lines


def test_audit_db_smoke_script_shape() -> None:
    """The smoke script exists, is executable, and exercises the same
    DSN resolver + asyncpg driver the module's audit sink uses — without
    a psql dependency (OP-960/OP-961 removed it; psql isn't in the runner
    env)."""
    assert SMOKE_SCRIPT.is_file(), SMOKE_SCRIPT
    assert os.access(SMOKE_SCRIPT, os.X_OK), f"{SMOKE_SCRIPT} must be executable"
    body = SMOKE_SCRIPT.read_text(encoding="utf-8")
    assert "OMNISIGHT_DATABASE_URL" in body
    assert "_resolve_pg_dsn" in body  # the exact resolver auto_promote_main uses
    assert "asyncpg" in body
    assert "release_audit" in body
    # `psql` may appear only in the informational `command -v psql` line —
    # never as the actual connectivity check.
    for raw in body.splitlines():
        line = raw.strip()
        if "psql" in line and not line.startswith("#"):
            assert "command -v psql" in line, f"smoke script must not depend on psql: {line!r}"


def test_audit_db_smoke_script_fails_loudly_without_dsn() -> None:
    """No OMNISIGHT_DATABASE_URL / DATABASE_URL -> exit 2 with a message
    naming the SQLite-fallback hazard (the failure mode OP-925 R3 hit)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env.pop("OMNISIGHT_DATABASE_URL", None)
    env.pop("DATABASE_URL", None)
    proc = subprocess.run(
        ["bash", str(SMOKE_SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "no Postgres DSN resolved" in proc.stdout
