"""Input-shape coverage for backend.agents.llm public entry points.

OP-1321 stress-test scope: call the public functions directly with
None, empty collections, wrong types, and very-large values where the
function accepts caller input. Provider construction is monkeypatched
so these tests stay local and do not require configured API keys.
"""

from __future__ import annotations

import pytest


class _FakeChat:
    def __init__(self, model_name: str = "fake-model") -> None:
        self.model_name = model_name
        self.bound_tools = None

    def with_config(self, **_kwargs):
        return self

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self


@pytest.fixture(autouse=True)
def _clear_llm_cache():
    from backend.agents import llm as llm_mod

    llm_mod._cache.clear()
    llm_mod._provider_failures.clear()
    yield
    llm_mod._cache.clear()
    llm_mod._provider_failures.clear()


@pytest.fixture(autouse=True)
def _no_token_freeze(monkeypatch):
    from backend.routers import system as system_mod

    monkeypatch.setattr(system_mod, "is_token_frozen", lambda: False)


@pytest.fixture
def captured_create(monkeypatch):
    from backend.agents import llm as llm_mod

    captured: list[dict] = []

    def fake_create(provider, model):
        captured.append({"provider": provider, "model": model})
        return _FakeChat(model_name=str(model or provider or "fake-model"))

    monkeypatch.setattr(llm_mod, "_create_llm", fake_create)
    monkeypatch.setattr(llm_mod, "_record_provider_success", lambda _provider: None)
    monkeypatch.setattr(llm_mod, "_record_provider_failure", lambda *_a, **_k: None)
    return captured


class TestGetLlmInputShapes:
    def test_none_provider_and_model_use_configured_defaults(
        self,
        captured_create,
        monkeypatch,
    ):
        from backend.agents import llm as llm_mod
        from backend.config import settings

        monkeypatch.setattr(settings, "llm_provider", "anthropic")
        monkeypatch.setattr(settings, "llm_model", "claude-opus-4-7")

        result = llm_mod.get_llm(provider=None, model=None)

        assert result is not None
        assert captured_create == [
            {"provider": "anthropic", "model": "claude-opus-4-7"},
        ]

    def test_empty_bind_tools_collection_does_not_bind(self, captured_create):
        from backend.agents import llm as llm_mod

        result = llm_mod.get_llm(provider="ollama", model="llama3.1", bind_tools=[])

        assert isinstance(result, _FakeChat)
        assert result.bound_tools is None
        assert captured_create == [{"provider": "ollama", "model": "llama3.1"}]

    def test_wrong_type_provider_and_model_are_passed_to_factory(
        self,
        captured_create,
    ):
        from backend.agents import llm as llm_mod

        result = llm_mod.get_llm(provider=123, model={"bad": "model"})

        assert result is not None
        assert captured_create == [{"provider": 123, "model": {"bad": "model"}}]

    def test_very_large_provider_and_model_strings_reach_factory(
        self,
        captured_create,
    ):
        from backend.agents import llm as llm_mod

        provider = "p" * 100_000
        model = "m" * 100_000

        result = llm_mod.get_llm(provider=provider, model=model)

        assert result is not None
        assert captured_create == [{"provider": provider, "model": model}]


class TestGetCheapestModelInputShapes:
    @pytest.mark.parametrize(
        "bind_tools",
        [None, [], object(), ["tool"] * 10_000],
        ids=["none", "empty", "wrong-type", "very-large"],
    )
    def test_bind_tools_input_shapes_are_forwarded(self, bind_tools, monkeypatch):
        from backend.agents import llm as llm_mod

        calls: list[dict] = []

        def fake_get_llm(**kwargs):
            calls.append(kwargs)
            return _FakeChat() if len(calls) == 1 else None

        monkeypatch.setattr(llm_mod, "get_llm", fake_get_llm)

        result = llm_mod.get_cheapest_model(bind_tools=bind_tools)

        assert isinstance(result, _FakeChat)
        assert calls == [
            {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "bind_tools": bind_tools,
                "allow_failover": False,
            },
        ]


class TestListProvidersInputShapes:
    def test_no_input_returns_provider_metadata(self, monkeypatch):
        from backend.agents import llm as llm_mod
        from backend import llm_credential_resolver

        monkeypatch.setattr(
            llm_credential_resolver,
            "is_provider_configured",
            lambda provider: provider == "ollama",
        )
        monkeypatch.setattr(llm_mod, "_load_ollama_tool_calling_compat", lambda: {})

        providers = llm_mod.list_providers()

        assert providers
        assert {p["id"] for p in providers} >= {"anthropic", "ollama"}
        assert all("configured" in p for p in providers)

    @pytest.mark.parametrize(
        "arg",
        [None, [], object(), "x" * 100_000],
        ids=["none", "empty", "wrong-type", "very-large"],
    )
    def test_unexpected_arguments_raise_type_error(self, arg):
        from backend.agents import llm as llm_mod

        with pytest.raises(TypeError):
            llm_mod.list_providers(arg)


class TestValidateModelSpecInputShapes:
    @pytest.mark.parametrize(
        "model_spec",
        [None, "", [], {}],
        ids=["none", "empty-string", "empty-list", "empty-dict"],
    )
    def test_none_and_empty_values_validate_as_no_override(self, model_spec):
        from backend.agents import llm as llm_mod

        result = llm_mod.validate_model_spec(model_spec)

        assert result == {
            "valid": True,
            "provider": "",
            "model": "",
            "configured": True,
            "warning": "",
        }

    @pytest.mark.parametrize(
        "model_spec",
        [123, object()],
        ids=["int", "object"],
    )
    def test_wrong_scalar_types_raise_type_error(self, model_spec):
        from backend.agents import llm as llm_mod

        with pytest.raises(TypeError):
            llm_mod.validate_model_spec(model_spec)

    def test_very_large_unknown_provider_returns_invalid(self):
        from backend.agents import llm as llm_mod

        provider = "p" * 100_000
        result = llm_mod.validate_model_spec(f"{provider}:model")

        assert result["valid"] is False
        assert result["provider"] == provider
        assert result["model"] == "model"
        assert "Unknown provider" in result["warning"]


class TestReloadOllamaCompatInputShapes:
    def test_no_input_clears_cache(self):
        from backend.agents import llm as llm_mod

        llm_mod._OLLAMA_TOOL_COMPAT_CACHE = (1.0, {"llama3.1": {"supports": True}})

        llm_mod.reload_ollama_tool_calling_compat_for_tests()

        assert llm_mod._OLLAMA_TOOL_COMPAT_CACHE is None

    @pytest.mark.parametrize(
        "arg",
        [None, [], object(), "x" * 100_000],
        ids=["none", "empty", "wrong-type", "very-large"],
    )
    def test_unexpected_arguments_raise_type_error(self, arg):
        from backend.agents import llm as llm_mod

        with pytest.raises(TypeError):
            llm_mod.reload_ollama_tool_calling_compat_for_tests(arg)
