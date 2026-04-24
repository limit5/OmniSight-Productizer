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
            "last_refreshed_at", "source", "raw", "stale_since",
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Batch endpoint — GET /runtime/providers/balance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# The nine-provider registry the batch endpoint iterates. Locked here
# so a silent drop from ``_VALID_PROVIDER_NAMES`` (e.g. someone removes
# ``ollama`` when we add a real ollama balance API) fails loudly at
# test-time rather than shrinking the dashboard response.
_EXPECTED_PROVIDERS = {
    "anthropic", "deepseek", "google", "groq",
    "ollama", "openai", "openrouter", "together", "xai",
}


class TestBatchEndpoint:
    """Z.2 checkbox 4 — batch endpoint contract.

    All nine providers must appear in every response regardless of
    support / key / cache state. Each envelope is the same shape as the
    single-provider endpoint; the batch only wraps them in
    ``{providers: [...]}``.
    """

    async def test_batch_covers_all_valid_providers(self):
        """Every name in ``_VALID_PROVIDER_NAMES`` appears exactly once."""
        out = await endpoint.resolve_all_balances(
            kv=_kv(),
            fetchers={
                "deepseek": _make_fetcher_ok(amount=1.0),
                "openrouter": _make_fetcher_ok(amount=2.0),
            },
            key_resolver=lambda p: "sk",
        )
        assert set(out.keys()) == {"providers"}
        assert isinstance(out["providers"], list)
        returned = [e["provider"] for e in out["providers"]]
        assert set(returned) == _EXPECTED_PROVIDERS
        assert len(returned) == len(_EXPECTED_PROVIDERS), (
            "No provider should appear twice"
        )

    async def test_batch_ordering_is_alphabetical(self):
        """Locks the stable render order the dashboard relies on."""
        out = await endpoint.resolve_all_balances(
            kv=_kv(),
            fetchers={
                "deepseek": _make_fetcher_ok(),
                "openrouter": _make_fetcher_ok(),
            },
            key_resolver=lambda p: "sk",
        )
        returned = [e["provider"] for e in out["providers"]]
        assert returned == sorted(_EXPECTED_PROVIDERS)

    async def test_batch_mixes_supported_and_unsupported_envelopes(self):
        """Supported providers surface ok/error, unsupported surface
        the static envelope in the same response."""
        kv = _kv()
        from backend.llm_balance_refresher import _serialise_balance
        kv.set("deepseek", _serialise_balance(_balance(amount=33.0)))

        out = await endpoint.resolve_all_balances(
            kv=kv,
            fetchers={
                "deepseek": _make_fetcher_ok(),
                "openrouter": _make_fetcher_ok(amount=4.0),
            },
            key_resolver=lambda p: "sk",
        )
        envelopes = {e["provider"]: e for e in out["providers"]}
        # Supported + cached → ok/cache
        assert envelopes["deepseek"]["status"] == "ok"
        assert envelopes["deepseek"]["source"] == "cache"
        assert envelopes["deepseek"]["balance_remaining"] == 33.0
        # Supported + live fetch → ok/live
        assert envelopes["openrouter"]["status"] == "ok"
        assert envelopes["openrouter"]["source"] == "live"
        assert envelopes["openrouter"]["balance_remaining"] == 4.0
        # Unsupported providers
        for name in _EXPECTED_PROVIDERS - {"deepseek", "openrouter"}:
            assert envelopes[name]["status"] == "unsupported"
            assert envelopes[name]["reason"] == (
                "provider does not expose a public balance API "
                "with API-key authentication"
            )

    async def test_batch_supported_no_key_returns_error_envelope(self):
        """Missing keys surface as per-provider error; unsupported
        providers still render unsupported in the same payload."""
        out = await endpoint.resolve_all_balances(
            kv=_kv(),
            fetchers={
                "deepseek": _make_fetcher_ok(),
                "openrouter": _make_fetcher_ok(),
            },
            key_resolver=lambda p: None,
        )
        envelopes = {e["provider"]: e for e in out["providers"]}
        assert envelopes["deepseek"]["status"] == "error"
        assert "no API key configured" in envelopes["deepseek"]["message"]
        assert envelopes["openrouter"]["status"] == "error"
        # Unsupported providers still render unsupported (no key lookup).
        assert envelopes["anthropic"]["status"] == "unsupported"

    async def test_batch_partial_failure_does_not_poison_others(self):
        """One provider's fetcher raising must not collapse the batch —
        the other provider's envelope still comes back healthy."""
        out = await endpoint.resolve_all_balances(
            kv=_kv(),
            fetchers={
                "deepseek": _make_fetcher_raises("upstream 502"),
                "openrouter": _make_fetcher_ok(amount=9.0),
            },
            key_resolver=lambda p: "sk",
        )
        envelopes = {e["provider"]: e for e in out["providers"]}
        assert envelopes["deepseek"]["status"] == "error"
        assert envelopes["deepseek"]["message"] == (
            "fetch failed: upstream 502"
        )
        assert envelopes["openrouter"]["status"] == "ok"
        assert envelopes["openrouter"]["balance_remaining"] == 9.0

    async def test_batch_shares_single_kv_handle_across_providers(self):
        """Live-fetched values for one provider must be readable by
        subsequent requests (writes landed in the shared KV, not in an
        ephemeral per-call handle)."""
        kv = _kv()
        await endpoint.resolve_all_balances(
            kv=kv,
            fetchers={
                "deepseek": _make_fetcher_ok(amount=11.0),
                "openrouter": _make_fetcher_ok(amount=22.0),
            },
            key_resolver=lambda p: "sk",
        )
        # Both supported providers' slots must be populated.
        deepseek_raw = kv.get("deepseek")
        openrouter_raw = kv.get("openrouter")
        assert deepseek_raw and json.loads(
            deepseek_raw
        )["balance_remaining"] == 11.0
        assert openrouter_raw and json.loads(
            openrouter_raw
        )["balance_remaining"] == 22.0

    async def test_batch_second_call_is_all_cache(self):
        """After one warm-up round, every supported provider should
        serve from cache on the second batch call (fetcher counters
        stay at their first-round totals)."""
        kv = _kv()
        call_counters = {"deepseek": 0, "openrouter": 0}

        async def ds_fetch(api_key: str, **_: Any):
            call_counters["deepseek"] += 1
            return _balance(amount=1.0)

        async def or_fetch(api_key: str, **_: Any):
            call_counters["openrouter"] += 1
            return _balance(amount=2.0)

        await endpoint.resolve_all_balances(
            kv=kv,
            fetchers={"deepseek": ds_fetch, "openrouter": or_fetch},
            key_resolver=lambda p: "sk",
        )
        assert call_counters == {"deepseek": 1, "openrouter": 1}

        out2 = await endpoint.resolve_all_balances(
            kv=kv,
            fetchers={"deepseek": ds_fetch, "openrouter": or_fetch},
            key_resolver=lambda p: "sk",
        )
        assert call_counters == {"deepseek": 1, "openrouter": 1}, (
            "Second batch round must hit cache, not re-fetch"
        )
        envelopes = {e["provider"]: e for e in out2["providers"]}
        assert envelopes["deepseek"]["source"] == "cache"
        assert envelopes["openrouter"]["source"] == "cache"

    async def test_batch_unexpected_resolve_balance_exception_is_isolated(
        self, monkeypatch,
    ):
        """Defence-in-depth: if ``resolve_balance`` itself raised
        (hypothetical abstraction leak past its inner except blocks),
        ``asyncio.gather(return_exceptions=True)`` must still surface
        the remaining providers normally and convert the crasher to an
        error envelope — not 500 the whole batch."""

        original = endpoint.resolve_balance

        async def flaky(provider: str, **kwargs: Any):
            if provider == "deepseek":
                raise ValueError("contrived failure")
            return await original(provider, **kwargs)

        monkeypatch.setattr(endpoint, "resolve_balance", flaky)

        out = await endpoint.resolve_all_balances(
            kv=_kv(),
            fetchers={"openrouter": _make_fetcher_ok(amount=5.0)},
            key_resolver=lambda p: "sk",
        )
        envelopes = {e["provider"]: e for e in out["providers"]}
        assert envelopes["deepseek"]["status"] == "error"
        assert "ValueError" in envelopes["deepseek"]["message"]
        assert envelopes["openrouter"]["status"] == "ok"
        assert envelopes["openrouter"]["balance_remaining"] == 5.0
        # Unsupported providers pass through unharmed.
        assert envelopes["anthropic"]["status"] == "unsupported"


