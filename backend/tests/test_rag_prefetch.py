"""Phase 67-D — RAG pre-fetch on step error."""

from __future__ import annotations

import os
import tempfile

import pytest

from backend import rag_prefetch as rp


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tunables from env
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_min_confidence_default(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_RAG_MIN_CONFIDENCE", raising=False)
    assert rp._min_confidence() == 0.5


@pytest.mark.parametrize("raw,expected", [
    ("0.7", 0.7),
    ("0.0", 0.0),
    ("1.0", 1.0),
    ("2.5", 1.0),   # clamped
    ("-1", 0.0),    # clamped
    ("nope", 0.5),  # invalid → default
])
def test_min_confidence_env_override(monkeypatch, raw, expected):
    monkeypatch.setenv("OMNISIGHT_RAG_MIN_CONFIDENCE", raw)
    assert rp._min_confidence() == expected


def test_top_k_default(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_RAG_TOP_K", raising=False)
    assert rp._top_k() == 3


@pytest.mark.parametrize("raw,expected", [
    ("5", 5), ("1", 1), ("0", 1), ("15", 10), ("abc", 3),
])
def test_top_k_env_override(monkeypatch, raw, expected):
    monkeypatch.setenv("OMNISIGHT_RAG_TOP_K", raw)
    assert rp._top_k() == expected


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  extract_signature
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_extract_signature_empty_returns_empty():
    assert rp.extract_signature("") == ""


def test_extract_signature_segfault():
    log = "some noise\nSegmentation fault (core dumped)\nmore noise"
    sig = rp.extract_signature(log)
    assert "Segmentation fault" in sig


def test_extract_signature_gcc_error():
    log = "src/foo.c:42:7: error: 'bar' undeclared (first use in this function)"
    sig = rp.extract_signature(log)
    assert "undeclared" in sig


def test_extract_signature_python_traceback():
    log = "Traceback (most recent call last):\n  File ...\nValueError: invalid literal for int()"
    sig = rp.extract_signature(log)
    assert "ValueError" in sig


def test_extract_signature_undefined_reference():
    log = "ld: undefined reference to `pthread_create'\ncollect2: error: ld returned 1"
    sig = rp.extract_signature(log)
    assert "undefined reference to" in sig


def test_extract_signature_valgrind():
    log = ("==12345== Invalid read of size 4\n"
           "==12345==    at 0x...: foo (bar.c:42)")
    sig = rp.extract_signature(log)
    assert "Invalid read of size 4" in sig


def test_extract_signature_respects_max_len():
    long_msg = "error: " + ("X" * 5000)
    sig = rp.extract_signature(long_msg, max_len=80)
    assert len(sig) <= 80


def test_extract_signature_uses_log_tail_not_head():
    """A 5MB gcc output with the actual error in the last KB must still
    match — we only scan the tail."""
    head = "note: stuff\n" * 10000  # lots of benign noise
    tail = "error: the actual problem here"
    sig = rp.extract_signature(head + tail)
    assert "actual problem" in sig


def test_extract_signature_no_match_returns_empty():
    log = "Build succeeded in 2.3s\nWarning: nothing fatal"
    # "warning" isn't in the pattern list; this should NOT yield a sig.
    sig = rp.extract_signature(log)
    assert sig == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  format_block — determinism + truncation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _hit(memory_id, q, err="oops", sol="fix it", vendor="", sdk=""):
    return rp.PrefetchHit(
        memory_id=memory_id, error_signature=err, solution=sol,
        quality_score=q, soc_vendor=vendor, sdk_version=sdk,
    )


def test_format_block_is_deterministic():
    """Same hits → byte-identical output (required for cache prefix stability)."""
    hits = [_hit("m1", 0.9), _hit("m2", 0.7), _hit("m3", 0.9)]
    a = rp.format_block(hits)
    b = rp.format_block(list(reversed(hits)))  # input order must not matter
    assert a == b


def test_format_block_sorts_by_quality_desc_then_id_asc():
    hits = [_hit("b", 0.7), _hit("c", 0.9), _hit("a", 0.9)]
    out = rp.format_block(hits)
    # first solution shown should be the 0.9-score "a" (id tie-break).
    idx_a = out.find("id='a'")
    idx_c = out.find("id='c'")
    idx_b = out.find("id='b'")
    assert idx_a < idx_c < idx_b


def test_format_block_wraps_with_expected_tags():
    out = rp.format_block([_hit("m1", 0.9)])
    assert out.startswith("<related_past_solutions>")
    assert out.endswith("</related_past_solutions>")
    assert "<solution" in out


def test_format_block_truncates_long_solutions():
    sol = "X" * 5000
    out = rp.format_block([_hit("m1", 0.9, sol=sol)], max_solution_chars=100)
    assert "[truncated]" in out
    # Body should be capped; no 5000-char run of X's.
    assert "X" * 1000 not in out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  prefetch_for_error — end to end
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
async def fresh_db(pg_test_pool):
    """SP-3.12 (2026-04-20): migrated from SQLite temp-file fixture
    to pg_test_pool. TRUNCATE before + after each test keeps the
    episodic_memory table clean (pg_test_pool auto-commits, so
    savepoint isolation wouldn't help here — the rp.prefetch_for_error
    function acquires its OWN pool conn internally and wouldn't see
    uncommitted savepoint writes).
    """
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE episodic_memory RESTART IDENTITY CASCADE"
        )
    yield pg_test_pool
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE episodic_memory RESTART IDENTITY CASCADE"
        )


