"""Y4 (#280) row 7 — drift guard for per-project quota override.

Two surfaces under test:

  * ``backend.project_quota`` — the new resolver +
    ``ProjectBudgetOversell`` exception + per-dimension SQL constants
    + ``check_disk_budget_oversell`` / ``check_llm_budget_oversell``.
  * ``backend.routers.tenant_projects`` — the POST + PATCH handlers
    now accept ``llm_budget_tokens`` in the body and run the oversell
    guard inside a per-tenant ``pg_advisory_xact_lock`` whenever a
    non-NULL budget override is set.

Drift guard families:
  (a) Module-level constants — ``PLAN_LLM_TOKEN_QUOTAS`` / lock prefix
      / patchable whitelist alignment.
  (b) Resolver — inheritance semantics per dimension + ``inherited``
      flag + non-NULL passthrough.
  (c) Oversell exception shape — public response body schema +
      computed ``would_be_total``.
  (d) SQL constants — PG ``$N`` placeholder, no-secret-leak,
      archived-exclusion, exclude-id support.
  (e) Pydantic body — ``CreateProjectRequest`` /
      ``PatchProjectRequest`` accept ``llm_budget_tokens``, reject
      negative, support tri-state explicit-null.
  (f) HTTP path: POST happy + LLM in response, POST oversell 409 on
      disk + on LLM, PATCH happy llm + PATCH oversell on bump,
      PATCH clear via null + oversell short-circuit on NULL, archived
      project not counted, sub-project sums correctly.
  (g) Self-fingerprint guard.
"""

from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP path depends on asyncpg pool — requires OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (a) Module-level constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_plan_llm_token_quotas_defines_all_four_plans():
    """Every plan in ``PLAN_DISK_QUOTAS`` (the M2 disk side) must also
    appear in ``PLAN_LLM_TOKEN_QUOTAS`` so the resolver returns a
    coherent number for every plan."""
    from backend.project_quota import PLAN_LLM_TOKEN_QUOTAS
    from backend.tenant_quota import PLAN_DISK_QUOTAS
    assert set(PLAN_LLM_TOKEN_QUOTAS.keys()) == set(PLAN_DISK_QUOTAS.keys())


def test_plan_llm_token_quotas_strictly_monotonic_by_tier():
    """A higher-priced plan must offer at least as many tokens — drift
    that flips the order would silently demote a paying tenant."""
    from backend.project_quota import PLAN_LLM_TOKEN_QUOTAS
    assert (
        PLAN_LLM_TOKEN_QUOTAS["free"]
        < PLAN_LLM_TOKEN_QUOTAS["starter"]
        < PLAN_LLM_TOKEN_QUOTAS["pro"]
        < PLAN_LLM_TOKEN_QUOTAS["enterprise"]
    )


def test_plan_llm_token_quotas_are_positive_integers():
    from backend.project_quota import PLAN_LLM_TOKEN_QUOTAS
    for plan, value in PLAN_LLM_TOKEN_QUOTAS.items():
        assert isinstance(value, int) and value > 0, (plan, value)


def test_project_quota_lock_prefix_is_per_tenant():
    """Lock key must be per-tenant so cross-tenant traffic does not
    contend on the same advisory lock."""
    from backend.routers.tenant_projects import _PROJECT_QUOTA_LOCK_PREFIX
    assert _PROJECT_QUOTA_LOCK_PREFIX == "omnisight_project_quota:"
    assert _PROJECT_QUOTA_LOCK_PREFIX.endswith(":")


