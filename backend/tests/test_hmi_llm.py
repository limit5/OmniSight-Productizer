"""C26 — HMI pluggable LLM backend tests (#261).

Covers: provider selection precedence (explicit > HMI_LLM_PROVIDER >
OMNISIGHT_LLM_PROVIDER > rule_based), rule-based deterministic output,
anthropic fallback when no API key, ollama fallback when no daemon.
"""

from __future__ import annotations

from dataclasses import dataclass


from backend import hmi_llm as hl


@dataclass
class _FakeField:
    name: str


@dataclass
class _FakeEndpoint:
    id: str = "wifi_connect"
    method: str = "POST"
    path: str = "/api/network/wifi"
    request_fields: list = None
    response_fields: list = None

    def __post_init__(self):
        if self.request_fields is None:
            self.request_fields = [_FakeField("ssid"), _FakeField("password")]
        if self.response_fields is None:
            self.response_fields = [_FakeField("connected")]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Provider selection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestChooseProvider:
    def test_default_is_rule_based(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("HMI_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("OMNISIGHT_LLM_PROVIDER", raising=False)
        assert hl.choose_provider() == "rule_based"

    def test_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_LLM_PROVIDER", "ollama")
        monkeypatch.setenv("HMI_LLM_PROVIDER", "anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Explicit rule_based trumps all env
        assert hl.choose_provider("rule_based") == "rule_based"

    def test_hmi_env_wins_over_global(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("HMI_LLM_PROVIDER", "ollama")
        assert hl.choose_provider() == "ollama"

    def test_anthropic_without_key_falls_back(self, monkeypatch):
        monkeypatch.setenv("HMI_LLM_PROVIDER", "anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert hl.choose_provider() == "rule_based"

    def test_anthropic_with_key_selected(self, monkeypatch):
        monkeypatch.setenv("HMI_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert hl.choose_provider() == "anthropic"

    def test_ollama_selectable(self, monkeypatch):
        monkeypatch.setenv("HMI_LLM_PROVIDER", "ollama")
        assert hl.choose_provider() == "ollama"

    def test_unknown_provider_falls_back(self, monkeypatch):
        monkeypatch.setenv("HMI_LLM_PROVIDER", "madeup")
        assert hl.choose_provider() == "rule_based"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rule-based enrichment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRuleBased:
    def test_wifi_prompt_matches_hint(self, monkeypatch):
        monkeypatch.delenv("HMI_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("OMNISIGHT_LLM_PROVIDER", raising=False)
        ep = _FakeEndpoint()
        result = hl.enrich_binding_description("connect to wifi", ep)
        assert result.provider == "rule_based"
        assert result.used_real_llm is False
        assert "Wi-Fi" in result.description

    def test_ota_prompt_matches_hint(self, monkeypatch):
        monkeypatch.delenv("HMI_LLM_PROVIDER", raising=False)
        ep = _FakeEndpoint(id="ota_apply", path="/api/ota/apply")
        result = hl.enrich_binding_description("upload firmware OTA", ep)
        assert "OTA" in result.description

    def test_fallback_unmatched_prompt(self, monkeypatch):
        monkeypatch.delenv("HMI_LLM_PROVIDER", raising=False)
        ep = _FakeEndpoint()
        result = hl.enrich_binding_description("zzzz", ep)
        assert "HMI binding" in result.description
        assert result.provider == "rule_based"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Anthropic / Ollama lazy fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLazyImport:
    def test_anthropic_import_failure_returns_none(self, monkeypatch):
        # Patch _try_anthropic to simulate no SDK installed
        monkeypatch.setattr(hl, "_try_anthropic", lambda *a, **kw: None)
        monkeypatch.setenv("HMI_LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
        ep = _FakeEndpoint()
        result = hl.enrich_binding_description("wifi", ep)
        # Should degrade to rule_based since SDK missing
        assert result.provider == "rule_based"
        assert result.used_real_llm is False

    def test_ollama_network_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(hl, "_try_ollama", lambda *a, **kw: None)
        monkeypatch.setenv("HMI_LLM_PROVIDER", "ollama")
        ep = _FakeEndpoint()
        result = hl.enrich_binding_description("wifi", ep)
        assert result.provider == "rule_based"


class TestSummary:
    def test_summary_lists_providers(self):
        s = hl.summary()
        assert s["llm_version"] == hl.LLM_VERSION
        assert set(s["supported_providers"]) == {"anthropic", "ollama", "rule_based"}
        assert s["active_provider"] in s["supported_providers"]
