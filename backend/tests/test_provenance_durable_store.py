"""OP-2622 — durable ProvenanceSnapshot store contract tests.

Offline tests prove serialization, fail-closed writes, tenant-scoped helper
calls, immutable manifest handling, and SQLite bootstrap shape.  The final
four tests use the standard PG fixture and skip when ``OMNI_TEST_PG_URL`` is
unset.
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest

from backend import db
from backend.agents import provenance
from backend.agents.provenance import (
    Attestation,
    PgSnapshotRepository,
    ProvenanceRecord,
    ProvenanceSnapshot,
    _build_snapshot,
    _records_from_json,
    _records_to_json,
    digest,
)


def _record(**overrides) -> ProvenanceRecord:
    values = {
        "source_kind": provenance.TOOL_RESULT,
        "source_id": "tool-call-1",
        "content_digest": digest("visible result"),
        "attestation": Attestation.UNATTESTED,
        "verification_status": "none",
        "verification_authority": "",
        "tenant_id": "t-provenance",
        "visibility": "tenant",
    }
    values.update(overrides)
    return ProvenanceRecord(**values)


def _snapshot(snapshot_id: str = "psnap-durable") -> ProvenanceSnapshot:
    return _build_snapshot(
        (_record(),),
        completeness="complete",
        omissions=("digest_truncated",),
        snapshot_id=snapshot_id,
    )


class _AcquireContext:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn
        self.acquire_count = 0

    def acquire(self):
        self.acquire_count += 1
        return _AcquireContext(self._conn)


class _UnusedPool:
    def acquire(self):
        raise AssertionError("pool must not be used")


class _FakeConn:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *params):
        self.calls.append((sql, params))
        return self.responses.pop(0) if self.responses else None


class _UnusedConn:
    async def fetchrow(self, *_args):
        raise AssertionError("connection must not be used")


def test_records_json_roundtrip_preserves_all_record_fields() -> None:
    records = (
        _record(),
        _record(
            source_kind=provenance.EPISODIC,
            source_id="memory-2",
            attestation=Attestation.DB_VERIFIED_GERRIT,
            verification_status="verified",
            verification_authority="gerrit",
            visibility="tenant_shared",
        ),
    )
    assert _records_from_json(_records_to_json(records)) == records


@pytest.mark.asyncio
async def test_persist_rejects_partial_snapshot_without_db_call() -> None:
    partial = _build_snapshot(
        (_record(),),
        completeness="partial",
        omissions=("capture_error",),
        snapshot_id="psnap-partial",
    )
    repo = PgSnapshotRepository(_UnusedPool())
    with pytest.raises(ValueError, match="only complete"):
        await repo.persist(
            partial, tenant_id="t-provenance", model_call_id="model-call-1"
        )


@pytest.mark.asyncio
async def test_repository_persist_calls_db_helper_with_snapshot_value(
    monkeypatch,
) -> None:
    calls: list[tuple[object, dict]] = []

    async def fake_insert(conn, **kwargs):
        calls.append((conn, kwargs))
        return True

    monkeypatch.setattr(db, "insert_provenance_snapshot", fake_insert)
    conn = object()
    snap = _snapshot()
    await PgSnapshotRepository(_Pool(conn)).persist(
        snap,
        tenant_id="t-provenance",
        model_call_id="model-call-1",
        request_id="request-1",
    )

    assert calls[0][0] is conn
    assert calls[0][1] == {
        "snapshot_id": snap.snapshot_id,
        "tenant_id": "t-provenance",
        "model_call_id": "model-call-1",
        "request_id": "request-1",
        "records_json": _records_to_json(snap.records),
        "omissions_json": '["digest_truncated"]',
        "manifest_digest": snap.manifest_digest,
    }


@pytest.mark.asyncio
async def test_repository_get_calls_tenant_scoped_helper_and_reconstructs(
    monkeypatch,
) -> None:
    snap = _snapshot()
    calls: list[tuple[object, str, str]] = []

    async def fake_get(conn, snapshot_id, *, tenant_id):
        calls.append((conn, snapshot_id, tenant_id))
        return {
            "snapshot_id": snap.snapshot_id,
            "records": _records_to_json(snap.records),
            "omissions": '["digest_truncated"]',
            "completeness": snap.completeness,
            "manifest_digest": snap.manifest_digest,
        }

    monkeypatch.setattr(db, "get_provenance_snapshot", fake_get)
    conn = object()
    got = await PgSnapshotRepository(_Pool(conn)).get(
        snap.snapshot_id, tenant_id="t-provenance"
    )

    assert got == snap
    assert calls == [(conn, snap.snapshot_id, "t-provenance")]


@pytest.mark.asyncio
async def test_repository_get_returns_none_on_manifest_corruption(
    monkeypatch, caplog
) -> None:
    snap = _snapshot()

    async def fake_get(_conn, _snapshot_id, *, tenant_id):
        assert tenant_id == "t-provenance"
        return {
            "snapshot_id": snap.snapshot_id,
            "records": _records_to_json(snap.records),
            "omissions": '["digest_truncated"]',
            "completeness": snap.completeness,
            "manifest_digest": "0" * 64,
        }

    monkeypatch.setattr(db, "get_provenance_snapshot", fake_get)
    got = await PgSnapshotRepository(_Pool(object())).get(
        snap.snapshot_id, tenant_id="t-provenance"
    )
    assert got is None
    assert "manifest mismatch" in caplog.text


@pytest.mark.asyncio
async def test_db_helpers_fail_closed_on_empty_tenant_or_model_call() -> None:
    kwargs = {
        "snapshot_id": "psnap-empty",
        "tenant_id": "t-provenance",
        "model_call_id": "model-call-1",
        "request_id": "",
        "records_json": "[]",
        "omissions_json": "[]",
        "manifest_digest": "a" * 64,
    }
    with pytest.raises(ValueError):
        await db.insert_provenance_snapshot(
            _UnusedConn(), **{**kwargs, "tenant_id": ""}
        )
    with pytest.raises(ValueError):
        await db.insert_provenance_snapshot(
            _UnusedConn(), **{**kwargs, "model_call_id": ""}
        )
    with pytest.raises(ValueError):
        await db.get_provenance_snapshot(
            _UnusedConn(), "psnap-empty", tenant_id=""
        )


@pytest.mark.asyncio
async def test_db_insert_is_idempotent_for_same_manifest() -> None:
    conn = _FakeConn((None, {"manifest_digest": "a" * 64}))
    inserted = await db.insert_provenance_snapshot(
        conn,
        snapshot_id="psnap-idempotent",
        tenant_id="t-provenance",
        model_call_id="model-call-1",
        request_id="",
        records_json="[]",
        omissions_json="[]",
        manifest_digest="a" * 64,
    )
    assert inserted is False
    assert "ON CONFLICT (snapshot_id) DO NOTHING" in conn.calls[0][0]


@pytest.mark.asyncio
async def test_db_insert_rejects_write_once_manifest_mismatch() -> None:
    conn = _FakeConn((None, {"manifest_digest": "b" * 64}))
    with pytest.raises(ValueError, match="snapshot manifest mismatch"):
        await db.insert_provenance_snapshot(
            conn,
            snapshot_id="psnap-collision",
            tenant_id="t-provenance",
            model_call_id="model-call-1",
            request_id="",
            records_json="[]",
            omissions_json="[]",
            manifest_digest="a" * 64,
        )


@pytest.mark.asyncio
async def test_db_get_query_is_tenant_scoped() -> None:
    conn = _FakeConn()
    assert (
        await db.get_provenance_snapshot(
            conn, "psnap-scoped", tenant_id="t-provenance"
        )
        is None
    )
    sql, params = conn.calls[0]
    assert "snapshot_id = $1 AND tenant_id = $2" in sql
    assert params == ("psnap-scoped", "t-provenance")


def test_sqlite_bootstrap_contains_provenance_snapshot_table_and_index() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(db._SCHEMA)
    columns = {
        row[1]: row for row in conn.execute("PRAGMA table_info(provenance_snapshots)")
    }
    assert set(columns) == {
        "snapshot_id",
        "tenant_id",
        "model_call_id",
        "request_id",
        "records",
        "omissions",
        "completeness",
        "manifest_digest",
        "schema_version",
        "created_at",
    }
    indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(provenance_snapshots)")
    }
    assert "idx_provenance_snapshots_tenant_model_call" in indexes


async def _seed_tenants(conn, *tenant_ids: str) -> None:
    for tenant_id in tenant_ids:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free') "
            "ON CONFLICT (id) DO NOTHING",
            tenant_id,
            tenant_id,
        )


@pytest.mark.asyncio
async def test_pg_insert_get_roundtrip_reconstructs_equal_snapshot(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-prov-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)
    repo = PgSnapshotRepository(_Pool(pg_test_conn))
    snap = _snapshot(f"psnap-{suffix}")
    await repo.persist(
        snap, tenant_id=tenant_id, model_call_id=f"model-{suffix}"
    )
    assert await repo.get(snap.snapshot_id, tenant_id=tenant_id) == snap


@pytest.mark.asyncio
async def test_pg_reput_different_manifest_raises(pg_test_conn) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-prov-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)
    repo = PgSnapshotRepository(_Pool(pg_test_conn))
    snap = _snapshot(f"psnap-{suffix}")
    await repo.persist(snap, tenant_id=tenant_id, model_call_id=f"model-{suffix}")
    changed = _build_snapshot(
        (_record(source_id="different"),),
        completeness="complete",
        omissions=snap.omissions,
        snapshot_id=snap.snapshot_id,
    )
    with pytest.raises(ValueError, match="snapshot manifest mismatch"):
        await repo.persist(
            changed, tenant_id=tenant_id, model_call_id=f"model-{suffix}"
        )


@pytest.mark.asyncio
async def test_pg_same_manifest_reput_is_idempotent(pg_test_conn) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-prov-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_id)
    snap = _snapshot(f"psnap-{suffix}")
    repo = PgSnapshotRepository(_Pool(pg_test_conn))
    await repo.persist(snap, tenant_id=tenant_id, model_call_id=f"model-{suffix}")
    await repo.persist(snap, tenant_id=tenant_id, model_call_id=f"model-{suffix}")
    count = await pg_test_conn.fetchval(
        "SELECT COUNT(*) FROM provenance_snapshots WHERE snapshot_id = $1",
        snap.snapshot_id,
    )
    assert count == 1


@pytest.mark.asyncio
async def test_pg_cross_tenant_get_returns_none(pg_test_conn) -> None:
    suffix = uuid.uuid4().hex
    tenant_a = f"t-prov-a-{suffix}"
    tenant_b = f"t-prov-b-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_a, tenant_b)
    snap = _snapshot(f"psnap-{suffix}")
    repo = PgSnapshotRepository(_Pool(pg_test_conn))
    await repo.persist(snap, tenant_id=tenant_a, model_call_id=f"model-{suffix}")
    assert await repo.get(snap.snapshot_id, tenant_id=tenant_b) is None
