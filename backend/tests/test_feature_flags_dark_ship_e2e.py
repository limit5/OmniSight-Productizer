"""RT-15c (OP-1595) -- dark-ship verified end-to-end (backend half).

RT-15a shipped ``GET /api/feature-flags/effective`` + the server-eval
SDK; RT-15b shipped the frontend provider / hook / SSR bootstrap that
consumes it. This suite is the RT-15c integration that proves the
*dark-ship lifecycle* across the real backend path -- endpoint ->
:func:`backend.feature_flag_sdk.effective_flags` -> tenant-aware
:func:`backend.agents.feature_flags.is_enabled` -> ``_decide`` -- emits
the exact wire payload the frontend consumes.

The bridge across the language boundary is a single golden contract
fixture, ``test/fixtures/dark-ship-effective-flags.json``. This file
asserts the REAL endpoint reproduces each phase's
``endpoint_payload`` byte-for-byte; the sibling vitest suite
(``test/lib/feature-flags-dark-ship-e2e.test.tsx``) asserts the REAL
frontend turns those same payloads into the documented UI visibility.
If either side drifts from the contract, its test goes red.

Dark-ship lifecycle covered (flag ``ui.release_train.enabled``):

  dark    -- no registry row  -> all flags False (feature ships dark)
  canary  -- allowed_tenants cohort -> True for cohort tenant only
  ga      -- rollout_pct=100        -> True for every tenant
  outage  -- DB read raises         -> 200 + all False (re-darkens)

No new API/provider code is added (RT-15c is tests-only); every
assertion drives the production handlers exactly as a browser would.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend import feature_flag_sdk as ff_sdk
from backend.routers import feature_flags as router


# ─── golden contract fixture (shared with the vitest E2E suite) ─────

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "test" / "fixtures" / "dark-ship-effective-flags.json"
)
_CONTRACT = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
_PHASES = _CONTRACT["phases"]
_FLAG = _CONTRACT["flag_under_test"]  # "ui.release_train.enabled"


# ─── asyncpg-flavoured fakes (mirror test_feature_flags_effective.py) ─


class _FakeRecord(dict):
    """dict-shaped stand-in for ``asyncpg.Record``."""


class _FakeConn:
    """Fake exposing ``fetchrow`` keyed by flag_name ($1 placeholder)."""

    def __init__(self, rows: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows = rows or {}

    async def fetchrow(self, _sql: str, *args: Any) -> _FakeRecord | None:
        row = self.rows.get(args[0])
        return _FakeRecord(row) if row else None


class _BrokenConn:
    """Fake whose read raises -- exercises the fail-closed branch."""

    async def fetchrow(self, _sql: str, *_args: Any) -> _FakeRecord | None:
        raise RuntimeError("simulated flag-service outage")


class _FakeAcquire:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _enabled_row(name: str, *, rollout_pct: int = 100, allowed=None) -> dict:
    """A ``feature_flags`` registry row -- deliberately carrying the
    owner / rollout_pct / allowed_tenants / tier the effective endpoint
    must NEVER surface to the browser."""
    return {
        "flag_name": name,
        "state": "enabled",
        "rollout_pct": rollout_pct,
        "allowed_tenants": json.dumps(list(allowed)) if allowed else "[]",
        "owner": "release-train-team",
        "tier": "staged",
    }


def _viewer(tenant_id: str):
    from backend import auth

    return auth.User(
        id="u-viewer",
        email="viewer@example.com",
        name="viewer",
        role="viewer",
        tenant_id=tenant_id,
    )


async def _call_endpoint(monkeypatch, *, tenant: str, conn: Any) -> dict:
    """Drive the REAL effective-flags handler for ``tenant`` against a
    fake pool wrapping ``conn`` and return the decoded JSON body."""
    monkeypatch.setattr(
        "backend.db_pool.get_pool", lambda: _FakePool(conn)
    )
    res = await router.get_effective_feature_flags(None, actor=_viewer(tenant))
    assert res.status_code == 200, "effective endpoint must never 5xx"
    return json.loads(res.body)


# ─── contract parity: the backend allow-list IS the shared contract ──


def test_backend_allowlist_matches_golden_contract() -> None:
    """The server-side public allow-list, the fixture's ``public_flags``,
    and the per-phase payload keys must all be the same set -- this is
    what lets the vitest suite trust the fixture as the wire contract."""
    contract_flags = set(_CONTRACT["public_flags"])
    assert set(ff_sdk.PUBLIC_FLAG_ALLOWLIST) == contract_flags
    assert _FLAG in contract_flags
    for phase in ("dark", "ga", "outage"):
        payload = _PHASES[phase]["endpoint_payload"]["flags"]
        assert set(payload) == contract_flags


# ─── dark phase: no row -> the feature ships dark ───────────────────


@pytest.mark.asyncio
async def test_dark_phase_no_row_ships_dark(monkeypatch) -> None:
    """With no registry row for any public flag, the endpoint emits the
    golden ``dark`` payload: every flag False. The incomplete release-
    train UI is invisible to every tenant."""
    phase = _PHASES["dark"]
    body = await _call_endpoint(
        monkeypatch, tenant=phase["tenant"], conn=_FakeConn(rows={})
    )
    assert body == phase["endpoint_payload"]
    assert body["flags"][_FLAG] is False


# ─── canary phase: allowed_tenants cohort lights up one tenant ──────


@pytest.mark.asyncio
async def test_canary_phase_cohort_only(monkeypatch) -> None:
    """An ``allowed_tenants`` cohort flips the dark-shipped flag True for
    the cohort tenant and leaves every other tenant dark -- proving the
    boolean is computed for the *authed* tenant, not echoed from the
    row."""
    phase = _PHASES["canary"]
    rows = {_FLAG: _enabled_row(_FLAG, rollout_pct=0,
                                allowed=[phase["canary_tenant"]])}

    canary_body = await _call_endpoint(
        monkeypatch, tenant=phase["canary_tenant"],
        conn=_FakeConn(rows=dict(rows)),
    )
    other_body = await _call_endpoint(
        monkeypatch, tenant=phase["other_tenant"],
        conn=_FakeConn(rows=dict(rows)),
    )

    assert canary_body == phase["endpoint_payload_for_canary_tenant"]
    assert other_body == phase["endpoint_payload_for_other_tenant"]


# ─── ga phase: rollout_pct=100 -> visible everywhere ────────────────


@pytest.mark.asyncio
async def test_ga_phase_visible_to_all(monkeypatch) -> None:
    """At rollout_pct=100 the flag resolves True for an arbitrary tenant;
    the previously dark-shipped UI is now generally available."""
    phase = _PHASES["ga"]
    rows = {_FLAG: _enabled_row(_FLAG, rollout_pct=100)}
    body = await _call_endpoint(
        monkeypatch, tenant=phase["tenant"], conn=_FakeConn(rows=rows)
    )
    assert body == phase["endpoint_payload"]
    assert body["flags"][_FLAG] is True


# ─── outage phase: DB error re-darkens via 200 + all False ──────────


@pytest.mark.asyncio
async def test_outage_phase_re_darkens(monkeypatch) -> None:
    """A flag-service outage during GA must not 5xx the page: the
    endpoint returns 200 with every flag False, re-darkening the
    feature (dark-ship safety net)."""
    phase = _PHASES["outage"]
    body = await _call_endpoint(
        monkeypatch, tenant=phase["tenant"], conn=_BrokenConn()
    )
    assert body == phase["endpoint_payload"]
    assert all(v is False for v in body["flags"].values())


# ─── leak guard: enabled row metadata never reaches the wire ────────


@pytest.mark.asyncio
async def test_ga_payload_leaks_no_internal_metadata(monkeypatch) -> None:
    """The GA row carries owner / rollout_pct / allowed_tenants / tier;
    none of it may appear in the serialized effective payload."""
    rows = {_FLAG: _enabled_row(_FLAG, rollout_pct=100, allowed=["t-anyone"])}
    monkeypatch.setattr(
        "backend.db_pool.get_pool", lambda: _FakePool(_FakeConn(rows))
    )
    res = await router.get_effective_feature_flags(
        None, actor=_viewer("t-anyone")
    )
    raw = res.body.decode("utf-8")
    for leaked in ("owner", "rollout_pct", "allowed_tenants", "tier",
                   "release-train-team", "staged"):
        assert leaked not in raw, f"effective payload leaked {leaked!r}"
