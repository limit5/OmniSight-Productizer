"""ZZ.C1 #305-1 checkbox 5 — Prompt-version cross-axis integration matrix.

Per-axis coverage already exists and is green:

  * **Hash dedupe (list endpoint)** →
    ``backend/tests/test_runtime_prompts_endpoint.py`` ::
    ``TestListPromptVersionsDedupe`` (3 cases — same-hash-flap / distinct
    hashes / supersedes_id chain).
  * **Hash dedupe + write concurrency (capture path)** →
    ``backend/tests/test_prompt_snapshot_capture.py`` — insert / skip /
    monotonic-versions / advisory-lock concurrent captures (13 cases).
  * **Diff renderer edge cases (backend)** →
    ``backend/tests/test_runtime_prompts_endpoint.py`` ::
    ``TestPromptDiffFormat`` (4 cases — unified headers / identical /
    total rewrite / both-empty).
  * **Diff renderer edge cases (frontend)** →
    ``test/components/prompt-version-drawer.test.tsx`` (changesToDiffRows
    + integration — identical-hash / empty-body / total-rewrite).

What those per-axis tests do **not** exercise is the **handoff contract
between the three axes** — the place regressions hide when each axis
stays green but the composed flow breaks:

  1. **capture → list**: a real ``capture_prompt_snapshot`` write (with
     advisory lock + version increment) must surface through
     ``list_prompt_versions`` in the shape the drawer expects
     (newest-first, deduped by hash, supersedes_id chain intact). Direct
     ``_seed_prompt`` inserts skip the capture write path and lose this
     contract.
  2. **concurrent capture → list → diff**: N concurrent captures on the
     same body must land exactly one row whose id is then a valid input
     to ``get_prompt_diff`` (paired with any other real capture), and
     the diff must render correctly. A broken dedupe would produce
     two rows with the same body_sha256; list dedupes them down to one
     in the response but the *losing* id would still resolve in the
     diff endpoint and yield a confusing zero-line diff between two ids
     that the drawer cannot pair.
  3. **capture → diff edge cases**: diff between two real captures must
     yield the same unified-diff shape the frontend side-by-side view
     assumes (identical → empty; total rewrite → no shared context;
     minor edit → header + hunk + per-line -/+).

This file adds exactly those cross-axis tests — 12 cases across 4
class groups — without duplicating what per-axis tests already lock.

Runs against the test PG via ``pg_test_pool`` (skips cleanly without
``OMNI_TEST_PG_URL`` — same pattern as sibling test files). Captures go
through the real pool so the advisory lock + tx boundary are exercised
just like production; ``list_prompt_versions`` / ``get_prompt_diff`` are
invoked with a borrowed pool conn so the route handler runs against the
same data the captures committed.
"""

from __future__ import annotations

import asyncio
import hashlib
import re

