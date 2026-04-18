"""M5 — prewarm pool multi-tenant safety.

Verifies:
  * ``prewarm_policy`` config default + whitelist + shape validation.
  * ``per_tenant`` policy: tenant A's prewarm cannot be consumed by B.
  * ``shared`` policy: any tenant can consume any slot (legacy).
  * ``disabled`` policy: prewarm_for / consume / cancel_all no-op.
  * Launch-time ``/tmp`` cleanup fires on every consume regardless
    of policy (the primary M5 residue defence).
  * ``cancel_all(tenant_id=...)`` scopes to the owning tenant's
    bucket — does NOT clear other tenants' speculation.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend import sandbox_prewarm as pw
from backend.dag_schema import DAG, Task


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture(autouse=True)
def _reset():
    pw._reset_for_tests()
    yield
    pw._reset_for_tests()


@pytest.fixture
def policy_per_tenant(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "prewarm_policy", "per_tenant")
    yield


@pytest.fixture
def policy_shared(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "prewarm_policy", "shared")
    yield


@pytest.fixture
def policy_disabled(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "prewarm_policy", "disabled")
    yield


def _t(task_id: str, *, tier: str = "t1", toolchain: str = "cmake",
       depends_on=None, expected_output: str | None = None) -> Task:
    return Task(
        task_id=task_id,
        description=f"t {task_id}",
        required_tier=tier,
        toolchain=toolchain,
        inputs=[],
        expected_output=expected_output or f"build/{task_id}.bin",
        depends_on=depends_on or [],
    )


def _dag(tasks: list[Task], dag_id: str = "REQ-m5") -> DAG:
    return DAG(dag_id=dag_id, tasks=tasks)


@dataclass
class _FakeInfo:
    agent_id: str
    container_id: str = "cid-fake"
    tenant_id: str = "t-default"


def _starter_factory(store: dict[str, _FakeInfo], *, record_tenant=False):
    """Starter that captures the agent_id + optionally tenant_id kw."""
    async def starter(agent_id, workspace_path, *, tenant_id="t-default"):
        info = _FakeInfo(agent_id=agent_id, tenant_id=tenant_id)
        store[agent_id] = info
        return info
    if record_tenant:
        return starter

    # Back-compat variant: accepts only positional args. Exercises
    # ``_call_starter`` fallback path when the injected starter has
    # the older 2-arg signature.
    async def plain(agent_id, workspace_path):
        info = _FakeInfo(agent_id=agent_id)
        store[agent_id] = info
        return info
    return plain


def _make_stopper(stopped: list[str]):
    async def stopper(agent_id):
        stopped.append(agent_id)
        return True
    return stopper


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config — prewarm_policy validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_default_policy_is_per_tenant():
    from backend.config import Settings
    s = Settings()
    assert s.prewarm_policy == "per_tenant"


def test_get_policy_falls_back_on_unknown(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "prewarm_policy", "bogus-mode")
    assert pw.get_policy() == "per_tenant"


def test_get_policy_normalises_case(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "prewarm_policy", "Per_Tenant")
    assert pw.get_policy() == "per_tenant"


@pytest.mark.parametrize("val", ["disabled", "shared", "per_tenant"])
def test_get_policy_accepts_whitelist(monkeypatch, val):
    from backend.config import settings
    monkeypatch.setattr(settings, "prewarm_policy", val)
    assert pw.get_policy() == val


def test_validate_startup_config_rejects_bad_policy(monkeypatch):
    """Strict mode must refuse to boot on a typo."""
    from backend import config as cfg
    monkeypatch.setattr(cfg.settings, "prewarm_policy", "per-tenant")  # hyphen typo
    monkeypatch.setattr(cfg.settings, "debug", True)  # skip strict gate for other checks
    warnings = cfg.validate_startup_config(strict=False)
    assert any("PREWARM_POLICY" in w for w in warnings)


def test_validate_startup_config_warns_on_shared(monkeypatch):
    from backend import config as cfg
    monkeypatch.setattr(cfg.settings, "prewarm_policy", "shared")
    warnings = cfg.validate_startup_config(strict=False)
    assert any("shared" in w and "tenant-bucketed" in w for w in warnings)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  per_tenant — isolation enforcement
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_per_tenant_policy_buckets_by_tenant(policy_per_tenant, tmp_path):
    """A's prewarm + B's prewarm live in separate buckets."""
    started: dict = {}
    starter = _starter_factory(started, record_tenant=True)
    dag = _dag([_t("A"), _t("B")])

    # Same DAG, different tenants — both prewarm their own copy.
    await pw.prewarm_for(dag, tmp_path, depth=2, starter=starter,
                           tenant_id="t-alpha")
    await pw.prewarm_for(dag, tmp_path, depth=2, starter=starter,
                           tenant_id="t-beta")

    snap = pw.snapshot_by_tenant()
    assert set(snap.keys()) == {"t-alpha", "t-beta"}
    # 2 tasks × 2 tenants = 4 containers launched, each agent_id unique.
    assert len(started) == 4


@pytest.mark.asyncio
async def test_per_tenant_a_cannot_consume_b_slot(policy_per_tenant, tmp_path,
                                                    monkeypatch):
    """The core M5 acceptance test: tenant A may not steal B's prewarm."""
    # Stub the /tmp cleanup so we don't depend on real FS in unit tests.
    from backend import tenant_quota
    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        tenant_quota, "cleanup_tenant_tmp",
        lambda tid: cleanup_calls.append(tid) or 0,
    )

    started: dict = {}
    starter = _starter_factory(started, record_tenant=True)
    dag = _dag([_t("A")])

    # Tenant beta pre-warms task A.
    await pw.prewarm_for(dag, tmp_path, depth=1, starter=starter,
                           tenant_id="t-beta")

    # Tenant alpha attempts to consume — must miss.
    slot = await pw.consume("A", tenant_id="t-alpha")
    assert slot is None, "alpha must NOT consume beta's pre-warm"

    # And beta's slot is still sitting in its bucket waiting.
    snap = pw.snapshot_by_tenant()
    assert "t-beta" in snap and "A" in snap["t-beta"]

    # beta's own consume still hits.
    hit = await pw.consume("A", tenant_id="t-beta")
    assert hit is not None
    assert hit.tenant_id == "t-beta"


