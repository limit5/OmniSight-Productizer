"""G4 #1 — Live Postgres Alembic upgrade/downgrade integration test.

This test runs the actual ``alembic upgrade head`` → ``alembic downgrade
0001`` → ``alembic upgrade head`` cycle against a real Postgres instance
to prove the shim works end-to-end, not just at the unit-test level.

Skipped when ``OMNI_TEST_PG_URL`` is not set — the main test-suite run
does not require a Postgres service container. CI's new
``postgres-matrix`` job (G4 row "CI 新增 Postgres service matrix")
provisions one and runs ``OMNI_TEST_PG_URL=postgresql+psycopg2://…
pytest backend/tests/test_alembic_pg_live_upgrade.py``.

Why a separate file: the live test has external dependencies (docker /
network / psycopg2) that the pure-unit file ``test_alembic_pg_compat.py``
deliberately avoids. Splitting keeps the unit file cheap (< 1 s) while
still pinning the runtime contract when a PG is available.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PG_URL = os.environ.get("OMNI_TEST_PG_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="OMNI_TEST_PG_URL not set — skip live PG integration test",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"


def _psql_reset_public_schema(url: str) -> None:
    """Drop+recreate the public schema so each test gets a fresh DB."""
    try:
        import psycopg2  # type: ignore
    except ImportError:
        pytest.skip("psycopg2 not installed")
    # SQLAlchemy-style URL → libpq DSN
    if url.startswith("postgresql+psycopg2://"):
        libpq = url.replace("postgresql+psycopg2://", "postgresql://", 1)
    else:
        libpq = url
    conn = psycopg2.connect(libpq)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
    finally:
        conn.close()


def _alembic(*argv: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["SQLALCHEMY_URL"] = PG_URL
    env["OMNISIGHT_SKIP_FS_MIGRATIONS"] = "1"
    return subprocess.run(
        ["alembic", *argv],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(autouse=True)
def fresh_public_schema():
    _psql_reset_public_schema(PG_URL)
    yield


class TestLiveAlembicPostgres:
    def test_upgrade_head_exits_zero(self):
        proc = _alembic("upgrade", "head")
        assert proc.returncode == 0, proc.stderr

    def test_upgrade_creates_all_expected_tables(self):
        _alembic("upgrade", "head")
        import psycopg2  # type: ignore
        libpq = PG_URL.replace("postgresql+psycopg2://", "postgresql://", 1)
        conn = psycopg2.connect(libpq)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' ORDER BY table_name"
            )
            tables = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()
        expected = {
            "agents", "alembic_version", "api_keys", "artifacts",
            "audit_log", "auto_decision_log", "debug_findings",
            "decision_profiles", "decision_rules", "episodic_memory",
            "event_log", "github_installations", "handoffs",
            "notifications", "npi_state", "project_runs", "sessions",
            "simulations", "task_comments", "tasks",
            "tenant_egress_policies", "tenant_egress_requests",
            "tenant_secrets", "tenants", "token_usage",
            "user_preferences", "users", "workflow_runs",
            "workflow_steps",
        }
        missing = expected - tables
        assert not missing, f"missing tables after upgrade: {missing}"

    def test_default_tenant_row_inserted(self):
        _alembic("upgrade", "head")
        import psycopg2  # type: ignore
        libpq = PG_URL.replace("postgresql+psycopg2://", "postgresql://", 1)
        conn = psycopg2.connect(libpq)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM tenants WHERE id='t-default'")
            row = cur.fetchone()
        finally:
            conn.close()
        assert row == ("t-default",)

    def test_downgrade_to_baseline(self):
        _alembic("upgrade", "head")
        proc = _alembic("downgrade", "0001")
        assert proc.returncode == 0, proc.stderr

    def test_upgrade_downgrade_upgrade_cycle(self):
        assert _alembic("upgrade", "head").returncode == 0
        assert _alembic("downgrade", "0001").returncode == 0
        assert _alembic("upgrade", "head").returncode == 0

    def test_current_head_revision(self):
        _alembic("upgrade", "head")
        proc = _alembic("current")
        assert proc.returncode == 0
        # The head revision id appears somewhere in the stdout. Drift
        # guard: assert (head) marker is present rather than hardcoding
        # the 4-digit id — Phase-3-Runtime-v2 SP-2.1 bumped head to
        # 0017, and future phases will bump it again.
        assert "(head)" in proc.stdout

    def test_decision_rules_negative_column_present(self):
        _alembic("upgrade", "head")
        import psycopg2  # type: ignore
        libpq = PG_URL.replace("postgresql+psycopg2://", "postgresql://", 1)
        conn = psycopg2.connect(libpq)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='decision_rules' "
                "AND column_name='negative'"
            )
            assert cur.fetchone() is not None
        finally:
            conn.close()

    def test_audit_log_id_is_identity_column(self):
        _alembic("upgrade", "head")
        import psycopg2  # type: ignore
        libpq = PG_URL.replace("postgresql+psycopg2://", "postgresql://", 1)
        conn = psycopg2.connect(libpq)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT is_identity FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='audit_log' "
                "AND column_name='id'"
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row and row[0] == "YES"