class TestBatchRouterSurface:

    async def test_batch_route_registered_on_router(self):
        paths = [r.path for r in endpoint.router.routes]
        assert "/runtime/providers/balance" in paths

    async def test_batch_route_registered_on_app(self):
        import backend.main as _main
        paths = [r.path for r in _main.app.routes]
        assert "/api/v1/runtime/providers/balance" in paths

    async def test_batch_route_does_not_shadow_single_route(self):
        """Both routes must coexist — structural segment-count difference
        disambiguates them. Regression guard in case a future refactor
        accidentally consolidates them."""
        paths = [r.path for r in endpoint.router.routes]
        assert "/runtime/providers/balance" in paths
        assert "/runtime/providers/{provider}/balance" in paths

    async def test_batch_route_requires_admin_auth(self):
        """Router-level dependency stays on require_admin — balances are
        billing-sensitive, batch must not accidentally downgrade to
        current_user."""
        from backend import auth as _auth
        dep_callables = [d.dependency for d in endpoint.router.dependencies]
        assert _auth.require_admin in dep_callables


class TestBatchEnvelopeShape:

    async def test_batch_top_level_shape(self):
        out = await endpoint.resolve_all_balances(
            kv=_kv(),
            fetchers={},
            key_resolver=lambda p: None,
        )
        assert set(out.keys()) == {"providers"}
        assert isinstance(out["providers"], list)
        assert all(isinstance(e, dict) for e in out["providers"])

    async def test_batch_each_envelope_matches_single_endpoint_shape(self):
        """Every inner envelope matches the corresponding
        single-provider shape: ok / unsupported / error key-sets."""
        kv = _kv()
        from backend.llm_balance_refresher import _serialise_balance
        kv.set("deepseek", _serialise_balance(_balance()))

        out = await endpoint.resolve_all_balances(
            kv=kv,
            fetchers={
                "openrouter": _make_fetcher_raises(),
            },
            key_resolver=lambda p: "sk",
        )
        envelopes = {e["provider"]: e for e in out["providers"]}
        # ok shape (cache hit)
        assert set(envelopes["deepseek"].keys()) == {
            "status", "provider", "currency",
            "balance_remaining", "granted_total", "usage_total",
            "last_refreshed_at", "source", "raw", "stale_since",
        }
        # error shape
        assert set(envelopes["openrouter"].keys()) == {
            "status", "provider", "message",
        }
        # unsupported shape
        assert set(envelopes["anthropic"].keys()) == {
            "status", "provider", "reason",
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Z.2 boundary — stale_since + auth-fail "no cache write" contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# The boundary row spells out two distinct failure-mode contracts:
#
# 1. **API key 格式錯 / 作廢** (auth failure) → ``{status: "error",
#    message: "..."}`` **不 cache**. Existing cache from a previous
#    successful fetch stays in place so "下次正常 key 要能立刻 pick up"
#    — the next refresh with a valid key repopulates without having
#    to invalidate an older-but-still-useful snapshot first.
#
# 2. **DeepSeek / OpenRouter API 本身 5xx** (provider-side failure)
#    → 回 **快取值** 並標 ``stale_since``. The cache is not
#    overwritten, but a separate marker is set so cache reads render
#    with a "value is from before {ts}" badge.
#
# These tests lock both halves of the contract at the service layer.


def _stale_kv() -> SharedKV:
    """Fresh stale-marker SharedKV namespace per test — separate from
    the cache namespace so a test can't accidentally read the marker
    as if it were a cached BalanceInfo."""
    return SharedKV(
        f"provider_balance_stale_endpoint_test_{uuid.uuid4().hex[:8]}"
    )


class TestBoundaryStaleSinceOnCacheHit:
    """Case 2: when a stale marker is set for a provider with cached
    data, the cache-hit path must surface ``stale_since``."""

    async def test_cache_hit_without_marker_emits_stale_since_none(self):
        """Happy path — cache hit, no marker → envelope carries
        ``stale_since=None`` (key always present for shape stability)."""
        kv = _kv()
        stale = _stale_kv()
        from backend.llm_balance_refresher import _serialise_balance
        kv.set("deepseek", _serialise_balance(_balance()))

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            fetchers={}, key_resolver=lambda p: None,
        )

        assert out["status"] == "ok"
        assert out["source"] == "cache"
        assert out["stale_since"] is None

    async def test_cache_hit_with_marker_emits_stale_since_epoch(self):
        """After the refresher recorded a failure and left the cache
        intact, the endpoint's cache read must surface the marker
        timestamp so the UI renders the stale badge."""
        kv = _kv()
        stale = _stale_kv()
        from backend.llm_balance_refresher import (
            _serialise_balance, _write_stale_marker,
        )
        kv.set("openrouter", _serialise_balance(_balance(amount=33.0)))
        failure_at = 1_700_000_000.0
        _write_stale_marker(stale, "openrouter", failure_at)

        out = await endpoint.resolve_balance(
            "openrouter",
            kv=kv, stale_kv=stale,
            fetchers={}, key_resolver=lambda p: None,
        )

        assert out["status"] == "ok"
        assert out["source"] == "cache"
        assert out["balance_remaining"] == 33.0, (
            "Cached balance still served — 5xx contract is 'return "
            "cached value' not 'return error'"
        )
        assert out["stale_since"] == pytest.approx(failure_at)

    async def test_cache_hit_unparseable_marker_self_heals(self):
        """A corrupted marker entry must not permanently stick — the
        read path deletes and returns None so the next successful
        refresh tick stops rendering stale."""
        kv = _kv()
        stale = _stale_kv()
        from backend.llm_balance_refresher import _serialise_balance
        kv.set("deepseek", _serialise_balance(_balance()))
        stale.set("deepseek", "not-a-float")

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            fetchers={}, key_resolver=lambda p: None,
        )

        assert out["status"] == "ok"
        assert out["stale_since"] is None
        assert stale.get("deepseek") == "", (
            "Unparseable marker should be self-healed (deleted)"
        )

    async def test_cache_hit_fetcher_never_invoked(self):
        """Cache hit must short-circuit — even with a stale marker, we
        do not trigger a live fetch (which would obscure the refresher's
        own failure detection + retry schedule)."""
        kv = _kv()
        stale = _stale_kv()
        from backend.llm_balance_refresher import (
            _serialise_balance, _write_stale_marker,
        )
        kv.set("deepseek", _serialise_balance(_balance()))
        _write_stale_marker(stale, "deepseek", 12345.0)

        called: list[str] = []

        async def fetcher(api_key: str, **_: Any):
            called.append(api_key)
            return _balance()

        await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            fetchers={"deepseek": fetcher},
            key_resolver=lambda p: "sk",
        )

        assert called == [], (
            "Cache hit + stale must NOT re-fetch — refresher owns "
            "that retry cadence"
        )