def test_quota_lock_prefix_distinct_from_patch_lock_prefix():
    """The two advisory lock keys must hash to different values per
    tenant so a single PATCH that touches both parent_id AND a budget
    override doesn't ABBA-deadlock with another PATCH that takes the
    locks in the opposite order. The prefixes alone differ; same
    tenant_id suffix → distinct hashtext inputs → distinct PG advisory
    lock keys."""
    from backend.routers.tenant_projects import (
        _PROJECT_PATCH_LOCK_PREFIX, _PROJECT_QUOTA_LOCK_PREFIX,
    )
    assert _PROJECT_PATCH_LOCK_PREFIX != _PROJECT_QUOTA_LOCK_PREFIX
    # Order matters for the no-deadlock argument — see the inline
    # comment in patch_project. "patch:" < "quota:" alphabetically;
    # both PATCH paths take parent first then quota.
    assert _PROJECT_PATCH_LOCK_PREFIX < _PROJECT_QUOTA_LOCK_PREFIX


def test_patchable_fields_includes_llm_budget_tokens():
    """Y4 row 7 extends the patch surface — drift that drops it would
    silently make ``llm_budget_tokens`` unwritable from PATCH."""
    from backend.routers.tenant_projects import _PATCHABLE_PROJECT_FIELDS
    assert "llm_budget_tokens" in _PATCHABLE_PROJECT_FIELDS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) Resolver semantics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("plan", ["free", "starter", "pro", "enterprise"])
def test_resolver_inherits_both_when_project_columns_are_null(plan):
    """NULL on either column ⇒ tenant plan default substitutes."""
    from backend.project_quota import (
        resolve_project_quota,
        tenant_disk_total_bytes,
        tenant_llm_total_tokens,
    )
    q = resolve_project_quota(plan, None, None)
    assert q.disk_inherited is True
    assert q.llm_inherited is True
    assert q.disk_budget_bytes == tenant_disk_total_bytes(plan)
    assert q.llm_budget_tokens == tenant_llm_total_tokens(plan)
    assert q.tenant_plan == plan


def test_resolver_passes_through_non_null_overrides_verbatim():
    """A non-NULL override is used verbatim — even if it exceeds the
    tenant plan total. Rejection of oversell is a write-time concern;
    the resolver's job is read-time inheritance only."""
    from backend.project_quota import resolve_project_quota
    q = resolve_project_quota("free", 99 * 1024 ** 3, 999_000_000)
    assert q.disk_inherited is False
    assert q.llm_inherited is False
    assert q.disk_budget_bytes == 99 * 1024 ** 3
    assert q.llm_budget_tokens == 999_000_000


def test_resolver_mixed_inheritance_per_dimension():
    """Caller may have set only one of the two columns; each dimension
    inherits independently."""
    from backend.project_quota import (
        resolve_project_quota, tenant_llm_total_tokens,
    )
    q = resolve_project_quota("starter", 1024, None)
    assert q.disk_inherited is False
    assert q.llm_inherited is True
    assert q.disk_budget_bytes == 1024
    assert q.llm_budget_tokens == tenant_llm_total_tokens("starter")


def test_resolver_unknown_plan_falls_back_to_free():
    """Unknown plan → DEFAULT_PLAN ('free'), matching the
    ``tenant_quota.quota_for_plan`` posture."""
    from backend.project_quota import (
        resolve_project_quota,
        tenant_disk_total_bytes,
        tenant_llm_total_tokens,
    )
    q = resolve_project_quota("does-not-exist", None, None)
    assert q.tenant_plan == "does-not-exist"  # echoed back
    assert q.disk_budget_bytes == tenant_disk_total_bytes("free")
    assert q.llm_budget_tokens == tenant_llm_total_tokens("free")


def test_resolver_null_plan_falls_back_to_free():
    from backend.project_quota import (
        resolve_project_quota,
        tenant_disk_total_bytes,
    )
    q = resolve_project_quota(None, None, None)
    assert q.disk_budget_bytes == tenant_disk_total_bytes("free")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) ProjectBudgetOversell exception shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_oversell_exception_carries_full_context():
    from backend.project_quota import ProjectBudgetOversell
    exc = ProjectBudgetOversell(
        dimension="disk_budget_bytes",
        tenant_id="t-acme",
        tenant_plan="free",
        tenant_total=10_000,
        existing_sum=7_000,
        proposed_value=4_000,
    )
    assert exc.dimension == "disk_budget_bytes"
    assert exc.tenant_id == "t-acme"
    assert exc.tenant_plan == "free"
    assert exc.tenant_total == 10_000
    assert exc.existing_sum == 7_000
    assert exc.proposed_value == 4_000
    assert exc.would_be_total == 11_000


