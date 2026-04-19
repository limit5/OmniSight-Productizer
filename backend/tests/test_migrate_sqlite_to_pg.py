"""G4 #4 — contract tests for scripts/migrate_sqlite_to_pg.py.

TODO row 1363:
    資料搬移腳本 `scripts/migrate_sqlite_to_pg.py`
    （含 audit_log hash chain 連續性驗證）

Locks the following deliverables:

* Pure hash-chain walk (``verify_hash_chain_in_rows``) reproduces the
  exact algorithm :func:`backend.audit._hash` uses — so a script-side
  detection of a broken chain lines up with a server-side one.
* INSERT SQL and IDENTITY sequence-reset SQL are well-formed and quote
  identifiers (protects against a future table named ``user`` etc.).
* CLI argument parsing rejects malformed URLs, demands ``--target`` for
  non-dry-run runs, and accepts bare SQLite paths for ``--source``.
* The Postgres connection path is exercised via ``unittest.mock`` so
  the SQLite-only CI track can pin the insert-builder and
  executemany-batching contract without asyncpg installed.
* End-to-end round-trip on a real SQLite source + a mocked PG target:
  audit_log rows preserve id ordering, prev_hash, curr_hash, and the
  chain verifies on the synthetic target side.
* Sequence reset covers exactly the IDENTITY-column tables and nothing
  else (``TABLES_WITH_IDENTITY_ID``).

Postgres live path is intentionally NOT covered here — a later
``OMNI_TEST_PG_URL``-gated live test will do that once the CI PG matrix
(G4 #5) lands. The mocks here freeze the wire-protocol shape so that
live test only has to verify the adapter stays the same.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "migrate_sqlite_to_pg.py"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# Put scripts/ on the import path like sibling G4 test files do.
sys.path.insert(0, str(SCRIPTS_DIR))

import migrate_sqlite_to_pg as mig  # noqa: E402


# ---------------------------------------------------------------------------
# (1) Physical file shape + module contract.
# ---------------------------------------------------------------------------


class TestScriptShape:
    def test_script_exists(self) -> None:
        assert SCRIPT_PATH.is_file(), (
            f"scripts/migrate_sqlite_to_pg.py missing at {SCRIPT_PATH}. "
            "Row 1363 requires this to be shippable with the repo."
        )

    def test_script_is_executable_python(self) -> None:
        # The `--help` subprocess call is the cheapest smoke for
        # "module imports cleanly without a DB".
        r = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, f"--help exited {r.returncode}: {r.stderr}"
        assert "SQLite → PostgreSQL" in r.stdout or "SQLite" in r.stdout

    def test_tables_in_order_is_nonempty(self) -> None:
        assert len(mig.TABLES_IN_ORDER) >= 10
        # tenants must come first so FK-referencing children below can
        # safely reference it. FKs land on api_keys / tenant_secrets /
        # tenant_egress_* (see migrations 0012-0015).
        assert mig.TABLES_IN_ORDER[0] == "tenants"

    def test_tables_in_order_covers_known_tables(self) -> None:
        # Sanity: ensure every FK-target table appears exactly once.
        assert len(set(mig.TABLES_IN_ORDER)) == len(mig.TABLES_IN_ORDER), \
            "duplicate table in TABLES_IN_ORDER"
        for t in ("audit_log", "tenants", "users", "sessions"):
            assert t in mig.TABLES_IN_ORDER, f"{t} missing from ordering"

    def test_identity_tables_are_subset_of_all(self) -> None:
        for t in mig.TABLES_WITH_IDENTITY_ID:
            assert t in mig.TABLES_IN_ORDER

    def test_identity_tables_matches_schema(self) -> None:
        # Migrations 0003/0004/0005/0001 declare the INTEGER PK with
        # AUTOINCREMENT (or plain INTEGER PRIMARY KEY for event_log).
        # If this set drifts from schema, the sequence reset will be
        # wrong and new inserts on PG will collide. Lock it.
        #
        # 2026-04-20 Phase-3 pre-req F2: five freshly-covered tables
        # (dag_plans / iq_runs / mfa_backup_codes / password_history /
        # prompt_versions) added — each has INTEGER PRIMARY KEY in
        # its alembic migration. bootstrap_state and user_mfa do NOT
        # appear here because their PKs are TEXT.
        assert set(mig.TABLES_WITH_IDENTITY_ID) == {
            "event_log",
            "audit_log",
            "auto_decision_log",
            "github_installations",
            "dag_plans",
            "iq_runs",
            "mfa_backup_codes",
            "password_history",
            "prompt_versions",
        }

    # 2026-04-20 Phase-3 pre-req F2 — pin the seven tables that the
    # migrator predated (schema drift caught by the Phase-3 Step-1
    # audit) so a future alembic migration adding ANOTHER new table
    # + forgetting to update this list explicitly breaks a test
    # rather than silently losing data at cutover time.
    def test_phase3_pre_req_tables_are_covered(self) -> None:
        expected_new_coverage = {
            "bootstrap_state",
            "dag_plans",
            "iq_runs",
            "mfa_backup_codes",
            "password_history",
            "prompt_versions",
            "user_mfa",
        }
        for t in expected_new_coverage:
            assert t in mig.TABLES_IN_ORDER, (
                f"{t} missing — Phase-3 F2 extension regressed. "
                "Adding a new alembic table requires a matching "
                "TABLES_IN_ORDER entry (and, for INTEGER PK tables, "
                "TABLES_WITH_IDENTITY_ID too)."
            )

    def test_mfa_tables_come_after_users(self) -> None:
        """FK contract: ``user_mfa``, ``mfa_backup_codes``, and
        ``password_history`` all reference ``users.id``. On PG with
        FK enforcement enabled, inserting them before their parent
        rows raises ForeignKeyViolation. Lock the ordering so a
        well-meaning alphabetical sort never silently breaks replay.
        """
        order = list(mig.TABLES_IN_ORDER)
        users_idx = order.index("users")
        for child in ("user_mfa", "mfa_backup_codes", "password_history"):
            child_idx = order.index(child)
            assert child_idx > users_idx, (
                f"{child} must be AFTER users in TABLES_IN_ORDER "
                f"(users at {users_idx}, {child} at {child_idx})"
            )

    def test_dag_plans_comes_after_workflow_runs(self) -> None:
        """Same FK-replay discipline: dag_plans.run_id references
        workflow_runs.id. The reference is soft-linked in SQLite
        (no FK constraint on that column) but PG may have it per
        the migration shim — play safe and order anyway."""
        order = list(mig.TABLES_IN_ORDER)
        wf_idx = order.index("workflow_runs")
        dp_idx = order.index("dag_plans")
        assert dp_idx > wf_idx, (
            f"dag_plans must be AFTER workflow_runs (wf at {wf_idx}, "
            f"dag_plans at {dp_idx})"
        )


# ---------------------------------------------------------------------------
# (2) Pure hash-chain verifier lines up with the server-side audit module.
# ---------------------------------------------------------------------------


def _hash(prev: str, payload_canon: str) -> str:
    return hashlib.sha256((prev + payload_canon).encode("utf-8")).hexdigest()


def _make_row(
    row_id: int,
    prev_hash: str,
    *,
    action: str = "act",
    entity_kind: str = "thing",
    entity_id: str = "x",
    actor: str = "system",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ts: float = 1_000_000.0,
    tenant_id: str = "t-default",
) -> dict[str, Any]:
    before_json = json.dumps(before or {}, ensure_ascii=False)
    after_json = json.dumps(after or {}, ensure_ascii=False)
    payload = {
        "action": action,
        "entity_kind": entity_kind,
        "entity_id": entity_id or "",
        "before": before or {},
        "after": after or {},
        "actor": actor,
    }
    payload_canon = mig.canonical_json(payload) + str(round(ts, 6))
    curr = _hash(prev_hash, payload_canon)
    return {
        "id":          row_id,
        "ts":          ts,
        "actor":       actor,
        "action":      action,
        "entity_kind": entity_kind,
        "entity_id":   entity_id,
        "before_json": before_json,
        "after_json":  after_json,
        "prev_hash":   prev_hash,
        "curr_hash":   curr,
        "tenant_id":   tenant_id,
    }


class TestHashChainVerifier:
    def test_empty_chain_passes(self) -> None:
        ok, brk = mig.verify_hash_chain_in_rows([])
        assert ok and brk is None

    def test_single_row_passes(self) -> None:
        row = _make_row(1, "")
        ok, brk = mig.verify_hash_chain_in_rows([row])
        assert ok, f"chain break at {brk!r}"

    def test_multi_row_chain_passes(self) -> None:
        rows = []
        prev = ""
        for i in range(1, 11):
            r = _make_row(i, prev, action=f"a_{i}", ts=1_000_000.0 + i)
            rows.append(r)
            prev = r["curr_hash"]
        ok, brk = mig.verify_hash_chain_in_rows(rows)
        assert ok, f"unexpected break: {brk!r}"

    def test_tampered_after_json_detected(self) -> None:
        rows = []
        prev = ""
        for i in range(1, 6):
            r = _make_row(i, prev, ts=1_000.0 + i)
            rows.append(r)
            prev = r["curr_hash"]
        # Flip one row's after_json without recomputing hash.
        rows[2]["after_json"] = '{"forged": true}'
        ok, brk = mig.verify_hash_chain_in_rows(rows)
        assert not ok
        assert brk is not None
        assert brk.row_id == rows[2]["id"]
        assert brk.why == "curr_hash"

    def test_broken_prev_hash_detected(self) -> None:
        rows = []
        prev = ""
        for i in range(1, 4):
            r = _make_row(i, prev, ts=1_000.0 + i)
            rows.append(r)
            prev = r["curr_hash"]
        # Forge the second row's prev_hash to something else.
        rows[1]["prev_hash"] = "deadbeef" * 8
        ok, brk = mig.verify_hash_chain_in_rows(rows)
        assert not ok
        assert brk is not None
        assert brk.row_id == rows[1]["id"]
        assert brk.why == "prev_hash"

    def test_matches_backend_audit_algorithm(self) -> None:
        """Reproduce the exact formula from :func:`backend.audit._hash`.

        This catches drift — if someone changes the canonical-json or
        the ``ts`` rounding on one side, the two stop agreeing and
        migrations stop being verifiable.
        """
        # Match audit.log: curr = sha256(prev + canonical_json(payload) + str(round(ts, 6)))
        ts = 1_700_000_000.123456
        payload = {
            "action":      "mode_change",
            "entity_kind": "operation_mode",
            "entity_id":   "global",
            "before":      {"mode": "supervised"},
            "after":       {"mode": "full_auto"},
            "actor":       "system",
        }
        expected = hashlib.sha256(
            ("" + mig.canonical_json(payload) + str(round(ts, 6))).encode()
        ).hexdigest()

        row = {
            "id": 1, "ts": ts, "actor": "system",
            "action": "mode_change", "entity_kind": "operation_mode",
            "entity_id": "global",
            "before_json": json.dumps({"mode": "supervised"}),
            "after_json": json.dumps({"mode": "full_auto"}),
            "prev_hash": "", "curr_hash": expected,
            "tenant_id": "t-default",
        }
        ok, _ = mig.verify_hash_chain_in_rows([row])
        assert ok, "script hash formula drifted from backend.audit._hash"

    def test_non_ascii_in_payload_is_preserved(self) -> None:
        rows = []
        prev = ""
        r = _make_row(
            1, prev, action="設定", entity_kind="任務",
            entity_id="x1", actor="使用者",
            before={"v": "舊"}, after={"v": "新"},
            ts=1.0,
        )
        rows.append(r)
        ok, _ = mig.verify_hash_chain_in_rows(rows)
        assert ok

    def test_per_tenant_genesis_empty_prev_hash(self) -> None:
        # Each tenant has its own chain; first row always has prev_hash "".
        tenants = {"t-a": [], "t-b": []}
        for tid in tenants:
            prev = ""
            for i in range(1, 4):
                r = _make_row(i, prev, tenant_id=tid, ts=1.0 + i)
                tenants[tid].append(r)
                prev = r["curr_hash"]
        for tid, rows in tenants.items():
            assert rows[0]["prev_hash"] == ""
            ok, _ = mig.verify_hash_chain_in_rows(rows)
            assert ok, f"tenant {tid} chain broken"

    def test_canonical_json_is_sorted(self) -> None:
        a = mig.canonical_json({"b": 1, "a": 2})
        b = mig.canonical_json({"a": 2, "b": 1})
        assert a == b

    def test_canonical_json_no_whitespace(self) -> None:
        out = mig.canonical_json({"a": 1, "b": [1, 2]})
        assert " " not in out

    def test_recompute_hash_deterministic(self) -> None:
        r = _make_row(1, "", ts=1.5)
        h1 = mig.recompute_hash("", r)
        h2 = mig.recompute_hash("", r)
        assert h1 == h2 == r["curr_hash"]


# ---------------------------------------------------------------------------
# (3) SQL builders.
# ---------------------------------------------------------------------------


class TestSqlBuilders:
    def test_insert_sql_basic(self) -> None:
        s = mig.build_insert_sql("audit_log", ["id", "ts", "actor"])
        assert s == (
            'INSERT INTO "audit_log" ("id", "ts", "actor") '
            "VALUES ($1, $2, $3)"
        )

    def test_insert_sql_quotes_identifiers(self) -> None:
        # "user" is a reserved word in PG — unquoted would error.
        s = mig.build_insert_sql("user", ["id", "email"])
        assert '"user"' in s
        assert '"email"' in s

    def test_insert_sql_with_on_conflict(self) -> None:
        s = mig.build_insert_sql(
            "tenants", ["id", "name"],
            on_conflict="ON CONFLICT (id) DO NOTHING",
        )
        assert s.endswith("ON CONFLICT (id) DO NOTHING")

    def test_insert_sql_rejects_empty_columns(self) -> None:
        with pytest.raises(ValueError):
            mig.build_insert_sql("audit_log", [])

    def test_insert_sql_placeholders_match_column_count(self) -> None:
        cols = ["a", "b", "c", "d", "e"]
        s = mig.build_insert_sql("t", cols)
        assert "$5" in s
        assert "$6" not in s

    def test_sequence_reset_sql_uses_pg_get_serial_sequence(self) -> None:
        s = mig.build_sequence_reset_sql("audit_log")
        assert "pg_get_serial_sequence" in s
        assert '"audit_log"' in s
        assert '"id"' in s

    def test_sequence_reset_sql_handles_empty_table(self) -> None:
        # The COALESCE + is-not-null boolean is what makes setval safe
        # for an empty table (setval with is_called=false leaves the
        # sequence's next call returning the default).
        s = mig.build_sequence_reset_sql("audit_log")
        assert "COALESCE" in s
        assert "IS NOT NULL" in s


# ---------------------------------------------------------------------------
# (4) CLI argument parsing.
# ---------------------------------------------------------------------------


class TestCli:
    def test_source_accepts_bare_path(self) -> None:
        url = mig.parse_source_arg("/tmp/foo.db")
        assert url.is_sqlite
        assert url.database == "/tmp/foo.db"

    def test_source_accepts_sqlite_url(self) -> None:
        url = mig.parse_source_arg("sqlite:///tmp/foo.db")
        assert url.is_sqlite

    def test_source_rejects_postgres_url(self) -> None:
        with pytest.raises(ValueError):
            mig.parse_source_arg("postgresql+asyncpg://h/db")

    def test_source_falls_back_to_env(self) -> None:
        url = mig.parse_source_arg(
            "", env={"OMNISIGHT_DATABASE_PATH": "/opt/x.db"},
        )
        assert url.database == "/opt/x.db"

    def test_source_empty_without_env_raises(self) -> None:
        with pytest.raises(ValueError):
            mig.parse_source_arg("", env={})

    def test_target_requires_asyncpg(self) -> None:
        # Plain `postgresql://` parses as psycopg2 — reject for runtime.
        with pytest.raises(ValueError):
            mig.parse_target_arg("postgresql://h/db")

    def test_target_accepts_asyncpg(self) -> None:
        url = mig.parse_target_arg("postgresql+asyncpg://u:p@h:5432/db")
        assert url.is_postgres and url.driver == "asyncpg"

    def test_target_rejects_sqlite(self) -> None:
        with pytest.raises(ValueError):
            mig.parse_target_arg("sqlite:///x.db")

    def test_target_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            mig.parse_target_arg("")

    def test_argparser_has_all_flags(self) -> None:
        p = mig.build_argparser()
        ns = p.parse_args([
            "--source", "foo.db",
            "--target", "postgresql+asyncpg://h/db",
            "--batch-size", "100",
            "--tables", "audit_log,tenants",
            "--truncate-target",
            "--skip-chain-verify",
            "--dry-run",
            "--json",
            "--quiet",
        ])
        assert ns.source == "foo.db"
        assert ns.target == "postgresql+asyncpg://h/db"
        assert ns.batch_size == 100
        assert ns.tables == "audit_log,tenants"
        assert ns.truncate_target is True
        assert ns.skip_chain_verify is True
        assert ns.dry_run is True
        assert ns.json is True
        assert ns.quiet is True

    def test_main_exit2_on_missing_source(self) -> None:
        # env cleared → no source discoverable → exit 2
        env = os.environ.copy()
        env.pop("OMNISIGHT_DATABASE_PATH", None)
        r = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True, text=True, timeout=30, env=env,
        )
        assert r.returncode == 2, f"stdout={r.stdout} stderr={r.stderr}"

    def test_main_exit2_on_bad_url(self) -> None:
        r = subprocess.run(
            [sys.executable, str(SCRIPT_PATH),
             "--source", "mysql://bad",
             "--target", "postgresql+asyncpg://h/db"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 2

    def test_main_exit2_when_target_missing_without_dry_run(self) -> None:
        # Provide a real source file but no target → exit 2.
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            src = f.name
        try:
            r = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--source", src],
                capture_output=True, text=True, timeout=30,
            )
            assert r.returncode == 2
        finally:
            os.unlink(src)


# ---------------------------------------------------------------------------
# (5) End-to-end dry-run against a real SQLite DB.
# ---------------------------------------------------------------------------


def _create_audit_fixture_db(path: Path, *, break_chain: bool = False) -> None:
    """Build a tiny SQLite DB with an audit_log table + a valid chain.

    If ``break_chain`` is True, tamper with one row so the pre-flight
    verifier will trip.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            actor       TEXT NOT NULL DEFAULT 'system',
            action      TEXT NOT NULL,
            entity_kind TEXT NOT NULL,
            entity_id   TEXT,
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json  TEXT NOT NULL DEFAULT '{}',
            prev_hash   TEXT NOT NULL DEFAULT '',
            curr_hash   TEXT NOT NULL,
            tenant_id   TEXT NOT NULL DEFAULT 't-default'
        );
        CREATE TABLE tenants (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free'
        );
        """
    )
    # Seed tenants table
    conn.execute(
        "INSERT INTO tenants (id, name, plan) VALUES (?, ?, ?)",
        ("t-default", "Default Tenant", "free"),
    )
    # Build a valid 5-row chain
    prev = ""
    for i in range(1, 6):
        ts = 1_700_000_000.0 + i
        payload = {
            "action": f"a_{i}",
            "entity_kind": "thing",
            "entity_id": f"x{i}",
            "before": {},
            "after": {"v": i},
            "actor": "system",
        }
        canon = mig.canonical_json(payload) + str(round(ts, 6))
        curr = hashlib.sha256((prev + canon).encode()).hexdigest()
        conn.execute(
            "INSERT INTO audit_log (id, ts, actor, action, entity_kind, "
            "entity_id, before_json, after_json, prev_hash, curr_hash, "
            "tenant_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (i, ts, "system", f"a_{i}", "thing", f"x{i}",
             "{}", json.dumps({"v": i}), prev, curr, "t-default"),
        )
        prev = curr
    if break_chain:
        conn.execute(
            "UPDATE audit_log SET after_json='{\"forged\":true}' WHERE id=3"
        )
    conn.commit()
    conn.close()


class TestDryRunE2E:
    def test_dry_run_against_valid_chain_reports_ok(self, tmp_path: Path) -> None:
        db = tmp_path / "fixture.db"
        _create_audit_fixture_db(db)
        r = subprocess.run(
            [sys.executable, str(SCRIPT_PATH),
             "--source", str(db),
             "--dry-run", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, f"stderr={r.stderr}"
        # Last line should be the JSON report
        last = [line for line in r.stdout.strip().splitlines() if line.startswith("{")][-1]
        report = json.loads(last)
        assert report["source_chain_ok"] is True
        assert report["dry_run"] is True
        # All tables with rows appear and marked skipped (dry-run)
        tables = {t["table"]: t for t in report["tables"]}
        assert "audit_log" in tables
        assert tables["audit_log"]["source_rows"] == 5
        assert tables["audit_log"]["skipped"] is True

    def test_dry_run_detects_broken_source_chain(self, tmp_path: Path) -> None:
        db = tmp_path / "broken.db"
        _create_audit_fixture_db(db, break_chain=True)
        r = subprocess.run(
            [sys.executable, str(SCRIPT_PATH),
             "--source", str(db), "--dry-run", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 3
        last = [line for line in r.stdout.strip().splitlines() if line.startswith("{")][-1]
        report = json.loads(last)
        assert report["source_chain_ok"] is False
        assert "t-default" in report["source_chain_tenants"]
        assert report["source_chain_tenants"]["t-default"]["ok"] is False
        assert report["source_chain_tenants"]["t-default"]["first_bad_id"] == 3

    def test_dry_run_skips_chain_when_opted_out(self, tmp_path: Path) -> None:
        db = tmp_path / "broken.db"
        _create_audit_fixture_db(db, break_chain=True)
        r = subprocess.run(
            [sys.executable, str(SCRIPT_PATH),
             "--source", str(db), "--dry-run",
             "--skip-chain-verify", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, f"stderr={r.stderr}"

    def test_nonexistent_source_file_exits_1(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.db"
        r = subprocess.run(
            [sys.executable, str(SCRIPT_PATH),
             "--source", str(missing), "--dry-run"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 1
        assert "not found" in r.stderr.lower() or "no such" in r.stderr.lower()


# ---------------------------------------------------------------------------
# (6) Async orchestrator with mocked asyncpg.
# ---------------------------------------------------------------------------


class _FakePgConn:
    """Minimum asyncpg.Connection stand-in for the orchestrator.

    Records every SQL statement so tests can assert on the insert
    order / sequence-reset SQL without needing a real PG instance.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list[tuple]]] = []
        self.audit_rows_on_target: list[dict[str, Any]] = []
        self.tables_that_exist: set[str] = set()
        self.row_counts: dict[str, int] = {}

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "OK"

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.executemany_calls.append((sql, rows))
        # Emulate inserts into audit_log so the post-flight chain
        # verifier sees real rows.
        if '"audit_log"' in sql:
            cols = [c.strip('"') for c in
                    sql.split("(", 1)[1].split(")", 1)[0].split(", ")]
            for tup in rows:
                self.audit_rows_on_target.append(dict(zip(cols, tup)))

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        # information_schema.tables existence check
        if "information_schema.tables" in sql:
            tname = args[0]
            return {"": 1} if tname in self.tables_that_exist else None
        # COUNT(*) gate
        if "SELECT COUNT(*)" in sql:
            # Extract table name from FROM clause
            tname = sql.split('FROM "', 1)[1].split('"', 1)[0]
            return {"n": self.row_counts.get(tname, 0)}
        # setval row
        if "pg_get_serial_sequence" in sql:
            tname = sql.split("FROM \"", 1)[1].split("\"", 1)[0]
            if tname == "audit_log" and self.audit_rows_on_target:
                val = max(r["id"] for r in self.audit_rows_on_target)
                return {"setval": val}
            return {"setval": 1}
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "SELECT DISTINCT tenant_id FROM audit_log" in sql:
            tids = sorted({r.get("tenant_id", "t-default")
                           for r in self.audit_rows_on_target})
            return [{"tenant_id": t} for t in tids]
        if "FROM audit_log WHERE tenant_id" in sql:
            tid = args[0]
            rows = [r for r in self.audit_rows_on_target
                    if r.get("tenant_id", "t-default") == tid]
            return sorted(rows, key=lambda r: r["id"])
        return []

    async def close(self) -> None:  # noqa: D401 - stub
        pass