class TestBoundaryStaleMarkerLifecycle:
    """Case 2 continued: the endpoint's own cache-miss write path
    must clear the stale marker on success and set it on 5xx."""

    async def test_live_fetch_success_clears_stale_marker(self):
        kv = _kv()
        stale = _stale_kv()
        from backend.llm_balance_refresher import _write_stale_marker
        _write_stale_marker(stale, "deepseek", 5_000.0)

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            fetchers={"deepseek": _make_fetcher_ok(amount=1.5)},
            key_resolver=lambda p: "sk",
        )

        assert out["status"] == "ok"
        assert out["source"] == "live"
        assert out["stale_since"] is None
        assert stale.get("deepseek") == "", (
            "Successful fetch must clear any prior stale marker"
        )

    async def test_live_fetch_5xx_writes_stale_marker(self):
        """Cache miss + 5xx currently returns an error envelope (nothing
        to serve), but we still write the marker so that a concurrent
        refresher worker that succeeded-then-failed leaves behind a
        consistent signal for the next cache hit."""
        kv = _kv()
        stale = _stale_kv()

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            fetchers={"deepseek": _make_fetcher_raises("upstream 502")},
            key_resolver=lambda p: "sk",
            now=9_999.0,
        )

        assert out["status"] == "error"
        assert out["message"] == "fetch failed: upstream 502"
        assert kv.get("deepseek") == "", "5xx does not create a cache"
        marker = stale.get("deepseek")
        assert marker, "5xx must write the stale marker"
        assert float(marker) == pytest.approx(9_999.0)

    async def test_live_fetch_unexpected_exception_writes_stale_marker(
        self,
    ):
        """Defence-in-depth — an unhandled exception escaping the
        fetcher is treated the same as a 5xx for stale-marker
        purposes. Keeps the UI coherent if a vendor rolls out a
        schema change that temporarily breaks normalisation."""
        kv = _kv()
        stale = _stale_kv()

        out = await endpoint.resolve_balance(
            "openrouter",
            kv=kv, stale_kv=stale,
            fetchers={"openrouter": _make_fetcher_crashes()},
            key_resolver=lambda p: "sk",
            now=42.0,
        )

        assert out["status"] == "error"
        assert "RuntimeError" in out["message"]
        assert float(stale.get("openrouter")) == pytest.approx(42.0)


