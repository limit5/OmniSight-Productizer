"""S2-9 (#354) — auth-by-default baseline middleware tests.

Covers :mod:`backend.auth_baseline`:

  * pure allowlist matcher semantics (startswith, near-miss, empty)
  * mode env var reading (default, value, normalization)
  * middleware behaviour end-to-end on a minimal FastAPI app
    - OFF mode passes everything
    - LOG mode never blocks (advisory-only)
    - ENFORCE mode allows allowlisted paths, allows OPTIONS,
      rejects unauthenticated non-allowlisted paths with 401
      and a `WWW-Authenticate: Cookie` header
    - ENFORCE mode accepts a request that carries a session cookie
      resolvable by :func:`backend.auth.get_session`
    - ENFORCE mode rejects when session lookup raises (DB blip
      must fail CLOSED, not open)

We build a minimal FastAPI app per test rather than using the shared
`client` fixture so the middleware is the *only* variable — no
bootstrap gate, no CORS, no DB. That keeps the test's surface area
small and the failure messages legible.
"""

from __future__ import annotations

import logging
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import auth_baseline


# ─── pure helpers (no app) ─────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/livez",
        "/readyz",
        "/healthz",
        "/api/v1/livez",
        "/api/v1/auth/login",
        "/api/v1/auth/bootstrap",
        "/api/v1/auth/oidc/callback",       # startswith
        "/api/v1/auth/oauth/google/authorize",  # startswith
        "/api/v1/bootstrap/status",
        "/api/v1/webhooks/github",
        "/api/v1/chatops/webhook/discord",
        "/api/v1/events/stream",
        "/metrics",
        "/docs",
        "/openapi.json",
    ],
)
def test_path_allowed_true(path):
    assert auth_baseline._path_allowed(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/agents",
        "/api/v1/chat",
        "/api/v1/auth/logout",          # logout is deliberately NOT on the list
        "/api/v1/auth/change-password",
        "/api/v1/chatops/mirror",       # only /chatops/webhook/ is allowlisted
        # near-miss: prefix `/metrics` must not swallow `/metricsx`
        # (startswith does match, so verify the lookalike IS caught —
        # this is the intentional lax semantics; documenting it via test.)
    ],
)
def test_path_allowed_false(path):
    assert auth_baseline._path_allowed(path) is False


def test_path_allowed_near_miss_documents_startswith_semantics():
    """`startswith` means `/metricsx` matches `/metrics`. That's the
    chosen semantic — entries in the allowlist are crafted as the
    longest safe deterministic prefix. This test pins the behaviour
    so a future refactor to exact-match doesn't silently change it."""
    assert auth_baseline._path_allowed("/metricsxyz") is True
    # adding a trailing slash in the allowlist where we want strict
    # sub-path scoping (e.g. `/api/v1/bootstrap/`) is how we get
    # exact-ish matching today.
    assert auth_baseline._path_allowed("/api/v1/bootstrapxyz") is False


def test_mode_default_is_log(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_AUTH_BASELINE_MODE", raising=False)
    assert auth_baseline._mode() == "log"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("enforce", "enforce"),
        ("ENFORCE", "enforce"),
        ("  enforce  ", "enforce"),
        ("off", "off"),
        ("", "log"),         # empty string falls through to default
        ("bogus", "bogus"),  # unrecognised → middleware treats as log
    ],
)
def test_mode_normalization(monkeypatch, raw, expected):
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", raw)
    assert auth_baseline._mode() == expected


# ─── middleware end-to-end on a minimal FastAPI app ───────────────


def _make_app() -> FastAPI:
    """Build a tiny FastAPI app with the middleware installed.

    Two endpoints:
      - `/livez` (allowlisted) — 200 with body `ok`
      - `/api/v1/agents` (NOT allowlisted) — 200 with body `secret`

    The middleware is the only thing between request and handler.
    """
    app = FastAPI()

    @app.get("/livez")
    async def _livez():
        return {"status": "ok"}

    @app.get("/api/v1/agents")
    async def _agents():
        return {"data": "secret"}

    @app.post("/api/v1/agents")
    async def _agents_post():
        return {"ok": True}

    auth_baseline.install(app)
    return app


