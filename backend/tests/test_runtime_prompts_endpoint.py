"""ZZ.C1 #305-1 checkbox 1 — ``GET /runtime/prompts`` + ``/prompts/diff`` tests.

Locks the contract for the prompt-version timeline + diff endpoints that
feed the ORCHESTRATOR AI drawer (ZZ.C1 checkboxes 3-4 render these):

1. **List shape** — response carries the dedup-by-hash timeline newest
   first, plus resolved ``path`` + applied ``limit`` so the drawer
   cache is keyed on what the backend actually served.
2. **Content-hash dedupe** — if the same body was re-registered across
   multiple version rows (``active → archive → active`` flap), only
   the most recent copy shows in the list.
3. **supersedes_id chain** — each entry points at the next-older
   distinct-hash entry in the deduped list, so the drawer can anchor
   "v7 replaced v5 at HH:MM" lines.
4. **Limit clamp** — ``limit`` clamps to [1, 200] so a malformed
   query can't DoS the endpoint.
5. **agent_type validation** — slug outside ``[A-Za-z0-9_-]+`` →
   HTTP 400; path traversal / wildcards rejected before the DB sees
   them.
6. **Empty timeline** — no rows → empty ``versions`` list (not 404).
7. **Diff — unified format** — output shape is literal ``difflib.
   unified_diff`` (``--- from``, ``+++ to``, ``@@`` hunks, ``-``/``+``
   line prefixes). Checked with rg-stable regex rather than
   character-exact.
8. **Diff — identical bodies** — empty diff string (``difflib``
   returns no lines when the two inputs match).
9. **Diff — total rewrite** — every line prefixed with ``-`` / ``+``;
   no shared context anchors.
10. **Diff — missing id** → 404; cross-agent id pair → 400.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
``OMNI_TEST_PG_URL`` — same pattern as ``test_tokens_burn_rate_endpoint``).
"""

from __future__ import annotations

import re

import pytest
from fastapi import HTTPException