class TestBoundaryAuthFailDoesNotMarkStale:
    """Case 1: auth failure is operator-side, not provider-down. The
    endpoint must NOT mark the provider stale on 401/403 — doing so
    would mislead the dashboard into showing a "provider unavailable"
    stale badge when the real issue is the operator's key."""

    async def test_live_fetch_auth_fail_leaves_stale_marker_absent(self):
        kv = _kv()
        stale = _stale_kv()

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            fetchers={"deepseek": _make_fetcher_auth_fail()},
            key_resolver=lambda p: "sk-revoked",
        )

        assert out["status"] == "error"
        assert "authentication failed" in out["message"]
        assert stale.get("deepseek") == "", (
            "Auth failure must NOT write stale marker — distinct "
            "contract from 5xx"
        )

    async def test_live_fetch_auth_fail_preserves_existing_stale_marker(
        self,
    ):
        """If the refresher recorded a prior 5xx and this request's
        fetch hits 401 (operator just rotated the key mid-outage), we
        leave the previously-recorded marker in place — the cache
        entry behind it is genuinely stale regardless of why the
        current attempt failed. Consistency > cleverness here."""
        kv = _kv()
        stale = _stale_kv()
        from backend.llm_balance_refresher import _write_stale_marker
        _write_stale_marker(stale, "deepseek", 8_888.0)

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            fetchers={"deepseek": _make_fetcher_auth_fail()},
            key_resolver=lambda p: "sk",
        )

        assert out["status"] == "error"
        marker = stale.get("deepseek")
        assert marker and float(marker) == pytest.approx(8_888.0), (
            "Prior stale marker survives the auth-fail branch"
        )

    async def test_live_fetch_auth_fail_does_not_touch_existing_cache(
        self,
    ):
        """Per Z.2 spec '下次正常 key 要能立刻 pick up' — auth-fail
        must not invalidate an existing cache, so that the next
        successful refresh-with-valid-key can overwrite cleanly
        without a gap."""
        kv = _kv()
        stale = _stale_kv()
        from backend.llm_balance_refresher import _serialise_balance
        # Seed a cache that would be served on subsequent reads; this
        # test targets the cache-miss path so we use a separate
        # provider to verify the miss branch without inverting cache
        # hit.
        kv.set("openrouter", _serialise_balance(_balance(amount=10.0)))
        # Now trigger deepseek which has NO cache.
        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            fetchers={"deepseek": _make_fetcher_auth_fail()},
            key_resolver=lambda p: "sk",
        )
        assert out["status"] == "error"
        # openrouter cache untouched by deepseek's auth-fail.
        assert kv.get("openrouter"), (
            "Auth-fail on one provider must not touch another "
            "provider's cache slot"
        )


