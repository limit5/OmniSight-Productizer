"""Z.2 (#291) — Unit tests for ``GET /runtime/providers/{provider}/balance``.

Covers the service-layer :func:`resolve_balance` in
:mod:`backend.routers.llm_balance` — the router function itself is a
thin HTTPException-on-unknown-provider wrapper around this, so the
service-layer contract is where the interesting branches live.

What's locked
─────────────
1. **Unsupported provider** — anthropic / openai / google / etc.
   collapse to the ``unsupported`` envelope with the verbatim spec
   reason string ("provider does not expose a public balance API with
   API-key authentication"). No fetch attempted, no cache written.
2. **Cache hit** — ``SharedKV`` prepopulated → endpoint returns the
   parsed ``BalanceInfo`` with ``source="cache"`` and no fetcher call.
3. **Cache miss → live fetch → cache write** — ``SharedKV`` empty →
   fetcher invoked exactly once with the resolved API key → response
   carries ``source="live"`` + the ``BalanceInfo`` + the raw vendor
   body; ``SharedKV`` slot is populated after the call so the next
   request hits the cache path.
4. **Cache miss + no key configured** → ``error`` envelope
   ("no API key configured for this provider"); fetcher must NOT run;
   cache untouched (operator may set the key before the next
   refresher tick).
5. **Cache miss + fetcher returns None (auth failure)** → ``error``
   envelope with the "authentication failed" message; cache untouched
   so a subsequent rotation lands cleanly.
6. **Cache miss + fetcher raises BalanceFetchError** → ``error``
   envelope carrying the vendor reason ("fetch failed: ..."); cache
   untouched.
7. **Cache miss + fetcher raises unexpected exception** → ``error``
   envelope with the exception class name; cache untouched, no crash.
8. **Malformed cache payload** — a non-JSON / wrong-type blob in
   SharedKV falls through to the live-fetch path rather than 500-ing
   the dashboard.
9. **Router-level unknown provider** (path param not in
   ``_VALID_PROVIDER_NAMES``) → HTTP 400, not a silent "unsupported"
   envelope — this is caller-error and must surface as such.
10. **Full FastAPI route** — app import + route presence + correct
    auth dependency (``require_admin``). Locks the wire contract so a
    future refactor that loses the router registration fails loudly.
11. **Response envelope shape locked** — key set for each of ``ok`` /
    ``unsupported`` / ``error`` so downstream UI / batch endpoint
    aggregation doesn't drift.

Module-global audit (SOP Step 1, 2026-04-21 rule)
─────────────────────────────────────────────────
Every test constructs its own ``SharedKV`` namespace via ``uuid4``
so in-memory fallback state cannot cross-pollute cases. The service
function takes ``kv`` / ``fetchers`` / ``key_resolver`` as kwargs so
no production ``Settings`` or Redis is touched.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from backend.llm_balance import BalanceFetchError, BalanceInfo
from backend.routers import llm_balance as endpoint
from backend.shared_state import SharedKV


pytestmark = pytest.mark.asyncio


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers / fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _kv() -> SharedKV:
    """Fresh SharedKV namespace per-test — belt-and-braces against
    in-memory fallback state leaking between cases."""
    return SharedKV(f"provider_balance_endpoint_test_{uuid.uuid4().hex[:8]}")


def _balance(
    *, amount: float = 12.5, when: float = 1_234.0, currency: str = "USD",
) -> BalanceInfo:
    return BalanceInfo(
        currency=currency,
        balance_remaining=amount,
        granted_total=amount + 5.0,
        usage_total=amount - 1.0,
        last_refreshed_at=when,
        raw={"hello": "world"},
    )


def _make_fetcher_ok(amount: float = 12.5, *, calls: list | None = None):
    """Return an async fetcher that yields a BalanceInfo and records
    each invocation into ``calls`` (for one-call assertions)."""

    async def fetcher(api_key: str, **_: Any) -> BalanceInfo:
        if calls is not None:
            calls.append(api_key)
        return _balance(amount=amount)

    return fetcher


def _make_fetcher_raises(reason: str = "upstream 502"):
    async def fetcher(api_key: str, **_: Any):
        raise BalanceFetchError("test-provider", reason)

    return fetcher


def _make_fetcher_auth_fail():
    async def fetcher(api_key: str, **_: Any):
        return None

    return fetcher


def _make_fetcher_crashes():
    async def fetcher(api_key: str, **_: Any):
        raise RuntimeError("surprise kaboom")

    return fetcher


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Unsupported providers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestUnsupportedProvider:

    @pytest.mark.parametrize(
        "provider",
        ["anthropic", "openai", "google", "xai", "groq",
         "together", "ollama"],
    )
    async def test_unsupported_providers_return_unsupported_envelope(
        self, provider: str,
    ):
        called: list[str] = []

        async def fetcher(api_key: str, **_: Any):
            called.append(api_key)
            return _balance()

        out = await endpoint.resolve_balance(
            provider,
            kv=_kv(),
            fetchers={provider: fetcher},  # fetcher present but
            # must not be invoked because ``is_balance_supported``
            # fails before the fetcher dispatch.
            key_resolver=lambda p: "sk-should-not-be-read",
        )

        assert out["status"] == "unsupported"
        assert out["provider"] == provider
        assert out["reason"] == (
            "provider does not expose a public balance API "
            "with API-key authentication"
        )
        assert called == [], (
            "Unsupported branch must not call the fetcher"
        )

    async def test_unsupported_envelope_does_not_write_cache(self):
        kv = _kv()
        out = await endpoint.resolve_balance(
            "anthropic",
            kv=kv,
            fetchers={},
            key_resolver=lambda p: "sk",
        )
        assert out["status"] == "unsupported"
        assert kv.get("anthropic") == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cache hit path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCacheHit:

    async def test_cache_hit_returns_cached_envelope(self):
        kv = _kv()
        from backend.llm_balance_refresher import _serialise_balance
        kv.set("deepseek", _serialise_balance(_balance(amount=99.0)))
        called: list[str] = []

        async def fetcher(api_key: str, **_: Any):
            called.append(api_key)
            return _balance()

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv,
            fetchers={"deepseek": fetcher},
            key_resolver=lambda p: "sk",
        )

        assert out["status"] == "ok"
        assert out["provider"] == "deepseek"
        assert out["balance_remaining"] == 99.0
        assert out["currency"] == "USD"
        assert out["granted_total"] == 99.0 + 5.0
        assert out["usage_total"] == 99.0 - 1.0
        assert out["last_refreshed_at"] == 1_234.0
        assert out["source"] == "cache"
        assert out["raw"] == {"hello": "world"}
        assert called == [], "Cache hit must not call fetcher"

    async def test_cache_hit_key_resolver_not_invoked(self):
        """Happy-path efficiency — no Settings lookup on the hot path."""
        kv = _kv()
        from backend.llm_balance_refresher import _serialise_balance
        kv.set("openrouter", _serialise_balance(_balance()))
        resolved: list[str] = []

        def resolver(provider: str) -> str | None:
            resolved.append(provider)
            return "sk"

        out = await endpoint.resolve_balance(
            "openrouter",
            kv=kv,
            fetchers={"openrouter": _make_fetcher_ok()},
            key_resolver=resolver,
        )

        assert out["status"] == "ok"
        assert resolved == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cache miss → live fetch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCacheMissLiveFetch:

    async def test_cache_miss_triggers_exactly_one_fetch(self):
        kv = _kv()
        calls: list[str] = []
        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv,
            fetchers={"deepseek": _make_fetcher_ok(
                amount=42.5, calls=calls,
            )},
            key_resolver=lambda p: "sk-live",
        )

        assert out["status"] == "ok"
        assert out["balance_remaining"] == 42.5
        assert out["source"] == "live"
        assert calls == ["sk-live"], (
            "Cache miss must invoke fetcher exactly once with "
            "the resolved key"
        )

    async def test_cache_miss_writes_to_sharedkv(self):
        kv = _kv()
        await endpoint.resolve_balance(
            "openrouter",
            kv=kv,
            fetchers={"openrouter": _make_fetcher_ok(amount=7.0)},
            key_resolver=lambda p: "sk",
        )
        raw = kv.get("openrouter")
        assert raw, "SharedKV must be populated after live fetch"
        parsed = json.loads(raw)
        assert parsed["balance_remaining"] == 7.0
        assert parsed["currency"] == "USD"

    async def test_next_call_after_live_fetch_hits_cache(self):
        """Locks the 'subsequent request hits cache' invariant so a
        future refactor that forgets to write the cache is caught."""
        kv = _kv()
        calls: list[str] = []
        fetcher_calls_made = {"n": 0}

        async def fetcher(api_key: str, **_: Any):
            fetcher_calls_made["n"] += 1
            calls.append(api_key)
            return _balance(amount=55.0)

        first = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, fetchers={"deepseek": fetcher},
            key_resolver=lambda p: "sk",
        )
        assert first["source"] == "live"

        second = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, fetchers={"deepseek": fetcher},
            key_resolver=lambda p: "sk",
        )
        assert second["source"] == "cache"
        assert second["balance_remaining"] == 55.0
        assert fetcher_calls_made["n"] == 1, (
            "Second request must not re-invoke the fetcher"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cache miss — no-key branch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCacheMissNoKey:

    async def test_missing_key_returns_error_without_fetching(self):
        kv = _kv()
        calls: list[str] = []
        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv,
            fetchers={"deepseek": _make_fetcher_ok(calls=calls)},
            key_resolver=lambda p: None,
        )

        assert out["status"] == "error"
        assert out["provider"] == "deepseek"
        assert "no API key configured" in out["message"]
        assert calls == []
        assert kv.get("deepseek") == "", (
            "No cache write when key missing"
        )

    async def test_empty_string_key_treated_as_missing(self):
        kv = _kv()
        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv,
            fetchers={"deepseek": _make_fetcher_ok()},
            key_resolver=lambda p: "",
        )
        assert out["status"] == "error"
        assert "no API key configured" in out["message"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cache miss — fetcher failure modes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCacheMissFetcherFailure:

    async def test_auth_failure_returns_error_no_cache(self):
        kv = _kv()
        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv,
            fetchers={"deepseek": _make_fetcher_auth_fail()},
            key_resolver=lambda p: "sk-revoked",
        )
        assert out["status"] == "error"
        assert "authentication failed" in out["message"]
        assert kv.get("deepseek") == ""

    async def test_balance_fetch_error_carries_reason(self):
        kv = _kv()
        out = await endpoint.resolve_balance(
            "openrouter",
            kv=kv,
            fetchers={"openrouter": _make_fetcher_raises(
                "provider returned 502",
            )},
            key_resolver=lambda p: "sk",
        )
        assert out["status"] == "error"
        assert out["message"] == "fetch failed: provider returned 502"
        assert kv.get("openrouter") == ""

    async def test_unexpected_exception_does_not_crash(self):
        kv = _kv()
        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv,
            fetchers={"deepseek": _make_fetcher_crashes()},
            key_resolver=lambda p: "sk",
        )
        assert out["status"] == "error"
        assert "RuntimeError" in out["message"]
        assert kv.get("deepseek") == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Defence-in-depth: malformed cache payload
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMalformedCachePayload:

    async def test_non_json_cache_falls_through_to_live(self):
        kv = _kv()
        kv.set("deepseek", "this is not json {")
        calls: list[str] = []

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv,
            fetchers={"deepseek": _make_fetcher_ok(
                amount=1.0, calls=calls,
            )},
            key_resolver=lambda p: "sk",
        )
        assert out["status"] == "ok"
        assert out["source"] == "live"
        assert calls == ["sk"]

    async def test_cache_payload_non_dict_falls_through_to_live(self):
        kv = _kv()
        kv.set("deepseek", json.dumps(["list", "not", "dict"]))
        calls: list[str] = []

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv,
            fetchers={"deepseek": _make_fetcher_ok(calls=calls)},
            key_resolver=lambda p: "sk",
        )
        assert out["status"] == "ok"
        assert out["source"] == "live"
        assert calls == ["sk"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Router-level validation + wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRouterSurface:

    async def test_unknown_provider_raises_http_400(self):
        """A path-param typo must not silently resolve to 'unsupported'."""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await endpoint.get_provider_balance("not-a-provider")
        assert exc_info.value.status_code == 400
        assert "Unknown provider" in exc_info.value.detail

    async def test_router_prefix_and_route_shape(self):
        paths = [r.path for r in endpoint.router.routes]
        assert "/runtime/providers/{provider}/balance" in paths
        assert endpoint.router.prefix == "/runtime/providers"

    async def test_router_requires_admin_auth(self):
        """Locks the auth baseline so someone who moves routes around
        can't accidentally expose balances to un-authed callers."""
        from backend import auth as _auth
        deps = endpoint.router.dependencies
        # The dependency is ``Depends(_auth.require_admin)`` — inspect
        # the underlying callable rather than relying on ``id`` equality
        # since Depends wraps it.
        dep_callables = [d.dependency for d in deps]
        assert _auth.require_admin in dep_callables, (
            "/runtime/providers/* must gate behind require_admin"
        )

    async def test_app_registers_route(self):
        """End-to-end wire check — the route exists on the live app at
        the expected prefixed path (``settings.api_prefix`` + router
        prefix). Guards against someone deleting the
        ``app.include_router`` line."""
        import backend.main as _main
        paths = [r.path for r in _main.app.routes]
        assert "/api/v1/runtime/providers/{provider}/balance" in paths


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Response envelope shape lock
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEnvelopeShape:

    async def test_ok_envelope_keys(self):
        kv = _kv()
        from backend.llm_balance_refresher import _serialise_balance
        kv.set("deepseek", _serialise_balance(_balance()))
        out = await endpoint.resolve_balance(
            "deepseek", kv=kv,
            fetchers={}, key_resolver=lambda p: None,
        )
        assert set(out.keys()) == {
            "status", "provider", "currency",
            "balance_remaining", "granted_total", "usage_total",
            "last_refreshed_at", "source", "raw",
        }

    async def test_unsupported_envelope_keys(self):
        out = await endpoint.resolve_balance(
            "anthropic", kv=_kv(),
            fetchers={}, key_resolver=lambda p: None,
        )
        assert set(out.keys()) == {"status", "provider", "reason"}

    async def test_error_envelope_keys(self):
        out = await endpoint.resolve_balance(
            "deepseek", kv=_kv(),
            fetchers={"deepseek": _make_fetcher_raises()},
            key_resolver=lambda p: "sk",
        )
        assert set(out.keys()) == {"status", "provider", "message"}