@pytest.mark.asyncio
async def test_per_tenant_cancel_scoped_to_tenant(policy_per_tenant, tmp_path):
    """cancel_all(tenant_id='t-alpha') must only drop alpha's bucket."""
    started: dict = {}
    stopped: list[str] = []
    starter = _starter_factory(started, record_tenant=True)

    await pw.prewarm_for(_dag([_t("A")]), tmp_path, depth=1,
                           starter=starter, tenant_id="t-alpha")
    await pw.prewarm_for(_dag([_t("B")]), tmp_path, depth=1,
                           starter=starter, tenant_id="t-beta")

    n = await pw.cancel_all(
        stopper=_make_stopper(stopped), tenant_id="t-alpha",
    )
    assert n == 1
    # Only alpha's agent was stopped.
    assert len(stopped) == 1
    assert "A" in stopped[0]

    # beta's bucket intact.
    snap = pw.snapshot_by_tenant()
    assert "t-beta" in snap
    assert "t-alpha" not in snap


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  shared — legacy behaviour preserved
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_shared_policy_lets_any_tenant_consume(policy_shared, tmp_path,
                                                       monkeypatch):
    from backend import tenant_quota
    monkeypatch.setattr(tenant_quota, "cleanup_tenant_tmp", lambda tid: 0)

    started: dict = {}
    starter = _starter_factory(started, record_tenant=True)
    await pw.prewarm_for(_dag([_t("A")]), tmp_path, depth=1,
                           starter=starter, tenant_id="t-alpha")

    # Under shared, any tenant id resolves to the shared bucket.
    slot = await pw.consume("A", tenant_id="t-beta")
    assert slot is not None, "shared mode should allow cross-tenant consume"


