"""Phase-2 wire-up (2026-04-20) — ollama model routing via Settings.

The Phase-2 change in ``backend/agents/llm.py::get_llm`` adds a
per-provider model resolution branch so that when the fallback
chain engages ollama, it picks the model named in
``settings.ollama_model`` rather than the hardcoded ``llama3.1``
fallback inside ``build_chat_model``. This matters because the
Path B production ``ai_engine`` container only carries the
``gemma4:*`` family — calling it with ``model="llama3.1"`` would
404.

These tests lock the resolution table so a future refactor can't
silently regress the fallback path:

  +-------------------+--------------------+------------------------+
  | provider          | caller's model arg | resolved model         |
  +-------------------+--------------------+------------------------+
  | primary (anth)    | explicit string    | the explicit string    |
  | primary (anth)    | None               | settings.get_model_name |
  | ollama (fallback) | None + ollama_model set | ollama_model    |
  | ollama (fallback) | None + ollama_model empty | None (→ adapter default) |
  | ollama (fallback) | explicit string    | the explicit string    |
  | other (openai)    | None               | None (→ adapter default) |
  +-------------------+--------------------+------------------------+

We mock ``_create_llm`` (the ``get_llm`` → ``build_chat_model`` seam)
rather than ``build_chat_model`` itself so the routing logic at the
top of ``get_llm`` is exercised without triggering the downstream
fallback chain on missing credentials. The credential / cooldown /
circuit-breaker behaviour lives in ``_create_llm`` + its helpers
and is covered by sibling tests; here we only care about the
(provider, model) pair that get_llm commits to passing downstream.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_llm_cache():
    """``get_llm`` caches by ``f"{provider}:{model}:..."``. Blow the
    cache between tests so each case builds fresh — otherwise a
    previous test's ``(anthropic, claude-opus-4-7)`` entry would
    satisfy a later test that expected a DIFFERENT model."""
    from backend.agents import llm as _llm_mod

    _llm_mod._cache.clear()
    yield
    _llm_mod._cache.clear()


@pytest.fixture
def captured_build(monkeypatch):
    """Replace ``_create_llm`` with a capturer so tests assert on
    what (provider, model) pair ``get_llm`` routed to.

    ``_create_llm`` is the seam AFTER the routing logic runs but
    BEFORE credential / fallback-chain handling. Patching here lets
    the Phase-2 resolution branch (lines 169-175 of llm.py) run
    fully without getting kicked into the fallback chain on
    missing credentials in the test environment."""
    from backend.agents import llm as _llm_mod

    captured: list[dict] = []

    class _FakeChat:
        """Minimal stand-in — ``get_llm`` calls ``.with_config`` and
        optionally ``.bind_tools`` on the returned instance."""

        def with_config(self, **_kwargs):
            return self

        def bind_tools(self, tools):
            return self

    def _fake_create(provider, model):
        captured.append({"provider": provider, "model": model})
        return _FakeChat()

    monkeypatch.setattr(_llm_mod, "_create_llm", _fake_create)
    return captured


# ─── primary provider (anthropic) paths ────────────────────────────


def test_primary_provider_uses_llm_model_when_arg_none(captured_build, monkeypatch):
    """When provider == settings.llm_provider and caller passes
    ``model=None``, ``get_llm`` must resolve to
    ``settings.get_model_name()`` — i.e. honour the operator's
    ``OMNISIGHT_LLM_MODEL`` pin."""
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "claude-opus-4-7")
    # Turn off token freeze (the happy path).
    from backend.routers import system as _sys_mod
    monkeypatch.setattr(_sys_mod, "is_token_frozen", lambda: False)

    _llm_mod.get_llm()
    assert len(captured_build) == 1
    assert captured_build[0]["provider"] == "anthropic"
    assert captured_build[0]["model"] == "claude-opus-4-7"


def test_primary_provider_explicit_model_wins_over_llm_model(
    captured_build, monkeypatch,
):
    """Explicit caller override beats settings.llm_model."""
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "claude-opus-4-7")
    from backend.routers import system as _sys_mod
    monkeypatch.setattr(_sys_mod, "is_token_frozen", lambda: False)

    _llm_mod.get_llm(model="claude-haiku-4-5-20251001")
    assert captured_build[0]["model"] == "claude-haiku-4-5-20251001"


# ─── ollama fallback paths — Phase-2 contract ──────────────────────


def test_ollama_fallback_uses_ollama_model_when_set(captured_build, monkeypatch):
    """Phase-2 core contract: when the fallback chain reaches ollama
    (provider != primary) AND the caller didn't specify a model,
    resolve to ``settings.ollama_model`` so the routed model is
    actually loaded in the target ollama instance.

    Primary stays Anthropic; we ask get_llm for provider=ollama
    without a model arg. Must pick gemma4:e4b."""
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "ollama_model", "gemma4:e4b")
    from backend.routers import system as _sys_mod
    monkeypatch.setattr(_sys_mod, "is_token_frozen", lambda: False)

    _llm_mod.get_llm(provider="ollama")
    assert captured_build[0]["provider"] == "ollama"
    assert captured_build[0]["model"] == "gemma4:e4b"


def test_ollama_fallback_with_empty_ollama_model_lets_adapter_default(
    captured_build, monkeypatch,
):
    """If ``ollama_model`` is empty (legacy / not-yet-wired-up),
    ``get_llm`` forwards ``model=None`` and ``build_chat_model`` is
    free to use its own hardcoded default (currently ``llama3.1``).
    This preserves backward-compat for dev boxes that preloaded
    that model."""
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "ollama_model", "")
    from backend.routers import system as _sys_mod
    monkeypatch.setattr(_sys_mod, "is_token_frozen", lambda: False)

    _llm_mod.get_llm(provider="ollama")
    assert captured_build[0]["provider"] == "ollama"
    assert captured_build[0]["model"] is None


def test_ollama_fallback_whitespace_ollama_model_treated_as_empty(
    captured_build, monkeypatch,
):
    """Defensive: operator types ``OMNISIGHT_OLLAMA_MODEL='   '`` →
    treated as unset, not as a model literally named with spaces."""
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "ollama_model", "   ")
    from backend.routers import system as _sys_mod
    monkeypatch.setattr(_sys_mod, "is_token_frozen", lambda: False)

    _llm_mod.get_llm(provider="ollama")
    assert captured_build[0]["model"] is None


def test_ollama_explicit_model_beats_ollama_model_setting(
    captured_build, monkeypatch,
):
    """Caller supplied ``model=`` overrides Settings default — e.g.
    dashboard UI picking a different loaded model from the Ollama
    list for one specific invoke."""
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "ollama_model", "gemma4:e4b")
    from backend.routers import system as _sys_mod
    monkeypatch.setattr(_sys_mod, "is_token_frozen", lambda: False)

    _llm_mod.get_llm(provider="ollama", model="gemma4:26b")
    assert captured_build[0]["model"] == "gemma4:26b"


# ─── other non-primary providers — no routing change ───────────────


def test_non_primary_non_ollama_model_stays_none(captured_build, monkeypatch):
    """For any fallback provider that is NOT ollama (e.g. openai as
    backup on an anthropic-primary deployment), ``get_llm`` must
    continue to forward ``model=None`` — respecting the adapter's
    own per-provider default (gpt-4o for openai, etc.). Phase-2
    ollama_model must NOT leak into other providers."""
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "ollama_model", "gemma4:e4b")
    from backend.routers import system as _sys_mod
    monkeypatch.setattr(_sys_mod, "is_token_frozen", lambda: False)

    _llm_mod.get_llm(provider="openai")
    assert captured_build[0]["provider"] == "openai"
    assert captured_build[0]["model"] is None
