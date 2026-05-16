"""RPG.W2.2 -- BP.F Guild model mapping import.

The RPG Guild registry consumes BP.F's ``configs/model_mapping.yaml`` but
keeps MP routing as the authority for provider-family labels. This test
guards the import boundary: Guild model preferences must resolve to
existing routing labels such as ``anthropic``, ``openai``, ``gemini``, or
``xai``; vendor aliases in BP.F (for example ``google``) are normalized
through the same labels routing already consumes.
"""

from __future__ import annotations

import pytest

from backend.agents import guild_registry as gr
from backend.agents import routing_policy
from backend.sandbox_tier import Guild


@pytest.fixture(autouse=True)
def _restore_model_routing_cache():
    before = routing_policy._MODEL_ROUTING_CACHE
    yield
    routing_policy._MODEL_ROUTING_CACHE = before


def test_lists_one_bp_f_model_preference_per_guild() -> None:
    preferences = gr.list_guild_model_preferences()

    assert [preference.guild for preference in preferences] == list(Guild)
    assert {preference.guild.value for preference in preferences} == set(gr.GUILDS)


def test_bp_f_provider_aliases_resolve_to_existing_routing_labels() -> None:
    preferences = {
        preference.guild: preference for preference in gr.list_guild_model_preferences()
    }

    assert preferences[Guild.backend].model_spec == (
        "anthropic:claude-sonnet-4-20250514"
    )
    assert preferences[Guild.backend].provider_family == "anthropic"
    assert preferences[Guild.intel].model_spec == "google:gemini-1.5-pro"
    assert preferences[Guild.intel].provider_family == "gemini"
    assert preferences[Guild.red_team].model_spec == "xai:grok-3-mini"
    assert preferences[Guild.red_team].provider_family == "xai"


def test_imported_provider_families_are_consumed_by_routing_policy() -> None:
    provider_families = frozenset(
        preference.provider_family
        for preference in gr.list_guild_model_preferences()
    )

    assert provider_families <= routing_policy.ROUTING_POLICY_CONSUMED_PROVIDER_LABELS


def test_missing_guild_mapping_fails_closed(tmp_path, monkeypatch) -> None:
    mapping_path = tmp_path / "model_mapping.yaml"
    mapping_path.write_text(
        """
version: 1
providers:
  anthropic:
    default_model: claude-sonnet-4-20250514
guilds:
  backend:
    model_spec: anthropic:claude-sonnet-4-20250514
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(routing_policy, "_MODEL_MAPPING_PATH", mapping_path)
    routing_policy._MODEL_ROUTING_CACHE = None

    assert gr.model_preference_for_guild(Guild.backend).provider_family == "anthropic"
    with pytest.raises(gr.UnknownGuildModelMappingError, match="frontend"):
        gr.model_preference_for_guild(Guild.frontend)


def test_unroutable_provider_mapping_fails_closed(tmp_path, monkeypatch) -> None:
    mapping_path = tmp_path / "model_mapping.yaml"
    mapping_path.write_text(
        """
version: 1
providers:
  groq:
    default_model: llama-3.3-70b-versatile
guilds:
  backend:
    model_spec: groq:llama-3.3-70b-versatile
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(routing_policy, "_MODEL_MAPPING_PATH", mapping_path)
    routing_policy._MODEL_ROUTING_CACHE = None

    with pytest.raises(gr.UnroutableGuildModelMappingError, match="groq"):
        gr.model_preference_for_guild(Guild.backend)
