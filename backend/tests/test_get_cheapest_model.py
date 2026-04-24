"""ZZ.B2 #304-2 checkbox 3 — ``get_cheapest_model()`` tests.

Locks the cheapest-first preference contract so a future refactor
cannot silently route utility traffic (auto-title, future short-form
classifiers) back through flagship Opus and burn the quota.

Covers:

1. ``_CHEAPEST_MODEL_PREFERENCE`` ordering + schema (drift guard against
   a future edit that reorders or drops an entry).
2. ``get_llm(..., allow_failover=False)`` returns ``None`` on missing
   credentials rather than walking the fallback chain (the
   cheapest-first helper depends on this).
3. ``get_cheapest_model`` picks the first preference entry whose
   ``_create_llm`` init succeeds.
4. ``get_cheapest_model`` skips providers without keys and lands on the
   next cheapest (DeepSeek absent → Haiku 4.5).
5. ``get_cheapest_model`` falls back to ``get_llm()`` primary when every
   entry in the preference list fails (fresh install, no keys).
6. ``_compose_title_via_llm`` uses ``get_cheapest_model``, not the
   operator-pinned primary via ``get_llm``. The integration rebind
   rides through the chat router so a future revert to ``get_llm``
   trips this test immediately.

Module-global audit (SOP Step 1): tests mutate ``_cache`` and
``settings`` fields via pytest fixtures + ``monkeypatch`` which auto-
revert on teardown. No cross-test leakage.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_llm_cache():
    """``get_llm`` caches by ``f"{provider}:{model}:..."``. A previous
    test's (deepseek, deepseek-chat) entry would satisfy the later
    anthropic-only case — blow it per-test."""
    from backend.agents import llm as _llm_mod

    _llm_mod._cache.clear()
    _llm_mod._provider_failures.clear()
    yield
    _llm_mod._cache.clear()
    _llm_mod._provider_failures.clear()


@pytest.fixture(autouse=True)
def _no_token_freeze(monkeypatch):
    """The freeze check short-circuits the whole helper — pin it off."""
    from backend.routers import system as _sys_mod
    monkeypatch.setattr(_sys_mod, "is_token_frozen", lambda: False)


@pytest.fixture
def captured_build(monkeypatch):
    """Patch ``_create_llm`` to record (provider, model) calls.

    Returning ``_FakeChat`` from here simulates the "provider has key,
    init succeeded" branch. Tests that want the "no key" branch
    monkeypatch this fixture's callable to return ``None`` for a
    specific provider.
    """
    from backend.agents import llm as _llm_mod

    captured: list[dict] = []

    class _FakeChat:
        """``get_llm`` calls ``.with_config`` + optionally
        ``.bind_tools``; returning ``self`` keeps the test happy."""
        def __init__(self, provider: str, model: str | None) -> None:
            self.provider = provider
            self.model_name = model or f"{provider}:default"

        def with_config(self, **_kwargs):
            return self

        def bind_tools(self, _tools):
            return self

    def _fake_create(provider, model):
        captured.append({"provider": provider, "model": model})
        return _FakeChat(provider, model)

    monkeypatch.setattr(_llm_mod, "_create_llm", _fake_create)
    return captured


# ─── preference list schema + drift guard ────────────────────────────

def test_cheapest_preference_list_ordered_as_spec():
    """Drift guard: if a future edit reorders entries — e.g. puts
    anthropic first because "it's more reliable" — flagship quota
    burn returns. Lock the exact declared order so the reviewer
    must acknowledge the change explicitly.
    """
    from backend.agents.llm import _CHEAPEST_MODEL_PREFERENCE
    assert _CHEAPEST_MODEL_PREFERENCE == (
        ("deepseek", "deepseek-chat"),
        ("anthropic", "claude-haiku-4-20250506"),
        ("openrouter", "anthropic/claude-haiku-4"),
        ("groq", "llama-3.1-8b-instant"),
    )


def test_cheapest_preference_models_cheaper_than_primary():
    """Drift guard against a pricing-table refactor that accidentally
    makes a "cheapest" entry more expensive than the primary flagship.
    Compare per-output-token cost (the dominant factor for 8-word
    titles) against Opus's $75/Mtok out — every preference entry must
    clear a 10× cheaper bar, leaving safety margin for provider price
    bumps.
    """
    from backend.agents.llm import _CHEAPEST_MODEL_PREFERENCE
    from backend.events import _MODEL_PRICING_PER_MTOK
    opus_out = _MODEL_PRICING_PER_MTOK["claude-opus"][1]
    for _, model in _CHEAPEST_MODEL_PREFERENCE:
        lower = model.lower()
        slash_idx = lower.rfind("/")
        normalized = lower[slash_idx + 1:] if slash_idx >= 0 else lower
        keys = sorted(_MODEL_PRICING_PER_MTOK.keys(), key=len, reverse=True)
        matched = None
        for key in keys:
            if normalized.startswith(key) or key in normalized:
                matched = key
                break
        assert matched is not None, f"No pricing entry for {model}"
        out_rate = _MODEL_PRICING_PER_MTOK[matched][1]
        assert out_rate * 10 <= opus_out, (
            f"{model} output rate ${out_rate}/Mtok is >10% of Opus ${opus_out}/Mtok "
            "— preference entry no longer cheap enough to justify routing"
        )


# ─── allow_failover=False contract ───────────────────────────────────

def test_get_llm_no_failover_returns_none_on_missing_key(monkeypatch):
    """With ``allow_failover=False`` the helper MUST stop at the
    specific provider rather than cascading — otherwise
    ``get_cheapest_model`` loses its guarantee (the first provider's
    missing key would silently route to Opus).
    """
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    # Blank DeepSeek key forces _create_llm to return None.
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    result = _llm_mod.get_llm(
        provider="deepseek", model="deepseek-chat", allow_failover=False,
    )
    assert result is None


def test_get_llm_no_failover_does_not_record_breaker_failure(monkeypatch):
    """Opting out of failover must NOT dirty the circuit breaker —
    "no key" is an operational state, not a health signal. Flipping
    the breaker here would spuriously open it for any later caller
    that DID want the failover chain (e.g. primary Opus init).
    """
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", "")
    _llm_mod.get_llm(
        provider="deepseek", model="deepseek-chat", allow_failover=False,
    )
    assert "deepseek" not in _llm_mod._provider_failures


def test_get_llm_failover_still_works_when_flag_default(monkeypatch):
    """Regression guard: the new ``allow_failover`` kwarg defaults to
    True, preserving the pre-change behaviour for every existing
    caller (chat pipeline, agent nodes, etc.). Verify the chain
    still engages on a primary miss.
    """
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "sk-xxx")
    monkeypatch.setattr(
        settings, "llm_fallback_chain", "anthropic,openai,google",
    )

    class _Fake:
        def with_config(self, **_k):
            return self

    monkeypatch.setattr(
        _llm_mod, "_create_llm",
        lambda p, m: None if p == "anthropic" else _Fake(),
    )

    llm = _llm_mod.get_llm()  # default allow_failover=True
    assert llm is not None  # failover kicked in to openai


# ─── get_cheapest_model picks + skips ────────────────────────────────

def test_cheapest_picks_deepseek_when_key_present(captured_build, monkeypatch):
    """Happy path: DeepSeek is first in the preference list. When its
    key is configured, the helper stops after one call — no cascade
    through Anthropic / OpenRouter / Groq."""
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", "ds-test-key")

    llm = _llm_mod.get_cheapest_model()
    assert llm is not None
    assert len(captured_build) == 1
    assert captured_build[0] == {"provider": "deepseek", "model": "deepseek-chat"}


def test_cheapest_skips_deepseek_lands_on_haiku(monkeypatch):
    """DeepSeek key absent → skip; Anthropic key present → pick Haiku.
    This is the user's actual deployment shape (Anthropic primary, no
    DeepSeek key, wants titles on Haiku 4.5 not Opus).
    """
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "groq_api_key", "")

    captured: list[dict] = []

    class _Fake:
        def with_config(self, **_k):
            return self

    def _fake_create(provider, model):
        # Simulate the real credential gate inside _create_llm: return
        # None when the provider's key is blank.
        key_map = {
            "deepseek": settings.deepseek_api_key,
            "anthropic": settings.anthropic_api_key,
            "openrouter": settings.openrouter_api_key,
            "groq": settings.groq_api_key,
        }
        if provider in key_map and not key_map[provider]:
            return None
        captured.append({"provider": provider, "model": model})
        return _Fake()

    monkeypatch.setattr(_llm_mod, "_create_llm", _fake_create)

    llm = _llm_mod.get_cheapest_model()
    assert llm is not None
    # Exactly one successful pick — Haiku — no stray Opus / primary leak.
    assert captured == [
        {"provider": "anthropic", "model": "claude-haiku-4-20250506"},
    ]


def test_cheapest_lands_on_openrouter_when_only_that_key(monkeypatch):
    """Aggregator-only deployment — single OpenRouter key covers many
    models. Helper must walk past deepseek + anthropic + land on
    openrouter's Haiku route.
    """
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-test-key")
    monkeypatch.setattr(settings, "groq_api_key", "")

    captured: list[dict] = []

    class _Fake:
        def with_config(self, **_k):
            return self

    def _fake_create(provider, model):
        key_map = {
            "deepseek": settings.deepseek_api_key,
            "anthropic": settings.anthropic_api_key,
            "openrouter": settings.openrouter_api_key,
            "groq": settings.groq_api_key,
        }
        if provider in key_map and not key_map[provider]:
            return None
        captured.append({"provider": provider, "model": model})
        return _Fake()

    monkeypatch.setattr(_llm_mod, "_create_llm", _fake_create)

    llm = _llm_mod.get_cheapest_model()
    assert llm is not None
    assert captured == [
        {"provider": "openrouter", "model": "anthropic/claude-haiku-4"},
    ]


def test_cheapest_falls_back_to_primary_when_nothing_cheap(monkeypatch):
    """Every preference entry fails → degrade to ``get_llm()`` (the
    primary + its failover chain). The caller accepts a temporary
    cost-guarantee downgrade rather than silently skipping title
    generation.
    """
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    # No keys for any cheapest entry.
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "groq_api_key", "")
    # But operator's primary has a key (google say) — the primary
    # path will pick it up.
    monkeypatch.setattr(settings, "llm_provider", "google")
    monkeypatch.setattr(settings, "google_api_key", "g-test-key")
    monkeypatch.setattr(settings, "llm_model", "")

    captured: list[dict] = []

    class _Fake:
        def with_config(self, **_k):
            return self

    def _fake_create(provider, model):
        key_attr = {
            "deepseek": "deepseek_api_key",
            "anthropic": "anthropic_api_key",
            "openrouter": "openrouter_api_key",
            "groq": "groq_api_key",
            "google": "google_api_key",
            "openai": "openai_api_key",
        }.get(provider)
        if key_attr and not getattr(settings, key_attr, ""):
            return None
        captured.append({"provider": provider, "model": model})
        return _Fake()

    monkeypatch.setattr(_llm_mod, "_create_llm", _fake_create)

    llm = _llm_mod.get_cheapest_model()
    assert llm is not None
    # Must have tried to route through google (the primary), not
    # Opus (which would have blank key and get skipped). The cheap
    # entries all got None back so they don't appear in captured.
    assert any(c["provider"] == "google" for c in captured), captured


def test_cheapest_returns_none_when_truly_nothing_available(monkeypatch):
    """Last-line-of-defense: no cheap provider AND no primary — the
    helper returns ``None`` and the caller (``_compose_title_via_llm``)
    skips the SSE emit rather than exploding.
    """
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    # Every provider dry.
    for attr in [
        "deepseek_api_key", "anthropic_api_key", "openrouter_api_key",
        "groq_api_key", "openai_api_key", "google_api_key",
        "xai_api_key", "together_api_key",
    ]:
        monkeypatch.setattr(settings, attr, "")
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "")
    monkeypatch.setattr(
        settings, "llm_fallback_chain", "anthropic,openai",
    )

    def _fake_create(_provider, _model):
        return None

    monkeypatch.setattr(_llm_mod, "_create_llm", _fake_create)

    # The primary ``get_llm()`` fallback will emit a token warning via
    # events.emit_token_warning; stub it out to keep the test pure.
    from backend import events as _events_mod
    monkeypatch.setattr(_events_mod, "emit_token_warning", lambda *a, **k: None)

    llm = _llm_mod.get_cheapest_model()
    assert llm is None


# ─── _compose_title_via_llm uses the cheapest helper ─────────────────

@pytest.mark.asyncio
async def test_compose_title_via_llm_routes_through_cheapest(monkeypatch):
    """Integration rebind guard: the chat router must import and call
    ``get_cheapest_model``, NOT ``get_llm``. If a future revert to
    ``get_llm`` silently lands, this test fires immediately.

    Implementation strategy: patch ``get_cheapest_model`` to return a
    deterministic fake LLM and assert the patched version was used.
    """
    from backend.agents import llm as _llm_mod
    from backend.routers import chat as chat_router

    calls: list[str] = []

    class _FakeLLM:
        async def ainvoke(self, prompt):
            calls.append(prompt)
            class _R:
                content = "Wire up dashboard deep link"
            return _R()

    def _fake_cheapest(bind_tools=None):
        return _FakeLLM()

    # Primary get_llm MUST NOT be called — if the router reverts, this
    # sentinel fires.
    def _fake_primary(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError(
            "chat._compose_title_via_llm must route through "
            "get_cheapest_model, not get_llm"
        )

    monkeypatch.setattr(_llm_mod, "get_cheapest_model", _fake_cheapest)
    monkeypatch.setattr(_llm_mod, "get_llm", _fake_primary)

    title = await chat_router._compose_title_via_llm(
        ["First user message", "Second follow-up", "Third question"],
    )
    assert title == "Wire up dashboard deep link"
    assert len(calls) == 1
    # Prompt must mention all three condensed turns.
    prompt = calls[0]
    assert "1. First user message" in prompt
    assert "2. Second follow-up" in prompt
    assert "3. Third question" in prompt


@pytest.mark.asyncio
async def test_compose_title_returns_empty_when_no_cheapest_available(monkeypatch):
    """When ``get_cheapest_model`` returns ``None`` (truly nothing
    configured, token-frozen state), the composer returns empty so
    the background task skips ``emit_session_titled``.
    """
    from backend.agents import llm as _llm_mod
    from backend.routers import chat as chat_router

    monkeypatch.setattr(_llm_mod, "get_cheapest_model", lambda bind_tools=None: None)

    title = await chat_router._compose_title_via_llm(
        ["a", "b", "c"],
    )
    assert title == ""
