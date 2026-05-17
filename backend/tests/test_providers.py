"""Property-based tests for ``backend.routers.providers`` public API."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st

from backend.routers import providers


pytestmark = pytest.mark.asyncio


PROVIDER_IDS = ("anthropic", "openai", "google", "ollama")
PROVIDER_ROWS = [
    {
        "id": pid,
        "name": pid.title(),
        "configured": pid in {"anthropic", "ollama"},
    }
    for pid in PROVIDER_IDS
]
SAFE_MODEL = st.one_of(st.none(), st.text(max_size=1024))
CHAIN = st.lists(st.sampled_from(PROVIDER_IDS), max_size=32)
SAFE_SCOPE = st.text(max_size=128)


class _Settings:
    def __init__(
        self,
        *,
        llm_provider: str = "anthropic",
        llm_model: str = "",
        llm_fallback_chain: str = "anthropic,openai",
    ):
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        self.llm_fallback_chain = llm_fallback_chain

    def get_model_name(self) -> str:
        return self.llm_model or f"default-{self.llm_provider}"


@pytest.fixture(autouse=True)
def provider_router_isolated(monkeypatch):
    settings = _Settings()
    cache: dict[str, object] = {"stale": object()}
    events: list[tuple[str, str]] = []

    monkeypatch.setattr(providers, "settings", settings)
    monkeypatch.setattr(providers, "list_providers", lambda: list(PROVIDER_ROWS))
    monkeypatch.setattr(providers, "get_llm", lambda **_: object())

    import backend.agents.llm as llm_mod
    import backend.events as events_mod

    monkeypatch.setattr(llm_mod, "_cache", cache)
    monkeypatch.setattr(events_mod, "emit_invoke", lambda *args: events.append(args))

    yield SimpleNamespace(settings=settings, cache=cache, events=events)


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(active=st.sampled_from(PROVIDER_IDS), model=st.text(max_size=512))
async def test_get_providers_reports_runtime_settings_without_rewriting(
    monkeypatch,
    provider_router_isolated,
    active: str,
    model: str,
):
    import backend.routers.integration as integration_router

    overlays: list[None] = []
    provider_router_isolated.settings.llm_provider = active
    provider_router_isolated.settings.llm_model = model
    monkeypatch.setattr(
        integration_router,
        "_overlay_runtime_settings",
        lambda: overlays.append(None),
    )

    first = await providers.get_providers()
    second = await providers.get_providers()

    assert first == second
    assert first["active_provider"] == active
    assert first["active_model"] == (model or f"default-{active}")
    assert first["providers"] == PROVIDER_ROWS
    assert len(overlays) == 2


@hyp_settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(provider=st.sampled_from(PROVIDER_IDS), model=SAFE_MODEL)
async def test_switch_provider_is_idempotent_for_valid_provider(
    provider_router_isolated,
    provider: str,
    model: str | None,
):
    body = providers.SwitchProviderRequest(provider=provider, model=model)

    first = await providers.switch_provider(body)
    second = await providers.switch_provider(body)

    expected_model = model or f"default-{provider}"
    assert first == second
    assert first == {
        "status": "switched",
        "provider": provider,
        "model": expected_model,
        "llm_active": True,
        "note": None,
    }
    assert provider_router_isolated.settings.llm_provider == provider
    assert provider_router_isolated.settings.llm_model == (model or "")
    assert provider_router_isolated.cache == {}
    assert provider_router_isolated.events[-1] == (
        "provider_switch",
        f"{provider}/{expected_model}",
    )


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(unknown=st.text(min_size=1, max_size=256).filter(lambda s: s not in PROVIDER_IDS))
async def test_switch_provider_rejects_unknown_provider_without_mutation(
    provider_router_isolated,
    unknown: str,
):
    before = (
        provider_router_isolated.settings.llm_provider,
        provider_router_isolated.settings.llm_model,
        dict(provider_router_isolated.cache),
    )

    with pytest.raises(HTTPException) as exc:
        await providers.switch_provider(providers.SwitchProviderRequest(provider=unknown))

    assert exc.value.status_code == 400
    assert "Unknown provider" in exc.value.detail
    assert (
        provider_router_isolated.settings.llm_provider,
        provider_router_isolated.settings.llm_model,
        dict(provider_router_isolated.cache),
    ) == before


@hyp_settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(chain=CHAIN)
async def test_update_fallback_chain_preserves_valid_chain_and_clears_cache(
    provider_router_isolated,
    chain: list[str],
):
    out = await providers.update_fallback_chain(providers.FallbackChainRequest(chain=chain))

    assert out == {"status": "updated", "chain": chain}
    assert provider_router_isolated.settings.llm_fallback_chain == ",".join(chain)
    assert provider_router_isolated.cache == {}


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    valid_prefix=CHAIN,
    invalid=st.text(min_size=1, max_size=128).filter(lambda s: s not in PROVIDER_IDS),
)
async def test_update_fallback_chain_rejects_any_unknown_provider(
    provider_router_isolated,
    valid_prefix: list[str],
    invalid: str,
):
    before = provider_router_isolated.settings.llm_fallback_chain

    with pytest.raises(HTTPException) as exc:
        await providers.update_fallback_chain(
            providers.FallbackChainRequest(chain=[*valid_prefix, invalid])
        )

    assert exc.value.status_code == 400
    assert "Unknown provider" in exc.value.detail
    assert provider_router_isolated.settings.llm_fallback_chain == before


@hyp_settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    chain=st.lists(
        st.one_of(
            st.sampled_from(PROVIDER_IDS),
            st.just(""),
            st.sampled_from([" anthropic ", " openai", "google "]),
        ),
        max_size=32,
    ),
    active=st.sampled_from(PROVIDER_IDS),
)
async def test_get_provider_health_contract_for_chain_edges(
    monkeypatch,
    provider_router_isolated,
    chain: list[str],
    active: str,
):
    import backend.circuit_breaker as cb
    import backend.db_context as db_context

    provider_router_isolated.settings.llm_provider = active
    provider_router_isolated.settings.llm_fallback_chain = ",".join(chain)
    monkeypatch.setattr(cb, "active_fingerprint", lambda pid: f"fp-{pid}")
    monkeypatch.setattr(cb, "is_open", lambda tenant_id, pid, fp: False)
    monkeypatch.setattr(cb, "cooldown_remaining", lambda tenant_id, pid, fp: 0)
    monkeypatch.setattr(db_context, "current_tenant_id", lambda: None)

    out = await providers.get_provider_health()
    expected_chain = [p.strip() for p in chain if p.strip()]

    assert out["chain"] == expected_chain
    assert len(out["health"]) == len(expected_chain)
    for item, pid in zip(out["health"], expected_chain, strict=True):
        assert item["id"] == pid
        assert item["name"]
        assert isinstance(item["configured"], bool)
        assert item["is_active"] is (pid == active)
        assert item["cooldown_remaining"] >= 0
        assert item["status"] in {
            "active",
            "cooldown",
            "available",
            "unconfigured",
        }


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(scope=SAFE_SCOPE)
async def test_get_circuit_breakers_scopes_snapshot_by_default(
    monkeypatch,
    scope: str,
):
    import backend.circuit_breaker as cb
    import backend.db_context as db_context

    calls: list[str | None] = []
    monkeypatch.setattr(db_context, "current_tenant_id", lambda: "tenant-a")
    monkeypatch.setattr(cb, "COOLDOWN_SECONDS", 123)
    monkeypatch.setattr(
        cb,
        "snapshot",
        lambda tenant_id=None: calls.append(tenant_id) or [{"tenant_id": tenant_id}],
    )

    out = await providers.get_circuit_breakers(scope=scope)

    expected_tenant = None if scope == "all" else "tenant-a"
    assert calls == [expected_tenant]
    assert out == {
        "tenant_id": "tenant-a",
        "scope": scope,
        "cooldown_seconds": 123,
        "circuits": [{"tenant_id": expected_tenant}],
    }


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    provider=st.one_of(st.none(), st.sampled_from(PROVIDER_IDS)),
    fingerprint=SAFE_MODEL,
    scope=SAFE_SCOPE,
)
async def test_reset_circuit_breaker_passes_optional_filters_without_rewriting(
    monkeypatch,
    provider: str | None,
    fingerprint: str | None,
    scope: str,
):
    import backend.circuit_breaker as cb
    import backend.db_context as db_context

    calls: list[dict[str, str | None]] = []
    monkeypatch.setattr(db_context, "current_tenant_id", lambda: "tenant-a")

    def fake_reset(*, tenant_id=None, provider=None, fingerprint=None):
        calls.append(
            {
                "tenant_id": tenant_id,
                "provider": provider,
                "fingerprint": fingerprint,
            }
        )
        return 7

    monkeypatch.setattr(cb, "reset", fake_reset)

    out = await providers.reset_circuit_breaker(
        providers.CircuitResetRequest(
            provider=provider,
            fingerprint=fingerprint,
            scope=scope,
        )
    )

    expected_tenant = None if scope == "all" else "tenant-a"
    assert calls == [
        {
            "tenant_id": expected_tenant,
            "provider": provider,
            "fingerprint": fingerprint,
        }
    ]
    assert out == {
        "status": "reset",
        "cleared": 7,
        "tenant_id": "tenant-a",
        "scope": scope,
    }


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(content=st.text(max_size=2048))
async def test_provider_test_truncates_success_response(monkeypatch, content: str):
    class _Llm:
        def invoke(self, prompt: str):
            assert prompt == "Reply with exactly: OMNISIGHT_OK"
            return SimpleNamespace(content=content)

    monkeypatch.setattr(providers, "get_llm", lambda: _Llm())

    out = await providers.test_provider()

    assert out["status"] == "ok"
    assert out["response"] == content[:200]
    assert len(out["response"]) <= 200


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(message=st.text(max_size=2048))
async def test_provider_test_truncates_error_response(monkeypatch, message: str):
    class _Llm:
        def invoke(self, prompt: str):
            raise RuntimeError(message)

    monkeypatch.setattr(providers, "get_llm", lambda: _Llm())

    out = await providers.test_provider()

    assert out["status"] == "error"
    assert out["error"] == message[:300]
    assert len(out["error"]) <= 300


@hyp_settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(model_spec=st.text(max_size=512))
async def test_validate_model_delegates_without_rewriting(monkeypatch, model_spec: str):
    import backend.agents.llm as llm_mod

    seen: list[str] = []

    def fake_validate(spec: str):
        seen.append(spec)
        return {"ok": True, "model_spec": spec}

    monkeypatch.setattr(llm_mod, "validate_model_spec", fake_validate)

    assert await providers.validate_model(model_spec) == {
        "ok": True,
        "model_spec": model_spec,
    }
    assert seen == [model_spec]
