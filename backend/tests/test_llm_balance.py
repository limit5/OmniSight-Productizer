"""Z.5 (#294) — Regression test net for the LLM-balance stack.

Slim canonical test file that locks the Z-priority "operator can see real
provider balance" contract against drift. The comprehensive per-module
contract suites live in ``test_llm_balance_fetchers.py`` (fetchers),
``test_llm_balance_refresher.py`` (background refresh), and
``test_llm_balance_endpoint.py`` (HTTP endpoint + batch). This file
targets the exact regression matrix spelled out in the Z.5 checkbox:

* **DeepSeek + OpenRouter** — 3 scenarios each (``ok`` / ``auth_fail`` /
  ``5xx``) exercised against the real fetcher coroutines via ``respx``
  so the vendor URL + Bearer auth + response envelope normalisation all
  stay wired.
* **Unsupported provider** — 1 scenario confirming the endpoint layer
  short-circuits to the static ``{"status": "unsupported"}`` envelope
  (no fetch attempt, no SharedKV touch) for providers absent from the
  ``SUPPORTED_BALANCE_PROVIDERS`` registry.
* **SharedKV cache hit / miss** — lock the branch that separates
  "serve cached snapshot, skip fetch" from "cache empty → call fetcher
  once, cache the result, subsequent hit is cache-source". The
  invariant this protects is the operator-visible behaviour: a cold
  boot fills the cache on first dashboard load, a warm boot does not
  re-hammer the vendor on every page refresh.

Module-global audit (SOP Step 1, 2026-04-21 rule)
─────────────────────────────────────────────────
Each test either uses ``respx.mock`` (transport interception, no
process-wide state) or constructs a fresh ``SharedKV`` namespace via
``uuid4`` so the in-memory fallback dict cannot cross-pollute cases.
No fixture mutates ``backend.config.settings`` nor the production
``SharedKV`` namespaces (``provider_balance`` / ``provider_balance_stale``).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import respx

from backend.llm_balance import (
    BalanceFetchError,
    BalanceInfo,
    fetch_balance_deepseek,
    fetch_balance_openrouter,
    is_balance_supported,
)
from backend.llm_balance_refresher import _serialise_balance
from backend.routers import llm_balance as endpoint
from backend.shared_state import SharedKV


pytestmark = pytest.mark.asyncio


# Vendor URLs — kept inline rather than imported from the module so a
# silent URL drift (e.g. someone moves to ``api.deepseek.com/v1/...``)
# breaks this test before production hits a vendor 404.
_DEEPSEEK_URL = "https://api.deepseek.com/user/balance"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/auth/key"


def _fresh_kv() -> SharedKV:
    """Per-test SharedKV namespace — isolates the in-memory fallback
    dict from every other test's writes."""
    return SharedKV(f"z5_test_balance_{uuid.uuid4().hex[:10]}")


