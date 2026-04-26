"""BS.1.2 — alembic 0052 catalog_seed migration contract.

Locks the load-bearing properties of the BS.1.2 first-batch seed:

1.  **Structural** — revision id / down_revision wire onto 0051; the
    ``_SEED_ENTRIES`` constant is a 30-tuple split 6/8/4/5/3/4 by
    family, every entry is ``shipped``-shaped (no tenant_id, no
    metadata.source override).

2.  **Functional (SQLite)** — bring 0051 then 0052 up against an
    in-memory SQLite, count rows by family, spot-check a couple of
    high-touch entries (NXP MCUXpresso / Zephyr / Arm GNU toolchain),
    and verify that JSON columns round-trip as parseable JSON.

3.  **Idempotency** — running ``upgrade()`` twice produces the same
    row set (the second pass falls through ``INSERT OR IGNORE``).

4.  **Symmetry** — ``upgrade()`` then ``downgrade()`` removes exactly
    the seeded rows; admin-touched rows (operator hand-edited a
    shipped row to ``hidden=true``, or wrote a ``source='operator'``
    sibling) are preserved.

5.  **Yaml mirror sanity** — moved out to
    ``backend/tests/test_catalog_schema.py`` (BS.1.5).  See the
    docstring there: the drift guard runs schema validation +
    per-field equality between yaml and alembic seed.

Pre-existing chain issue
────────────────────────

A pre-0017 SQLite issue (``CREATE INDEX`` on the not-yet-added
``episodic_memory.last_used_at``) blocks ``alembic upgrade head`` on
vanilla SQLite mid-chain.  We bring 0051 + 0052 up directly (without
the rest of the chain) by hand-creating the bare-minimum tables 0051
references — same approach as
``test_alembic_0051_catalog_tables.TestSqliteUpgradeCreatesTables``.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0051 = (
    BACKEND_ROOT / "alembic" / "versions" / "0051_catalog_tables.py"
)
MIGRATION_0052 = (
    BACKEND_ROOT / "alembic" / "versions" / "0052_catalog_seed.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def m0051():
    return _load_module(MIGRATION_0051, "_alembic_test_0051_for_0052")


@pytest.fixture(scope="module")
def m0052():
    return _load_module(MIGRATION_0052, "_alembic_test_0052")


# ─── Group 1: structural guards ───────────────────────────────────────────


class TestMigrationFileStructure:
    @pytest.fixture(scope="class")
    def source(self) -> str:
        return MIGRATION_0052.read_text()

    def test_revision_id_is_0052(self, source: str) -> None:
        assert 'revision = "0052"' in source

    def test_down_revision_is_0051(self, source: str) -> None:
        assert 'down_revision = "0051"' in source

    def test_uses_insert_or_ignore(self, source: str) -> None:
        assert "INSERT OR IGNORE INTO catalog_entries" in source

    def test_dialect_branch_for_jsonb(self, source: str) -> None:
        # PG path emits ``::jsonb`` cast; SQLite path emits TEXT-of-JSON.
        assert "::jsonb" in source
        assert 'dialect == "postgresql"' in source

    def test_seed_entries_counts_by_family(self, m0052) -> None:
        entries = m0052.SEED_ENTRIES
        assert len(entries) == 30
        by_family: dict[str, int] = {}
        for e in entries:
            by_family[e["family"]] = by_family.get(e["family"], 0) + 1
        assert by_family == {
            "mobile": 6,
            "embedded": 8,
            "web": 4,
            "software": 5,
            "rtos": 3,
            "cross-toolchain": 4,
        }

    def test_seed_install_methods_within_enum(self, m0052) -> None:
        valid = {"noop", "docker_pull", "shell_script", "vendor_installer"}
        for entry in m0052.SEED_ENTRIES:
            assert entry["install_method"] in valid, entry["id"]

    def test_seed_ids_are_unique(self, m0052) -> None:
        ids = [e["id"] for e in m0052.SEED_ENTRIES]
        assert len(set(ids)) == len(ids)

    def test_seed_required_fields_present(self, m0052) -> None:
        required = {
            "id",
            "vendor",
            "family",
            "display_name",
            "version",
            "install_method",
        }
        for entry in m0052.SEED_ENTRIES:
            missing = required - set(entry.keys())
            assert not missing, f"{entry.get('id')}: missing {missing}"

    def test_seed_no_tenant_id(self, m0052) -> None:
        for entry in m0052.SEED_ENTRIES:
            assert "tenant_id" not in entry, entry["id"]

    def test_seed_no_explicit_source_override(self, m0052) -> None:
        # The migration hard-codes source='shipped' in _build_insert.
        for entry in m0052.SEED_ENTRIES:
            assert "source" not in entry, entry["id"]

    def test_depends_on_references_are_in_seed(self, m0052) -> None:
        ids = {e["id"] for e in m0052.SEED_ENTRIES}
        for entry in m0052.SEED_ENTRIES:
            for dep in entry.get("depends_on", []):
                assert dep in ids, (
                    f"{entry['id']} depends_on {dep!r} which is not in seed"
                )


# ─── Group 2: functional SQLite upgrade ───────────────────────────────────


def _bootstrap_minimal_schema(conn: sqlite3.Connection) -> None:
    """Create just enough chain prefix for 0051's FK targets."""
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
    """Mimics enough of an alembic context bind for ``conn.exec_driver_sql``."""

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
def upgraded_db(monkeypatch, m0051, m0052) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    _bootstrap_minimal_schema(conn)
    _bind(monkeypatch, conn)
    m0051.upgrade()
    m0052.upgrade()
    return conn