@pytest.mark.asyncio
async def test_shared_policy_single_bucket(policy_shared, tmp_path):
    started: dict = {}
    starter = _starter_factory(started, record_tenant=True)

    await pw.prewarm_for(_dag([_t("A")]), tmp_path, depth=1,
                           starter=starter, tenant_id="t-alpha")
    await pw.prewarm_for(_dag([_t("B")]), tmp_path, depth=1,
                           starter=starter, tenant_id="t-beta")

    snap = pw.snapshot_by_tenant()
    assert set(snap.keys()) == {"_shared"}, (
        "shared policy must co-mingle all tenants under one bucket"
    )
    assert set(snap["_shared"].keys()) == {"A", "B"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  disabled — short-circuit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_disabled_policy_skips_prewarm_for(policy_disabled, tmp_path):
    started: dict = {}
    starter = _starter_factory(started, record_tenant=True)
    slots = await pw.prewarm_for(_dag([_t("A"), _t("B")]),
                                   tmp_path, depth=2, starter=starter,
                                   tenant_id="t-alpha")
    assert slots == []
    assert started == {}, "disabled policy must not launch any containers"


@pytest.mark.asyncio
async def test_disabled_policy_returns_none_on_consume(policy_disabled):
    slot = await pw.consume("A", tenant_id="t-alpha")
    assert slot is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /tmp cleanup on consume — the residue defence
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_consume_force_clears_tmp_on_hit(policy_per_tenant, tmp_path,
                                                 monkeypatch):
    """Every hit must wipe /tmp for the tenant so no speculative
    scratch residue leaks into the real task."""
    from backend import tenant_quota
    calls: list[str] = []
    monkeypatch.setattr(
        tenant_quota, "cleanup_tenant_tmp",
        lambda tid: calls.append(tid) or 0,
    )

    started: dict = {}
    starter = _starter_factory(started, record_tenant=True)
    await pw.prewarm_for(_dag([_t("A")]), tmp_path, depth=1,
                           starter=starter, tenant_id="t-alpha")
    await pw.consume("A", tenant_id="t-alpha")
    assert calls == ["t-alpha"]


@pytest.mark.asyncio
async def test_consume_clears_tmp_even_on_miss(policy_per_tenant, monkeypatch):
    """Miss path should still scrub /tmp — the container the dispatcher
    falls back to launching freshly will land in the same namespace."""
    from backend import tenant_quota
    calls: list[str] = []
    monkeypatch.setattr(
        tenant_quota, "cleanup_tenant_tmp",
        lambda tid: calls.append(tid) or 0,
    )
    slot = await pw.consume("no-such-task", tenant_id="t-alpha")
    assert slot is None
    assert calls == ["t-alpha"]


@pytest.mark.asyncio
async def test_consume_tmp_cleanup_also_fires_under_shared(policy_shared,
                                                             tmp_path,
                                                             monkeypatch):
    """M5 task spec: 'Launch 前強制 /tmp 清空（即使 shared 模式亦然）' —
    cleanup runs regardless of policy."""
    from backend import tenant_quota
    calls: list[str] = []
    monkeypatch.setattr(
        tenant_quota, "cleanup_tenant_tmp",
        lambda tid: calls.append(tid) or 0,
    )
    started: dict = {}
    starter = _starter_factory(started, record_tenant=True)
    await pw.prewarm_for(_dag([_t("A")]), tmp_path, depth=1,
                           starter=starter, tenant_id="t-alpha")
    await pw.consume("A", tenant_id="t-beta")  # cross-tenant OK in shared
    assert len(calls) == 1
    # Slot is tagged with the launcher's tenant (alpha), so cleanup
    # targets alpha — ensures no alpha-residue leaks to beta.
    assert calls[0] == "t-alpha"


@pytest.mark.asyncio
async def test_consume_cleanup_failure_does_not_void_hit(policy_per_tenant,
                                                          tmp_path, monkeypatch):
    """If /tmp cleanup fails, we must STILL return the slot — never let
    a cleanup blip void a valid pre-warm."""
    from backend import tenant_quota

    def boom(tid):
        raise OSError("EROFS simulated")
    monkeypatch.setattr(tenant_quota, "cleanup_tenant_tmp", boom)

    started: dict = {}
    starter = _starter_factory(started, record_tenant=True)
    await pw.prewarm_for(_dag([_t("A")]), tmp_path, depth=1,
                           starter=starter, tenant_id="t-alpha")
    slot = await pw.consume("A", tenant_id="t-alpha")
    assert slot is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Starter signature introspection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_starter_without_tenant_param_still_works(policy_per_tenant,
                                                          tmp_path):
    """Back-compat: legacy tests inject 2-arg starters; the shim must
    detect the missing kw and fall back to positional call."""
    started: dict = {}
    starter = _starter_factory(started, record_tenant=False)  # 2-arg only
    slots = await pw.prewarm_for(_dag([_t("A")]), tmp_path, depth=1,
                                   starter=starter, tenant_id="t-alpha")
    assert len(slots) == 1


@pytest.mark.asyncio
async def test_starter_with_tenant_param_receives_tenant(policy_per_tenant,
                                                           tmp_path):
    received: list[str] = []

    async def starter(agent_id, workspace_path, *, tenant_id):
        received.append(tenant_id)
        return _FakeInfo(agent_id=agent_id, tenant_id=tenant_id)

    await pw.prewarm_for(_dag([_t("A")]), tmp_path, depth=1,
                           starter=starter, tenant_id="t-gamma")
    assert received == ["t-gamma"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Slot metadata
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_slot_carries_tenant_id(policy_per_tenant, tmp_path):
    started: dict = {}
    starter = _starter_factory(started, record_tenant=True)
    slots = await pw.prewarm_for(_dag([_t("A")]), tmp_path, depth=1,
                                   starter=starter, tenant_id="t-omega")
    assert slots[0].tenant_id == "t-omega"


@pytest.mark.asyncio
async def test_snapshot_flat_view_spans_tenants(policy_per_tenant, tmp_path):
    started: dict = {}
    starter = _starter_factory(started, record_tenant=True)
    await pw.prewarm_for(_dag([_t("A")]), tmp_path, depth=1,
                           starter=starter, tenant_id="t-alpha")
    await pw.prewarm_for(_dag([_t("B")]), tmp_path, depth=1,
                           starter=starter, tenant_id="t-beta")
    snap = pw.snapshot()  # flat
    assert set(snap.keys()) == {"A", "B"}
