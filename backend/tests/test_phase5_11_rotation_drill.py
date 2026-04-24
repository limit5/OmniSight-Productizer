"""Phase 5-11 (#multi-account-forge) — token rotation drill.

Proves the end-to-end security contract for rotating a secret on an
existing ``git_accounts`` row:

1.  **Immediate effect** — PATCH /git-accounts/{id} with a new token
    returns a public dict whose ``token_fingerprint`` matches the new
    token's last-4 (not the old). ``version`` is bumped. A subsequent
    ``get_account`` sees the same new fingerprint + bumped version
    (read-after-write contract from Phase 5-4).

2.  **Old secret is gone from storage** — after rotation the decrypted
    ``encrypted_token`` column resolves to the new plaintext, NOT the
    old. Stale-read under the old token must fail.

3.  **Audit trail carries a trace** — an ``audit_log`` row for the
    rotation mutation exists under the tenant's chain, with
    ``action='git_account.update'`` and a ``before``/``after`` diff
    that names ``token_fingerprint`` but NEVER contains the plaintext
    token in either column. (A rotation audit row that accidentally
    echoed the new PAT would itself be the breach the rotation was
    meant to prevent.)

4.  **Chain integrity survives rotation** — ``verify_chain`` still
    returns ``(True, None)`` after the rotation. A rotation that
    corrupted hash chaining would fail this.

5.  **Rotation covers every secret column** — the same contract holds
    for ``ssh_key`` and ``webhook_secret``, not just ``token``.

6.  **Empty-string clears** — PATCH with ``{"token": ""}`` (explicit
    empty) clears the ciphertext; PATCH with ``{"token": None}``
    leaves it alone. Per the Phase-5-4 docstring contract.

Module-global audit (SOP Step 1, qualified answer #1)
─────────────────────────────────────────────────────
No new module-globals. ``secret_store._fernet`` is a per-worker
cache derived from the same key source; rotation never touches it.

Read-after-write audit (SOP Step 1)
───────────────────────────────────
PATCH returns only AFTER the transaction commits
(``async with conn.transaction()`` in
``git_accounts.update_account``), and the subsequent
``get_account`` / ``get_plaintext_token`` uses a different pool
conn — but since the commit is complete, the new state is visible
on any connection per PG's read-committed semantics. The tests
assert this visibility.

Gated on ``OMNI_TEST_PG_URL``.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


TENANT = "t-phase511-rotation"


@pytest.fixture()
async def _rot_db(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES "
            "($1, 'Phase-5-11 Rotation', 'starter') "
            "ON CONFLICT (id) DO NOTHING",
            TENANT,
        )
        await conn.execute(
            "TRUNCATE git_accounts, audit_log RESTART IDENTITY CASCADE"
        )
    from backend.db_context import set_tenant_id
    set_tenant_id(TENANT)
    import backend.git_accounts as ga
    try:
        yield ga, pg_test_pool
    finally:
        set_tenant_id(None)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE git_accounts, audit_log RESTART IDENTITY CASCADE"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token rotation — happy path + old-token gone
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_token_rotation_changes_fingerprint_and_bumps_version(_rot_db):
    ga, _ = _rot_db
    created = await ga.create_account(
        platform="github", label="rotate-me",
        token="ghp_OLD_000000_oldd",
    )
    assert created["token_fingerprint"] == "…oldd"
    assert created["version"] == 0

    rotated = await ga.update_account(
        created["id"], updates={"token": "ghp_NEW_111111_neww"},
    )
    # New fingerprint reflects new last-4.
    assert rotated["token_fingerprint"] == "…neww"
    # Version bumped monotonically.
    assert rotated["version"] == 1

    # Subsequent GET sees the new state (read-after-write).
    fetched = await ga.get_account(created["id"])
    assert fetched is not None
    assert fetched["token_fingerprint"] == "…neww"
    assert fetched["version"] == 1


async def test_token_rotation_replaces_plaintext_in_storage(_rot_db):
    """After rotation, ``get_plaintext_token`` returns the new
    token; decrypting the stored ciphertext under the old plaintext
    is impossible (the ciphertext is a Fernet blob of the new
    plaintext). This is the "old token no longer works" assertion
    expressed at the storage layer.
    """
    ga, _ = _rot_db
    created = await ga.create_account(
        platform="gitlab", label="rotate-gl",
        token="glpat_OLD_oldd",
    )
    # Sanity: retrieves old plaintext initially.
    assert await ga.get_plaintext_token(created["id"]) == "glpat_OLD_oldd"

    await ga.update_account(
        created["id"], updates={"token": "glpat_NEW_neww"},
    )

    # Storage now returns the new plaintext, never the old.
    after = await ga.get_plaintext_token(created["id"])
    assert after == "glpat_NEW_neww"
    assert after != "glpat_OLD_oldd"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit trail carries a trace — but not the plaintext
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_rotation_writes_audit_row_without_plaintext(
    _rot_db,
):
    ga, pool = _rot_db
    import json

    created = await ga.create_account(
        platform="github", label="audit-rotate",
        token="ghp_AUDIT_OLD_oldd",
    )
    await ga.update_account(
        created["id"], updates={"token": "ghp_AUDIT_NEW_neww"},
    )

    # Pull all audit rows for this entity_id under this tenant.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, before_json, after_json FROM audit_log "
            "WHERE tenant_id = $1 AND entity_kind = 'git_account' "
            "AND entity_id = $2 ORDER BY id ASC",
            TENANT, created["id"],
        )
    actions = [r["action"] for r in rows]
    assert "git_account.create" in actions
    assert "git_account.update" in actions, (
        "rotation did not write a git_account.update audit row"
    )

    # CRITICAL security invariant: no row's before_json / after_json
    # contains either plaintext token. The audit chain carries only
    # the fingerprint (and metadata like version / updated_at).
    for r in rows:
        before_s = r["before_json"] or "{}"
        after_s = r["after_json"] or "{}"
        assert "ghp_AUDIT_OLD_oldd" not in before_s, (
            "PLAINTEXT LEAK in audit.before_json: old token echoed"
        )
        assert "ghp_AUDIT_OLD_oldd" not in after_s, (
            "PLAINTEXT LEAK in audit.after_json: old token echoed"
        )
        assert "ghp_AUDIT_NEW_neww" not in before_s, (
            "PLAINTEXT LEAK in audit.before_json: new token echoed"
        )
        assert "ghp_AUDIT_NEW_neww" not in after_s, (
            "PLAINTEXT LEAK in audit.after_json: new token echoed"
        )

    # The update row's diff mentions token_fingerprint (that's the
    # field that changed in the public-dict shape) — proving the
    # rotation was detected and recorded, without leaking the secret.
    update_row = next(r for r in rows if r["action"] == "git_account.update")
    after_obj = json.loads(update_row["after_json"] or "{}")
    assert "token_fingerprint" in after_obj, (
        "rotation audit row did not record token_fingerprint diff"
    )
    assert after_obj["token_fingerprint"] == "…neww"


async def test_audit_chain_intact_after_rotation(_rot_db):
    """``verify_chain`` must return (True, None) after rotation.
    A rotation that broke hash chaining (e.g. by not going through
    the canonical audit.log path) would fail this.
    """
    from backend import audit

    ga, _ = _rot_db
    c1 = await ga.create_account(
        platform="github", label="chain-a",
        token="ghp_chainA_last",
    )
    c2 = await ga.create_account(
        platform="gitlab", label="chain-b",
        token="glpat_chainB_last",
    )
    # Multiple rotations over different accounts.
    await ga.update_account(c1["id"], updates={"token": "ghp_chainA_rotA"})
    await ga.update_account(c2["id"], updates={"token": "glpat_chainB_rotB"})
    await ga.update_account(
        c1["id"], updates={"token": "ghp_chainA_rot2", "label": "chain-a-v2"},
    )
    ok, bad = await audit.verify_chain(tenant_id=TENANT)
    assert ok and bad is None, (
        f"audit chain broken after rotations; first bad id = {bad}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rotation covers all three secret columns
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_ssh_key_rotation_updates_fingerprint(_rot_db):
    ga, _ = _rot_db
    # Trailing 4 chars of each key payload deliberately differ so the
    # last-4 fingerprint detects the rotation. We don't wrap in a real
    # OpenSSH envelope here — the column stores whatever bytes the
    # caller supplied, and ``fingerprint`` just takes the last 4 chars.
    created = await ga.create_account(
        platform="gerrit", label="rotate-ssh",
        ssh_host="gerrit.example.com", ssh_port=29418,
        project="acme/platform",
        ssh_key="AAAA_OPENSSH_PRIVATE_KEY_PAYLOAD_oldd",
    )
    assert created["ssh_key_fingerprint"] == "…oldd"

    rotated = await ga.update_account(
        created["id"],
        updates={"ssh_key": "AAAA_OPENSSH_PRIVATE_KEY_PAYLOAD_neww"},
    )
    # Fingerprint changed to the new key's last-4.
    assert rotated["ssh_key_fingerprint"] == "…neww"
    assert rotated["ssh_key_fingerprint"] != created["ssh_key_fingerprint"]
    # Version bumped.
    assert rotated["version"] == 1


async def test_webhook_secret_rotation_updates_fingerprint(_rot_db):
    ga, _ = _rot_db
    created = await ga.create_account(
        platform="github", label="rotate-whs",
        token="ghp_whs_last",
        webhook_secret="webhook_OLD_hmac_oldd",
    )
    assert created["webhook_secret_fingerprint"] == "…oldd"

    rotated = await ga.update_account(
        created["id"],
        updates={"webhook_secret": "webhook_NEW_hmac_neww"},
    )
    assert rotated["webhook_secret_fingerprint"] == "…neww"
    # Token was NOT rotated — its fingerprint is unchanged.
    assert rotated["token_fingerprint"] == created["token_fingerprint"]
    assert rotated["version"] == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Empty-string clears / None leaves-alone semantics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_patch_empty_string_clears_token(_rot_db):
    ga, _ = _rot_db
    created = await ga.create_account(
        platform="github", label="clear-me",
        token="ghp_willbecleared_last",
    )
    assert created["token_fingerprint"] == "…last"

    cleared = await ga.update_account(
        created["id"], updates={"token": ""},
    )
    # Explicitly cleared — no more fingerprint.
    assert cleared["token_fingerprint"] == ""
    # Plaintext retrieval returns empty string (not None — row exists,
    # ciphertext column is empty).
    assert await ga.get_plaintext_token(created["id"]) == ""


async def test_patch_none_token_leaves_token_alone(_rot_db):
    """Per update_account docstring: ``token=None`` is a no-op on that
    column, allowing callers to PATCH other fields without disturbing
    the secret.
    """
    ga, _ = _rot_db
    created = await ga.create_account(
        platform="github", label="keep-token",
        token="ghp_keep_last",
    )

    patched = await ga.update_account(
        created["id"], updates={"token": None, "label": "keep-token-renamed"},
    )
    # Label changed but fingerprint did not.
    assert patched["label"] == "keep-token-renamed"
    assert patched["token_fingerprint"] == "…last"
    # Plaintext still matches the original.
    assert await ga.get_plaintext_token(created["id"]) == "ghp_keep_last"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Monotonic version across multiple rotations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_version_monotonic_across_multi_rotation(_rot_db):
    ga, _ = _rot_db
    created = await ga.create_account(
        platform="github", label="multi-rot", token="ghp_r0_last",
    )
    assert created["version"] == 0

    v1 = await ga.update_account(created["id"], updates={"token": "ghp_r1_last"})
    assert v1["version"] == 1

    v2 = await ga.update_account(created["id"], updates={"token": "ghp_r2_last"})
    assert v2["version"] == 2

    v3 = await ga.update_account(created["id"], updates={"label": "v3"})
    assert v3["version"] == 3

    v4 = await ga.update_account(
        created["id"], updates={"webhook_secret": "hmac_v4_last"},
    )
    assert v4["version"] == 4