def test_oversell_response_body_shape():
    """The 409 body shape is part of the public REST contract — UIs
    rely on these field names. Drift here breaks the dashboard."""
    from backend.project_quota import ProjectBudgetOversell
    body = ProjectBudgetOversell(
        dimension="llm_budget_tokens",
        tenant_id="t-acme",
        tenant_plan="pro",
        tenant_total=100_000_000,
        existing_sum=80_000_000,
        proposed_value=30_000_000,
    ).to_response_body()
    assert set(body.keys()) == {
        "detail",
        "tenant_id",
        "tenant_plan",
        "dimension",
        "tenant_total",
        "existing_sum_of_other_projects",
        "proposed_value",
        "would_be_total",
    }
    assert body["dimension"] == "llm_budget_tokens"
    assert body["would_be_total"] == 110_000_000
    # Detail string is human-readable but stable enough for log
    # scrapers — pin the key arithmetic phrase.
    assert "Σ" in body["detail"] or "sum" in body["detail"].lower()
    assert "100000000" in body["detail"]  # tenant_total
    assert "110000000" in body["detail"]  # would_be_total


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) SQL constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_SUM_SQL_NAMES = (
    "_SUM_PROJECT_DISK_BUDGETS_SQL",
    "_SUM_PROJECT_LLM_BUDGETS_SQL",
)


@pytest.mark.parametrize("sql_name", _SUM_SQL_NAMES)
def test_sum_sql_uses_pg_placeholders_only(sql_name):
    from backend import project_quota as m
    sql = getattr(m, sql_name)
    assert "?" not in sql, f"{sql_name} contains SQLite-style ?"
    assert "$1" in sql, f"{sql_name} missing $1"
    assert "$2" in sql, f"{sql_name} missing $2"


@pytest.mark.parametrize("sql_name", _SUM_SQL_NAMES)
def test_sum_sql_does_not_leak_secret_columns(sql_name):
    from backend import project_quota as m
    sql = getattr(m, sql_name)
    for forbidden in ("password_hash", "oidc_subject", "token_hash"):
        assert forbidden not in sql, f"{sql_name} projects {forbidden}"


@pytest.mark.parametrize("sql_name", _SUM_SQL_NAMES)
def test_sum_sql_excludes_archived_projects(sql_name):
    """Archived projects' reservations stop taxing the live budget.
    The GC reaps them later (see ``gc_archived_projects``)."""
    from backend import project_quota as m
    sql = getattr(m, sql_name)
    assert "archived_at IS NULL" in sql, (
        f"{sql_name} missing archived_at predicate"
    )


@pytest.mark.parametrize("sql_name", _SUM_SQL_NAMES)
def test_sum_sql_supports_exclude_project_id(sql_name):
    """PATCH excludes the row being patched so its old value doesn't
    double-count against the new value."""
    from backend import project_quota as m
    sql = getattr(m, sql_name)
    assert "$2::text IS NULL OR id != $2" in sql, (
        f"{sql_name} missing exclude_project_id support"
    )


def test_disk_sum_sql_filters_non_null_only():
    from backend.project_quota import _SUM_PROJECT_DISK_BUDGETS_SQL
    assert "disk_budget_bytes IS NOT NULL" in _SUM_PROJECT_DISK_BUDGETS_SQL


def test_llm_sum_sql_filters_non_null_only():
    from backend.project_quota import _SUM_PROJECT_LLM_BUDGETS_SQL
    assert "llm_budget_tokens IS NOT NULL" in _SUM_PROJECT_LLM_BUDGETS_SQL


