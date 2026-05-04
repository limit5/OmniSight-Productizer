"""Phase 5-3 (#multi-account-forge) — URL-pattern resolver contract tests.

Locks the behaviour landed by row 5-3 of Phase 5:

1. **Pattern syntax** — glob via :mod:`fnmatch`. Matched against the
   scheme-stripped lowercased URL form so a single pattern (e.g.
   ``github.com/acme-corp/*``) covers both HTTPS and SSH URLs for
   the same repo.
2. **Multi-match → first-match-wins** — deterministic ordering driven
   by the SELECT ``ORDER BY is_default DESC, last_used_at DESC NULLS
   LAST, platform, id``. Default account beats non-default; among
   non-defaults, more-recently-used wins.
3. **No-match → falls back to platform default** (Phase 5-2 already
   tested; this row re-asserts the contract).
4. **No-default + no-match → raise** via
   :func:`require_account_for_url` raising
   :class:`MissingCredentialError`. Distinct exception so call sites
   can catch this specifically vs. unrelated lookup errors.
5. **Special chars in org name** — dots / dashes / underscores are
   literal in glob patterns, NOT regex metacharacters.
   ``github.com/acme.corp/*`` matches ``github.com/acme.corp/repo``
   but not ``github.com/acmeXcorp/repo``.
6. **``last_used_at`` touch on successful resolve** — best-effort
   UPDATE fires after each :func:`pick_account_for_url` /
   :func:`pick_default` / :func:`pick_by_id` that returns a row.
   ``touch=False`` suppresses for debug callers. No touch when the
   pool is absent or the resolve returned ``None``.

Module-global / read-after-write audit (SOP Step 1)
───────────────────────────────────────────────────
No new module-globals introduced by row 5-3. The autouse fixture
from this module resets ``_CREDENTIALS_CACHE`` /
``_LEGACY_WARN_EMITTED`` per test (same pattern as the 5-2 tests).
The touch helper uses a single-statement auto-commit UPDATE on the
pool — no read-after-write timing changes vs the 5-2 baseline.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures — isolate cache + warn flag per test, build a stub pool
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture(autouse=True)
def _fresh_credential_module_state():
    from backend import git_credentials as gc
    gc.clear_credential_cache()
    gc._reset_deprecation_warn_for_tests()
    yield
    gc.clear_credential_cache()
    gc._reset_deprecation_warn_for_tests()


class _FakeRow(dict):
    """Minimal asyncpg.Record-compatible stand-in."""


class _FakeConn:
    """Stub asyncpg.Connection that records every query so tests can
    assert SELECT vs UPDATE ordering and arg shape."""

    def __init__(self, rows: list[_FakeRow], touch_log: list[tuple]):
        self._rows = rows
        self._touch_log = touch_log

    async def fetch(self, sql: str, *args: Any) -> list[_FakeRow]:
        tid = args[0]
        return [r for r in self._rows if r["tenant_id"] == tid]

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRow | None:
        tid = args[0]
        target_id = args[1] if len(args) > 1 else None
        for r in self._rows:
            if r["tenant_id"] == tid and (target_id is None or r["id"] == target_id):
                return r
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        # Record the touch so tests can assert it fired with the right id.
        if "UPDATE git_accounts" in sql:
            # signature: (last_used_at, id, tenant_id)
            self._touch_log.append(("touch", args[1], args[2], args[0]))
            for r in self._rows:
                if r["id"] == args[1] and r["tenant_id"] == args[2]:
                    r["last_used_at"] = args[0]
        return "OK"


class _FakePool:
    def __init__(self, rows: list[_FakeRow]):
        self.rows = rows
        self.touch_log: list[tuple] = []

    def acquire(self):
        conn = _FakeConn(self.rows, self.touch_log)

        class _CM:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _CM()


def _encrypted(plain: str) -> str:
    from backend.secret_store import encrypt
    return encrypt(plain) if plain else ""


def _row(
    *,
    account_id: str,
    platform: str = "github",
    instance_url: str = "https://github.com",
    token_plain: str = "tkn",
    url_patterns: list[str] | None = None,
    is_default: bool = False,
    last_used_at: float | None = None,
    tenant_id: str = "t-default",
) -> _FakeRow:
    return _FakeRow({
        "id": account_id,
        "tenant_id": tenant_id,
        "platform": platform,
        "instance_url": instance_url,
        "label": "",
        "username": "",
        "encrypted_token": _encrypted(token_plain),
        "encrypted_ssh_key": "",
        "ssh_host": "",
        "ssh_port": 0,
        "project": "",
        "encrypted_webhook_secret": "",
        "url_patterns": json.dumps(url_patterns or []),
        "auth_type": "pat",
        "is_default": is_default,
        "enabled": True,
        "metadata": "{}",
        "last_used_at": last_used_at,
        "created_at": 1.0,
        "updated_at": 1.0,
        "version": 0,
    })


@pytest.fixture
def _pool_with(monkeypatch):
    """Factory fixture — caller passes rows, gets a stubbed pool wired
    in via ``monkeypatch.setattr('backend.db_pool.get_pool', ...)``."""

    def _make(rows: list[_FakeRow]) -> _FakePool:
        pool = _FakePool(rows)
        monkeypatch.setattr("backend.db_pool.get_pool", lambda: pool)
        return pool

    return _make


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Pattern syntax — glob via fnmatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_normalize_url_strips_https_scheme():
    from backend.git_credentials import _normalize_url_for_pattern_match
    assert _normalize_url_for_pattern_match(
        "https://github.com/acme/app"
    ) == "github.com/acme/app"


def test_normalize_url_strips_git_at_form():
    from backend.git_credentials import _normalize_url_for_pattern_match
    assert _normalize_url_for_pattern_match(
        "git@github.com:acme/app.git"
    ) == "github.com/acme/app.git"


def test_normalize_url_strips_ssh_scheme_with_user():
    from backend.git_credentials import _normalize_url_for_pattern_match
    assert _normalize_url_for_pattern_match(
        "ssh://git@github.com/acme/app"
    ) == "github.com/acme/app"


def test_normalize_url_lowercases():
    from backend.git_credentials import _normalize_url_for_pattern_match
    assert _normalize_url_for_pattern_match(
        "https://GitHub.com/AcMe/App"
    ) == "github.com/acme/app"


def test_matches_pattern_basic_glob():
    from backend.git_credentials import _matches_pattern
    assert _matches_pattern("github.com/acme/app", "github.com/acme/*")
    assert _matches_pattern("github.com/acme/sub/nested", "github.com/acme/*")
    assert not _matches_pattern("github.com/other/app", "github.com/acme/*")


def test_matches_pattern_anchored_not_substring():
    """fnmatch is full-string anchored — pattern must match the whole
    URL, not just a substring. ``acme/*`` should NOT match
    ``github.com/acme/app`` because the pattern doesn't cover the
    leading host."""
    from backend.git_credentials import _matches_pattern
    assert not _matches_pattern("github.com/acme/app", "acme/*")


def test_matches_pattern_question_mark_single_char():
    from backend.git_credentials import _matches_pattern
    # ``?`` matches exactly one character.
    assert _matches_pattern("github.com/a/repo", "github.com/?/repo")
    assert not _matches_pattern("github.com/ab/repo", "github.com/?/repo")


def test_matches_pattern_empty_inputs_return_false():
    from backend.git_credentials import _matches_pattern
    assert not _matches_pattern("", "github.com/*")
    assert not _matches_pattern("github.com/x", "")
    assert not _matches_pattern("github.com/x", None)  # type: ignore[arg-type]
    assert not _matches_pattern("github.com/x", 42)  # type: ignore[arg-type]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Special chars in org name — dots/dashes/underscores are literal
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_dot_in_org_name_is_literal_not_regex_wildcard():
    """``.`` in a fnmatch pattern is a literal char (unlike regex).
    Pattern ``github.com/acme.corp/*`` must match
    ``github.com/acme.corp/repo`` but NOT ``github.com/acmeXcorp/repo``
    (which would match if dots were regex wildcards)."""
    from backend.git_credentials import _matches_pattern
    assert _matches_pattern(
        "github.com/acme.corp/repo", "github.com/acme.corp/*",
    )
    assert not _matches_pattern(
        "github.com/acmexcorp/repo", "github.com/acme.corp/*",
    )


def test_dash_underscore_in_org_name_literal():
    from backend.git_credentials import _matches_pattern
    assert _matches_pattern(
        "github.com/acme-corp_internal/x",
        "github.com/acme-corp_internal/*",
    )
    # Hyphens in patterns are literal — they're NOT a "range" outside
    # of a [...] character class.
    assert not _matches_pattern(
        "github.com/acmecorpinternal/x",
        "github.com/acme-corp_internal/*",
    )


def test_unicode_org_name_passes_through():
    """fnmatch is byte-string oblivious — non-ASCII org names should
    work as long as both pattern and URL are normalised the same way
    (lowercased here). Real-world case: ``gitlab.com/münchen-team/*``."""
    from backend.git_credentials import _matches_pattern
    assert _matches_pattern(
        "gitlab.com/münchen-team/repo",
        "gitlab.com/münchen-team/*",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Multi-match → first-match-wins (deterministic SELECT order)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_multi_match_default_row_wins(_pool_with):
    """Two rows both have patterns that match the URL — the
    ``is_default=TRUE`` row wins because the SELECT puts it first."""
    rows = [
        _row(
            account_id="ga-corp",
            token_plain="tok-corp",
            url_patterns=["github.com/acme/*"],
            is_default=False,
            last_used_at=100.0,
        ),
        _row(
            account_id="ga-personal",
            token_plain="tok-personal",
            url_patterns=["github.com/acme/*", "github.com/*"],
            is_default=True,
            last_used_at=50.0,
        ),
    ]
    # Sort by SELECT order (is_default DESC, last_used_at DESC NULLS LAST)
    # — the FakeConn returns rows as-is so we pre-sort to match prod.
    rows.sort(
        key=lambda r: (
            0 if r["is_default"] else 1,
            -(r["last_used_at"] or 0),
        )
    )
    _pool_with(rows)

    from backend.git_credentials import pick_account_for_url
    entry = await pick_account_for_url(
        "https://github.com/acme/app", tenant_id="t-default",
    )
    assert entry is not None
    assert entry["id"] == "ga-personal"
    assert entry["token"] == "tok-personal"


@pytest.mark.asyncio
async def test_multi_match_lru_breaks_ties_among_non_defaults(_pool_with):
    """No row is default — two non-default rows match. The more-
    recently-used one (later last_used_at) wins."""
    rows = [
        _row(
            account_id="ga-old",
            token_plain="tok-old",
            url_patterns=["github.com/acme/*"],
            is_default=False,
            last_used_at=10.0,
        ),
        _row(
            account_id="ga-recent",
            token_plain="tok-recent",
            url_patterns=["github.com/acme/*"],
            is_default=False,
            last_used_at=999.0,
        ),
    ]
    rows.sort(
        key=lambda r: (
            0 if r["is_default"] else 1,
            -(r["last_used_at"] or 0),
        )
    )
    _pool_with(rows)

    from backend.git_credentials import pick_account_for_url
    entry = await pick_account_for_url(
        "https://github.com/acme/x", tenant_id="t-default",
    )
    assert entry is not None
    assert entry["id"] == "ga-recent"


@pytest.mark.asyncio
async def test_specific_pattern_does_not_lose_to_default_with_no_pattern(
    _pool_with,
):
    """A specific ``url_patterns`` match wins over a platform default
    that has NO patterns set — pattern matching happens in step 1,
    platform default only fires in step 4 as a fallback."""
    rows = [
        _row(
            account_id="ga-corp",
            token_plain="tok-corp",
            url_patterns=["github.com/acme-corp/*"],
            is_default=False,
            last_used_at=10.0,
        ),
        _row(
            account_id="ga-default",
            token_plain="tok-default",
            url_patterns=[],  # no patterns
            is_default=True,
            last_used_at=999.0,
        ),
    ]
    rows.sort(
        key=lambda r: (
            0 if r["is_default"] else 1,
            -(r["last_used_at"] or 0),
        )
    )
    _pool_with(rows)

    from backend.git_credentials import pick_account_for_url
    entry = await pick_account_for_url(
        "https://github.com/acme-corp/secret-repo",
        tenant_id="t-default",
    )
    assert entry is not None
    # The pattern-matching row wins even though the other is_default.
    # Because pattern-matching is step 1 and looks at every row in
    # SELECT order; ga-corp's pattern matches before ga-default's
    # empty pattern list.
    assert entry["id"] == "ga-corp"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. No-match → platform default (already in 5-2, re-asserted here)
#     and 5. No-default + no-match → MissingCredentialError
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_no_pattern_match_falls_back_to_platform_default(_pool_with):
    rows = [
        _row(
            account_id="ga-default",
            token_plain="tok-default",
            url_patterns=[],
            is_default=True,
        ),
        _row(
            account_id="ga-specific",
            token_plain="tok-specific",
            url_patterns=["github.com/acme-corp/*"],
            is_default=False,
        ),
    ]
    rows.sort(key=lambda r: 0 if r["is_default"] else 1)
    _pool_with(rows)

    from backend.git_credentials import pick_account_for_url
    entry = await pick_account_for_url(
        "https://github.com/random-org/repo", tenant_id="t-default",
    )
    assert entry is not None
    # No pattern match → platform default wins.
    assert entry["id"] == "ga-default"


@pytest.mark.asyncio
async def test_require_returns_row_when_match(_pool_with):
    rows = [
        _row(
            account_id="ga-default",
            token_plain="tok-default",
            url_patterns=[],
            is_default=True,
        ),
    ]
    _pool_with(rows)

    from backend.git_credentials import require_account_for_url
    entry = await require_account_for_url(
        "https://github.com/x/y", tenant_id="t-default",
    )
    assert entry["id"] == "ga-default"
    assert entry["token"] == "tok-default"


@pytest.mark.asyncio
async def test_require_raises_when_no_default_and_no_match(
    _pool_with, monkeypatch,
):
    """No row is_default, no row has a matching pattern, no row even
    has the right platform — require_account_for_url raises
    MissingCredentialError, NOT a generic LookupError or KeyError."""
    rows = [
        _row(
            account_id="ga-gl",
            platform="gitlab",
            instance_url="https://gitlab.com",
            token_plain="tok-gl",
            url_patterns=["gitlab.com/team-a/*"],
            is_default=False,
        ),
    ]
    _pool_with(rows)
    # Force the legacy shim path to also return nothing — patch settings.
    from unittest.mock import patch
    with patch("backend.git_credentials.settings") as mock_settings:
        for attr in (
            "github_token", "gitlab_token", "gitlab_url", "git_ssh_key_path",
            "gerrit_ssh_host", "gerrit_url", "gerrit_project",
            "gerrit_webhook_secret", "git_credentials_file",
            "git_ssh_key_map", "github_token_map", "gitlab_token_map",
            "gerrit_instances", "github_webhook_secret",
            "gitlab_webhook_secret",
            # Phase 5-8: shim also reads JIRA scalars — empty them
            # so the "no-match" path stays truly empty.
            "notification_jira_url", "notification_jira_token",
            "notification_jira_project", "jira_webhook_secret",
        ):
            setattr(mock_settings, attr, "")
        mock_settings.gerrit_enabled = False
        mock_settings.gerrit_ssh_port = 29418

        from backend.git_credentials import (
            MissingCredentialError, require_account_for_url,
        )
        with pytest.raises(MissingCredentialError) as ei:
            await require_account_for_url(
                "https://github.com/acme/app", tenant_id="t-default",
            )
    # Error message names the URL + tenant so operators can grep logs.
    msg = str(ei.value)
    assert "github.com/acme/app" in msg
    assert "t-default" in msg


def test_missing_credential_error_is_lookup_error_subclass():
    """Inheriting from LookupError lets call sites that catch
    LookupError as a generic 'no credential' bucket still work,
    while specific call sites can catch MissingCredentialError."""
    from backend.git_credentials import MissingCredentialError
    assert issubclass(MissingCredentialError, LookupError)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. last_used_at touch on successful resolve
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_pick_account_for_url_touches_last_used_at(_pool_with):
    rows = [
        _row(
            account_id="ga-corp",
            token_plain="tok-corp",
            url_patterns=["github.com/acme/*"],
            is_default=False,
        ),
    ]
    pool = _pool_with(rows)

    from backend.git_credentials import pick_account_for_url
    await pick_account_for_url(
        "https://github.com/acme/app", tenant_id="t-default",
    )
    # Exactly one touch fired with the resolved id.
    touches = [t for t in pool.touch_log if t[0] == "touch"]
    assert len(touches) == 1
    assert touches[0][1] == "ga-corp"
    assert touches[0][2] == "t-default"
    # The touch timestamp is recent (epoch seconds, > 0).
    assert touches[0][3] > 0


@pytest.mark.asyncio
async def test_pick_default_touches_last_used_at(_pool_with):
    rows = [
        _row(
            account_id="ga-d",
            token_plain="tok-d",
            is_default=True,
        ),
    ]
    pool = _pool_with(rows)

    from backend.git_credentials import pick_default
    entry = await pick_default("github", tenant_id="t-default")
    assert entry is not None
    touches = [t for t in pool.touch_log if t[0] == "touch"]
    assert len(touches) == 1
    assert touches[0][1] == "ga-d"


@pytest.mark.asyncio
async def test_pick_by_id_touches_last_used_at(_pool_with):
    rows = [
        _row(
            account_id="ga-by-id",
            token_plain="tok-id",
        ),
    ]
    pool = _pool_with(rows)

    from backend.git_credentials import pick_by_id
    entry = await pick_by_id("ga-by-id", tenant_id="t-default")
    assert entry is not None
    touches = [t for t in pool.touch_log if t[0] == "touch"]
    assert len(touches) == 1
    assert touches[0][1] == "ga-by-id"


@pytest.mark.asyncio
async def test_touch_false_suppresses_update(_pool_with):
    rows = [
        _row(
            account_id="ga-corp",
            token_plain="tok-corp",
            url_patterns=["github.com/acme/*"],
            is_default=False,
        ),
    ]
    pool = _pool_with(rows)

    from backend.git_credentials import pick_account_for_url
    await pick_account_for_url(
        "https://github.com/acme/app",
        tenant_id="t-default",
        touch=False,
    )
    touches = [t for t in pool.touch_log if t[0] == "touch"]
    assert touches == []


@pytest.mark.asyncio
async def test_no_touch_when_resolve_returns_none(_pool_with):
    """A failed resolve must not write a touch row — there's no
    account id to touch. Defensive: regression guard against future
    refactors that bubble a touch up before checking the result."""
    pool = _pool_with([])  # empty registry

    from backend.git_credentials import pick_account_for_url

    # Force the shim fallback to also be empty so we get a True None.
    from unittest.mock import patch
    with patch("backend.git_credentials.settings") as mock_settings:
        for attr in (
            "github_token", "gitlab_token", "gitlab_url", "git_ssh_key_path",
            "gerrit_ssh_host", "gerrit_url", "gerrit_project",
            "gerrit_webhook_secret", "git_credentials_file",
            "git_ssh_key_map", "github_token_map", "gitlab_token_map",
            "gerrit_instances", "github_webhook_secret",
            "gitlab_webhook_secret",
            # Phase 5-8: shim also reads JIRA scalars — empty them
            # so the "no-match" path stays truly empty.
            "notification_jira_url", "notification_jira_token",
            "notification_jira_project", "jira_webhook_secret",
        ):
            setattr(mock_settings, attr, "")
        mock_settings.gerrit_enabled = False
        mock_settings.gerrit_ssh_port = 29418

        result = await pick_account_for_url(
            "https://nowhere.example/x", tenant_id="t-default",
        )
    assert result is None
    touches = [t for t in pool.touch_log if t[0] == "touch"]
    assert touches == []


@pytest.mark.asyncio
async def test_touch_silently_skips_when_no_pool(_empty_settings_mock=None):
    """``_touch_last_used_at`` must not raise when the pool isn't
    initialised — it's best-effort so dev / unit-test paths don't
    crash on every resolve."""
    from backend import db_pool
    from backend.git_credentials import _touch_last_used_at
    db_pool._reset_for_tests()
    # Should return cleanly, no exception.
    await _touch_last_used_at("ga-x", "t-default")


@pytest.mark.asyncio
async def test_touch_silently_skips_for_empty_account_id():
    from backend.git_credentials import _touch_last_used_at
    # No raise even if pool isn't set up — guard skips before pool lookup.
    await _touch_last_used_at("", "t-default")
    await _touch_last_used_at(None, "t-default")  # type: ignore[arg-type]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. SSH-form URL resolves via same pattern as HTTPS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_ssh_url_matches_same_pattern_as_https(_pool_with):
    """One pattern (``github.com/acme/*``) covers both
    ``https://github.com/acme/x`` and ``git@github.com:acme/x`` —
    operator only configures one pattern per repo set."""
    rows = [
        _row(
            account_id="ga-acme",
            token_plain="tok-acme",
            url_patterns=["github.com/acme/*"],
        ),
    ]
    _pool_with(rows)

    from backend.git_credentials import pick_account_for_url
    entry_https = await pick_account_for_url(
        "https://github.com/acme/repo", tenant_id="t-default",
    )
    entry_ssh = await pick_account_for_url(
        "git@github.com:acme/repo.git", tenant_id="t-default",
    )
    assert entry_https is not None and entry_ssh is not None
    assert entry_https["id"] == entry_ssh["id"] == "ga-acme"
