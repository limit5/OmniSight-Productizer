"""Z.2 (#291) — Per-provider balance fetchers.

Two LLM providers expose a public balance API gated by the same API
key the application already holds for chat-completion calls:

* **DeepSeek** — ``GET https://api.deepseek.com/user/balance`` returns
  ``{is_available: bool, balance_infos: [{currency, total_balance,
  granted_balance, topped_up_balance}]}``. Multi-currency accounts get
  one entry per currency; single-currency accounts (the common case)
  return a one-element list. Documented at
  ``platform.deepseek.com/api-docs/api/get-user-balance``.
* **OpenRouter** — ``GET https://openrouter.ai/api/v1/auth/key``
  returns ``{data: {label, usage, limit, limit_remaining, is_free_tier,
  rate_limit: {...}}}``. ``limit`` is the credit cap in USD; ``usage``
  is cumulative spend; ``limit_remaining`` is the live remainder.
  Documented at ``openrouter.ai/docs/api-reference/api-keys/get-current-api-key``.

Each fetcher returns a normalised ``BalanceInfo`` dict (see the dataclass
definition below) so callers — the upcoming background refresh task and
the ``GET /runtime/providers/{provider}/balance`` endpoint, both
delivered by later Z.2 checkboxes — can render without per-provider
branching.

Out of scope for this checkbox (delivered by next Z.2 rows):
* Background async task that refreshes every 10 min.
* SharedKV("provider_balance") write.
* The ``/runtime/providers/{provider}/balance`` and batch endpoints.
* The "unsupported" / "stale_since" envelope used by those endpoints —
  the fetchers below return ``None`` on auth failure and re-raise
  ``BalanceFetchError`` on transport / 5xx so the orchestration layer
  can decide whether to fall back to the cache vs surface the error.

Module-global audit (SOP Step 1, 2026-04-21 rule)
─────────────────────────────────────────────────
Zero module-globals. Every fetcher takes an injected ``api_key``,
opens a fresh ``httpx.AsyncClient`` per call (matches the pattern at
``backend/cms/sanity.py:110`` and the sentry mobile adapter), and
returns plain dicts. Cross-worker consistency is moot — fetchers are
pure functions of (api_key, vendor response). The cache layer lives
one level up in the upcoming background task that writes
``SharedKV("provider_balance")``.

Read-after-write audit (SOP Step 1, 2026-04-21 rule)
───────────────────────────────────────────────────
N/A — no writes happen here. Callers either store results in
SharedKV (Redis-coordinated; concurrent writers compose by overwriting
the freshest snapshot) or display them directly.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, TypedDict

import httpx

logger = logging.getLogger(__name__)


# Default timeout: balance APIs are documented to respond well under a
# second; a generous 10 s upper bound keeps the background refresh task
# from hanging a worker if a provider is slow but not yet 5xx.
_DEFAULT_TIMEOUT_SECONDS = 10.0


class BalanceInfo(TypedDict, total=False):
    """Normalised balance snapshot for one provider.

    All numeric fields are in the provider's reported currency unit
    (typically USD; DeepSeek may return CNY for CN-region accounts).
    Fields are optional because providers expose different subsets:

    * ``currency`` — ISO 4217 code (``"USD"``, ``"CNY"``); ``""`` when
      provider does not state one.
    * ``balance_remaining`` — live spendable amount.
    * ``granted_total`` — promotional / trial credit granted by the
      provider; ``None`` for OpenRouter (it reports a single ``limit``).
    * ``usage_total`` — cumulative spend since account creation;
      ``None`` for DeepSeek (it reports a remaining-balance, not a
      cumulative usage figure).
    * ``last_refreshed_at`` — unix epoch seconds when this fetch
      completed (NOT when the provider's snapshot was taken — providers
      do not stamp their own balance responses).
    * ``raw`` — the verbatim provider response body (parsed JSON), kept
      so the dashboard can show a "see raw response" disclosure if the
      operator suspects normalisation drift.
    """

    currency: str
    balance_remaining: float | None
    granted_total: float | None
    usage_total: float | None
    last_refreshed_at: float
    raw: dict[str, Any]


class BalanceFetchError(Exception):
    """Raised when the provider returned a non-auth error (5xx, network
    timeout, malformed JSON). Auth failures (401 / 403) instead resolve
    to ``None`` so the cache layer above can distinguish "key is
    revoked → don't cache, retry on next refresh with possibly-rotated
    key" from "provider is down → keep showing cached value with a
    ``stale_since`` marker"."""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"{provider}: {reason}")
        self.provider = provider
        self.reason = reason