def test_sum_sqls_use_coalesce_zero():
    """Empty set ⇒ SUM is NULL by default; COALESCE(0) keeps the
    handler arithmetic simple."""
    from backend.project_quota import (
        _SUM_PROJECT_DISK_BUDGETS_SQL, _SUM_PROJECT_LLM_BUDGETS_SQL,
    )
    for sql in (_SUM_PROJECT_DISK_BUDGETS_SQL, _SUM_PROJECT_LLM_BUDGETS_SQL):
        assert "COALESCE(SUM(" in sql
        assert ", 0)" in sql


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (e) Pydantic body schemas
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_create_project_request_accepts_llm_budget_tokens():
    from backend.routers.tenant_projects import CreateProjectRequest
    body = CreateProjectRequest(
        product_line="embedded", name="P", slug="p",
        llm_budget_tokens=500_000,
    )
    assert body.llm_budget_tokens == 500_000


def test_create_project_request_rejects_negative_llm_budget_tokens():
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectRequest
    with pytest.raises(ValidationError):
        CreateProjectRequest(
            product_line="embedded", name="P", slug="p",
            llm_budget_tokens=-1,
        )


def test_create_project_request_accepts_zero_llm_budget_tokens():
    from backend.routers.tenant_projects import CreateProjectRequest
    body = CreateProjectRequest(
        product_line="embedded", name="P", slug="p",
        llm_budget_tokens=0,
    )
    assert body.llm_budget_tokens == 0


def test_create_project_request_llm_budget_tokens_defaults_to_none():
    from backend.routers.tenant_projects import CreateProjectRequest
    body = CreateProjectRequest(
        product_line="embedded", name="P", slug="p",
    )
    assert body.llm_budget_tokens is None


def test_patch_project_request_accepts_llm_budget_tokens():
    from backend.routers.tenant_projects import PatchProjectRequest
    body = PatchProjectRequest(llm_budget_tokens=42_000)
    assert body.llm_budget_tokens == 42_000
    assert "llm_budget_tokens" in body.model_fields_set


def test_patch_project_request_explicit_null_llm_budget_tokens_in_fields_set():
    """Tri-state semantics — explicit JSON null clears the override."""
    from backend.routers.tenant_projects import PatchProjectRequest
    body = PatchProjectRequest.model_validate({"llm_budget_tokens": None})
    assert "llm_budget_tokens" in body.model_fields_set
    assert body.llm_budget_tokens is None


def test_patch_project_request_rejects_negative_llm_budget_tokens():
    from pydantic import ValidationError
    from backend.routers.tenant_projects import PatchProjectRequest
    with pytest.raises(ValidationError):
        PatchProjectRequest(llm_budget_tokens=-100)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (f) Insert SQL surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_insert_sql_includes_llm_budget_tokens_column():
    """POST must persist ``llm_budget_tokens`` from the body — drift
    here would silently drop the value to NULL on insert."""
    from backend.routers.tenant_projects import _INSERT_PROJECT_SQL
    upper = _INSERT_PROJECT_SQL.upper()
    assert "LLM_BUDGET_TOKENS" in upper
    # Insert column list and RETURNING list must both reference it.
    insert_columns_part, _, _ = upper.partition("VALUES")
    returning_part = upper.split("RETURNING", 1)[1]
    assert "LLM_BUDGET_TOKENS" in insert_columns_part
    assert "LLM_BUDGET_TOKENS" in returning_part


def test_insert_sql_has_ten_value_placeholders():
    """10 = 9 base columns + 1 added in row 7 (llm_budget_tokens)."""
    from backend.routers.tenant_projects import _INSERT_PROJECT_SQL
    # Match $1..$N occurrences in the VALUES clause.
    values_part = _INSERT_PROJECT_SQL.split("VALUES", 1)[1].split(
        "ON CONFLICT", 1,
    )[0]
    placeholders = re.findall(r"\$\d+", values_part)
    assert len(placeholders) == 10, placeholders