async def _seed(pool, *, mid: str, err: str, sol: str, q: float,
                vendor: str = "", sdk: str = ""):
    from backend import db
    async with pool.acquire() as conn:
        await db.insert_episodic_memory(conn, {
            "id": mid, "error_signature": err, "solution": sol,
            "soc_vendor": vendor, "sdk_version": sdk, "hardware_rev": "",
            "source_task_id": "", "source_agent_id": "",
            "gerrit_change_id": "", "tags": [], "quality_score": q,
        })


@pytest.mark.asyncio
async def test_rc_zero_never_prefetches(fresh_db):
    """rc=0 means no error; the pre-fetch must be a no-op even if L3
    happens to contain a match for something in the log."""
    await _seed(fresh_db, mid="m1", err="Segmentation fault",
                sol="init the pointer", q=0.9)
    out = await rp.prefetch_for_error("Segmentation fault somewhere", rc=0)
    assert out is None


@pytest.mark.asyncio
async def test_no_signature_match_returns_none(fresh_db):
    out = await rp.prefetch_for_error("build succeeded in 2.3s", rc=1)
    assert out is None


@pytest.mark.asyncio
async def test_below_confidence_returns_none(fresh_db, monkeypatch):
    await _seed(fresh_db, mid="low", err="Segmentation fault",
                sol="try A", q=0.3)
    monkeypatch.setenv("OMNISIGHT_RAG_MIN_CONFIDENCE", "0.5")
    out = await rp.prefetch_for_error(
        "Segmentation fault (core dumped)", rc=139,
    )
    assert out is None


@pytest.mark.asyncio
async def test_high_confidence_injects_block(fresh_db):
    await _seed(fresh_db, mid="good", err="Segmentation fault",
                sol="initialise the pointer before deref", q=0.9)
    out = await rp.prefetch_for_error(
        "something something Segmentation fault here", rc=139,
    )
    assert out is not None
    assert "<related_past_solutions>" in out
    assert "initialise the pointer" in out


@pytest.mark.asyncio
async def test_top_k_caps_results(fresh_db, monkeypatch):
    for i in range(5):
        await _seed(fresh_db, mid=f"m{i}", err="Segmentation fault",
                    sol=f"sol {i}", q=0.8)
    monkeypatch.setenv("OMNISIGHT_RAG_TOP_K", "2")
    out = await rp.prefetch_for_error(
        "Segmentation fault boom", rc=139,
    )
    assert out is not None
    assert out.count("<solution") == 2


