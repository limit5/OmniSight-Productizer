"""Phase-3-Runtime-v2 SP-1.4 — lifespan wiring contract tests.

Instead of driving the full FastAPI lifespan (which requires mocking the
entire startup chain — validate_startup_config, db.init, seed_defaults,
auth.ensure_default_admin, and a dozen background tasks), we verify the
wiring *structurally*: the contract that ``backend/main.py`` lifespan
MUST call ``db_pool.init_pool()`` after ``db.init()`` and
``db_pool.close_pool()`` before ``db.close()``, gated on
``_resolve_pg_dsn()`` being non-empty.

This approach:
  * Runs in milliseconds (no app boot, no DB, no mocks)
  * Is robust to refactoring that renames local variables but
    preserves the pattern — we assert on the canonical names the SP-1.4
    commit introduced, which doubles as a lint for drift
  * Breaks loudly if someone removes/reorders the pool wiring

Runtime correctness of ``init_pool`` / ``close_pool`` themselves is
covered by ``test_db_pool.py`` (SP-1.3), which exercises them against
the real test PG.

Runtime correctness of the LIFESPAN actually invoking the wiring is
covered by the Epic 11 smoke test (``test_db_pool_live_lifespan.py``
in Epic 11, post-deploy step) — that test will run against a fully
provisioned environment (real DSN, secrets, etc.) so it can drive
lifespan end-to-end without extensive mocking.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = BACKEND_ROOT / "main.py"


@pytest.fixture(scope="module")
def main_source() -> str:
    return MAIN_PY.read_text()


class TestLifespanWiring:
    """Contract on backend/main.py lifespan — the pool must be wired in,
    gated on DSN, and ordered correctly relative to db.init / db.close."""

    def test_main_imports_db_pool(self, main_source: str) -> None:
        # We accept either `from backend import db_pool` or the SP-1.4
        # canonical `as _db_pool` alias, or any alias — the point is
        # the module is imported somewhere in main.py. This is an
        # import contract: Epic 7 (compat wrapper delete) will further
        # enforce exclusivity, but SP-1.4 just requires presence.
        assert re.search(r"from\s+backend\s+import\s+db_pool", main_source), (
            "backend/main.py must import db_pool for SP-1.4 wiring. "
            "If this test is failing, someone removed the pool startup "
            "hook — either restore it or update this test to reflect a "
            "deliberate scope change."
        )

    def test_init_pool_called_with_gate(self, main_source: str) -> None:
        # Gate pattern: verify via line-based check (avoids catastrophic
        # regex backtracking). Requirements:
        #   (a) there IS an `init_pool(` call
        #   (b) somewhere within ~5 lines above it, there IS an `if ` on
        #       a local name (the DSN gate)
        lines = main_source.splitlines()
        init_pool_line = next(
            (i for i, ln in enumerate(lines) if "init_pool(" in ln),
            None,
        )
        assert init_pool_line is not None, (
            "No init_pool( call found in backend/main.py"
        )
        # Look back up to 8 lines for a gate. The current implementation
        # uses `if _pg_dsn:`, but we accept any local-name `if`.
        window_start = max(0, init_pool_line - 8)
        window = lines[window_start:init_pool_line]
        has_gate = any(
            re.match(r"\s*if\s+_?\w+\s*:\s*$", ln) for ln in window
        )
        assert has_gate, (
            "init_pool must be preceded by an `if <name>:` gate (within "
            "~8 lines above) to preserve SQLite dev mode. Context lines:\n"
            + "\n".join(f"  {i + window_start:4d}: {ln}" for i, ln in enumerate(window))
        )

    def test_close_pool_called_on_shutdown(self, main_source: str) -> None:
        # close_pool is idempotent — can be called unconditionally since
        # it no-ops when pool wasn't initialised. No gate required here.
        assert re.search(r"await\s+\w+\.close_pool\(", main_source), (
            "backend/main.py lifespan must call db_pool.close_pool() on "
            "shutdown. Without it, PG connections leak across lifespan "
            "restarts and eventually exhaust pg `max_connections`."
        )


class TestLifespanOrdering:
    """Verify init_pool fires AFTER db.init (compat wrapper + alembic-
    owned schema comes up first) and close_pool fires BEFORE db.close
    (symmetric teardown)."""

    def test_init_pool_after_db_init(self, main_source: str) -> None:
        db_init_idx = main_source.find("await db.init()")
        init_pool_match = re.search(r"await\s+\w+\.init_pool\(", main_source)
        assert db_init_idx >= 0, "main.py should still call `await db.init()`"
        assert init_pool_match is not None, "init_pool call not found"
        init_pool_idx = init_pool_match.start()
        assert db_init_idx < init_pool_idx, (
            f"init_pool (at {init_pool_idx}) must come AFTER db.init "
            f"(at {db_init_idx}) so the compat wrapper + schema "
            f"migrations land first. Reversing the order risks the pool "
            f"hitting a half-migrated schema during the alembic 0017 "
            f"FTS5→tsvector deploy window."
        )

    def test_close_pool_before_db_close(self, main_source: str) -> None:
        db_close_idx = main_source.find("await db.close()")
        close_pool_match = re.search(r"await\s+\w+\.close_pool\(", main_source)
        assert db_close_idx >= 0, "main.py should still call `await db.close()`"
        assert close_pool_match is not None, "close_pool call not found"
        close_pool_idx = close_pool_match.start()
        assert close_pool_idx < db_close_idx, (
            f"close_pool (at {close_pool_idx}) must come BEFORE db.close "
            f"(at {db_close_idx}) — the compat wrapper's underlying "
            f"single connection should be the LAST DB handle torn down, "
            f"so any in-flight pool-using code path has a chance to "
            f"finish before the process exits."
        )


class TestLifespanGateSafety:
    """Contract that the SQLite dev path is preserved — no PG URL, no
    pool. This is what makes the SP-1.4 change backwards-compatible
    with every existing local dev workflow."""

    def test_lifespan_references_resolve_pg_dsn(self, main_source: str) -> None:
        # The gate must consult `_resolve_pg_dsn` (from backend.db) —
        # that's the canonical URL resolver the compat wrapper already
        # uses. Any other source of truth would drift.
        assert "_resolve_pg_dsn" in main_source, (
            "Lifespan must use db._resolve_pg_dsn() as the DSN source of "
            "truth (same as the compat wrapper's own dispatch). Using a "
            "different resolver risks the pool and compat wrapper "
            "disagreeing on whether PG is configured."
        )

    def test_sqlite_fallback_log_present(self, main_source: str) -> None:
        # When DSN is empty, we explicitly log "db_pool: skipped — ..."
        # so operators running in SQLite dev mode see *why* the pool
        # didn't come up. Without this log, a misconfigured DSN would
        # silently fail to enable the pool and surface as weird
        # behaviour at query time.
        assert "db_pool" in main_source
        assert re.search(
            r"SQLite\s+dev\s+mode",
            main_source,
        ), (
            "Lifespan should log when it skips pool init in SQLite mode "
            "(message containing 'SQLite dev mode'). This is operator-"
            "facing signal; without it a misconfigured DSN silently "
            "skips the pool and surfaces as cryptic query errors later."
        )
