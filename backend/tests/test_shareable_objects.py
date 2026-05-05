"""WP.9.2 -- shareable_objects permalink slug contracts."""

from __future__ import annotations

import pathlib
import re
import secrets
from datetime import UTC, datetime

import pytest

from backend import shareable_objects as so
from backend.db_context import current_tenant_id, set_tenant_id


def test_share_slug_token_urlsafe_floor_and_shape() -> None:
    slug = so.mint_share_slug()
    assert slug.startswith("sh-")
    assert len(slug) == len("sh-") + 22
    assert so.is_valid_share_slug(slug)
    for ch in slug.removeprefix("sh-"):
        assert ch.isalnum() or ch in "-_", repr(slug)


def test_share_slug_uniqueness_smoke() -> None:
    seen = {so.mint_share_slug() for _ in range(100)}
    assert len(seen) == 100


@pytest.mark.parametrize("bad_slug", [
    "",
    "SH-abcdefghijklmnopqrstuv",
    "sh-abcdefghijklmnopqrstu",
    "sh-abcdefghijklmnopqrstuvw",
    "sh-abcdefghijklmnopqrstu=",
    "sh-abcdefghijklmnopqrstu/",
    " sh-abcdefghijklmnopqrstuv",
])
def test_share_slug_validator_rejects_bad_values(bad_slug: str) -> None:
    assert not so.is_valid_share_slug(bad_slug)


def test_slug_minter_uses_token_urlsafe_with_pinned_byte_count() -> None:
    src = pathlib.Path("backend/shareable_objects.py").read_text(encoding="utf-8")
    assert "SHARE_SLUG_BYTES = 16" in src
    assert "secrets.token_urlsafe(SHARE_SLUG_BYTES)" in src
    plaintext = secrets.token_urlsafe(so.SHARE_SLUG_BYTES)
    assert len(plaintext) == 22


def test_insert_sql_uses_atomic_collision_check() -> None:
    sql = so._INSERT_SHAREABLE_OBJECT_SQL
    assert "INSERT INTO shareable_objects" in sql
    assert "visibility, redaction_applied" in sql
    assert "ON CONFLICT (share_id) DO NOTHING" in sql
    assert "RETURNING share_id" in sql
    assert "$7::jsonb" in sql


def test_expiry_cleanup_sql_uses_row_locking_and_returning_delete() -> None:
    assert "FOR UPDATE SKIP LOCKED" in so._SELECT_EXPIRED_SHAREABLE_OBJECTS_SQL
    assert "expires_at <= $1" in so._SELECT_EXPIRED_SHAREABLE_OBJECTS_SQL
    assert "DELETE FROM shareable_objects" in so._DELETE_EXPIRED_SHAREABLE_OBJECT_SQL
    assert "RETURNING share_id" in so._DELETE_EXPIRED_SHAREABLE_OBJECT_SQL


def test_enforce_share_redaction_mask_masks_nested_paths_without_mutating_source() -> None:
    source = {
        "payload": {
            "stdout": [
                {"line": "public"},
                {"line": "secret token"},
            ],
            "stderr": "secret error",
        },
        "metadata": {"customer": {"email": "alice@example.com"}},
    }

    redacted = so.enforce_share_redaction_mask(
        source,
        {
            "payload.stdout.1.line": "secret",
            "metadata.customer.email": ["pii", "customer_ip"],
        },
    )

    assert redacted["payload"]["stdout"][0]["line"] == "public"
    assert redacted["payload"]["stdout"][1]["line"] == "[REDACTED:secret]"
    assert redacted["payload"]["stderr"] == "secret error"
    assert redacted["metadata"]["customer"]["email"] == (
        "[REDACTED:pii+customer_ip]"
    )
    assert source["payload"]["stdout"][1]["line"] == "secret token"
    assert source["metadata"]["customer"]["email"] == "alice@example.com"


def test_build_share_payload_uses_durable_row_mask_without_override() -> None:
    share = _expired_row()
    share["redaction_applied"] = {"payload.command": "secret"}
    payload = {
        "payload": {
            "command": "curl https://internal.test",
            "stdout": "public",
        },
    }

    redacted = so.build_share_payload(share, payload)

    assert redacted == {
        "payload": {
            "command": "[REDACTED:secret]",
            "stdout": "public",
        },
    }


@pytest.mark.parametrize("bad_mask", [
    {"payload.missing": "secret"},
    {"payload.stdout.3": "secret"},
    {"payload.stdout": "none"},
])
def test_enforce_share_redaction_mask_fails_closed_for_bypass_masks(bad_mask) -> None:
    with pytest.raises(ValueError, match="redaction_mask"):
        so.enforce_share_redaction_mask(
            {"payload": {"stdout": ["line"]}},
            bad_mask,
        )


