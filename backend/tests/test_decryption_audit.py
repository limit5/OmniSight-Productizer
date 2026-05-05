"""KS.1.5 — decryption audit rows land in the N10 ledger."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.security import decryption_audit as da


REPO_ROOT = Path(__file__).resolve().parents[2]


def _ctx(**overrides) -> da.DecryptionAuditContext:
    base = dict(
        tenant_id="t-ks15",
        user_id="user-ks15",
        key_id="local-fernet",
        request_id="req-ks15",
        purpose="as-token-vault",
        provider="local-fernet",
        actor="user-ks15",
        dek_id="dek_ks15",
    )
    base.update(overrides)
    return da.DecryptionAuditContext(**base)


@pytest.mark.asyncio
async def test_emit_decryption_writes_canonical_row(monkeypatch):
    captured = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 15

    monkeypatch.setattr("backend.security.decryption_audit.audit.log", fake_log)
    rid = await da.emit_decryption(_ctx())

    assert rid == 15
    assert captured["action"] == da.EVENT_KS_DECRYPTION == "ks.decryption"
    assert captured["entity_kind"] == da.ENTITY_KIND_DECRYPTION == "decryption"
    assert captured["entity_id"] == "local-fernet"
    assert captured["actor"] == "user-ks15"
    assert captured["before"] == {
        "tenant_id": "t-ks15",
        "user_id": "user-ks15",
        "key_id": "local-fernet",
        "request_id": "req-ks15",
    }
    assert captured["after"] == {
        "tenant_id": "t-ks15",
        "user_id": "user-ks15",
        "key_id": "local-fernet",
        "request_id": "req-ks15",
        "purpose": "as-token-vault",
        "provider": "local-fernet",
        "dek_id": "dek_ks15",
    }


@pytest.mark.asyncio
async def test_emit_decryption_restores_prior_tenant(monkeypatch):
    from backend.db_context import current_tenant_id, set_tenant_id

    async def fake_log(**kwargs):
        assert current_tenant_id() == "t-ks15"
        return 15

    set_tenant_id("t-prior")
    monkeypatch.setattr("backend.security.decryption_audit.audit.log", fake_log)
    await da.emit_decryption(_ctx())
    assert current_tenant_id() == "t-prior"
    set_tenant_id(None)


@pytest.mark.asyncio
async def test_emit_decryption_lands_in_tamper_evident_chain(pg_test_pool):
    from backend import audit

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE audit_log RESTART IDENTITY CASCADE")
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free') "
            "ON CONFLICT (id) DO NOTHING",
            "t-ks15",
            "KS15",
        )
    try:
        rid = await da.emit_decryption(_ctx())
        assert isinstance(rid, int)

        rows = await audit.query(entity_kind=da.ENTITY_KIND_DECRYPTION, limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == da.EVENT_KS_DECRYPTION
        assert row["after"]["tenant_id"] == "t-ks15"
        assert row["after"]["user_id"] == "user-ks15"
        assert row["after"]["key_id"] == "local-fernet"
        assert row["after"]["request_id"] == "req-ks15"

        ok, bad = await audit.verify_chain(tenant_id="t-ks15")
        assert ok and bad is None
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE audit_log RESTART IDENTITY CASCADE")


def test_production_token_decrypt_call_sites_use_audited_wrapper():
    """KS DoD: production OAuth plaintext recovery must write N10 rows.

    ``token_vault.decrypt_for_user`` remains as the pure primitive for
    tests, backfills, and the audited wrapper itself. Production modules
    outside the vault must call ``decrypt_for_user_with_audit`` so every
    runtime plaintext recovery emits ``ks.decryption``.
    """

    offenders = []
    for path in (REPO_ROOT / "backend").rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith("backend/tests/"):
            continue
        if rel == "backend/security/token_vault.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "decrypt_for_user(" in source:
            offenders.append(rel)

    assert offenders == []