class TestBoundaryNoKeyPath:
    """Case 1 narrower — the no-key branch (resolver returns None)
    short-circuits before touching the fetcher. It must not write the
    stale marker either (no HTTP attempt means no "provider is down"
    signal to record)."""

    async def test_no_key_does_not_write_stale_marker(self):
        kv = _kv()
        stale = _stale_kv()

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            fetchers={"deepseek": _make_fetcher_ok()},
            key_resolver=lambda p: None,
        )

        assert out["status"] == "error"
        assert "no API key configured" in out["message"]
        assert stale.get("deepseek") == ""


class TestBoundaryBatchAggregation:
    """Sanity check that the batch endpoint threads the same stale_kv
    handle across all per-provider calls, so a marker set by one
    call's fetcher-raise path is visible to a subsequent call's
    cache-hit read in the same round."""

    async def test_batch_propagates_stale_since_to_cache_envelopes(self):
        kv = _kv()
        stale = _stale_kv()
        from backend.llm_balance_refresher import (
            _serialise_balance, _write_stale_marker,
        )
        # Seed both supported providers with cache + distinct markers.
        kv.set("deepseek", _serialise_balance(_balance(amount=1.0)))
        kv.set("openrouter", _serialise_balance(_balance(amount=2.0)))
        _write_stale_marker(stale, "deepseek", 1_111.0)
        # openrouter has cache but NO marker — fresh.

        out = await endpoint.resolve_all_balances(
            kv=kv, stale_kv=stale,
            fetchers={},
            key_resolver=lambda p: None,
        )
        envelopes = {e["provider"]: e for e in out["providers"]}
        assert envelopes["deepseek"]["stale_since"] == pytest.approx(
            1_111.0,
        )
        assert envelopes["openrouter"]["stale_since"] is None

    async def test_batch_unsupported_envelopes_have_no_stale_since(self):
        """Unsupported providers (anthropic etc.) never carry
        ``stale_since`` — the key is exclusive to the ``ok`` shape
        since only ``ok`` envelopes represent cached data."""
        out = await endpoint.resolve_all_balances(
            kv=_kv(), stale_kv=_stale_kv(),
            fetchers={}, key_resolver=lambda p: None,
        )
        envelopes = {e["provider"]: e for e in out["providers"]}
        # Unsupported providers: {anthropic, google, groq, ollama,
        # openai, together, xai}. Their envelope shape is
        # {status, provider, reason} with no stale_since key.
        for name in {
            "anthropic", "google", "groq", "ollama",
            "openai", "together", "xai",
        }:
            assert envelopes[name]["status"] == "unsupported"
            assert "stale_since" not in envelopes[name], (
                "Unsupported envelope must not carry stale_since"
            )
