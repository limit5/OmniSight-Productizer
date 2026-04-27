"""AS.0.3 — ``backend.account_linking`` helper module contract.

The helper is the canonical owner of the ``users.auth_methods``
column.  It enforces the takeover-prevention rule
(design doc §3.3 / inventory R31) and is the only legitimate
writer of method tags.  This test exercises every branch:

* method-name vocabulary validation (password, oauth_<provider>,
  rejection of unknowns)
* read helpers (``get_auth_methods``, ``has_method``,
  ``is_oauth_only``)
* low-level mutators (``add_auth_method`` is idempotent;
  ``remove_auth_method`` is a no-op when absent)
* takeover guard:
    - returns silently when user has no password method
    - raises ``PasswordRequiredForLinkError`` on missing/wrong
      password for password-having users
    - returns silently when verification succeeds
* one-shot wrapper (verify-then-link) refuses non-oauth tags
* INSERT-path helpers (``initial_methods_for_new_user``,
  ``encode_methods_for_insert``)

The DB layer is faked with an in-memory dict of users — that's
sufficient because the helper's SQL is exactly two statements
(``SELECT auth_methods, password_hash, enabled FROM users WHERE
id = $1`` and ``UPDATE users SET auth_methods = $1 WHERE id =
$2``) so a hand-rolled fake conn that returns the right shape
exercises the same code path the production asyncpg conn does.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from backend.account_linking import (
    AccountLinkingError,
    METHOD_PASSWORD,
    OAUTH_METHOD_PREFIX,
    OAuthOnlyAccountError,
    PasswordRequiredForLinkError,
    add_auth_method,
    encode_methods_for_insert,
    get_auth_methods,
    has_method,
    initial_methods_for_new_user,
    is_oauth_only,
    is_valid_method,
    link_oauth_after_verification,
    remove_auth_method,
    require_password_verification_before_link,
)


# ─── Fake DB conn ─────────────────────────────────────────────────────────


class _FakeRow(dict):
    """Dict that also supports ``row["key"]`` access like asyncpg.Record."""


class _FakeConn:
    """Tiny stand-in for asyncpg.Connection — enough surface to
    drive the account_linking helpers."""

    def __init__(self, users: dict[str, dict[str, Any]]) -> None:
        self.users = users
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        if "SELECT auth_methods FROM users WHERE id" in sql:
            uid = args[0]
            user = self.users.get(uid)
            if user is None:
                return None
            return _FakeRow(auth_methods=user["auth_methods"])
        if "SELECT password_hash, enabled FROM users WHERE id" in sql:
            uid = args[0]
            user = self.users.get(uid)
            if user is None:
                return None
            return _FakeRow(
                password_hash=user["password_hash"],
                enabled=user["enabled"],
            )
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        if "UPDATE users SET auth_methods" in sql:
            payload, uid = args
            self.users[uid]["auth_methods"] = payload
            return
        raise AssertionError(f"unexpected execute SQL: {sql}")


def _make_user(
    *,
    auth_methods: list[str] | str = "[]",
    password_hash: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    if isinstance(auth_methods, list):
        auth_methods = json.dumps(auth_methods)
    return {
        "auth_methods": auth_methods,
        "password_hash": password_hash,
        "enabled": enabled,
    }


# ─── Group 1: method-name vocabulary ──────────────────────────────────────


class TestIsValidMethod:
    def test_password_is_valid(self):
        assert is_valid_method("password")

    @pytest.mark.parametrize(
        "provider", ["google", "github", "apple", "microsoft"],
    )
    def test_known_oauth_providers_valid(self, provider):
        assert is_valid_method(f"{OAUTH_METHOD_PREFIX}{provider}")

    @pytest.mark.parametrize(
        "bad", [
            "",                  # empty
            "Password",          # case-sensitive
            "oauth_facebook",    # unknown provider
            "oauth_",            # bare prefix
            "mfa_totp",          # second-factor — not a first-factor tag
            "api_key",           # second-factor — not a first-factor tag
            "  password  ",      # whitespace
        ],
    )
    def test_unknown_methods_rejected(self, bad):
        assert not is_valid_method(bad)

    def test_non_string_rejected(self):
        assert not is_valid_method(None)  # type: ignore[arg-type]
        assert not is_valid_method(123)   # type: ignore[arg-type]


# ─── Group 2: read helpers ────────────────────────────────────────────────


class TestGetAuthMethods:
    @pytest.mark.asyncio
    async def test_returns_decoded_list_for_text_column(self):
        conn = _FakeConn({"u1": _make_user(auth_methods=["password"])})
        assert await get_auth_methods(conn, "u1") == ["password"]

    @pytest.mark.asyncio
    async def test_returns_empty_for_missing_user(self):
        conn = _FakeConn({})
        assert await get_auth_methods(conn, "ghost") == []

    @pytest.mark.asyncio
    async def test_handles_pg_already_parsed_list(self):
        # asyncpg returns jsonb columns as already-parsed Python objects.
        conn = _FakeConn({
            "u1": {
                "auth_methods": ["password", "oauth_google"],
                "password_hash": "h",
                "enabled": True,
            },
        })
        assert await get_auth_methods(conn, "u1") == [
            "password", "oauth_google",
        ]

    @pytest.mark.asyncio
    async def test_handles_garbage_text_gracefully(self):
        # Defence in depth — a hand-edit that planted non-JSON
        # shouldn't crash login flows.
        conn = _FakeConn({
            "u1": {
                "auth_methods": "not-json",
                "password_hash": "h",
                "enabled": True,
            },
        })
        assert await get_auth_methods(conn, "u1") == []


class TestHasMethod:
    @pytest.mark.asyncio
    async def test_true_for_present_method(self):
        conn = _FakeConn({"u1": _make_user(auth_methods=["password"])})
        assert await has_method(conn, "u1", "password") is True

    @pytest.mark.asyncio
    async def test_false_for_absent_method(self):
        conn = _FakeConn({"u1": _make_user(auth_methods=["password"])})
        assert await has_method(conn, "u1", "oauth_google") is False


class TestIsOauthOnly:
    @pytest.mark.asyncio
    async def test_true_when_only_oauth(self):
        conn = _FakeConn({
            "u1": _make_user(auth_methods=["oauth_google"]),
        })
        assert await is_oauth_only(conn, "u1") is True

    @pytest.mark.asyncio
    async def test_false_when_password_present(self):
        conn = _FakeConn({
            "u1": _make_user(auth_methods=["password", "oauth_google"]),
        })
        assert await is_oauth_only(conn, "u1") is False

    @pytest.mark.asyncio
    async def test_false_when_credential_less(self):
        # Empty array = invited-but-not-completed — the password-reset
        # flow is the legitimate path to attach a password, NOT a
        # case-C reject.
        conn = _FakeConn({"u1": _make_user(auth_methods=[])})
        assert await is_oauth_only(conn, "u1") is False


# ─── Group 3: low-level mutators ──────────────────────────────────────────


class TestAddAuthMethod:
    @pytest.mark.asyncio
    async def test_appends_new_method(self):
        conn = _FakeConn({"u1": _make_user(auth_methods=["password"])})
        result = await add_auth_method(conn, "u1", "oauth_google")
        assert result == ["password", "oauth_google"]
        assert json.loads(conn.users["u1"]["auth_methods"]) == [
            "password", "oauth_google",
        ]

    @pytest.mark.asyncio
    async def test_idempotent_when_method_present(self):
        conn = _FakeConn({"u1": _make_user(auth_methods=["password"])})
        result = await add_auth_method(conn, "u1", "password")
        assert result == ["password"]
        # Idempotent path still issues the read but should NOT
        # write — verify no UPDATE landed.
        assert not any(
            "UPDATE users" in sql for sql, _ in conn.execute_calls
        )

    @pytest.mark.asyncio
    async def test_rejects_unknown_method(self):
        conn = _FakeConn({"u1": _make_user()})
        with pytest.raises(ValueError):
            await add_auth_method(conn, "u1", "oauth_facebook")


class TestRemoveAuthMethod:
    @pytest.mark.asyncio
    async def test_removes_present_method(self):
        conn = _FakeConn({
            "u1": _make_user(auth_methods=["password", "oauth_google"]),
        })
        result = await remove_auth_method(conn, "u1", "oauth_google")
        assert result == ["password"]

    @pytest.mark.asyncio
    async def test_noop_when_absent(self):
        conn = _FakeConn({"u1": _make_user(auth_methods=["password"])})
        result = await remove_auth_method(conn, "u1", "oauth_google")
        assert result == ["password"]
        assert not any(
            "UPDATE users" in sql for sql, _ in conn.execute_calls
        )

    @pytest.mark.asyncio
    async def test_rejects_unknown_method(self):
        conn = _FakeConn({"u1": _make_user()})
        with pytest.raises(ValueError):
            await remove_auth_method(conn, "u1", "totally_made_up")


# ─── Group 4: takeover-prevention guard ───────────────────────────────────


@pytest.fixture(autouse=True)
def patch_verify_password(monkeypatch):
    """Replace ``backend.auth.verify_password`` with a deterministic
    stub so the test doesn't take Argon2's slow path.  The stub
    accepts only the literal pair ``("correct-password", "correct-hash")``.
    """
    from backend import auth as backend_auth

    def _stub(plaintext: str, stored_hash: str) -> bool:
        return (
            plaintext == "correct-password"
            and stored_hash == "correct-hash"
        )

    monkeypatch.setattr(backend_auth, "verify_password", _stub)


class TestRequirePasswordVerificationBeforeLink:
    @pytest.mark.asyncio
    async def test_returns_silently_when_no_password_method(self):
        # Case B / C — user has no password to verify against.  The
        # takeover risk doesn't apply, so the guard is a no-op.
        conn = _FakeConn({
            "u1": _make_user(
                auth_methods=["oauth_google"],
                password_hash="",
            ),
        })
        # Should NOT raise even with None presented_password.
        await require_password_verification_before_link(conn, "u1", None)

    @pytest.mark.asyncio
    async def test_raises_when_presented_password_missing(self):
        conn = _FakeConn({
            "u1": _make_user(
                auth_methods=["password"],
                password_hash="correct-hash",
            ),
        })
        with pytest.raises(PasswordRequiredForLinkError):
            await require_password_verification_before_link(
                conn, "u1", None,
            )

    @pytest.mark.asyncio
    async def test_raises_when_password_wrong(self):
        conn = _FakeConn({
            "u1": _make_user(
                auth_methods=["password"],
                password_hash="correct-hash",
            ),
        })
        with pytest.raises(PasswordRequiredForLinkError):
            await require_password_verification_before_link(
                conn, "u1", "wrong-password",
            )

    @pytest.mark.asyncio
    async def test_returns_silently_when_password_correct(self):
        conn = _FakeConn({
            "u1": _make_user(
                auth_methods=["password"],
                password_hash="correct-hash",
            ),
        })
        await require_password_verification_before_link(
            conn, "u1", "correct-password",
        )

    @pytest.mark.asyncio
    async def test_raises_when_user_disabled(self):
        # Disabled users are a takeover target too — refuse to bind
        # OAuth onto a disabled account even when the password is
        # correct.  The router that enabled the user can re-enable
        # then re-link; the helper refuses to be the bypass.
        conn = _FakeConn({
            "u1": _make_user(
                auth_methods=["password"],
                password_hash="correct-hash",
                enabled=False,
            ),
        })
        with pytest.raises(PasswordRequiredForLinkError):
            await require_password_verification_before_link(
                conn, "u1", "correct-password",
            )

    @pytest.mark.asyncio
    async def test_inherits_account_linking_error(self):
        # The router-layer mapper distinguishes AccountLinkingError
        # from a generic Exception so the inheritance is part of
        # the contract.
        conn = _FakeConn({
            "u1": _make_user(
                auth_methods=["password"],
                password_hash="correct-hash",
            ),
        })
        with pytest.raises(AccountLinkingError):
            await require_password_verification_before_link(
                conn, "u1", "wrong-password",
            )


class TestLinkOauthAfterVerification:
    @pytest.mark.asyncio
    async def test_password_user_correct_password_links(self):
        conn = _FakeConn({
            "u1": _make_user(
                auth_methods=["password"],
                password_hash="correct-hash",
            ),
        })
        result = await link_oauth_after_verification(
            conn, "u1", "oauth_google", "correct-password",
        )
        assert result == ["password", "oauth_google"]

    @pytest.mark.asyncio
    async def test_password_user_wrong_password_blocked(self):
        conn = _FakeConn({
            "u1": _make_user(
                auth_methods=["password"],
                password_hash="correct-hash",
            ),
        })
        with pytest.raises(PasswordRequiredForLinkError):
            await link_oauth_after_verification(
                conn, "u1", "oauth_google", "wrong-password",
            )
        # Crucially, the OAuth method must NOT be appended on the
        # rejection path — verify the column wasn't mutated.
        assert json.loads(conn.users["u1"]["auth_methods"]) == ["password"]

    @pytest.mark.asyncio
    async def test_oauth_only_user_links_without_password(self):
        # Case C-adjacent: the user already has oauth_google, they're
        # binding a second OAuth provider.  No password to verify.
        conn = _FakeConn({
            "u1": _make_user(
                auth_methods=["oauth_google"],
                password_hash="",
            ),
        })
        result = await link_oauth_after_verification(
            conn, "u1", "oauth_github", None,
        )
        assert result == ["oauth_google", "oauth_github"]

    @pytest.mark.asyncio
    async def test_refuses_password_method(self):
        # The wrapper is OAuth-only by design — bootstrapping a
        # password method goes through the password-set path
        # (change_password / invite acceptance).
        conn = _FakeConn({"u1": _make_user()})
        with pytest.raises(ValueError):
            await link_oauth_after_verification(
                conn, "u1", "password", None,
            )

    @pytest.mark.asyncio
    async def test_refuses_unknown_oauth_provider(self):
        conn = _FakeConn({"u1": _make_user()})
        with pytest.raises(ValueError):
            await link_oauth_after_verification(
                conn, "u1", "oauth_facebook", None,
            )


# ─── Group 5: INSERT-path helpers ─────────────────────────────────────────


class TestInitialMethodsForNewUser:
    def test_password_only(self):
        assert initial_methods_for_new_user(password="pw") == ["password"]

    def test_no_password_no_oauth(self):
        # Invited-but-not-completed shape — empty array.
        assert initial_methods_for_new_user(password=None) == []
        assert initial_methods_for_new_user(password="") == []

    def test_oauth_first_signup(self):
        result = initial_methods_for_new_user(
            password=None, oauth_methods=["oauth_google"],
        )
        assert result == ["oauth_google"]

    def test_password_plus_oauth(self):
        result = initial_methods_for_new_user(
            password="pw", oauth_methods=["oauth_google"],
        )
        assert result == ["password", "oauth_google"]

    def test_dedupes(self):
        result = initial_methods_for_new_user(
            password="pw",
            oauth_methods=["oauth_google", "oauth_google"],
        )
        assert result == ["password", "oauth_google"]

    def test_rejects_unknown_oauth_method(self):
        with pytest.raises(ValueError):
            initial_methods_for_new_user(
                password="pw", oauth_methods=["oauth_facebook"],
            )


class TestEncodeMethodsForInsert:
    def test_empty_array_encodes_to_jsonb_literal(self):
        # Empty list maps to "[]" — the column DEFAULT shape, valid
        # JSONB on PG, valid TEXT on SQLite.  Critically NOT "" (an
        # empty TEXT would not parse as JSON downstream).
        assert encode_methods_for_insert([]) == "[]"

    def test_password_only_encodes(self):
        assert encode_methods_for_insert(["password"]) == '["password"]'

    def test_compact_no_whitespace(self):
        # asyncpg jsonb codec accepts either form, but the canonical
        # encoding is whitespace-free for byte-stable round-trip.
        result = encode_methods_for_insert(["password", "oauth_google"])
        assert " " not in result