class FakeConn:
    def __init__(self, rows):
        self._rows = list(rows)
        self.calls = []

    async def fetchrow(self, sql, *params):
        self.calls.append((sql, params))
        return self._rows.pop(0)


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeCleanupConn:
    def __init__(self, rows, delete_rows):
        self._rows = list(rows)
        self._delete_rows = list(delete_rows)
        self.fetch_calls = []
        self.fetchrow_calls = []
        self.transactions = 0

    def transaction(self):
        self.transactions += 1
        return FakeTransaction()

    async def fetch(self, sql, *params):
        self.fetch_calls.append((sql, params))
        return self._rows

    async def fetchrow(self, sql, *params):
        self.fetchrow_calls.append((sql, params))
        return self._delete_rows.pop(0)


def _expired_row(
    share_id: str = "sh-expired_slug________",
    *,
    tenant_id: str = "t-a",
    expires_at: datetime | None = None,
) -> dict:
    return {
        "share_id": share_id,
        "object_kind": "block",
        "object_id": "b-1",
        "tenant_id": tenant_id,
        "owner_user_id": "u-a",
        "visibility": "tenant",
        "expires_at": expires_at or datetime(2026, 5, 5, tzinfo=UTC),
        "redaction_applied": {},
        "created_at": datetime(2026, 5, 1, tzinfo=UTC),
    }


@pytest.mark.asyncio
async def test_cleanup_expired_shareable_objects_audits_before_delete() -> None:
    now = datetime(2026, 5, 6, tzinfo=UTC)
    conn = FakeCleanupConn([_expired_row()], [{"share_id": "sh-expired_slug________"}])
    audit_calls = []

    async def audit_log(**kwargs):
        audit_calls.append((current_tenant_id(), kwargs))
        return 42

    set_tenant_id("t-original")
    try:
        summary = await so.cleanup_expired_shareable_objects(
            conn,
            now=now,
            limit=25,
            audit_log=audit_log,
        )
    finally:
        assert current_tenant_id() == "t-original"
        set_tenant_id(None)

    assert summary.to_dict() == {
        "scanned": 1,
        "audited": 1,
        "deleted": 1,
        "skipped_audit": 0,
    }
    assert conn.transactions == 1
    assert conn.fetch_calls == [
        (so._SELECT_EXPIRED_SHAREABLE_OBJECTS_SQL, (now, 25)),
    ]
    assert audit_calls[0][0] == "t-a"
    assert audit_calls[0][1]["action"] == "shareable_object.expired_deleted"
    assert audit_calls[0][1]["entity_kind"] == "shareable_object"
    assert audit_calls[0][1]["entity_id"] == "sh-expired_slug________"
    assert conn.fetchrow_calls == [
        (so._DELETE_EXPIRED_SHAREABLE_OBJECT_SQL, ("sh-expired_slug________", now)),
    ]


@pytest.mark.asyncio
async def test_cleanup_expired_shareable_objects_skips_delete_without_audit() -> None:
    now = datetime(2026, 5, 6, tzinfo=UTC)
    conn = FakeCleanupConn([_expired_row()], [])

    async def audit_log(**kwargs):
        return None

    summary = await so.cleanup_expired_shareable_objects(
        conn,
        now=now,
        audit_log=audit_log,
    )

    assert summary.to_dict() == {
        "scanned": 1,
        "audited": 0,
        "deleted": 0,
        "skipped_audit": 1,
    }
    assert conn.fetchrow_calls == []


@pytest.mark.asyncio
async def test_cleanup_expired_shareable_objects_rejects_empty_limit() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        await so.cleanup_expired_shareable_objects(limit=0)


@pytest.mark.asyncio
async def test_create_shareable_object_retries_on_slug_collision(monkeypatch) -> None:
    slugs = iter(["collision_slug________", "accepted_slug_________"])
    monkeypatch.setattr(
        so.secrets,
        "token_urlsafe",
        lambda _n: next(slugs),
    )
    row = {
        "share_id": "sh-accepted_slug_________",
        "object_kind": "block",
        "object_id": "b-1",
        "tenant_id": "t-a",
        "owner_user_id": "u-a",
        "visibility": "private",
        "expires_at": None,
        "redaction_applied": {"payload.stdout": "secret"},
        "created_at": "2026-05-06 00:00:00",
    }
    conn = FakeConn([None, row])

    created = await so.create_shareable_object(
        conn,
        object_kind="block",
        object_id="b-1",
        tenant_id="t-a",
        owner_user_id="u-a",
        redaction_applied={"payload.stdout": "secret"},
    )

    assert created.to_dict() == row
    assert len(conn.calls) == 2
    assert conn.calls[0][1][0] == "sh-collision_slug________"
    assert conn.calls[1][1][0] == "sh-accepted_slug_________"
    assert conn.calls[1][1][5] == "private"
    assert conn.calls[1][1][6] == '{"payload.stdout":"secret"}'