def _sample_balance(amount: float = 12.5) -> BalanceInfo:
    return BalanceInfo(
        currency="USD",
        balance_remaining=amount,
        granted_total=amount + 5.0,
        usage_total=amount - 1.0,
        last_refreshed_at=1_700_000_000.0,
        raw={"source": "z5-regression"},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DeepSeek — ok / auth fail / 5xx
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDeepSeekFetcher:
    """Three canonical DeepSeek scenarios: success, auth failure, 5xx."""

    @respx.mock
    async def test_ok_returns_normalised_balance(self):
        body = {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "12.50",
                    "granted_balance": "5.00",
                    "topped_up_balance": "7.50",
                },
            ],
        }
        route = respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=body),
        )

        info = await fetch_balance_deepseek(
            "sk-deepseek-z5", now=1_700_000_000.0,
        )

        assert route.called
        # Bearer header wired correctly — protects the auth-scheme
        # contract from drifting back to "api-key" / querystring etc.
        sent = route.calls.last.request
        assert sent.headers.get("Authorization") == "Bearer sk-deepseek-z5"

        assert info is not None
        assert info["currency"] == "USD"
        assert info["balance_remaining"] == 12.5
        assert info["granted_total"] == 5.0
        # DeepSeek does not report cumulative usage — keep as None so
        # the dashboard renders "—" not "0 spent".
        assert info["usage_total"] is None
        assert info["last_refreshed_at"] == 1_700_000_000.0
        assert info["raw"] == body

    @respx.mock
    async def test_auth_fail_returns_none(self):
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(
                401, json={"error": {"message": "Authentication Fails"}},
            ),
        )
        # 401/403 collapse to None so the cache layer can skip the
        # write (operator may be mid-rotation). Regression guard: if
        # the fetcher ever starts raising instead, the refresher's
        # backoff semantics break.
        assert await fetch_balance_deepseek("sk-revoked") is None

    @respx.mock
    async def test_5xx_raises_balance_fetch_error(self):
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(502, text="bad gateway"),
        )
        with pytest.raises(BalanceFetchError) as exc_info:
            await fetch_balance_deepseek("sk-provider-down")
        assert exc_info.value.provider == "deepseek"
        # 5xx must surface the status code in the reason so the
        # refresher's stale-marker + operator log lines are actionable.
        assert "502" in exc_info.value.reason


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OpenRouter — ok / auth fail / 5xx
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestOpenRouterFetcher:
    """Three canonical OpenRouter scenarios: success, auth failure, 5xx."""

    @respx.mock
    async def test_ok_returns_normalised_balance(self):
        body = {
            "data": {
                "label": "sk-or-v1-...",
                "usage": 4.42,
                "limit": 10.0,
                "limit_remaining": 5.58,
                "is_free_tier": False,
                "rate_limit": {"requests": 200, "interval": "10s"},
            },
        }
        route = respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=body),
        )

        info = await fetch_balance_openrouter(
            "sk-or-z5", now=1_700_000_001.0,
        )

        assert route.called
        sent = route.calls.last.request
        assert sent.headers.get("Authorization") == "Bearer sk-or-z5"

        assert info is not None
        # OpenRouter always prices in USD — we set it explicitly so
        # renderers don't have to special-case a missing currency.
        assert info["currency"] == "USD"
        assert info["balance_remaining"] == pytest.approx(5.58)
        assert info["granted_total"] == 10.0
        assert info["usage_total"] == pytest.approx(4.42)
        assert info["last_refreshed_at"] == 1_700_000_001.0
        assert info["raw"] == body

    @respx.mock
    async def test_auth_fail_returns_none(self):
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(
                403, json={"error": {"message": "Invalid API key"}},
            ),
        )
        assert await fetch_balance_openrouter("sk-or-revoked") is None

    @respx.mock
    async def test_5xx_raises_balance_fetch_error(self):
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(503, text="service unavailable"),
        )
        with pytest.raises(BalanceFetchError) as exc_info:
            await fetch_balance_openrouter("sk-or-down")
        assert exc_info.value.provider == "openrouter"
        assert "503" in exc_info.value.reason


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Unsupported provider
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestUnsupportedProvider:
    """Providers without a public API-key-auth balance endpoint
    (Anthropic / OpenAI / Google / xAI / Groq / Together / Ollama)
    collapse to the static ``unsupported`` envelope without any HTTP
    call or cache touch."""

    async def test_unsupported_provider_returns_static_envelope(self):
        kv = _fresh_kv()
        fetcher_calls: list[str] = []

        async def never_call_me(api_key: str, **_: Any):
            fetcher_calls.append(api_key)
            return _sample_balance()

        out = await endpoint.resolve_balance(
            "anthropic",
            kv=kv,
            # Inject a fetcher to prove the unsupported branch
            # short-circuits BEFORE any fetcher dispatch.
            fetchers={"anthropic": never_call_me},
            # Same for the key resolver — unsupported providers must
            # not trigger a Settings lookup either.
            key_resolver=lambda p: "sk-should-not-read",
        )

        assert out == {
            "status": "unsupported",
            "provider": "anthropic",
            "reason": (
                "provider does not expose a public balance API "
                "with API-key authentication"
            ),
        }
        assert fetcher_calls == [], (
            "Unsupported branch must not invoke the fetcher"
        )
        assert kv.get("anthropic") == "", (
            "Unsupported branch must not touch SharedKV"
        )
        # Module-level helper agrees.
        assert is_balance_supported("anthropic") is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SharedKV cache hit / miss
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSharedKVCacheLogic:
    """The operator-visible contract: warm dashboard loads hit cache
    (no vendor call); cold dashboard loads trigger exactly one fetch
    and populate the cache so subsequent loads are warm too."""

    async def test_cache_hit_serves_from_sharedkv_without_fetching(self):
        kv = _fresh_kv()
        # Seed the cache via the same serialiser the refresher uses —
        # guards against a silent divergence between write path
        # (refresher) and read path (endpoint).
        kv.set(
            "deepseek",
            _serialise_balance(_sample_balance(amount=99.0)),
        )

        fetcher_calls: list[str] = []

        async def fetcher(api_key: str, **_: Any):
            fetcher_calls.append(api_key)
            return _sample_balance()

        out = await endpoint.resolve_balance(
            "deepseek",
            kv=kv,
            fetchers={"deepseek": fetcher},
            key_resolver=lambda p: "sk-warm",
        )

        assert out["status"] == "ok"
        assert out["source"] == "cache"
        assert out["balance_remaining"] == 99.0
        assert out["currency"] == "USD"
        assert fetcher_calls == [], (
            "Cache hit must not call the fetcher — operator would "
            "pay a vendor round-trip on every dashboard load"
        )

    async def test_cache_miss_fetches_once_writes_and_reheats(self):
        """Cold-start → warm-start transition: empty cache triggers a
        single live fetch whose result lands in SharedKV so the next
        call hits the cache path with ``source='cache'``."""
        kv = _fresh_kv()
        fetcher_calls: list[str] = []

        async def fetcher(api_key: str, **_: Any):
            fetcher_calls.append(api_key)
            return _sample_balance(amount=42.0)

        # First call — cache empty → live fetch.
        first = await endpoint.resolve_balance(
            "openrouter",
            kv=kv,
            fetchers={"openrouter": fetcher},
            key_resolver=lambda p: "sk-cold",
        )
        assert first["status"] == "ok"
        assert first["source"] == "live"
        assert first["balance_remaining"] == 42.0
        assert fetcher_calls == ["sk-cold"], (
            "Cache miss must fetch exactly once with the resolved key"
        )

        # Cache now populated — confirm the write landed.
        raw = kv.get("openrouter")
        assert raw, "Live fetch must write its result into SharedKV"
        assert json.loads(raw)["balance_remaining"] == 42.0

        # Second call — cache hit, fetcher NOT re-invoked.
        second = await endpoint.resolve_balance(
            "openrouter",
            kv=kv,
            fetchers={"openrouter": fetcher},
            key_resolver=lambda p: "sk-cold",
        )
        assert second["status"] == "ok"
        assert second["source"] == "cache"
        assert second["balance_remaining"] == 42.0
        assert fetcher_calls == ["sk-cold"], (
            "Warm cache must short-circuit before the fetcher — "
            "dashboard polling shouldn't burn vendor rate-limit"
        )
