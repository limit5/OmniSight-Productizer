"""KS.1.5 -- decryption audit emitter for the N10 tamper-evident ledger.

Every KS envelope decrypt call that returns plaintext through a caller
surface should fan out here so the audit row shape stays stable:
tenant / user / ledger time / key_id / request_id. The ledger time is
the ``audit_log.ts`` column written by :func:`backend.audit.log`.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
No module-level mutable state. The frozen context dataclass is built
per call. Tenant routing uses the existing ``db_context`` ContextVar
only for the duration of one ``audit.log`` call and restores the prior
value in ``finally``; cross-worker chain serialisation remains PG's
``pg_advisory_xact_lock`` inside ``backend.audit``.

Read-after-write timing audit
─────────────────────────────
N/A -- fan-out is to ``audit.log`` which serialises per-tenant chain
appends through PG. The emitter does not read its own writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend import audit
from backend.db_context import current_tenant_id, set_tenant_id


EVENT_KS_DECRYPTION = "ks.decryption"
ENTITY_KIND_DECRYPTION = "decryption"


@dataclass(frozen=True)
class DecryptionAuditContext:
    """Inputs for one KS.1.5 decryption audit row."""

    tenant_id: str
    user_id: str
    key_id: str
    request_id: str
    purpose: str
    provider: str
    actor: Optional[str] = None
    dek_id: Optional[str] = None


async def emit_decryption(ctx: DecryptionAuditContext) -> Optional[int]:
    """Emit one KS.1.5 decryption row into the tenant's audit chain.

    Returns the audit row id, or ``None`` if ``audit.log`` swallowed a
    transient write failure. Raw plaintext and ciphertext are never
    included in the row body.
    """

    before = {
        "tenant_id": ctx.tenant_id,
        "user_id": ctx.user_id,
        "key_id": ctx.key_id,
        "request_id": ctx.request_id,
    }
    after = {
        "tenant_id": ctx.tenant_id,
        "user_id": ctx.user_id,
        "key_id": ctx.key_id,
        "request_id": ctx.request_id,
        "purpose": ctx.purpose,
        "provider": ctx.provider,
        "dek_id": ctx.dek_id,
    }
    saved = current_tenant_id()
    try:
        set_tenant_id(ctx.tenant_id)
        return await audit.log(
            action=EVENT_KS_DECRYPTION,
            entity_kind=ENTITY_KIND_DECRYPTION,
            entity_id=ctx.key_id,
            before=before,
            after=after,
            actor=ctx.actor or ctx.user_id,
        )
    finally:
        set_tenant_id(saved)


__all__ = [
    "DecryptionAuditContext",
    "ENTITY_KIND_DECRYPTION",
    "EVENT_KS_DECRYPTION",
    "emit_decryption",
]
