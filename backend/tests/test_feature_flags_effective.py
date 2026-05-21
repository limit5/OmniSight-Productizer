"""RT-15a (OP-1593) -- frontend-safe effective feature-flags endpoint.

Covers the RT-15a Acceptance Criteria:

1. ``GET /api/feature-flags/effective`` returns ONLY the public-allow-
   listed flags, server-evaluated to booleans, for the authed tenant.
2. A DB error resolves every flag to ``False`` (fail-closed) -- never a
   5xx and never an "enabled" leak.
3. The payload leaks no internal flag metadata: no owner / rollout_pct /
   allowed_tenants / tier / tenant_id, and no non-allow-listed flag.
4. Per-tenant evaluation: an ``allowed_tenants`` cohort flips True only
   for listed tenants (proving the answer is computed for the *authed*
   tenant, not echoed from the DB row).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend import feature_flag_sdk as ff_sdk


# ─── fakes (mirror test_feature_flags.py asyncpg shapes) ────────────


class _FakeRecord(dict):
    """dict-shaped stand-in for asyncpg.Record."""


class _FakeConn:
    """asyncpg-flavoured fake exposing ``fetchrow`` with $1 placeholders."""

    def __init__(self, rows: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows = rows or {}

    async def fetchrow(self, _sql: str, *args: Any) -> _FakeRecord | None:
        flag_name = args[0]
        row = self.rows.get(flag_name)
        return _FakeRecord(row) if row else None


class _BrokenConn:
    """Fake whose read raises -- exercises the fail-closed branch."""

    async def fetchrow(self, _sql: str, *_args: Any) -> _FakeRecord | None:
        raise RuntimeError("simulated DB outage")


class _FakePool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> "_FakeAcquire":
        return _FakeAcquire(self._conn)


class _FakeAcquire:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def _enabled_row(name: str, *, rollout_pct: int = 100, allowed=None) -> dict:
    return {
        "flag_name": name,
        "state": "enabled",
        "rollout_pct": rollout_pct,
        "allowed_tenants": json.dumps(list(allowed)) if allowed else "[]",
    }


def _viewer(tenant_id: str = "t-alpha"):
    from backend import auth

    return auth.User(
        id="u-viewer",
        email="viewer@example.com",
        name="viewer",
        role="viewer",
        tenant_id=tenant_id,
    )


# ─── AC #1 + #3: only allow-listed booleans, no metadata leak ───────


@pytest.mark.asyncio
async def test_effective_flags_returns_only_allowlisted_booleans() -> None:
    """effective_flags() returns one bool per allow-listed name and
    drops any non-allow-listed (internal) flag, even when its row is
    present and enabled."""
    conn = _FakeConn(
        rows={
            "ui.a": _enabled_row("ui.a"),
            "ui.b": {  # disabled -> False
                "flag_name": "ui.b",
                "state": "disabled",
                "rollout_pct": 100,
                "allowed_tenants": "[]",
            },
            # An internal flag that is enabled must NOT appear in output.
            "ks.cmek.enabled": _enabled_row("ks.cmek.enabled"),
        }
    )

    result = await ff_sdk.effective_flags(
        "t-alpha", allowlist={"ui.a", "ui.b"}, conn=conn
    )

    assert set(result.keys()) == {"ui.a", "ui.b"}
    assert result["ui.a"] is True
    assert result["ui.b"] is False
    assert "ks.cmek.enabled" not in result
    # Every value is a plain bool -- never a row/dict that could carry
    # owner / rollout_pct / allowed_tenants.
    assert all(isinstance(v, bool) for v in result.values())


# ─── AC #2: DB error -> False (fail-closed) ─────────────────────────


@pytest.mark.asyncio
async def test_effective_flags_db_error_fails_closed() -> None:
    """A DB read failure resolves every allow-listed flag to False
    instead of raising."""
    result = await ff_sdk.effective_flags(
        "t-alpha", allowlist={"ui.a", "ui.b"}, conn=_BrokenConn()
    )

    assert result == {"ui.a": False, "ui.b": False}


@pytest.mark.asyncio
async def test_effective_flags_missing_row_fails_closed() -> None:
    """An allow-listed flag with no registry row resolves False
    (FlagNotDefined -> fail-closed)."""
    result = await ff_sdk.effective_flags(
        "t-alpha", allowlist={"ui.never.seeded"}, conn=_FakeConn(rows={})
    )

    assert result == {"ui.never.seeded": False}


# ─── AC #4: per-tenant evaluation honours the allow-list cohort ─────


@pytest.mark.asyncio
async def test_effective_flags_evaluated_for_authed_tenant() -> None:
    """An allowed_tenants cohort flips True only for the listed tenant,
    proving the boolean is computed for the *authed* tenant."""
    rows = {"ui.vip": _enabled_row("ui.vip", rollout_pct=0, allowed=["t-vip"])}

    vip = await ff_sdk.effective_flags(
        "t-vip", allowlist={"ui.vip"}, conn=_FakeConn(rows=dict(rows))
    )
    other = await ff_sdk.effective_flags(
        "t-other", allowlist={"ui.vip"}, conn=_FakeConn(rows=dict(rows))
    )

    assert vip == {"ui.vip": True}
    assert other == {"ui.vip": False}


# ─── endpoint: leak guard + fail-closed wiring ──────────────────────


@pytest.mark.asyncio
async def test_endpoint_returns_flat_bool_map_no_leak(monkeypatch) -> None:
    """GET /feature-flags/effective returns ``{"flags": {name: bool}}``
    for the real PUBLIC_FLAG_ALLOWLIST and never serialises owner /
    rollout_pct / allowed_tenants / tier / tenant_id."""
    from backend.routers import feature_flags as router

    # Seed an enabled row (carrying owner/rollout/allowed_tenants that
    # MUST NOT surface) for every real public-allow-listed flag.
    rows = {
        name: {
            "flag_name": name,
            "state": "enabled",
            "rollout_pct": 100,
            "allowed_tenants": json.dumps(["t-alpha"]),
            "owner": "secret-team",
            "tier": "staged",
        }
        for name in ff_sdk.PUBLIC_FLAG_ALLOWLIST
    }
    monkeypatch.setattr(
        "backend.db_pool.get_pool", lambda: _FakePool(_FakeConn(rows))
    )

    res = await router.get_effective_feature_flags(None, actor=_viewer())
    body = json.loads(res.body)

    assert res.status_code == 200
    assert set(body.keys()) == {"flags"}
    assert set(body["flags"].keys()) == set(ff_sdk.PUBLIC_FLAG_ALLOWLIST)
    assert all(isinstance(v, bool) for v in body["flags"].values())

    raw = res.body.decode("utf-8")
    for leaked in ("owner", "rollout_pct", "allowed_tenants", "tier",
                   "tenant_id", "secret-team", "staged"):
        assert leaked not in raw, f"effective payload leaked {leaked!r}"


@pytest.mark.asyncio
async def test_endpoint_fails_closed_on_pool_outage(monkeypatch) -> None:
    """If the pool itself is unavailable the endpoint still returns 200
    with every public flag False -- no 5xx reaches the browser."""
    from backend.routers import feature_flags as router

    def _boom():
        raise RuntimeError("pool not initialised")

    monkeypatch.setattr("backend.db_pool.get_pool", _boom)

    res = await router.get_effective_feature_flags(None, actor=_viewer())
    body = json.loads(res.body)

    assert res.status_code == 200
    assert set(body["flags"].keys()) == set(ff_sdk.PUBLIC_FLAG_ALLOWLIST)
    assert all(v is False for v in body["flags"].values())


# ─── contract guard: no internal flag may be public ────────────────


def test_public_allowlist_excludes_internal_env_knobs() -> None:
    """The public allow-list must never contain a backend-internal env
    knob (ks.* / wp.*) -- those leak infra posture to the browser."""
    from backend import feature_flags as flags

    knob_flags = {k.flag_name for k in flags.FEATURE_FLAG_ENV_KNOBS.values()}
    assert ff_sdk.PUBLIC_FLAG_ALLOWLIST.isdisjoint(knob_flags)
    assert all(isinstance(n, str) and n for n in ff_sdk.PUBLIC_FLAG_ALLOWLIST)
    # Defence in depth: no entry should look like an internal namespace.
    assert not any(
        n.startswith(("ks.", "wp.", "bp.", "hd.", "mp."))
        for n in ff_sdk.PUBLIC_FLAG_ALLOWLIST
    )
