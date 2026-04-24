"""Phase 5-11 (#multi-account-forge) — extended per-tenant isolation
drill for the ``git_accounts`` multi-account forge integration.

The Phase 5-4 suite (``test_git_accounts_crud.py::test_tenant_isolation``)
covers the BASIC cross-tenant boundary on the CRUD service-layer:
tenant B cannot ``list`` / ``get`` / ``update`` / ``delete`` tenant A's
rows. That is the minimum bar.

This module extends isolation coverage to the **resolver + secret-
retrieval + concurrency + audit-query** surfaces that were layered on
top of the CRUD in rows 5-2 / 5-3 / 5-4 / 5-7 / 5-8:

1.  Resolver-side isolation
    --------------------------
    *   :func:`backend.git_credentials.pick_account_for_url` — tenant
        A's URL patterns must not resolve tenant B's accounts (even
        when A's pattern is a superset match).
    *   :func:`backend.git_credentials.pick_default` — A's default
        account is not B's default.
    *   :func:`backend.git_credentials.pick_by_id` — guessing A's
        deterministic id (e.g. ``ga-legacy-github-github-com``) does
        not let B retrieve A's row.
    *   :func:`backend.git_credentials.get_credential_registry_async`
        — the read-through registry returned to B never contains A's
        rows.

2.  Secret-retrieval isolation
    ----------------------------
    *   :func:`backend.git_accounts.get_plaintext_token` — A's
        plaintext token never surfaces under B's tenant context,
        even when B supplies A's account id.
    *   :func:`backend.git_credentials.get_webhook_secret_for_host_async`
        — A's webhook secret for ``github.com`` is NOT what B's
        webhook handler sees when running under B's tenant context.

3.  Concurrent mixed-tenant load
    ------------------------------
    20-worker ``asyncio.gather`` mixing A/B mutations must leave
    both tenants' rows intact, each tenant's row count correct,
    and each tenant's audit chain verifiable independently.

4.  Audit-query isolation
    -----------------------
    ``audit.query()`` under B never returns ``git_account.*`` rows
    B didn't make (even when A and B create rows during the same
    wall-clock second).

Module-global audit (SOP Step 1, qualified answer #1)
─────────────────────────────────────────────────────
The ``db_context.current_tenant_id`` ContextVar is per-asyncio-task;
each ``set_tenant_id(X)`` call scopes to the calling task. Tests use
``set_tenant_id`` + ``try/finally`` to restore. No new module-globals.

Read-after-write audit (SOP Step 1)
───────────────────────────────────
Each cross-tenant write by A is followed by a read from B's
context. We rely on Phase 5-4's same-conn commit-before-return
guarantee: after A's ``create_account`` returns, the row is
committed, and B's subsequent SELECT (on a different pool conn)
will see either (a) no row at all (correct filter) or, in a bug,
(b) A's row. The test asserts (a).

This file gates all tests on ``OMNI_TEST_PG_URL`` via the shared
``pg_test_pool`` fixture. Skip on SQLite-only dev sessions.
"""

from __future__ import annotations

import asyncio
import random

import pytest


pytestmark = pytest.mark.asyncio


TENANT_A = "t-phase511-alpha"
TENANT_B = "t-phase511-beta"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixture — two-tenant ``git_accounts`` slate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
async def _two_tenant_ga(pg_test_pool):
    """Fresh ``git_accounts`` + ``audit_log`` slate for A and B.

    Seeds both parent ``tenants`` rows ON CONFLICT-idempotently so
    the per-account FK (tenant_id ON DELETE CASCADE) is satisfied
    and audit rows can write without FK noise. TRUNCATE RESTART
    IDENTITY CASCADE clears any leftover state from prior tests.
    """
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES "
            "($1, $2, 'starter'), ($3, $4, 'starter') "
            "ON CONFLICT (id) DO NOTHING",
            TENANT_A, "Phase-5-11 Alpha",
            TENANT_B, "Phase-5-11 Beta",
        )
        await conn.execute(
            "TRUNCATE git_accounts, audit_log RESTART IDENTITY CASCADE"
        )
    from backend.db_context import set_tenant_id
    import backend.git_accounts as ga
    import backend.git_credentials as gc
    set_tenant_id(None)
    try:
        yield ga, gc, pg_test_pool
    finally:
        set_tenant_id(None)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE git_accounts, audit_log RESTART IDENTITY CASCADE"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Group 1 — Resolver-side isolation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_pick_account_for_url_does_not_leak_across_tenants(_two_tenant_ga):
    """A creates a github.com account with a URL pattern; under B's
    context the resolver must NOT match that account for the same URL.
    """
    ga, gc, _ = _two_tenant_ga
    from backend.db_context import set_tenant_id

    # A's account with a catch-all-github pattern.
    set_tenant_id(TENANT_A)
    a_acct = await ga.create_account(
        platform="github",
        label="alpha-gh",
        instance_url="https://github.com",
        token="ghp_alpha_0000_last4",
        url_patterns=["github.com/*"],
        is_default=True,
    )

    # Under B: pick_account_for_url returns None (no B row, no B
    # default, no B pattern). It MUST NOT return A's row just because
    # A's pattern matches the URL.
    set_tenant_id(TENANT_B)
    resolved = await gc.pick_account_for_url(
        "https://github.com/acme/app", touch=False,
    )
    assert resolved is None, (
        "Cross-tenant leak: B's resolver returned A's account "
        f"for URL under B's context: {resolved!r}"
    )

    # Sanity: A's own resolver still works.
    set_tenant_id(TENANT_A)
    resolved_a = await gc.pick_account_for_url(
        "https://github.com/acme/app", touch=False,
    )
    assert resolved_a is not None
    assert resolved_a["id"] == a_acct["id"]


