"""OP-2166 — alembic 0251 catalog_seed_rtsp_onvif_server contract.

Mirrors the structure of ``test_alembic_0052_catalog_seed.py`` but
scoped to the single follow-on entry this migration inserts. Pins:

1.  **Structural** — revision id wires onto 0250 (the current head as
    of OP-2166 pickup); the ``SEED_ENTRIES`` constant is a 1-tuple with
    the ``rtsp-onvif-server`` row shaped to mirror the
    ``beaglebone-debian-image`` precedent (``install_method='noop'``
    with ``metadata.manual_step=true``).

2.  **Functional (SQLite)** — bring 0051 + 0052 + 0251 up against an
    in-memory SQLite (skipping the noisy 0017 chain prefix the way the
    BS.1.2 0052 test does), assert the new row is present at
    ``source='shipped'`` and that JSON columns round-trip.

3.  **Idempotency** — re-running 0251.upgrade() leaves the row count
    unchanged (``INSERT OR IGNORE`` path).

4.  **Symmetry** — 0251.downgrade() removes only this migration's row;
    rows from 0052 are untouched.

Pre-existing chain issue
------------------------
A pre-0017 SQLite issue blocks ``alembic upgrade head`` on vanilla
SQLite mid-chain; we therefore hand-bootstrap only the bare-minimum
tables 0051's FK targets need (mirroring
``test_alembic_0052_catalog_seed.py``'s approach) and step 0051 →
0052 → 0251 directly.

Module-global state audit
-------------------------
Pure DML migration; no in-memory cache, no module-level singleton.
Every test fixture re-derives state from a fresh in-memory SQLite
connection.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0051 = (
    BACKEND_ROOT / "alembic" / "versions" / "0051_catalog_tables.py"
)
MIGRATION_0052 = (
    BACKEND_ROOT / "alembic" / "versions" / "0052_catalog_seed.py"
)
MIGRATION_0251 = (
    BACKEND_ROOT / "alembic" / "versions"
    / "0251_catalog_seed_rtsp_onvif_server.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0051():
    return _load_module(MIGRATION_0051, "_alembic_test_0051_for_0251")


@pytest.fixture(scope="module")
def m0052():
    return _load_module(MIGRATION_0052, "_alembic_test_0052_for_0251")


@pytest.fixture(scope="module")
def m0251():
    return _load_module(MIGRATION_0251, "_alembic_test_0251")


# ─── Group 1: structural guards ───────────────────────────────────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0251.read_text()

    def test_revision_id_is_0251(self, source: str) -> None:
        assert 'revision = "0251"' in source

    def test_down_revision_is_0250(self, source: str) -> None:
        assert 'down_revision = "0250"' in source

    def test_uses_insert_or_ignore(self, source: str) -> None:
        assert "INSERT OR IGNORE INTO catalog_entries" in source

    def test_dialect_branch_for_jsonb(self, source: str) -> None:
        # PG path emits ``::jsonb`` cast; SQLite path emits TEXT-of-JSON.
        # Same pattern as 0052 so the alembic_pg_compat shim's PG
        # translation lights up uniformly.
        assert "::jsonb" in source
        assert 'dialect == "postgresql"' in source

    def test_seed_entries_singleton(self, m0251) -> None:
        assert len(m0251.SEED_ENTRIES) == 1

    def test_seed_entry_is_rtsp_onvif_server(self, m0251) -> None:
        entry = m0251.SEED_ENTRIES[0]
        assert entry["id"] == "rtsp-onvif-server"
        assert entry["family"] == "software"
        assert entry["install_method"] == "noop"
        assert entry["vendor"] == "omnisight"

    def test_seed_entry_manual_step_metadata(self, m0251) -> None:
        # Beaglebone precedent: noop installs always carry
        # ``manual_step: True`` so the operator UI surfaces the
        # off-band install workflow.
        entry = m0251.SEED_ENTRIES[0]
        assert entry["metadata"]["manual_step"] is True

    def test_seed_entry_no_tenant_id(self, m0251) -> None:
        for entry in m0251.SEED_ENTRIES:
            assert "tenant_id" not in entry, entry["id"]

    def test_seed_entry_no_explicit_source_override(self, m0251) -> None:
        for entry in m0251.SEED_ENTRIES:
            assert "source" not in entry, entry["id"]


# ─── Group 2: functional SQLite upgrade ───────────────────────────────────


def _bootstrap_minimal_schema(conn: sqlite3.Connection) -> None:
    """Bare-minimum FK targets for 0051 (mirrors the 0052 test)."""
    conn.executescript(
        """
        CREATE TABLE tenants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            tenant_id TEXT REFERENCES tenants(id),
            role TEXT NOT NULL DEFAULT 'user'
        );
        """
    )


class _StubBind:
    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

        class _Dialect:
            name = "sqlite"

        self.dialect = _Dialect()

    def exec_driver_sql(self, sql: str, *args, **kwargs):
        return self._raw.execute(sql)


def _bind(monkeypatch, conn: sqlite3.Connection) -> None:
    from alembic import op as alembic_op

    bind = _StubBind(conn)
    monkeypatch.setattr(alembic_op, "get_bind", lambda: bind)
    monkeypatch.setattr(alembic_op, "execute", lambda s: conn.execute(s))


@pytest.fixture()
def upgraded_db(
    monkeypatch, m0051, m0052, m0251
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    _bootstrap_minimal_schema(conn)
    _bind(monkeypatch, conn)
    m0051.upgrade()
    m0052.upgrade()
    m0251.upgrade()
    return conn


class TestSqliteUpgradeSeedsRow:
    def test_row_count_after_0251(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT COUNT(*) FROM catalog_entries WHERE source='shipped'"
        )
        # 34 from 0052 (the BS.1.2 first-batch 30 plus the four OP-1918
        # Phase 0 cross-toolchain rows that grew the migration after
        # merge) + 1 from 0251.
        assert cur.fetchone()[0] == 35

    def test_software_family_grew_by_one(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT COUNT(*) FROM catalog_entries "
            "WHERE source='shipped' AND family='software'"
        )
        # 5 from 0052 (python-uv, rust-stable, go-1-22, docker-engine,
        # git-lfs) + 1 from 0251 (rtsp-onvif-server) = 6.
        assert cur.fetchone()[0] == 6

    def test_row_present_and_shape(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT vendor, family, install_method, install_url, "
            "       depends_on, metadata, tenant_id, hidden "
            "FROM catalog_entries WHERE id='rtsp-onvif-server'"
        )
        row = cur.fetchone()
        assert row is not None
        vendor, family, method, url, depends_on, metadata, tenant, hidden = row
        assert vendor == "omnisight"
        assert family == "software"
        assert method == "noop"
        assert url == "https://gitlab.com/omnisight/rtsp-onvif-server"
        # ``depends_on`` is empty for the prebuilt server.
        assert json.loads(depends_on) == []
        meta = json.loads(metadata)
        assert meta["manual_step"] is True
        assert meta["repo"] == "omnisight/rtsp-onvif-server"
        assert meta["integrates_via"] == "ipcam_prebuilt_server_integration"
        # Shipped rows are tenant-scopeless and visible.
        assert tenant is None
        assert hidden == 0


# ─── Group 3: idempotency ─────────────────────────────────────────────────


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup(
        self, monkeypatch, m0051, m0052, m0251
    ) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        _bootstrap_minimal_schema(conn)
        _bind(monkeypatch, conn)
        m0051.upgrade()
        m0052.upgrade()
        m0251.upgrade()
        first = conn.execute(
            "SELECT COUNT(*) FROM catalog_entries"
        ).fetchone()[0]
        m0251.upgrade()
        second = conn.execute(
            "SELECT COUNT(*) FROM catalog_entries"
        ).fetchone()[0]
        # 34 from 0052 + 1 from 0251 = 35; idempotent re-upgrade leaves
        # it unchanged.
        assert first == second == 35


# ─── Group 4: downgrade symmetry ──────────────────────────────────────────


class TestDowngradeRemovesOnly0251Row:
    def test_downgrade_clears_only_0251_seed(
        self, monkeypatch, m0051, m0052, m0251
    ) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        _bootstrap_minimal_schema(conn)
        _bind(monkeypatch, conn)
        m0051.upgrade()
        m0052.upgrade()
        m0251.upgrade()
        m0251.downgrade()
        # The 0251 row is gone; 0052's 34 rows remain.
        cur = conn.execute(
            "SELECT COUNT(*) FROM catalog_entries WHERE source='shipped'"
        )
        assert cur.fetchone()[0] == 34
        cur = conn.execute(
            "SELECT COUNT(*) FROM catalog_entries WHERE id='rtsp-onvif-server'"
        )
        assert cur.fetchone()[0] == 0


# ─── Group 5: PG dialect branch executes ──────────────────────────────────


class TestPgBranchExecutes:
    def test_pg_branch_emits_jsonb_cast(self, monkeypatch, m0251) -> None:
        """The PG branch should issue an INSERT containing ``::jsonb``.

        Same shape as 0052's TestPgBranchExecutes: bind stub carries
        ``dialect.name == 'postgresql'`` so the JSONB branch fires;
        ``op.execute`` is monkeypatched to capture the rendered SQL.
        """
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        monkeypatch.setattr(
            alembic_op, "execute", lambda sql: captured.append(sql)
        )
        m0251.upgrade()
        assert len(captured) == 1
        sql = captured[0]
        assert "::jsonb" in sql
        assert "INSERT OR IGNORE INTO catalog_entries" in sql
        assert "'shipped'" in sql
        assert "'rtsp-onvif-server'" in sql
