"""U6-0 T9/T10 challenge creation with provenance binding.

The helper seals a prepared action and a complete model snapshot into durable
PostgreSQL state.  It has no mutable module-global state; cross-worker retry
coordination is provided by deterministic identities and database constraints.
It is wired only to the runner dispatch and remains inert while the runner SDK
action-guard mode matrix has no enforce entry.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone

from backend.agents import action_grant
from backend.agents import auto_auth_policy
from backend.agents.action_canonicalize import PreparedAction
from backend.agents.execution_context import ExecutionContext, is_unbound
from backend.agents.provenance import (
    PgSnapshotRepository,
    TurnProvenance,
    is_grant_eligible,
)


CHALLENGE_TTL_SECONDS = 3600

# U6-0 B-autoauth (AA-3): the synthetic server confirmer recorded on an
# auto-granted challenge (never a human/model identity).
_AUTO_AUTH_ACTOR = "u6-auto-auth"
_AUTO_AUTH_PRINCIPAL_TYPE = "service"
_AUTO_AUTH_REASON = "server_auto_auth: workspace-contained code_write"


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

        # U6-0 B-autoauth (AA-3): server-side auto-authorization (default OFF).
        # PURE decision (no DB), computed before the txn: only a server-launched
        # runner writing a workspace-contained, inert file with clean provenance
        # is auto-granted; everything else falls through to the human challenge.
        auto_grant = False
        if auto_auth_policy.auto_auth_enabled():
            from backend.agents.authoritative_context_resolver import (
                resolve_authoritative_workspace,
            )

            auth_ws = resolve_authoritative_workspace(
                ctx, adapter_namespace, tool_name, schema_version
            )
            if auth_ws is not None:
                workspace_id, workspace_root = auth_ws
                auto_grant = (
                    auto_auth_policy.evaluate_auto_auth(
                        execution_context=ctx,
                        prepared_action=challenge_prepared,
                        workspace_id=workspace_id,
                        workspace_root=workspace_root,
                        snapshot=turn_provenance,
                    )
                    is auto_auth_policy.AutoAuthVerdict.AUTO_GRANT
                )

        db = importlib.import_module("backend.db")
        created = False
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
                if auto_grant:
                    await db.auto_grant_from_prepared(
                        conn,
                        tenant_id=ctx.tenant_id,
                        action_instance_id=action_instance_id,
                        challenge_id=challenge_id,
                        grant_id="grant-" + digest,
                        resume_id="resume-" + digest,
                        confirmer_actor=_AUTO_AUTH_ACTOR,
                        confirmer_principal_type=_AUTO_AUTH_PRINCIPAL_TYPE,
                        confirmer_auth_event_id="server_auto_grant:" + digest,
                        reason=_AUTO_AUTH_REASON,
                        grant_expires_at=expires_at,
                        challenge_expires_at=expires_at,
                    )
                else:
                    created = await db.put_challenge(
                        conn,
                        challenge_id=challenge_id,
                        tenant_id=ctx.tenant_id,
                        action_instance_id=action_instance_id,
                        expires_at=expires_at,
                    )
            if auto_grant or created:
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
