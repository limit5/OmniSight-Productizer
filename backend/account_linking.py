"""AS.0.3 — account-linking takeover-prevention policy module.

Why this module exists
──────────────────────
When the AS.1 OAuth client lands, the riskiest single transition
is the moment an OAuth identity is bound to an *existing* OmniSight
user.  The classic takeover vector:

  1. Victim has ``foo@x.com`` with a password on OmniSight.
  2. Attacker registers ``foo@x.com`` at an OAuth IdP (DNS hijack,
     domain re-purchase, IdP signup loophole).
  3. Attacker clicks "Sign in with Google" — naive auto-link binds
     the IdP subject to the victim's user row.
  4. Attacker is logged in as the victim.

The fix codified here: **before adding any new auth method to a
user that already carries** ``"password"``, **the caller must prove
control of the password**.  This module owns the verification
guard so AS.1's OAuth callback handler is just a few lines:

    methods = await get_auth_methods(conn, user.id)
    if "password" in methods:
        # Will raise PasswordRequiredForLinkError if the password
        # the user typed in the link-confirmation form does not
        # verify against the stored hash.
        await require_password_verification_before_link(
            conn, user.id, presented_password,
        )
    await add_auth_method(conn, user.id, "oauth_google")

The schema half (``users.auth_methods`` JSONB / TEXT) is delivered
by alembic 0058.  This module is the only place that should
mutate the column — direct ``UPDATE users SET auth_methods = ...``
in handlers is forbidden so the takeover guard cannot be skipped
by accident.

What's NOT in this module
─────────────────────────
* OAuth client itself (provider handshake, JWT verification) —
  lands in AS.1 as ``backend/auth/oauth_client.py``.
* MFA enforcement — already lives in ``backend/mfa.py``;
  account-linking-after-MFA is composed at the router layer.
* Password verification primitive — uses the already-existing
  ``backend.auth.verify_password`` so we share the same Argon2
  hasher + the same dummy-hash timing-oracle defence.

Module-global / cross-worker state audit
────────────────────────────────────────
This module holds **zero** module-level state.  All public
functions take a DB connection (or use the pool internally) and
operate on row state in PG.  Every worker reads / writes the same
``auth_methods`` JSONB through the same connection abstraction —
**Answer #1** for the cross-worker question.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# ─── Method-name vocabulary ───────────────────────────────────────────────


METHOD_PASSWORD = "password"
"""Canonical name for the password (knowledge-factor) method."""

OAUTH_METHOD_PREFIX = "oauth_"
"""Prefix every OAuth-derived method name shares: ``oauth_google``,
``oauth_github``, ``oauth_apple``, ``oauth_microsoft``, ``oauth_discord``,
``oauth_gitlab``, ``oauth_bitbucket``, ``oauth_slack``, ``oauth_notion``, ...
"""

# Whitelist of provider names AS.1 will land.  Adding a new
# provider here without a matching client implementation in
# AS.1 is harmless (the helper accepts the tag, but no caller
# emits it); removing one risks silently breaking an existing
# bound user — don't.
_AS1_OAUTH_PROVIDERS = frozenset(
    {
        "google", "github", "apple", "microsoft", "discord", "gitlab",
        "bitbucket", "slack", "notion",
    }
)


def is_valid_method(method: str) -> bool:
    """Return True iff *method* is a syntactically valid method tag.

    The set of legal tags is:

    * ``"password"`` — the knowledge factor.
    * ``"oauth_<provider>"`` — where ``<provider>`` is one of the
      AS.1 catalog providers.

    Anything else (empty string, unknown OAuth provider, mixed
    case, leading/trailing whitespace) returns False.  Callers
    raise ``ValueError`` rather than swallow — silently ignoring
    a bad tag would let a future bug write garbage into the
    column.
    """
    if not isinstance(method, str) or not method:
        return False
    if method == METHOD_PASSWORD:
        return True
    if method.startswith(OAUTH_METHOD_PREFIX):
        provider = method[len(OAUTH_METHOD_PREFIX):]
        return provider in _AS1_OAUTH_PROVIDERS
    return False


def _ensure_valid_method(method: str) -> None:
    if not is_valid_method(method):
        raise ValueError(
            f"unknown auth method: {method!r} "
            f"(expected {METHOD_PASSWORD!r} or "
            f"{OAUTH_METHOD_PREFIX}<provider> for "
            f"provider in {sorted(_AS1_OAUTH_PROVIDERS)})"
        )


# ─── Exceptions ───────────────────────────────────────────────────────────


class AccountLinkingError(Exception):
    """Base class for AS.0.3 link-flow errors."""


class PasswordRequiredForLinkError(AccountLinkingError):
    """Raised when an OAuth-link attempt against a password-having
    user is missing or has an invalid presented password.

    Routers should map this to **HTTP 401** with a body that
    instructs the user to enter their existing password to confirm
    they own the account before the OAuth identity is bound.
    """


class OAuthOnlyAccountError(AccountLinkingError):
    """Raised when a password-reset-style endpoint is invoked
    against a user whose ``auth_methods`` does NOT contain
    ``"password"``.

    Routers should map this to **HTTP 400** with a body that
    instructs the user to manage credentials at the OAuth
    provider (per design doc §3.3 case C).
    """


# ─── Read helpers ─────────────────────────────────────────────────────────


async def get_auth_methods(conn, user_id: str) -> list[str]:
    """Return the user's current auth-methods array (decoded).

    Reads ``users.auth_methods`` (JSONB on PG, TEXT-of-JSON on
    SQLite).  Returns ``[]`` for a missing user — callers that
    care about user existence should fetch the row themselves.

    The list is returned in insertion order (whatever order the
    last UPDATE wrote).  Callers that need set semantics should
    convert via ``set(...)``.
    """
    row = await conn.fetchrow(
        "SELECT auth_methods FROM users WHERE id = $1", user_id,
    )
    if row is None:
        return []
    raw = row["auth_methods"]
    return _decode(raw)


def _decode(raw) -> list[str]:
    """Normalise the column value to ``list[str]``.

    asyncpg returns ``jsonb`` columns as already-parsed Python
    objects; aiosqlite returns the underlying TEXT verbatim.
    Either shape is converted to a list of strings; non-string
    elements are silently dropped (defence in depth — the helper
    is the only legitimate writer, but a hand-edit could plant
    a non-string and we don't want to crash login flows).
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(m) for m in raw if isinstance(m, str)]
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(m) for m in parsed if isinstance(m, str)]
    return []


