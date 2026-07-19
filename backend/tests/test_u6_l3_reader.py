"""U6-7 — L3 read-path tests (RB4a): loader gating, loud statuses, fence, INV-3.

Offline: a ``_FakeConn`` serves rows sealed with the REAL U6-0b crypto, so the
decrypt → integrity-check → closed-schema render → fence path is exercised end
to end without PG. The live-PG read path (real ``l3_facts`` DDL + RLS) was
hand-verified against the isolated test container during the increment and runs
via the PG-gated store/confirm suites in CI.
"""

from __future__ import annotations

import json

import pytest

from backend.agents import u6_l3_reader as rd
from backend.agents.execution_context import for_human, for_machine
from backend.agents.provenance import EPISODIC, provenance_scope
from backend.agents.u6_fact_schema import Fact, FactType, render_fact
from backend.agents.u6_memory_crypto import seal
from backend.agents.u6_memory_scope import MemoryScope

_FLAG = "OMNISIGHT_SORA_L3_READ"


class _User:
    id = "user-1"
    role = "operator"


def _human_ctx() -> object:
    return for_human(
        user=_User(), tenant_id="omnisight-self", session_id="s1",
        request_id="req-1", message_id="m1", authorization_source="chat",
    )


_SCOPE = MemoryScope(tenant_id="omnisight-self", user_id="user-1")


def _fact(predicate: str = "preferred_ipc", value: str = "named_pipes") -> Fact:
    fact_type = {
        "preferred_ipc": FactType.PREFERENCE,
        "preferred_editor": FactType.PREFERENCE,
        "default_branch": FactType.PROJECT_CONTEXT,
    }[predicate]
    return Fact(
        fact_type=fact_type, subject="user", predicate=predicate, value=value,
        source_span="chat:s1:m1",
    )


