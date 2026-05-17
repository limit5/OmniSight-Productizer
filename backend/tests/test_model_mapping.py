"""BP.F.7 -- model mapping parser and routing preference guardrails."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from backend.agents import routing_policy
from backend.sandbox_tier import Guild


@pytest.fixture(autouse=True)
def _restore_model_mapping_cache():
    before_path = routing_policy._MODEL_MAPPING_PATH
    before_cache = routing_policy._MODEL_ROUTING_CACHE
    yield
    routing_policy._MODEL_MAPPING_PATH = before_path
    routing_policy._MODEL_ROUTING_CACHE = before_cache


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("anthropic", "anthropic"),
        ("claude", "anthropic"),
        ("openai", "openai"),
        ("codex", "openai"),
        ("google", "gemini"),
        ("gemini", "gemini"),
        ("xai", "xai"),
        ("grok", "xai"),
        ("Google Gemini Pro", "gemini"),
    ],
)
def test_adr_vendor_label_aliases(raw: str, expected: str) -> None:
    assert routing_policy._adr_vendor_label(raw) == expected


def test_adr_vendor_label_unknown_returns_none() -> None:
    assert routing_policy._adr_vendor_label("deepseek") is None


@pytest.mark.parametrize(
    ("model_spec", "expected"),
    [
        ("anthropic:claude-sonnet-4-20250514", "anthropic"),
        (" google : gemini-1.5-pro ", "google"),
        ("openrouter:anthropic/claude-sonnet-4", "openrouter"),
        ("claude-sonnet-4-20250514", None),
        ("anthropic:", None),
        (":claude-sonnet-4-20250514", None),
    ],
)
def test_provider_from_model_spec(model_spec: str, expected: str | None) -> None:
    assert routing_policy._provider_from_model_spec(model_spec) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("anthropic:claude-sonnet-4-20250514", "anthropic:claude-sonnet-4-20250514"),
        ({"model_spec": " google:gemini-1.5-pro "}, "google:gemini-1.5-pro"),
        ({"model_spec": ""}, ""),
        ({"default_model": "gpt-4o"}, ""),
        (["anthropic:claude-sonnet-4-20250514"], ""),
        (None, ""),
    ],
)
def test_model_spec_from_mapping_value(value: object, expected: str) -> None:
    assert routing_policy._model_spec_from_mapping_value(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Guild.backend, Guild.backend),
        ("backend", Guild.backend),
        (" red-team ", Guild.red_team),
        ("sa-sd", Guild.sa_sd),
        ("unknown", None),
        (None, None),
    ],
)
def test_coerce_guild(value: object, expected: Guild | None) -> None:
    assert routing_policy._coerce_guild(value) == expected


def test_parse_model_routing_matrix_collects_supported_provider_families() -> None:
    _, provider_matrix = routing_policy._parse_model_routing_matrix(
        {
            "providers": {
                "anthropic": {"default_model": "claude-sonnet-4-20250514"},
                "google": {"default_model": "gemini-1.5-pro"},
                "codex": {"default_model": "gpt-4o"},
                "deepseek": {"default_model": "deepseek-chat"},
                "xai": {},
                "bad": "not-a-dict",
            },
            "guilds": {},
        }
    )

    assert provider_matrix == {"anthropic", "gemini", "openai"}


def test_parse_model_routing_matrix_collects_known_guild_specs() -> None:
    guild_specs, _ = routing_policy._parse_model_routing_matrix(
        {
            "providers": {},
            "guilds": {
                "backend": {"model_spec": "anthropic:claude-sonnet-4-20250514"},
                "red-team": {"model_spec": "xai:grok-3-mini"},
                "intel": "google:gemini-1.5-pro",
                "unknown": {"model_spec": "anthropic:claude-haiku-4-20250506"},
                "qa": {"model_spec": ""},
            },
        }
    )

    assert guild_specs == {
        "backend": "anthropic:claude-sonnet-4-20250514",
        "red_team": "xai:grok-3-mini",
        "intel": "google:gemini-1.5-pro",
    }


def test_parse_model_routing_matrix_rejects_non_dict_root() -> None:
    assert routing_policy._parse_model_routing_matrix(["not", "a", "dict"]) == (
        {},
        set(),
    )


def test_load_model_routing_matrix_uses_mtime_cache(tmp_path, monkeypatch) -> None:
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

    first = routing_policy._load_model_routing_matrix()
    mapping_path.write_text("not: the original content\n", encoding="utf-8")
    stat = mapping_path.stat()
    os.utime(mapping_path, (stat.st_atime, routing_policy._MODEL_ROUTING_CACHE[0]))

    assert routing_policy._load_model_routing_matrix() == first


def test_load_model_routing_matrix_reloads_after_mtime_change(
    tmp_path, monkeypatch,
) -> None:
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

    assert routing_policy._load_model_routing_matrix()[0]["backend"].startswith(
        "anthropic:"
    )
    mapping_path.write_text(
        """
version: 1
providers:
  google:
    default_model: gemini-1.5-pro
guilds:
  backend:
    model_spec: google:gemini-1.5-pro
""",
        encoding="utf-8",
    )
    stat = mapping_path.stat()
    os.utime(mapping_path, (stat.st_atime + 2, stat.st_mtime + 2))

    guild_specs, provider_matrix = routing_policy._load_model_routing_matrix()
    assert guild_specs["backend"] == "google:gemini-1.5-pro"
    assert provider_matrix == {"gemini"}


def test_load_model_routing_matrix_missing_file_fails_closed(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        routing_policy, "_MODEL_MAPPING_PATH", tmp_path / "missing.yaml"
    )
    routing_policy._MODEL_ROUTING_CACHE = None

    assert routing_policy._load_model_routing_matrix() == ({}, set())


def test_load_model_routing_matrix_malformed_yaml_fails_closed(
    tmp_path, monkeypatch,
) -> None:
    mapping_path = tmp_path / "model_mapping.yaml"
    mapping_path.write_text("providers: [unterminated\n", encoding="utf-8")
    monkeypatch.setattr(routing_policy, "_MODEL_MAPPING_PATH", mapping_path)
    routing_policy._MODEL_ROUTING_CACHE = None

    assert routing_policy._load_model_routing_matrix() == ({}, set())


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (SimpleNamespace(area=[], guild_id="backend"), "anthropic"),
        (SimpleNamespace(area=[], guild=Guild.intel), "gemini"),
    ],
)
def test_preferred_provider_family_for_task_uses_task_guild(
    task: SimpleNamespace,
    expected: str,
) -> None:
    assert routing_policy._preferred_provider_family_for_task(task) == expected


def test_preferred_provider_family_for_task_uses_area_guild() -> None:
    task = SimpleNamespace(area=["backend"])

    assert routing_policy._preferred_provider_family_for_task(task) == "anthropic"


@pytest.mark.parametrize(
    "task",
    [
        SimpleNamespace(area=["not-a-guild"]),
        SimpleNamespace(area=[]),
    ],
)
def test_preferred_provider_family_for_task_without_mapping_returns_none(
    task: SimpleNamespace,
) -> None:
    assert routing_policy._preferred_provider_family_for_task(task) is None