import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend import prompt_registry as pr
from backend.routers.system import (
    get_prompt_diff,
    list_prompt_versions,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures + helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_MATRIX_PATH = "backend/agents/prompts/orchestrator.md"
_MATRIX_AGENT = "orchestrator"
_SIBLING_PATH = "backend/agents/prompts/firmware.md"
_SIBLING_AGENT = "firmware"


@pytest_asyncio.fixture()
async def matrix_db(pg_test_pool, pg_test_dsn, monkeypatch):
    """Wipes ``prompt_versions``, installs the test pool as the process
    pool so ``capture_prompt_snapshot(conn=None)`` exercises the real
    pool acquire + tx boundary (same path production runs), and cleans
    up on teardown.

    ``pg_test_pool`` already does ``db_pool.init_pool`` so the pool is
    reachable via ``backend.db_pool.get_pool()``. We only need to reset
    the table between tests (the outer-tx trick in ``pg_test_conn``
    does not work here because each capture commits its own tx through
    the pool — savepoints won't roll back committed rows).
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE prompt_versions RESTART IDENTITY CASCADE"
        )
    try:
        yield pg_test_pool
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE prompt_versions RESTART IDENTITY CASCADE"
            )


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# unified_diff header/hunk regexes — match the contract the frontend
# drawer's ``Diff.diffLines`` / backend difflib join produces.
_UNIFIED_HEADER_RE = re.compile(r"^--- .+\n\+\+\+ .+\n", re.MULTILINE)
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Axis 1 → Axis 2 handoff:
#    capture_prompt_snapshot → list_prompt_versions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCaptureFeedsList:
    """Rows written by ``capture_prompt_snapshot`` must be the exact rows
    the drawer lists. Regression here breaks operator ability to see
    what the agent saw."""

    @pytest.mark.asyncio
    async def test_dedupe_is_visible_in_list(self, matrix_db):
        """Three captures of the same body → one row in the list endpoint.
        Locks the axis-1 → axis-2 handoff: captured dedupe is respected
        by the read API without post-filtering drift."""
        body = "orchestrator prompt v1\nline two\nline three\n"
        ids = [
            await pr.capture_prompt_snapshot(_MATRIX_PATH, body)
            for _ in range(3)
        ]
        # First capture wins an id; the next two see the hash and no-op.
        assert ids[0] is not None
        assert ids[1] is None
        assert ids[2] is None

        async with matrix_db.acquire() as conn:
            resp = await list_prompt_versions(
                agent_type=_MATRIX_AGENT, conn=conn,
            )
        assert len(resp.versions) == 1
        entry = resp.versions[0]
        assert entry.id == ids[0]
        assert entry.content == body
        assert entry.content_hash == _sha(body)
        # Bottom of timeline — no older sibling.
        assert entry.supersedes_id is None

    @pytest.mark.asyncio
    async def test_distinct_bodies_monotonic_with_supersedes_chain(
        self, matrix_db,
    ):
        """Three captures with distinct bodies must produce a three-row
        list response whose ``supersedes_id`` chain threads through the
        captured ids in version-descending order."""
        bodies = [f"orchestrator prompt v{i}\nline two\n" for i in range(3)]
        ids = [
            await pr.capture_prompt_snapshot(_MATRIX_PATH, b) for b in bodies
        ]
        assert all(i is not None for i in ids)

        async with matrix_db.acquire() as conn:
            resp = await list_prompt_versions(
                agent_type=_MATRIX_AGENT, conn=conn,
            )
        # Newest first — captured order reversed.
        expected_ids = list(reversed(ids))
        assert [v.id for v in resp.versions] == expected_ids
        # supersedes_id chain: newest → next-older → … → None.
        assert resp.versions[0].supersedes_id == expected_ids[1]
        assert resp.versions[1].supersedes_id == expected_ids[2]
        assert resp.versions[2].supersedes_id is None
        # All three distinct hashes surface.
        assert len({v.content_hash for v in resp.versions}) == 3

    @pytest.mark.asyncio
    async def test_capture_does_not_bleed_across_agent_paths(
        self, matrix_db,
    ):
        """Captures on ``orchestrator.md`` must not pollute the
        ``firmware.md`` timeline and vice versa — the agent-type switch
        in the drawer is a hard fence."""
        await pr.capture_prompt_snapshot(_MATRIX_PATH, "orch body\n")
        await pr.capture_prompt_snapshot(_SIBLING_PATH, "firm body\n")

        async with matrix_db.acquire() as conn:
            orch = await list_prompt_versions(
                agent_type=_MATRIX_AGENT, conn=conn,
            )
            firm = await list_prompt_versions(
                agent_type=_SIBLING_AGENT, conn=conn,
            )
        assert len(orch.versions) == 1
        assert orch.versions[0].content == "orch body\n"
        assert len(firm.versions) == 1
        assert firm.versions[0].content == "firm body\n"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Axis 1 (concurrent) → Axis 2 → Axis 3 handoff:
#    parallel captures → list → diff
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConcurrentCaptureFlow:
    """The advisory lock must hold under real concurrent writers, and the
    id returned by the one winner must be a valid diff endpoint input."""

    @pytest.mark.asyncio
    async def test_eight_concurrent_same_body_produces_one_row(
        self, matrix_db,
    ):
        """Regression guard for the ``UNIQUE(path, version)`` race: 8
        concurrent captures with the same body must all complete without
        raising, exactly one must return an id, and the list must show
        exactly one row."""
        body = "concurrent body matrix\n"
        results = await asyncio.gather(
            *[pr.capture_prompt_snapshot(_MATRIX_PATH, body) for _ in range(8)],
            return_exceptions=True,
        )
        # No raised exceptions — the advisory lock serialises writers.
        assert all(not isinstance(r, BaseException) for r in results), results
        inserted = [r for r in results if r is not None]
        assert len(inserted) == 1, results

        async with matrix_db.acquire() as conn:
            resp = await list_prompt_versions(
                agent_type=_MATRIX_AGENT, conn=conn,
            )
        assert len(resp.versions) == 1
        assert resp.versions[0].id == inserted[0]
        assert resp.versions[0].content_hash == _sha(body)

    @pytest.mark.asyncio
    async def test_mixed_concurrent_captures_land_deterministic_set(
        self, matrix_db,
    ):
        """8 concurrent same-body + 5 concurrent distinct-body captures
        → dedupe + advisory lock together produce exactly 6 rows
        (1 deduped "same" row + 5 distinct) in the list response. No
        UNIQUE violation; no lost capture; no phantom duplicates."""
        same_body = "shared concurrent body\n"
        distinct_bodies = [f"unique body {i}\n" for i in range(5)]
        tasks = (
            [pr.capture_prompt_snapshot(_MATRIX_PATH, same_body) for _ in range(8)]
            + [pr.capture_prompt_snapshot(_MATRIX_PATH, b) for b in distinct_bodies]
        )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(not isinstance(r, BaseException) for r in results), results
        inserted = [r for r in results if r is not None]
        # 1 winner from the 8 same-body contenders + 5 distinct bodies = 6.
        assert len(inserted) == 6, results

        async with matrix_db.acquire() as conn:
            resp = await list_prompt_versions(
                agent_type=_MATRIX_AGENT, conn=conn,
            )
        # list dedupes by hash — 1 same + 5 distinct = 6 rows.
        assert len(resp.versions) == 6
        hashes = {v.content_hash for v in resp.versions}
        assert len(hashes) == 6
        assert _sha(same_body) in hashes

    @pytest.mark.asyncio
    async def test_concurrent_capture_id_feeds_diff_endpoint(
        self, matrix_db,
    ):
        """The id returned by a concurrent capture winner must resolve
        in the diff endpoint — i.e. the row actually committed, not a
        phantom id returned by the lock loser."""
        body_a = "concurrent winner A\nline two\n"
        body_b = "concurrent winner B\nline two differs\n"
        # Each gather batch produces ONE winner id.
        res_a = await asyncio.gather(
            *[pr.capture_prompt_snapshot(_MATRIX_PATH, body_a) for _ in range(4)],
        )
        res_b = await asyncio.gather(
            *[pr.capture_prompt_snapshot(_MATRIX_PATH, body_b) for _ in range(4)],
        )
        id_a = next(r for r in res_a if r is not None)
        id_b = next(r for r in res_b if r is not None)
        async with matrix_db.acquire() as conn:
            diff = await get_prompt_diff(from_=id_a, to=id_b, conn=conn)
        assert diff.from_id == id_a
        assert diff.to_id == id_b
        assert diff.agent_type == _MATRIX_AGENT
        # Both hashes surface (no "not-found" masking from the race).
        assert diff.from_hash == _sha(body_a)
        assert diff.to_hash == _sha(body_b)
        # Minor edit → unified-diff emits both header + hunk marker.
        assert _UNIFIED_HEADER_RE.search(diff.diff) is not None
        assert _HUNK_HEADER_RE.search(diff.diff) is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Axis 1 → Axis 3 handoff:
#    capture → diff edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCaptureDiffEdgeCases:
    """Edge cases the frontend side-by-side view short-circuits on:
    identical bodies → "identical" placeholder, empty/empty → "empty"
    placeholder, total rewrite → all red + green rows no context."""

    @pytest.mark.asyncio
    async def test_total_rewrite_diff_has_no_shared_context(
        self, matrix_db,
    ):
        """Two captures with zero shared lines produce a diff where every
        body line is prefixed ``-`` or ``+`` and no `` `` context
        line appears. This is the edge case the drawer colours red+green
        top-to-bottom with zero grey context."""
        body_a = "alpha\nbeta\ngamma\n"
        body_b = "one\ntwo\nthree\n"
        id_a = await pr.capture_prompt_snapshot(_MATRIX_PATH, body_a)
        id_b = await pr.capture_prompt_snapshot(_MATRIX_PATH, body_b)
        assert id_a is not None and id_b is not None

        async with matrix_db.acquire() as conn:
            resp = await get_prompt_diff(from_=id_a, to=id_b, conn=conn)
        body_lines = [
            ln for ln in resp.diff.splitlines()
            if ln and not ln.startswith(("---", "+++", "@@"))
        ]
        assert body_lines, "expected non-empty diff body"
        for ln in body_lines:
            assert ln.startswith(("-", "+")), (
                f"unexpected context line in full-rewrite diff: {ln!r}"
            )

    @pytest.mark.asyncio
    async def test_identical_hash_pair_diff_is_empty(
        self, matrix_db, pg_test_pool,
    ):
        """Two rows sharing the same ``body_sha256`` — only reachable when
        the capture path's dedupe fast-path races a parallel writer
        (extremely rare but possible) — produce an empty diff because
        ``difflib.unified_diff`` yields zero lines on equal inputs. The
        drawer short-circuits to the ``prompt-version-diff-identical``
        placeholder on this shape."""
        # We cannot trigger this race on purpose from capture_prompt_snapshot
        # (the double-checked lock prevents it). Seed two rows with the
        # same body_sha256 directly to mimic the "loser" scenario.
        body = "shared hash but two rows\n"
        sha = _sha(body)
        async with pg_test_pool.acquire() as conn:
            id_a = await conn.fetchval(
                "INSERT INTO prompt_versions "
                "(path, version, role, body, body_sha256, created_at) "
                "VALUES ($1, 1, 'archive', $2, $3, $4) RETURNING id",
                _MATRIX_PATH, body, sha, 1_700_000_000.0,
            )
            id_b = await conn.fetchval(
                "INSERT INTO prompt_versions "
                "(path, version, role, body, body_sha256, created_at) "
                "VALUES ($1, 2, 'active', $2, $3, $4) RETURNING id",
                _MATRIX_PATH, body, sha, 1_700_000_100.0,
            )

        async with matrix_db.acquire() as conn:
            resp = await get_prompt_diff(from_=id_a, to=id_b, conn=conn)
        assert resp.diff == ""
        assert resp.from_hash == resp.to_hash == sha
        # Envelope metadata still fills in for the drawer's "v1 → v2
        # (identical)" banner.
        assert resp.from_version == 1
        assert resp.to_version == 2

    @pytest.mark.asyncio
    async def test_empty_bodies_diff_is_empty(
        self, matrix_db, pg_test_pool,
    ):
        """Two empty-body rows → empty diff. Only reachable by direct
        seed (capture_prompt_snapshot dedupes the first empty string and
        refuses to write a second)."""
        empty_sha = _sha("")
        async with pg_test_pool.acquire() as conn:
            id_a = await conn.fetchval(
                "INSERT INTO prompt_versions "
                "(path, version, role, body, body_sha256, created_at) "
                "VALUES ($1, 1, 'archive', $2, $3, $4) RETURNING id",
                _MATRIX_PATH, "", empty_sha, 1_700_000_000.0,
            )
            id_b = await conn.fetchval(
                "INSERT INTO prompt_versions "
                "(path, version, role, body, body_sha256, created_at) "
                "VALUES ($1, 2, 'active', $2, $3, $4) RETURNING id",
                _MATRIX_PATH, "", empty_sha, 1_700_000_100.0,
            )

        async with matrix_db.acquire() as conn:
            resp = await get_prompt_diff(from_=id_a, to=id_b, conn=conn)
        assert resp.diff == ""

    @pytest.mark.asyncio
    async def test_small_edit_diff_has_unified_headers_and_hunks(
        self, matrix_db,
    ):
        """Minor edit via capture — full unified-diff contract: headers,
        at least one hunk, per-line -/+ markers for the changed lines."""
        body_a = "line one\nline two\nline three\nline four\n"
        body_b = "line one\nline TWO\nline three\nline four\nadded\n"
        id_a = await pr.capture_prompt_snapshot(_MATRIX_PATH, body_a)
        id_b = await pr.capture_prompt_snapshot(_MATRIX_PATH, body_b)
        assert id_a is not None and id_b is not None

        async with matrix_db.acquire() as conn:
            resp = await get_prompt_diff(from_=id_a, to=id_b, conn=conn)
        assert _UNIFIED_HEADER_RE.search(resp.diff) is not None
        assert _HUNK_HEADER_RE.search(resp.diff) is not None
        assert "-line two\n" in resp.diff
        assert "+line TWO\n" in resp.diff
        assert "+added\n" in resp.diff


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Full lifecycle: capture → list → diff → dedupe + error cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFullLifecycle:
    """End-to-end operator scenario: three prompt versions captured over
    time, drawer lists them newest-first, operator picks two, diff
    endpoint returns the expected unified output. Also verifies the
    cross-axis error surface (cross-agent pair rejected even when both
    ids came from real captures)."""

    @pytest.mark.asyncio
    async def test_three_captures_list_and_diff_round_trip(
        self, matrix_db,
    ):
        bodies = [
            "v1 body\nshared\ncontext\n",
            "v2 body\nshared\ncontext\n",
            "v3 body\nshared\ncontext\n",
        ]
        ids = []
        for b in bodies:
            ids.append(await pr.capture_prompt_snapshot(_MATRIX_PATH, b))
        assert all(i is not None for i in ids)

        async with matrix_db.acquire() as conn:
            resp = await list_prompt_versions(
                agent_type=_MATRIX_AGENT, conn=conn,
            )
            # Operator picks the oldest + newest for diff.
            diff = await get_prompt_diff(
                from_=ids[0], to=ids[2], conn=conn,
            )
        # List is newest-first.
        assert [v.id for v in resp.versions] == [ids[2], ids[1], ids[0]]
        # Diff between v1 and v3 shows a hunk with one - and one + (the
        # first line differs; middle + last two lines are shared).
        assert _UNIFIED_HEADER_RE.search(diff.diff) is not None
        assert _HUNK_HEADER_RE.search(diff.diff) is not None
        assert "-v1 body\n" in diff.diff
        assert "+v3 body\n" in diff.diff

    @pytest.mark.asyncio
    async def test_cross_agent_pair_from_real_captures_rejected_400(
        self, matrix_db,
    ):
        """Captures on two different agents both produce valid ids, but
        the diff endpoint must still reject the cross-agent pair with 400
        — the guard is path-based, not role-based, so it survives the
        full capture → list → diff pipeline."""
        orch_id = await pr.capture_prompt_snapshot(
            _MATRIX_PATH, "orchestrator body\n",
        )
        firm_id = await pr.capture_prompt_snapshot(
            _SIBLING_PATH, "firmware body\n",
        )
        assert orch_id is not None and firm_id is not None
        async with matrix_db.acquire() as conn:
            with pytest.raises(HTTPException) as exc:
                await get_prompt_diff(
                    from_=orch_id, to=firm_id, conn=conn,
                )
        assert exc.value.status_code == 400
        assert "cross-agent" in str(exc.value.detail).lower()
