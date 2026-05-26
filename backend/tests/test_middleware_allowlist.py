"""OP-1746 (⑦-1bc) — single-source public-path allowlist contract tests.

Covers :mod:`backend.middleware_allowlist` per the Family ⑦ contract
(``docs/sprint-s12/2026-05-16-v2-family7-allowlist-contract.md`` §4):

  * :func:`is_public` exact + prefix membership
  * BOTH-forms resolution (raw path AND API-prefix-stripped form)
  * :func:`is_static_asset` prefix + suffix matching
  * a superset-of-union (no-regression) assertion that imports the five
    *live* sibling allowlists and asserts every entry is still public
    (or, for static assets, still a static asset) — this is the §4.4
    strict-superset guard that fails the moment an entry is dropped.

These are pure-function tests; no FastAPI app is constructed for the
membership checks (the module performs no I/O — §4.5).
"""

from __future__ import annotations

import pytest

from backend.middleware_allowlist import (
    PUBLIC_PATH_ALLOWLIST,
    PUBLIC_PATH_PREFIXES,
    is_public,
    is_static_asset,
)


# ─── is_public: exact-match membership ─────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/livez",
        "/readyz",
        "/healthz",
        "/api/v1/livez",
        "/api/v1/readyz",
        "/api/v1/healthz",
        "/api/v1/health",
        "/metrics",
        "/api/v1/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/api/v1/auth/login",
        "/auth/login",
        "/auth/logout",
        "/auth/change-password",
        "/auth/whoami",
        "/",
        "/version",
        "/api/version",
        "/favicon.ico",
        "/robots.txt",
    ],
)
def test_known_public_path_is_public(path):
    assert is_public(path) is True


# ─── AC: both path forms resolve (contract §4.4 / 4-AC bullet 1) ──


def test_ac_api_prefixed_and_bare_livez_both_public():
    # The headline acceptance criterion: is_public('/api/v1/livez') AND
    # is_public('/livez') are BOTH True.
    assert is_public("/api/v1/livez") is True
    assert is_public("/livez") is True


@pytest.mark.parametrize(
    ("bare", "prefixed"),
    [
        ("/livez", "/api/v1/livez"),
        ("/readyz", "/api/v1/readyz"),
        ("/healthz", "/api/v1/healthz"),
        ("/health", "/api/v1/health"),
        ("/metrics", "/api/v1/metrics"),
        # v2 prefix is stripped by the same normalizer (api_versioning)
        ("/livez", "/api/v2/livez"),
    ],
)
def test_both_forms_resolve_public(bare, prefixed):
    assert is_public(bare) is True
    assert is_public(prefixed) is True


# ─── is_public: prefix-match membership ────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/bootstrap/step-1",
        "/api/v1/bootstrap/finalize",
        "/api/v1/webhooks/github",
        "/api/v1/chatops/webhook/discord",
        "/api/v1/auth/oidc/callback",
        "/api/v1/auth/oauth/authorize",
        "/api/v1/events/stream",
        "/cloudflare/tunnel",
    ],
)
def test_prefix_paths_are_public(path):
    assert is_public(path) is True


def test_bootstrap_prefix_resolves_across_versions():
    # /bootstrap/ is seeded in BOTH raw and API-relative form, so a
    # v2-mounted wizard path strips to /bootstrap/finalize and still
    # matches via the rel form (contract §4.4 "test BOTH forms").
    assert is_public("/api/v2/bootstrap/finalize") is True
    assert is_public("/bootstrap/finalize") is True


def test_versioned_family_root_keeps_historical_scope():
    # The oidc/oauth/webhooks/events prefixes were migrated verbatim as
    # their raw /api/v1/ forms (auth_baseline never allowlisted the v2
    # mount), so v2 stays gated — a superset of, not broader than, the
    # historical union.
    assert is_public("/api/v1/auth/oidc/callback") is True
    assert is_public("/api/v2/auth/oidc/callback") is False


