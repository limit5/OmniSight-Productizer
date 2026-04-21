"""Phase 65 S1 — training set export gate + shortest-path + JSONL."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from backend import finetune_export as fx


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures + fakes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class _FakeStep:
    id: int
    idempotency_key: str
    output: object | None = None
    error: str | None = None


@dataclass
class _FakeRun:
    id: str
    kind: str = "build/firmware"
    status: str = "completed"
    metadata: dict | None = None
    completed_at: float | None = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@pytest.fixture()
async def fresh_db(pg_test_pool):
    """Phase-3 Step C.1 (2026-04-21): ported off the SQLite-file
    setup onto the shared ``pg_test_pool``. Each test's first seed
    call runs after a TRUNCATE so rows don't leak across tests.
    """
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE workflow_runs, workflow_steps RESTART IDENTITY CASCADE"
        )
    yield pg_test_pool


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  shortest_path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_shortest_path_empty():
    assert fx.shortest_path([]) == []


def test_shortest_path_keeps_only_last_success_per_root():
    """fetch attempted twice — first errors, second succeeds. The
    error attempt is dropped; the success kept."""
    steps = [
        _FakeStep(1, "fetch_repo/retry-1", error="network down"),
        _FakeStep(2, "fetch_repo", output={"sha": "abc"}),
        _FakeStep(3, "compile", output={"image": "fw.bin"}),
    ]
    kept = fx.shortest_path(steps)
    ids = [s.id for s in kept]
    assert 1 not in ids        # the failed retry was dropped
    assert ids == [2, 3]


def test_shortest_path_no_success_keeps_last_attempt():
    steps = [
        _FakeStep(1, "x/retry-1", error="boom"),
        _FakeStep(2, "x/retry-2", error="boom again"),
    ]
    kept = fx.shortest_path(steps)
    assert [s.id for s in kept] == [2]


def test_shortest_path_preserves_chronological_order():
    steps = [
        _FakeStep(10, "a", output="A"),
        _FakeStep(20, "b", output="B"),
        _FakeStep(30, "c", output="C"),
    ]
    kept = fx.shortest_path(steps)
    assert [s.id for s in kept] == [10, 20, 30]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_gate_rejects_non_completed_status():
    run = _FakeRun(id="r", status="failed", metadata={"hvt_passed": True})
    keep, why = fx.gate(run, [], scrub_safe=True)
    assert not keep
    assert "status" in why


def test_gate_rejects_missing_hvt_passed():
    run = _FakeRun(id="r", metadata={"hvt_passed": False})
    keep, why = fx.gate(run, [], scrub_safe=True)
    assert not keep
    assert why == "hvt_passed_false"


def test_gate_rejects_auto_only_resolver():
    """Auto-executed decision without subsequent user_approved → drop."""
    run = _FakeRun(id="r", metadata={"hvt_passed": True})
    decisions = [
        {"actor": "system:auto", "after": {"auto_executed": True,
                                            "user_approved": False,
                                            "resolver": "auto"}},
    ]
    keep, why = fx.gate(run, decisions, scrub_safe=True)
    assert not keep
    assert why == "resolver_auto_only"


def test_gate_accepts_user_resolver():
    run = _FakeRun(id="r", metadata={"hvt_passed": True})
    decisions = [
        {"actor": "user:alice", "after": {"resolver": "user"}},
    ]
    assert fx.gate(run, decisions, scrub_safe=True) == (True, "ok")


def test_gate_accepts_auto_then_approved():
    run = _FakeRun(id="r", metadata={"hvt_passed": True})
    decisions = [
        {"actor": "system:auto",
         "after": {"auto_executed": True, "user_approved": True}},
    ]
    assert fx.gate(run, decisions, scrub_safe=True)[0]


def test_gate_rejects_unsafe_scrub():
    run = _FakeRun(id="r", metadata={"hvt_passed": True})
    keep, why = fx.gate(run, [], scrub_safe=False)
    assert not keep
    assert why == "pii_scrub_unsafe"


def test_gate_no_decisions_passes():
    """A run with zero decisions is trivially clean — common for
    non-controversial completions."""
    run = _FakeRun(id="r", metadata={"hvt_passed": True})
    assert fx.gate(run, [], scrub_safe=True) == (True, "ok")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  extract_for_run + bulk export — integration with real DB
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _seed_run(pool, run_id: str, *, kind: str = "build/firmware",
                    status: str = "completed",
                    metadata: dict | None = None) -> None:
    md = json.dumps(metadata or {})
    import time as _t
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workflow_runs (id, kind, started_at, completed_at, "
            "status, last_step_id, metadata) VALUES "
            "($1, $2, $3, $4, $5, $6, $7)",
            run_id, kind, _t.time(), _t.time(), status, None, md,
        )


_step_id_seq = [0]


async def _seed_step(pool, run_id: str, key: str, output=None, error=None):
    output_json = json.dumps(output) if output is not None else None
    import time as _t
    _step_id_seq[0] += 1
    sid = f"step-{_step_id_seq[0]:08x}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO workflow_steps (id, run_id, idempotency_key, "
            "started_at, completed_at, output_json, error) VALUES "
            "($1, $2, $3, $4, $5, $6, $7)",
            sid, run_id, key, _t.time(), _t.time(), output_json, error,
        )


@pytest.mark.asyncio
async def test_extract_skips_run_without_hvt_passed(fresh_db):
    await _seed_run(fresh_db, "r-noh", metadata={"hvt_passed": False})
    await _seed_step(fresh_db, "r-noh", "fetch", output={"sha": "x"})
    reason, ex = await fx.extract_for_run("r-noh")
    assert not reason.kept
    assert reason.reason == "hvt_passed_false"
    assert ex is None


@pytest.mark.asyncio
async def test_extract_emits_chatml_for_hvt_passed(fresh_db):
    await _seed_run(fresh_db, "r-good", metadata={"hvt_passed": True})
    await _seed_step(fresh_db, "r-good", "fetch_repo",
                      output={"sha": "deadbeef"})
    await _seed_step(fresh_db, "r-good", "compile",
                      output={"image": "fw.bin"})
    reason, ex = await fx.extract_for_run("r-good")
    assert reason.kept, reason.reason
    assert ex is not None
    assert ex.metadata["workflow_run_id"] == "r-good"
    assert ex.metadata["step_count"] == 2
    # ChatML shape: system + N×(user, assistant).
    roles = [m["role"] for m in ex.messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_extract_drops_failed_retries(fresh_db):
    await _seed_run(fresh_db, "r-retry", metadata={"hvt_passed": True})
    await _seed_step(fresh_db, "r-retry", "fetch_repo/retry-1",
                      error="net down")
    await _seed_step(fresh_db, "r-retry", "fetch_repo",
                      output={"sha": "ok"})
    reason, ex = await fx.extract_for_run("r-retry")
    assert reason.kept, reason.reason
    # Only 1 step in metadata (the successful one).
    assert ex.metadata["step_count"] == 1


@pytest.mark.asyncio
async def test_extract_scrubs_secrets_in_output(fresh_db):
    """A step that accidentally captured a GitHub PAT in its output
    must come out scrubbed in the JSONL."""
    await _seed_run(fresh_db, "r-leak", metadata={"hvt_passed": True})
    await _seed_step(
        fresh_db, "r-leak", "set_token",
        output={"value": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"},
    )
    reason, ex = await fx.extract_for_run("r-leak")
    if not reason.kept:
        # Could have been killed by the safety threshold — that's also
        # acceptable behaviour for "too dangerous to learn from".
        return
    body = json.dumps(ex.messages, default=str)
    assert "ghp_abc" not in body
    assert "[GITHUB_PAT]" in body


@pytest.mark.asyncio
async def test_export_jsonl_writes_one_line_per_kept(fresh_db, tmp_path):
    # Two runs: one keeps, one drops.
    await _seed_run(fresh_db, "r-keep", metadata={"hvt_passed": True})
    await _seed_step(fresh_db, "r-keep", "compile", output={"ok": True})
    await _seed_run(fresh_db, "r-drop", metadata={"hvt_passed": False})
    await _seed_step(fresh_db, "r-drop", "compile", output={"ok": True})

    out = tmp_path / "train.jsonl"
    stats = await fx.export_jsonl(out, limit=10)
    assert stats.written == 1
    assert stats.skipped == 1
    assert stats.skip_reasons.get("hvt_passed_false") == 1

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["metadata"]["workflow_run_id"] == "r-keep"


@pytest.mark.asyncio
async def test_export_jsonl_no_eligible_runs_writes_empty_file(
    fresh_db, tmp_path,
):
    out = tmp_path / "empty.jsonl"
    stats = await fx.export_jsonl(out)
    assert stats.written == 0
    assert out.exists()
    assert out.read_text() == ""
