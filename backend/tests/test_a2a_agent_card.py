"""BP.A2A.1 — AgentCard schema and specialist descriptor contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.a2a.agent_card import (
    DEFAULT_A2A_SCOPES,
    DEFAULT_DISCOVERY_PATH,
    DEFAULT_INVOKE_PATH_TEMPLATE,
    DEFAULT_STREAM_PATH_TEMPLATE,
    SCHEMA_VERSION,
    AgentCard,
    CapabilityDescriptor,
    build_agent_card,
    build_capability_descriptors,
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
