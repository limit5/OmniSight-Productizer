"""T11 enforce-loop end-to-end proof (controlled; fleet flip deferred).

Proves the FULL U6-0 enforce loop is live-capable WITHOUT touching any live
runner: an auto-granted, workspace-contained ``code_write`` is executed by the
REAL O_NOFOLLOW workspace writer and lands on disk.

  * The OFFLINE tests exercise the EXECUTION half directly (the real executor +
    the hash-bound fd resolver) — runnable everywhere; they prove the granted
    write actually happens AND that a write outside the allowlisted root fails
    closed (the containment the enforce loop relies on).
  * The PG test chains the WHOLE loop — ``auto_grant_from_prepared`` (AA-2) ->
    ``run_resume_job`` -> real executor -> file on disk -> grant consumed — and
    skips when ``OMNI_TEST_PG_URL`` is unset (runs in CI).

This is the T11 validation artifact. The actual FLEET enforce cutover is a
separate operator/integration step: the synchronous runner tool-call model is
incompatible with the async grant→resume→execute model (an enforce block returns
``action_guard_denied`` synchronously), so fleet enforce needs a
resume-continuation model that is intentionally NOT flipped here.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend import db
from backend.agents import execution_gate
from backend.agents.execution_contract import Applied, StoredAction
from backend.agents.execution_service import run_resume_job
from backend.agents.executor_workspace_write import make_dispatch_executor
from backend.agents.workspace_root_resolver import resolve_root_fd


def _workspace_id(tenant: str, root: str) -> str:
    """The ws-id EXACTLY as authoritative_context_resolver derives it."""
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()
    return f"ws:{tenant}:runner_sdk:{digest}"


# ── OFFLINE: the execution half (real executor writes the granted file) ─────
@pytest.mark.asyncio
async def test_auto_granted_write_lands_on_disk_via_real_executor(
    tmp_path, monkeypatch
) -> None:
    root = str(tmp_path)
    monkeypatch.setenv("OMNISIGHT_U6_WORKSPACE_FD_ROOTS", json.dumps([root]))
    rel = "backend/gen/result.txt"
    (tmp_path / "backend" / "gen").mkdir(parents=True)  # executor opens, never creates, dirs
    content = "auto-granted contained write\n"
    stored = StoredAction(
        grant_id="grant-e2e",
        idempotency_key=None,
        recovery_mode="non_replayable",
        adapter_namespace="runner_sdk",
        tool_name="Write",
        schema_version="v1",
        canonical_target=rel,
        executable_args={
            "workspace_id": _workspace_id("t-e2e", root),
            "relative_path": rel,
            "content": content,
        },
    )

    outcome = await make_dispatch_executor(resolve_root_fd)(stored)

    assert isinstance(outcome, Applied)
    assert (tmp_path / rel).read_text() == content
    assert outcome.result["bytes_written"] == len(content.encode("utf-8"))


@pytest.mark.asyncio
async def test_write_to_unregistered_root_fails_closed_no_file(
    tmp_path, monkeypatch
) -> None:
    # A workspace_id whose digest is NOT an allowlisted root ⇒ resolver returns
    # None ⇒ nothing is written (the containment the enforce loop relies on).
    root = str(tmp_path)
    monkeypatch.setenv("OMNISIGHT_U6_WORKSPACE_FD_ROOTS", json.dumps([root]))
    stored = StoredAction(
        grant_id="grant-e2e",
        idempotency_key=None,
        recovery_mode="non_replayable",
        adapter_namespace="runner_sdk",
        tool_name="Write",
        schema_version="v1",
        canonical_target="x.txt",
        executable_args={
            "workspace_id": _workspace_id("t-e2e", str(tmp_path / "unregistered")),
            "relative_path": "x.txt",
            "content": "must not be written",
        },
    )

    outcome = await make_dispatch_executor(resolve_root_fd)(stored)

    assert not isinstance(outcome, Applied)
    assert not (tmp_path / "x.txt").exists()


# ── PG: the WHOLE loop (auto_grant → resume → executor → file) ──────────────
@pytest.mark.asyncio
async def test_enforce_loop_auto_grant_through_resume_writes_file(
    pg_test_pool, tmp_path, monkeypatch
) -> None:
    root = str(tmp_path)
    monkeypatch.setenv("OMNISIGHT_U6_WORKSPACE_FD_ROOTS", json.dumps([root]))
    monkeypatch.setenv("OMNISIGHT_U6_EXECUTE", "1")
    monkeypatch.setenv("OMNISIGHT_U6_EXECUTE_ALLOWLIST", "runner_sdk:Write")

    suffix = uuid.uuid4().hex
    tenant = f"t-e2e-{suffix}"
    aiid = f"action-{suffix}"
    grant_id = f"grant-{suffix}"
    resume_id = f"resume-{suffix}"
    rel = "out/e2e.txt"
    (tmp_path / "out").mkdir(parents=True)  # executor opens, never creates, dirs
    content = "enforce-loop-end-to-end\n"
    executable_args = {
        "workspace_id": _workspace_id(tenant, root),
        "relative_path": rel,
        "content": content,
    }

    async with pg_test_pool.acquire() as conn:
        await conn.execute("DELETE FROM resume_jobs")  # claim only our job
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free')",
            tenant,
            tenant,
        )
        assert await db.put_prepared_action(
            conn,
            tenant_id=tenant,
            action_instance_id=aiid,
            principal_type="service",
            actor_id="s1-jira-runner",
            request_id=f"req-{suffix}",
            model_call_id="",
            adapter_namespace="runner_sdk",
            tool_name="Write",
            schema_version="v1",
            family="code_write",
            effect="mutating",
            canonical_target=rel,
            args_hash="a" * 64,
            provenance_kind="no_model_input",
            model_snapshot_id=None,
            no_model_input_source="slash_command",
            executable_args_json=json.dumps(executable_args),
            human_rendering_json=json.dumps({"summary": "write"}),
            prepared_action_digest="d" * 64,
            recovery_mode="non_replayable",
        ) is True
        assert await db.auto_grant_from_prepared(
            conn,
            tenant_id=tenant,
            action_instance_id=aiid,
            challenge_id=f"chal-{suffix}",
            grant_id=grant_id,
            resume_id=resume_id,
            confirmer_actor="u6-auto-auth",
            confirmer_principal_type="service",
            confirmer_auth_event_id=f"server_auto_grant:{suffix}",
            reason="server_auto_auth: e2e",
            grant_expires_at=datetime.now(UTC) + timedelta(minutes=30),
            challenge_expires_at=datetime.now(UTC) + timedelta(hours=1),
        ) == "auto_granted"

    result = await run_resume_job(
        pg_test_pool,
        worker_id="e2e-worker",
        lease_ttl_seconds=60,
        executor=make_dispatch_executor(resolve_root_fd),
        authorizer=execution_gate.authorizer,
        attempt_id_factory=lambda: uuid.uuid4().hex,
        result_of=execution_gate.result_of,
    )

    assert result == "done"
    assert (tmp_path / rel).read_text() == content
    async with pg_test_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT state FROM action_grants WHERE tenant_id=$1 AND grant_id=$2",
            tenant,
            grant_id,
        ) == "consumed"
        assert await conn.fetchval(
            "SELECT state FROM resume_jobs WHERE tenant_id=$1 AND resume_id=$2",
            tenant,
            resume_id,
        ) == "done"
