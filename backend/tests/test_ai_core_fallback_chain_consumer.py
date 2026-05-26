"""OP-1743 (G.A-v2 Family ⑨ · v2-⑨-2bc) — contract tests for the
``llm_fallback_chain`` builder consuming the ai-core availability flag.

Covers the chain-builder obligations from
``docs/sprint-s12/2026-05-16-v2-family9-aux-service-contract.md`` §3.5
(and §3.6 cases #12–#14): ollama is dropped from the *active* failover
chain when ai-core is unavailable (fail-closed per §2.2/§3.4), kept when
available, and the declared ``settings.llm_fallback_chain`` is never
mutated.
"""

from __future__ import annotations

import pytest

from backend.agents import ai_core_probe as probe_mod
from backend.agents import llm
from backend.config import settings

_DECLARED_CHAIN = "anthropic,openai,google,groq,deepseek,openrouter,ollama"


@pytest.fixture(autouse=True)
def _declared_chain(monkeypatch):
    """Pin a known declared chain (with ollama present) for each test."""
    monkeypatch.setattr(
        settings, "llm_fallback_chain", _DECLARED_CHAIN, raising=False
    )


def test_chain_builder_excludes_ollama_when_ai_core_unavailable(monkeypatch):
    """§3.6 #12 — ai-core down ⇒ ollama removed from the active chain."""
    monkeypatch.setattr(probe_mod, "AI_CORE_AVAILABLE", False, raising=False)

    chain = llm.build_active_fallback_chain()

    assert "ollama" not in chain
    assert chain == ["anthropic", "openai", "google", "groq", "deepseek", "openrouter"]


def test_chain_builder_keeps_ollama_when_ai_core_available(monkeypatch):
    """§3.6 #13 — ai-core up ⇒ ollama retained in declared order."""
    monkeypatch.setattr(probe_mod, "AI_CORE_AVAILABLE", True, raising=False)

    chain = llm.build_active_fallback_chain()

    assert chain[-1] == "ollama"
    assert chain == [
        "anthropic", "openai", "google", "groq", "deepseek", "openrouter", "ollama",
    ]


def test_chain_builder_does_not_mutate_settings_llm_fallback_chain(monkeypatch):
    """§3.6 #14 — the operator's declared chain is never mutated."""
    monkeypatch.setattr(probe_mod, "AI_CORE_AVAILABLE", False, raising=False)

    llm.build_active_fallback_chain()

    assert settings.llm_fallback_chain == _DECLARED_CHAIN


def test_chain_builder_is_idempotent(monkeypatch):
    """§3.5 — repeated calls with no state change return equal lists."""
    monkeypatch.setattr(probe_mod, "AI_CORE_AVAILABLE", False, raising=False)

    assert llm.build_active_fallback_chain() == llm.build_active_fallback_chain()


def test_fallback_chain_for_guild_drops_ollama_when_ai_core_unavailable(monkeypatch):
    """The actual failover consumer (get_llm path) honours the gate."""
    monkeypatch.setattr(probe_mod, "AI_CORE_AVAILABLE", False, raising=False)

    chain = llm._fallback_chain_for_guild(
        guild=None, primary_provider="anthropic", guild_provider=None
    )

    assert "ollama" not in chain


def test_fallback_chain_for_guild_keeps_ollama_when_ai_core_available(monkeypatch):
    """Symmetric: ai-core up ⇒ ollama remains an eligible failover target."""
    monkeypatch.setattr(probe_mod, "AI_CORE_AVAILABLE", True, raising=False)

    chain = llm._fallback_chain_for_guild(
        guild=None, primary_provider="anthropic", guild_provider=None
    )

    assert "ollama" in chain
