"""ZZ.C1 #305-1 checkbox 2 — auto-capture of runtime-assembled
system prompts into ``prompt_versions``.

Coverage:
  * Hash-dedupe: identical content does not produce duplicate rows.
  * Distinct hashes land as separate rows with monotonically-
    increasing ``version`` numbers under the same ``path``.
  * Advisory-lock coordination: N concurrent workers calling
    ``capture_prompt_snapshot`` on the SAME body insert exactly ONE
    row (no UNIQUE(path, version) collision, no duplicate hash row).
  * Path/role fence: the shipped active/canary rows are untouched;
    new rows are tagged ``role='snapshot'``.
  * Read API integration: the ZZ.C1 list endpoint surfaces snapshot
    rows alongside registered active rows (no role filter).
  * ``build_system_prompt`` sync wrapper schedules a capture task on
    an active event loop; when captured the table contains the
    exact assembled prompt string.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from backend import prompt_registry as pr


VALID = "backend/agents/prompts/orchestrator.md"


@pytest.fixture()
async def fresh_db(pg_test_pool, pg_test_dsn, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE prompt_versions RESTART IDENTITY CASCADE"
        )
    from backend import db
    if db._db is not None:
        await db.close()
    await db.init()
    try:
        yield db
    finally:
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE prompt_versions RESTART IDENTITY CASCADE"
            )


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  capture_prompt_snapshot — hash dedupe
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_capture_inserts_new_hash(fresh_db):
    body = "system prompt body A"
    new_id = await pr.capture_prompt_snapshot(VALID, body)
    assert new_id is not None

    rows = await pr.list_all(VALID)
    assert len(rows) == 1
    assert rows[0].body_sha256 == _sha(body)
    assert rows[0].role == pr.SNAPSHOT_ROLE
    assert rows[0].version == 1


@pytest.mark.asyncio
async def test_capture_skips_existing_hash(fresh_db):
    body = "identical body"
    first = await pr.capture_prompt_snapshot(VALID, body)
    second = await pr.capture_prompt_snapshot(VALID, body)
    third = await pr.capture_prompt_snapshot(VALID, body)
    assert first is not None
    assert second is None
    assert third is None

    rows = await pr.list_all(VALID)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_capture_distinct_bodies_monotonic_versions(fresh_db):
    ids = []
    for i in range(3):
        ids.append(await pr.capture_prompt_snapshot(VALID, f"body v{i}"))
    assert all(i is not None for i in ids)

    rows = sorted(await pr.list_all(VALID), key=lambda r: r.version)
    assert [r.version for r in rows] == [1, 2, 3]
    assert len({r.body_sha256 for r in rows}) == 3


@pytest.mark.asyncio
async def test_capture_does_not_touch_active_row(fresh_db):
    """An already-registered active row is preserved — snapshot rows
    must not flip the active/canary routing."""
    active = await pr.register_active(VALID, "active body")
    await pr.capture_prompt_snapshot(VALID, "snapshot-only body")

    current_active = await pr.get_active(VALID)
    assert current_active is not None
    assert current_active.id == active.id
    assert current_active.role == "active"

    rows = await pr.list_all(VALID)
    # One active + one snapshot = 2 rows.
    roles = sorted(r.role for r in rows)
    assert roles == ["active", pr.SNAPSHOT_ROLE]


@pytest.mark.asyncio
async def test_capture_identical_to_active_still_dedupes(fresh_db):
    """If the active row has hash X and the runtime assembly also
    produces hash X, capture must see the existing row (via body_sha256
    lookup, regardless of role) and skip — no duplicate, no role flip."""
    body = "shared body between active and runtime assembly"
    await pr.register_active(VALID, body)
    res = await pr.capture_prompt_snapshot(VALID, body)
    assert res is None

    rows = await pr.list_all(VALID)
    assert len(rows) == 1
    assert rows[0].role == "active"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Concurrent capture — advisory lock correctness
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_capture_concurrent_same_body_inserts_once(fresh_db):
    """8 concurrent capture calls with the same body must land exactly
    one row. Without the advisory lock, two would pick the same
    ``version`` and one would fail on UNIQUE(path, version); without
    dedupe, all 8 would insert distinct versions of the same hash."""
    body = "concurrent snapshot body"
    results = await asyncio.gather(
        *[pr.capture_prompt_snapshot(VALID, body) for _ in range(8)],
        return_exceptions=True,
    )
    # No raised exceptions.
    assert all(not isinstance(r, BaseException) for r in results), results
    # Exactly one winner returned an id; the rest returned None.
    inserted = [r for r in results if r is not None]
    assert len(inserted) == 1, results

    rows = await pr.list_all(VALID)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_capture_concurrent_distinct_bodies(fresh_db):
    """Concurrent captures with distinct bodies all land (no dedupe
    collision) and receive unique monotonic versions via the shared
    advisory lock."""
    bodies = [f"distinct body {i}" for i in range(5)]
    results = await asyncio.gather(
        *[pr.capture_prompt_snapshot(VALID, b) for b in bodies],
    )
    assert all(r is not None for r in results)
    rows = await pr.list_all(VALID)
    versions = sorted(r.version for r in rows)
    assert versions == [1, 2, 3, 4, 5]
    assert len({r.body_sha256 for r in rows}) == 5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Read-API integration — ZZ.C1 list endpoint sees snapshots
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_snapshot_rows_surface_via_list_api(fresh_db, pg_test_pool):
    """``GET /runtime/prompts`` does not filter by role — snapshots
    must appear in the timeline alongside the active registration."""
    await pr.register_active(VALID, "active v1")
    await pr.capture_prompt_snapshot(VALID, "snapshot body v1")
    await pr.capture_prompt_snapshot(VALID, "snapshot body v2")

    # Mirror the list endpoint's query (ORDER BY version DESC).
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, role, body_sha256, version "
            "FROM prompt_versions WHERE path = $1 "
            "ORDER BY version DESC",
            VALID,
        )
    assert len(rows) == 3
    roles_top_down = [r["role"] for r in rows]
    # Newest first: two snapshots, then the active baseline at v1.
    assert roles_top_down.count(pr.SNAPSHOT_ROLE) == 2
    assert roles_top_down[-1] == "active"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  build_system_prompt integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_build_system_prompt_sync_context_does_not_capture():
    """Called from a sync context (no running event loop) capture must
    silently no-op — the feature is best-effort and offline scripts /
    unit tests that invoke ``build_system_prompt`` directly must not
    raise."""
    from backend.prompt_loader import build_system_prompt
    # Must not raise.
    prompt = build_system_prompt(model_name="", agent_type="firmware")
    assert isinstance(prompt, str)
    assert len(prompt) > 0


@pytest.mark.asyncio
async def test_build_system_prompt_async_context_captures(fresh_db, pg_test_pool):
    """When ``build_system_prompt`` runs on a live event loop + pool,
    the assembled prompt must land as a ``snapshot`` row whose
    ``body_sha256`` matches the returned string's SHA-256."""
    from backend.prompt_loader import build_system_prompt, _CAPTURE_TASKS
    assembled = build_system_prompt(model_name="", agent_type="orchestrator")
    assert assembled

    # Drain outstanding capture tasks before asserting DB state.
    if _CAPTURE_TASKS:
        await asyncio.gather(*list(_CAPTURE_TASKS), return_exceptions=True)

    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT body_sha256, body, role FROM prompt_versions "
            "WHERE path = $1 AND body_sha256 = $2",
            VALID, _sha(assembled),
        )
    assert row is not None
    assert row["role"] == pr.SNAPSHOT_ROLE
    assert row["body"] == assembled


