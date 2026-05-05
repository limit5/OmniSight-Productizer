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
    _load_hd_skill_schemas,
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
    "KnowledgeRetrieval",
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


def test_hd_skills_are_loaded_from_wp2_bundled_skills():
    """BP.B Guild SKILL_HD_* schemas come from WP.2 markdown skills."""
    project_root = Path(__file__).resolve().parents[2]
    loaded = _load_hd_skill_schemas(project_root)
    assert len(loaded) == EXPECTED_HD_SKILL_COUNT
    loaded_names = {s.name for s in loaded}
    registered_names = {
        s.name for s in _REGISTRY.values() if s.category == "skill_hd"
    }
    assert loaded_names == registered_names
    assert get_schema("SKILL_HD_PARSE").description.startswith("[HD.1]")


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


# ─── AB.10.5 schema validation ───────────────────────────────────


def test_validate_schemas_clean_on_default_registry():
    """Every shipped ToolSchema's input_schema is a well-formed JSON Schema
    object (CI gate against future malformed registrations)."""
    from backend.agents.tool_schemas import _validate_schemas

    error_count = _validate_schemas()
    assert error_count == 0


def test_validate_schemas_catches_required_not_in_properties(monkeypatch):
    """Locks the validator catches drift: required field declared but not
    in properties — most common typo."""
    from backend.agents.tool_schemas import _REGISTRY, _validate_schemas, ToolSchema

    bad = ToolSchema(
        name="_BadDriftCanary",
        description="x",
        category="meta",
        input_schema={
            "type": "object",
            "properties": {"foo": {"type": "string"}},
            "required": ["foo", "missing_property"],  # drift — not in properties
        },
    )
    # Inject directly bypassing register_tool() guard.
    monkeypatch.setitem(_REGISTRY, "_BadDriftCanary", bad)

    assert _validate_schemas() >= 1


def test_validate_schemas_catches_missing_property_type(monkeypatch):
    """Property must have 'type' or 'enum' — common Anthropic API rejection."""
    from backend.agents.tool_schemas import _REGISTRY, _validate_schemas, ToolSchema

    bad = ToolSchema(
        name="_BadMissingType",
        description="x",
        category="meta",
        input_schema={
            "type": "object",
            "properties": {"foo": {"description": "no type!"}},
            "required": ["foo"],
        },
    )
    monkeypatch.setitem(_REGISTRY, "_BadMissingType", bad)

    assert _validate_schemas() >= 1


def test_validate_schemas_catches_non_object_root(monkeypatch):
    """Top-level type must be 'object' for Anthropic tools=[] payload."""
    from backend.agents.tool_schemas import _REGISTRY, _validate_schemas, ToolSchema

    bad = ToolSchema(
        name="_BadRootType",
        description="x",
        category="meta",
        input_schema={"type": "array"},  # wrong
    )
    monkeypatch.setitem(_REGISTRY, "_BadRootType", bad)

    assert _validate_schemas() >= 1


# ─── FX.5.12 validator regression coverage ──────────────────────


def test_validate_schemas_catches_non_dict_input_schema(monkeypatch):
    """Input schema itself must be a dict, not a JSON-ish list/string."""
    from backend.agents.tool_schemas import _REGISTRY, _validate_schemas, ToolSchema

    bad = ToolSchema.model_construct(
        name="_BadInputSchemaType",
        description="x",
        category="meta",
        input_schema=["not", "a", "dict"],
        deferred=False,
    )
    monkeypatch.setitem(_REGISTRY, "_BadInputSchemaType", bad)

    assert _validate_schemas() >= 1


def test_validate_schemas_catches_non_dict_properties(monkeypatch):
    """properties must be an object map; Anthropic rejects array-shaped fields."""
    from backend.agents.tool_schemas import _REGISTRY, _validate_schemas, ToolSchema

    bad = ToolSchema(
        name="_BadPropertiesType",
        description="x",
        category="meta",
        input_schema={
            "type": "object",
            "properties": ["foo"],  # wrong
        },
    )
    monkeypatch.setitem(_REGISTRY, "_BadPropertiesType", bad)

    assert _validate_schemas() >= 1


def test_validate_schemas_catches_required_not_list(monkeypatch):
    """required must be a list, not a single string field name."""
    from backend.agents.tool_schemas import _REGISTRY, _validate_schemas, ToolSchema

    bad = ToolSchema(
        name="_BadRequiredType",
        description="x",
        category="meta",
        input_schema={
            "type": "object",
            "properties": {"foo": {"type": "string"}},
            "required": "foo",  # wrong
        },
    )
    monkeypatch.setitem(_REGISTRY, "_BadRequiredType", bad)

    assert _validate_schemas() >= 1


def test_validate_schemas_catches_required_non_string_entry(monkeypatch):
    """required entries must be strings so schema names round-trip exactly."""
    from backend.agents.tool_schemas import _REGISTRY, _validate_schemas, ToolSchema

    bad = ToolSchema(
        name="_BadRequiredEntryType",
        description="x",
        category="meta",
        input_schema={
            "type": "object",
            "properties": {"foo": {"type": "string"}},
            "required": ["foo", 123],  # wrong
        },
    )
    monkeypatch.setitem(_REGISTRY, "_BadRequiredEntryType", bad)

    assert _validate_schemas() >= 1


def test_validate_schemas_catches_property_not_dict(monkeypatch):
    """Each property definition must itself be a JSON Schema object."""
    from backend.agents.tool_schemas import _REGISTRY, _validate_schemas, ToolSchema

    bad = ToolSchema(
        name="_BadPropertyDefinitionType",
        description="x",
        category="meta",
        input_schema={
            "type": "object",
            "properties": {"foo": "string"},  # wrong
        },
    )
    monkeypatch.setitem(_REGISTRY, "_BadPropertyDefinitionType", bad)

    assert _validate_schemas() >= 1
