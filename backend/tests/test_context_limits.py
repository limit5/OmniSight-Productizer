"""ZZ.A2 #303-2 — `backend/context_limits.py::get_context_limit` regression
lock. Covers: exact-match, per-provider `default` fallback, `default: null`
→ None, unknown-provider → None, case-insensitive provider, Ollama env-var
override semantics, and the "quoted slash-path key" case (together /
openrouter model ids).
"""

from __future__ import annotations

import pytest

from backend import context_limits
from backend.context_limits import get_context_limit, reset_cache_for_tests


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


class TestExactMatch:
    def test_claude_opus_4_7(self):
        assert get_context_limit("anthropic", "claude-opus-4-7") == 1_000_000

    def test_gpt_4o(self):
        assert get_context_limit("openai", "gpt-4o") == 128_000

    def test_gemini_2_5_pro(self):
        assert get_context_limit("google", "gemini-2.5-pro") == 2_000_000

    def test_quoted_slash_key_together(self):
        assert (
            get_context_limit("together", "meta-llama/Llama-3.3-70B-Instruct-Turbo")
            == 131_072
        )

    def test_quoted_slash_key_openrouter(self):
        assert (
            get_context_limit("openrouter", "anthropic/claude-opus-4-7")
            == 1_000_000
        )


class TestProviderDefaultFallback:
    def test_unknown_model_uses_provider_default(self):
        assert get_context_limit("anthropic", "claude-unknown-model") == 200_000

    def test_unknown_openai_model_uses_default(self):
        assert get_context_limit("openai", "gpt-unknown") == 128_000

    def test_empty_model_uses_default(self):
        assert get_context_limit("anthropic", "") == 200_000

    def test_none_model_uses_default(self):
        assert get_context_limit("anthropic", None) == 200_000


class TestNullSemantics:
    def test_ollama_unknown_model_returns_none(self):
        assert get_context_limit("ollama", "something-local-nobody-pulled") is None

    def test_openrouter_unknown_route_returns_none(self):
        assert get_context_limit("openrouter", "unknown/route") is None

    def test_unknown_provider_returns_none(self):
        assert get_context_limit("some-vendor-nobody-ships", "any") is None

    def test_empty_provider_returns_none(self):
        assert get_context_limit("", "claude-opus-4-7") is None

    def test_none_provider_returns_none(self):
        assert get_context_limit(None, "claude-opus-4-7") is None


class TestProviderCaseInsensitive:
    def test_mixed_case_provider(self):
        assert get_context_limit("Anthropic", "claude-opus-4-7") == 1_000_000

    def test_upper_case_provider(self):
        assert get_context_limit("OPENAI", "gpt-4o") == 128_000


class TestOllamaEnvOverride:
    def test_env_override_beats_known_model(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_OLLAMA_CONTEXT_LIMIT", "8192")
        assert get_context_limit("ollama", "llama3.1") == 8192

    def test_env_override_beats_null_default(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_OLLAMA_CONTEXT_LIMIT", "4096")
        assert get_context_limit("ollama", "whatever-not-in-yaml") == 4096

    def test_env_override_scoped_to_ollama(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_OLLAMA_CONTEXT_LIMIT", "4096")
        assert get_context_limit("anthropic", "claude-opus-4-7") == 1_000_000
        assert get_context_limit("openai", "gpt-4o") == 128_000

    def test_garbage_override_falls_through_to_yaml(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_OLLAMA_CONTEXT_LIMIT", "not-a-number")
        assert get_context_limit("ollama", "llama3.1") == 131_072

    def test_empty_override_ignored(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_OLLAMA_CONTEXT_LIMIT", "")
        assert get_context_limit("ollama", "llama3.1") == 131_072

    def test_zero_override_ignored(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_OLLAMA_CONTEXT_LIMIT", "0")
        assert get_context_limit("ollama", "llama3.1") == 131_072


class TestYamlLoaderRobustness:
    def test_missing_yaml_returns_none_for_all_lookups(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            context_limits, "_LIMITS_PATH", tmp_path / "does-not-exist.yaml"
        )
        reset_cache_for_tests()
        assert get_context_limit("anthropic", "claude-opus-4-7") is None
        assert get_context_limit("openai", "gpt-4o") is None

    def test_corrupt_yaml_degrades_to_none(self, monkeypatch, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("::::: not valid yaml :::::\n", encoding="utf-8")
        monkeypatch.setattr(context_limits, "_LIMITS_PATH", bad)
        reset_cache_for_tests()
        assert get_context_limit("anthropic", "claude-opus-4-7") is None

    def test_null_entry_in_yaml_coerces_to_none(self, monkeypatch, tmp_path):
        p = tmp_path / "custom.yaml"
        p.write_text(
            "myvendor:\n"
            "  known-model: 65536\n"
            "  placeholder-model: null\n"
            "  default: null\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(context_limits, "_LIMITS_PATH", p)
        reset_cache_for_tests()
        assert get_context_limit("myvendor", "known-model") == 65536
        assert get_context_limit("myvendor", "placeholder-model") is None
        assert get_context_limit("myvendor", "missing") is None
