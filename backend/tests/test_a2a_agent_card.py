"""BP.A2A.1 — AgentCard schema and specialist descriptor contracts."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

import backend.a2a.agent_card as agent_card_module
from backend.a2a.agent_card import (
    A2A_PROVIDER_IDS,
    DEFAULT_A2A_SCOPES,
    DEFAULT_DISCOVERY_PATH,
    DEFAULT_INVOKE_PATH_TEMPLATE,
    DEFAULT_STREAM_PATH_TEMPLATE,
    PROVIDER_DISCOVERY_PATH_TEMPLATE,
    PROVIDER_INVOKE_PATH_TEMPLATE,
    SCHEMA_VERSION,
    AgentCard,
    CapabilityDescriptor,
    build_agent_card,
    build_capability_descriptors,
    build_provider_agent_card,
    build_provider_agent_cards,
    build_provider_capability_descriptors,
    reload_model_mapping_for_tests,
    resolve_specialist_a2a_endpoint,
)
from backend.sandbox_tier import Guild, admitted_tiers


BASE_URL = "https://omnisight.example.com"


def _capabilities_by_name(card: AgentCard) -> dict[str, CapabilityDescriptor]:
    return {cap.agent_name: cap for cap in card.capabilities}


class TestAgentCardShape:
    def test_builds_public_card_with_endpoint_templates(self) -> None:
        card = build_agent_card(BASE_URL)

        assert card.schema_version == SCHEMA_VERSION == "1.0.0"
        assert card.protocol == "a2a"
        assert card.url == BASE_URL + DEFAULT_DISCOVERY_PATH
        assert card.endpoints.discovery_url == BASE_URL + DEFAULT_DISCOVERY_PATH
        assert card.endpoints.invoke_url_template == BASE_URL + DEFAULT_INVOKE_PATH_TEMPLATE
        assert card.endpoints.stream_url_template == BASE_URL + DEFAULT_STREAM_PATH_TEMPLATE

    def test_declares_pep_oauth_scopes_and_streaming_sse(self) -> None:
        card = build_agent_card(BASE_URL)

        assert card.auth.scheme == "oauth2"
        assert card.auth.scopes == DEFAULT_A2A_SCOPES
        assert card.streaming.supported is True
        assert card.streaming.transport == "sse"
        assert card.protocol_capabilities.streaming is True
        assert card.protocol_capabilities.state_transition_history is True

    def test_card_is_frozen_and_extra_fields_are_rejected(self) -> None:
        card = build_agent_card(BASE_URL)

        with pytest.raises(ValidationError):
            card.name = "changed"  # type: ignore[misc]

        payload = card.model_dump()
        payload["rogue"] = True
        with pytest.raises(ValidationError) as exc:
            AgentCard(**payload)
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_json_round_trip(self) -> None:
        card = build_agent_card(BASE_URL)
        rebuilt = AgentCard.model_validate_json(card.model_dump_json())
        assert rebuilt == card


class TestCapabilityDescriptors:
    def test_generates_one_descriptor_for_each_sandbox_guild(self) -> None:
        descriptors = build_capability_descriptors(BASE_URL)
        by_name = {d.agent_name: d for d in descriptors}

        for guild in Guild:
            assert guild.value in by_name
            cap = by_name[guild.value]
            assert cap.source == "guild"
            assert cap.endpoint_url == f"{BASE_URL}/a2a/invoke/{guild.value}"
            assert cap.stream_endpoint_url == f"{BASE_URL}/a2a/invoke/{guild.value}?stream=true"
            assert cap.admitted_tiers == tuple(sorted(t.value for t in admitted_tiers(guild)))

    @pytest.mark.parametrize("agent_name", ["architect", "bsp", "hal", "isp", "intel"])
    def test_key_guild_specialists_have_public_invoke_urls(self, agent_name: str) -> None:
        card = build_agent_card(BASE_URL)
        cap = _capabilities_by_name(card)[agent_name]

        assert cap.endpoint_url == f"{BASE_URL}/a2a/invoke/{agent_name}"
        assert cap.stream_endpoint_url == f"{BASE_URL}/a2a/invoke/{agent_name}?stream=true"
        assert "guild" in cap.tags

    @pytest.mark.parametrize("agent_name", ["orchestrator", "hd"])
    def test_omnisight_runtime_specialists_are_appended(self, agent_name: str) -> None:
        card = build_agent_card(BASE_URL)
        cap = _capabilities_by_name(card)[agent_name]

        assert cap.endpoint_url == f"{BASE_URL}/a2a/invoke/{agent_name}"
        assert cap.stream_endpoint_url == f"{BASE_URL}/a2a/invoke/{agent_name}?stream=true"
        assert cap.source in {"runtime_specialist", "domain_specialist"}

    def test_trailing_slash_base_url_is_normalized(self) -> None:
        card = build_agent_card(BASE_URL + "/")

        assert card.url == BASE_URL + DEFAULT_DISCOVERY_PATH
        assert _capabilities_by_name(card)["hal"].endpoint_url == f"{BASE_URL}/a2a/invoke/hal"

    @pytest.mark.parametrize("base_url", ["", "omnisight.example.com"])
    def test_public_base_url_is_required_and_absolute(self, base_url: str) -> None:
        with pytest.raises(ValueError):
            build_agent_card(base_url)

    def test_rejects_non_slug_agent_name(self) -> None:
        with pytest.raises(ValidationError):
            CapabilityDescriptor(
                agent_name="Not A Slug",
                display_name="Bad",
                description="bad",
                source="runtime_specialist",
                endpoint_url=f"{BASE_URL}/a2a/invoke/bad",
                stream_endpoint_url=f"{BASE_URL}/a2a/invoke/bad?stream=true",
            )


class TestProviderScopedAgentCards:
    def test_generates_one_card_for_each_supported_provider(self) -> None:
        cards = build_provider_agent_cards(BASE_URL)

        assert len(cards) == 9
        assert {card.provider.lower().split()[0] for card in cards} >= {
            "anthropic",
            "openai",
            "google",
            "xai",
            "groq",
            "deepseek",
            "together",
            "openrouter",
            "ollama",
        }

    @pytest.mark.parametrize("provider_id", A2A_PROVIDER_IDS)
    def test_provider_card_exposes_specialists_as_a2a_endpoints(self, provider_id: str) -> None:
        card = build_provider_agent_card(BASE_URL, provider_id)
        by_name = _capabilities_by_name(card)

        assert card.protocol == "a2a"
        assert card.url == (
            BASE_URL + PROVIDER_DISCOVERY_PATH_TEMPLATE.format(provider_id=provider_id)
        )
        assert card.endpoints.invoke_url_template == (
            BASE_URL + PROVIDER_INVOKE_PATH_TEMPLATE.format(
                provider_id=provider_id,
                agent_name="{agent_name}",
            )
        )
        assert by_name["hal"].source == "provider_specialist"
        assert by_name["hal"].provider_id == provider_id
        assert by_name["hal"].model_spec is not None
        assert by_name["hal"].endpoint_url == (
            f"{BASE_URL}/a2a/providers/{provider_id}/invoke/hal"
        )
        assert "provider" in by_name["hal"].tags
        assert provider_id in by_name["hal"].tags

    def test_provider_capabilities_cover_public_and_graph_specialists(self) -> None:
        public_names = {cap.agent_name for cap in build_capability_descriptors(BASE_URL)}
        provider_names = {
            cap.agent_name
            for cap in build_provider_capability_descriptors(BASE_URL, "openai")
        }

        assert public_names.issubset(provider_names)
        assert {"firmware", "software", "validator", "reviewer", "general"}.issubset(
            provider_names
        )

    def test_orchestrator_resolves_provider_specialist_to_endpoint_only(self) -> None:
        endpoint = resolve_specialist_a2a_endpoint(
            BASE_URL,
            provider_id="openrouter",
            agent_name="reviewer",
        )

        assert endpoint.protocol == "a2a"
        assert endpoint.endpoint_url == (
            f"{BASE_URL}/a2a/providers/openrouter/invoke/reviewer"
        )
        assert endpoint.stream_endpoint_url == (
            f"{BASE_URL}/a2a/providers/openrouter/invoke/reviewer?stream=true"
        )
        assert endpoint.model_spec.startswith("openrouter:")
        assert "ChatOpenAI" not in endpoint.model_dump_json()

    @pytest.mark.parametrize("provider_id", ["", "not-real"])
    def test_unknown_provider_is_rejected(self, provider_id: str) -> None:
        with pytest.raises(ValueError):
            build_provider_agent_card(BASE_URL, provider_id)


class TestModelMappingAgentCards:
    def test_public_card_uses_per_guild_model_mapping(self) -> None:
        card = build_agent_card(BASE_URL)
        by_name = _capabilities_by_name(card)

        assert by_name["architect"].model_spec == "anthropic:claude-opus-4-20250514"
        assert by_name["intel"].model_spec == "google:gemini-1.5-pro"
        assert by_name["reporter"].model_spec == "anthropic:claude-haiku-4-20250506"

    def test_provider_card_keeps_model_spec_inside_provider_boundary(self) -> None:
        card = build_provider_agent_card(BASE_URL, "openai")
        by_name = _capabilities_by_name(card)

        assert by_name["architect"].model_spec == "openai:gpt-4o"
        assert by_name["validator"].model_spec == "openai:gpt-4o"

    def test_model_mapping_reloads_when_yaml_mtime_changes(self, tmp_path, monkeypatch) -> None:
        mapping_path = tmp_path / "model_mapping.yaml"
        mapping_path.write_text(
            """
version: 1
providers:
  openai:
    default_model: gpt-4o-mini
guilds:
  hal:
    model_spec: openai:gpt-4o-mini
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(agent_card_module, "_MODEL_MAPPING_PATH", mapping_path)
        reload_model_mapping_for_tests()

        first = build_agent_card(BASE_URL)
        assert _capabilities_by_name(first)["hal"].model_spec == "openai:gpt-4o-mini"

        mapping_path.write_text(
            """
version: 1
providers:
  openai:
    default_model: gpt-4o
guilds:
  hal:
    model_spec: anthropic:claude-sonnet-4-20250514
""",
            encoding="utf-8",
        )
        stat = mapping_path.stat()
        os.utime(mapping_path, (stat.st_atime + 2, stat.st_mtime + 2))

        second = build_agent_card(BASE_URL)
        provider_card = build_provider_agent_card(BASE_URL, "openai")
        assert _capabilities_by_name(second)["hal"].model_spec == (
            "anthropic:claude-sonnet-4-20250514"
        )
        assert _capabilities_by_name(provider_card)["hal"].model_spec == "openai:gpt-4o"