class TestSqliteUpgradeSeedsAllRows:
    def test_total_row_count(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT COUNT(*) FROM catalog_entries WHERE source='shipped'"
        )
        assert cur.fetchone()[0] == 30

    def test_count_by_family_matches_split(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT family, COUNT(*) "
            "FROM catalog_entries "
            "WHERE source='shipped' "
            "GROUP BY family "
            "ORDER BY family"
        )
        rows = dict(cur.fetchall())
        assert rows == {
            "cross-toolchain": 4,
            "embedded": 8,
            "mobile": 6,
            "rtos": 3,
            "software": 5,
            "web": 4,
        }

    def test_all_rows_have_tenant_id_null(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT COUNT(*) FROM catalog_entries "
            "WHERE source='shipped' AND tenant_id IS NOT NULL"
        )
        assert cur.fetchone()[0] == 0

    def test_all_rows_have_schema_version_1(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT DISTINCT schema_version FROM catalog_entries "
            "WHERE source='shipped'"
        )
        assert cur.fetchall() == [(1,)]

    def test_all_rows_have_hidden_zero(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT DISTINCT hidden FROM catalog_entries "
            "WHERE source='shipped'"
        )
        assert cur.fetchall() == [(0,)]

    def test_spotcheck_nxp_mcuxpresso(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT vendor, family, install_method, depends_on "
            "FROM catalog_entries WHERE id='nxp-mcuxpresso-imxrt1170'"
        )
        row = cur.fetchone()
        assert row is not None
        vendor, family, method, depends_on = row
        assert vendor == "nxp"
        assert family == "embedded"
        assert method == "vendor_installer"
        assert json.loads(depends_on) == ["arm-gnu-toolchain-13"]

    def test_spotcheck_zephyr_metadata_round_trip(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT metadata FROM catalog_entries WHERE id='zephyr-rtos-3-7'"
        )
        raw = cur.fetchone()[0]
        meta = json.loads(raw)
        assert meta["branch"] == "v3.7-branch"
        assert meta["west_init_required"] is True

    def test_spotcheck_arm_gnu_toolchain(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT family, install_method, size_bytes "
            "FROM catalog_entries WHERE id='arm-gnu-toolchain-13'"
        )
        row = cur.fetchone()
        assert row is not None
        family, method, size_bytes = row
        assert family == "cross-toolchain"
        assert method == "vendor_installer"
        assert size_bytes == 322961408

    def test_install_methods_diversified(self, upgraded_db) -> None:
        cur = upgraded_db.execute(
            "SELECT DISTINCT install_method FROM catalog_entries "
            "WHERE source='shipped'"
        )
        methods = {row[0] for row in cur.fetchall()}
        # We seed all four install_method values at least once.
        assert methods == {
            "noop",
            "docker_pull",
            "shell_script",
            "vendor_installer",
        }