def _coerce_float(value: Any) -> float | None:
    """Best-effort numeric coercion.

    Both providers stringify some monetary fields (DeepSeek returns
    ``"total_balance": "0.85"``; OpenRouter returns ``"usage": 12.34``
    as a number). Treat ``None`` / missing / unparseable as missing
    rather than raising — keeps the snapshot useful when a provider
    rolls out a partial schema change.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _http_get_json(
    provider: str,
    url: str,
    api_key: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> dict[str, Any] | None:
    """Shared HTTP plumbing for the provider fetchers.

    Returns the parsed JSON body, or ``None`` for the auth-failure
    path (HTTP 401 / 403). Raises :class:`BalanceFetchError` on
    transport errors, 5xx, and malformed JSON.

    ``client_factory`` is injectable for tests that want to assert on
    the request shape without going through ``respx``; production
    callers leave it ``None`` so a fresh ``httpx.AsyncClient`` is
    constructed (matches ``backend/cms/*.py`` plumbing — connection
    pooling for these once-per-10-min calls is not worth the global
    client lifecycle).
    """
    if not api_key:
        # Treated as auth failure — same downstream handling as a
        # provider 401. Keeps the caller's branch matrix small.
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    if client_factory is None:
        client_cm = httpx.AsyncClient(timeout=timeout)
    else:
        client_cm = client_factory()

    try:
        async with client_cm as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise BalanceFetchError(
            provider, f"transport error: {type(exc).__name__}: {exc}"
        ) from exc

    status = resp.status_code
    if status in (401, 403):
        # Either the key was never valid or the provider revoked it.
        # The cache layer should not store this — the operator may
        # rotate the key and we want the next refresh to pick it up
        # cleanly without a stale "auth_failed" envelope sticking
        # around.
        return None

    if status >= 500:
        raise BalanceFetchError(
            provider, f"provider returned {status}"
        )

    if status >= 400:
        # 4xx other than auth → treat as fetcher error; the cache
        # layer will surface it but not cache it (same rationale as
        # auth failure).
        raise BalanceFetchError(
            provider,
            f"provider returned {status}: "
            f"{(resp.text or '')[:200]!r}",
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise BalanceFetchError(
            provider, f"non-JSON response: {exc}"
        ) from exc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DeepSeek
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"


async def fetch_balance_deepseek(
    api_key: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
    now: float | None = None,
) -> BalanceInfo | None:
    """Fetch the current DeepSeek account balance.

    DeepSeek's ``/user/balance`` returns a list of ``balance_infos``,
    one per currency. We pick the first entry — multi-currency accounts
    are rare and the per-tenant cache write up the stack is keyed
    only by provider name, so we cannot represent two simultaneously.
    The full vendor payload is preserved in ``raw`` so an operator who
    cares can drill down.

    Returns ``None`` when the key is missing / revoked (HTTP 401/403).
    Raises :class:`BalanceFetchError` for transport errors and 5xx.
    """
    body = await _http_get_json(
        "deepseek", _DEEPSEEK_BALANCE_URL, api_key,
        timeout=timeout, client_factory=client_factory,
    )
    if body is None:
        return None

    infos = body.get("balance_infos") if isinstance(body, dict) else None
    if not isinstance(infos, list) or not infos:
        # Malformed body: log + treat as fetch error so the cache layer
        # falls back to the previous snapshot rather than masking the
        # real balance with "0".
        raise BalanceFetchError(
            "deepseek",
            "response missing balance_infos list",
        )

    primary = infos[0] if isinstance(infos[0], dict) else {}
    total = _coerce_float(primary.get("total_balance"))
    granted = _coerce_float(primary.get("granted_balance"))
    # DeepSeek does not report cumulative usage — only the live
    # spendable balance + the original granted credit. Leave
    # ``usage_total`` as ``None`` so the dashboard can render
    # "—" rather than fabricate a "0 spent" that is misleading.
    return BalanceInfo(
        currency=str(primary.get("currency") or ""),
        balance_remaining=total,
        granted_total=granted,
        usage_total=None,
        last_refreshed_at=now if now is not None else time.time(),
        raw=body if isinstance(body, dict) else {},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OpenRouter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_OPENROUTER_BALANCE_URL = "https://openrouter.ai/api/v1/auth/key"


async def fetch_balance_openrouter(
    api_key: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
    now: float | None = None,
) -> BalanceInfo | None:
    """Fetch the current OpenRouter API key status.

    OpenRouter's ``/auth/key`` returns ``{data: {label, usage, limit,
    limit_remaining, is_free_tier, rate_limit}}``. We map:

    * ``data.limit_remaining`` → ``balance_remaining``. If it is
      ``None`` (unlimited account, free tier with no cap), we fall back
      to ``limit - usage`` when both are present, else leave it
      ``None`` so the dashboard renders the "unlimited" affordance.
    * ``data.limit`` → ``granted_total`` (the credit cap; for paid
      accounts this is the topped-up amount, for free tier it's the
      free quota).
    * ``data.usage`` → ``usage_total`` (cumulative spend so far).
    * Currency is always USD per OpenRouter's pricing model; we set it
      explicitly so renderers don't have to special-case "missing".

    Returns ``None`` for HTTP 401 / 403; raises
    :class:`BalanceFetchError` otherwise.
    """
    body = await _http_get_json(
        "openrouter", _OPENROUTER_BALANCE_URL, api_key,
        timeout=timeout, client_factory=client_factory,
    )
    if body is None:
        return None

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise BalanceFetchError(
            "openrouter",
            "response missing data envelope",
        )

    usage = _coerce_float(data.get("usage"))
    limit = _coerce_float(data.get("limit"))
    remaining = _coerce_float(data.get("limit_remaining"))
    if remaining is None and limit is not None and usage is not None:
        # Older API versions used to omit limit_remaining; derive it
        # so the dashboard always has something to show. The vendor's
        # current schema does include it, but the fallback is cheap
        # and keeps us robust against future schema drift.
        remaining = max(limit - usage, 0.0)

    return BalanceInfo(
        currency="USD",
        balance_remaining=remaining,
        granted_total=limit,
        usage_total=usage,
        last_refreshed_at=now if now is not None else time.time(),
        raw=body if isinstance(body, dict) else {},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Provider registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Mapping from provider name (matching ``llm_credentials._VALID_PROVIDERS``)
# to the fetcher coroutine. Providers absent from this dict have no
# public balance API with API-key authentication — the upcoming
# ``GET /runtime/providers/{provider}/balance`` endpoint will surface
# them as ``{status: "unsupported"}`` by checking ``provider not in
# SUPPORTED_BALANCE_PROVIDERS`` rather than threading a parallel list.
SUPPORTED_BALANCE_PROVIDERS: dict[
    str,
    Callable[..., Awaitable[BalanceInfo | None]],
] = {
    "deepseek": fetch_balance_deepseek,
    "openrouter": fetch_balance_openrouter,
}


def is_balance_supported(provider: str) -> bool:
    """Return True iff the provider exposes a public balance API
    callable with the API key the app already holds for completions.

    Used by the upcoming ``/runtime/providers/{provider}/balance``
    endpoint to short-circuit unsupported providers without attempting
    a fetch."""
    return provider in SUPPORTED_BALANCE_PROVIDERS
