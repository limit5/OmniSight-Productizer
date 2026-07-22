"""γ-0 (leg-3) — claude-memory ingest: parser rules, endpoint validation,
upsert semantics (PG-gated e2e in CI)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from claude_memory_ingest import (  # noqa: E402
    _mem_type,
    _parse_frontmatter,
    harvest_index,
)


def test_frontmatter_both_variants(tmp_path):
    old = "---\nname: A Title\ndescription: d\ntype: feedback\noriginSessionId: s1\n---\nBody"
    fm, body = _parse_frontmatter(old)
    assert fm["name"] == "A Title" and fm["type"] == "feedback" and body == "Body"
    new = "---\nname: some-slug\ndescription: d\nmetadata:\n  node_type: memory\n  type: project\n  originSessionId: s2\n---\nB2"
    fm2, body2 = _parse_frontmatter(new)
    assert fm2["metadata"]["type"] == "project"
    assert fm2["metadata"]["originSessionId"] == "s2"
    assert body2 == "B2"


def test_mem_type_fallback_is_filename_prefix():
    assert _mem_type({}, "project_foo") == "project"
    assert _mem_type({}, "reference_bar") == "reference"
    assert _mem_type({"type": "feedback"}, "project_x") == "feedback"
    assert _mem_type({"metadata": {"type": "user"}}, "project_x") == "user"


def test_harvest_index_rank_hook_and_dupe_collapse(tmp_path):
    (tmp_path / "MEMORY.md").write_text(
        "- [T1](a_one.md) — hook one\n"
        "- [T2](b_two.md) — hook two\n"
        "- [T1 again](a_one.md) — dupe hook\n",
        encoding="utf-8",
    )
    idx = harvest_index(tmp_path)
    assert idx["a_one"] == (1, "hook one", "T1")  # first occurrence wins
    assert idx["b_two"][0] == 2


@pytest.mark.asyncio
async def test_ingest_validation_matrix():
    from backend.routers.claude_memories import IngestItem, _ingest_one

    class _NoDB:
        async def fetchrow(self, *_a, **_kw):
            raise AssertionError("invalid items must not reach the DB")

    bad = IngestItem(slug="Bad Slug!", title="", mem_type="nope", body=" ")
    out = await _ingest_one(_NoDB(), bad, actor="t")
    assert out["action"] == "rejected"
    assert set(out["reasons"]) >= {"bad_slug", "bad_mem_type", "empty_title", "empty_body"}


# ── PG-gated: created → unchanged → revised; published never clobbered ──────

@pytest.mark.asyncio
async def test_upsert_semantics_end_to_end(pg_test_conn):
    from backend.routers.claude_memories import IngestItem, _ingest_one

    item = IngestItem(slug="feedback_x", title="T", mem_type="feedback",
                      body="v1", rank=3, hook="h")
    r1 = await _ingest_one(pg_test_conn, item, actor="t")
    assert r1["action"] == "created"
    r2 = await _ingest_one(pg_test_conn, item, actor="t")
    assert r2["action"] == "unchanged"
    r3 = await _ingest_one(
        pg_test_conn,
        IngestItem(slug="feedback_x", title="T", mem_type="feedback", body="v2"),
        actor="t",
    )
    assert r3["action"] == "revised"
    row = await pg_test_conn.fetchrow(
        "SELECT state, rank, hook, published_version_id FROM claude_memory_state "
        "WHERE slug = 'feedback_x'"
    )
    assert row["state"] == "quarantined"
    assert row["rank"] == 3 and row["hook"] == "h"  # COALESCE preserved
    assert row["published_version_id"] is None
    n = await pg_test_conn.fetchval(
        "SELECT COUNT(*) FROM claude_memory_versions WHERE slug = 'feedback_x'"
    )
    assert n == 2  # append-only revisions
