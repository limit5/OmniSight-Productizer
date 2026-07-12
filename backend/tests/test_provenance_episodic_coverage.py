"""OP-2618 — episodic-read provenance coverage for search_past_solutions.

Offline tests: no Postgres and no model call. The tool's trusted tenant
ContextVar and module-top pool import are patched at their real read points.
"""

from __future__ import annotations

import pytest

from backend.agents import provenance
from backend.agents.provenance import (
    Attestation,
    ProvenanceCollector,
    active_collector,
    digest,
    provenance_scope,
    record_episodic,
)


class _FakeConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        return []

    async def fetchrow(self, sql, *params):
        self.calls.append((sql, params))
        return None


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_a):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


_ROWS = [
    {
        "id": "episode-1",
        "error_signature": "link error " + "x" * 140,
        "solution": "add the missing library " + "a" * 320,
        "soc_vendor": "rockchip",
        "sdk_version": "1.2",
        "hardware_rev": "rev-a",
        "quality_score": 0.9,
        "verified": True,
        "source": "service_gerrit_merge",
        "verification_authority": "gerrit",
        "tenant_id": "t-acme",
        "visibility": "tenant_shared",
    },
    {
        "id": "episode-2",
        "error_signature": "undefined reference to codec_init",
        "solution": "link libcodec before the platform archive",
        "soc_vendor": "",
        "sdk_version": "",
        "hardware_rev": "",
        "quality_score": 0.8,
        "verified": True,
        "source": "service_gerrit_merge",
        "verification_authority": "gerrit",
        "tenant_id": "t-acme",
        "visibility": "tenant_shared",
    },
]


def _block(index: int, row: dict) -> str:
    vendor_info = f" | vendor={row['soc_vendor']}" if row.get("soc_vendor") else ""
    sdk_info = f" | sdk={row['sdk_version']}" if row.get("sdk_version") else ""
    hw_info = f" | hw={row['hardware_rev']}" if row.get("hardware_rev") else ""
    score = f" | quality={row.get('quality_score', 0):.1f}"
    return (
        f"  {index}. Error: {row['error_signature'][:120]}\n"
        f"     Solution: {row['solution'][:300]}\n"
        f"     Meta:{vendor_info}{sdk_info}{hw_info}{score}\n"
    )


def _expected_output() -> str:
    return "\n".join(
        [f"[L3] Found {len(_ROWS)} past solution(s):\n"]
        + [_block(i, row) for i, row in enumerate(_ROWS, 1)]
    )


def _patch_search(monkeypatch):
    from backend import db
    from backend.agents import tools as agent_tools

    calls: list[dict] = []

    async def fake_search(_conn, query, **kwargs):
        calls.append({"query": query, **kwargs})
        return _ROWS

    monkeypatch.setattr(db, "search_verified_tenant_solutions", fake_search)
    monkeypatch.setattr(agent_tools, "get_pool", lambda: _FakePool(_FakeConn()))
    return calls


def test_record_episodic_attests_verified_and_quarantined_rows() -> None:
    record_episodic(None, {}, "ignored")

    col = ProvenanceCollector()
    record_episodic(col, _ROWS[0], "verified text")
    quarantined = {**_ROWS[1], "verified": False}
    record_episodic(col, quarantined, "quarantined text")

    assert len(col._records) == 2
    verified_rec, quarantined_rec = col._records
    assert verified_rec.attestation is Attestation.DB_VERIFIED_GERRIT
    assert verified_rec.source_id == str(_ROWS[0]["id"])
    assert verified_rec.content_digest == digest("verified text")
    assert quarantined_rec.attestation is Attestation.DB_QUARANTINED
    assert quarantined_rec.source_id == str(quarantined["id"])
    assert quarantined_rec.content_digest == digest("quarantined text")


def test_record_episodic_factory_error_latches_failure(monkeypatch) -> None:
    def boom(_row, _text):
        raise RuntimeError("injected attestation failure")

    monkeypatch.setattr(provenance, "attested_episodic_record", boom)
    col = ProvenanceCollector()
    record_episodic(col, _ROWS[0], "text")

    assert col._failed is True
    assert col._records == []
    assert "record_error" in col._omissions


@pytest.mark.asyncio
async def test_search_past_solutions_records_each_result_in_active_scope(
    monkeypatch,
) -> None:
    from backend.agents.anthropic_native_client import _active_execution_context
    from backend.agents.execution_context import for_machine
    from backend.agents.tools import search_past_solutions

    calls = _patch_search(monkeypatch)
    token = _active_execution_context.set(
        for_machine(service_name="t", request_id="r", tenant_id="t-acme")
    )
    try:
        with provenance_scope() as col:
            out = await search_past_solutions.ainvoke(
                {"error_signature": "link error"}
            )
    finally:
        _active_execution_context.reset(token)

    assert out == _expected_output()
    assert out.startswith("[L3] Found 2")
    assert calls == [{
        "query": "link error",
        "tenant_id": "t-acme",
        "soc_vendor": "",
        "sdk_version": "",
        "limit": 3,
    }]
    assert len(col._records) == 2
    assert {rec.source_id for rec in col._records} == {row["id"] for row in _ROWS}
    assert [rec.source_kind for rec in col._records] == [provenance.EPISODIC] * 2
    assert [rec.content_digest for rec in col._records] == [
        digest(_block(i, row)) for i, row in enumerate(_ROWS, 1)
    ]


@pytest.mark.asyncio
async def test_search_past_solutions_without_scope_is_same_output_noop(
    monkeypatch,
) -> None:
    from backend.agents.anthropic_native_client import _active_execution_context
    from backend.agents.execution_context import for_machine
    from backend.agents.tools import search_past_solutions

    _patch_search(monkeypatch)
    assert active_collector() is None
    token = _active_execution_context.set(
        for_machine(service_name="t", request_id="r", tenant_id="t-acme")
    )
    try:
        out = await search_past_solutions.ainvoke({"error_signature": "link error"})
    finally:
        _active_execution_context.reset(token)

    assert out == _expected_output()
    assert active_collector() is None


@pytest.mark.asyncio
async def test_search_past_solutions_without_tenant_fails_closed_before_search(
    monkeypatch,
) -> None:
    from backend.agents.anthropic_native_client import _active_execution_context
    from backend.agents.tools import search_past_solutions

    calls = _patch_search(monkeypatch)
    token = _active_execution_context.set(None)
    try:
        with provenance_scope() as col:
            out = await search_past_solutions.ainvoke(
                {"error_signature": "link error"}
            )
    finally:
        _active_execution_context.reset(token)

    assert out == "[L3] No verified past solutions in scope."
    assert calls == []
    assert col._records == []
