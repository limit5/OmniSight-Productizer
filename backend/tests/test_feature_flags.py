"""OP-884 D12 -- feature flag SDK + admin contract tests.

Covers the five OP-884 test plan cases:

1. flag-defined enabled -> ``is_enabled`` returns ``True`` for tenants
   that pass the rollout gate.
2. flag-undefined -> ``is_enabled`` fails closed (returns ``False`` and
   logs ``FlagNotDefined``).
3. % rollout deterministic per tenant -> hash-bucket assignment is
   stable across calls and across raising the rollout percentage.
4. admin UI flip persists -> PATCH adjusts ``rollout_pct`` /
   ``allowed_tenants`` and a subsequent GET returns the new values.
5. audit row -> a PATCH writes one ``audit_log`` entry with
   ``entity_kind='feature_flag'`` and before / after payloads that
   reflect the change.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.agents import feature_flags as ff_sdk


# ─── helpers / fakes ────────────────────────────────────────────────


class _FakeAsyncpgRecord(dict):
    """Stand-in for asyncpg.Record that is dict-shaped."""


class _FakeAsyncpgConn:
    """asyncpg-flavoured fake: ``fetchrow`` with $1 placeholders."""

    def __init__(self, rows: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = rows or {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> _FakeAsyncpgRecord | None:
        self.calls.append((sql, args))
        if "FOR UPDATE" in sql or "WHERE flag_name = $1" in sql and "UPDATE" not in sql:
            flag_name = args[0]
            row = self.rows.get(flag_name)
            return _FakeAsyncpgRecord(row) if row else None
        if "UPDATE feature_flags" in sql:
            flag_name, state, rollout_pct, allowed_json = args
            row = {
                **self.rows[flag_name],
                "state": state,
                "rollout_pct": rollout_pct,
                "allowed_tenants": allowed_json,
                "updated_at": "2026-05-11 00:00:00",
            }
            self.rows[flag_name] = row
            return _FakeAsyncpgRecord(row)
        raise AssertionError(f"unexpected SQL: {sql}")

    async def fetch(self, _sql: str) -> list[_FakeAsyncpgRecord]:
        return [_FakeAsyncpgRecord(r) for r in self.rows.values()]

    def transaction(self) -> "_FakeTxn":
        return _FakeTxn()


class _FakeTxn:
    async def __aenter__(self) -> "_FakeTxn":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


# ─── AC #1: flag-defined enabled resolves True ─────────────────────


@pytest.mark.asyncio
async def test_flag_defined_enabled_resolves_true_for_full_rollout() -> None:
    """AC #1: a row in ``feature_flags`` with state=enabled and
    rollout_pct=100 resolves ``True`` for every tenant."""
    conn = _FakeAsyncpgConn(
        rows={
            "ks.cmek.enabled": {
                "flag_name": "ks.cmek.enabled",
                "state": "enabled",
                "rollout_pct": 100,
                "allowed_tenants": "[]",
            }
        }
    )

    assert await ff_sdk.is_enabled(
        "ks.cmek.enabled", "t-alpha", conn=conn
    ) is True
    assert await ff_sdk.is_enabled(
        "ks.cmek.enabled", "t-beta", conn=conn
    ) is True


# ─── AC #2 + error catalog: FlagNotDefined fails closed ────────────


@pytest.mark.asyncio
async def test_flag_undefined_fails_closed_with_warning(caplog) -> None:
    """AC #2 + error catalog ``FlagNotDefined``: a missing row resolves
    ``False`` and logs a warning so the operator can spot the stale
    call site."""
    conn = _FakeAsyncpgConn(rows={})

    with caplog.at_level("WARNING"):
        result = await ff_sdk.is_enabled(
            "does.not.exist", "t-alpha", conn=conn
        )

    assert result is False
    assert any(
        "FlagNotDefined" in rec.message for rec in caplog.records
    ), "FlagNotDefined warning must be emitted"


@pytest.mark.asyncio
async def test_disabled_state_short_circuits_to_false() -> None:
    """A disabled row resolves False regardless of rollout_pct."""
    conn = _FakeAsyncpgConn(
        rows={
            "wp.early_access.feature": {
                "flag_name": "wp.early_access.feature",
                "state": "disabled",
                "rollout_pct": 100,
                "allowed_tenants": "[]",
            }
        }
    )
    assert await ff_sdk.is_enabled(
        "wp.early_access.feature", "t-alpha", conn=conn
    ) is False


# ─── AC #3 / AC #4: deterministic % rollout per tenant ─────────────


def test_bucket_for_tenant_is_deterministic_and_independent() -> None:
    """The bucket is stable across calls and independent across flags
    -- so dialing one flag to 10% doesn't tie a different flag to the
    same 10% of tenants."""
    a = ff_sdk.bucket_for_tenant("flag.one", "t-alpha")
    b = ff_sdk.bucket_for_tenant("flag.one", "t-alpha")
    assert a == b, "same (flag, tenant) -> same bucket"
    assert 0 <= a < 100

    # Different flags should redistribute the tenant -- assert that at
    # least one mismatch exists across a small set of flags so the
    # cohort isn't degenerate.
    distinct = {
        ff_sdk.bucket_for_tenant(f"flag.{i}", "t-alpha") for i in range(10)
    }
    assert len(distinct) > 1, (
        "bucket function must spread the same tenant across flag names"
    )


@pytest.mark.asyncio
async def test_rollout_pct_decides_per_tenant_and_is_monotonic() -> None:
    """AC #4 (hash(tenant_id) % 100 < rollout_pct): a tenant whose
    bucket is < pct resolves True; a tenant whose bucket is >= pct
    resolves False. Increasing pct can only add tenants -- it never
    flips a previously-True tenant to False."""
    # Find a tenant whose bucket places it strictly below pct=50.
    chosen_in: str | None = None
    chosen_out: str | None = None
    for i in range(200):
        candidate = f"t-{i:03d}"
        bucket = ff_sdk.bucket_for_tenant("rollout.test", candidate)
        if chosen_in is None and bucket < 25:
            chosen_in = candidate
        if chosen_out is None and bucket >= 75:
            chosen_out = candidate
        if chosen_in and chosen_out:
            break
    assert chosen_in and chosen_out, "fixture must find both cohorts"

    def _row(pct: int) -> dict[str, Any]:
        return {
            "flag_name": "rollout.test",
            "state": "enabled",
            "rollout_pct": pct,
            "allowed_tenants": "[]",
        }

    # At 50%: the low-bucket tenant is in, the high-bucket tenant is out.
    conn = _FakeAsyncpgConn(rows={"rollout.test": _row(50)})
    assert await ff_sdk.is_enabled(
        "rollout.test", chosen_in, conn=conn
    ) is True
    assert await ff_sdk.is_enabled(
        "rollout.test", chosen_out, conn=conn
    ) is False

    # Monotonicity: at 100% both are in.
    conn.rows["rollout.test"] = _row(100)
    assert await ff_sdk.is_enabled(
        "rollout.test", chosen_in, conn=conn
    ) is True
    assert await ff_sdk.is_enabled(
        "rollout.test", chosen_out, conn=conn
    ) is True

    # At 0%, both are out (fail-closed lower bound).
    conn.rows["rollout.test"] = _row(0)
    assert await ff_sdk.is_enabled(
        "rollout.test", chosen_in, conn=conn
    ) is False
    assert await ff_sdk.is_enabled(
        "rollout.test", chosen_out, conn=conn
    ) is False


@pytest.mark.asyncio
async def test_allowed_tenants_overrides_rollout_pct() -> None:
    """An explicit allow-list is authoritative -- the percent gate is
    bypassed."""
    conn = _FakeAsyncpgConn(
        rows={
            "explicit.cohort": {
                "flag_name": "explicit.cohort",
                "state": "enabled",
                "rollout_pct": 0,  # would normally exclude every tenant
                "allowed_tenants": json.dumps(["t-vip"]),
            }
        }
    )

    assert await ff_sdk.is_enabled(
        "explicit.cohort", "t-vip", conn=conn
    ) is True
    assert await ff_sdk.is_enabled(
        "explicit.cohort", "t-other", conn=conn
    ) is False


# ─── AC #4 admin UI flip persists ───────────────────────────────────


def _admin_user():
    from backend import auth

    return auth.User(
        id="u-admin",
        email="admin@example.com",
        name="admin",
        role="admin",
        tenant_id="t-default",
    )


class _FakePool:
    def __init__(self, conn: _FakeAsyncpgConn) -> None:
        self._conn = conn

    def acquire(self) -> "_FakeAcquire":
        return _FakeAcquire(self._conn)


class _FakeAcquire:
    def __init__(self, conn: _FakeAsyncpgConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeAsyncpgConn:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


@pytest.mark.asyncio
async def test_admin_patch_persists_rollout_pct_and_allowed_tenants(
    monkeypatch,
) -> None:
    """AC #4 (admin UI flip persists): a PATCH that adjusts
    ``rollout_pct`` + ``allowed_tenants`` is reflected in the row that
    the SDK reads afterwards."""
    from backend.routers import feature_flags as router

    conn = _FakeAsyncpgConn(
        rows={
            "wp.rollout": {
                "flag_name": "wp.rollout",
                "tier": "ga",
                "state": "enabled",
                "expires_at": None,
                "owner": "wp",
                "rollout_pct": 10,
                "allowed_tenants": "[]",
                "created_at": "2026-05-11 00:00:00",
                "updated_at": "2026-05-11 00:00:00",
            }
        }
    )

    monkeypatch.setattr(router, "get_pool", lambda: _FakePool(conn))
    monkeypatch.setattr(
        router._flags,
        "publish_feature_flags_invalidate",
        lambda **_: True,
    )
    from backend import audit
    monkeypatch.setattr(audit, "log", _capture_audit([]))

    res = await router.patch_feature_flag(
        "wp.rollout",
        router.PatchFeatureFlagRequest(
            rollout_pct=75,
            allowed_tenants=["t-vip", "t-beta"],
        ),
        None,
        actor=_admin_user(),
    )
    body = json.loads(res.body)

    assert body["feature_flag"]["rollout_pct"] == 75
    assert body["feature_flag"]["allowed_tenants"] == ["t-vip", "t-beta"]

    # The next SDK read returns the new values from the same connection.
    enabled = await ff_sdk.is_enabled("wp.rollout", "t-vip", conn=conn)
    assert enabled is True
    blocked = await ff_sdk.is_enabled("wp.rollout", "t-not-listed", conn=conn)
    assert blocked is False, "allow-list excludes non-listed tenants"


# ─── AC #5: audit row written on update ─────────────────────────────


def _capture_audit(sink: list[dict[str, Any]]):
    async def _log(**kwargs: Any) -> int:
        sink.append(kwargs)
        return 7
    return _log


@pytest.mark.asyncio
async def test_admin_patch_writes_audit_row_with_before_and_after(
    monkeypatch,
) -> None:
    """AC #5: a PATCH writes one ``audit_log`` row with
    ``entity_kind='feature_flag'`` and before/after payloads that
    carry the OP-884 columns."""
    from backend.routers import feature_flags as router

    conn = _FakeAsyncpgConn(
        rows={
            "wp.rollout": {
                "flag_name": "wp.rollout",
                "tier": "ga",
                "state": "disabled",
                "expires_at": None,
                "owner": "wp",
                "rollout_pct": 100,
                "allowed_tenants": "[]",
                "created_at": "2026-05-11 00:00:00",
                "updated_at": "2026-05-11 00:00:00",
            }
        }
    )

    audit_rows: list[dict[str, Any]] = []
    monkeypatch.setattr(router, "get_pool", lambda: _FakePool(conn))
    monkeypatch.setattr(
        router._flags,
        "publish_feature_flags_invalidate",
        lambda **_: True,
    )
    from backend import audit
    monkeypatch.setattr(audit, "log", _capture_audit(audit_rows))

    await router.patch_feature_flag(
        "wp.rollout",
        router.PatchFeatureFlagRequest(
            state="enabled",
            rollout_pct=25,
        ),
        None,
        actor=_admin_user(),
    )

    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row["action"] == "feature_flag.toggled"
    assert row["entity_kind"] == "feature_flag"
    assert row["entity_id"] == "wp.rollout"
    assert row["actor"] == "admin@example.com"
    assert row["before"]["state"] == "disabled"
    assert row["before"]["rollout_pct"] == 100
    assert row["after"]["state"] == "enabled"
    assert row["after"]["rollout_pct"] == 25


# ─── decision-function unit coverage (rounds out edge cases) ────────


def test_decide_helper_matches_ac4_truth_table() -> None:
    # disabled wins over everything
    assert ff_sdk._decide(
        flag_name="x",
        tenant_id="t",
        state="disabled",
        rollout_pct=100,
        allowed_tenants=("t",),
    ) is False

    # allow-list authoritative when populated
    assert ff_sdk._decide(
        flag_name="x", tenant_id="t-yes",
        state="enabled", rollout_pct=0, allowed_tenants=("t-yes",),
    ) is True
    assert ff_sdk._decide(
        flag_name="x", tenant_id="t-no",
        state="enabled", rollout_pct=100, allowed_tenants=("t-yes",),
    ) is False

    # missing rollout_pct (NULL) defaults to 100 / full cohort
    assert ff_sdk._decide(
        flag_name="x", tenant_id="t",
        state="enabled", rollout_pct=None, allowed_tenants=(),
    ) is True
