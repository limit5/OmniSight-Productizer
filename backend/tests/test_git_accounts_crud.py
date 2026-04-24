"""Phase 5-4 (#multi-account-forge) — git_accounts CRUD tests.

Covers the service-layer in :mod:`backend.git_accounts` (the router
is a thin wrapper that re-raises into HTTPException; testing the
service layer at the pool level gives us the full CRUD + resolver
contract without booting the FastAPI app):

* Create 3 ``github.com`` accounts with same host + different label
  + different ``url_patterns`` — verify :func:`pick_account_for_url`
  routes a matching URL to the correct account (Phase 5-4 row
  explicit requirement).
* Fingerprint-only list responses — plaintext tokens never leak.
* Default-per-(tenant, platform) invariant — a second ``is_default``
  create raises :class:`GitAccountConflict`.
* Delete-of-default semantics — both the ``refuse`` path and the
  ``auto_elect_new_default`` path covered.
* Tenant isolation — tenant B cannot list / update / delete tenant
  A's rows.
* Audit log receives a row per mutation (create / update / delete).

All PG tests are gated on ``OMNI_TEST_PG_URL`` via the shared
``pg_test_pool`` fixture; run skipped locally when the test PG
container isn't up.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


DEFAULT_TENANT = "t-default"
OTHER_TENANT = "t-ga-other"


@pytest.fixture()
async def _ga_db(pg_test_pool):
    """Fresh ``git_accounts`` slate + two seeded tenants.

    The test ``git_accounts`` rows have an FK to ``tenants`` (ON
    DELETE CASCADE) so we must ensure both tenants exist before
    the service inserts. TRUNCATE clears previous test pollution;
    CASCADE flows through audit_log / git_accounts ties.
    """
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES "
            "($1, $2, $3), ($4, $5, $6) "
            "ON CONFLICT (id) DO NOTHING",
            DEFAULT_TENANT, "Default", "starter",
            OTHER_TENANT, "Other", "starter",
        )
        await conn.execute(
            "TRUNCATE git_accounts, audit_log RESTART IDENTITY CASCADE"
        )
    from backend.db_context import set_tenant_id
    set_tenant_id(DEFAULT_TENANT)
    import backend.git_accounts as ga
    import backend.git_credentials as gc
    try:
        yield ga, gc
    finally:
        set_tenant_id(None)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE git_accounts, audit_log RESTART IDENTITY CASCADE"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Create / list / get
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_create_returns_public_dict_without_plaintext_token(_ga_db):
    ga, _ = _ga_db
    out = await ga.create_account(
        platform="github",
        label="acme prod",
        instance_url="https://github.com",
        token="ghp_supersecret1234",
    )
    assert out["platform"] == "github"
    assert out["label"] == "acme prod"
    # Must not echo the token or its ciphertext — only the fingerprint.
    assert "token" not in out
    assert "encrypted_token" not in out
    assert out["token_fingerprint"].endswith("1234")
    assert out["token_fingerprint"].startswith("…")
    assert out["id"].startswith("ga-")
    assert out["version"] == 0


async def test_list_uses_fingerprint_never_plaintext(_ga_db):
    ga, _ = _ga_db
    await ga.create_account(
        platform="github", label="main", token="ghp_zzzzzzzz_last4",
    )
    items = (await ga.list_accounts())
    assert len(items) == 1
    assert items[0]["token_fingerprint"] == "…ast4"
    assert "token" not in items[0]
    # No encrypted ciphertext either.
    assert "encrypted_token" not in items[0]


async def test_get_account_and_missing(_ga_db):
    ga, _ = _ga_db
    created = await ga.create_account(platform="github", label="one")
    fetched = await ga.get_account(created["id"])
    assert fetched is not None
    assert fetched["label"] == "one"
    assert await ga.get_account("ga-does-not-exist") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Core Phase-5-4 acceptance scenario:
#  3 github.com accounts with different labels + url_patterns,
#  verify resolve routes URLs correctly.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_three_github_accounts_resolve_by_url_pattern(_ga_db):
    ga, gc = _ga_db
    # 3 github.com accounts — same host, different labels + patterns.
    acme = await ga.create_account(
        platform="github",
        label="acme-corp",
        instance_url="https://github.com",
        token="ghp_acme_000001",
        url_patterns=["github.com/acme-corp/*"],
    )
    widgets = await ga.create_account(
        platform="github",
        label="widgets-inc",
        instance_url="https://github.com",
        token="ghp_widgets_02",
        url_patterns=["github.com/widgets-inc/*"],
    )
    personal = await ga.create_account(
        platform="github",
        label="personal",
        instance_url="https://github.com",
        token="ghp_personal_x9",
        is_default=True,  # fallback for any unmatched github.com URL
    )

    # Pattern-specific URLs route to the matching account.
    picked_acme = await gc.pick_account_for_url(
        "https://github.com/acme-corp/app", touch=False,
    )
    assert picked_acme is not None and picked_acme["id"] == acme["id"]

    picked_widgets = await gc.pick_account_for_url(
        "https://github.com/widgets-inc/mono", touch=False,
    )
    assert picked_widgets is not None
    assert picked_widgets["id"] == widgets["id"]

    # SSH form of the same repo resolves via the same pattern (5-3's
    # scheme-strip normalisation).
    picked_acme_ssh = await gc.pick_account_for_url(
        "git@github.com:acme-corp/infra.git", touch=False,
    )
    assert picked_acme_ssh is not None
    assert picked_acme_ssh["id"] == acme["id"]

    # An unmatched-but-same-host URL falls through to the default.
    picked_fallback = await gc.pick_account_for_url(
        "https://github.com/third-party/unknown-repo", touch=False,
    )
    assert picked_fallback is not None
    assert picked_fallback["id"] == personal["id"]

    # Listing returns all 3 accounts; plaintext tokens absent.
    items = await ga.list_accounts(platform="github")
    assert {r["id"] for r in items} == {
        acme["id"], widgets["id"], personal["id"],
    }
    assert all("token" not in r for r in items)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Default-per-platform invariant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_second_default_create_raises_conflict(_ga_db):
    ga, _ = _ga_db
    await ga.create_account(
        platform="github", label="p1", is_default=True,
    )
    with pytest.raises(ga.GitAccountConflict):
        await ga.create_account(
            platform="github", label="p2", is_default=True,
        )


async def test_patch_to_default_unsets_current_default(_ga_db):
    ga, _ = _ga_db
    p1 = await ga.create_account(
        platform="gitlab", label="p1", is_default=True,
    )
    p2 = await ga.create_account(
        platform="gitlab", label="p2", is_default=False,
    )
    # Flip p2 → default. p1 must be silently demoted in the same tx.
    out = await ga.update_account(p2["id"], updates={"is_default": True})
    assert out["is_default"] is True

    fresh = {row["id"]: row for row in await ga.list_accounts()}
    assert fresh[p2["id"]]["is_default"] is True
    assert fresh[p1["id"]]["is_default"] is False


async def test_patch_rotates_token_fingerprint_changes(_ga_db):
    ga, _ = _ga_db
    a = await ga.create_account(
        platform="github", label="rot", token="ghp_old_value_1111",
    )
    assert a["token_fingerprint"].endswith("1111")
    b = await ga.update_account(a["id"], updates={"token": "ghp_NEW_2222"})
    assert b["token_fingerprint"].endswith("2222")
    assert b["version"] > a["version"]


async def test_patch_unknown_field_raises(_ga_db):
    ga, _ = _ga_db
    a = await ga.create_account(platform="github", label="x")
    with pytest.raises(ValueError, match="Unknown update fields"):
        await ga.update_account(a["id"], updates={"nope": 1})


async def test_patch_not_found_raises(_ga_db):
    ga, _ = _ga_db
    with pytest.raises(ga.GitAccountNotFound):
        await ga.update_account(
            "ga-nonexistent", updates={"label": "whatever"},
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Delete — refuse-without-replacement AND auto-elect paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_delete_nondefault_just_deletes(_ga_db):
    ga, _ = _ga_db
    a = await ga.create_account(platform="gitlab", label="one", is_default=True)
    b = await ga.create_account(platform="gitlab", label="two")
    out = await ga.delete_account(b["id"])
    assert out["promoted_id"] is None
    # 'a' still default.
    remaining = await ga.list_accounts()
    assert len(remaining) == 1
    assert remaining[0]["id"] == a["id"]
    assert remaining[0]["is_default"] is True


async def test_delete_default_auto_elects_new_default(_ga_db):
    ga, _ = _ga_db
    d = await ga.create_account(
        platform="github", label="default-one", is_default=True,
    )
    runner_up = await ga.create_account(
        platform="github", label="runner-up",
    )
    out = await ga.delete_account(d["id"])
    assert out["promoted_id"] == runner_up["id"]

    fresh = await ga.list_accounts(platform="github")
    assert len(fresh) == 1
    assert fresh[0]["id"] == runner_up["id"]
    assert fresh[0]["is_default"] is True


async def test_delete_default_refuse_without_replacement(_ga_db):
    ga, _ = _ga_db
    d = await ga.create_account(
        platform="github", label="default", is_default=True,
    )
    # A second non-default account exists → auto_elect=False must
    # raise (the refuse path).
    await ga.create_account(platform="github", label="other")
    with pytest.raises(ga.GitAccountConflict):
        await ga.delete_account(d["id"], auto_elect_new_default=False)
    # Row remains.
    assert await ga.get_account(d["id"]) is not None


async def test_delete_sole_account_even_if_default_succeeds(_ga_db):
    """refuse path only fires when a replacement exists. A solo
    default on the platform is allowed to leave the tenant
    defaultless (otherwise we could never delete the last row)."""
    ga, _ = _ga_db
    d = await ga.create_account(
        platform="github", label="only-one", is_default=True,
    )
    out = await ga.delete_account(d["id"], auto_elect_new_default=False)
    assert out["promoted_id"] is None
    assert (await ga.list_accounts(platform="github")) == []


async def test_delete_missing_raises_not_found(_ga_db):
    ga, _ = _ga_db
    with pytest.raises(ga.GitAccountNotFound):
        await ga.delete_account("ga-nonexistent")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tenant isolation — A cannot touch B's rows
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_tenant_isolation(_ga_db):
    ga, _ = _ga_db
    from backend.db_context import set_tenant_id

    # Tenant A creates an account.
    set_tenant_id(DEFAULT_TENANT)
    a_row = await ga.create_account(
        platform="github", label="A-owned", token="ghp_tenantA_xyz",
    )
    assert len(await ga.list_accounts()) == 1

    # Switch to tenant B — list must be empty, detail-by-id must miss.
    set_tenant_id(OTHER_TENANT)
    assert await ga.list_accounts() == []
    assert await ga.get_account(a_row["id"]) is None

    # B cannot update A's row.
    with pytest.raises(ga.GitAccountNotFound):
        await ga.update_account(a_row["id"], updates={"label": "hijack"})
    # Nor delete it.
    with pytest.raises(ga.GitAccountNotFound):
        await ga.delete_account(a_row["id"])

    # Back to A — row is intact, unchanged.
    set_tenant_id(DEFAULT_TENANT)
    still_a = await ga.get_account(a_row["id"])
    assert still_a is not None
    assert still_a["label"] == "A-owned"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit log — each mutation writes a row
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_mutations_write_audit_rows(_ga_db, pg_test_pool):
    ga, _ = _ga_db
    created = await ga.create_account(
        platform="github", label="audit-test",
    )
    await ga.update_account(created["id"], updates={"label": "audit-rot"})
    await ga.delete_account(created["id"])

    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action FROM audit_log "
            "WHERE tenant_id = $1 AND entity_kind = 'git_account' "
            "ORDER BY id ASC",
            DEFAULT_TENANT,
        )
    actions = [r["action"] for r in rows]
    assert "git_account.create" in actions
    assert "git_account.update" in actions
    assert "git_account.delete" in actions


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Resolve endpoint helper — _classify_match tag
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_classify_match_tags(_ga_db):
    ga, _ = _ga_db
    # Pattern account
    pat = await ga.create_account(
        platform="github", label="pat",
        instance_url="https://github.com",
        url_patterns=["github.com/acme-corp/*"],
    )
    # Default account (no patterns)
    default = await ga.create_account(
        platform="github", label="default",
        instance_url="https://github.com",
        is_default=True,
    )
    from backend.routers.git_accounts import _classify_match

    # Pattern-URL → "url_pattern"
    r = await ga.get_account(pat["id"])
    assert _classify_match(
        r, "https://github.com/acme-corp/app",
    ) == "url_pattern"

    # Default row + a random path → "platform_default" when matched
    # via is_default path.
    r2 = await ga.get_account(default["id"])
    assert _classify_match(
        r2, "https://github.com/random/unknown",
    ) in {"platform_default", "exact_host"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  List masking stays consistent when ciphertext is absent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_no_token_leaves_empty_fingerprint(_ga_db):
    ga, _ = _ga_db
    a = await ga.create_account(
        platform="gerrit", label="ssh-only",
        ssh_host="gerrit.example.com", ssh_port=29418,
    )
    assert a["token_fingerprint"] == ""
    assert a["ssh_key_fingerprint"] == ""