async def _run_with_fake_pg(args: Any, fake: _FakePgConn) -> mig.MigrationReport:
    async def _open(_url: Any) -> _FakePgConn:
        return fake
    with mock.patch.object(mig, "_pg_open", _open):
        return await mig._run_migration(args)


class TestOrchestratorMocked:
    def _default_args(self, src: Path, target: str = "postgresql+asyncpg://u:p@h/db") -> Any:
        ns = mig.build_argparser().parse_args([
            "--source", str(src),
            "--target", target,
            "--batch-size", "10",
            "--truncate-target",
            "--quiet",
        ])
        return ns

    def test_orchestrator_fails_when_target_not_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "src.db"
        _create_audit_fixture_db(db)

        fake = _FakePgConn()
        # Present tables that matter
        fake.tables_that_exist = set(mig.TABLES_IN_ORDER)
        # audit_log already has rows on target → must abort
        fake.row_counts["audit_log"] = 42

        ns = mig.build_argparser().parse_args([
            "--source", str(db),
            "--target", "postgresql+asyncpg://u:p@h/db",
            "--batch-size", "10",
            "--quiet",
        ])
        report = asyncio.run(_run_with_fake_pg(ns, fake))
        assert report.exit_code == 4, "dirty target without --truncate-target must exit 4"

    def test_orchestrator_allows_seeded_tenants_row(self, tmp_path: Path) -> None:
        db = tmp_path / "src.db"
        _create_audit_fixture_db(db)

        fake = _FakePgConn()
        fake.tables_that_exist = set(mig.TABLES_IN_ORDER)
        # ``tenants`` with exactly 1 row = Alembic-seeded t-default;
        # must NOT trigger the exit-4 gate.
        fake.row_counts["tenants"] = 1
        fake.row_counts["audit_log"] = 0

        ns = mig.build_argparser().parse_args([
            "--source", str(db),
            "--target", "postgresql+asyncpg://u:p@h/db",
            "--batch-size", "10",
            "--quiet",
        ])
        # Emulate ON CONFLICT DO NOTHING for the tenants seed row: the
        # target already has 1 row (``t-default``) from Alembic, and
        # the source carries the same row — in prod asyncpg would skip.
        orig_executemany = fake.executemany

        async def tracking_executemany(sql: str, rows: list[tuple]) -> None:
            await orig_executemany(sql, rows)
            tname = sql.split('INSERT INTO "', 1)[1].split('"', 1)[0]
            if "ON CONFLICT" in sql and tname == "tenants":
                # Seed row stays at 1; our fixture inserts 1 row with
                # the same id — conflict path leaves the count at 1.
                fake.row_counts[tname] = max(1, fake.row_counts.get(tname, 0))
            else:
                fake.row_counts[tname] = fake.row_counts.get(tname, 0) + len(rows)
        fake.executemany = tracking_executemany  # type: ignore[method-assign]

        report = asyncio.run(_run_with_fake_pg(ns, fake))
        assert report.exit_code == 0, (
            f"expected exit 0, got {report.exit_code}; "
            f"tenants={report.target_chain_tenants}"
        )
        assert report.source_chain_ok is True
        assert report.target_chain_ok is True

    def test_orchestrator_preserves_audit_log_id_ordering(self, tmp_path: Path) -> None:
        db = tmp_path / "src.db"
        _create_audit_fixture_db(db)

        fake = _FakePgConn()
        fake.tables_that_exist = set(mig.TABLES_IN_ORDER)
        ns = self._default_args(db)

        orig_executemany = fake.executemany

        async def tracking_executemany(sql: str, rows: list[tuple]) -> None:
            await orig_executemany(sql, rows)
            tname = sql.split('INSERT INTO "', 1)[1].split('"', 1)[0]
            fake.row_counts[tname] = fake.row_counts.get(tname, 0) + len(rows)
        fake.executemany = tracking_executemany  # type: ignore[method-assign]

        report = asyncio.run(_run_with_fake_pg(ns, fake))
        assert report.exit_code == 0, f"{report.to_dict()}"
        # Verify inserted audit_log rows are in id ASC order
        ids = [r["id"] for r in fake.audit_rows_on_target]
        assert ids == sorted(ids), f"ids were reordered: {ids}"
        assert ids == [1, 2, 3, 4, 5]

    def test_orchestrator_resets_identity_sequences(self, tmp_path: Path) -> None:
        db = tmp_path / "src.db"
        _create_audit_fixture_db(db)

        fake = _FakePgConn()
        fake.tables_that_exist = set(mig.TABLES_IN_ORDER)
        ns = self._default_args(db)

        orig_executemany = fake.executemany

        async def tracking_executemany(sql: str, rows: list[tuple]) -> None:
            await orig_executemany(sql, rows)
            tname = sql.split('INSERT INTO "', 1)[1].split('"', 1)[0]
            fake.row_counts[tname] = fake.row_counts.get(tname, 0) + len(rows)
        fake.executemany = tracking_executemany  # type: ignore[method-assign]

        report = asyncio.run(_run_with_fake_pg(ns, fake))
        assert report.exit_code == 0
        # audit_log was among the identity tables, with 5 rows → max id 5
        assert "audit_log" in report.sequences_reset
        assert report.sequences_reset["audit_log"] == 5

    def test_orchestrator_aborts_on_broken_source_chain(self, tmp_path: Path) -> None:
        db = tmp_path / "broken.db"
        _create_audit_fixture_db(db, break_chain=True)

        fake = _FakePgConn()
        fake.tables_that_exist = set(mig.TABLES_IN_ORDER)
        ns = self._default_args(db)
        report = asyncio.run(_run_with_fake_pg(ns, fake))
        assert report.exit_code == 3
        # Crucially: no writes issued to the target.
        assert fake.executemany_calls == [], (
            "script must not write to target when source chain is broken"
        )

    def test_orchestrator_truncates_before_copy(self, tmp_path: Path) -> None:
        db = tmp_path / "src.db"
        _create_audit_fixture_db(db)

        fake = _FakePgConn()
        fake.tables_that_exist = set(mig.TABLES_IN_ORDER)
        # Dirty target — but --truncate-target is in default_args()
        fake.row_counts["audit_log"] = 99
        ns = self._default_args(db)

        orig_executemany = fake.executemany

        async def tracking_executemany(sql: str, rows: list[tuple]) -> None:
            await orig_executemany(sql, rows)
            tname = sql.split('INSERT INTO "', 1)[1].split('"', 1)[0]
            fake.row_counts[tname] = len(rows)
        fake.executemany = tracking_executemany  # type: ignore[method-assign]

        # After truncate, row_counts["audit_log"] gets overwritten by
        # the tracking_executemany to reflect actual inserts.
        report = asyncio.run(_run_with_fake_pg(ns, fake))
        assert report.exit_code == 0, f"{report.to_dict()}"
        assert report.truncated_target is True
        # First executed statement must be the TRUNCATE.
        assert any(
            "TRUNCATE TABLE" in stmt for stmt, _ in fake.executed
        ), "TRUNCATE was not issued"

    def test_orchestrator_table_filter_respected(self, tmp_path: Path) -> None:
        db = tmp_path / "src.db"
        _create_audit_fixture_db(db)
        fake = _FakePgConn()
        fake.tables_that_exist = {"audit_log", "tenants"}

        ns = mig.build_argparser().parse_args([
            "--source", str(db),
            "--target", "postgresql+asyncpg://u:p@h/db",
            "--tables", "audit_log,tenants",
            "--batch-size", "10",
            "--truncate-target",
            "--quiet",
        ])
        orig_executemany = fake.executemany

        async def tracking_executemany(sql: str, rows: list[tuple]) -> None:
            await orig_executemany(sql, rows)
            tname = sql.split('INSERT INTO "', 1)[1].split('"', 1)[0]
            fake.row_counts[tname] = fake.row_counts.get(tname, 0) + len(rows)
        fake.executemany = tracking_executemany  # type: ignore[method-assign]

        report = asyncio.run(_run_with_fake_pg(ns, fake))
        assert report.exit_code == 0
        inserted_tables = {
            sql.split('INSERT INTO "', 1)[1].split('"', 1)[0]
            for sql, _ in fake.executemany_calls
        }
        assert inserted_tables <= {"audit_log", "tenants"}

    def test_orchestrator_unknown_table_rejected(self, tmp_path: Path) -> None:
        db = tmp_path / "src.db"
        _create_audit_fixture_db(db)
        fake = _FakePgConn()

        ns = mig.build_argparser().parse_args([
            "--source", str(db),
            "--target", "postgresql+asyncpg://u:p@h/db",
            "--tables", "audit_log,not_a_real_table",
            "--quiet",
        ])
        with pytest.raises(ValueError):
            asyncio.run(_run_with_fake_pg(ns, fake))


# ---------------------------------------------------------------------------
# (7) MigrationReport dataclass shape.
# ---------------------------------------------------------------------------


class TestReportShape:
    def test_empty_report_serialises(self) -> None:
        r = mig.MigrationReport()
        d = r.to_dict()
        for key in (
            "source_url", "target_url", "dry_run", "truncated_target",
            "source_chain_ok", "source_chain_tenants", "target_chain_ok",
            "target_chain_tenants", "tables", "sequences_reset",
            "duration_seconds", "exit_code",
        ):
            assert key in d

    def test_table_result_fields(self) -> None:
        t = mig.TableResult(
            table="audit_log", source_rows=5, copied_rows=5,
            columns=["id", "ts"],
        )
        r = mig.MigrationReport(tables=[t])
        d = r.to_dict()
        assert d["tables"][0]["table"] == "audit_log"
        assert d["tables"][0]["copied_rows"] == 5