def _sealed_row(fact: Fact, fact_id: str, scope: MemoryScope = _SCOPE) -> dict:
    sealed = seal(scope, json.dumps({
        "value": fact.value, "source_span": fact.source_span,
        "fact_type": fact.fact_type.value, "subject": fact.subject,
        "predicate": fact.predicate,
    }))
    return {
        "id": fact_id, "fact_type": fact.fact_type.value, "subject": fact.subject,
        "predicate": fact.predicate, "sealed_ciphertext": sealed.ciphertext,
        "dek_ref": sealed.dek_ref, "sensitivity": fact.sensitivity.value,
        "valid_from": fact.valid_from, "valid_until": fact.valid_until,
        "state": "promoted", "revision": 0,
    }


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Just enough surface for ``u6_l3_store.list_facts``."""

    def __init__(self, rows=None, fetch_error: Exception | None = None):
        self.rows = list(rows or [])
        self.fetch_error = fetch_error
        self.touched = False

    def transaction(self):
        self.touched = True
        return _FakeTxn()

    async def execute(self, sql, *args):
        self.touched = True

    async def fetch(self, sql, *args):
        self.touched = True
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.rows


def _load(ctx, conn, **kw):
    import asyncio

    return asyncio.run(rd.load_l3_prompt_block(ctx, conn=conn, **kw))


# ── gating (the shipped default is a no-op) ──────────────────────────────
def test_flag_off_is_disabled_and_touches_nothing(monkeypatch) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    conn = _FakeConn(fetch_error=AssertionError("must not be touched"))
    out = _load(_human_ctx(), conn)
    assert out.status == "disabled"
    assert out.block == "" and out.fact_ids == ()
    assert conn.touched is False


def test_flag_on_non_human_principal_not_applicable(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "1")
    conn = _FakeConn(fetch_error=AssertionError("must not be touched"))
    ctx = for_machine(service_name="sora", tenant_id="omnisight-self", request_id="r")
    out = _load(ctx, conn)
    assert out.status == "not_applicable"
    assert conn.touched is False
    assert _load(None, conn).status == "not_applicable"


# ── loud outcomes (anti-hollow) ──────────────────────────────────────────
def test_store_error_degrades_loudly(monkeypatch, caplog) -> None:
    monkeypatch.setenv(_FLAG, "1")
    with caplog.at_level("WARNING", logger="backend.agents.u6_l3_reader"):
        out = _load(_human_ctx(), _FakeConn(fetch_error=RuntimeError("pg down")))
    assert out.status == "empty_degraded" and out.block == ""
    assert any("empty_degraded" in r.message for r in caplog.records)


def test_no_pool_degrades_loudly_not_raises(monkeypatch, caplog) -> None:
    # conn=None + uninitialised shared pool (the offline default) — the flag-ON
    # path must degrade LOUDLY, never raise into the chat turn.
    monkeypatch.setenv(_FLAG, "1")
    with caplog.at_level("WARNING", logger="backend.agents.u6_l3_reader"):
        out = _load(_human_ctx(), None)
    assert out.status == "empty_degraded"
    assert any("empty_degraded" in r.message for r in caplog.records)


def test_zero_rows_is_empty_expected_not_degraded(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "1")
    out = _load(_human_ctx(), _FakeConn(rows=[]))
    assert out.status == "empty_expected"
    assert out.block == ""


def test_tampered_row_degrades_whole_read(monkeypatch) -> None:
    # Swap the CLEAR predicate after sealing — the store's key↔value integrity
    # tie must reject it at open; the loader degrades ALL-or-nothing.
    monkeypatch.setenv(_FLAG, "1")
    row = _sealed_row(_fact(), "l3f-tamper")
    row["predicate"] = "preferred_editor"
    good = _sealed_row(_fact("default_branch", "develop"), "l3f-good")
    out = _load(_human_ctx(), _FakeConn(rows=[good, row]))
    assert out.status == "empty_degraded"
    assert out.block == "" and out.fact_ids == ()


def test_loader_is_total_on_arbitrary_conn_faults(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "1")

    class _Broken:
        def transaction(self):
            raise TypeError("hostile conn")

    assert _load(_human_ctx(), _Broken()).status == "empty_degraded"


def test_loader_is_total_even_on_repr_bomb_exceptions(monkeypatch) -> None:
    # The degrade handler must not evaluate anything that can raise — an
    # exception whose __repr__ raises (audit A1) must still degrade cleanly.
    monkeypatch.setenv(_FLAG, "1")

    class _ReprBomb(Exception):
        def __repr__(self):
            raise RuntimeError("repr bomb")

        __str__ = __repr__

    out = _load(_human_ctx(), _FakeConn(fetch_error=_ReprBomb()))
    assert out.status == "empty_degraded"


def test_persona_helper_is_total_even_on_repr_bomb(monkeypatch) -> None:
    import asyncio

    from backend.agents import nodes as nodes_mod

    class _ReprBomb(Exception):
        def __repr__(self):
            raise RuntimeError("repr bomb")

        __str__ = __repr__

    async def _boom(ctx):
        raise _ReprBomb()

    monkeypatch.setattr(
        "backend.agents.u6_l3_reader.load_l3_prompt_block", _boom
    )
    out = asyncio.run(nodes_mod._append_l3_user_memory("P", _human_ctx()))
    assert out == "P"


# ── the loaded path: fence + order + provenance ──────────────────────────
def test_loaded_block_shape_and_order(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "1")
    f1, f2 = _fact(), _fact("default_branch", "develop")
    conn = _FakeConn(rows=[_sealed_row(f1, "l3f-1"), _sealed_row(f2, "l3f-2")])
    out = _load(_human_ctx(), conn, nonce="deadbeef00000000")
    assert out.status == "loaded"
    assert out.fact_ids == ("l3f-1", "l3f-2")
    lines = out.block.split("\n")
    assert lines[0] == rd.BLOCK_HEADER
    assert lines[1] == (
        "----- BEGIN UNTRUSTED USER-MEMORY DATA [u6l3-fence-deadbeef00000000] -----"
    )
    assert lines[2:5] == rd.DATAMARK_PRELUDE.split("\n")
    assert lines[5] == render_fact(f1) == "user preferred_ipc named_pipes"
    assert lines[6] == render_fact(f2) == "user default_branch develop"
    assert lines[7] == (
        "----- END UNTRUSTED USER-MEMORY DATA [u6l3-fence-deadbeef00000000] -----"
    )
    assert len(lines) == 8


def test_default_nonce_is_fresh_per_render(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "1")
    conn = _FakeConn(rows=[_sealed_row(_fact(), "l3f-1")])
    a = _load(_human_ctx(), conn)
    b = _load(_human_ctx(), conn)
    assert a.status == b.status == "loaded"
    assert a.block != b.block  # attacker-unpredictable fence tag


def test_rendered_line_is_structurally_never_a_fence_line(monkeypatch) -> None:
    # A value CAN be "-----" under the short-token grammar; the rendered line is
    # still exactly 3 space-separated tokens vs the fence lines' 7 — and the
    # block must contain exactly one BEGIN and one END line.
    monkeypatch.setenv(_FLAG, "1")
    evil = _fact("preferred_editor", "-----")
    out = _load(
        _human_ctx(), _FakeConn(rows=[_sealed_row(evil, "l3f-e")]), nonce="ab" * 8
    )
    assert out.status == "loaded"
    lines = out.block.split("\n")
    assert lines[5] == "user preferred_editor -----"
    begins = [ln for ln in lines if ln.startswith("----- BEGIN ")]
    ends = [ln for ln in lines if ln.startswith("----- END ")]
    assert len(begins) == 1 and len(ends) == 1
    assert all(len(ln.split(" ")) == 3 for ln in lines[5:6])


def test_inv3_provenance_recorded_per_injected_fact(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "1")
    f1, f2 = _fact(), _fact("default_branch", "develop")
    conn = _FakeConn(rows=[_sealed_row(f1, "l3f-1"), _sealed_row(f2, "l3f-2")])
    with provenance_scope() as pcol:
        out = _load(_human_ctx(), conn)
        recs = list(pcol._records)
    assert out.status == "loaded"
    assert [(r.source_kind, r.source_id) for r in recs] == [
        (EPISODIC, "l3f-1"), (EPISODIC, "l3f-2"),
    ]
    assert all(r.attestation.value == "unattested" for r in recs)
    assert all(r.tenant_id == "omnisight-self" for r in recs)


def test_no_active_collector_is_fine(monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "1")
    out = _load(_human_ctx(), _FakeConn(rows=[_sealed_row(_fact(), "l3f-1")]))
    assert out.status == "loaded"  # record_content(None, …) is a no-op


# ── outcome record invariants ────────────────────────────────────────────
def test_block_only_on_loaded() -> None:
    with pytest.raises(ValueError):
        rd.L3PromptBlock(status="empty_expected", block="sneaky")
    with pytest.raises(ValueError):
        rd.L3PromptBlock(status="nonsense")


# ── the nodes.py wiring helper (byte-identical when OFF) ─────────────────
def test_persona_byte_identical_when_flag_off(monkeypatch) -> None:
    import asyncio

    from backend.agents.nodes import _append_l3_user_memory

    monkeypatch.delenv(_FLAG, raising=False)
    persona = "SECURITY PRELUDE\n\npersona body\n\nGuidelines: …"
    out = asyncio.run(_append_l3_user_memory(persona, _human_ctx()))
    assert out == persona  # byte-identical — the shipped default changes nothing


def test_persona_appends_block_below_everything_when_loaded(monkeypatch) -> None:
    import asyncio

    from backend.agents import nodes as nodes_mod

    async def _fake_load(ctx):
        return rd.L3PromptBlock(
            status="loaded", block="MEMBLOCK", fact_ids=("l3f-1",)
        )

    monkeypatch.setattr(
        "backend.agents.u6_l3_reader.load_l3_prompt_block", _fake_load
    )
    persona = "SECURITY\n\nGuidelines"
    out = asyncio.run(nodes_mod._append_l3_user_memory(persona, _human_ctx()))
    assert out == "SECURITY\n\nGuidelines\n\nMEMBLOCK"  # appended at the BOTTOM


def test_persona_helper_is_total(monkeypatch) -> None:
    import asyncio

    from backend.agents import nodes as nodes_mod

    async def _boom(ctx):
        raise RuntimeError("loader exploded")

    monkeypatch.setattr(
        "backend.agents.u6_l3_reader.load_l3_prompt_block", _boom
    )
    persona = "P"
    out = asyncio.run(nodes_mod._append_l3_user_memory(persona, _human_ctx()))
    assert out == "P"  # degrade to no-injection, never a broken chat turn