def test_patch_sql_includes_llm_budget_tokens_set_clause():
    """The CASE WHEN $flag THEN value ELSE col END pattern must extend
    to llm_budget_tokens or PATCH silently drops it."""
    from backend.routers.tenant_projects import _PATCH_PROJECT_SQL
    upper = _PATCH_PROJECT_SQL.upper()
    assert "LLM_BUDGET_TOKENS = CASE WHEN" in upper


def test_patch_sql_placeholder_count_matches_field_count():
    """5 patchable fields + 2 path ids = 12 placeholders."""
    from backend.routers.tenant_projects import _PATCH_PROJECT_SQL
    placeholders = set(re.findall(r"\$\d+", _PATCH_PROJECT_SQL))
    assert placeholders == {f"${i}" for i in range(1, 13)}, placeholders


def test_fetch_tenant_sql_projects_plan_column():
    """The Y4 row 7 oversell guard reads tenant.plan to resolve the
    plan ceiling. Drift here would 500 (KeyError) on every POST/PATCH
    that sets a non-NULL budget."""
    from backend.routers.tenant_projects import _FETCH_TENANT_SQL
    upper = _FETCH_TENANT_SQL.upper()
    assert "PLAN" in upper
    assert "FROM TENANTS" in upper


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (g) HTTP path — happy + oversell branches (require live PG)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_tenant(pool, tid: str, plan: str = "free") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, $2, $3, 1) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"Test {tid}", plan,
        )


