"""β-3a (leg-2) — graph-injection spine: provenance kind, read flag,
bearer-token approval semantics, revert-409, and the provenance sink.

The auto-auth DOWNGRADE for LEARNED_ITEM is covered by the (auto-extending)
parametrize in test_u6_memory_capability_contract.py — the reviewed INV-2
boundary pin. Here we lock the seams around it.
"""

from __future__ import annotations

import pytest

from backend.agents import provenance as prov
from backend.learned_item_approval import (
    ApprovalValidationError,
    record_memory_approval,
)


def test_learned_item_is_a_memory_source_kind():
    from backend.agents.auto_auth_policy import (
        _HIGH_INJECTION_SOURCES,
        _MEMORY_SOURCE_KINDS,
    )

    assert prov.LEARNED_ITEM in prov.SOURCE_KINDS
    assert prov.LEARNED_ITEM in _MEMORY_SOURCE_KINDS
    assert prov.LEARNED_ITEM in _HIGH_INJECTION_SOURCES


def test_read_requires_both_flags(monkeypatch):
    from backend import learned_item_loader as lil

    monkeypatch.setenv("OMNISIGHT_LEARNED_ITEM_PROMOTION_ENABLED", "1")
    monkeypatch.delenv("OMNISIGHT_LEARNED_ITEM_READ", raising=False)
    block, result = lil.get_learned_items_block(tenant_id="t", context="")
    assert (block, result) == ("", "kill_switch_off")  # frozen label reused
    monkeypatch.setenv("OMNISIGHT_LEARNED_ITEM_READ", "1")
    _block, result2 = lil.get_learned_items_block(tenant_id="t", context="")
    assert result2 != "kill_switch_off"  # now past the switch (cache-empty path)


# ── bearer-token approval matrix (fake conn) ────────────────────────────────

_NOW = "2026-07-21T00:00:00+00:00"


class _ApprovalConn:
    def __init__(
        self, *, renderer="u4r2", ran_at="2026-07-20T00:00:00+00:00",
        run_hash="a" * 64, later=False, later_reject=False,
    ) -> None:
        self._renderer = renderer
        self._ran_at = ran_at
        self._run_hash = run_hash
        self._later = later
        self._later_reject = later_reject
        self.inserted = False

    async def fetchrow(self, sql, *args):
        if "renderer_version FROM learned_item_versions" in sql:
            return {"renderer_version": self._renderer}
        if "ran_at, live_set_hash FROM memory_eval_runs" in sql:
            return {"ran_at": self._ran_at, "live_set_hash": self._run_hash}
        if "ran_at > $2" in sql:
            return {"?": 1} if self._later else None
        if "decision = 'reject'" in sql:
            return {"?": 1} if self._later_reject else None
        raise AssertionError(f"unexpected fetchrow: {sql[:60]}")

    async def execute(self, sql, *args):
        self.inserted = True


async def _approve(conn, **over):
    kw = dict(
        approval_id="a-1", version_id="v-1", eval_run_id="r-1",
        live_set_hash="a" * 64, approved_by="sora",
    )
    kw.update(over)
    await record_memory_approval(conn, **kw)


@pytest.mark.asyncio
async def test_approval_happy_path_inserts():
    conn = _ApprovalConn()
    await _approve(conn)
    assert conn.inserted is True


@pytest.mark.asyncio
async def test_approval_rejects_stale_renderer():
    with pytest.raises(ApprovalValidationError, match="stale_renderer"):
        await _approve(_ApprovalConn(renderer="u4r1"))


@pytest.mark.asyncio
async def test_approval_rejects_non_latest_run():
    with pytest.raises(ApprovalValidationError, match="eval_run_not_latest"):
        await _approve(_ApprovalConn(later=True))


@pytest.mark.asyncio
async def test_approval_rejects_later_reject():
    with pytest.raises(ApprovalValidationError, match="later_reject_exists"):
        await _approve(_ApprovalConn(later_reject=True))


@pytest.mark.asyncio
async def test_approval_rejects_stale_run():
    with pytest.raises(ApprovalValidationError, match="eval_run_stale"):
        await _approve(_ApprovalConn(ran_at="2026-01-01T00:00:00+00:00"))


@pytest.mark.asyncio
async def test_approval_rejects_live_set_hash_mismatch():
    with pytest.raises(ApprovalValidationError, match="live_set_hash_mismatch"):
        await _approve(_ApprovalConn(run_hash="b" * 64))


# ── the pending-card verdict source (never inferred from stats) ─────────────

def test_card_verdict_from_catch_not_stats():
    from backend.routers.memory_promotion import _card_verdict

    forced = _card_verdict(
        "reject", {"neg_control_catches": ["s::nc01"], "net_flips": 16},
    )
    assert forced["verdict_label"] == "reject (neg-control forced)"
    reverted = _card_verdict("reject", {"reason": "reverted_later"})
    assert reverted["verdict_label"] == "reject (reverted)"
    plain = _card_verdict("insufficient_evidence", "{}")
    assert plain["verdict_label"] == "insufficient_evidence"


# ── the run_with_tools / prompt_loader provenance seam ──────────────────────

def test_prompt_loader_sink_and_ambient_record(monkeypatch):
    from backend import prompt_loader as pl
    from backend import learned_item_loader as lil
    from backend.agents.provenance import provenance_scope

    monkeypatch.setattr(
        lil, "get_learned_items_block",
        lambda **_kw: ("LEARNED BLOCK", "non_empty"),
    )
    sink: list = []
    with provenance_scope() as col:
        pl.build_system_prompt(tenant_id="t-x", learned_provenance_sink=sink)
        recorded = list(col._records)  # pre-seal inspection (test-only)
    assert sink and sink[0][0] == prov.LEARNED_ITEM
    assert "LEARNED BLOCK" in sink[0][2]
    assert any(
        getattr(r, "source_kind", None) == prov.LEARNED_ITEM for r in recorded
    )