@pytest.mark.asyncio
async def test_build_system_prompt_identical_calls_dedupe(
    fresh_db, pg_test_pool,
):
    """Two back-to-back ``build_system_prompt`` calls with the same
    arguments produce one snapshot row, not two — the content hash
    dedup fires before INSERT."""
    from backend.prompt_loader import build_system_prompt, _CAPTURE_TASKS
    for _ in range(3):
        build_system_prompt(model_name="", agent_type="orchestrator")
    if _CAPTURE_TASKS:
        await asyncio.gather(*list(_CAPTURE_TASKS), return_exceptions=True)

    async with pg_test_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM prompt_versions WHERE path = $1",
            VALID,
        )
    assert count == 1


@pytest.mark.asyncio
async def test_build_system_prompt_sub_type_uses_double_underscore_path(
    fresh_db, pg_test_pool,
):
    """``sub_type`` is encoded as ``<agent>__<sub>.md`` so different
    sub-specialisations produce distinct timelines — the
    orchestrator.md timeline is not polluted by firmware/bsp content."""
    from backend.prompt_loader import build_system_prompt, _CAPTURE_TASKS
    build_system_prompt(
        model_name="", agent_type="firmware", sub_type="bsp",
    )
    if _CAPTURE_TASKS:
        await asyncio.gather(*list(_CAPTURE_TASKS), return_exceptions=True)

    expected_path = "backend/agents/prompts/firmware__bsp.md"
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT path, role FROM prompt_versions WHERE path = $1",
            expected_path,
        )
    assert row is not None
    assert row["role"] == pr.SNAPSHOT_ROLE


def test_snapshot_path_for_rejects_malformed_slug():
    """Defensive path-fence: malformed agent_type slugs (path-traversal
    attempts, whitespace, wildcards) must return None so the scheduler
    skips rather than writing a crafted path."""
    from backend.prompt_loader import _snapshot_path_for
    assert _snapshot_path_for("") is None
    assert _snapshot_path_for("../etc/passwd") is None
    assert _snapshot_path_for("foo bar") is None
    assert _snapshot_path_for("foo*") is None
    assert _snapshot_path_for("orch", "../bad") is None
    # Valid cases.
    assert _snapshot_path_for("orchestrator") == (
        "backend/agents/prompts/orchestrator.md"
    )
    assert _snapshot_path_for("firmware", "bsp") == (
        "backend/agents/prompts/firmware__bsp.md"
    )