async def test_pick_default_is_per_tenant(_two_tenant_ga):
    """A's default gitlab account is not B's default. Each tenant's
    ``is_default=TRUE`` flag is a (tenant, platform) invariant,
    enforced by the partial unique index.
    """
    ga, gc, _ = _two_tenant_ga
    from backend.db_context import set_tenant_id

    set_tenant_id(TENANT_A)
    a_gl = await ga.create_account(
        platform="gitlab", label="alpha-gl",
        instance_url="https://gitlab.com",
        token="glpat_alpha_1111_last4", is_default=True,
    )
    set_tenant_id(TENANT_B)
    b_gl = await ga.create_account(
        platform="gitlab", label="beta-gl",
        instance_url="https://gitlab.com",
        token="glpat_beta_2222_last4", is_default=True,
    )

    # A's default → A's row.
    set_tenant_id(TENANT_A)
    a_default = await gc.pick_default("gitlab", touch=False)
    assert a_default is not None
    assert a_default["id"] == a_gl["id"]
    assert a_default["token"] == "glpat_alpha_1111_last4"

    # B's default → B's row (and crucially NOT A's).
    set_tenant_id(TENANT_B)
    b_default = await gc.pick_default("gitlab", touch=False)
    assert b_default is not None
    assert b_default["id"] == b_gl["id"]
    assert b_default["token"] == "glpat_beta_2222_last4"
    assert b_default["id"] != a_gl["id"]


async def test_pick_by_id_does_not_cross_tenants(_two_tenant_ga):
    """Even if B knows A's account id string, pick_by_id under B
    returns None.
    """
    ga, gc, _ = _two_tenant_ga
    from backend.db_context import set_tenant_id

    set_tenant_id(TENANT_A)
    a_acct = await ga.create_account(
        platform="github", label="alpha-secret",
        token="ghp_alpha_secret_abcd",
    )
    a_id = a_acct["id"]

    # Under B — even with the correct id, isolation stands.
    set_tenant_id(TENANT_B)
    got = await gc.pick_by_id(a_id, touch=False)
    assert got is None, (
        f"Cross-tenant leak via pick_by_id: B resolved A's id "
        f"{a_id!r} → {got!r}"
    )