async def _purge_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project' "
            "AND entity_id IN (SELECT id FROM projects WHERE tenant_id = $1)",
            tid,
        )
        await conn.execute("DELETE FROM projects WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


@_requires_pg
async def test_post_project_with_llm_budget_tokens_201(client, pg_test_pool):
    """Body that sets only ``llm_budget_tokens`` (not disk) lands a row
    with that LLM value and NULL disk."""
    tid = "t-y4row7-llm"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="pro")
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded",
                "name": "LLM-only",
                "slug": "llm-only",
                "llm_budget_tokens": 5_000_000,
            },
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["llm_budget_tokens"] == 5_000_000
        assert body["disk_budget_bytes"] is None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_project_disk_budget_oversell_returns_409(
    client, pg_test_pool,
):
    """``free`` plan disk total is 10 GiB. Pre-seed a project with
    9 GiB disk override; a second POST asking for 2 GiB on the same
    tenant must be rejected with 409 + dimension=disk_budget_bytes."""
    from backend.project_quota import tenant_disk_total_bytes
    tid = "t-y4row7-disk-os"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="free")
        ceiling = tenant_disk_total_bytes("free")
        existing = ceiling * 9 // 10  # 9 GiB
        proposed = ceiling * 2 // 10  # 2 GiB → 11 GiB > 10 GiB
        res1 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "Big",
                "slug": "big", "disk_budget_bytes": existing,
            },
        )
        assert res1.status_code == 201, res1.text

        res2 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "Bigger",
                "slug": "bigger", "disk_budget_bytes": proposed,
            },
        )
        assert res2.status_code == 409, res2.text
        body = res2.json()
        assert body["dimension"] == "disk_budget_bytes"
        assert body["tenant_plan"] == "free"
        assert body["existing_sum_of_other_projects"] == existing
        assert body["proposed_value"] == proposed
        assert body["would_be_total"] == existing + proposed
        assert body["tenant_total"] == ceiling
        # The losing row must NOT have landed.
        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM projects WHERE tenant_id = $1",
                tid,
            )
        assert count == 1
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_project_llm_budget_oversell_returns_409(
    client, pg_test_pool,
):
    """``free`` plan LLM total is 1M tokens. Pre-seed 900k; a 200k
    POST must 409."""
    from backend.project_quota import tenant_llm_total_tokens
    tid = "t-y4row7-llm-os"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="free")
        ceiling = tenant_llm_total_tokens("free")
        existing = 900_000
        proposed = 200_000
        assert existing + proposed > ceiling
        res1 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "A",
                "slug": "a", "llm_budget_tokens": existing,
            },
        )
        assert res1.status_code == 201, res1.text

        res2 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "B",
                "slug": "b", "llm_budget_tokens": proposed,
            },
        )
        assert res2.status_code == 409, res2.text
        body = res2.json()
        assert body["dimension"] == "llm_budget_tokens"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_project_with_null_budgets_skips_oversell(
    client, pg_test_pool,
):
    """A POST that leaves both budget columns NULL ('inherit') skips
    the oversell guard entirely — the SUM check is not applicable."""
    tid = "t-y4row7-null"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="free")
        # Pre-fill the tenant cap with one project's override.
        from backend.project_quota import tenant_disk_total_bytes
        full = tenant_disk_total_bytes("free")
        res1 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "Full",
                "slug": "full", "disk_budget_bytes": full,
            },
        )
        assert res1.status_code == 201, res1.text
        # Now POST a NULL-budget project — must succeed (inherits, no allocation).
        res2 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "Inherits",
                "slug": "inherits",
            },
        )
        assert res2.status_code == 201, res2.text
        body = res2.json()
        assert body["disk_budget_bytes"] is None
        assert body["llm_budget_tokens"] is None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_disk_budget_excludes_self_from_sum(
    client, pg_test_pool,
):
    """PATCH bumping a project's own budget must NOT count its OLD
    value against the new value. Project A holds 80% of cap; PATCH
    that drops it to 40% must succeed (sum of OTHERS=0 + new=40%)."""
    from backend.project_quota import tenant_disk_total_bytes
    tid = "t-y4row7-self-exclude"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="pro")
        ceiling = tenant_disk_total_bytes("pro")
        existing = ceiling * 8 // 10
        new_value = ceiling * 4 // 10
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "P", "slug": "p",
                "disk_budget_bytes": existing,
            },
        )
        assert res.status_code == 201, res.text
        pid = res.json()["project_id"]

        # PATCH down — must succeed (own old value excluded).
        patched = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"disk_budget_bytes": new_value},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["disk_budget_bytes"] == new_value
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_project_oversell_when_bumping_above_cap(
    client, pg_test_pool,
):
    """Two projects each at 40% + a third at 10% (total 90%). PATCH
    bumping the third to 30% must 409 (would push to 110%)."""
    from backend.project_quota import tenant_disk_total_bytes
    tid = "t-y4row7-bump-os"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="pro")
        ceiling = tenant_disk_total_bytes("pro")
        a_size = ceiling * 4 // 10
        b_size = ceiling * 4 // 10
        c_initial = ceiling * 1 // 10
        c_new = ceiling * 3 // 10
        for slug, sz in (("a", a_size), ("b", b_size)):
            r = await client.post(
                f"/api/v1/tenants/{tid}/projects",
                json={
                    "product_line": "embedded", "name": slug.upper(),
                    "slug": slug, "disk_budget_bytes": sz,
                },
            )
            assert r.status_code == 201, r.text
        rc = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "C",
                "slug": "c", "disk_budget_bytes": c_initial,
            },
        )
        assert rc.status_code == 201, rc.text
        c_id = rc.json()["project_id"]

        # Bump C from 10% to 30% → total would be 40+40+30 = 110% → 409.
        patched = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{c_id}",
            json={"disk_budget_bytes": c_new},
        )
        assert patched.status_code == 409, patched.text
        body = patched.json()
        assert body["dimension"] == "disk_budget_bytes"
        # Existing sum excludes C's own old value → A + B = 80%.
        assert body["existing_sum_of_other_projects"] == a_size + b_size
        assert body["proposed_value"] == c_new
        # Row state untouched after 409.
        async with pg_test_pool.acquire() as conn:
            cur = await conn.fetchval(
                "SELECT disk_budget_bytes FROM projects WHERE id = $1",
                c_id,
            )
        assert cur == c_initial
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_clear_budget_via_explicit_null_skips_oversell(
    client, pg_test_pool,
):
    """Explicit ``null`` clears the override — never triggers oversell
    (allocation goes from N back to 0, can't possibly overshoot)."""
    from backend.project_quota import tenant_disk_total_bytes
    tid = "t-y4row7-clear"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="free")
        ceiling = tenant_disk_total_bytes("free")
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "P", "slug": "p",
                "disk_budget_bytes": ceiling,  # exactly the cap
            },
        )
        assert res.status_code == 201, res.text
        pid = res.json()["project_id"]
        patched = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}",
            json={"disk_budget_bytes": None},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["disk_budget_bytes"] is None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_archived_project_does_not_count_against_oversell(
    client, pg_test_pool,
):
    """A soft-archived project's reservation stops taxing the live
    budget so a tenant that fills its cap, archives, then POSTs a new
    overriding project must succeed."""
    from backend.project_quota import tenant_disk_total_bytes
    tid = "t-y4row7-archived"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="free")
        ceiling = tenant_disk_total_bytes("free")
        res1 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "Old",
                "slug": "old", "disk_budget_bytes": ceiling,
            },
        )
        assert res1.status_code == 201, res1.text
        old_pid = res1.json()["project_id"]
        # Archive the old project — its reservation no longer counts.
        arc = await client.post(
            f"/api/v1/tenants/{tid}/projects/{old_pid}/archive",
        )
        assert arc.status_code == 200, arc.text
        # Now a fresh override of half the cap must succeed.
        res2 = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "New",
                "slug": "new", "disk_budget_bytes": ceiling // 2,
            },
        )
        assert res2.status_code == 201, res2.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_audit_blob_for_create_includes_llm_budget_tokens(
    client, pg_test_pool,
):
    """The ``tenant_project_created`` audit ``after`` payload must
    include the new ``llm_budget_tokens`` field — accounting reconciles
    against this row."""
    tid = "t-y4row7-audit"
    try:
        await _seed_tenant(pg_test_pool, tid, plan="starter")
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects",
            json={
                "product_line": "embedded", "name": "P",
                "slug": "p", "llm_budget_tokens": 1_234,
            },
        )
        assert res.status_code == 201, res.text
        pid = res.json()["project_id"]
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT after_json FROM audit_log "
                "WHERE entity_kind = 'project' AND entity_id = $1 "
                "ORDER BY ts DESC LIMIT 1",
                pid,
            )
        assert row is not None
        blob = row["after_json"]
        # asyncpg may surface JSON as str or dict depending on pool init.
        import json as _json
        if isinstance(blob, str):
            blob = _json.loads(blob)
        assert blob.get("llm_budget_tokens") == 1_234
        # No secret leak.
        for forbidden in ("password_hash", "oidc_subject", "token_hash"):
            assert forbidden not in (str(blob)), (
                f"audit blob leaked {forbidden}: {blob!r}"
            )
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (h) Self-fingerprint guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Pre-commit fingerprint grep — any compat-shim residue (`_conn()` /
# `await conn.commit()` / `datetime('now')` / SQLite `?` placeholders)
# in the production files would silently break under asyncpg. The
# fingerprint pattern itself is the regex strings inside this guard;
# string-literal hits in the prod files are the only matches it should
# admit, and the production module here uses none.


def test_project_quota_module_is_fingerprint_clean():
    src = Path(
        "backend/project_quota.py"
    ).read_text(encoding="utf-8")
    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    matches = [
        (i + 1, line)
        for i, line in enumerate(src.splitlines())
        if fingerprint.search(line)
    ]
    assert not matches, (
        f"backend/project_quota.py has compat-shim fingerprints: {matches!r}"
    )


def test_tenant_projects_module_remains_fingerprint_clean():
    """The Y4 row 7 wiring did not regress the prod file fingerprint
    cleanliness established by row 1-6."""
    src = Path(
        "backend/routers/tenant_projects.py"
    ).read_text(encoding="utf-8")
    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    matches = [
        (i + 1, line)
        for i, line in enumerate(src.splitlines())
        if fingerprint.search(line)
    ]
    assert not matches, (
        f"backend/routers/tenant_projects.py has compat-shim "
        f"fingerprints: {matches!r}"
    )
