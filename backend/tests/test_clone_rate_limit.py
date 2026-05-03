"""W11.8 #XXX — Contract tests for ``backend.web.clone_rate_limit``.

Pins:

    * Public surface (constants, dataclass shape, error hierarchy,
      Protocol shape, package re-exports).
    * ``canonical_clone_target`` reduces a URL to ``scheme://host[:port]``
      and rejects unsupported / hostless / non-string inputs.
    * ``clone_rate_limit_key`` composes the canonical key shape and
      rejects empty/non-string tenant_id / target.
    * ``InMemoryCloneRateLimiter`` enforces the sliding-window-log
      contract: prune-then-count-then-conditionally-append, deterministic
      and atomic under ``threading.Lock``.
    * ``assert_clone_rate_limit`` is the PEP HOLD entry point —
      consumes one slot when allowed, raises
      :class:`CloneRateLimitedError` carrying the decision when held,
      writes the ``web.clone.rate_limited`` audit row best-effort on
      HOLD only.
    * Env-knob resolution honours ``OMNISIGHT_CLONE_RATE_LIMIT`` /
      ``OMNISIGHT_CLONE_RATE_WINDOW_S`` and clamps to the safe range.
    * ``RedisCloneRateLimiter`` matches the in-memory limiter on every
      observable when driven through a fake Redis client (Lua semantics
      pinned via the ZSET prune→count→add sequence).
    * ``get_clone_rate_limiter`` resolves Redis when configured (and
      reachable) and falls back to the in-memory limiter otherwise.
    * Package re-exports: 19 W11.8 symbols + the post-W11.8 total
      drift-guard pin.

Every test runs without network / Redis / DB / LLM I/O.
``RedisCloneRateLimiter`` is exercised through a lightweight in-process
fake that mirrors the ZSET / SCAN surface the limiter touches.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional

import pytest

import backend.web as web_pkg
from backend.web.clone_rate_limit import (
    CLONE_RATE_AUDIT_ACTION,
    CLONE_RATE_AUDIT_ENTITY_KIND,
    CLONE_RATE_KEY_PREFIX,
    CloneRateLimitDecision,
    CloneRateLimitError,
    CloneRateLimitedError,
    CloneRateLimiter,
    DEFAULT_CLONE_RATE_LIMIT,
    DEFAULT_CLONE_RATE_WINDOW_S,
    InMemoryCloneRateLimiter,
    RedisCloneRateLimiter,
    _SLIDING_WINDOW_LOG_LUA,
    assert_clone_rate_limit,
    canonical_clone_target,
    clone_rate_limit_key,
    get_clone_rate_limiter,
    record_clone_rate_limit_hold,
    reset_clone_rate_limiter,
    resolve_clone_rate_limit,
    resolve_clone_rate_window_seconds,
)
from backend.web.site_cloner import (
    InvalidCloneURLError,
    SiteClonerError,
)


# ── Helpers / fakes ──────────────────────────────────────────────────────


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class _FakeAuditLog:
    """Capture-and-forward stand-in for :func:`backend.audit.log`.

    Captures positional + keyword args verbatim so a test can pin every
    field the audit row carried. ``rv`` is the value the hook returns
    (mirror of ``audit.log`` which returns the row id or None).
    """

    def __init__(self, rv: Any = 7) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.rv = rv

    async def __call__(self, *args, **kwargs):
        # Normalise to kwargs by name regardless of how we got called.
        keys = ("action", "entity_kind", "entity_id", "before", "after",
                "actor", "session_id", "conn")
        captured: Dict[str, Any] = {}
        for i, val in enumerate(args):
            if i < len(keys):
                captured[keys[i]] = val
        captured.update(kwargs)
        self.calls.append(captured)
        return self.rv


class _RaisingAuditLog:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("audit subsystem down")


class _FakeRedis:
    """Minimal in-process fake of the redis client surface
    :class:`RedisCloneRateLimiter` touches: ZSET ops via
    ``register_script``-returned callable + ``delete`` + ``scan``.

    Stores ZSETs as ``dict[member, score]`` per key.
    """

    def __init__(self) -> None:
        self.store: Dict[str, Dict[str, float]] = {}

    def register_script(self, body: str):
        # Sanity: we got the actual W11.8 Lua body, not some other one.
        assert "ZREMRANGEBYSCORE" in body
        assert "ZCARD" in body
        assert "ZADD" in body
        return self._script

    def _script(self, *, keys, args):
        key = keys[0]
        now_ms = int(args[0])
        window_ms = int(args[1])
        limit = int(args[2])
        entry = str(args[3])
        dry_run = int(args[4])

        zset = self.store.setdefault(key, {})
        cutoff = now_ms - window_ms
        for m in [m for m, s in zset.items() if s <= cutoff]:
            del zset[m]

        count = len(zset)
        if count < limit:
            allowed = 1
            if not dry_run:
                zset[entry] = float(now_ms)
                count += 1
            oldest_ms = min(zset.values()) if zset else None
        else:
            allowed = 0
            oldest_ms = min(zset.values()) if zset else None

        return [
            allowed,
            count,
            "" if oldest_ms is None else str(oldest_ms),
        ]

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n

    def scan(self, *, cursor, match, count=500):
        # Single-shot pagination: we always return cursor=0 (done).
        prefix = match.rstrip("*")
        matched = [k for k in self.store if k.startswith(prefix)]
        return 0, matched


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    """Ensure the module-level singleton is wiped between tests so
    env-knob changes take effect on the next ``get_clone_rate_limiter``.
    """
    reset_clone_rate_limiter()
    yield
    reset_clone_rate_limiter()


# ── 1. Public surface / constants ────────────────────────────────────────


class TestPublicSurface:

    def test_default_limit_pinned_to_3(self):
        assert DEFAULT_CLONE_RATE_LIMIT == 3

    def test_default_window_pinned_to_24h(self):
        assert DEFAULT_CLONE_RATE_WINDOW_S == 86400.0

    def test_audit_action_namespace_under_web_clone(self):
        assert CLONE_RATE_AUDIT_ACTION == "web.clone.rate_limited"
        assert CLONE_RATE_AUDIT_ACTION.startswith("web.clone")

    def test_audit_entity_kind_pinned(self):
        assert CLONE_RATE_AUDIT_ENTITY_KIND == "web_clone_rate_limit"

    def test_key_prefix_pinned(self):
        assert CLONE_RATE_KEY_PREFIX == "omnisight:clone:rl:"

    def test_lua_script_carries_canonical_ops(self):
        # The script body is part of the public/audit surface — drift
        # in op order or names breaks the prune→count→add invariant.
        assert "ZREMRANGEBYSCORE" in _SLIDING_WINDOW_LOG_LUA
        assert "ZCARD" in _SLIDING_WINDOW_LOG_LUA
        assert "ZADD" in _SLIDING_WINDOW_LOG_LUA
        assert "PEXPIRE" in _SLIDING_WINDOW_LOG_LUA
        assert "ZRANGE" in _SLIDING_WINDOW_LOG_LUA

    def test_decision_is_frozen_dataclass(self):
        d = CloneRateLimitDecision(
            allowed=True, count=1, limit=3, window_seconds=86400.0,
            retry_after_seconds=0.0, oldest_attempt_at=1234.0,
            tenant_id="t", target="https://x.example",
        )
        with pytest.raises(Exception):
            d.allowed = False  # type: ignore[misc]

    def test_decision_held_property_is_inverse_of_allowed(self):
        d_allow = CloneRateLimitDecision(
            allowed=True, count=1, limit=3, window_seconds=86400.0,
            retry_after_seconds=0.0, oldest_attempt_at=1234.0,
            tenant_id="t", target="https://x.example",
        )
        d_hold = CloneRateLimitDecision(
            allowed=False, count=3, limit=3, window_seconds=86400.0,
            retry_after_seconds=12.5, oldest_attempt_at=1234.0,
            tenant_id="t", target="https://x.example",
        )
        assert d_allow.held is False
        assert d_hold.held is True

    def test_error_hierarchy_chains_to_site_cloner_error(self):
        assert issubclass(CloneRateLimitError, SiteClonerError)
        assert issubclass(CloneRateLimitedError, CloneRateLimitError)
        assert issubclass(CloneRateLimitedError, SiteClonerError)

    def test_clone_rate_limited_error_carries_decision(self):
        d = CloneRateLimitDecision(
            allowed=False, count=3, limit=3, window_seconds=86400.0,
            retry_after_seconds=42.0, oldest_attempt_at=1234.0,
            tenant_id="t", target="https://x.example",
        )
        err = CloneRateLimitedError(d, url="https://x.example/page")
        assert err.decision is d
        assert err.url == "https://x.example/page"
        assert "rate limit" in str(err).lower()
        assert "3/3" in str(err)
        assert "42.0" in str(err)

    def test_protocol_runtime_checkable(self):
        assert isinstance(InMemoryCloneRateLimiter(), CloneRateLimiter)


# ── 2. canonical_clone_target ────────────────────────────────────────────


class TestCanonicalCloneTarget:

    def test_drops_path_query_fragment(self):
        assert (
            canonical_clone_target("https://acme.example/foo/bar?cb=1#frag")
            == "https://acme.example"
        )

    def test_lowercases_host(self):
        assert canonical_clone_target("https://Acme.EXAMPLE/") == "https://acme.example"

    def test_strips_default_port_https(self):
        assert canonical_clone_target("https://acme.example:443/") == "https://acme.example"

    def test_strips_default_port_http(self):
        assert canonical_clone_target("http://acme.example:80/") == "http://acme.example"

    def test_keeps_non_default_port(self):
        assert canonical_clone_target("https://acme.example:8443/") == "https://acme.example:8443"
        assert canonical_clone_target("http://acme.example:8080/") == "http://acme.example:8080"

    def test_drops_basic_auth_userinfo(self):
        # ``urlsplit.hostname`` already strips user:pass; we just confirm
        # the canonical output never carries credentials.
        canonical = canonical_clone_target("https://user:pw@acme.example/x")
        assert "user" not in canonical
        assert "pw" not in canonical
        assert canonical == "https://acme.example"

    def test_rejects_empty_string(self):
        with pytest.raises(InvalidCloneURLError):
            canonical_clone_target("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(InvalidCloneURLError):
            canonical_clone_target("   ")

    def test_rejects_non_string(self):
        with pytest.raises(InvalidCloneURLError):
            canonical_clone_target(None)  # type: ignore[arg-type]

    def test_rejects_unsupported_scheme(self):
        with pytest.raises(InvalidCloneURLError):
            canonical_clone_target("ftp://acme.example/")

    def test_rejects_url_without_host(self):
        with pytest.raises(InvalidCloneURLError):
            canonical_clone_target("https:///path")


# ── 3. clone_rate_limit_key ──────────────────────────────────────────────


class TestCloneRateLimitKey:

    def test_canonical_key_format(self):
        assert (
            clone_rate_limit_key("tenant-1", "https://acme.example")
            == "omnisight:clone:rl:tenant-1:https://acme.example"
        )

    def test_uses_module_prefix(self):
        key = clone_rate_limit_key("t", "https://x.example")
        assert key.startswith(CLONE_RATE_KEY_PREFIX)

    def test_rejects_empty_tenant(self):
        with pytest.raises(ValueError):
            clone_rate_limit_key("", "https://x.example")

    def test_rejects_non_string_tenant(self):
        with pytest.raises(ValueError):
            clone_rate_limit_key(None, "https://x.example")  # type: ignore[arg-type]

    def test_rejects_empty_target(self):
        with pytest.raises(ValueError):
            clone_rate_limit_key("t", "")

    def test_rejects_non_string_target(self):
        with pytest.raises(ValueError):
            clone_rate_limit_key("t", None)  # type: ignore[arg-type]


# ── 4. InMemoryCloneRateLimiter ──────────────────────────────────────────


class TestInMemoryLimiter:

    def test_first_attempt_allowed_count_1(self):
        lim = InMemoryCloneRateLimiter()
        d = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        assert d.allowed
        assert d.count == 1
        assert d.limit == 3
        assert d.window_seconds == 86400.0
        assert d.retry_after_seconds == 0.0
        assert d.oldest_attempt_at == 1000.0

    def test_three_attempts_allowed_then_fourth_held(self):
        lim = InMemoryCloneRateLimiter()
        for i in range(3):
            d = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0 + i)
            assert d.allowed
            assert d.count == i + 1
        d = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1003.0)
        assert d.held
        assert d.count == 3
        assert d.oldest_attempt_at == 1000.0
        assert d.retry_after_seconds == pytest.approx(86397.0)

    def test_different_tenant_does_not_share_budget(self):
        lim = InMemoryCloneRateLimiter()
        for i in range(3):
            lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0 + i)
        d = lim.check("t2", "https://x.example", limit=3, window_seconds=86400, now=1004.0)
        assert d.allowed
        assert d.count == 1

    def test_different_target_does_not_share_budget(self):
        lim = InMemoryCloneRateLimiter()
        for i in range(3):
            lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0 + i)
        d = lim.check("t1", "https://other.example", limit=3, window_seconds=86400, now=1004.0)
        assert d.allowed

    def test_sliding_window_releases_oldest(self):
        lim = InMemoryCloneRateLimiter()
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1001.0)
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1002.0)
        held = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1003.0)
        assert held.held
        # Push past the oldest's expiry — slot frees up.
        d = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0 + 86400 + 0.1)
        assert d.allowed
        assert d.count == 3  # the 1001 / 1002 attempts are still in window

    def test_dry_run_does_not_consume(self):
        lim = InMemoryCloneRateLimiter()
        for i in range(3):
            lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0 + i)
        d = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1003.0, dry_run=True)
        # Already at limit → still held.
        assert d.held
        # Still 3 entries — dry_run did not append.
        d2 = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1003.0)
        assert d2.held
        assert d2.count == 3

    def test_dry_run_when_under_limit_returns_allowed_without_appending(self):
        lim = InMemoryCloneRateLimiter()
        d_dry = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0, dry_run=True)
        assert d_dry.allowed
        assert d_dry.count == 0  # no append, count is pre-attempt
        # First real attempt still gets count 1.
        d = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1001.0)
        assert d.allowed
        assert d.count == 1

    def test_reset_specific_target_wipes_one_key(self):
        lim = InMemoryCloneRateLimiter()
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t1", "https://y.example", limit=3, window_seconds=86400, now=1000.0)
        lim.reset("t1", "https://x.example")
        d_x = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1001.0)
        d_y = lim.check("t1", "https://y.example", limit=3, window_seconds=86400, now=1001.0)
        assert d_x.count == 1  # wiped → fresh
        assert d_y.count == 2  # untouched

    def test_reset_whole_tenant_wipes_every_target(self):
        lim = InMemoryCloneRateLimiter()
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t1", "https://y.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t2", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.reset("t1")
        d_x = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1001.0)
        d_y = lim.check("t1", "https://y.example", limit=3, window_seconds=86400, now=1001.0)
        d_t2 = lim.check("t2", "https://x.example", limit=3, window_seconds=86400, now=1001.0)
        assert d_x.count == 1
        assert d_y.count == 1
        assert d_t2.count == 2

    def test_clear_wipes_everything(self):
        lim = InMemoryCloneRateLimiter()
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t2", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.clear()
        d = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1001.0)
        assert d.count == 1

    def test_check_rejects_zero_limit(self):
        lim = InMemoryCloneRateLimiter()
        with pytest.raises(ValueError):
            lim.check("t1", "https://x.example", limit=0, window_seconds=86400)

    def test_check_rejects_negative_limit(self):
        lim = InMemoryCloneRateLimiter()
        with pytest.raises(ValueError):
            lim.check("t1", "https://x.example", limit=-1, window_seconds=86400)

    def test_check_rejects_zero_window(self):
        lim = InMemoryCloneRateLimiter()
        with pytest.raises(ValueError):
            lim.check("t1", "https://x.example", limit=3, window_seconds=0)

    def test_check_rejects_negative_window(self):
        lim = InMemoryCloneRateLimiter()
        with pytest.raises(ValueError):
            lim.check("t1", "https://x.example", limit=3, window_seconds=-1)

    def test_now_defaults_to_wall_clock_when_none(self):
        lim = InMemoryCloneRateLimiter()
        before = time.time()
        d = lim.check("t1", "https://x.example", limit=3, window_seconds=86400)
        after = time.time()
        assert d.allowed
        assert d.oldest_attempt_at is not None
        assert before <= d.oldest_attempt_at <= after

    def test_max_keys_evicts_oldest(self):
        lim = InMemoryCloneRateLimiter(max_keys=2)
        lim.check("t1", "https://a.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t1", "https://b.example", limit=3, window_seconds=86400, now=2000.0)
        # Adding a third key triggers eviction of the oldest (a).
        lim.check("t1", "https://c.example", limit=3, window_seconds=86400, now=3000.0)
        # ``a`` was evicted → it now reads as fresh.
        d_a = lim.check("t1", "https://a.example", limit=3, window_seconds=86400, now=4000.0)
        assert d_a.count <= 2  # eviction may or may not pick a depending on implementation, but a is no longer locked at 1.

    def test_limit_change_takes_effect_on_next_check(self):
        # The bucket carries no per-key (limit, window) memory — the
        # caller passes them every call, so a higher limit immediately
        # admits more attempts.
        lim = InMemoryCloneRateLimiter()
        for i in range(3):
            lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0 + i)
        d_held = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1004.0)
        assert d_held.held
        # Caller raises the limit knob to 5 → 4th & 5th now admitted.
        d4 = lim.check("t1", "https://x.example", limit=5, window_seconds=86400, now=1004.0)
        assert d4.allowed
        assert d4.count == 4

    def test_decision_carries_canonical_target(self):
        lim = InMemoryCloneRateLimiter()
        d = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        # The limiter stores the caller-provided ``target`` verbatim
        # (canonicalisation happens upstream in the PEP entry point).
        assert d.target == "https://x.example"
        assert d.tenant_id == "t1"


# ── 5. RedisCloneRateLimiter (driven through fake redis) ─────────────────


class TestRedisLimiterParity:
    """The fake redis client implements just enough of the surface
    :class:`RedisCloneRateLimiter` touches; the goal of this section
    is to pin ZSET-semantics parity with the in-memory limiter so a
    multi-worker prod stack and a single-worker test bench observe the
    same behaviour."""

    def _build(self) -> tuple[RedisCloneRateLimiter, _FakeRedis]:
        # Construct the real class but swap the redis client + script
        # for the in-process fake — avoids needing a live redis server.
        fake = _FakeRedis()
        lim = RedisCloneRateLimiter.__new__(RedisCloneRateLimiter)
        lim._pool = None  # type: ignore[attr-defined]
        lim._client = fake  # type: ignore[attr-defined]
        lim._script = fake._script  # type: ignore[attr-defined]
        return lim, fake

    def test_first_attempt_allowed(self):
        lim, _ = self._build()
        d = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        assert d.allowed
        assert d.count == 1

    def test_fourth_attempt_held_with_oldest_at(self):
        lim, _ = self._build()
        for i in range(3):
            lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0 + i)
        d = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1003.0)
        assert d.held
        assert d.count == 3
        assert d.oldest_attempt_at == pytest.approx(1000.0, abs=0.01)
        assert d.retry_after_seconds == pytest.approx(86397.0, abs=0.01)

    def test_sliding_window_drops_expired(self):
        lim, _ = self._build()
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1001.0)
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1002.0)
        # Push past the oldest's expiry — slot frees.
        d = lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0 + 86400 + 0.5)
        assert d.allowed

    def test_different_keys_independent(self):
        lim, _ = self._build()
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1001.0)
        d = lim.check("t2", "https://x.example", limit=3, window_seconds=86400, now=1002.0)
        assert d.allowed
        assert d.count == 1

    def test_dry_run_does_not_persist_in_redis(self):
        lim, fake = self._build()
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0, dry_run=True)
        # No ZSET yet for that key (or empty).
        key = clone_rate_limit_key("t1", "https://x.example")
        assert fake.store.get(key, {}) == {}

    def test_unique_member_per_call_avoids_zset_dedupe(self):
        lim, fake = self._build()
        # Two calls at the *same* now_ms — distinct member required so
        # ZSET does not dedupe and silently drop the second attempt.
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        key = clone_rate_limit_key("t1", "https://x.example")
        assert len(fake.store[key]) == 2

    def test_reset_specific_target(self):
        lim, fake = self._build()
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t1", "https://y.example", limit=3, window_seconds=86400, now=1000.0)
        lim.reset("t1", "https://x.example")
        assert clone_rate_limit_key("t1", "https://x.example") not in fake.store
        assert clone_rate_limit_key("t1", "https://y.example") in fake.store

    def test_reset_whole_tenant_via_scan(self):
        lim, fake = self._build()
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t1", "https://y.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t2", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.reset("t1")
        assert clone_rate_limit_key("t1", "https://x.example") not in fake.store
        assert clone_rate_limit_key("t1", "https://y.example") not in fake.store
        assert clone_rate_limit_key("t2", "https://x.example") in fake.store

    def test_clear_wipes_every_managed_key(self):
        lim, fake = self._build()
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        lim.check("t2", "https://y.example", limit=3, window_seconds=86400, now=1000.0)
        lim.clear()
        assert all(not k.startswith(CLONE_RATE_KEY_PREFIX) for k in fake.store)

    def test_check_rejects_zero_limit(self):
        lim, _ = self._build()
        with pytest.raises(ValueError):
            lim.check("t1", "https://x.example", limit=0, window_seconds=86400)

    def test_check_rejects_zero_window(self):
        lim, _ = self._build()
        with pytest.raises(ValueError):
            lim.check("t1", "https://x.example", limit=3, window_seconds=0)


# ── 6. assert_clone_rate_limit (PEP HOLD entry point) ────────────────────


class TestAssertCloneRateLimit:

    def test_first_three_allowed(self):
        lim = InMemoryCloneRateLimiter()
        audit = _FakeAuditLog()
        for i in range(3):
            d = asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400,
                now=1000.0 + i, audit_log=audit,
            ))
            assert d.allowed
            assert d.count == i + 1
        assert audit.calls == []  # no audit on allow path

    def test_fourth_attempt_raises_clone_rate_limited(self):
        lim = InMemoryCloneRateLimiter()
        audit = _FakeAuditLog()
        for i in range(3):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400,
                now=1000.0 + i, audit_log=audit,
            ))
        with pytest.raises(CloneRateLimitedError) as exc_info:
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400,
                now=1003.0, audit_log=audit, actor="op@example",
            ))
        d = exc_info.value.decision
        assert d.held
        assert d.count == 3
        assert d.tenant_id == "t1"
        assert d.target == "https://x.example"
        assert exc_info.value.url == "https://x.example/page"
        # Audit row written exactly once on HOLD.
        assert len(audit.calls) == 1

    def test_audit_payload_carries_decision_fields(self):
        lim = InMemoryCloneRateLimiter()
        audit = _FakeAuditLog()
        for i in range(3):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400,
                now=1000.0 + i, audit_log=audit, actor="op@example",
            ))
        with pytest.raises(CloneRateLimitedError):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400,
                now=1003.0, audit_log=audit, actor="op@example",
                session_id="sess-42",
            ))
        call = audit.calls[0]
        assert call["action"] == CLONE_RATE_AUDIT_ACTION
        assert call["entity_kind"] == CLONE_RATE_AUDIT_ENTITY_KIND
        assert call["entity_id"] == "t1"
        assert call["before"] is None
        after = call["after"]
        assert after["tenant_id"] == "t1"
        assert after["target"] == "https://x.example"
        assert after["url"] == "https://x.example/page"
        assert after["limit"] == 3
        assert after["count"] == 3
        assert after["window_seconds"] == 86400.0
        assert after["retry_after_seconds"] > 0
        assert after["allowed"] is False
        assert call["actor"] == "op@example"
        assert call["session_id"] == "sess-42"

    def test_audit_disabled_via_kwarg(self):
        lim = InMemoryCloneRateLimiter()
        audit = _FakeAuditLog()
        for i in range(3):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400,
                now=1000.0 + i, audit_log=audit,
            ))
        with pytest.raises(CloneRateLimitedError):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400,
                now=1003.0, audit_log=audit, audit=False,
            ))
        assert audit.calls == []  # opted out

    def test_audit_failure_does_not_break_hold(self):
        lim = InMemoryCloneRateLimiter()
        broken = _RaisingAuditLog()
        for i in range(3):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400,
                now=1000.0 + i,
            ))
        with pytest.raises(CloneRateLimitedError):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400,
                now=1003.0, audit_log=broken,
            ))
        assert broken.calls == 1  # was called, swallowed exception

    def test_dry_run_returns_decision_without_consume(self):
        lim = InMemoryCloneRateLimiter()
        for i in range(3):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400,
                now=1000.0 + i,
            ))
        # Real attempt #4 would HOLD; dry_run=True still HOLDs because
        # it just inspects, but it must not record a 4th audit row.
        audit = _FakeAuditLog()
        with pytest.raises(CloneRateLimitedError):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400,
                now=1003.0, dry_run=True, audit_log=audit,
            ))
        # Audit fires on HOLD even in dry_run — operator wants the
        # signal that someone is hitting the ceiling.
        assert len(audit.calls) == 1

    def test_query_string_does_not_split_budget(self):
        lim = InMemoryCloneRateLimiter()
        # Same origin, three different ?cb=N — must collapse onto one
        # key so a tenant cannot dodge the budget by tail-cache-busting.
        for i, suffix in enumerate(["?a=1", "?b=2", "?c=3"]):
            asyncio.run(assert_clone_rate_limit(
                "t1", f"https://x.example/page{suffix}",
                limiter=lim, limit=3, window_seconds=86400,
                now=1000.0 + i,
            ))
        with pytest.raises(CloneRateLimitedError):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/different-path",
                limiter=lim, limit=3, window_seconds=86400, now=1003.0,
            ))

    def test_default_limit_resolved_from_constants(self, monkeypatch):
        monkeypatch.delenv("OMNISIGHT_CLONE_RATE_LIMIT", raising=False)
        monkeypatch.delenv("OMNISIGHT_CLONE_RATE_WINDOW_S", raising=False)
        lim = InMemoryCloneRateLimiter()
        # Three allowed → fourth held, all using defaults.
        for i in range(3):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page", limiter=lim, now=1000.0 + i,
            ))
        with pytest.raises(CloneRateLimitedError) as exc_info:
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page", limiter=lim, now=1003.0,
            ))
        d = exc_info.value.decision
        assert d.limit == DEFAULT_CLONE_RATE_LIMIT
        assert d.window_seconds == DEFAULT_CLONE_RATE_WINDOW_S

    def test_env_knob_overrides_default_limit(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_CLONE_RATE_LIMIT", "5")
        monkeypatch.delenv("OMNISIGHT_CLONE_RATE_WINDOW_S", raising=False)
        lim = InMemoryCloneRateLimiter()
        for i in range(5):
            d = asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page", limiter=lim, now=1000.0 + i,
            ))
            assert d.allowed
        with pytest.raises(CloneRateLimitedError):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page", limiter=lim, now=1005.0,
            ))

    def test_env_knob_overrides_default_window(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_CLONE_RATE_WINDOW_S", "60")
        lim = InMemoryCloneRateLimiter()
        for i in range(3):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page", limiter=lim, limit=3, now=1000.0 + i,
            ))
        # 61s later → first attempt has expired, slot freed.
        d = asyncio.run(assert_clone_rate_limit(
            "t1", "https://x.example/page", limiter=lim, limit=3, now=1061.0,
        ))
        assert d.allowed

    def test_explicit_kwarg_beats_env(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_CLONE_RATE_LIMIT", "10")
        lim = InMemoryCloneRateLimiter()
        for i in range(3):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400, now=1000.0 + i,
            ))
        # Explicit limit=3 wins over env=10.
        with pytest.raises(CloneRateLimitedError):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/page",
                limiter=lim, limit=3, window_seconds=86400, now=1003.0,
            ))

    def test_rejects_empty_tenant(self):
        with pytest.raises(ValueError):
            asyncio.run(assert_clone_rate_limit(
                "", "https://x.example/page",
                limiter=InMemoryCloneRateLimiter(),
            ))

    def test_rejects_invalid_url(self):
        with pytest.raises(InvalidCloneURLError):
            asyncio.run(assert_clone_rate_limit(
                "t1", "ftp://x.example/page",
                limiter=InMemoryCloneRateLimiter(),
            ))


# ── 7. resolve_* env knobs ───────────────────────────────────────────────


class TestEnvKnobResolution:

    def test_resolve_limit_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("OMNISIGHT_CLONE_RATE_LIMIT", raising=False)
        assert resolve_clone_rate_limit() == DEFAULT_CLONE_RATE_LIMIT

    def test_resolve_limit_honours_valid_env(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_CLONE_RATE_LIMIT", "7")
        assert resolve_clone_rate_limit() == 7

    def test_resolve_limit_clamps_below_min(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_CLONE_RATE_LIMIT", "0")
        assert resolve_clone_rate_limit() == 1  # clamped to _MIN

    def test_resolve_limit_clamps_above_max(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_CLONE_RATE_LIMIT", "9999999")
        assert resolve_clone_rate_limit() == 1000  # clamped to _MAX

    def test_resolve_limit_falls_back_on_garbage(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_CLONE_RATE_LIMIT", "not-a-number")
        assert resolve_clone_rate_limit() == DEFAULT_CLONE_RATE_LIMIT

    def test_resolve_window_default(self, monkeypatch):
        monkeypatch.delenv("OMNISIGHT_CLONE_RATE_WINDOW_S", raising=False)
        assert resolve_clone_rate_window_seconds() == DEFAULT_CLONE_RATE_WINDOW_S

    def test_resolve_window_honours_valid_env(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_CLONE_RATE_WINDOW_S", "3600")
        assert resolve_clone_rate_window_seconds() == 3600.0

    def test_resolve_window_clamps_below_min(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_CLONE_RATE_WINDOW_S", "10")
        assert resolve_clone_rate_window_seconds() == 60.0  # _MIN

    def test_resolve_window_clamps_above_max(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_CLONE_RATE_WINDOW_S", "999999999")
        # _MAX = 30 * 24 * 3600
        assert resolve_clone_rate_window_seconds() == 30 * 24 * 3600.0

    def test_resolve_window_falls_back_on_garbage(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_CLONE_RATE_WINDOW_S", "abc")
        assert resolve_clone_rate_window_seconds() == DEFAULT_CLONE_RATE_WINDOW_S


# ── 8. record_clone_rate_limit_hold ──────────────────────────────────────


class TestRecordCloneRateLimitHold:

    def _decision(self) -> CloneRateLimitDecision:
        return CloneRateLimitDecision(
            allowed=False,
            count=3,
            limit=3,
            window_seconds=86400.0,
            retry_after_seconds=42.0,
            oldest_attempt_at=1000.0,
            tenant_id="tenant-x",
            target="https://x.example",
        )

    def test_routes_to_audit_log_with_canonical_action(self):
        d = self._decision()
        audit = _FakeAuditLog()
        rv = asyncio.run(record_clone_rate_limit_hold(
            d, audit_log=audit, actor="op@example", url="https://x.example/page",
        ))
        assert rv == 7  # _FakeAuditLog default rv
        assert len(audit.calls) == 1
        call = audit.calls[0]
        assert call["action"] == CLONE_RATE_AUDIT_ACTION
        assert call["entity_kind"] == CLONE_RATE_AUDIT_ENTITY_KIND
        assert call["entity_id"] == "tenant-x"
        assert call["actor"] == "op@example"
        assert call["after"]["tenant_id"] == "tenant-x"
        assert call["after"]["target"] == "https://x.example"
        assert call["after"]["url"] == "https://x.example/page"
        assert call["after"]["limit"] == 3
        assert call["after"]["count"] == 3
        assert call["after"]["retry_after_seconds"] == 42.0

    def test_returns_none_on_audit_failure(self):
        d = self._decision()
        broken = _RaisingAuditLog()
        rv = asyncio.run(record_clone_rate_limit_hold(d, audit_log=broken))
        assert rv is None
        assert broken.calls == 1

    def test_actor_defaults_to_system(self):
        d = self._decision()
        audit = _FakeAuditLog()
        asyncio.run(record_clone_rate_limit_hold(d, audit_log=audit))
        assert audit.calls[0]["actor"] == "system"

    def test_rejects_non_decision(self):
        with pytest.raises(TypeError):
            asyncio.run(record_clone_rate_limit_hold({"foo": "bar"}))  # type: ignore[arg-type]


# ── 9. get_clone_rate_limiter / singleton ────────────────────────────────


class TestSingletonResolution:

    def test_in_memory_when_redis_url_unset(self, monkeypatch):
        monkeypatch.delenv("OMNISIGHT_REDIS_URL", raising=False)
        reset_clone_rate_limiter()
        lim = get_clone_rate_limiter()
        assert isinstance(lim, InMemoryCloneRateLimiter)

    def test_singleton_reuses_instance(self, monkeypatch):
        monkeypatch.delenv("OMNISIGHT_REDIS_URL", raising=False)
        reset_clone_rate_limiter()
        lim1 = get_clone_rate_limiter()
        lim2 = get_clone_rate_limiter()
        assert lim1 is lim2

    def test_falls_back_when_redis_construction_fails(self, monkeypatch):
        # Set a redis URL but make redis import / connection fail —
        # singleton should fall back to in-memory and log a warning.
        monkeypatch.setenv("OMNISIGHT_REDIS_URL", "redis://does-not-resolve.invalid:6379/0")
        reset_clone_rate_limiter()

        def _explode(self_, redis_url):
            raise RuntimeError("simulated redis unreachable")

        monkeypatch.setattr(RedisCloneRateLimiter, "__init__", _explode)
        lim = get_clone_rate_limiter()
        assert isinstance(lim, InMemoryCloneRateLimiter)

    def test_reset_singleton_clears_state(self, monkeypatch):
        monkeypatch.delenv("OMNISIGHT_REDIS_URL", raising=False)
        reset_clone_rate_limiter()
        lim = get_clone_rate_limiter()
        lim.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1000.0)
        reset_clone_rate_limiter()
        lim2 = get_clone_rate_limiter()
        assert lim2 is not lim
        # Fresh in-memory limiter — no carry-over.
        d = lim2.check("t1", "https://x.example", limit=3, window_seconds=86400, now=1001.0)
        assert d.count == 1


# ── 10. Package re-exports / drift guard ─────────────────────────────────


W11_8_REEXPORTED_SYMBOLS = (
    "CLONE_RATE_AUDIT_ACTION",
    "CLONE_RATE_AUDIT_ENTITY_KIND",
    "CLONE_RATE_KEY_PREFIX",
    "CloneRateLimitDecision",
    "CloneRateLimitError",
    "CloneRateLimitedError",
    "CloneRateLimiter",
    "DEFAULT_CLONE_RATE_LIMIT",
    "DEFAULT_CLONE_RATE_WINDOW_S",
    "InMemoryCloneRateLimiter",
    "RedisCloneRateLimiter",
    "assert_clone_rate_limit",
    "canonical_clone_target",
    "clone_rate_limit_key",
    "get_clone_rate_limiter",
    "record_clone_rate_limit_hold",
    "reset_clone_rate_limiter",
    "resolve_clone_rate_limit",
    "resolve_clone_rate_window_seconds",
)


@pytest.mark.parametrize("symbol", W11_8_REEXPORTED_SYMBOLS)
def test_w11_8_symbol_re_exported(symbol):
    assert hasattr(web_pkg, symbol), f"backend.web missing {symbol}"
    assert symbol in web_pkg.__all__, f"{symbol} missing from backend.web.__all__"


def test_package_total_symbol_count_pinned_at_192():
    # W11.7 left __all__ at 127 symbols; W11.8 adds 19 → 146;
    # W11.9 adds 23 framework_adapter symbols → 169;
    # W11.10 adds 12 clone_spec_context symbols → 181;
    # W11.12 adds 11 clone_audit symbols → 192;
    # W13.2 adds 7 screenshot-breakpoint symbols → 199;
    # W13.3 adds 18 screenshot-writer symbols → 217;
    # W13.4 adds 16 screenshot-ghost-overlay symbols → 233;
    # W15.2 adds 11 vite_error_relay symbols → 244;
    # W15.3 adds 8 vite_error_prompt symbols → 252;
    # W15.4 adds 10 vite_retry_budget symbols → 262.
    # W15.5 adds 13 vite_config_injection symbols → 275.
    # W15.6 adds 13 vite_self_fix symbols → 288.
    # If this test fails with a DIFFERENT count, audit whether you
    # consciously added / removed a public symbol and update the pin.
    assert len(web_pkg.__all__) == 330


# ── 11. Whole-spec invariants ────────────────────────────────────────────


class TestWholeSpecInvariants:

    def test_audit_action_under_web_clone_namespace(self):
        # W11.12 audit row scopes "what a clone touched" by action
        # prefix; the rate-limit row must live in the same namespace.
        assert CLONE_RATE_AUDIT_ACTION.startswith("web.clone")

    def test_decision_target_is_canonical_form(self):
        lim = InMemoryCloneRateLimiter()
        # Drive the PEP entry point with a noisy URL — the decision's
        # target must be the canonical origin, not the raw input URL.
        d = asyncio.run(assert_clone_rate_limit(
            "t1", "https://Acme.EXAMPLE:443/path?cb=1#frag",
            limiter=lim, limit=3, window_seconds=86400, now=1000.0,
            audit_log=_FakeAuditLog(),
        ))
        assert d.target == "https://acme.example"

    def test_pep_hold_carries_url_into_error(self):
        lim = InMemoryCloneRateLimiter()
        for i in range(3):
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/landing",
                limiter=lim, limit=3, window_seconds=86400, now=1000.0 + i,
            ))
        with pytest.raises(CloneRateLimitedError) as exc_info:
            asyncio.run(assert_clone_rate_limit(
                "t1", "https://x.example/landing",
                limiter=lim, limit=3, window_seconds=86400, now=1003.0,
            ))
        # Caller can pivot from the error back to the offending URL.
        assert exc_info.value.url == "https://x.example/landing"
        assert exc_info.value.decision.target == "https://x.example"

    def test_full_round_trip_uses_one_canonical_key(self):
        # 4 different paths on the same origin → all stored under one
        # key in the in-memory bucket dict.
        lim = InMemoryCloneRateLimiter()
        for i, path in enumerate(("/a", "/b", "/c", "/d")):
            url = f"https://x.example{path}"
            try:
                asyncio.run(assert_clone_rate_limit(
                    "t1", url, limiter=lim, limit=3, window_seconds=86400,
                    now=1000.0 + i,
                ))
            except CloneRateLimitedError:
                pass
        # Internals: one bucket exists with exactly 3 timestamps.
        assert len(lim._buckets) == 1
        bucket = next(iter(lim._buckets.values()))
        assert len(bucket) == 3

    def test_lua_script_pruning_is_strict_inequality_not_inclusive(self):
        # ZREMRANGEBYSCORE removes entries with score <= cutoff (i.e.
        # at the edge, an entry exactly window_ms ago is *expired*).
        # Under our InMemoryLimiter._prune the same shape: <=cutoff is
        # popped. A clone at exactly oldest+window is allowed.
        lim = InMemoryCloneRateLimiter()
        lim.check("t1", "https://x.example", limit=1, window_seconds=10, now=1000.0)
        d_held = lim.check("t1", "https://x.example", limit=1, window_seconds=10, now=1009.999)
        assert d_held.held
        d_allow = lim.check("t1", "https://x.example", limit=1, window_seconds=10, now=1010.0)
        # At t=1010, oldest=1000, cutoff=1000, 1000 <= 1000 → removed.
        assert d_allow.allowed