# ─── Group 3: idempotency ─────────────────────────────────────────────────


class TestIdempotentReupgrade:
    def test_running_upgrade_twice_no_dup(
        self, monkeypatch, m0051, m0052
    ) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        _bootstrap_minimal_schema(conn)
        _bind(monkeypatch, conn)
        m0051.upgrade()
        m0052.upgrade()
        first = conn.execute(
            "SELECT COUNT(*) FROM catalog_entries"
        ).fetchone()[0]
        m0052.upgrade()
        second = conn.execute(
            "SELECT COUNT(*) FROM catalog_entries"
        ).fetchone()[0]
        assert first == second == 30


# ─── Group 4: downgrade symmetry ─────────────────────────────────────────


class TestDowngradeRemovesShippedSeed:
    def test_downgrade_clears_shipped_rows(
        self, monkeypatch, m0051, m0052
    ) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        _bootstrap_minimal_schema(conn)
        _bind(monkeypatch, conn)
        m0051.upgrade()
        m0052.upgrade()
        m0052.downgrade()
        cur = conn.execute(
            "SELECT COUNT(*) FROM catalog_entries WHERE source='shipped'"
        )
        assert cur.fetchone()[0] == 0

    def test_downgrade_preserves_operator_rows(
        self, monkeypatch, m0051, m0052
    ) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        _bootstrap_minimal_schema(conn)
        # Operator-side state: a tenant + a hand-written 'operator' row
        # that re-uses one of the seed ids.
        conn.execute("INSERT INTO tenants (id) VALUES ('t-acme')")
        _bind(monkeypatch, conn)
        m0051.upgrade()
        m0052.upgrade()
        conn.execute(
            "INSERT INTO catalog_entries "
            "(id, source, tenant_id, vendor, family, display_name, "
            "version, install_method, depends_on, metadata) "
            "VALUES ('nxp-mcuxpresso-imxrt1170', 'operator', 't-acme', "
            "'nxp', 'embedded', 'NXP MCUXpresso (ACME)', '11.10.0-acme', "
            "'vendor_installer', '[]', '{}')"
        )
        m0052.downgrade()
        cur = conn.execute(
            "SELECT source FROM catalog_entries "
            "WHERE id='nxp-mcuxpresso-imxrt1170'"
        )
        rows = cur.fetchall()
        # The shipped row was removed; the operator row stays.
        assert rows == [("operator",)]


# ─── Group 5: yaml mirror sanity ──────────────────────────────────────────
# Moved to backend/tests/test_catalog_schema.py (BS.1.5).  The drift guard
# there does per-field equality between every yaml entry and every
# alembic 0052 _SEED_ENTRIES row, plus schema-validation against
# configs/embedded_catalog/_schema.yaml — superset of what this group
# previously asserted.  Tests deleted (not skipped) because keeping
# them here would require duplicated _schema.yaml-exclusion logic and
# would mask drift if BS.1.5 contracts loosen.


# ─── Group 6: PG dialect branch executes ──────────────────────────────────


class TestPgBranchExecutes:
    def test_pg_branch_emits_jsonb_cast(self, monkeypatch, m0052) -> None:
        """The PG branch should issue at least one INSERT containing ``::jsonb``.

        We don't try to reproduce a PG semantic check via mock — only that
        the dialect branch is taken and the JSON literal is emitted with
        the cast suffix the PG side needs.
        """
        from alembic import op as alembic_op

        captured: list[str] = []

        class _PgBind:
            class _Dialect:
                name = "postgresql"

            dialect = _Dialect()

            def exec_driver_sql(self, sql, *a, **k):
                captured.append(sql)

        monkeypatch.setattr(alembic_op, "get_bind", lambda: _PgBind())
        m0052.upgrade()
        assert len(captured) == 30
        joined = "\n".join(captured)
        assert "::jsonb" in joined
        assert "INSERT OR IGNORE INTO catalog_entries" in joined
        # Sanity — every INSERT ends up referencing source='shipped'.
        for sql in captured:
            assert "'shipped'" in sql
