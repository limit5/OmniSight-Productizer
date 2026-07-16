"""U6-0 T9/T10 challenge creation with provenance binding (dormant).

The helper seals a prepared action and a complete model snapshot into durable
PostgreSQL state.  It has no mutable module-global state; cross-worker retry
coordination is provided by deterministic identities and database constraints.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone

from backend.agents import action_grant
from backend.agents.action_canonicalize import PreparedAction
from backend.agents.execution_context import ExecutionContext, is_unbound
from backend.agents.provenance import (
    PgSnapshotRepository,
    TurnProvenance,
    is_grant_eligible,
)


CHALLENGE_TTL_SECONDS = 3600


async def create_challenge_from_block(
    pool,
    challenge_prepared: PreparedAction,
    ctx: ExecutionContext,
    turn_provenance: TurnProvenance | None,
    *,
    adapter_namespace: str,
    tool_name: str,
    schema_version: str,
) -> str | None:
    """Persist a pending challenge, or fail closed without raising."""
    if not is_grant_eligible(turn_provenance):
        return None
    if is_unbound(ctx):
        return None

    try:
        snap = turn_provenance.snapshot  # type: ignore[union-attr]
        model_call_id = snap.snapshot_id
        identity = action_grant.OperationIdentity(
            tenant_id=ctx.tenant_id,
            principal_type=ctx.principal_type,
            actor_id=ctx.actor_id,
            request_id=ctx.request_id,
            model_call_id=model_call_id,
            adapter_namespace=adapter_namespace,
            tool_name=tool_name,
            schema_version=schema_version,
            family=challenge_prepared.operation_descriptor.family,
            canonical_target=challenge_prepared.canonical_target,
            args_hash=action_grant.args_hash(challenge_prepared.executable_args),
            provenance=action_grant.ModelProvenanceRef(
                model_snapshot_id=snap.snapshot_id
            ),
        )
        action_grant.validate(identity)
        digest = action_grant.prepared_action_digest(
            identity,
            challenge_prepared.executable_args,
        )
        action_instance_id = "aiid-" + digest
        challenge_id = "chal-" + digest
        executable_args_json = json.dumps(
            dict(challenge_prepared.executable_args),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        human_rendering_json = json.dumps(
            dict(challenge_prepared.human_rendering),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=CHALLENGE_TTL_SECONDS
        )

        repo = PgSnapshotRepository(pool)
        await repo.persist(
            snap,
            tenant_id=ctx.tenant_id,
            model_call_id=model_call_id,
            request_id=ctx.request_id,
        )
        if await repo.get(snap.snapshot_id, tenant_id=ctx.tenant_id) is None:
            return None

        db = importlib.import_module("backend.db")
        async with pool.acquire() as conn:
            async with conn.transaction():
                await db.put_prepared_action(
                    conn,
                    action_instance_id=action_instance_id,
                    tenant_id=ctx.tenant_id,
                    principal_type=ctx.principal_type,
                    actor_id=ctx.actor_id,
                    request_id=ctx.request_id,
                    model_call_id=model_call_id,
                    adapter_namespace=adapter_namespace,
                    tool_name=tool_name,
                    schema_version=schema_version,
                    family=challenge_prepared.operation_descriptor.family,
                    effect=challenge_prepared.operation_descriptor.effect,
                    canonical_target=challenge_prepared.canonical_target,
                    args_hash=identity.args_hash,
                    provenance_kind="model",
                    model_snapshot_id=snap.snapshot_id,
                    no_model_input_source=None,
                    executable_args_json=executable_args_json,
                    human_rendering_json=human_rendering_json,
                    prepared_action_digest=digest,
                    recovery_mode="non_replayable",
                )
                created = await db.put_challenge(
                    conn,
                    challenge_id=challenge_id,
                    tenant_id=ctx.tenant_id,
                    action_instance_id=action_instance_id,
                    expires_at=expires_at,
                )
            if created:
                return challenge_id
            row = await db.get_challenge(
                conn,
                challenge_id,
                tenant_id=ctx.tenant_id,
            )
        return (
            challenge_id
            if row is not None and row["state"] == "pending"
            else None
        )
    except Exception:  # noqa: BLE001 - the whole binding is fail-closed
        return None