async def has_method(conn, user_id: str, method: str) -> bool:
    """Convenience: True iff the user currently has *method* bound."""
    return method in await get_auth_methods(conn, user_id)


async def is_oauth_only(conn, user_id: str) -> bool:
    """True iff the user has at least one OAuth method and no
    password method.

    Used by the password-reset endpoint guard (design doc §3.3
    case C): refuse the request and surface ``OAuthOnlyAccountError``.
    A user with NO methods recorded (empty array) is NOT considered
    oauth-only — they're "credential-less" (e.g. invited but never
    completed signup) and the password-reset flow is the legitimate
    way to attach a password.
    """
    methods = set(await get_auth_methods(conn, user_id))
    if not methods:
        return False
    if METHOD_PASSWORD in methods:
        return False
    return any(m.startswith(OAUTH_METHOD_PREFIX) for m in methods)


# ─── Write helpers ────────────────────────────────────────────────────────


async def _write_methods(conn, user_id: str, methods: Iterable[str]) -> None:
    """Persist *methods* to the user's row, deduped and ordered.

    Uses a JSON literal in the SQL because asyncpg's jsonb codec
    accepts a JSON-encoded string for the cast; the SQLite mirror
    in dev tests stores the same TEXT shape so the round-trip is
    identical.
    """
    deduped: list[str] = []
    seen: set[str] = set()
    for m in methods:
        if m in seen:
            continue
        seen.add(m)
        deduped.append(m)
    payload = json.dumps(deduped, separators=(",", ":"))
    # The CAST is a no-op on SQLite (TEXT-of-JSON column) and the
    # canonical jsonb-bind path on PG.  ``COALESCE`` is unnecessary
    # because the column is NOT NULL with default ``'[]'``.
    await conn.execute(
        "UPDATE users SET auth_methods = $1 WHERE id = $2",
        payload, user_id,
    )


async def add_auth_method(conn, user_id: str, method: str) -> list[str]:
    """Append *method* to the user's ``auth_methods`` array if absent.

    **Security note**: this helper does NOT enforce the
    takeover-prevention rule by itself — callers wiring an OAuth
    link flow must call ``require_password_verification_before_link``
    *before* invoking this helper if the user already has a
    ``"password"`` method.  The helper is intentionally low-level
    so the bootstrap admin path (which trivially has the password
    in hand because it just hashed it) and the AS.1 OAuth router
    can share it.

    Returns the new methods list.
    """
    _ensure_valid_method(method)
    current = await get_auth_methods(conn, user_id)
    if method in current:
        return current
    new = current + [method]
    await _write_methods(conn, user_id, new)
    logger.info(
        "[ACCOUNT_LINKING] add_auth_method user=%s method=%s now=%s",
        user_id, method, new,
    )
    return new


async def remove_auth_method(
    conn, user_id: str, method: str,
) -> list[str]:
    """Remove *method* from the user's ``auth_methods`` array.

    No-op if *method* is not present.  Callers should refuse to
    leave an enabled user with an empty methods array (that
    user would be unable to log in); this helper does NOT enforce
    that — it's a low-level mutator.  The router-level guard
    against "removing the last method" lives where the human
    intent is visible (e.g. the unlink-OAuth UI confirms the
    user still has password before letting the request through).
    """
    _ensure_valid_method(method)
    current = await get_auth_methods(conn, user_id)
    if method not in current:
        return current
    new = [m for m in current if m != method]
    await _write_methods(conn, user_id, new)
    logger.info(
        "[ACCOUNT_LINKING] remove_auth_method user=%s method=%s now=%s",
        user_id, method, new,
    )
    return new


# ─── Takeover-prevention guard ────────────────────────────────────────────


