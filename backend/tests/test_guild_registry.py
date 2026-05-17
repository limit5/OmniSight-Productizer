"""Input validation coverage for ``backend.agents.guild_registry``."""

from __future__ import annotations

import pytest

from backend.agents import guild_registry as gr
from backend.sandbox_tier import Guild


VERY_LARGE_AGENT_CLASS = "subscription-codex" * 1024
VERY_LARGE_LEVEL = 10**9


@pytest.mark.parametrize(
    ("agent_class", "exc_type"),
    [
        (None, AttributeError),
        ([], AttributeError),
        ({}, AttributeError),
        (VERY_LARGE_AGENT_CLASS, gr.UnknownAgentClassError),
    ],
)
def test_eligible_guilds_rejects_invalid_agent_class_inputs(
    agent_class: object,
    exc_type: type[BaseException],
) -> None:
    with pytest.raises(exc_type):
        gr.eligible_guilds_for_agent_class(agent_class)  # type: ignore[arg-type]


@pytest.mark.parametrize("agent_class", ["", "   "])
def test_eligible_guilds_rejects_empty_agent_class(agent_class: str) -> None:
    with pytest.raises(gr.UnknownAgentClassError, match="No RPG Guild mapping"):
        gr.eligible_guilds_for_agent_class(agent_class)


@pytest.mark.parametrize(
    ("guild", "exc_type"),
    [
        (None, KeyError),
        ([], TypeError),
        ({}, TypeError),
        ("frontend" * 1024, KeyError),
    ],
)
def test_get_guild_definition_rejects_invalid_guild_inputs(
    guild: object,
    exc_type: type[BaseException],
) -> None:
    with pytest.raises(exc_type):
        gr.get_guild_definition(guild)  # type: ignore[arg-type]


def test_get_guild_definition_accepts_known_slug_string() -> None:
    assert gr.get_guild_definition("frontend").guild == Guild.frontend  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("agent_class", "exc_type"),
    [
        (None, AttributeError),
        ([], AttributeError),
        ({}, AttributeError),
        ("", gr.UnknownAgentClassError),
        (VERY_LARGE_AGENT_CLASS, gr.UnknownAgentClassError),
    ],
)
def test_agent_class_supports_guild_rejects_invalid_agent_class_inputs(
    agent_class: object,
    exc_type: type[BaseException],
) -> None:
    with pytest.raises(exc_type):
        gr.agent_class_supports_guild(agent_class, Guild.frontend)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("guild", "expected"),
    [
        (None, False),
        ("frontend", True),
        ("frontend" * 1024, False),
    ],
)
def test_agent_class_supports_guild_handles_scalar_guild_inputs(
    guild: object,
    expected: bool,
) -> None:
    assert gr.agent_class_supports_guild("subscription-codex", guild) is expected  # type: ignore[arg-type]


@pytest.mark.parametrize("guild", [[], {}])
def test_agent_class_supports_guild_rejects_empty_collection_guild(
    guild: object,
) -> None:
    with pytest.raises(TypeError):
        gr.agent_class_supports_guild("subscription-codex", guild)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "guild",
    [
        None,
        [],
        {},
        "",
        "backend",
        "backend" * 1024,
    ],
)
def test_model_preference_rejects_non_enum_inputs(guild: object) -> None:
    with pytest.raises(AttributeError):
        gr.model_preference_for_guild(guild)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "primary_guild",
    [
        None,
        [],
        {},
    ],
)
def test_secondary_guilds_rejects_wrong_primary_guild_type(
    primary_guild: object,
) -> None:
    with pytest.raises(TypeError, match="primary_guild must be a Guild or string"):
        gr.secondary_guilds_for_agent_class(
            "subscription-codex",
            primary_guild,  # type: ignore[arg-type]
            gr.SECONDARY_GUILD_UNLOCK_LEVEL,
        )


@pytest.mark.parametrize("primary_guild", ["", "   "])
def test_secondary_guilds_rejects_empty_primary_guild(primary_guild: str) -> None:
    with pytest.raises(ValueError, match="primary_guild is required"):
        gr.secondary_guilds_for_agent_class(
            "subscription-codex",
            primary_guild,
            gr.SECONDARY_GUILD_UNLOCK_LEVEL,
        )


@pytest.mark.parametrize(
    ("agent_class", "exc_type"),
    [
        (None, AttributeError),
        ([], AttributeError),
        ({}, AttributeError),
        ("", gr.UnknownAgentClassError),
        (VERY_LARGE_AGENT_CLASS, gr.UnknownAgentClassError),
    ],
)
def test_secondary_guilds_rejects_invalid_agent_class_inputs(
    agent_class: object,
    exc_type: type[BaseException],
) -> None:
    with pytest.raises(exc_type):
        gr.secondary_guilds_for_agent_class(
            agent_class,  # type: ignore[arg-type]
            Guild.frontend,
            gr.SECONDARY_GUILD_UNLOCK_LEVEL,
        )


