"""Z.2 (#291) — Unit tests for ``backend.llm_balance`` fetchers.

Covers the two provider fetchers shipped by the first Z.2 checkbox
(``fetch_balance_deepseek`` + ``fetch_balance_openrouter``) and the
``SUPPORTED_BALANCE_PROVIDERS`` registry helpers. Future Z.2
checkboxes (background refresh task, SharedKV write, batch endpoint,
unsupported-provider handling) get their own test files —
``test_llm_balance_fetchers.py`` deliberately stops at the
fetcher contract.

What's locked
─────────────
1. **Happy-path normalisation** — realistic vendor responses round-trip
   through the fetcher into a ``BalanceInfo`` dict with the expected
   numeric and currency fields. Vendor payload preserved verbatim in
   ``raw``.
2. **Auth failure → ``None``** — HTTP 401 / 403 collapse to ``None``
   (cache layer treats this as "do not cache; key may be rotating").
3. **5xx + transport errors → ``BalanceFetchError``** — caller can
   distinguish "provider down → keep cached value" from "key
   revoked → drop cache".
4. **Empty / missing api_key short-circuits to ``None``** without
   an HTTP call.
5. **Malformed but-200 response → ``BalanceFetchError``** so the cache
   layer doesn't store bogus zeros.
6. **OpenRouter ``limit_remaining`` fallback** to ``limit - usage``
   when vendor omits it (schema-drift insurance).
7. **DeepSeek picks the first ``balance_infos`` entry** (multi-currency
   accounts) and preserves the full list under ``raw``.
8. **Bearer header + URL** asserted to lock the wire contract — if
   either provider URL or auth scheme drifts, the test fails before
   prod sees the bad request.

Module-global audit (SOP Step 1, 2026-04-21 rule)
─────────────────────────────────────────────────
Zero module-global state introduced by these tests. ``respx.mock``
intercepts at the transport layer per-test; nothing leaks between
cases.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from backend import llm_balance
from backend.llm_balance import (
    BalanceFetchError,
    SUPPORTED_BALANCE_PROVIDERS,
    fetch_balance_deepseek,
    fetch_balance_openrouter,
    is_balance_supported,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures / helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_DEEPSEEK_URL = "https://api.deepseek.com/user/balance"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/auth/key"


def _deepseek_ok_body() -> dict[str, Any]:
    """Realistic DeepSeek /user/balance response shape."""
    return {
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


def _openrouter_ok_body() -> dict[str, Any]:
    """Realistic OpenRouter /auth/key response shape."""
    return {
        "data": {
            "label": "sk-or-v1-...",
            "usage": 4.42,
            "limit": 10.0,
            "limit_remaining": 5.58,
            "is_free_tier": False,
            "rate_limit": {
                "requests": 200,
                "interval": "10s",
            },
        },
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRegistry:

    def test_supported_provider_set(self):
        assert set(SUPPORTED_BALANCE_PROVIDERS) == {"deepseek", "openrouter"}

    def test_registry_callables_match_module(self):
        assert SUPPORTED_BALANCE_PROVIDERS["deepseek"] is fetch_balance_deepseek
        assert SUPPORTED_BALANCE_PROVIDERS["openrouter"] is fetch_balance_openrouter

    @pytest.mark.parametrize("name", ["deepseek", "openrouter"])
    def test_is_supported_true(self, name):
        assert is_balance_supported(name) is True

    @pytest.mark.parametrize(
        "name",
        ["anthropic", "google", "openai", "xai",
         "groq", "together", "ollama", ""],
    )
    def test_is_supported_false(self, name):
        assert is_balance_supported(name) is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DeepSeek
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFetchDeepSeek:

    @respx.mock
    async def test_happy_path_normalises_balance(self):
        body = _deepseek_ok_body()
        route = respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=body),
        )

        info = await fetch_balance_deepseek(
            "sk-deepseek-test", now=1_700_000_000.0,
        )

        assert route.called
        assert info is not None
        assert info["currency"] == "USD"
        assert info["balance_remaining"] == 12.5
        assert info["granted_total"] == 5.0
        # DeepSeek does not report cumulative usage.
        assert info["usage_total"] is None
        assert info["last_refreshed_at"] == 1_700_000_000.0
        assert info["raw"] == body

    @respx.mock
    async def test_sends_bearer_header(self):
        route = respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=_deepseek_ok_body()),
        )
        await fetch_balance_deepseek("sk-deepseek-bearer-check")
        assert route.called
        sent = route.calls.last.request
        assert sent.headers.get("Authorization") == "Bearer sk-deepseek-bearer-check"
        assert sent.headers.get("Accept") == "application/json"

    @respx.mock
    async def test_picks_first_balance_info_preserves_raw(self):
        body = {
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "100",
                 "granted_balance": "0", "topped_up_balance": "100"},
                {"currency": "CNY", "total_balance": "50",
                 "granted_balance": "25", "topped_up_balance": "25"},
            ],
        }
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=body),
        )
        info = await fetch_balance_deepseek("sk-multi")
        assert info is not None
        assert info["currency"] == "USD"
        assert info["balance_remaining"] == 100.0
        # Multi-currency accounts: full vendor list still in raw so
        # operator can see the second entry.
        assert info["raw"] == body
        assert len(info["raw"]["balance_infos"]) == 2

    @respx.mock
    async def test_coerces_string_amounts(self):
        body = {
            "is_available": True,
            "balance_infos": [
                {"currency": "USD",
                 "total_balance": "0.85",
                 "granted_balance": "0.50",
                 "topped_up_balance": "0.35"},
            ],
        }
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=body),
        )
        info = await fetch_balance_deepseek("sk-string-amounts")
        assert info is not None
        assert info["balance_remaining"] == pytest.approx(0.85)
        assert info["granted_total"] == pytest.approx(0.50)

    @respx.mock
    async def test_unparseable_amounts_become_none(self):
        body = {
            "is_available": True,
            "balance_infos": [
                {"currency": "USD",
                 "total_balance": "not-a-number",
                 "granted_balance": None,
                 "topped_up_balance": "0"},
            ],
        }
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json=body),
        )
        info = await fetch_balance_deepseek("sk-bad-amount")
        assert info is not None
        assert info["balance_remaining"] is None
        assert info["granted_total"] is None

    @respx.mock
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_failure_returns_none(self, status):
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(
                status, json={"error": {"message": "Authentication Fails"}},
            ),
        )
        result = await fetch_balance_deepseek("sk-revoked")
        assert result is None

    @respx.mock
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_5xx_raises_balance_fetch_error(self, status):
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(status, text="upstream broken"),
        )
        with pytest.raises(BalanceFetchError) as exc_info:
            await fetch_balance_deepseek("sk-server-down")
        assert exc_info.value.provider == "deepseek"
        assert str(status) in exc_info.value.reason

    @respx.mock
    async def test_4xx_other_than_auth_raises(self):
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(429, text="too many requests"),
        )
        with pytest.raises(BalanceFetchError):
            await fetch_balance_deepseek("sk-rate-limited")

    @respx.mock
    async def test_transport_error_raises_balance_fetch_error(self):
        respx.get(_DEEPSEEK_URL).mock(
            side_effect=httpx.ConnectTimeout("upstream unreachable"),
        )
        with pytest.raises(BalanceFetchError) as exc_info:
            await fetch_balance_deepseek("sk-net-down")
        assert exc_info.value.provider == "deepseek"
        assert "transport error" in exc_info.value.reason

    @respx.mock
    async def test_non_json_body_raises(self):
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(
                200, text="<html>nginx error page</html>",
            ),
        )
        with pytest.raises(BalanceFetchError):
            await fetch_balance_deepseek("sk-bad-body")

    @respx.mock
    async def test_missing_balance_infos_raises(self):
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(200, json={"is_available": False}),
        )
        with pytest.raises(BalanceFetchError) as exc_info:
            await fetch_balance_deepseek("sk-empty")
        assert "balance_infos" in exc_info.value.reason

    @respx.mock
    async def test_empty_balance_infos_list_raises(self):
        respx.get(_DEEPSEEK_URL).mock(
            return_value=httpx.Response(
                200, json={"is_available": True, "balance_infos": []},
            ),
        )
        with pytest.raises(BalanceFetchError):
            await fetch_balance_deepseek("sk-empty-list")

    async def test_empty_api_key_short_circuits(self):
        # No respx.mock — if the fetcher tried to make a network call
        # against the real DeepSeek API the test would either flake
        # or fail with a real auth error. Empty key must short-circuit
        # before any HTTP traffic.
        result = await fetch_balance_deepseek("")
        assert result is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OpenRouter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFetchOpenRouter:

    @respx.mock
    async def test_happy_path_normalises_balance(self):
        body = _openrouter_ok_body()
        route = respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=body),
        )
        info = await fetch_balance_openrouter(
            "sk-or-v1-test", now=1_700_000_001.0,
        )
        assert route.called
        assert info is not None
        assert info["currency"] == "USD"
        assert info["balance_remaining"] == pytest.approx(5.58)
        assert info["granted_total"] == 10.0
        assert info["usage_total"] == pytest.approx(4.42)
        assert info["last_refreshed_at"] == 1_700_000_001.0
        assert info["raw"] == body

    @respx.mock
    async def test_sends_bearer_header(self):
        route = respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=_openrouter_ok_body()),
        )
        await fetch_balance_openrouter("sk-or-bearer-check")
        sent = route.calls.last.request
        assert sent.headers.get("Authorization") == "Bearer sk-or-bearer-check"
        assert sent.headers.get("Accept") == "application/json"

    @respx.mock
    async def test_derives_remaining_when_omitted(self):
        body = {
            "data": {
                "label": "sk-or-v1-...",
                "usage": 3.5,
                "limit": 10.0,
                # limit_remaining absent → derive from limit - usage
                "is_free_tier": False,
            },
        }
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=body),
        )
        info = await fetch_balance_openrouter("sk-or-derive")
        assert info is not None
        assert info["balance_remaining"] == pytest.approx(6.5)
        assert info["granted_total"] == 10.0
        assert info["usage_total"] == pytest.approx(3.5)

    @respx.mock
    async def test_unlimited_account_remaining_stays_none(self):
        body = {
            "data": {
                "label": "sk-or-v1-...",
                "usage": 3.5,
                "limit": None,
                "limit_remaining": None,
                "is_free_tier": False,
            },
        }
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=body),
        )
        info = await fetch_balance_openrouter("sk-or-unlimited")
        assert info is not None
        assert info["balance_remaining"] is None
        assert info["granted_total"] is None
        assert info["usage_total"] == pytest.approx(3.5)

    @respx.mock
    async def test_derived_remaining_floored_at_zero(self):
        body = {
            "data": {
                # usage > limit (e.g. metering lag) → don't return
                # a negative balance, clamp to zero.
                "usage": 12.0,
                "limit": 10.0,
                "is_free_tier": False,
            },
        }
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=body),
        )
        info = await fetch_balance_openrouter("sk-or-overshoot")
        assert info is not None
        assert info["balance_remaining"] == 0.0

    @respx.mock
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_failure_returns_none(self, status):
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(
                status, json={"error": {"message": "Invalid API key"}},
            ),
        )
        result = await fetch_balance_openrouter("sk-or-revoked")
        assert result is None

    @respx.mock
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_5xx_raises_balance_fetch_error(self, status):
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(status, text="bad gateway"),
        )
        with pytest.raises(BalanceFetchError) as exc_info:
            await fetch_balance_openrouter("sk-or-server-down")
        assert exc_info.value.provider == "openrouter"
        assert str(status) in exc_info.value.reason

    @respx.mock
    async def test_transport_error_raises_balance_fetch_error(self):
        respx.get(_OPENROUTER_URL).mock(
            side_effect=httpx.ReadTimeout("read timed out"),
        )
        with pytest.raises(BalanceFetchError) as exc_info:
            await fetch_balance_openrouter("sk-or-timeout")
        assert exc_info.value.provider == "openrouter"
        assert "transport error" in exc_info.value.reason

    @respx.mock
    async def test_missing_data_envelope_raises(self):
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json={"unexpected": "shape"}),
        )
        with pytest.raises(BalanceFetchError) as exc_info:
            await fetch_balance_openrouter("sk-or-bad-shape")
        assert "data envelope" in exc_info.value.reason

    @respx.mock
    async def test_non_json_body_raises(self):
        respx.get(_OPENROUTER_URL).mock(
            return_value=httpx.Response(200, text="not json at all"),
        )
        with pytest.raises(BalanceFetchError):
            await fetch_balance_openrouter("sk-or-html")

    async def test_empty_api_key_short_circuits(self):
        # As above: no respx.mock — empty key MUST not hit the wire.
        result = await fetch_balance_openrouter("")
        assert result is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Module surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestModuleSurface:

    def test_balance_info_typed_dict_keys(self):
        # BalanceInfo is total=False so we can't enforce required keys
        # at the type level — instead document the canonical key set
        # so a future refactor that drops one breaks here visibly.
        expected_keys = {
            "currency", "balance_remaining", "granted_total",
            "usage_total", "last_refreshed_at", "raw",
        }
        # __annotations__ on a TypedDict reflects declared keys.
        assert set(llm_balance.BalanceInfo.__annotations__) == expected_keys

    def test_balance_fetch_error_carries_provider_and_reason(self):
        exc = BalanceFetchError("deepseek", "broken pipe")
        assert exc.provider == "deepseek"
        assert exc.reason == "broken pipe"
        assert "deepseek" in str(exc)
        assert "broken pipe" in str(exc)