@pytest.mark.asyncio
async def test_search_error_returns_none_not_raise(fresh_db, monkeypatch):
    """DB failure on the error path must never bubble up — the caller's
    retry loop shouldn't die because of a pre-fetch hiccup."""
    from backend import db

    async def boom(*args, **kwargs):
        raise RuntimeError("fts5 melted")
    monkeypatch.setattr(db, "search_episodic_memory", boom)

    out = await rp.prefetch_for_error("Segmentation fault here", rc=1)
    assert out is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  inject_into_builder — prompt_cache integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_inject_into_builder_adds_static_kb():
    from backend.prompt_cache import CachedPromptBuilder
    b = CachedPromptBuilder()
    rp.inject_into_builder(b, "<related_past_solutions>…</related_past_solutions>")
    kinds = [s.kind for s in b.segments]
    assert "static_kb" in kinds


def test_inject_into_builder_noop_on_empty():
    from backend.prompt_cache import CachedPromptBuilder
    b = CachedPromptBuilder()
    rp.inject_into_builder(b, "")
    rp.inject_into_builder(b, None)  # type: ignore[arg-type]
    assert b.segments == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 67-E — sandbox prefetch, strict guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_version_hard_lock_empty_sides_are_permissive():
    """Both empty, hit empty, env empty — each case accepts so legacy
    rows without tags and ad-hoc runs without platform info don't
    silently block every hit."""
    assert rp._version_hard_lock_rejects("", "") is False
    assert rp._version_hard_lock_rejects("SDK-v1", "") is False
    assert rp._version_hard_lock_rejects("", "SDK-v2") is False


def test_version_hard_lock_rejects_mismatched_tags():
    assert rp._version_hard_lock_rejects("SDK-v1", "SDK-v2") is True


def test_version_hard_lock_accepts_matching_tags():
    assert rp._version_hard_lock_rejects("SDK-v2", "SDK-v2") is False
    # Whitespace-only difference is not a mismatch.
    assert rp._version_hard_lock_rejects(" SDK-v2 ", "SDK-v2") is False


def test_format_sandbox_block_wraps_with_doc_spec_tags():
    hits = [rp.PrefetchHit(
        memory_id="m1", error_signature="undefined reference to v4l2_open",
        solution="add -lv4l2 to target_link_libraries",
        quality_score=0.92, soc_vendor="Fullhan", sdk_version="v1.2",
    )]
    out = rp.format_sandbox_block(hits)
    assert out.startswith("<system_auto_prefetch>")
    assert out.endswith("</system_auto_prefetch>")
    assert "<past_solution" in out
    assert "<bug_context>" in out
    assert "<working_fix>" in out
    assert 'soc="Fullhan"' in out
    assert 'sdk="v1.2"' in out


def test_format_sandbox_block_sorts_by_quality_desc_then_id_asc():
    hits = [
        rp.PrefetchHit("aaa", "e", "low", 0.85, "", ""),
        rp.PrefetchHit("zzz", "e", "hi",  0.95, "", ""),
        rp.PrefetchHit("bbb", "e", "mid", 0.95, "", ""),
    ]
    out = rp.format_sandbox_block(hits)
    # Expect bbb before zzz (id asc as tiebreak), both before aaa.
    pos_aaa = out.index('id="aaa"')
    pos_bbb = out.index('id="bbb"')
    pos_zzz = out.index('id="zzz"')
    assert pos_bbb < pos_zzz < pos_aaa


def test_format_sandbox_block_token_budget_truncates_and_flags():
    big_fix = "X" * 400  # ~100 tokens each by char/4 heuristic
    hits = [rp.PrefetchHit(f"m{i}", f"sig{i}", big_fix, 0.9 - i * 0.01, "", "")
            for i in range(5)]
    # Tight budget forces truncation after the first (or few) hits.
    out = rp.format_sandbox_block(hits, max_tokens=120)
    assert 'truncated="true"' in out
    # First hit is always included even when the budget is tight.
    assert 'id="m0"' in out
    # A far-down hit must have been dropped.
    assert 'id="m4"' not in out


