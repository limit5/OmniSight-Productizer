"""OP-2669 challenge creation fail-closed and PostgreSQL tests.

The offline tests cover every pre-database gate, error containment,
deterministic sealing, and dormancy.  PostgreSQL cases use one transaction-
scoped connection for all three helper acquires and skip when
``OMNI_TEST_PG_URL`` is unset.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from backend import db
from backend.agents import (
    action_challenge,
    authoritative_context_resolver,
    auto_auth_policy,
    provenance,
)
from backend.agents.action_canonicalize import PreparedAction
from backend.agents.execution_context import ExecutionContext, for_unbound
from backend.agents.provenance import (
    CaptureUnavailable,
    ModelSnapshot,
    NoModelInput,
    PgSnapshotRepository,
    ProvenanceSnapshot,
    _build_snapshot,
)
from backend.agents.tool_registry import OperationDescriptor


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _snapshot(
    *,
    tenant_id: str = "t-challenge",
    snapshot_id: str = "psnap-challenge",
    completeness: str = "complete",
    source_id: str = "tool-call-1",
) -> ProvenanceSnapshot:
    record = provenance.untrusted_record(
        provenance.TOOL_RESULT,
        source_id,
        "visible tool result",
        tenant_id=tenant_id,
        visibility="tenant",
    )
    return _build_snapshot(
        (record,),
        completeness=completeness,
        omissions=() if completeness == "complete" else ("capture_error",),
        snapshot_id=snapshot_id,
    )


def _ctx(*, tenant_id: str = "t-challenge") -> ExecutionContext:
    return ExecutionContext(
        principal_type="service",
        tenant_id=tenant_id,
        actor_id="service-codex",
        roles=("runner",),
        session_id=None,
        request_id="request-1",
        message_id=None,
        authorization_source="runner_service",
    )


def _prepared(
    *,
    executable_args: dict[str, object] | None = None,
) -> PreparedAction:
    return PreparedAction(
        operation_descriptor=OperationDescriptor(
            tool_name="write_file",
            effect="mutating",
            family="code_write",
        ),
        canonical_target="/workspace/output.txt",
        executable_args=(
            executable_args
            if executable_args is not None
            else {"content": "done", "path": "/workspace/output.txt"}
        ),
        human_rendering={"summary": "Write output.txt"},
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
    def __init__(self):
        self.touched = False

    def acquire(self):
        self.touched = True
        raise AssertionError("pool must not be used")


class _ExplodingPool:
    def acquire(self):
        raise RuntimeError("database unavailable")


class _TransactionContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _RecordingConn:
    def transaction(self):
        return _TransactionContext()


class _PassRepository:
    def __init__(self, _pool):
        self.snapshot = None

    async def persist(self, snapshot, **_kwargs):
        self.snapshot = snapshot

    async def get(self, snapshot_id, *, tenant_id):
        del snapshot_id, tenant_id
        return self.snapshot


@pytest.mark.parametrize(
    "turn_provenance",
    [
        None,
        CaptureUnavailable(reason="capture failed"),
        NoModelInput(authorization_source="slash_command"),
        ModelSnapshot(_snapshot(completeness="partial")),
    ],
    ids=("none", "capture-unavailable", "no-model-input", "partial"),
)
@pytest.mark.asyncio
async def test_gate_a_rejects_incomplete_provenance_without_pool_access(
    turn_provenance,
) -> None:
    pool = _UnusedPool()

    result = await action_challenge.create_challenge_from_block(
        pool,
        _prepared(),
        _ctx(),
        turn_provenance,
        adapter_namespace="builtin",
        tool_name="write_file",
        schema_version="v1",
    )

    assert result is None
    assert pool.touched is False


@pytest.mark.asyncio
async def test_gate_b_rejects_unbound_principal_without_pool_access() -> None:
    pool = _UnusedPool()

    result = await action_challenge.create_challenge_from_block(
        pool,
        _prepared(),
        for_unbound(),
        ModelSnapshot(_snapshot()),
        adapter_namespace="builtin",
        tool_name="write_file",
        schema_version="v1",
    )

    assert result is None
    assert pool.touched is False


@pytest.mark.asyncio
async def test_any_exception_fails_closed_without_propagating() -> None:
    result = await action_challenge.create_challenge_from_block(
        _ExplodingPool(),
        _prepared(),
        _ctx(),
        ModelSnapshot(_snapshot()),
        adapter_namespace="builtin",
        tool_name="write_file",
        schema_version="v1",
    )

    assert result is None


@pytest.mark.asyncio
async def test_deterministic_ids_are_stable_and_bind_changed_args(
    monkeypatch,
) -> None:
    prepared_calls: list[dict] = []
    challenge_calls: list[dict] = []

    async def fake_put_prepared_action(_conn, **kwargs):
        prepared_calls.append(kwargs)
        return True

    async def fake_put_challenge(_conn, **kwargs):
        challenge_calls.append(kwargs)
        return True

    monkeypatch.setattr(
        action_challenge,
        "PgSnapshotRepository",
        _PassRepository,
    )
    monkeypatch.setattr(db, "put_prepared_action", fake_put_prepared_action)
    monkeypatch.setattr(db, "put_challenge", fake_put_challenge)
    pool = _Pool(_RecordingConn())
    turn = ModelSnapshot(_snapshot())
    inputs = (
        _prepared(),
        _prepared(),
        _prepared(
            executable_args={
                "content": "changed",
                "path": "/workspace/output.txt",
            }
        ),
    )

    results = []
    for prepared in inputs:
        results.append(
            await action_challenge.create_challenge_from_block(
                pool,
                prepared,
                _ctx(),
                turn,
                adapter_namespace="builtin",
                tool_name="write_file",
                schema_version="v1",
            )
        )

    assert results[0] == results[1]
    assert results[0] != results[2]
    assert results[0] == "chal-" + prepared_calls[0]["prepared_action_digest"]
    assert prepared_calls[0]["action_instance_id"] == (
        "aiid-" + prepared_calls[0]["prepared_action_digest"]
    )
    assert json.loads(prepared_calls[0]["executable_args_json"]) == dict(
        inputs[0].executable_args
    )
    assert prepared_calls[0]["executable_args_json"] == (
        '{"content":"done","path":"/workspace/output.txt"}'
    )
    assert json.loads(prepared_calls[0]["human_rendering_json"]) == dict(
        inputs[0].human_rendering
    )
    assert all(
        isinstance(call["expires_at"], datetime)
        and call["expires_at"].tzinfo is not None
        for call in challenge_calls
    )


async def _seed_tenants(conn, *tenant_ids: str) -> None:
    for tenant_id in tenant_ids:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free') "
            "ON CONFLICT (id) DO NOTHING",
            tenant_id,
            tenant_id,
        )


def _pg_case(prefix: str) -> tuple[str, ModelSnapshot, ExecutionContext]:
    suffix = uuid.uuid4().hex
    tenant_id = f"t-{prefix}-{suffix}"
    snapshot = ModelSnapshot(
        _snapshot(
            tenant_id=tenant_id,
            snapshot_id=f"psnap-{suffix}",
        )
    )
    ctx = _ctx(tenant_id=tenant_id)
    return tenant_id, snapshot, ctx


async def _create(pool, turn: ModelSnapshot, ctx: ExecutionContext):
    return await action_challenge.create_challenge_from_block(
        pool,
        _prepared(),
        ctx,
        turn,
        adapter_namespace="builtin",
        tool_name="write_file",
        schema_version="v1",
    )


@pytest.mark.asyncio
async def test_pg_happy_path_persists_bound_pending_challenge(
    pg_test_conn,
) -> None:
    tenant_id, turn, ctx = _pg_case("challenge-happy")
    await _seed_tenants(pg_test_conn, tenant_id)
    pool = _Pool(pg_test_conn)

    challenge_id = await _create(pool, turn, ctx)

    assert challenge_id is not None
    assert pool.acquire_count == 3
    snapshot_row = await pg_test_conn.fetchrow(
        "SELECT * FROM provenance_snapshots WHERE snapshot_id = $1",
        turn.snapshot.snapshot_id,
    )
    prepared_row = await pg_test_conn.fetchrow(
        "SELECT * FROM prepared_actions WHERE tenant_id = $1",
        tenant_id,
    )
    challenge_row = await pg_test_conn.fetchrow(
        "SELECT * FROM challenges WHERE challenge_id = $1",
        challenge_id,
    )
    assert snapshot_row["tenant_id"] == tenant_id
    assert snapshot_row["model_call_id"] == turn.snapshot.snapshot_id
    assert prepared_row["tenant_id"] == tenant_id
    assert prepared_row["model_snapshot_id"] == turn.snapshot.snapshot_id
    assert prepared_row["model_call_id"] == turn.snapshot.snapshot_id
    assert prepared_row["provenance_kind"] == "model"
    assert prepared_row["recovery_mode"] == "non_replayable"
    assert json.loads(prepared_row["executable_args"]) == dict(
        _prepared().executable_args
    )
    assert challenge_row["tenant_id"] == tenant_id
    assert challenge_row["state"] == "pending"
    assert challenge_row["model_snapshot_id"] == turn.snapshot.snapshot_id
    assert challenge_row["expires_at"] > challenge_row["created_at"]
    assert challenge_row["expires_at"].tzinfo is not None


@pytest.mark.asyncio
async def test_pg_idempotent_retry_returns_same_single_challenge(
    pg_test_conn,
) -> None:
    tenant_id, turn, ctx = _pg_case("challenge-retry")
    await _seed_tenants(pg_test_conn, tenant_id)
    pool = _Pool(pg_test_conn)

    first = await _create(pool, turn, ctx)
    second = await _create(pool, turn, ctx)

    assert first is not None
    assert second == first
    counts = await pg_test_conn.fetchrow(
        "SELECT "
        "(SELECT count(*) FROM provenance_snapshots WHERE tenant_id = $1) "
        "AS snapshots, "
        "(SELECT count(*) FROM prepared_actions WHERE tenant_id = $1) "
        "AS prepared, "
        "(SELECT count(*) FROM challenges WHERE tenant_id = $1) "
        "AS challenges",
        tenant_id,
    )
    assert tuple(counts) == (1, 1, 1)


@pytest.mark.asyncio
async def test_pg_cross_tenant_snapshot_fails_closed_before_action_write(
    pg_test_conn,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_a = f"t-challenge-a-{suffix}"
    tenant_b = f"t-challenge-b-{suffix}"
    await _seed_tenants(pg_test_conn, tenant_a, tenant_b)
    snap = _snapshot(tenant_id=tenant_a, snapshot_id=f"psnap-{suffix}")
    await PgSnapshotRepository(_Pool(pg_test_conn)).persist(
        snap,
        tenant_id=tenant_a,
        model_call_id=snap.snapshot_id,
        request_id="request-a",
    )
    pool = _Pool(pg_test_conn)

    result = await _create(pool, ModelSnapshot(snap), _ctx(tenant_id=tenant_b))

    assert result is None
    assert pool.acquire_count == 2
    assert await pg_test_conn.fetchval(
        "SELECT count(*) FROM prepared_actions WHERE tenant_id = $1",
        tenant_b,
    ) == 0
    assert await pg_test_conn.fetchval(
        "SELECT count(*) FROM challenges WHERE tenant_id = $1",
        tenant_b,
    ) == 0


@pytest.mark.asyncio
async def test_pg_dead_handle_retry_returns_none(pg_test_conn) -> None:
    tenant_id, turn, ctx = _pg_case("challenge-dead")
    await _seed_tenants(pg_test_conn, tenant_id)
    pool = _Pool(pg_test_conn)
    challenge_id = await _create(pool, turn, ctx)
    assert challenge_id is not None
    assert await db.reject_challenge(
        pg_test_conn,
        tenant_id=tenant_id,
        challenge_id=challenge_id,
        confirmer_actor="reviewer-1",
        confirmer_principal_type="user",
        confirmer_auth_event_id=f"auth-{uuid.uuid4().hex}",
        reason="Not approved",
    ) == "rejected"

    assert await _create(pool, turn, ctx) is None


@pytest.mark.asyncio
async def test_pg_different_manifest_replay_fails_closed(
    pg_test_conn,
) -> None:
    tenant_id, turn, ctx = _pg_case("challenge-manifest")
    await _seed_tenants(pg_test_conn, tenant_id)
    await PgSnapshotRepository(_Pool(pg_test_conn)).persist(
        turn.snapshot,
        tenant_id=tenant_id,
        model_call_id=turn.snapshot.snapshot_id,
        request_id=ctx.request_id,
    )
    changed = ModelSnapshot(
        _snapshot(
            tenant_id=tenant_id,
            snapshot_id=turn.snapshot.snapshot_id,
            source_id="different-tool-call",
        )
    )

    assert await _create(_Pool(pg_test_conn), changed, ctx) is None
    assert await pg_test_conn.fetchval(
        "SELECT count(*) FROM prepared_actions WHERE tenant_id = $1",
        tenant_id,
    ) == 0
    assert await pg_test_conn.fetchval(
        "SELECT count(*) FROM challenges WHERE tenant_id = $1",
        tenant_id,
    ) == 0


def test_create_challenge_caller_is_only_the_runner_dispatch() -> None:
    callers: list[str] = []
    for py in BACKEND_ROOT.rglob("*.py"):
        rel = py.relative_to(BACKEND_ROOT)
        if rel == Path("agents/action_challenge.py"):
            continue
        if rel.parts[0] == "tests" or rel.parts[:2] == ("alembic", "versions"):
            continue
        if "create_challenge_from_block" in py.read_text(errors="ignore"):
            callers.append(str(rel))

    assert sorted(callers) == ["agents/tool_dispatcher.py"]


# ── U6-0 B-autoauth (AA-3) — flag-gated branch into auto-grant vs challenge ──
_WS = ("ws:t-challenge:runner_sdk:" + "a" * 64, "/root/ws")


async def _run_autoauth(monkeypatch, *, flag_on, verdict, workspace):
    challenge_calls: list[dict] = []
    autogrant_calls: list[dict] = []

    async def _fake_put_prepared_action(_conn, **kwargs):
        return True

    async def _fake_put_challenge(_conn, **kwargs):
        challenge_calls.append(kwargs)
        return True

    async def _fake_auto_grant(_conn, **kwargs):
        autogrant_calls.append(kwargs)
        return "auto_granted"

    monkeypatch.setattr(action_challenge, "PgSnapshotRepository", _PassRepository)
    monkeypatch.setattr(db, "put_prepared_action", _fake_put_prepared_action)
    monkeypatch.setattr(db, "put_challenge", _fake_put_challenge)
    monkeypatch.setattr(db, "auto_grant_from_prepared", _fake_auto_grant)
    if flag_on:
        monkeypatch.setenv("OMNISIGHT_U6_AUTO_AUTH", "1")
    else:
        monkeypatch.delenv("OMNISIGHT_U6_AUTO_AUTH", raising=False)
    monkeypatch.setattr(
        authoritative_context_resolver,
        "resolve_authoritative_workspace",
        lambda *a, **k: workspace,
    )
    if verdict is not None:
        monkeypatch.setattr(auto_auth_policy, "evaluate_auto_auth", lambda **k: verdict)

    result = await action_challenge.create_challenge_from_block(
        _Pool(_RecordingConn()),
        _prepared(),
        _ctx(),
        ModelSnapshot(_snapshot()),
        adapter_namespace="runner_sdk",
        tool_name="write_file",
        schema_version="v1",
    )
    return challenge_calls, autogrant_calls, result


@pytest.mark.asyncio
async def test_autoauth_flag_off_creates_pending_challenge(monkeypatch) -> None:
    # Default OFF: even a would-be AUTO_GRANT verdict is never consulted; the
    # existing pending-challenge path runs (byte-identical to today).
    challenge_calls, autogrant_calls, result = await _run_autoauth(
        monkeypatch,
        flag_on=False,
        verdict=auto_auth_policy.AutoAuthVerdict.AUTO_GRANT,
        workspace=_WS,
    )
    assert len(challenge_calls) == 1
    assert autogrant_calls == []
    assert result is not None


@pytest.mark.asyncio
async def test_autoauth_grant_verdict_issues_auto_grant(monkeypatch) -> None:
    challenge_calls, autogrant_calls, result = await _run_autoauth(
        monkeypatch,
        flag_on=True,
        verdict=auto_auth_policy.AutoAuthVerdict.AUTO_GRANT,
        workspace=_WS,
    )
    assert len(autogrant_calls) == 1
    assert challenge_calls == []
    # The synthetic confirmer is server-owned (never a human/model identity).
    assert autogrant_calls[0]["confirmer_actor"] == "u6-auto-auth"
    assert autogrant_calls[0]["confirmer_principal_type"] == "service"
    assert result is not None


@pytest.mark.asyncio
async def test_autoauth_human_verdict_creates_pending_challenge(monkeypatch) -> None:
    challenge_calls, autogrant_calls, _ = await _run_autoauth(
        monkeypatch,
        flag_on=True,
        verdict=auto_auth_policy.AutoAuthVerdict.HUMAN_CHALLENGE,
        workspace=_WS,
    )
    assert len(challenge_calls) == 1
    assert autogrant_calls == []


@pytest.mark.asyncio
async def test_autoauth_no_authoritative_workspace_creates_pending_challenge(
    monkeypatch,
) -> None:
    # No server-resolved workspace ⇒ the predicate is never consulted and the
    # human-challenge path runs (fail-safe).
    evaluated = {"n": 0}

    def _spy(**_kwargs):
        evaluated["n"] += 1
        return auto_auth_policy.AutoAuthVerdict.AUTO_GRANT

    monkeypatch.setattr(auto_auth_policy, "evaluate_auto_auth", _spy)
    challenge_calls, autogrant_calls, _ = await _run_autoauth(
        monkeypatch, flag_on=True, verdict=None, workspace=None
    )
    assert len(challenge_calls) == 1
    assert autogrant_calls == []
    assert evaluated["n"] == 0