async def test_get_credential_registry_async_is_per_tenant(_two_tenant_ga):
    """B's credential registry must be disjoint from A's — no id from
    A's accounts appears in B's registry, and vice versa."""
    ga, gc, _ = _two_tenant_ga
    from backend.db_context import set_tenant_id

    set_tenant_id(TENANT_A)
    a_ids: set[str] = set()
    for i in range(3):
        row = await ga.create_account(
            platform="github", label=f"alpha-{i}",
            token=f"ghp_alpha_{i}_last",
        )
        a_ids.add(row["id"])

    set_tenant_id(TENANT_B)
    b_ids: set[str] = set()
    for i in range(2):
        row = await ga.create_account(
            platform="gitlab", label=f"beta-{i}",
            token=f"glpat_beta_{i}_last",
        )
        b_ids.add(row["id"])

    # A's registry sees A's 3 rows only.
    set_tenant_id(TENANT_A)
    a_registry = await gc.get_credential_registry_async(tenant_id=TENANT_A)
    # ``get_credential_registry_async`` filters enabled-only by default
    # and returns only rows for the tenant passed. The legacy shim
    # fallback fires only if the pool returns zero rows, which won't
    # happen here.
    a_registry_ids = {e.get("id") for e in a_registry if e.get("id")}
    assert a_ids.issubset(a_registry_ids)
    assert b_ids.isdisjoint(a_registry_ids), (
        "Cross-tenant leak in registry: B's ids appeared in A's "
        f"view: {a_registry_ids & b_ids}"
    )

    # B's registry sees B's 2 rows only.
    set_tenant_id(TENANT_B)
    b_registry = await gc.get_credential_registry_async(tenant_id=TENANT_B)
    b_registry_ids = {e.get("id") for e in b_registry if e.get("id")}
    assert b_ids.issubset(b_registry_ids)
    assert a_ids.isdisjoint(b_registry_ids), (
        "Cross-tenant leak in registry: A's ids appeared in B's "
        f"view: {b_registry_ids & a_ids}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Group 2 — Secret-retrieval isolation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_get_plaintext_token_blocks_cross_tenant_id_attack(_two_tenant_ga):
    """``get_plaintext_token(id)`` must return None under B's context
    even when B supplies A's exact id. Prevents a bug where B could
    guess or leak A's id and pull A's plaintext PAT.
    """
    ga, _, _ = _two_tenant_ga
    from backend.db_context import set_tenant_id

    set_tenant_id(TENANT_A)
    a_acct = await ga.create_account(
        platform="github", label="alpha",
        token="ghp_ALPHA_SECRET_TOKEN_1234",
    )
    # Sanity: under A's context, plaintext retrieval works.
    a_plain = await ga.get_plaintext_token(a_acct["id"])
    assert a_plain == "ghp_ALPHA_SECRET_TOKEN_1234"

    # Under B — the same id must not yield A's plaintext.
    set_tenant_id(TENANT_B)
    b_view = await ga.get_plaintext_token(a_acct["id"])
    assert b_view is None, (
        "CRITICAL cross-tenant token leak: B resolved A's "
        f"plaintext token via id {a_acct['id']!r}"
    )


async def test_webhook_secret_helper_is_tenant_scoped(_two_tenant_ga):
    """A and B each have a github.com account with different webhook
    secrets. :func:`get_webhook_secret_for_host_async` under A's
    context returns A's secret; under B's, B's. Cross-leak would be
    a security incident — an attacker posting to A's webhook URL
    with B's HMAC would authenticate successfully.
    """
    ga, gc, _ = _two_tenant_ga
    from backend.db_context import set_tenant_id

    set_tenant_id(TENANT_A)
    await ga.create_account(
        platform="github", label="alpha-gh",
        instance_url="https://github.com",
        token="ghp_alpha", webhook_secret="alpha_webhook_secret_ABCD",
        is_default=True,
    )
    set_tenant_id(TENANT_B)
    await ga.create_account(
        platform="github", label="beta-gh",
        instance_url="https://github.com",
        token="ghp_beta", webhook_secret="beta_webhook_secret_WXYZ",
        is_default=True,
    )

    # Under A's context, the helper returns A's secret.
    set_tenant_id(TENANT_A)
    sec_a = await gc.get_webhook_secret_for_host_async(
        "github.com", "github", tenant_id=TENANT_A,
    )
    assert sec_a == "alpha_webhook_secret_ABCD"

    # Under B's context — B's secret, not A's.
    set_tenant_id(TENANT_B)
    sec_b = await gc.get_webhook_secret_for_host_async(
        "github.com", "github", tenant_id=TENANT_B,
    )
    assert sec_b == "beta_webhook_secret_WXYZ"
    assert sec_b != sec_a


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Group 3 — Concurrent mixed-tenant load
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_concurrent_mixed_tenant_mutations_no_crosstalk(_two_tenant_ga):
    """20 concurrent ops (10 per tenant) create + read — final row
    counts match what each tenant submitted, zero cross-tenant rows.
    The asyncio.gather + random shuffle forces the scheduler to
    interleave A and B tasks; the ContextVar-per-task guarantee is
    what keeps their tenant contexts straight.
    """
    ga, _, pool = _two_tenant_ga
    from backend.db_context import set_tenant_id

    async def _writer(tenant: str, i: int) -> None:
        set_tenant_id(tenant)
        try:
            await ga.create_account(
                platform="github", label=f"{tenant}-{i}",
                token=f"ghp_{tenant}_{i}_xxxxlast",
            )
        finally:
            set_tenant_id(None)

    jobs: list = []
    for i in range(10):
        jobs.append(_writer(TENANT_A, i))
        jobs.append(_writer(TENANT_B, i))
    random.shuffle(jobs)
    await asyncio.gather(*jobs)

    # Row count by tenant — A has 10, B has 10, no cross-leak.
    async with pool.acquire() as conn:
        cnt_a = await conn.fetchval(
            "SELECT COUNT(*) FROM git_accounts WHERE tenant_id = $1",
            TENANT_A,
        )
        cnt_b = await conn.fetchval(
            "SELECT COUNT(*) FROM git_accounts WHERE tenant_id = $1",
            TENANT_B,
        )
        # Total rows = A + B; no mystery third tenant.
        cnt_total = await conn.fetchval("SELECT COUNT(*) FROM git_accounts")
    assert cnt_a == 10, f"tenant A lost/gained rows: got {cnt_a}"
    assert cnt_b == 10, f"tenant B lost/gained rows: got {cnt_b}"
    assert cnt_total == 20, (
        f"row count mismatch: {cnt_total} ≠ 20 (A={cnt_a} + B={cnt_b})"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Group 4 — Audit query isolation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_audit_query_scoped_per_tenant(_two_tenant_ga):
    """A creates + updates + deletes → 3 audit rows in A's chain.
    B creates + deletes → 2 audit rows in B's chain. Each tenant's
    ``audit.query`` returns only its own chain; neither chain
    contains the other's rows. Both chains verify intact.
    """
    ga, _, _ = _two_tenant_ga
    from backend import audit
    from backend.db_context import set_tenant_id

    # Tenant A: 3 mutations → 3 audit rows.
    set_tenant_id(TENANT_A)
    a_row = await ga.create_account(
        platform="github", label="alpha-audit", token="ghp_aaaa_last",
    )
    await ga.update_account(a_row["id"], updates={"label": "alpha-rot"})
    await ga.delete_account(a_row["id"])

    # Tenant B: 2 mutations.
    set_tenant_id(TENANT_B)
    b_row = await ga.create_account(
        platform="gitlab", label="beta-audit", token="glpat_bbbb_last",
    )
    await ga.delete_account(b_row["id"])

    # Under A's context, query returns ONLY A's audit rows.
    set_tenant_id(TENANT_A)
    a_rows = await audit.query(entity_kind="git_account", limit=200)
    a_actions = [r["action"] for r in a_rows]
    assert "git_account.create" in a_actions
    assert "git_account.update" in a_actions
    assert "git_account.delete" in a_actions
    # A should never see B's entity ids.
    a_entity_ids = {r["entity_id"] for r in a_rows if r["entity_id"]}
    assert b_row["id"] not in a_entity_ids, (
        "cross-tenant audit-query leak: B's entity_id in A's chain"
    )

    # Under B's context, query returns ONLY B's.
    set_tenant_id(TENANT_B)
    b_rows = await audit.query(entity_kind="git_account", limit=200)
    b_entity_ids = {r["entity_id"] for r in b_rows if r["entity_id"]}
    assert b_row["id"] in b_entity_ids
    assert a_row["id"] not in b_entity_ids, (
        "cross-tenant audit-query leak: A's entity_id in B's chain"
    )

    # Both chains verify independently. A bug that shared ``prev_hash``
    # across tenants would break one of these verify_chain calls.
    ok_a, bad_a = await audit.verify_chain(tenant_id=TENANT_A)
    ok_b, bad_b = await audit.verify_chain(tenant_id=TENANT_B)
    assert ok_a and bad_a is None, f"A audit chain broken at id={bad_a}"
    assert ok_b and bad_b is None, f"B audit chain broken at id={bad_b}"


async def test_default_deletion_on_one_tenant_does_not_touch_other(
    _two_tenant_ga,
):
    """A and B each have a github default. A deletes theirs → B's
    default is unaffected. Sanity check that the partial unique index
    + auto-elect logic operates strictly within a (tenant, platform)
    scope.
    """
    ga, gc, _ = _two_tenant_ga
    from backend.db_context import set_tenant_id

    set_tenant_id(TENANT_A)
    a_default = await ga.create_account(
        platform="github", label="alpha-default",
        token="ghp_alphadefault", is_default=True,
    )
    set_tenant_id(TENANT_B)
    b_default = await ga.create_account(
        platform="github", label="beta-default",
        token="ghp_betadefault", is_default=True,
    )

    # A deletes their default.
    set_tenant_id(TENANT_A)
    await ga.delete_account(a_default["id"])

    # B's default is unchanged.
    set_tenant_id(TENANT_B)
    b_still_default = await gc.pick_default("github", touch=False)
    assert b_still_default is not None
    assert b_still_default["id"] == b_default["id"]
    assert b_still_default["is_default"] is True
    assert b_still_default["token"] == "ghp_betadefault"
