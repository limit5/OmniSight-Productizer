"""Y4 (#280) row 7 — per-project quota override resolver + oversell guard.

Per-project ``disk_budget_bytes`` and ``llm_budget_tokens`` columns on
the ``projects`` table (alembic 0033) are nullable: ``NULL`` means
*inherit the tenant's plan default*; a non-``NULL`` value carves a
slice of the tenant's plan budget exclusively for that project.

This module owns the two derived behaviours:

  1. **Inheritance resolution**
     ``resolve_project_quota(tenant_plan, project_disk_budget,
     project_llm_budget) -> ResolvedProjectQuota`` returns the effective
     numbers a downstream consumer (workspace allocator, LLM router,
     dashboard widget) should treat as authoritative — substituting the
     tenant plan default whenever the project column is ``NULL`` and
     surfacing a per-dimension ``inherited`` flag for UIs that want to
     render "inherited from tenant" copy.

  2. **Oversell guard**
     ``check_disk_budget_oversell(...)`` and
     ``check_llm_budget_oversell(...)`` enforce the TODO row literal:
     ``Σ(non-NULL project budgets) ≤ tenant 總額``. Each consults the
     ``projects`` table for the sum of all *other* live (non-archived)
     projects' overrides on the same tenant; if that sum plus the
     proposed new value exceeds the tenant's plan total, raises
     ``ProjectBudgetOversell``. Callers in ``tenant_projects.py`` (POST
     create, PATCH update) translate the exception into a 409 response.

The tenant plan disk total is sourced from the existing
``backend/tenant_quota.py::PLAN_DISK_QUOTAS`` (the M2 table). The LLM
token total has no pre-existing surface so ``PLAN_LLM_TOKEN_QUOTAS``
ships here as the new source of truth — the numbers mirror the disk
quota tier ratios so a tenant on the same plan gets a consistent
"size" for both dimensions.

Module-global state audit
─────────────────────────
``PLAN_LLM_TOKEN_QUOTAS`` (dict[str, int]) is module-level immutable;
every uvicorn worker derives the same value from source — qualifying
answer #1 from the SOP. The oversell guard reads the projects table
through the caller-supplied PG connection and is intended to be called
inside the same transaction that holds the per-tenant
``pg_advisory_xact_lock`` (see ``tenant_projects.py``); cross-worker
concurrency is therefore PG-coordinated — qualifying answer #2.

Read-after-write timing audit
─────────────────────────────
The oversell guard issues a single ``SELECT COALESCE(SUM(...), 0)``
inside the caller's transaction. As long as the caller has taken the
per-tenant advisory lock, the read sees a quiescent set of project
budgets — no concurrent UPDATE / INSERT can squeeze its own
contribution between this SELECT and the caller's INSERT/UPDATE.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.tenant_quota import PLAN_DISK_QUOTAS, DEFAULT_PLAN


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Plan-tier LLM token totals (per-tenant ceilings)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Mirrors the disk quota tier ratios (free 5 GiB → enterprise 500 GiB
# is a 100x spread; same spread used here from 1M tokens → 1B tokens).
# Treated as a per-tenant *ceiling* for the oversell guard — a tenant
# on the ``free`` plan cannot allocate more than 1M tokens worth of
# project-level overrides in aggregate. Downstream consumers (LLM
# router, billing) are free to interpret the unit (per-month vs. per-
# day vs. lifetime) however they choose; this module only owns the
# arithmetic.

PLAN_LLM_TOKEN_QUOTAS: dict[str, int] = {
    "free":          1_000_000,    # 1M tokens
    "starter":      10_000_000,    # 10M
    "pro":         100_000_000,    # 100M
    "enterprise":  1_000_000_000,  # 1B
}


def tenant_disk_total_bytes(plan: str | None) -> int:
    """Return the tenant's plan-tier disk ceiling in bytes.

    Falls back to the ``free`` plan if ``plan`` is unknown / NULL —
    same posture as ``backend.tenant_quota.quota_for_plan``.
    """
    quota = PLAN_DISK_QUOTAS.get(plan or DEFAULT_PLAN, PLAN_DISK_QUOTAS[DEFAULT_PLAN])
    return quota.hard_bytes


def tenant_llm_total_tokens(plan: str | None) -> int:
    """Return the tenant's plan-tier LLM token ceiling."""
    return PLAN_LLM_TOKEN_QUOTAS.get(
        plan or DEFAULT_PLAN, PLAN_LLM_TOKEN_QUOTAS[DEFAULT_PLAN],
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Inheritance resolver
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class ResolvedProjectQuota:
    """Effective (post-inheritance) per-project quota.

    Both ``disk_inherited`` / ``llm_inherited`` track whether the
    column on the ``projects`` row was NULL (i.e. "inherited from
    tenant plan default") so UIs can render "inherited" copy. A
    consumer that only cares about the effective numbers can ignore
    the flags.
    """

    disk_budget_bytes: int
    llm_budget_tokens: int
    disk_inherited: bool
    llm_inherited: bool
    tenant_plan: str = field(default=DEFAULT_PLAN)


def resolve_project_quota(
    tenant_plan: str | None,
    project_disk_budget: int | None,
    project_llm_budget: int | None,
) -> ResolvedProjectQuota:
    """Coalesce ``project.X ?? tenant_plan_total(X)`` per dimension.

    NULL on a project column → fall back to the tenant's plan total.
    Non-NULL → use the project value verbatim (even if it exceeds the
    tenant total; the oversell guard owns rejection of that case at
    write time, not the resolver at read time).
    """
    plan = tenant_plan or DEFAULT_PLAN
    disk_inherited = project_disk_budget is None
    llm_inherited = project_llm_budget is None
    effective_disk = (
        tenant_disk_total_bytes(plan) if disk_inherited else project_disk_budget
    )
    effective_llm = (
        tenant_llm_total_tokens(plan) if llm_inherited else project_llm_budget
    )
    return ResolvedProjectQuota(
        disk_budget_bytes=int(effective_disk),
        llm_budget_tokens=int(effective_llm),
        disk_inherited=disk_inherited,
        llm_inherited=llm_inherited,
        tenant_plan=plan,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Oversell guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ProjectBudgetOversell(Exception):
    """Raised when setting a non-NULL project budget would push the
    tenant's Σ of project overrides past the plan ceiling.

    Carries enough context to surface a meaningful 409 body: the
    dimension being checked (``"disk_budget_bytes"`` or
    ``"llm_budget_tokens"``), the tenant plan + plan ceiling, the sum
    of the tenant's other projects' overrides, the value the caller
    proposed, and the would-be total.
    """

    def __init__(
        self,
        *,
        dimension: str,
        tenant_id: str,
        tenant_plan: str,
        tenant_total: int,
        existing_sum: int,
        proposed_value: int,
    ) -> None:
        self.dimension = dimension
        self.tenant_id = tenant_id
        self.tenant_plan = tenant_plan
        self.tenant_total = tenant_total
        self.existing_sum = existing_sum
        self.proposed_value = proposed_value
        self.would_be_total = existing_sum + proposed_value
        super().__init__(
            f"oversell on {dimension} for tenant {tenant_id!r} "
            f"(plan={tenant_plan!r}): "
            f"sum of other projects {existing_sum} + proposed "
            f"{proposed_value} = {self.would_be_total} > "
            f"tenant plan ceiling {tenant_total}"
        )

    def to_response_body(self) -> dict:
        """Render the exception as the 409 JSON body. Schema is part
        of the public contract — ``test_oversell_response_body_shape``
        in the tests file rejects field renames."""
        return {
            "detail": (
                f"per-project {self.dimension} override would exceed "
                f"the tenant's plan ceiling: Σ(other project budgets) "
                f"= {self.existing_sum} + proposed {self.proposed_value} "
                f"= {self.would_be_total} > tenant ({self.tenant_plan}) "
                f"total {self.tenant_total}"
            ),
            "tenant_id": self.tenant_id,
            "tenant_plan": self.tenant_plan,
            "dimension": self.dimension,
            "tenant_total": self.tenant_total,
            "existing_sum_of_other_projects": self.existing_sum,
            "proposed_value": self.proposed_value,
            "would_be_total": self.would_be_total,
        }


# ``$1`` = tenant_id; ``$2`` = exclude_project_id (text or NULL — the
# project being PATCH'd is excluded from the sum so its old value
# doesn't double-count against its new value). Archived projects are
# excluded so a soft-archived row's reservation stops taxing the live
# budget; the GC reaps it later. ``COALESCE(..., 0)`` handles the
# empty-set case (no other non-NULL overrides). The ``::bigint`` cast
# guards against the rare 9 GiB+ sum that would otherwise overflow
# asyncpg's int4 fast-path inference.
_SUM_PROJECT_DISK_BUDGETS_SQL = """
SELECT COALESCE(SUM(disk_budget_bytes), 0)::bigint AS total
FROM projects
WHERE tenant_id = $1
  AND archived_at IS NULL
  AND disk_budget_bytes IS NOT NULL
  AND ($2::text IS NULL OR id != $2)
"""

_SUM_PROJECT_LLM_BUDGETS_SQL = """
SELECT COALESCE(SUM(llm_budget_tokens), 0)::bigint AS total
FROM projects
WHERE tenant_id = $1
  AND archived_at IS NULL
  AND llm_budget_tokens IS NOT NULL
  AND ($2::text IS NULL OR id != $2)
"""


async def _check_oversell(
    conn,
    *,
    sql: str,
    dimension: str,
    tenant_id: str,
    tenant_plan: str,
    tenant_total: int,
    exclude_project_id: str | None,
    new_value: int | None,
) -> None:
    """Internal: shared body for the per-dimension oversell checks."""
    if new_value is None:
        return  # NULL clears override → no allocation, nothing to check
    existing = await conn.fetchval(sql, tenant_id, exclude_project_id)
    existing_sum = int(existing or 0)
    if existing_sum + new_value > tenant_total:
        raise ProjectBudgetOversell(
            dimension=dimension,
            tenant_id=tenant_id,
            tenant_plan=tenant_plan,
            tenant_total=tenant_total,
            existing_sum=existing_sum,
            proposed_value=new_value,
        )


async def check_disk_budget_oversell(
    conn,
    *,
    tenant_id: str,
    tenant_plan: str,
    exclude_project_id: str | None,
    new_value: int | None,
) -> None:
    """Raise ``ProjectBudgetOversell`` if setting ``disk_budget_bytes``
    on a project to ``new_value`` would push Σ over the tenant cap.

    ``new_value=None`` short-circuits (clearing an override allocates
    nothing, so it can never oversell). ``exclude_project_id`` is the
    PATCH-time excluded id (None for POST since the row doesn't exist
    yet)."""
    await _check_oversell(
        conn,
        sql=_SUM_PROJECT_DISK_BUDGETS_SQL,
        dimension="disk_budget_bytes",
        tenant_id=tenant_id,
        tenant_plan=tenant_plan,
        tenant_total=tenant_disk_total_bytes(tenant_plan),
        exclude_project_id=exclude_project_id,
        new_value=new_value,
    )


async def check_llm_budget_oversell(
    conn,
    *,
    tenant_id: str,
    tenant_plan: str,
    exclude_project_id: str | None,
    new_value: int | None,
) -> None:
    """Raise ``ProjectBudgetOversell`` if setting ``llm_budget_tokens``
    on a project to ``new_value`` would push Σ over the tenant cap."""
    await _check_oversell(
        conn,
        sql=_SUM_PROJECT_LLM_BUDGETS_SQL,
        dimension="llm_budget_tokens",
        tenant_id=tenant_id,
        tenant_plan=tenant_plan,
        tenant_total=tenant_llm_total_tokens(tenant_plan),
        exclude_project_id=exclude_project_id,
        new_value=new_value,
    )