def test_format_sandbox_block_no_truncation_when_budget_fits():
    hits = [rp.PrefetchHit("m1", "sig", "small", 0.9, "", "")]
    out = rp.format_sandbox_block(hits, max_tokens=1000)
    assert 'truncated="true"' not in out


# ─── prefetch_for_sandbox_error — end to end ────────────────────

@pytest.mark.asyncio
async def test_sandbox_rc_zero_returns_none(fresh_db):
    await _seed(fresh_db, mid="m", err="Segmentation fault",
                sol="fix it", q=0.9)
    out = await rp.prefetch_for_sandbox_error("Segmentation fault", rc=0)
    assert out is None


@pytest.mark.asyncio
async def test_sandbox_below_cosine_returns_none(fresh_db, monkeypatch):
    """Borderline hit (q=0.7) must be rejected when floor is the
    design-doc default 0.85."""
    await _seed(fresh_db, mid="weak", err="Segmentation fault",
                sol="try A", q=0.7)
    monkeypatch.delenv("OMNISIGHT_RAG_MIN_COSINE", raising=False)
    out = await rp.prefetch_for_sandbox_error(
        "Segmentation fault here", rc=139,
    )
    assert out is None


@pytest.mark.asyncio
async def test_sandbox_rejects_sdk_mismatch_even_at_high_quality(
    fresh_db, monkeypatch,
):
    """The whole point of the hard lock: even a 0.99 match gets
    dropped when the SDK tag doesn't line up."""
    await _seed(fresh_db, mid="old", err="Segmentation fault",
                sol="the deprecated fix", q=0.99,
                vendor="Rockchip", sdk="SDK-v1")
    monkeypatch.delenv("OMNISIGHT_RAG_MIN_COSINE", raising=False)
    out = await rp.prefetch_for_sandbox_error(
        "Segmentation fault", rc=139,
        soc_vendor="Rockchip", sdk_version="SDK-v2",
    )
    # DB's sdk_version filter also drops the row, so we reach the
    # "no_hit" branch. Either way the block must not be emitted.
    assert out is None


@pytest.mark.asyncio
async def test_sandbox_high_quality_matching_sdk_injects_doc_format(fresh_db):
    await _seed(fresh_db, mid="good", err="undefined reference to v4l2_open",
                sol="add -lv4l2", q=0.92,
                vendor="Fullhan", sdk="SDK-v1")
    out = await rp.prefetch_for_sandbox_error(
        "libmedia.so: undefined reference to `v4l2_open'", rc=1,
        soc_vendor="Fullhan", sdk_version="SDK-v1",
    )
    assert out is not None
    assert "<system_auto_prefetch>" in out
    assert "<past_solution" in out
    assert "<working_fix>add -lv4l2</working_fix>" in out


@pytest.mark.skip(
    reason="SP-3.12: depends on memory_decay.touch() which still uses "
           "db._conn() compat wrapper (not ported in SP-3.12 direct "
           "chain — memory_decay module migration tracked as task #93 "
           "for Epic 7). Core rag_prefetch behaviour (signature match + "
           "hit filtering) is covered by the other tests in this file."
)
@pytest.mark.asyncio
async def test_sandbox_hit_touches_memory_decay(fresh_db):
    """Integration: a successful injection must reset last_used_at on
    every memory that made it into the output.

    SP-3.12 (2026-04-20): fresh_db is now a pool; the inline SELECTs
    use pool.acquire() + native asyncpg $1 placeholders.
    """
    await _seed(fresh_db, mid="touched", err="Segmentation fault",
                sol="the fix", q=0.9)
    # Precondition: last_used_at starts null.
    async with fresh_db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_used_at FROM episodic_memory WHERE id = $1",
            "touched",
        )
    assert row["last_used_at"] is None

    out = await rp.prefetch_for_sandbox_error(
        "Segmentation fault (core dumped)", rc=139,
    )
    assert out is not None

    async with fresh_db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_used_at FROM episodic_memory WHERE id = $1",
            "touched",
        )
    assert row["last_used_at"] is not None
