"""Z.2 (#291) — Integration tests for LLM balance: mocked DeepSeek +
OpenRouter HTTP → real fetchers → refresher → SharedKV → endpoint.

What this file is (and is not)
──────────────────────────────
The existing three Z.2 test files each lock one layer in isolation:

* ``test_llm_balance_fetchers.py`` — pure fetcher contract, no
  refresher, no SharedKV, no endpoint.
* ``test_llm_balance_refresher.py`` — refresher logic driven by
  **injected** fake fetchers; never calls the real HTTP fetchers.
* ``test_llm_balance_endpoint.py`` — endpoint logic driven by
  **injected** fake fetchers; never calls the real HTTP fetchers.

This integration file is the Z.2-final checkbox — it drives the
whole stack with a single mocked HTTP layer (``respx``) so the
verification covers the full wiring: vendor JSON → real
``fetch_balance_deepseek`` / ``fetch_balance_openrouter`` →
:func:`backend.llm_balance_refresher.refresh_once` → ``SharedKV`` →
:func:`backend.routers.llm_balance.resolve_balance` +
:func:`resolve_all_balances`. If any cross-layer contract drifts
(e.g. the refresher stops calling the serialiser, the endpoint stops
reading the right namespace, a fetcher starts returning a shape the
endpoint envelope builder can't consume), one of these tests fails.

What's locked (per the Z.2 spec row)
───────────────────────────────────
1. **Valid balance** — mocked 200 responses for both providers →
   refresh_once populates ``SharedKV("provider_balance")`` with a
   JSON envelope round-trippable by the endpoint → cache-hit path
   renders ``ok`` envelope with ``source="cache"``.
2. **Auth fail** — mocked 401 / 403 → fetchers return ``None`` →
   refresher records ``auth_fail`` outcome, cache **untouched**,
   stale marker **untouched** (auth-fail is operator-side, not
   provider-down).
3. **5xx** — mocked 500 / 502 / 503 / 504 → fetchers raise
   ``BalanceFetchError`` → refresher records ``fetch_error`` outcome,
   cache **untouched**, stale marker **written** at ``now``; a
   subsequent endpoint cache-hit (from a prior successful fetch)
   surfaces ``stale_since`` pointing at the failure timestamp.
4. **Batch endpoint aggregation** — mixed responses across providers
   → batch payload has exactly 9 envelopes (sorted alphabetically),
   supported providers carry live / cached / error shape, unsupported
   providers carry the verbatim-reason envelope.
5. **Unsupported provider** — anthropic / google / groq / ollama /
   openai / together / xai surface ``unsupported`` envelope even when
   the supported-provider HTTP layer is live; the fetcher path is
   never invoked for them regardless of whether an API key exists in
   the resolver.

Module-global audit (SOP Step 1, 2026-04-21 rule)
─────────────────────────────────────────────────
* ``respx.mock`` context per-test at the transport layer — nothing
  leaks between cases.
* Every test constructs its own ``SharedKV`` namespace via
  ``uuid.uuid4`` so the in-memory fallback dict cannot cross-pollute
  even if Redis is not available in the test env.
* The refresher's ``_LOOP_RUNNING`` module-global is never touched —
  this file drives ``refresh_once`` directly (one-shot tick), not
  ``run_refresh_loop``; the singleton-guard contract is already
  locked by ``test_llm_balance_refresher.py::TestRunRefreshLoop``.
* Every test passes its own ``kv`` / ``stale_kv`` / ``key_resolver``
  kwargs and lets ``fetchers=None`` default through to
  ``SUPPORTED_BALANCE_PROVIDERS`` so the **real** fetcher code path
  is exercised end-to-end (this is the key distinction from the three
  sibling unit files).

Read-after-write audit
──────────────────────
Two write sites: ``_store.set(provider, ...)`` on successful fetch
and ``_stale.set(provider, ...)`` on fetch-error. Each is atomic
(Redis HSET / in-memory threading.Lock). Where a test writes then
reads in the same coroutine (``refresh_once`` → ``resolve_balance``),
the write landed before the read happens, so the read-after-write is
never timing-sensitive in these tests.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import respx

from backend import llm_balance_refresher as lbr
from backend.routers import llm_balance as endpoint
from backend.shared_state import SharedKV


pytestmark = pytest.mark.asyncio


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared fixtures / helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_DEEPSEEK_URL = "https://api.deepseek.com/user/balance"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/auth/key"


def _kv(kind: str = "cache") -> SharedKV:
    """Fresh SharedKV per test, distinct namespace for cache vs stale
    so the two stores cannot alias even if the suite runs with an
    in-memory fallback whose dict is shared by namespace string."""
    return SharedKV(f"provider_balance_{kind}_integration_{uuid.uuid4().hex[:8]}")


def _deepseek_ok_body(
    *, balance: float = 12.50, granted: float = 5.00,
    currency: str = "USD",
) -> dict[str, Any]:
    """Realistic DeepSeek /user/balance response — string-typed amounts
    to match the real vendor wire format (fetcher coerces via
    ``_coerce_float``)."""
    return {
        "is_available": True,
        "balance_infos": [
            {
                "currency": currency,
                "total_balance": f"{balance:.2f}",
                "granted_balance": f"{granted:.2f}",
                "topped_up_balance": f"{balance - granted:.2f}",
            },
        ],
    }


def _openrouter_ok_body(
    *, remaining: float = 5.58, limit: float = 10.0,
    usage: float = 4.42,
) -> dict[str, Any]:
    """Realistic OpenRouter /auth/key response."""
    return {
        "data": {
            "label": "sk-or-v1-integration",
            "usage": usage,
            "limit": limit,
            "limit_remaining": remaining,
            "is_free_tier": False,
            "rate_limit": {"requests": 200, "interval": "10s"},
        },
    }


def _key_resolver(keys: dict[str, str] | None = None):
    """Build an injectable ``key_resolver`` that returns from a static
    map — the integration layer never touches ``backend.config.settings``.
    """
    mapping = keys or {
        "deepseek": "sk-deepseek-integration",
        "openrouter": "sk-or-v1-integration",
    }

    def resolver(provider: str) -> str | None:
        return mapping.get(provider)

    return resolver


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Valid balance — full stack round-trip
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIntegrationValidBalance:
    """Mocked 200 → real fetchers → refresher populates SharedKV →
    endpoint cache-hit renders ``ok`` envelope."""

    @respx.mock
    async def test_refresher_populates_sharedkv_for_both_providers(self):
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=_deepseek_ok_body(
                balance=12.50, granted=5.00,
            )),
        )
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=_openrouter_ok_body(
                remaining=5.58, limit=10.0, usage=4.42,
            )),
        )
        kv = _kv("cache")
        stale = _kv("stale")
        state: dict[str, lbr._ProviderBackoff] = {}

        outcomes = await lbr.refresh_once(
            state=state,
            base_interval_s=600.0,
            now=1_700_000_000.0,
            key_resolver=_key_resolver(),
            kv=kv, stale_kv=stale,
        )

        assert outcomes == {"deepseek": "ok", "openrouter": "ok"}

        ds_cached = json.loads(kv.get("deepseek"))
        assert ds_cached["currency"] == "USD"
        assert ds_cached["balance_remaining"] == pytest.approx(12.5)
        assert ds_cached["granted_total"] == pytest.approx(5.0)
        # DeepSeek does not report cumulative usage.
        assert ds_cached["usage_total"] is None

        or_cached = json.loads(kv.get("openrouter"))
        assert or_cached["currency"] == "USD"
        assert or_cached["balance_remaining"] == pytest.approx(5.58)
        assert or_cached["granted_total"] == pytest.approx(10.0)
        assert or_cached["usage_total"] == pytest.approx(4.42)

    @respx.mock
    async def test_endpoint_serves_cache_after_refresher(self):
        """End-to-end: mocked HTTP → refresher write → endpoint read."""
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=_deepseek_ok_body(
                balance=7.25, granted=2.25,
            )),
        )
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=_openrouter_ok_body()),
        )
        kv = _kv("cache")
        stale = _kv("stale")
        state: dict[str, lbr._ProviderBackoff] = {}

        await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=1_700_000_000.0,
            key_resolver=_key_resolver(), kv=kv, stale_kv=stale,
        )

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            # Cache is warm — fetchers/resolver must not be invoked.
            fetchers={}, key_resolver=lambda p: None,
        )

        assert out["status"] == "ok"
        assert out["source"] == "cache"
        assert out["provider"] == "deepseek"
        assert out["balance_remaining"] == pytest.approx(7.25)
        assert out["granted_total"] == pytest.approx(2.25)
        assert out["stale_since"] is None, (
            "Successful refresh must not leave a stale marker behind"
        )

    @respx.mock
    async def test_endpoint_cache_miss_triggers_live_http(self):
        """Cache-miss path exercises the real fetcher through respx
        too, not just through the refresher."""
        route_ds = respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=_deepseek_ok_body(
                balance=99.0, granted=50.0,
            )),
        )
        kv = _kv("cache")
        stale = _kv("stale")

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            key_resolver=_key_resolver(),
        )

        assert route_ds.called, (
            "Cache-miss + resolvable key must hit the real HTTP path"
        )
        assert out["status"] == "ok"
        assert out["source"] == "live"
        assert out["balance_remaining"] == pytest.approx(99.0)
        # SharedKV now populated so the next call short-circuits.
        assert kv.get("deepseek"), (
            "Live fetch must persist the result in SharedKV"
        )

    @respx.mock
    async def test_refresher_resets_backoff_after_success(self):
        """A successful tick clears ``consecutive_failures`` regardless
        of prior state — the real refresher code path (not an injected
        fake) keeps the backoff contract."""
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=_deepseek_ok_body()),
        )
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=_openrouter_ok_body()),
        )
        kv = _kv("cache")
        stale = _kv("stale")
        state = {
            "deepseek": lbr._ProviderBackoff(
                consecutive_failures=3, next_attempt_at=0.0,
            ),
            "openrouter": lbr._ProviderBackoff(
                consecutive_failures=1, next_attempt_at=0.0,
            ),
        }

        await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=1_000.0,
            key_resolver=_key_resolver(), kv=kv, stale_kv=stale,
        )

        assert state["deepseek"].consecutive_failures == 0
        assert state["openrouter"].consecutive_failures == 0
        # next_attempt_at pushed forward by base_interval.
        assert state["deepseek"].next_attempt_at == pytest.approx(1_600.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Auth failure — no cache, no stale marker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIntegrationAuthFail:
    """Mocked 401 / 403 → real fetchers return ``None`` → refresher +
    endpoint leave cache + stale marker untouched so a subsequent
    rotation picks up cleanly."""

    @respx.mock
    @pytest.mark.parametrize("status", [401, 403])
    async def test_deepseek_auth_fail_leaves_sharedkv_empty(self, status):
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(
                status,
                json={"error": {"message": "Authentication Fails"}},
            ),
        )
        # OpenRouter must not be called for this test — we only
        # register the deepseek route here.
        kv = _kv("cache")
        stale = _kv("stale")
        state: dict[str, lbr._ProviderBackoff] = {}

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=500.0,
            # Only deepseek has a key → openrouter skips as "no_key".
            key_resolver=_key_resolver(
                {"deepseek": "sk-deepseek-revoked"},
            ),
            kv=kv, stale_kv=stale,
        )

        assert outcomes["deepseek"] == "auth_fail"
        assert outcomes["openrouter"] == "no_key"

        assert kv.get("deepseek") == "", (
            "Auth-fail path must NOT write to SharedKV — next rotation "
            "lands cleanly without a stale 'auth_failed' envelope"
        )
        assert stale.get("deepseek") == "", (
            "Auth-fail is operator-side — stale marker must stay clean"
        )
        # Backoff still advances so we don't hammer the revoked key.
        assert state["deepseek"].consecutive_failures == 1

    @respx.mock
    @pytest.mark.parametrize("status", [401, 403])
    async def test_openrouter_auth_fail_leaves_sharedkv_empty(self, status):
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(
                status, json={"error": {"message": "Invalid API key"}},
            ),
        )
        kv = _kv("cache")
        stale = _kv("stale")
        state: dict[str, lbr._ProviderBackoff] = {}

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=500.0,
            key_resolver=_key_resolver({"openrouter": "sk-or-revoked"}),
            kv=kv, stale_kv=stale,
        )

        assert outcomes["openrouter"] == "auth_fail"
        assert kv.get("openrouter") == ""
        assert stale.get("openrouter") == ""

    @respx.mock
    async def test_auth_fail_preserves_existing_cache_entry(self):
        """The "下次正常 key 要能立刻 pick up" contract: auth-fail does
        not overwrite or invalidate a previous good snapshot — the next
        valid fetch lands cleanly over the old cache without a gap
        where the dashboard shows 'unknown'."""
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(401, json={}),
        )
        kv = _kv("cache")
        stale = _kv("stale")
        # Seed a prior good snapshot.
        prior = lbr._serialise_balance({
            "currency": "USD",
            "balance_remaining": 99.0,
            "granted_total": 50.0,
            "usage_total": None,
            "last_refreshed_at": 1_000.0,
            "raw": {"seeded": True},
        })
        kv.set("deepseek", prior)

        state: dict[str, lbr._ProviderBackoff] = {}
        await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=2_000.0,
            key_resolver=_key_resolver(
                {"deepseek": "sk-deepseek-revoked"},
            ),
            kv=kv, stale_kv=stale,
        )

        # Prior snapshot still there.
        after = json.loads(kv.get("deepseek"))
        assert after["balance_remaining"] == 99.0
        assert after["raw"] == {"seeded": True}

    @respx.mock
    async def test_endpoint_auth_fail_on_cache_miss_returns_error(self):
        """End-to-end: cache-miss + 401 at the endpoint layer surfaces
        the spec-locked ``authentication failed`` message; cache and
        stale marker stay clean."""
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(
                403, json={"error": {"message": "API key invalid"}},
            ),
        )
        kv = _kv("cache")
        stale = _kv("stale")

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            key_resolver=_key_resolver(
                {"deepseek": "sk-deepseek-revoked"},
            ),
        )

        assert out["status"] == "error"
        assert "authentication failed" in out["message"]
        assert kv.get("deepseek") == ""
        assert stale.get("deepseek") == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5xx — cache untouched, stale_since set
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIntegration5xx:
    """Mocked 5xx → real fetchers raise ``BalanceFetchError`` →
    refresher + endpoint leave the cache intact while writing the
    stale marker; cache-hit reads surface ``stale_since``."""

    @respx.mock
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_deepseek_5xx_writes_stale_marker_not_cache(self, status):
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(status, text="bad gateway"),
        )
        kv = _kv("cache")
        stale = _kv("stale")
        state: dict[str, lbr._ProviderBackoff] = {}

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=5_000.0,
            key_resolver=_key_resolver({"deepseek": "sk-valid"}),
            kv=kv, stale_kv=stale,
        )

        assert outcomes["deepseek"] == "fetch_error"
        assert kv.get("deepseek") == "", (
            "5xx must NOT create a cache entry — the operator would "
            "otherwise see fabricated zeros"
        )
        marker = stale.get("deepseek")
        assert marker, "5xx must write the stale marker"
        assert float(marker) == pytest.approx(5_000.0)
        assert state["deepseek"].consecutive_failures == 1

    @respx.mock
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_openrouter_5xx_writes_stale_marker_not_cache(self, status):
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(status, text="upstream broken"),
        )
        kv = _kv("cache")
        stale = _kv("stale")
        state: dict[str, lbr._ProviderBackoff] = {}

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=7_000.0,
            key_resolver=_key_resolver({"openrouter": "sk-valid"}),
            kv=kv, stale_kv=stale,
        )

        assert outcomes["openrouter"] == "fetch_error"
        assert kv.get("openrouter") == ""
        assert float(stale.get("openrouter")) == pytest.approx(7_000.0)

    @respx.mock
    async def test_5xx_after_prior_success_serves_cache_with_stale_since(self):
        """The canonical Z.2 boundary flow: tick-1 succeeds (cache
        populated, marker cleared) → tick-2 hits 5xx (cache preserved,
        marker written) → endpoint cache-hit carries ``stale_since``
        set to tick-2's timestamp."""
        # Tick 1 — success.
        ok_route = respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=_deepseek_ok_body(
                balance=42.0, granted=10.0,
            )),
        )
        kv = _kv("cache")
        stale = _kv("stale")
        state: dict[str, lbr._ProviderBackoff] = {}

        await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=1_000.0,
            key_resolver=_key_resolver({"deepseek": "sk-valid"}),
            kv=kv, stale_kv=stale,
        )
        assert kv.get("deepseek"), "tick-1 must populate cache"
        assert stale.get("deepseek") == "", "tick-1 success clears marker"

        # Tick 2 — 5xx. Advance past backoff window so the refresher
        # actually attempts the fetch (tick-1's reset pushed
        # next_attempt_at to 1600; we schedule tick-2 well after).
        ok_route.mock(return_value=httpx.Response(503, text="outage"))
        await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=2_000.0,
            key_resolver=_key_resolver({"deepseek": "sk-valid"}),
            kv=kv, stale_kv=stale,
        )

        # Cache preserved.
        preserved = json.loads(kv.get("deepseek"))
        assert preserved["balance_remaining"] == pytest.approx(42.0)
        # Marker written at tick-2's now.
        assert float(stale.get("deepseek")) == pytest.approx(2_000.0)

        # Endpoint cache-hit path surfaces stale_since.
        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            fetchers={}, key_resolver=lambda p: None,
        )
        assert out["status"] == "ok"
        assert out["source"] == "cache"
        assert out["balance_remaining"] == pytest.approx(42.0)
        assert out["stale_since"] == pytest.approx(2_000.0)

    @respx.mock
    async def test_endpoint_cache_miss_5xx_writes_marker(self):
        """Cache-miss + 5xx at the endpoint layer: nothing to serve,
        so returns ``error`` — but the marker is written so a
        concurrent worker that succeeded earlier (cache-seeded between
        our miss-read and this request's fetch) still gets a coherent
        ``stale_since`` on the next cache-hit."""
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(502, text="bad gateway"),
        )
        kv = _kv("cache")
        stale = _kv("stale")

        out = await endpoint.resolve_balance(
            "openrouter",
            kv=kv, stale_kv=stale,
            key_resolver=_key_resolver({"openrouter": "sk-valid"}),
            now=9_999.0,
        )

        assert out["status"] == "error"
        assert "fetch failed" in out["message"]
        assert kv.get("openrouter") == ""
        assert float(stale.get("openrouter")) == pytest.approx(9_999.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Batch endpoint aggregation — mixed HTTP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_SUPPORTED = {"deepseek", "openrouter"}
_UNSUPPORTED = {
    "anthropic", "google", "groq", "ollama",
    "openai", "together", "xai",
}
_EXPECTED_PROVIDERS = _SUPPORTED | _UNSUPPORTED


class TestIntegrationBatchAggregation:
    """``resolve_all_balances`` under real HTTP for supported providers:
    the batch payload aggregates two live fetches + seven static
    ``unsupported`` envelopes into a single sorted ``providers``
    array."""

    @respx.mock
    async def test_batch_aggregates_mixed_success_unsupported(self):
        """Both supported providers return 200 → batch contains 2 ok
        envelopes + 7 unsupported envelopes, ordered alphabetically."""
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=_deepseek_ok_body(
                balance=12.5, granted=5.0,
            )),
        )
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=_openrouter_ok_body(
                remaining=3.3, limit=10.0, usage=6.7,
            )),
        )
        kv = _kv("cache")
        stale = _kv("stale")

        out = await endpoint.resolve_all_balances(
            kv=kv, stale_kv=stale,
            key_resolver=_key_resolver(),
        )

        assert set(out.keys()) == {"providers"}
        envelopes = out["providers"]
        returned = [e["provider"] for e in envelopes]
        assert returned == sorted(_EXPECTED_PROVIDERS), (
            "Batch ordering must be alphabetical across all 9 providers"
        )

        lookup = {e["provider"]: e for e in envelopes}
        # Supported — live fetch via real HTTP layer.
        assert lookup["deepseek"]["status"] == "ok"
        assert lookup["deepseek"]["source"] == "live"
        assert lookup["deepseek"]["balance_remaining"] == pytest.approx(12.5)
        assert lookup["openrouter"]["status"] == "ok"
        assert lookup["openrouter"]["source"] == "live"
        assert lookup["openrouter"]["balance_remaining"] == pytest.approx(3.3)
        # Unsupported — static envelope, no HTTP touched.
        for name in _UNSUPPORTED:
            assert lookup[name]["status"] == "unsupported"
            assert lookup[name]["reason"] == (
                "provider does not expose a public balance API "
                "with API-key authentication"
            )
            assert "stale_since" not in lookup[name]

    @respx.mock
    async def test_batch_aggregates_mixed_success_authfail_5xx(self):
        """Canonical three-way: deepseek 200, openrouter 500 → batch
        contains 1 ok + 1 error + 7 unsupported."""
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=_deepseek_ok_body(
                balance=1.0, granted=0.5,
            )),
        )
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(500, text="boom"),
        )
        kv = _kv("cache")
        stale = _kv("stale")

        out = await endpoint.resolve_all_balances(
            kv=kv, stale_kv=stale,
            key_resolver=_key_resolver(),
        )

        lookup = {e["provider"]: e for e in out["providers"]}
        assert lookup["deepseek"]["status"] == "ok"
        assert lookup["deepseek"]["source"] == "live"
        assert lookup["openrouter"]["status"] == "error"
        assert "fetch failed" in lookup["openrouter"]["message"]
        # openrouter cache stays empty; deepseek cache populated.
        assert kv.get("deepseek"), "deepseek success must cache"
        assert kv.get("openrouter") == "", (
            "openrouter 5xx must not create a cache entry"
        )
        # Stale marker written only for the failing provider.
        assert stale.get("openrouter"), (
            "5xx on openrouter must write stale marker"
        )
        assert stale.get("deepseek") == "", (
            "deepseek success must not leave a stale marker"
        )

    @respx.mock
    async def test_batch_both_supported_auth_fail(self):
        """Both supported providers 401/403 → both envelopes carry the
        ``authentication failed`` message; no SharedKV write; no
        stale marker write. Unsupported providers still render."""
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(401, json={"error": "nope"}),
        )
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(403, json={"error": "nope"}),
        )
        kv = _kv("cache")
        stale = _kv("stale")

        out = await endpoint.resolve_all_balances(
            kv=kv, stale_kv=stale,
            key_resolver=_key_resolver(),
        )

        lookup = {e["provider"]: e for e in out["providers"]}
        assert lookup["deepseek"]["status"] == "error"
        assert "authentication failed" in lookup["deepseek"]["message"]
        assert lookup["openrouter"]["status"] == "error"
        assert "authentication failed" in lookup["openrouter"]["message"]
        # Unsupported still there.
        for name in _UNSUPPORTED:
            assert lookup[name]["status"] == "unsupported"
        # No cache, no marker.
        assert kv.get("deepseek") == ""
        assert kv.get("openrouter") == ""
        assert stale.get("deepseek") == ""
        assert stale.get("openrouter") == ""

    @respx.mock
    async def test_batch_cached_then_second_call_is_all_cache(self):
        """After a warm-up batch, a second batch hits cache for the
        supported providers and does not re-issue HTTP."""
        ds_route = respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=_deepseek_ok_body()),
        )
        or_route = respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=_openrouter_ok_body()),
        )
        kv = _kv("cache")
        stale = _kv("stale")
        resolver = _key_resolver()

        await endpoint.resolve_all_balances(
            kv=kv, stale_kv=stale, key_resolver=resolver,
        )
        assert ds_route.call_count == 1
        assert or_route.call_count == 1

        out2 = await endpoint.resolve_all_balances(
            kv=kv, stale_kv=stale, key_resolver=resolver,
        )
        # No additional HTTP calls — second round served from cache.
        assert ds_route.call_count == 1
        assert or_route.call_count == 1

        lookup = {e["provider"]: e for e in out2["providers"]}
        assert lookup["deepseek"]["source"] == "cache"
        assert lookup["openrouter"]["source"] == "cache"

    @respx.mock
    async def test_batch_cache_hit_with_stale_marker_propagates(self):
        """Refresher has written a prior snapshot + stale marker for
        deepseek; batch endpoint reads both and surfaces
        ``stale_since`` in the deepseek envelope while openrouter
        remains fresh."""
        # No HTTP routes defined — we seed the cache directly and
        # assert the batch never touches the wire for cache-hit
        # providers.
        kv = _kv("cache")
        stale = _kv("stale")
        kv.set("deepseek", lbr._serialise_balance({
            "currency": "USD",
            "balance_remaining": 55.0,
            "granted_total": 10.0,
            "usage_total": None,
            "last_refreshed_at": 100.0,
            "raw": {"seeded": True},
        }))
        kv.set("openrouter", lbr._serialise_balance({
            "currency": "USD",
            "balance_remaining": 3.0,
            "granted_total": 10.0,
            "usage_total": 7.0,
            "last_refreshed_at": 200.0,
            "raw": {"seeded": True},
        }))
        lbr._write_stale_marker(stale, "deepseek", 9_876.5)

        out = await endpoint.resolve_all_balances(
            kv=kv, stale_kv=stale,
            # Resolver returns None so any cache-miss (there should be
            # none) collapses to the 'no key configured' error — making
            # the test loud if cache-hit ever silently degrades.
            key_resolver=lambda p: None,
        )

        lookup = {e["provider"]: e for e in out["providers"]}
        assert lookup["deepseek"]["status"] == "ok"
        assert lookup["deepseek"]["source"] == "cache"
        assert lookup["deepseek"]["stale_since"] == pytest.approx(9_876.5)
        assert lookup["openrouter"]["status"] == "ok"
        assert lookup["openrouter"]["source"] == "cache"
        assert lookup["openrouter"]["stale_since"] is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Unsupported provider behaviour
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIntegrationUnsupportedProvider:
    """Unsupported providers (anthropic / google / groq / ollama /
    openai / together / xai) never attempt HTTP and never touch the
    SharedKV or stale namespaces — regardless of whether supported
    providers are returning 200, 5xx, or anything else, and
    regardless of what the resolver says."""

    @respx.mock
    @pytest.mark.parametrize("provider", sorted(_UNSUPPORTED))
    async def test_unsupported_single_endpoint_no_http(self, provider):
        # No respx route needed — if the endpoint ever tried to hit
        # the network, respx would 500 the call (by default) and the
        # status would be ``error`` instead of ``unsupported``.
        kv = _kv("cache")
        stale = _kv("stale")

        out = await endpoint.resolve_balance(
            provider,
            kv=kv, stale_kv=stale,
            # Resolver would return a key if it were consulted — locking
            # the "unsupported short-circuits before resolver" invariant.
            key_resolver=lambda p: "sk-should-not-be-read",
        )

        assert out["status"] == "unsupported"
        assert out["provider"] == provider
        assert out["reason"] == (
            "provider does not expose a public balance API "
            "with API-key authentication"
        )
        # Nothing written for unsupported providers.
        assert kv.get(provider) == ""
        assert stale.get(provider) == ""

    @respx.mock
    async def test_refresher_iterates_supported_only(self):
        """The refresher's default fetchers dict is
        ``SUPPORTED_BALANCE_PROVIDERS`` — it must not surface entries
        for unsupported providers at all (no auth_fail / no_key noise
        for providers that have no balance API)."""
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=_deepseek_ok_body()),
        )
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=_openrouter_ok_body()),
        )
        kv = _kv("cache")
        stale = _kv("stale")
        state: dict[str, lbr._ProviderBackoff] = {}

        outcomes = await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=100.0,
            # Resolver returns a key for every provider — if the
            # refresher queried unsupported providers we'd see them in
            # outcomes. We want only {"deepseek", "openrouter"}.
            key_resolver=lambda p: f"sk-{p}",
            kv=kv, stale_kv=stale,
        )

        assert set(outcomes) == {"deepseek", "openrouter"}, (
            "Refresher must iterate SUPPORTED_BALANCE_PROVIDERS only"
        )
        # Unsupported providers never touched the cache namespace.
        for name in _UNSUPPORTED:
            assert kv.get(name) == ""
            assert stale.get(name) == ""

    async def test_unsupported_envelope_in_batch_never_calls_fetcher(self):
        """Batch path: inject a fetcher keyed to an unsupported
        provider name to prove the ``is_balance_supported`` gate fires
        before fetcher dispatch — the fetcher must not run."""
        calls: list[str] = []

        async def spy_fetcher(api_key: str, **_: Any):
            calls.append(api_key)
            return None

        # Even with spy_fetcher registered for anthropic, the
        # unsupported gate must short-circuit and ignore it.
        out = await endpoint.resolve_all_balances(
            kv=_kv("cache"), stale_kv=_kv("stale"),
            fetchers={
                "deepseek": spy_fetcher,
                "openrouter": spy_fetcher,
                "anthropic": spy_fetcher,  # fetcher present but
                # must be ignored by the is_balance_supported gate.
            },
            key_resolver=lambda p: "sk",
        )

        lookup = {e["provider"]: e for e in out["providers"]}
        assert lookup["anthropic"]["status"] == "unsupported"
        # deepseek + openrouter went through the fetcher (auth_fail
        # because the spy returns None); anthropic never did.
        assert calls == ["sk", "sk"], (
            "Only the two supported providers invoke the fetcher; "
            "anthropic's fetcher entry must be ignored"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cross-flow: refresher writes → endpoint reads
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIntegrationRefresherToEndpoint:
    """Regression guard: the refresher's write format and the
    endpoint's read format must stay lock-step. If either side renames
    a BalanceInfo field or changes the JSON shape, the assertions
    below fail."""

    @respx.mock
    async def test_refresher_write_is_endpoint_readable(self):
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=_deepseek_ok_body(
                balance=3.14, granted=1.0,
            )),
        )
        kv = _kv("cache")
        stale = _kv("stale")
        state: dict[str, lbr._ProviderBackoff] = {}

        await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=123.0,
            key_resolver=_key_resolver({"deepseek": "sk-valid"}),
            kv=kv, stale_kv=stale,
        )

        envelope = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            fetchers={}, key_resolver=lambda p: None,
        )

        assert envelope == {
            "status": "ok",
            "provider": "deepseek",
            "currency": "USD",
            "balance_remaining": pytest.approx(3.14),
            "granted_total": pytest.approx(1.0),
            "usage_total": None,
            "last_refreshed_at": pytest.approx(123.0),
            "source": "cache",
            "raw": {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "USD",
                        "total_balance": "3.14",
                        "granted_balance": "1.00",
                        "topped_up_balance": "2.14",
                    },
                ],
            },
            "stale_since": None,
        }

    @respx.mock
    async def test_refresher_then_5xx_then_endpoint_renders_stale(self):
        """Concatenated lifecycle: success → failure → read. Anchors
        the cross-layer story the Z.2 checkbox asks us to verify."""
        ds_route = respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=_deepseek_ok_body(
                balance=88.8, granted=10.0,
            )),
        )
        kv = _kv("cache")
        stale = _kv("stale")
        state: dict[str, lbr._ProviderBackoff] = {}

        # Tick 1 — success.
        await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=1_000.0,
            key_resolver=_key_resolver({"deepseek": "sk-valid"}),
            kv=kv, stale_kv=stale,
        )

        # Flip the mock to 503 and move the clock past the backoff
        # window so tick-2 actually fires.
        ds_route.mock(return_value=httpx.Response(503, text="sorry"))
        await lbr.refresh_once(
            state=state, base_interval_s=600.0, now=5_000.0,
            key_resolver=_key_resolver({"deepseek": "sk-valid"}),
            kv=kv, stale_kv=stale,
        )

        # Endpoint cache-hit carries the preserved balance + the
        # stale marker recorded at tick-2's timestamp.
        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv, stale_kv=stale,
            fetchers={}, key_resolver=lambda p: None,
        )
        assert out["status"] == "ok"
        assert out["source"] == "cache"
        assert out["balance_remaining"] == pytest.approx(88.8)
        assert out["stale_since"] == pytest.approx(5_000.0)
