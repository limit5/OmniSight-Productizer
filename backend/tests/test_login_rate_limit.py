"""Internet-exposure auth S5 — brute-force defence + login audit.

Unit-tests the rate-limit helpers directly (no FastAPI machinery),
plus one integration test through the real router to confirm the
rate limit is actually wired to the endpoint.
"""

from __future__ import annotations

import time

import pytest
from starlette.requests import Request

from backend.rate_limit import reset_limiters
from backend.routers import auth as auth_router


def _fake_request(ip: str = "1.2.3.4", cf_ip: str | None = None) -> Request:
    """Build a minimal ASGI scope — enough for the helpers, which only
    read `request.client.host` and `request.headers`."""
    headers: list[tuple[bytes, bytes]] = []
    if cf_ip:
        headers.append((b"cf-connecting-ip", cf_ip.encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "client": (ip, 0),
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _reset_window():
    auth_router._LOGIN_ATTEMPTS.clear()
    reset_limiters()
    yield
    auth_router._LOGIN_ATTEMPTS.clear()
    reset_limiters()


def test_client_key_prefers_cf_connecting_ip():
    """Real client IP must come from the CF header when present,
    otherwise every request behind the tunnel shares the same limit."""
    r = _fake_request(ip="10.0.0.1", cf_ip="203.0.113.42")
    assert auth_router._client_key(r) == "203.0.113.42"


def test_client_key_falls_back_to_peer_ip():
    r = _fake_request(ip="10.0.0.1")
    assert auth_router._client_key(r) == "10.0.0.1"


def test_rate_limit_permits_up_to_cap():
    """Cap fails should all pass, nth+1 attempt blocks."""
    r = _fake_request()
    for _ in range(auth_router._login_max_attempts()):
        auth_router._check_login_rate_limit(r)
        auth_router._record_failed_login(r)
    with pytest.raises(Exception) as excinfo:
        auth_router._check_login_rate_limit(r)
    assert getattr(excinfo.value, "status_code", None) == 429
    assert "Retry-After" in (getattr(excinfo.value, "headers", {}) or {})


def test_rate_limit_per_ip_independent():
    """A blocked IP must not leak into another IP's bucket."""
    a = _fake_request(cf_ip="203.0.113.1")
    b = _fake_request(cf_ip="203.0.113.2")
    for _ in range(auth_router._login_max_attempts()):
        auth_router._record_failed_login(a)
    with pytest.raises(Exception):
        auth_router._check_login_rate_limit(a)
    # b should pass unimpeded.
    auth_router._check_login_rate_limit(b)


def test_rate_limit_ages_out(monkeypatch):
    """An attempt older than OMNISIGHT_LOGIN_WINDOW_S must stop counting."""
    monkeypatch.setenv("OMNISIGHT_LOGIN_WINDOW_S", "60")
    r = _fake_request()
    bucket = auth_router._LOGIN_ATTEMPTS[auth_router._client_key(r)]
    # Manually plant cap entries, all 120s ago.
    old = time.time() - 120
    for _ in range(auth_router._login_max_attempts()):
        bucket.append(old)
    # Should prune on next check and allow through.
    auth_router._check_login_rate_limit(r)
    assert len(bucket) == 0


def test_max_keys_bounded():
    """A parade of unique IPs going through the real
    check→auth→record flow must keep the bucket dict bounded.

    Shape mirrors production: _check_login_rate_limit fires first on
    every request (it evicts when the new IP would overflow the cap),
    _record_failed_login only runs after the auth attempt 401s.
    """
    original = auth_router._LOGIN_ATTEMPTS_MAX_KEYS
    try:
        auth_router._LOGIN_ATTEMPTS_MAX_KEYS = 8
        for i in range(40):
            r = _fake_request(cf_ip=f"10.0.0.{i}")
            auth_router._check_login_rate_limit(r)
            auth_router._record_failed_login(r)
        # Eviction caps at MAX_KEYS; the one extra slot may open
        # between the eviction and the record, so +1 is the ceiling.
        assert len(auth_router._LOGIN_ATTEMPTS) <= auth_router._LOGIN_ATTEMPTS_MAX_KEYS + 1
    finally:
        auth_router._LOGIN_ATTEMPTS_MAX_KEYS = original


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Integration: through the live FastAPI router
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_login_failure_hits_rate_limit(client):
    """Sixth bad login in a row must 429 even if the password were
    correct. Same-IP caller (client fixture)."""
    max_attempts = auth_router._login_max_attempts()
    for i in range(max_attempts):
        r = await client.post(
            "/api/v1/auth/login",
            json={"email": f"nobody-{i}@example.com", "password": "wrong"},
        )
        assert r.status_code == 401, f"attempt {i + 1} unexpected: {r.status_code}"
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "wrong"},
    )
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}


@pytest.mark.asyncio
async def test_login_failure_writes_audit_row(client):
    """A failed login must emit a login_failed audit row with a
    masked email — full email would defeat the "can't tell which
    accounts exist" property of the masking."""
    from backend.db_pool import get_pool

    await client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@example.com", "password": "whatever"},
    )

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT action, entity_id FROM audit_log "
            "WHERE action = 'auth.login.fail' "
            "ORDER BY id DESC LIMIT 1"
        )
    assert row is not None
    # Email must be masked: first 2 chars, '***', then the domain.
    entity_id = row["entity_id"]
    assert entity_id.startswith("gh***"), entity_id
    assert "@example.com" in entity_id
    assert "ghost@example.com" != entity_id  # actually masked, not raw
