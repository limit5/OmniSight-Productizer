"""AB.1 — Tool schema registry tests.

Locks:
  - registry has expected eager + deferred tool counts (drift guard)
  - HD skills (26) all registered with skill_hd category
  - to_anthropic_tools() serializes to valid Anthropic tools=[] shape
  - generate_markdown_reference() output matches docs/agents/tool-reference.md
    (run `python -m backend.agents.tool_schemas --regen-doc` if drift)
  - register_tool() rejects duplicate names
  - list_schemas() filters work as documented

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §2
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents.tool_schemas import (
    ToolSchema,
    _REGISTRY,
    generate_markdown_reference,
    get_schema,
    list_schemas,
    register_tool,
    to_anthropic_tools,
)


# ─── Eager tool drift guard ──────────────────────────────────────

EAGER_TOOL_NAMES = {
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Grep",
    "Glob",
    "Agent",
    "WebFetch",
    "ToolSearch",
    "Skill",
}


def test_eager_tools_match_expected_set():
    """Catch accidental promotion / demotion of tools between eager / deferred."""
    eager = {s.name for s in list_schemas(include_deferred=False)}
    assert eager == EAGER_TOOL_NAMES, (
        f"Eager tool set drifted. Expected {EAGER_TOOL_NAMES}, got {eager}. "
        "If intentional, update EAGER_TOOL_NAMES + regen doc."
    )


def test_eager_tools_have_minimum_schema():
    """Each eager tool must have a meaningful input_schema (not empty placeholder)."""
    for name in EAGER_TOOL_NAMES:
        s = get_schema(name)
        assert s.input_schema != {"type": "object"}, (
            f"Eager tool {name} has placeholder input_schema. "
            "Eager tools must specify properties + required."
        )
        assert "properties" in s.input_schema, f"{name}: missing properties"
        assert s.input_schema.get("required"), f"{name}: missing required[]"


# ─── HD skills coverage ──────────────────────────────────────────

EXPECTED_HD_SKILL_COUNT = 28


def test_hd_skills_all_registered():
    """All 26+ HD skills (per ADR §2.2) registered with skill_hd category."""
    hd_skills = [s for s in _REGISTRY.values() if s.category == "skill_hd"]
    assert len(hd_skills) == EXPECTED_HD_SKILL_COUNT, (
        f"Expected {EXPECTED_HD_SKILL_COUNT} HD skills, got {len(hd_skills)}. "
        "If adding new HD skill, update EXPECTED_HD_SKILL_COUNT + ADR §2.2."
    )
    for s in hd_skills:
        assert s.name.startswith("SKILL_HD_"), f"HD skill {s.name} missing prefix"
        assert s.deferred, f"HD skill {s.name} should be deferred"


# ─── Anthropic API serialization ─────────────────────────────────


def test_to_anthropic_tools_default_returns_eager_only():
    """Default call returns all eager tools, no deferred."""
    payload = to_anthropic_tools()
    names = {t["name"] for t in payload}
    assert names == EAGER_TOOL_NAMES


def test_to_anthropic_tools_payload_shape():
    """Each entry has Anthropic-required keys: name, description, input_schema."""
    for entry in to_anthropic_tools():
        assert set(entry.keys()) == {"name", "description", "input_schema"}
        assert isinstance(entry["name"], str)
        assert isinstance(entry["description"], str) and entry["description"]
        assert isinstance(entry["input_schema"], dict)
        assert entry["input_schema"]["type"] == "object"


def test_to_anthropic_tools_named_subset_includes_deferred():
    """Selecting deferred tool by name returns it (use case: batch task with MCP)."""
    payload = to_anthropic_tools(["Read", "WebSearch", "SKILL_HD_PARSE"])
    names = [t["name"] for t in payload]
    assert names == ["Read", "WebSearch", "SKILL_HD_PARSE"]


def test_to_anthropic_tools_unknown_name_raises():
    with pytest.raises(KeyError):
        to_anthropic_tools(["DoesNotExist"])


# ─── Registry mechanics ──────────────────────────────────────────


def test_register_tool_rejects_duplicate():
    """Re-registering an existing name raises (catches typos / bad merges)."""
    schema = ToolSchema(
        name="Read",  # already registered at module load
        description="dup",
        category="filesystem",
        input_schema={"type": "object"},
    )
    with pytest.raises(ValueError, match="already registered"):
        register_tool(schema)


def test_list_schemas_filter_by_category():
    fs = list_schemas(category="filesystem")
    assert {s.name for s in fs} == {"Read", "Write", "Edit"}


def test_list_schemas_include_deferred_flag():
    eager = list_schemas(include_deferred=False)
    everything = list_schemas(include_deferred=True)
    assert len(everything) > len(eager)
    eager_names = {s.name for s in eager}
    everything_names = {s.name for s in everything}
    assert "WebSearch" not in eager_names
    assert "WebSearch" in everything_names


def test_tool_schema_is_frozen():
    """Pydantic model_config frozen=True — schemas immutable post-construction."""
    s = get_schema("Read")
    with pytest.raises((TypeError, ValueError)):
        s.description = "mutated"  # type: ignore[misc]


# ─── Doc sync drift guard ────────────────────────────────────────


DOC_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "agents" / "tool-reference.md"
)


def test_generated_doc_matches_committed_doc():
    """Catch registry/doc drift. Run `python -m backend.agents.tool_schemas --regen-doc`."""
    assert DOC_PATH.exists(), (
        f"{DOC_PATH} missing. Run: python -m backend.agents.tool_schemas --regen-doc"
    )
    expected = generate_markdown_reference()
    actual = DOC_PATH.read_text()
    assert actual == expected, (
        "tool-reference.md out of sync with backend/agents/tool_schemas.py registry. "
        "Run: python -m backend.agents.tool_schemas --regen-doc"
    )


def test_doc_contains_all_eager_tool_anchors():
    """Doc must have an h3 header for every eager tool (visible to readers)."""
    doc = DOC_PATH.read_text()
    for name in EAGER_TOOL_NAMES:
        assert f"### `{name}`" in doc, f"Doc missing anchor for {name}"