@pytest.mark.asyncio
async def test_create_shareable_object_accepts_explicit_visibility(monkeypatch) -> None:
    monkeypatch.setattr(so.secrets, "token_urlsafe", lambda _n: "team_slug____________")
    row = {
        "share_id": "sh-team_slug____________",
        "object_kind": "block",
        "object_id": "b-1",
        "tenant_id": "t-a",
        "owner_user_id": "u-a",
        "visibility": "team",
        "expires_at": None,
        "redaction_applied": {},
        "created_at": "2026-05-06 00:00:00",
    }
    conn = FakeConn([row])

    created = await so.create_shareable_object(
        conn,
        object_kind="block",
        object_id="b-1",
        tenant_id="t-a",
        owner_user_id="u-a",
        visibility="team",
    )

    assert created.visibility == "team"
    assert conn.calls[0][1][5] == "team"


@pytest.mark.asyncio
async def test_create_shareable_object_raises_after_retry_budget(monkeypatch) -> None:
    monkeypatch.setattr(so.secrets, "token_urlsafe", lambda _n: "same_slug____________")
    conn = FakeConn([None, None])

    with pytest.raises(so.ShareSlugCollisionError):
        await so.create_shareable_object(
            conn,
            object_kind="block",
            object_id="b-1",
            tenant_id="t-a",
            owner_user_id="u-a",
            max_attempts=2,
        )

    assert len(conn.calls) == 2


@pytest.mark.parametrize("bad_kind", ["", "Block", "block kind", "1block"])
def test_create_shareable_object_rejects_bad_object_kind(bad_kind: str) -> None:
    with pytest.raises(ValueError, match=re.escape("object_kind must match")):
        so._validate_object_kind(bad_kind)


def test_visibility_policy_constants_lock_four_levels() -> None:
    assert so.SHARE_VISIBILITIES == ("private", "team", "tenant", "public")
    assert so._TEAM_VISIBILITY_MEMBERSHIP_ROLES == frozenset(
        {"owner", "admin", "member"}
    )
    assert so._TENANT_VISIBILITY_MEMBERSHIP_ROLES == frozenset(
        {"owner", "admin", "member", "viewer"}
    )


@pytest.mark.parametrize("bad_visibility", ["", "workspace", "PUBLIC", "viewer"])
def test_validate_visibility_rejects_unknown_levels(bad_visibility: str) -> None:
    with pytest.raises(ValueError, match=re.escape("visibility must be one of")):
        so.validate_visibility(bad_visibility)


class FakeMembershipConn:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, sql, *params):
        self.calls.append((sql, params))
        return self.row


def _share(visibility: str) -> dict[str, str]:
    return {
        "share_id": "sh-test_slug__________",
        "object_kind": "block",
        "object_id": "b-1",
        "tenant_id": "t-a",
        "owner_user_id": "u-owner",
        "visibility": visibility,
    }


@pytest.mark.asyncio
async def test_acl_private_is_owner_only_without_membership_lookup() -> None:
    conn = FakeMembershipConn({"role": "admin", "status": "active"})

    assert await so.user_can_access_shareable_object(
        conn, _share("private"), caller_user_id="u-owner",
    )
    assert not await so.user_can_access_shareable_object(
        conn, _share("private"), caller_user_id="u-other",
    )

    assert conn.calls == []


@pytest.mark.asyncio
async def test_acl_team_includes_active_non_viewer_tenant_members() -> None:
    conn = FakeMembershipConn({"role": "member", "status": "active"})

    assert await so.user_can_access_shareable_object(
        conn, _share("team"), caller_user_id="u-member",
    )
    assert conn.calls == [
        (so._FETCH_USER_TENANT_MEMBERSHIP_SQL, ("u-member", "t-a")),
    ]


@pytest.mark.asyncio
async def test_acl_team_excludes_viewer_but_tenant_includes_viewer() -> None:
    viewer = {"role": "viewer", "status": "active"}

    assert not await so.user_can_access_shareable_object(
        FakeMembershipConn(viewer), _share("team"), caller_user_id="u-viewer",
    )
    assert await so.user_can_access_shareable_object(
        FakeMembershipConn(viewer), _share("tenant"), caller_user_id="u-viewer",
    )


@pytest.mark.asyncio
async def test_acl_rejects_suspended_membership() -> None:
    conn = FakeMembershipConn({"role": "admin", "status": "suspended"})

    assert not await so.user_can_access_shareable_object(
        conn, _share("tenant"), caller_user_id="u-admin",
    )


@pytest.mark.asyncio
async def test_acl_public_and_super_admin_short_circuit_membership_lookup() -> None:
    conn = FakeMembershipConn(None)

    assert await so.user_can_access_shareable_object(
        conn, _share("public"), caller_user_id=None,
    )
    assert await so.user_can_access_shareable_object(
        conn, _share("private"), caller_user_id="u-platform",
        caller_role="super_admin",
    )
    assert conn.calls == []
