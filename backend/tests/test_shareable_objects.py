"""WP.9.2 -- shareable_objects permalink slug contracts."""

from __future__ import annotations

import pathlib
import re
import secrets

import pytest

from backend import shareable_objects as so


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
    assert "ON CONFLICT (share_id) DO NOTHING" in sql
    assert "RETURNING share_id" in sql
    assert "$6::jsonb" in sql


class FakeConn:
    def __init__(self, rows):
        self._rows = list(rows)
        self.calls = []

    async def fetchrow(self, sql, *params):
        self.calls.append((sql, params))
        return self._rows.pop(0)


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
    assert conn.calls[1][1][5] == '{"payload.stdout":"secret"}'


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