from backend.db_context import set_tenant_id
from backend.routers.system import (
    get_prompt_diff,
    list_prompt_versions,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures / helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture(autouse=True)
def _reset_tenant_context():
    """prompt_versions has no tenant column, but other tests in the same
    session may set a tenant — clear it so the :class:`pg_test_conn`
    fixture's TRUNCATE of adjacent tables is sane."""
    set_tenant_id(None)
    yield
    set_tenant_id(None)


async def _seed_prompt(
    conn,
    *,
    path: str,
    version: int,
    body: str,
    body_sha256: str | None = None,
    role: str = "archive",
    created_at: float | None = None,
) -> int:
    """Insert a row into ``prompt_versions``. Returns the new id.

    We insert directly rather than through
    :func:`backend.prompt_registry.register_active` so tests can
    fabricate edge cases (same hash on consecutive versions, non-
    monotonic created_at, etc.) that the registered-writer path
    deliberately prevents.
    """
    import hashlib
    import time as _time

    if body_sha256 is None:
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if created_at is None:
        created_at = _time.time()

    row = await conn.fetchrow(
        """
        INSERT INTO prompt_versions
            (path, version, role, body, body_sha256, created_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        path,
        version,
        role,
        body,
        body_sha256,
        created_at,
    )
    return int(row["id"])


ORCH_PATH = "backend/agents/prompts/orchestrator.md"
FIRM_PATH = "backend/agents/prompts/firmware.md"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /runtime/prompts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestListPromptVersionsShape:
    @pytest.mark.asyncio
    async def test_empty_timeline_returns_empty_list(self, pg_test_conn):
        # No seed — endpoint must NOT 404; empty list is the contract
        # because "this agent has no registered prompts yet" is a
        # normal state on fresh installs.
        resp = await list_prompt_versions(
            agent_type="orchestrator", conn=pg_test_conn,
        )
        assert resp.agent_type == "orchestrator"
        assert resp.path == ORCH_PATH
        assert resp.limit == 20
        assert resp.versions == []

    @pytest.mark.asyncio
    async def test_single_version_round_trip(self, pg_test_conn):
        vid = await _seed_prompt(
            pg_test_conn,
            path=ORCH_PATH, version=1, role="active",
            body="you are orchestrator v1\nsecond line\nthird line",
        )
        resp = await list_prompt_versions(
            agent_type="orchestrator", conn=pg_test_conn,
        )
        assert len(resp.versions) == 1
        entry = resp.versions[0]
        assert entry.id == vid
        assert entry.agent_type == "orchestrator"
        assert entry.version == 1
        assert entry.role == "active"
        assert entry.content.startswith("you are orchestrator v1")
        # Row spec: preview = first two non-empty lines.
        assert entry.content_preview == "you are orchestrator v1\nsecond line"
        # Content hash looks like a sha256 hex (64 chars).
        assert re.fullmatch(r"[0-9a-f]{64}", entry.content_hash)
        # ISO-8601 UTC shape.
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry.created_at)
        # Bottom of timeline → supersedes_id None.
        assert entry.supersedes_id is None


class TestListPromptVersionsDedupe:
    @pytest.mark.asyncio
    async def test_same_hash_on_consecutive_versions_dedups(self, pg_test_conn):
        """Regression guard for the ``active → archive → active`` flap:
        if the same body got registered three times the operator should
        see one row, not three."""
        import hashlib
        body = "canonical body"
        h = hashlib.sha256(body.encode()).hexdigest()

        # Three rows, same hash, monotonically-increasing version.
        id1 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=1,
                                 body=body, body_sha256=h, role="archive")
        id2 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=2,
                                 body=body, body_sha256=h, role="archive")
        id3 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=3,
                                 body=body, body_sha256=h, role="active")

        resp = await list_prompt_versions(
            agent_type="orchestrator", conn=pg_test_conn,
        )
        assert len(resp.versions) == 1
        # The survivor is the newest copy (version DESC).
        assert resp.versions[0].id == id3
        assert resp.versions[0].version == 3

    @pytest.mark.asyncio
    async def test_distinct_hashes_preserve_all_entries(self, pg_test_conn):
        id1 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=1,
                                 body="v1 body", role="archive")
        id2 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=2,
                                 body="v2 body", role="archive")
        id3 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=3,
                                 body="v3 body", role="active")

        resp = await list_prompt_versions(
            agent_type="orchestrator", conn=pg_test_conn,
        )
        assert len(resp.versions) == 3
        # Newest first.
        assert [v.id for v in resp.versions] == [id3, id2, id1]

    @pytest.mark.asyncio
    async def test_supersedes_id_chains_through_deduped_list(self, pg_test_conn):
        id1 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=1,
                                 body="v1 body", role="archive")
        id2 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=2,
                                 body="v2 body", role="archive")
        id3 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=3,
                                 body="v3 body", role="active")

        resp = await list_prompt_versions(
            agent_type="orchestrator", conn=pg_test_conn,
        )
        # Newest points at next-older; bottom of list points at None.
        assert resp.versions[0].id == id3 and resp.versions[0].supersedes_id == id2
        assert resp.versions[1].id == id2 and resp.versions[1].supersedes_id == id1
        assert resp.versions[2].id == id1 and resp.versions[2].supersedes_id is None


class TestListPromptVersionsFiltering:
    @pytest.mark.asyncio
    async def test_other_agents_are_filtered_out(self, pg_test_conn):
        """One agent's rows must not bleed into another agent's timeline.
        The WHERE clause is an exact-string match on ``path``."""
        await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=1,
                           body="orch body", role="active")
        await _seed_prompt(pg_test_conn, path=FIRM_PATH, version=1,
                           body="firm body", role="active")

        resp_orch = await list_prompt_versions(
            agent_type="orchestrator", conn=pg_test_conn,
        )
        resp_firm = await list_prompt_versions(
            agent_type="firmware", conn=pg_test_conn,
        )
        assert len(resp_orch.versions) == 1
        assert resp_orch.versions[0].content == "orch body"
        assert len(resp_firm.versions) == 1
        assert resp_firm.versions[0].content == "firm body"


class TestListPromptVersionsLimitClamp:
    @pytest.mark.asyncio
    async def test_default_limit_is_twenty(self, pg_test_conn):
        # Seed 25 distinct rows → default-limit response caps at 20.
        for i in range(25):
            await _seed_prompt(
                pg_test_conn, path=ORCH_PATH, version=i + 1,
                body=f"body v{i + 1}", role="archive",
            )
        resp = await list_prompt_versions(
            agent_type="orchestrator", conn=pg_test_conn,
        )
        assert resp.limit == 20
        assert len(resp.versions) == 20
        # The 20 newest (v25 down to v6).
        assert resp.versions[0].version == 25
        assert resp.versions[-1].version == 6

    @pytest.mark.asyncio
    async def test_explicit_limit_honoured(self, pg_test_conn):
        for i in range(10):
            await _seed_prompt(
                pg_test_conn, path=ORCH_PATH, version=i + 1,
                body=f"body v{i + 1}", role="archive",
            )
        resp = await list_prompt_versions(
            agent_type="orchestrator", limit=3, conn=pg_test_conn,
        )
        assert resp.limit == 3
        assert len(resp.versions) == 3
        assert [v.version for v in resp.versions] == [10, 9, 8]

    @pytest.mark.asyncio
    async def test_zero_or_negative_limit_clamps_to_one(self, pg_test_conn):
        await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=1,
                           body="only", role="active")
        for bad in (0, -5):
            resp = await list_prompt_versions(
                agent_type="orchestrator", limit=bad, conn=pg_test_conn,
            )
            assert resp.limit == 1
            assert len(resp.versions) == 1

    @pytest.mark.asyncio
    async def test_oversize_limit_clamps_to_max(self, pg_test_conn):
        resp = await list_prompt_versions(
            agent_type="orchestrator", limit=9999, conn=pg_test_conn,
        )
        assert resp.limit == 200


class TestListPromptVersionsInputValidation:
    @pytest.mark.asyncio
    async def test_rejects_path_traversal_agent_type(self, pg_test_conn):
        for bad in ("../etc/passwd", "orch/../firm", "orch md", "", "foo*"):
            with pytest.raises(HTTPException) as exc:
                await list_prompt_versions(agent_type=bad, conn=pg_test_conn)
            assert exc.value.status_code == 400
            assert "agent_type" in str(exc.value.detail)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /runtime/prompts/diff
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_UNIFIED_HEADER_RE = re.compile(r"^--- .+\n\+\+\+ .+\n", re.MULTILINE)
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)


class TestPromptDiffFormat:
    @pytest.mark.asyncio
    async def test_unified_diff_headers_and_hunks(self, pg_test_conn):
        v1 = await _seed_prompt(
            pg_test_conn, path=ORCH_PATH, version=1,
            body="line one\nline two\nline three\n", role="archive",
        )
        v2 = await _seed_prompt(
            pg_test_conn, path=ORCH_PATH, version=2,
            body="line one\nline TWO\nline three\n", role="active",
        )
        resp = await get_prompt_diff(from_=v1, to=v2, conn=pg_test_conn)

        assert resp.from_id == v1
        assert resp.to_id == v2
        assert resp.from_version == 1
        assert resp.to_version == 2
        assert resp.agent_type == "orchestrator"
        # unified-diff shape: `--- …\n+++ …\n`, a hunk header `@@ …`.
        assert _UNIFIED_HEADER_RE.search(resp.diff) is not None
        assert _HUNK_HEADER_RE.search(resp.diff) is not None
        # The change is present as -/+ line pair.
        assert "-line two\n" in resp.diff
        assert "+line TWO\n" in resp.diff

    @pytest.mark.asyncio
    async def test_identical_bodies_produce_empty_diff(self, pg_test_conn):
        """``difflib.unified_diff`` yields zero lines when the inputs
        match — we surface that verbatim. An empty diff is the correct
        "no changes between these two hashes" signal."""
        body = "exact same body\nacross versions\n"
        v1 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=1,
                                body=body, role="archive")
        v2 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=2,
                                body=body, role="active")
        resp = await get_prompt_diff(from_=v1, to=v2, conn=pg_test_conn)
        assert resp.diff == ""
        # But the envelope still carries both sides' metadata so the
        # drawer can still render "v1 → v2 (identical)".
        assert resp.from_hash == resp.to_hash
        assert resp.from_version == 1 and resp.to_version == 2

    @pytest.mark.asyncio
    async def test_complete_rewrite_diff_has_no_shared_context(self, pg_test_conn):
        v1 = await _seed_prompt(
            pg_test_conn, path=ORCH_PATH, version=1,
            body="alpha\nbeta\ngamma\n", role="archive",
        )
        v2 = await _seed_prompt(
            pg_test_conn, path=ORCH_PATH, version=2,
            body="one\ntwo\nthree\n", role="active",
        )
        resp = await get_prompt_diff(from_=v1, to=v2, conn=pg_test_conn)
        # Every non-header line is either `-` or `+` (plus the final
        # blank-line trailer sometimes emitted). No ` ` (context)
        # prefix appears anywhere.
        body_lines = [
            ln for ln in resp.diff.splitlines()
            if ln and not ln.startswith("---")
            and not ln.startswith("+++")
            and not ln.startswith("@@")
        ]
        assert body_lines, "diff body was empty"
        for ln in body_lines:
            assert ln.startswith(("-", "+")), (
                f"unexpected context line in full-rewrite diff: {ln!r}"
            )

    @pytest.mark.asyncio
    async def test_empty_on_both_sides_yields_empty_diff(self, pg_test_conn):
        v1 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=1,
                                body="", role="archive")
        v2 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=2,
                                body="", role="active")
        resp = await get_prompt_diff(from_=v1, to=v2, conn=pg_test_conn)
        assert resp.diff == ""


class TestPromptDiffErrorCases:
    @pytest.mark.asyncio
    async def test_missing_from_id_returns_404(self, pg_test_conn):
        v1 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=1,
                                body="only row", role="active")
        with pytest.raises(HTTPException) as exc:
            await get_prompt_diff(from_=999_999, to=v1, conn=pg_test_conn)
        assert exc.value.status_code == 404
        assert "999999" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_missing_to_id_returns_404(self, pg_test_conn):
        v1 = await _seed_prompt(pg_test_conn, path=ORCH_PATH, version=1,
                                body="only row", role="active")
        with pytest.raises(HTTPException) as exc:
            await get_prompt_diff(from_=v1, to=999_999, conn=pg_test_conn)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_cross_agent_pair_rejected_with_400(self, pg_test_conn):
        """Two ids that straddle different agents must fail loudly —
        otherwise the drawer would render a full add/remove block that
        looks like a genuine rewrite, which is misleading."""
        orch_id = await _seed_prompt(
            pg_test_conn, path=ORCH_PATH, version=1,
            body="orch body", role="active",
        )
        firm_id = await _seed_prompt(
            pg_test_conn, path=FIRM_PATH, version=1,
            body="firm body", role="active",
        )
        with pytest.raises(HTTPException) as exc:
            await get_prompt_diff(from_=orch_id, to=firm_id, conn=pg_test_conn)
        assert exc.value.status_code == 400
        assert "cross-agent" in str(exc.value.detail).lower()
