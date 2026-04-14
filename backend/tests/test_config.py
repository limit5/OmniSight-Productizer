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
    from backend.config import Settings
    s = Settings()
    assert s.llm_temperature == 0.9
    assert s.notification_max_retries == 7


def test_env_override_bool_coerces(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_DEBUG", "true")
    monkeypatch.setenv("OMNISIGHT_RTK_ENABLED", "false")
    from backend.config import Settings
    s = Settings()
    assert s.debug is True
    assert s.rtk_enabled is False


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
