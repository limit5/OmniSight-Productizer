"""BP.N.3 -- web-search provider and budget env knobs."""

from __future__ import annotations

import pytest


class _Settings:
    def __init__(self, provider: str = "", budget: object = "") -> None:
        self.web_search_provider = provider
        self.web_search_daily_budget_usd = budget


def test_runtime_config_defaults_to_disabled_with_five_dollar_budget(monkeypatch) -> None:
    from backend.web_search import DEFAULT_DAILY_BUDGET_USD, WebSearchRuntimeConfig

    monkeypatch.delenv("OMNISIGHT_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("OMNISIGHT_WEB_SEARCH_DAILY_BUDGET_USD", raising=False)

    config = WebSearchRuntimeConfig.from_settings()

    assert config.provider == "none"
    assert config.daily_budget_usd == DEFAULT_DAILY_BUDGET_USD


@pytest.mark.parametrize("provider", ["none", "tavily", "exa", "perplexity"])
def test_runtime_config_accepts_declared_provider_set(provider: str) -> None:
    from backend.web_search import WebSearchRuntimeConfig

    config = WebSearchRuntimeConfig.from_settings(_Settings(provider=provider, budget=2.5))

    assert config.provider == provider
    assert config.daily_budget_usd == 2.5


def test_runtime_config_rejects_unknown_provider() -> None:
    from backend.web_search import WebSearchConfigError, WebSearchRuntimeConfig

    with pytest.raises(WebSearchConfigError):
        WebSearchRuntimeConfig.from_settings(_Settings(provider="bing"))


def test_runtime_config_rejects_negative_budget() -> None:
    from backend.web_search import WebSearchConfigError, WebSearchRuntimeConfig

    with pytest.raises(WebSearchConfigError):
        WebSearchRuntimeConfig.from_settings(_Settings(provider="none", budget=-0.01))


def test_make_web_search_client_none_returns_disabled() -> None:
    from backend.web_search import make_web_search_client

    client = make_web_search_client(settings=_Settings(provider="none", budget=1.0))

    assert client is None


def test_make_web_search_client_tavily_applies_budget() -> None:
    from backend.web_search import (
        InMemoryWebSearchCostStore,
        TavilyWebSearchClient,
        make_web_search_client,
    )

    store = InMemoryWebSearchCostStore()

    client = make_web_search_client(
        settings=_Settings(provider="tavily", budget=0.01),
        cost_store=store,
        api_key="tvly-test",
    )

    assert isinstance(client, TavilyWebSearchClient)
    assert client.cost_tracker.daily_budget_usd == 0.01
    assert client.cost_tracker.store is store


@pytest.mark.parametrize("provider", ["exa", "perplexity"])
def test_make_web_search_client_future_providers_raise_until_adapters_land(
    provider: str,
) -> None:
    from backend.web_search import UnsupportedWebSearchProviderError, make_web_search_client

    with pytest.raises(UnsupportedWebSearchProviderError):
        make_web_search_client(settings=_Settings(provider=provider, budget=1.0))


def test_make_web_search_client_explicit_provider_beats_settings() -> None:
    from backend.web_search import make_web_search_client

    client = make_web_search_client(
        "none",
        settings=_Settings(provider="tavily", budget=0.75),
        api_key="tvly-test",
    )

    assert client is None
