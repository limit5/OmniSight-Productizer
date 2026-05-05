"""WP.7.8 -- operator feature flag UI API contract."""

from __future__ import annotations

import json
import inspect

import pytest

from backend import auth


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


class _FakeConn:
    def __init__(self):
        self.rows = {
            "wp.diff_validation.enabled": {
                "flag_name": "wp.diff_validation.enabled",
                "tier": "release",
                "state": "disabled",
                "expires_at": None,
                "owner": "wp",
                "created_at": "2026-05-05 00:00:00",
            },
        }

    def transaction(self):
        return _FakeTransaction()

    async def fetch(self, _sql):
        return list(self.rows.values())

    async def fetchrow(self, sql, flag_name, state=None):
        if "FOR UPDATE" in sql:
            return self.rows.get(flag_name)
        if "UPDATE feature_flags" in sql:
            self.rows[flag_name] = {**self.rows[flag_name], "state": state}
            return self.rows[flag_name]
        raise AssertionError(f"unexpected SQL: {sql}")


def _user(role: str) -> auth.User:
    return auth.User(
        id=f"u-{role}",
        email=f"{role}@example.com",
        name=role,
        role=role,
        tenant_id="t-default",
    )


def test_router_roles_are_read_all_write_admin() -> None:
    from backend.routers import feature_flags

    list_src = inspect.getsource(feature_flags.list_feature_flags)
    patch_src = inspect.getsource(feature_flags.patch_feature_flag)

    assert "Depends(auth.require_viewer)" in list_src
    assert "Depends(auth.require_admin)" in patch_src


@pytest.mark.asyncio
async def test_viewer_can_inspect_but_response_is_read_only(monkeypatch):
    from backend.routers import feature_flags

    conn = _FakeConn()
    monkeypatch.setattr(feature_flags, "get_pool", lambda: _FakePool(conn))

    res = await feature_flags.list_feature_flags(None, actor=_user("viewer"))
    body = json.loads(res.body)

    assert body["can_toggle"] is False
    assert body["feature_flags"][0]["flag_name"] == "wp.diff_validation.enabled"


@pytest.mark.asyncio
async def test_admin_toggle_updates_row_audits_and_invalidates(monkeypatch):
    from backend.routers import feature_flags

    conn = _FakeConn()
    audit_rows = []
    invalidations = []

    async def _audit_log(**kwargs):
        audit_rows.append(kwargs)
        return 123

    monkeypatch.setattr(feature_flags, "get_pool", lambda: _FakePool(conn))
    monkeypatch.setattr(
        feature_flags._flags,
        "publish_feature_flags_invalidate",
        lambda **kwargs: invalidations.append(kwargs) or True,
    )
    from backend import audit
    monkeypatch.setattr(audit, "log", _audit_log)

    res = await feature_flags.patch_feature_flag(
        "wp.diff_validation.enabled",
        feature_flags.PatchFeatureFlagRequest(state="enabled"),
        None,
        actor=_user("admin"),
    )
    body = json.loads(res.body)

    assert body["feature_flag"]["state"] == "enabled"
    assert audit_rows[0]["action"] == "feature_flag.toggled"
    assert audit_rows[0]["entity_kind"] == "feature_flag"
    assert audit_rows[0]["entity_id"] == "wp.diff_validation.enabled"
    assert audit_rows[0]["before"]["state"] == "disabled"
    assert audit_rows[0]["after"]["state"] == "enabled"
    assert invalidations == [
        {
            "flag_name": "wp.diff_validation.enabled",
            "origin_worker": "operator-ui",
        }
    ]
