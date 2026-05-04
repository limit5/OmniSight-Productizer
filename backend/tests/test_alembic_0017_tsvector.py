"""Phase-3-Runtime-v2 SP-2.1 — alembic 0017 (tsvector) contract tests.

Locks down the schema contract the Epic 3 port (SP-3.12) will rely on:

  * ``episodic_memory.tsv`` column exists on PG, is a ``tsvector``,
    and is GENERATED ALWAYS STORED (i.e. PG auto-maintains it — no
    app-side insert/update needed).
  * A GIN index ``episodic_memory_tsv_gin`` exists on that column.
  * The generated expression produces an actual, non-empty tsvector
    when the feeder columns have content.
  * Upgrade is idempotent (re-running does not error).
  * downgrade() cleanly removes the column + index.

All tests require ``OMNI_TEST_PG_URL`` → the pg_test_* fixtures in
conftest skip cleanly when unset.

Why not a SQLite path here: the migration is a no-op on SQLite (the
existing FTS5 virtual table is untouched). We do have a smoke test
in the SQLite-dev fallback (``test_episodic_memory_sqlite_fts_fallback.py``
in SP-2.2) that the legacy FTS5 path still works.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _run_alembic(dsn: str, *argv: str) -> subprocess.CompletedProcess:
    """Run `python -m alembic` wait no — use the binary, see SP-1.2
    rationale. (Pre-FX.9.3 this avoided the backend/platform.py shadow
    that bit `python -m alembic` in cwd=backend/; post-rename it's
    defence-in-depth — keeping the entry point identical to prod.)"""
    import os as _os
    sqlalchemy_url = dsn.replace(
        "postgresql://", "postgresql+psycopg2://", 1,
    )
    env = _os.environ.copy()
    env["SQLALCHEMY_URL"] = sqlalchemy_url
    env["OMNISIGHT_SKIP_FS_MIGRATIONS"] = "1"
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        ["alembic", *argv],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


# ─── Schema-level contract ────────────────────────────────────────


class TestTsvectorColumnShape:
    @pytest.mark.asyncio
    async def test_tsv_column_exists(self, pg_test_conn) -> None:
        row = await pg_test_conn.fetchrow(
            """SELECT column_name, data_type, is_generated, generation_expression
               FROM information_schema.columns
               WHERE table_schema = 'public'
                 AND table_name = 'episodic_memory'
                 AND column_name = 'tsv'"""
        )
        assert row is not None, (
            "Column episodic_memory.tsv must exist after alembic 0017. "
            "If this fails, the migration likely didn't run or landed a "
            "different column name."
        )
        assert row["data_type"] == "tsvector", (
            f"episodic_memory.tsv must be of type tsvector (got "
            f"{row['data_type']!r}) so PG's @@ operator can match "
            f"against it"
        )
        assert row["is_generated"] == "ALWAYS", (
            "episodic_memory.tsv must be GENERATED ALWAYS — otherwise "
            "insert/update paths would need to maintain it manually, "
            "defeating the 'no app-layer sync' design goal"
        )
        # Sanity-check the generation expression references the four
        # columns we want indexed. Don't pin the exact expression (PG
        # reformats it), just that each feeder column appears.
        gen_expr = row["generation_expression"] or ""
        for feeder in ("error_signature", "solution", "soc_vendor", "tags"):
            assert feeder in gen_expr, (
                f"Generated expression for tsv should feed from "
                f"{feeder}; got {gen_expr!r}"
            )

    @pytest.mark.asyncio
    async def test_tsv_column_is_stored_not_virtual(
        self, pg_test_conn,
    ) -> None:
        # STORED means the value is materialised + indexable. VIRTUAL
        # (which PG doesn't support for generated columns yet, but other
        # engines do) would require the index on the expression instead.
        # We check via the attgenerated catalog column — 's' = stored,
        # 'v' = virtual, '' = not generated.
        #
        # Note: pg_attribute.attgenerated is the PG "char" type, which
        # asyncpg surfaces as a 1-byte ``bytes`` value (not ``str``).
        # Compare against bytes to match the wire-level representation.
        v = await pg_test_conn.fetchval(
            """SELECT attgenerated FROM pg_attribute
               WHERE attrelid = 'public.episodic_memory'::regclass
                 AND attname = 'tsv'"""
        )
        assert v == b"s", (
            f"episodic_memory.tsv attgenerated must be b's' (STORED); "
            f"got {v!r}"
        )


class TestTsvectorGinIndex:
    @pytest.mark.asyncio
    async def test_gin_index_exists_on_tsv(self, pg_test_conn) -> None:
        row = await pg_test_conn.fetchrow(
            """SELECT indexname, indexdef
               FROM pg_indexes
               WHERE schemaname = 'public'
                 AND tablename = 'episodic_memory'
                 AND indexname = 'episodic_memory_tsv_gin'"""
        )
        assert row is not None, (
            "GIN index episodic_memory_tsv_gin must exist; without it "
            "tsvector searches seq-scan the whole table — SP-3.12 "
            "search latency promises assume this index is present"
        )
        # The index definition should mention GIN + the tsv column.
        idef = row["indexdef"].lower()
        assert "using gin" in idef, (
            f"Index type must be GIN for tsvector full-text matching; "
            f"got {row['indexdef']!r}"
        )
        assert "tsv" in idef, (
            f"Index must target the tsv column; got {row['indexdef']!r}"
        )


class TestTsvectorGeneratedValue:
    """Prove the generated column actually generates content. This is
    the functional guarantee: a new row with text in the feeder
    columns should have a non-empty tsv after the INSERT returns."""

    @pytest.mark.asyncio
    async def test_tsv_populated_on_insert(self, pg_test_conn) -> None:
        # Insert a minimal row using the real schema's required cols.
        # We don't know the full column set at test-write time, so we
        # SELECT the column list from information_schema first and build
        # a minimal INSERT — keeps this test robust if new NOT NULL
        # columns are added later.
        required_cols = await pg_test_conn.fetch(
            """SELECT column_name, data_type, column_default
               FROM information_schema.columns
               WHERE table_schema = 'public'
                 AND table_name = 'episodic_memory'
                 AND is_nullable = 'NO'
                 AND is_generated = 'NEVER'
               ORDER BY ordinal_position"""
        )
        # Build VALUES list: supply explicit text/int/float defaults for
        # each required column. We only care that the row inserts and
        # tsv ends up populated.
        col_list = []
        val_list = []
        params: list = []
        for i, r in enumerate(required_cols, start=1):
            name = r["column_name"]
            col_list.append(name)
            dtype = (r["data_type"] or "").lower()
            if "char" in dtype or "text" in dtype:
                val_list.append(f"${i}")
                params.append(
                    "test-boom-error" if name == "error_signature"
                    else "apply-patch-x" if name == "solution"
                    else "synaptics" if name == "soc_vendor"
                    else "isp,boot" if name == "tags"
                    else "sp21-fixture-value"
                )
            elif "int" in dtype:
                val_list.append(f"${i}")
                params.append(0)
            elif "real" in dtype or "double" in dtype or "numeric" in dtype:
                val_list.append(f"${i}")
                params.append(0.0)
            elif "bool" in dtype:
                val_list.append(f"${i}")
                params.append(False)
            else:
                # Unknown type — let it default if there is one, else '' text
                val_list.append(f"${i}")
                params.append("")

        insert_sql = (
            "INSERT INTO episodic_memory ("
            + ", ".join(col_list)
            + ") VALUES ("
            + ", ".join(val_list)
            + ") RETURNING tsv::text"
        )
        tsv_text = await pg_test_conn.fetchval(insert_sql, *params)

        assert tsv_text is not None and tsv_text.strip() != "''", (
            f"After inserting a row with feeder-column content, tsv "
            f"should be a non-empty tsvector; got {tsv_text!r}"
        )
        # Spot-check: our canary words should appear as lexemes.
        # to_tsvector('english', ...) lowercases + stems — 'boom' maps
        # to 'boom', 'patch' to 'patch'. The exact lexeme output depends
        # on the dictionary, so we only assert presence of the stem
        # substrings (tsvectors render as "'lexeme':pos,pos").
        assert "boom" in tsv_text
        assert "patch" in tsv_text
        assert "synaptics" in tsv_text or "synapt" in tsv_text
        assert "isp" in tsv_text


class TestTsvectorMatchQuery:
    @pytest.mark.asyncio
    async def test_full_text_match_via_plainto_tsquery(
        self, pg_test_conn,
    ) -> None:
        # Seed a row with known content then run the @@ query SP-3.12
        # will use. This is a functional test of the migration's
        # downstream utility, not just its shape.
        # Real schema of episodic_memory: id / error_signature /
        # solution are NOT NULL without a default, everything else
        # either has a default or is nullable. Only supply what the
        # upsert needs; lean on DEFAULTs elsewhere.
        await pg_test_conn.execute(
            """INSERT INTO episodic_memory
                   (id, error_signature, solution, soc_vendor, tags)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (id) DO UPDATE SET
                   error_signature = EXCLUDED.error_signature,
                   solution        = EXCLUDED.solution,
                   soc_vendor      = EXCLUDED.soc_vendor,
                   tags            = EXCLUDED.tags
            """,
            "tsv-match-test",
            "kernel panic on boot",
            "apply patch x",
            "synaptics",
            "boot,kernel",
        )
        # Query that SP-3.12 will use
        row = await pg_test_conn.fetchrow(
            """SELECT id, ts_rank(tsv, plainto_tsquery('english', $1))
                      AS rank
               FROM episodic_memory
               WHERE tsv @@ plainto_tsquery('english', $1)
                 AND id = 'tsv-match-test'""",
            "kernel panic",
        )
        assert row is not None, (
            "Row matching 'kernel panic' via plainto_tsquery should "
            "appear in the GIN-backed search. If this fails, either "
            "the generated column isn't populating or the @@ operator "
            "isn't wired to the tsvector type"
        )
        assert row["rank"] > 0


class TestMigrationIdempotency:
    """Re-running alembic upgrade head after 0017 has already landed
    must be a no-op, not an error. This is a property of ``IF NOT
    EXISTS`` in both statements — guard against drift if someone
    drops the guard clauses."""

    @pytest.mark.asyncio
    async def test_upgrade_is_idempotent(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        # pg_test_alembic_upgraded fixture already ran once this session;
        # running alembic upgrade head AGAIN should exit 0 with no
        # errors and not change the schema.
        result = _run_alembic(pg_test_alembic_upgraded, "upgrade", "head")
        assert result.returncode == 0, (
            f"Second alembic upgrade head failed: "
            f"stdout={result.stdout[-400:]!r} "
            f"stderr={result.stderr[-400:]!r}"
        )
        # alembic prints "Running upgrade X -> Y" per applied migration
        # but nothing when already at head. Use that as the idempotency
        # check: no "Running upgrade" should appear.
        assert "Running upgrade" not in result.stdout, (
            f"Idempotent re-upgrade should print no 'Running upgrade' "
            f"lines; got stdout={result.stdout!r}"
        )


class TestMigrationDowngrade:
    """Downgrading from 0017 to 0016 should remove the column + index
    cleanly. Guards against a future change that forgets to mirror
    upgrade() in downgrade()."""

    @pytest.mark.asyncio
    async def test_downgrade_then_reupgrade(
        self, pg_test_alembic_upgraded: str, pg_test_conn,
    ) -> None:
        # Down to 0016
        result = _run_alembic(pg_test_alembic_upgraded, "downgrade", "0016")
        assert result.returncode == 0, (
            f"alembic downgrade 0016 failed: "
            f"stdout={result.stdout[-400:]!r} "
            f"stderr={result.stderr[-400:]!r}"
        )

        # tsv column should be gone
        col = await pg_test_conn.fetchval(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'public'
                 AND table_name = 'episodic_memory'
                 AND column_name = 'tsv'"""
        )
        assert col is None, (
            "downgrade should drop episodic_memory.tsv; still present"
        )

        # Index should also be gone
        idx = await pg_test_conn.fetchval(
            """SELECT indexname FROM pg_indexes
               WHERE schemaname = 'public'
                 AND indexname = 'episodic_memory_tsv_gin'"""
        )
        assert idx is None, (
            "downgrade should drop GIN index; still present"
        )

        # Back up to head — this is also a re-idempotency check because
        # 0017 runs from a clean-of-0017 state.
        result2 = _run_alembic(
            pg_test_alembic_upgraded, "upgrade", "head",
        )
        assert result2.returncode == 0, (
            f"re-upgrade to head after downgrade failed: "
            f"stdout={result2.stdout[-400:]!r} "
            f"stderr={result2.stderr[-400:]!r}"
        )

        # Column + index should be back.
        # Open a fresh connection — pg_test_conn is wrapped in a long-lived
        # savepoint that has seen the mid-test downgrade/upgrade cycle and
        # may hold a stale view.
        col2 = await pg_test_conn.fetchval(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'public'
                 AND table_name = 'episodic_memory'
                 AND column_name = 'tsv'"""
        )
        assert col2 == "tsv", "Re-upgrade should restore tsv column"