@pytest.mark.parametrize(
    ("level", "exc_type", "match"),
    [
        (None, TypeError, "level must be an int"),
        ([], TypeError, "level must be an int"),
        ({}, TypeError, "level must be an int"),
        (True, TypeError, "level must be an int"),
        (0, ValueError, "level must be >= 1"),
    ],
)
def test_secondary_guilds_rejects_invalid_level_inputs(
    level: object,
    exc_type: type[BaseException],
    match: str,
) -> None:
    with pytest.raises(exc_type, match=match):
        gr.secondary_guilds_for_agent_class(
            "subscription-codex",
            Guild.frontend,
            level,  # type: ignore[arg-type]
        )


def test_secondary_guilds_accepts_very_large_level() -> None:
    assert gr.secondary_guilds_for_agent_class(
        "subscription-codex",
        Guild.frontend,
        VERY_LARGE_LEVEL,
    ) == frozenset({Guild.custom, Guild.qa, Guild.reporter})


@pytest.mark.parametrize(
    "secondary_guild",
    [
        None,
        [],
        {},
    ],
)
def test_choose_secondary_guild_rejects_wrong_secondary_guild_type(
    secondary_guild: object,
) -> None:
    with pytest.raises(TypeError, match="secondary_guild must be a Guild or string"):
        gr.choose_secondary_guild(
            "subscription-codex",
            Guild.frontend,
            secondary_guild,  # type: ignore[arg-type]
            gr.SECONDARY_GUILD_UNLOCK_LEVEL,
        )


@pytest.mark.parametrize("primary_guild", [None, [], {}])
def test_choose_secondary_guild_rejects_wrong_primary_guild_type(
    primary_guild: object,
) -> None:
    with pytest.raises(TypeError, match="primary_guild must be a Guild or string"):
        gr.choose_secondary_guild(
            "subscription-codex",
            primary_guild,  # type: ignore[arg-type]
            Guild.qa,
            gr.SECONDARY_GUILD_UNLOCK_LEVEL,
        )


@pytest.mark.parametrize("primary_guild", ["", "   "])
def test_choose_secondary_guild_rejects_empty_primary_guild(
    primary_guild: str,
) -> None:
    with pytest.raises(ValueError, match="primary_guild is required"):
        gr.choose_secondary_guild(
            "subscription-codex",
            primary_guild,
            Guild.qa,
            gr.SECONDARY_GUILD_UNLOCK_LEVEL,
        )


@pytest.mark.parametrize("secondary_guild", ["", "   "])
def test_choose_secondary_guild_rejects_empty_secondary_guild(
    secondary_guild: str,
) -> None:
    with pytest.raises(ValueError, match="secondary_guild is required"):
        gr.choose_secondary_guild(
            "subscription-codex",
            Guild.frontend,
            secondary_guild,
            gr.SECONDARY_GUILD_UNLOCK_LEVEL,
        )


@pytest.mark.parametrize(
    ("agent_class", "exc_type"),
    [
        (None, AttributeError),
        ([], AttributeError),
        ({}, AttributeError),
        ("", gr.UnknownAgentClassError),
        (VERY_LARGE_AGENT_CLASS, gr.UnknownAgentClassError),
    ],
)
def test_choose_secondary_guild_rejects_invalid_agent_class_inputs(
    agent_class: object,
    exc_type: type[BaseException],
) -> None:
    with pytest.raises(exc_type):
        gr.choose_secondary_guild(
            agent_class,  # type: ignore[arg-type]
            Guild.frontend,
            Guild.qa,
            gr.SECONDARY_GUILD_UNLOCK_LEVEL,
        )


def test_choose_secondary_guild_rejects_very_large_primary_guild_slug() -> None:
    with pytest.raises(ValueError, match="unknown primary_guild"):
        gr.choose_secondary_guild(
            "subscription-codex",
            "frontend" * 1024,
            Guild.qa,
            gr.SECONDARY_GUILD_UNLOCK_LEVEL,
        )


def test_choose_secondary_guild_rejects_very_large_secondary_guild_slug() -> None:
    with pytest.raises(ValueError, match="unknown secondary_guild"):
        gr.choose_secondary_guild(
            "subscription-codex",
            Guild.frontend,
            "qa" * 1024,
            gr.SECONDARY_GUILD_UNLOCK_LEVEL,
        )


def test_choose_secondary_guild_accepts_very_large_level() -> None:
    choice = gr.choose_secondary_guild(
        "subscription-codex",
        Guild.frontend,
        Guild.qa,
        VERY_LARGE_LEVEL,
    )

    assert choice.agent_class == "subscription-codex"
    assert choice.primary_guild == Guild.frontend
    assert choice.secondary_guild == Guild.qa
    assert choice.level == VERY_LARGE_LEVEL
