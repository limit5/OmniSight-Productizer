"""Fix-D D6 — Settings env override + provider-model resolution."""

from __future__ import annotations

import pytest


def test_default_settings_have_sane_defaults():
    from backend.config import Settings
    s = Settings()
    assert s.api_prefix == "/api/v1"
    assert s.llm_provider == "anthropic"
    assert s.llm_temperature == 0.3
    assert s.notification_max_retries == 3


def test_web_search_settings_have_sane_defaults(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("OMNISIGHT_WEB_SEARCH_DAILY_BUDGET_USD", raising=False)
    from backend.config import Settings
    s = Settings()
    assert s.web_search_provider == "none"
    assert s.web_search_daily_budget_usd == 5.00


@pytest.mark.parametrize("env,expected_provider", [
    ({"OMNISIGHT_LLM_PROVIDER": "xai"}, "xai"),
    ({"OMNISIGHT_LLM_PROVIDER": "groq"}, "groq"),
    ({"OMNISIGHT_LLM_PROVIDER": "openai"}, "openai"),
])
def test_env_override_llm_provider(monkeypatch, env, expected_provider):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from backend.config import Settings
    s = Settings()
    assert s.llm_provider == expected_provider


def test_env_override_numeric_coerces(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_LLM_TEMPERATURE", "0.9")
    monkeypatch.setenv("OMNISIGHT_NOTIFICATION_MAX_RETRIES", "7")
    monkeypatch.setenv("OMNISIGHT_WEB_SEARCH_DAILY_BUDGET_USD", "12.50")
    from backend.config import Settings
    s = Settings()
    assert s.llm_temperature == 0.9
    assert s.notification_max_retries == 7
    assert s.web_search_daily_budget_usd == 12.50


def test_env_override_bool_coerces(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_DEBUG", "true")
    monkeypatch.setenv("OMNISIGHT_RTK_ENABLED", "false")
    from backend.config import Settings
    s = Settings()
    assert s.debug is True
    assert s.rtk_enabled is False


@pytest.mark.parametrize("provider", ["none", "tavily", "exa", "perplexity"])
def test_env_override_web_search_provider(monkeypatch, provider):
    monkeypatch.setenv("OMNISIGHT_WEB_SEARCH_PROVIDER", provider)
    from backend.config import Settings
    s = Settings()
    assert s.web_search_provider == provider


def test_validate_startup_config_warns_on_bad_web_search_provider(monkeypatch):
    from backend import config as cfg
    monkeypatch.setattr(cfg.settings, "web_search_provider", "bing")
    warnings = cfg.validate_startup_config(strict=False)
    assert any("WEB_SEARCH_PROVIDER" in w for w in warnings)


def test_validate_startup_config_warns_on_bad_web_search_budget(monkeypatch):
    from backend import config as cfg
    monkeypatch.setattr(cfg.settings, "web_search_daily_budget_usd", -1)
    warnings = cfg.validate_startup_config(strict=False)
    assert any("WEB_SEARCH_DAILY_BUDGET_USD" in w for w in warnings)


@pytest.mark.parametrize("provider,default_model", [
    ("anthropic", "claude-sonnet-4-20250514"),
    ("google", "gemini-1.5-pro"),
    ("openai", "gpt-4o"),
    ("xai", "grok-3-mini"),
    ("ollama", "llama3.1"),
])
def test_get_model_name_falls_back_to_provider_default(provider, default_model, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_LLM_PROVIDER", provider)
    monkeypatch.delenv("OMNISIGHT_LLM_MODEL", raising=False)
    from backend.config import Settings
    s = Settings()
    assert s.get_model_name() == default_model


def test_get_model_name_honours_explicit_override(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("OMNISIGHT_LLM_MODEL", "claude-opus-4-6")
    from backend.config import Settings
    s = Settings()
    assert s.get_model_name() == "claude-opus-4-6"


def test_get_model_name_unknown_provider_falls_back_to_anthropic_default(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_LLM_PROVIDER", "mystery-vendor")
    monkeypatch.delenv("OMNISIGHT_LLM_MODEL", raising=False)
    from backend.config import Settings
    s = Settings()
    assert s.get_model_name() == "claude-sonnet-4-20250514"


# ── Y6 #282 row 2 — workspace_root / workspace_quota_mb_default ──

def test_workspace_root_default_is_data_workspaces(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_WORKSPACE_ROOT", raising=False)
    from backend.config import Settings
    s = Settings()
    assert s.workspace_root == "./data/workspaces"


def test_workspace_quota_mb_default_default_is_zero_unlimited(monkeypatch):
    """Default 0 = unlimited, preserves pre-Y6 behaviour until row 5
    enforcement lands so flipping just this row cannot silently start
    rejecting writes."""
    monkeypatch.delenv("OMNISIGHT_WORKSPACE_QUOTA_MB_DEFAULT", raising=False)
    from backend.config import Settings
    s = Settings()
    assert s.workspace_quota_mb_default == 0


def test_workspace_root_env_override(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_WORKSPACE_ROOT", "/srv/omnisight/workspaces")
    from backend.config import Settings
    s = Settings()
    assert s.workspace_root == "/srv/omnisight/workspaces"


def test_workspace_quota_mb_default_env_override_coerces_int(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_WORKSPACE_QUOTA_MB_DEFAULT", "8192")
    from backend.config import Settings
    s = Settings()
    assert s.workspace_quota_mb_default == 8192
    assert isinstance(s.workspace_quota_mb_default, int)
