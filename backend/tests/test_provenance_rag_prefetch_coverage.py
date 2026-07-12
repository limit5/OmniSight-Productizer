"""OP-2621 — episodic provenance coverage for RAG prefetch reads.

Offline tests: the function-local DB imports are patched at their source
modules, and the access-count touch is disabled so no Postgres is needed.
"""

from __future__ import annotations

import pytest

import backend.db
import backend.db_pool
import backend.rag_prefetch as rag_prefetch
from backend.agents import provenance
from backend.agents.provenance import (
    Attestation,
    active_collector,
    digest,
    provenance_scope,
)


class _FakeConn:
    pass


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
        "error_signature": "undefined reference to codec_init",
        "solution": "link libcodec before the platform archive",
        "quality_score": 0.99,
        "soc_vendor": "",
        "sdk_version": "",
        "verified": True,
        "source": "service_gerrit_merge",
        "verification_authority": "gerrit",
        "tenant_id": "t-acme",
        "visibility": "tenant_shared",
    },
    {
        "id": "episode-2",
        "error_signature": "undefined reference to codec_open",
        "solution": "enable the codec feature before rebuilding",
        "quality_score": 0.98,
        "soc_vendor": "",
        "sdk_version": "",
        "verified": True,
        "source": "service_gerrit_merge",
        "verification_authority": "gerrit",
        "tenant_id": "t-acme",
        "visibility": "tenant_shared",
    },
]

_EXPECTED_BLOCK = """<related_past_solutions>
  <solution id='episode-1' quality=0.99>
    signature: undefined reference to codec_init
    solution: |
      link libcodec before the platform archive
  </solution>
  <solution id='episode-2' quality=0.98>
    signature: undefined reference to codec_open
    solution: |
      enable the codec feature before rebuilding
  </solution>
</related_past_solutions>"""


def _patch_prefetch_dependencies(monkeypatch):
    calls: list[dict] = []

    monkeypatch.delenv("OMNISIGHT_RAG_MIN_CONFIDENCE", raising=False)
    monkeypatch.delenv("OMNISIGHT_RAG_MIN_COSINE", raising=False)

    async def fake_search(_conn, query, **kwargs):
        calls.append({"query": query, **kwargs})
        return _ROWS

    async def no_touch(_hits):
        return None

    monkeypatch.setattr(
        backend.db, "search_verified_tenant_solutions", fake_search,
    )
    monkeypatch.setattr(
        backend.db_pool, "get_pool", lambda: _FakePool(_FakeConn()),
    )
    monkeypatch.setattr(rag_prefetch, "_touch_hits", no_touch)
    return calls


@pytest.mark.asyncio
async def test_prefetch_for_error_records_each_verified_hit_in_active_scope(
    monkeypatch,
) -> None:
    calls = _patch_prefetch_dependencies(monkeypatch)

    with provenance_scope() as col:
        out = await rag_prefetch.prefetch_for_error(
            "ld: undefined reference to codec_init",
            tenant_id="t-acme",
        )

    assert out == _EXPECTED_BLOCK
    assert len(calls) == 1
    assert calls[0]["tenant_id"] == "t-acme"
    assert len(col._records) == 2
    assert [record.source_kind for record in col._records] == [
        provenance.EPISODIC,
        provenance.EPISODIC,
    ]
    assert {record.source_id for record in col._records} == {
        row["id"] for row in _ROWS
    }
    assert [record.attestation for record in col._records] == [
        Attestation.DB_VERIFIED_GERRIT,
        Attestation.DB_VERIFIED_GERRIT,
    ]
    assert [record.content_digest for record in col._records] == [
        digest(row["solution"]) for row in _ROWS
    ]


@pytest.mark.asyncio
async def test_prefetch_for_error_without_scope_returns_same_output_noop(
    monkeypatch,
) -> None:
    _patch_prefetch_dependencies(monkeypatch)
    seen_collectors = []
    real_record_episodic = rag_prefetch.record_episodic

    def record_spy(collector, row, exposed_text):
        seen_collectors.append(collector)
        real_record_episodic(collector, row, exposed_text)

    monkeypatch.setattr(rag_prefetch, "record_episodic", record_spy)
    assert active_collector() is None

    out = await rag_prefetch.prefetch_for_error(
        "ld: undefined reference to codec_init",
        tenant_id="t-acme",
    )

    assert out == _EXPECTED_BLOCK
    assert seen_collectors == [None, None]
    assert active_collector() is None


@pytest.mark.asyncio
async def test_sandbox_prefetch_records_only_verified_attestations(
    monkeypatch,
) -> None:
    calls = _patch_prefetch_dependencies(monkeypatch)

    with provenance_scope() as col:
        out = await rag_prefetch.prefetch_for_sandbox_error(
            "ld: undefined reference to codec_init",
            tenant_id="t-acme",
        )

    assert out is not None
    assert "<system_auto_prefetch>" in out
    assert calls[0]["tenant_id"] == "t-acme"
    assert calls[0]["min_quality"] == 0.85
    assert len(col._records) == 2
    assert {record.attestation for record in col._records} == {
        Attestation.DB_VERIFIED_GERRIT
    }
    assert all(
        record.attestation
        not in {Attestation.UNATTESTED, Attestation.DB_QUARANTINED}
        for record in col._records
    )