# ─── is_public: negative cases ─────────────────────────────────────


def test_non_public_path_is_not_public():
    assert is_public("/api/v1/users") is False


def test_authed_auth_paths_are_not_public():
    # /auth/logout IS public (clear a compromised session), but a random
    # post-session auth endpoint is not.
    assert is_public("/api/v1/auth/sessions") is False


def test_case_sensitivity():
    assert is_public("/LIVEZ") is False


def test_trailing_slash_not_normalized_for_exact_entries():
    # /livez is an EXACT entry, not a prefix — a trailing slash misses.
    assert is_public("/livez/") is False


def test_exact_entry_does_not_match_as_prefix():
    # The seed deliberately makes /docs and /health exact (not prefixes)
    # to kill the historical /docs -> /docstore accidental startswith
    # match (contract §4.4).
    assert is_public("/docstore") is False
    assert is_public("/healthcheck") is False


# ─── is_static_asset ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/_next/static/chunk.js",
        "/static/app.css",
        "/assets/logo.svg",
        "/public/manifest.png",
        "/app/bundle.js",
        "/styles/main.css",
        "/img/icon.png",
        "/fonts/inter.woff2",
        "/favicon.ico",
    ],
)
def test_static_assets_match(path):
    assert is_static_asset(path) is True


def test_static_asset_negative():
    assert is_static_asset("/api/v1/users") is False
    assert is_static_asset("/livez") is False


def test_static_asset_resolves_api_relative_form():
    assert is_static_asset("/api/v1/_next/static/x.js") is True


# ─── data-shape invariants ─────────────────────────────────────────


def test_allowlist_is_immutable_frozenset():
    assert isinstance(PUBLIC_PATH_ALLOWLIST, frozenset)
    assert isinstance(PUBLIC_PATH_PREFIXES, frozenset)
    with pytest.raises(AttributeError):
        PUBLIC_PATH_ALLOWLIST.add("/pwned")  # type: ignore[attr-defined]


def test_prefix_entries_end_with_slash():
    # Prefix semantics only make sense for "family root" entries.
    for prefix in PUBLIC_PATH_PREFIXES:
        assert prefix.endswith("/"), prefix


# ─── superset-of-union (no-regression) assertion — §4.4 strict-superset


def test_superset_of_live_sibling_union():
    """Every entry in the five LIVE sibling allowlists must still be
    resolved public by the new single source — dropping any one is a
    §4.4 strict-superset violation. Imports the real constants so this
    fails if a sibling list grows an entry the seed forgot to mirror.
    """
    from backend import auth_baseline, main

    # Entries whose membership is answered by is_public().
    public_union: set[str] = set()
    public_union.update(auth_baseline.AUTH_BASELINE_ALLOWLIST)
    public_union.update(main._RATE_LIMIT_EXEMPT)
    public_union.update(main._PASSWORD_CHANGE_EXEMPT)
    public_union.update(main._GRACEFUL_SHUTDOWN_EXEMPT_RAW)
    public_union.update(main._BOOTSTRAP_EXEMPT_REL)
    public_union.update(main._BOOTSTRAP_EXEMPT_REL_PREFIXES)
    public_union.update(main._BOOTSTRAP_EXEMPT_RAW)
    # _bootstrap_path_is_exempt special-cases that are not in any set.
    public_union.update({"/api/version", "/bootstrap"})

    missing = sorted(p for p in public_union if not is_public(p))
    assert not missing, f"union entries no longer public: {missing}"

    # Static-asset prefixes/suffixes are answered by is_static_asset().
    for prefix in main._BOOTSTRAP_EXEMPT_RAW_PREFIXES:
        assert is_static_asset(prefix + "x"), prefix
    for suffix in main._BOOTSTRAP_STATIC_SUFFIXES:
        assert is_static_asset("/asset" + suffix), suffix