async def require_password_verification_before_link(
    conn,
    user_id: str,
    presented_password: Optional[str],
) -> None:
    """Refuse to proceed unless *presented_password* verifies against
    the user's stored hash.

    This is the canonical guard wrappers around every code path
    that adds a NEW auth method to a user that already carries
    ``"password"``.  A caller that skips this guard is, by the
    AS.0.3 contract, opening the takeover hole.

    The guard:

    * Reads the current ``auth_methods``.  If ``"password"`` is
      NOT present, the takeover scenario doesn't apply — the user
      is OAuth-only or credential-less, and a different flow
      handles those (case B + case C in design doc §3.3); the
      guard returns silently so callers can use it
      unconditionally.
    * If ``"password"`` IS present and *presented_password* is
      None / empty, raises ``PasswordRequiredForLinkError`` so
      the router can return 401 with a "type your password to
      confirm" prompt.
    * If *presented_password* is non-empty but does NOT verify
      against the stored hash (or the user row is missing /
      disabled), raises ``PasswordRequiredForLinkError`` — same
      response shape, no oracle distinction between
      missing-user and wrong-password (we lean on the
      ``_DUMMY_PASSWORD_HASH`` timing-oracle defence in
      ``backend.auth``).

    The function is async so it can sit in the same await chain
    as the OAuth handler's other DB I/O — no separate
    transaction is opened so the verification + the subsequent
    ``add_auth_method`` ride the caller's tx.
    """
    methods = await get_auth_methods(conn, user_id)
    if METHOD_PASSWORD not in methods:
        # Case B / C — user has no password method, nothing to verify.
        return

    if not presented_password:
        raise PasswordRequiredForLinkError(
            "password verification required before account linking"
        )

    row = await conn.fetchrow(
        "SELECT password_hash, enabled FROM users WHERE id = $1", user_id,
    )
    # Import inside the function so test fixtures that monkeypatch
    # backend.auth still see the patched copy and we don't spin up
    # the Argon2 hasher just by importing this module.
    from backend.auth import verify_password

    stored_hash = (row["password_hash"] if row else "") or ""
    enabled = bool(row["enabled"]) if row else False

    # Always run verify_password — even on a missing user — so the
    # response time of the failure path matches the success path
    # (timing-oracle defence; same pattern as the login route).
    if not verify_password(presented_password, stored_hash) or not enabled:
        raise PasswordRequiredForLinkError(
            "password verification failed"
        )


async def link_oauth_after_verification(
    conn,
    user_id: str,
    oauth_method: str,
    presented_password: Optional[str],
) -> list[str]:
    """One-shot: verify password (if applicable), then add the OAuth
    method.

    Convenience wrapper that bundles the takeover-prevention guard
    with the actual ``add_auth_method``.  Routers should prefer
    this over calling the two pieces by hand so the guard is
    impossible to forget.

    Raises ``PasswordRequiredForLinkError`` (verbatim from the
    inner guard) when verification fails; raises ``ValueError``
    if *oauth_method* doesn't match the OAuth-prefix vocabulary.
    Returns the post-add methods list on success.
    """
    if not oauth_method.startswith(OAUTH_METHOD_PREFIX):
        raise ValueError(
            f"link_oauth_after_verification refuses non-oauth method "
            f"{oauth_method!r}; use add_auth_method directly for "
            f"the password-bootstrap path"
        )
    _ensure_valid_method(oauth_method)
    await require_password_verification_before_link(
        conn, user_id, presented_password,
    )
    return await add_auth_method(conn, user_id, oauth_method)


# ─── Default-method helpers for INSERT paths ──────────────────────────────


def initial_methods_for_new_user(
    *, password: Optional[str], oauth_methods: Iterable[str] = (),
) -> list[str]:
    """Compute the ``auth_methods`` array a brand-new user row
    should be inserted with.

    * If *password* is non-empty (and therefore the caller will
      hash + store it), seed ``["password"]``.
    * If the user is being created via an OAuth-first flow —
      brand-new IdP subject, no existing OmniSight user row —
      *oauth_methods* lists the providers to seed.  AS.0.3 itself
      doesn't drive this code path (no caller emits OAuth methods
      yet — AS.1 will), but the helper accepts it so callers
      don't write the JSON literal by hand.
    * If both are empty, returns ``[]`` — the
      invited-but-not-completed shape.

    The OAuth method names are validated; an unknown name raises
    ``ValueError`` to fail-fast at the call site rather than
    plant garbage in the column.
    """
    methods: list[str] = []
    if password:
        methods.append(METHOD_PASSWORD)
    for m in oauth_methods:
        _ensure_valid_method(m)
        if m not in methods:
            methods.append(m)
    return methods


def encode_methods_for_insert(methods: Iterable[str]) -> str:
    """Stable JSON encoding of *methods* for INSERT VALUES.

    Returns the canonical JSON-array string the column stores.
    Empty list maps to ``"[]"`` (NOT ``""``) — that's the column
    DEFAULT shape, so a caller that passes an empty iterable
    still produces a valid JSONB literal on PG.
    """
    return json.dumps(list(methods), separators=(",", ":"))