async def _client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_off_mode_passes_non_allowlisted(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", "off")
    app = _make_app()
    async with await _client(app) as c:
        r = await c.get("/api/v1/agents")
    assert r.status_code == 200
    assert r.json() == {"data": "secret"}


@pytest.mark.asyncio
async def test_log_mode_never_blocks(monkeypatch, caplog):
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", "log")
    app = _make_app()
    caplog.set_level(logging.WARNING, logger="backend.auth_baseline")
    async with await _client(app) as c:
        r = await c.get("/api/v1/agents")
    assert r.status_code == 200
    assert "would-block" in caplog.text
    assert "/api/v1/agents" in caplog.text


@pytest.mark.asyncio
async def test_enforce_mode_allows_allowlisted(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", "enforce")
    app = _make_app()
    async with await _client(app) as c:
        r = await c.get("/livez")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_enforce_mode_rejects_non_allowlisted_no_session(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", "enforce")

    # backend.auth may raise on import if the DB isn't set up; make
    # `_has_valid_session` short-circuit to False without touching auth.
    async def _no_session(req):
        return False

    monkeypatch.setattr(auth_baseline, "_has_valid_session", _no_session)

    app = _make_app()
    async with await _client(app) as c:
        r = await c.get("/api/v1/agents")
    assert r.status_code == 401
    assert r.json()["detail"] == "authentication required"
    assert r.json()["path"] == "/api/v1/agents"
    assert r.headers.get("www-authenticate") == "Cookie"


@pytest.mark.asyncio
async def test_enforce_mode_bypasses_options_preflight(monkeypatch):
    """CORS preflight must never be rejected — the browser sends no
    credentials on OPTIONS and the CORS middleware upstream has already
    answered with Allow-Origin / Allow-Credentials."""
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", "enforce")
    app = _make_app()
    async with await _client(app) as c:
        r = await c.options("/api/v1/agents")
    # FastAPI has no OPTIONS handler registered → 405, but the body is
    # Starlette's "Method Not Allowed", NOT our "authentication required".
    # That proves the middleware didn't short-circuit with a 401 first.
    assert r.status_code in (200, 405)
    assert "authentication required" not in r.text


@pytest.mark.asyncio
async def test_enforce_mode_accepts_valid_session(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", "enforce")

    async def _yes_session(req):
        return True

    monkeypatch.setattr(auth_baseline, "_has_valid_session", _yes_session)

    app = _make_app()
    async with await _client(app) as c:
        r = await c.get("/api/v1/agents")
    assert r.status_code == 200
    assert r.json() == {"data": "secret"}


@pytest.mark.asyncio
async def test_enforce_mode_session_lookup_failure_fails_closed(monkeypatch):
    """If session lookup raises (DB blip), enforce mode must still
    reject — not fail-open. The module logs and returns False from
    `_has_valid_session`; the middleware then rejects with 401."""
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", "enforce")

    async def _boom(req):
        # Simulate a DB blip by running the real path with a broken
        # get_session. The real `_has_valid_session` catches and returns
        # False — verify that by simulating the same outcome directly.
        return False

    monkeypatch.setattr(auth_baseline, "_has_valid_session", _boom)

    app = _make_app()
    async with await _client(app) as c:
        r = await c.get(
            "/api/v1/agents",
            headers={"cookie": "omnisight_session=x"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_has_valid_session_catches_get_session_exception(monkeypatch):
    """Drive `_has_valid_session` directly with a patched
    `backend.auth.get_session` that raises — it must return False
    rather than propagating, so the middleware's own branch stays
    in control."""
    from backend import auth as _auth

    async def _exploding_get_session(token):
        raise RuntimeError("db offline")

    monkeypatch.setattr(_auth, "get_session", _exploding_get_session)

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/agents",
        "headers": [(b"cookie", b"omnisight_session=abc")],
    }
    req = Request(scope)
    assert await auth_baseline._has_valid_session(req) is False


@pytest.mark.asyncio
async def test_has_valid_session_retries_once_on_sqlite_lock(monkeypatch):
    """SQLite WAL contention under the Path B dual-replica topology
    briefly raises ``OperationalError: database is locked`` on the
    first session lookup of a dashboard burst. The middleware must
    absorb that single blip by retrying once before failing closed —
    otherwise one unlucky XHR per page-load kicks the operator back
    to /login."""
    from backend import auth as _auth

    calls = {"n": 0}

    async def _flaky(token):
        calls["n"] += 1
        if calls["n"] == 1:
            # First attempt — raise the exact message auth_baseline
            # uses to detect the transient case.
            raise RuntimeError("database is locked")
        # Second attempt — return a real-ish session object so
        # `_has_valid_session` treats it as valid.
        class _S:
            pass
        s = _S()
        return s

    monkeypatch.setattr(_auth, "get_session", _flaky)

    from starlette.requests import Request
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/agents",
        "headers": [(b"cookie", b"omnisight_session=abc")],
    }
    req = Request(scope)
    assert await auth_baseline._has_valid_session(req) is True
    assert calls["n"] == 2  # exactly one retry


@pytest.mark.asyncio
async def test_has_valid_session_fails_closed_after_persistent_lock(monkeypatch):
    """If the lock survives the retry window too, we fail closed so
    a genuinely broken DB doesn't open the gate."""
    from backend import auth as _auth

    calls = {"n": 0}

    async def _always_locked(token):
        calls["n"] += 1
        raise RuntimeError("database is locked")

    monkeypatch.setattr(_auth, "get_session", _always_locked)

    from starlette.requests import Request
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/agents",
        "headers": [(b"cookie", b"omnisight_session=abc")],
    }
    req = Request(scope)
    assert await auth_baseline._has_valid_session(req) is False
    assert calls["n"] == 2  # first attempt + one retry, no third try


@pytest.mark.asyncio
async def test_has_valid_session_no_cookie_returns_false():
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/agents",
        "headers": [],
    }
    req = Request(scope)
    assert await auth_baseline._has_valid_session(req) is False


# ─── Bearer-token recognition (2026-04-24 follow-up) ──────────────
#
# Before this fix, auth_baseline only recognised cookie sessions, so
# API-key-only integrations (operator scripts like prod_smoke_test.py)
# got 401'd at the baseline even though per-handler ``current_user``
# would have accepted the Bearer token. Tests below pin the new
# contract: a valid ``Authorization: Bearer <omni_...>`` header alone
# is enough to pass the baseline.


class _FakeApiKey:
    """Minimal stand-in for ``backend.api_keys.ApiKey`` — avoids
    pulling the full dataclass (and its transitive DB imports) into
    these unit tests."""

    def __init__(self, id_: str = "ak-test", name: str = "test-key"):
        self.id = id_
        self.name = name


def _scope_with_headers(headers: list[tuple[bytes, bytes]]) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/agents",
        "headers": headers,
        "client": ("1.2.3.4", 0),
    }


@pytest.mark.asyncio
async def test_has_valid_session_accepts_valid_bearer(monkeypatch):
    """Valid Bearer token → baseline says authenticated, no cookie
    needed. Caches the ApiKey on request.state.api_key so downstream
    current_user skips re-validation."""
    from backend import api_keys as _api_keys
    from starlette.requests import Request

    captured = {"raw": None, "ip": None}
    fake = _FakeApiKey(id_="ak-smoke", name="smoke-test")

    async def _fake_validate(raw, ip=""):
        captured["raw"] = raw
        captured["ip"] = ip
        return fake

    monkeypatch.setattr(_api_keys, "validate_bearer", _fake_validate)

    scope = _scope_with_headers([(b"authorization", b"Bearer omni_abc123")])
    req = Request(scope)
    assert await auth_baseline._has_valid_session(req) is True
    assert captured["raw"] == "omni_abc123"
    assert captured["ip"] == "1.2.3.4"
    # api_key cached so current_user doesn't re-UPDATE last_used_at.
    assert getattr(req.state, "api_key", None) is fake


@pytest.mark.asyncio
async def test_has_valid_session_rejects_invalid_bearer(monkeypatch):
    from backend import api_keys as _api_keys
    from starlette.requests import Request

    async def _no_match(raw, ip=""):
        return None

    monkeypatch.setattr(_api_keys, "validate_bearer", _no_match)
    monkeypatch.delenv("OMNISIGHT_DECISION_BEARER", raising=False)

    scope = _scope_with_headers([(b"authorization", b"Bearer omni_bogus")])
    req = Request(scope)
    assert await auth_baseline._has_valid_session(req) is False


@pytest.mark.asyncio
async def test_has_valid_session_accepts_legacy_bearer_env(monkeypatch):
    """Pre-K6 single-shared-secret env var still satisfies the baseline
    — mirrors what ``backend.auth._legacy_bearer_matches`` accepts in
    ``current_user`` so the floor and the handler agree."""
    from backend import api_keys as _api_keys
    from starlette.requests import Request

    async def _no_match(raw, ip=""):
        return None

    monkeypatch.setattr(_api_keys, "validate_bearer", _no_match)
    monkeypatch.setenv("OMNISIGHT_DECISION_BEARER", "legacy-secret-42")

    scope = _scope_with_headers(
        [(b"authorization", b"Bearer legacy-secret-42")]
    )
    req = Request(scope)
    assert await auth_baseline._has_valid_session(req) is True


@pytest.mark.asyncio
async def test_has_valid_session_bearer_validation_failure_fails_closed(monkeypatch):
    """If ``validate_bearer`` raises (DB blip), baseline must NOT
    treat the request as authenticated — fail closed, same posture
    as the cookie path's non-lock-exception branch."""
    from backend import api_keys as _api_keys
    from starlette.requests import Request

    async def _boom(raw, ip=""):
        raise RuntimeError("pg pool exhausted")

    monkeypatch.setattr(_api_keys, "validate_bearer", _boom)
    monkeypatch.delenv("OMNISIGHT_DECISION_BEARER", raising=False)

    scope = _scope_with_headers([(b"authorization", b"Bearer omni_x")])
    req = Request(scope)
    assert await auth_baseline._has_valid_session(req) is False


@pytest.mark.asyncio
async def test_has_valid_session_ignores_non_bearer_authorization(monkeypatch):
    """Authorization header that isn't ``Bearer ...`` (e.g. Basic)
    must NOT trigger validate_bearer — avoid DB calls for schemes we
    don't handle."""
    from backend import api_keys as _api_keys
    from starlette.requests import Request

    called = {"n": 0}

    async def _counter(raw, ip=""):
        called["n"] += 1
        return None

    monkeypatch.setattr(_api_keys, "validate_bearer", _counter)

    scope = _scope_with_headers([(b"authorization", b"Basic dXNlcjpwYXNz")])
    req = Request(scope)
    assert await auth_baseline._has_valid_session(req) is False
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_has_valid_session_empty_bearer_token_false(monkeypatch):
    """``Authorization: Bearer `` with no token value must not pass
    and must not hit the DB."""
    from backend import api_keys as _api_keys
    from starlette.requests import Request

    called = {"n": 0}

    async def _counter(raw, ip=""):
        called["n"] += 1
        return None

    monkeypatch.setattr(_api_keys, "validate_bearer", _counter)

    scope = _scope_with_headers([(b"authorization", b"Bearer   ")])
    req = Request(scope)
    assert await auth_baseline._has_valid_session(req) is False
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_enforce_mode_allows_bearer_only_request(monkeypatch):
    """End-to-end: in enforce mode, a Bearer-only request (no cookie)
    against a non-allowlisted path should pass the middleware and
    reach the handler. This is the A2 smoke-test scenario."""
    monkeypatch.setenv("OMNISIGHT_AUTH_BASELINE_MODE", "enforce")

    from backend import api_keys as _api_keys

    async def _accept_any(raw, ip=""):
        return _FakeApiKey(id_="ak-e2e", name="e2e")

    monkeypatch.setattr(_api_keys, "validate_bearer", _accept_any)

    # Isolate the cookie path — don't depend on backend.auth being
    # importable / wired up in this test harness.
    async def _no_cookie_session(req):
        return False

    monkeypatch.setattr(
        auth_baseline, "_has_valid_cookie_session", _no_cookie_session,
    )

    app = _make_app()
    async with await _client(app) as c:
        r = await c.get(
            "/api/v1/agents",
            headers={"authorization": "Bearer omni_smoketest"},
        )
    assert r.status_code == 200
    assert r.json() == {"data": "secret"}


# ─── allowlist integrity ────────────────────────────────────────────


def test_allowlist_is_a_tuple_of_strings():
    """Prevent accidental mutation: the allowlist ships as a tuple.
    Adding new entries requires editing the source file (which is
    code-reviewed)."""
    assert isinstance(auth_baseline.AUTH_BASELINE_ALLOWLIST, tuple)
    for p in auth_baseline.AUTH_BASELINE_ALLOWLIST:
        assert isinstance(p, str)
        assert p.startswith("/"), f"allowlist entry must be a path: {p}"


def test_allowlist_has_no_empty_or_root_entries():
    """An empty string or `/` would allowlist everything — a security
    regression nightmare. Pin that it never appears."""
    assert "" not in auth_baseline.AUTH_BASELINE_ALLOWLIST
    assert "/" not in auth_baseline.AUTH_BASELINE_ALLOWLIST


def test_install_rejects_non_fastapi_app():
    class NotApp:
        pass

    with pytest.raises(AssertionError):
        auth_baseline.install(NotApp())  # type: ignore[arg-type]
