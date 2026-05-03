"""AB.5 — External tool registry + handler tests.

Locks:
  - 7 default tool definitions (HD borrows) construct without error
  - License boundary enforcement (R57): GPL must be subprocess,
    AGPL must be docker_sidecar, GPL/AGPL must require sandbox
  - DEFAULT dispatch table maps known task kinds; unknown falls back
    to generic_dev with warning
  - PythonLibHandler: missing module raises, sync callable wrapped
    in executor, async callable awaited, dict-vs-non-dict result
  - SubprocessHandler: success path returns exit_code+stdout+stderr,
    timeout raises ExternalToolError, missing command raises at init
  - DockerMCPHandler: read-only whitelist blocks write methods (R55),
    missing image raises, returns deferred stub on success
  - DockerSidecarHandler: missing rest_url raises, returns deferred
    stub on success
  - ExternalToolRegistry: build_handler resolves correctly, unknown
    tool raises ToolNotFoundError, disabled raises ToolNotEnabledError,
    seed_default_bindings idempotent
  - list_for_task_kind: passes through Claude Code built-ins,
    filters disabled external tools

ADR: docs/operations/anthropic-api-migration-and-batch-mode.md §5
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone

import pytest

from backend.agents.external_tool_registry import (
    DEFAULT_TOOL_DEFINITIONS,
    DockerMCPHandler,
    DockerSidecarHandler,
    ExternalToolBinding,
    ExternalToolDefinition,
    ExternalToolError,
    ExternalToolRegistry,
    HANDLER_CLASSES,
    InMemoryExternalToolRegistryStore,
    LicenseBoundaryViolation,
    PythonLibHandler,
    SubprocessHandler,
    TASK_KIND_DISPATCH,
    ToolNotEnabledError,
    ToolNotFoundError,
    tools_for_task_kind,
)


# ─── Default definitions sanity ───────────────────────────────────


def test_default_definitions_load_clean():
    """All 7 HD-borrow definitions construct (license boundary checked at __post_init__)."""
    names = {d.tool_name for d in DEFAULT_TOOL_DEFINITIONS}
    assert names == {
        "KiCadMCP", "Altium2KiCad", "OdbDesign",
        "VisionParse", "SKiDL", "PyFDT", "LDParser",
    }


def test_handler_classes_cover_all_integration_types():
    integration_types = {d.integration_type for d in DEFAULT_TOOL_DEFINITIONS}
    assert integration_types <= set(HANDLER_CLASSES.keys())


# ─── License boundary enforcement (R57) ──────────────────────────


def test_license_gpl_must_be_subprocess():
    with pytest.raises(LicenseBoundaryViolation, match="must use integration_type='subprocess'"):
        ExternalToolDefinition(
            tool_name="EvilGPLLib",
            integration_type="python_lib",  # WRONG — would link in-process
            license_tier="gpl",
            sandbox_required=True,
            description="should fail",
        )


def test_license_agpl_must_be_docker_sidecar():
    with pytest.raises(LicenseBoundaryViolation, match="must use integration_type='docker_sidecar'"):
        ExternalToolDefinition(
            tool_name="EvilAGPLLib",
            integration_type="docker_mcp",  # WRONG — STDIO is process boundary, not network
            license_tier="agpl",
            sandbox_required=True,
            description="should fail",
        )


def test_license_gpl_must_require_sandbox():
    with pytest.raises(LicenseBoundaryViolation, match="must set sandbox_required=True"):
        ExternalToolDefinition(
            tool_name="EvilGPLNoSandbox",
            integration_type="subprocess",
            license_tier="gpl",
            sandbox_required=False,  # WRONG
            description="should fail",
        )


def test_license_agpl_must_require_sandbox():
    with pytest.raises(LicenseBoundaryViolation, match="must set sandbox_required=True"):
        ExternalToolDefinition(
            tool_name="EvilAGPLNoSandbox",
            integration_type="docker_sidecar",
            license_tier="agpl",
            sandbox_required=False,  # WRONG
            description="should fail",
        )


def test_license_mit_can_use_any_integration():
    """MIT/Apache tools can use any integration type — boundary is opt-in."""
    for itype in ("python_lib", "subprocess", "docker_mcp", "docker_sidecar"):
        ExternalToolDefinition(
            tool_name=f"MitTool_{itype}",
            integration_type=itype,  # type: ignore[arg-type]
            license_tier="mit_apache_bsd",
            sandbox_required=False,
            description="ok",
        )


# ─── Dispatch table (AB.5.7) ─────────────────────────────────────


def test_dispatch_table_known_kind():
    tools = tools_for_task_kind("hd_parse_kicad")
    assert tools == ("Read", "KiCadMCP", "SKILL_HD_PARSE")


def test_dispatch_table_unknown_falls_back_to_generic():
    tools = tools_for_task_kind("never_seen_kind_xyz")
    assert tools == TASK_KIND_DISPATCH["generic_dev"]


def test_dispatch_table_generic_no_hd_skills():
    """generic_dev should NOT pull in HD-specific tools (saves prompt budget)."""
    tools = TASK_KIND_DISPATCH["generic_dev"]
    assert not any(t.startswith("SKILL_HD_") for t in tools)
    assert "KiCadMCP" not in tools


def test_dispatch_table_hd_diff_includes_required_tools():
    tools = TASK_KIND_DISPATCH["hd_diff_reference"]
    assert "SKILL_HD_DIFF_REFERENCE" in tools
    assert "KiCadMCP" in tools


# ─── PythonLibHandler ────────────────────────────────────────────


def _binding(definition: ExternalToolDefinition, **config_overrides) -> ExternalToolBinding:
    cfg = dict(definition.default_config)
    cfg.update(config_overrides)
    return ExternalToolBinding(definition=definition, config=cfg)


def _vision_def() -> ExternalToolDefinition:
    return next(d for d in DEFAULT_TOOL_DEFINITIONS if d.tool_name == "VisionParse")


@pytest.mark.asyncio
async def test_python_lib_handler_missing_module_raises():
    binding = _binding(_vision_def(), module="totally_made_up_module_xyz")
    handler = PythonLibHandler(binding)
    with pytest.raises(ExternalToolError, match="not installed"):
        await handler({"x": 1})


@pytest.mark.asyncio
async def test_python_lib_handler_sync_callable(monkeypatch):
    """Sync callable runs in executor, result wrapped to dict."""
    fake_module = types.ModuleType("_fake_for_test")

    def fake_callable(**kwargs):
        return {"echoed": kwargs}

    fake_module.MyCallable = fake_callable  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_fake_for_test", fake_module)

    definition = ExternalToolDefinition(
        tool_name="FakeMit",
        integration_type="python_lib",
        license_tier="mit_apache_bsd",
        sandbox_required=False,
        description="for test",
        default_config={"module": "_fake_for_test", "callable": "MyCallable"},
    )
    handler = PythonLibHandler(_binding(definition))
    result = await handler({"x": 1, "y": 2})
    assert result.handler_kind == "python_lib"
    assert result.output == {"echoed": {"x": 1, "y": 2}}
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_python_lib_handler_async_callable(monkeypatch):
    fake_module = types.ModuleType("_fake_async_test")

    async def fake_async(**kwargs):
        return {"async_result": kwargs}

    fake_module.AsyncCall = fake_async  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_fake_async_test", fake_module)

    definition = ExternalToolDefinition(
        tool_name="FakeAsync",
        integration_type="python_lib",
        license_tier="mit_apache_bsd",
        sandbox_required=False,
        description="for test",
        default_config={"module": "_fake_async_test", "callable": "AsyncCall"},
    )
    handler = PythonLibHandler(_binding(definition))
    result = await handler({"a": 1})
    assert result.output == {"async_result": {"a": 1}}


@pytest.mark.asyncio
async def test_python_lib_handler_non_dict_result_wrapped(monkeypatch):
    fake_module = types.ModuleType("_fake_scalar")
    fake_module.scalar = lambda **kwargs: 42  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_fake_scalar", fake_module)
    definition = ExternalToolDefinition(
        tool_name="FakeScalar",
        integration_type="python_lib",
        license_tier="mit_apache_bsd",
        sandbox_required=False,
        description="x",
        default_config={"module": "_fake_scalar", "callable": "scalar"},
    )
    handler = PythonLibHandler(_binding(definition))
    result = await handler({})
    assert result.output == {"result": 42}


def test_python_lib_handler_missing_module_config_raises_at_init():
    definition = ExternalToolDefinition(
        tool_name="NoModule",
        integration_type="python_lib",
        license_tier="mit_apache_bsd",
        sandbox_required=False,
        description="x",
        default_config={},  # no module
    )
    binding = ExternalToolBinding(definition=definition, config={})
    with pytest.raises(ExternalToolError, match="config.module missing"):
        PythonLibHandler(binding)


# ─── SubprocessHandler ───────────────────────────────────────────


def _altium_def() -> ExternalToolDefinition:
    return next(d for d in DEFAULT_TOOL_DEFINITIONS if d.tool_name == "Altium2KiCad")


@pytest.mark.asyncio
async def test_subprocess_handler_runs_echo():
    """Run real /bin/echo to validate the subprocess pipeline end-to-end."""
    definition = ExternalToolDefinition(
        tool_name="EchoTest",
        integration_type="subprocess",
        license_tier="mit_apache_bsd",  # echo is shell builtin / GNU coreutils
        sandbox_required=False,
        description="echo for test",
        default_config={"command": ["/bin/echo"], "timeout_sec": 5},
    )
    binding = _binding(definition)
    handler = SubprocessHandler(binding)
    result = await handler({"args": ["hello", "world"]})
    assert result.handler_kind == "subprocess"
    assert result.output["exit_code"] == 0
    assert "hello world" in result.output["stdout"]


@pytest.mark.asyncio
async def test_subprocess_handler_timeout():
    """Long-running command exceeds timeout → ExternalToolError."""
    definition = ExternalToolDefinition(
        tool_name="SleepTest",
        integration_type="subprocess",
        license_tier="mit_apache_bsd",
        sandbox_required=False,
        description="sleep for test",
        default_config={"command": ["/bin/sleep", "10"], "timeout_sec": 0.1},
    )
    handler = SubprocessHandler(_binding(definition))
    with pytest.raises(ExternalToolError, match="timed out"):
        await handler({})


@pytest.mark.asyncio
async def test_subprocess_handler_stdin_payload():
    """stdin payload (string) flows through to subprocess."""
    definition = ExternalToolDefinition(
        tool_name="CatTest",
        integration_type="subprocess",
        license_tier="mit_apache_bsd",
        sandbox_required=False,
        description="cat",
        default_config={"command": ["/bin/cat"], "timeout_sec": 5},
    )
    handler = SubprocessHandler(_binding(definition))
    result = await handler({"stdin": "echo back this string\n"})
    assert result.output["exit_code"] == 0
    assert "echo back this string" in result.output["stdout"]


def test_subprocess_handler_missing_command_at_init():
    definition = ExternalToolDefinition(
        tool_name="NoCmd",
        integration_type="subprocess",
        license_tier="gpl",
        sandbox_required=True,
        description="x",
        default_config={},
    )
    binding = ExternalToolBinding(definition=definition, config={})
    with pytest.raises(ExternalToolError, match="config.command missing"):
        SubprocessHandler(binding)


def test_subprocess_handler_altium_default_config_correct():
    """Altium2KiCad default config wires Perl + path correctly."""
    definition = _altium_def()
    binding = _binding(definition)
    handler = SubprocessHandler(binding)
    assert handler._command[0] == "perl"
    assert "altium2kicad" in handler._command[1]


# ─── DockerMCPHandler ────────────────────────────────────────────


def _kicad_def() -> ExternalToolDefinition:
    return next(d for d in DEFAULT_TOOL_DEFINITIONS if d.tool_name == "KiCadMCP")


@pytest.mark.asyncio
async def test_docker_mcp_handler_returns_deferred_stub():
    handler = DockerMCPHandler(_binding(_kicad_def()))
    result = await handler({"method": "list_components"})
    assert result.handler_kind == "docker_mcp"
    assert result.output["status"] == "deferred"
    assert result.output["binding"] == "KiCadMCP"


@pytest.mark.asyncio
async def test_docker_mcp_handler_missing_image_raises():
    binding = _binding(_kicad_def(), docker_image=None)
    binding.config.pop("docker_image", None)
    handler = DockerMCPHandler(binding)
    with pytest.raises(ExternalToolError, match="docker_image missing"):
        await handler({"method": "list_components"})


@pytest.mark.asyncio
async def test_docker_mcp_handler_read_only_whitelist_blocks_write():
    """R55: read-only whitelist must block write_/create_/delete_/edit_/modify_ methods."""
    handler = DockerMCPHandler(_binding(_kicad_def()))
    for blocked_method in ["write_file", "create_component", "delete_net", "edit_pad", "modify_layer"]:
        with pytest.raises(ExternalToolError, match="blocked by read-only whitelist"):
            await handler({"method": blocked_method})


@pytest.mark.asyncio
async def test_docker_mcp_handler_read_methods_pass():
    handler = DockerMCPHandler(_binding(_kicad_def()))
    # These don't match the write_/create_/delete_/edit_/modify_ prefix.
    result = await handler({"method": "get_components"})
    assert result.output["status"] == "deferred"


# ─── DockerSidecarHandler ────────────────────────────────────────


def _odb_def() -> ExternalToolDefinition:
    return next(d for d in DEFAULT_TOOL_DEFINITIONS if d.tool_name == "OdbDesign")


@pytest.mark.asyncio
async def test_docker_sidecar_handler_returns_deferred_stub():
    handler = DockerSidecarHandler(_binding(_odb_def()))
    result = await handler({"path": "/tmp/x.odb"})
    assert result.handler_kind == "docker_sidecar"
    assert result.output["status"] == "deferred"
    assert result.output["binding"] == "OdbDesign"
    assert "rest_url" in result.output


@pytest.mark.asyncio
async def test_docker_sidecar_handler_missing_url_raises():
    binding = _binding(_odb_def())
    binding.config.pop("rest_url", None)
    handler = DockerSidecarHandler(binding)
    with pytest.raises(ExternalToolError, match="rest_url missing"):
        await handler({})


# ─── ExternalToolRegistry ────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_seed_idempotent():
    registry = ExternalToolRegistry()
    n1 = await registry.seed_default_bindings()
    n2 = await registry.seed_default_bindings()
    assert n1 == 7
    assert n2 == 0  # already present, no duplicates


@pytest.mark.asyncio
async def test_registry_seed_does_not_overwrite_operator_binding():
    """Existing store rows are operator-owned; seeding fills gaps only."""
    store = InMemoryExternalToolRegistryStore()
    registry = ExternalToolRegistry(store=store)
    vision = _vision_def()
    await store.upsert_binding(
        ExternalToolBinding(
            definition=vision,
            config={"module": "operator_vision_parse", "callable": "CustomParser"},
            enabled=False,
            health_status="degraded",
        )
    )

    inserted = await registry.seed_default_bindings()
    binding = await store.get_binding("VisionParse")

    assert inserted == 6
    assert binding is not None
    assert binding.config == {
        "module": "operator_vision_parse",
        "callable": "CustomParser",
    }
    assert binding.enabled is False
    assert binding.health_status == "degraded"


@pytest.mark.asyncio
async def test_registry_build_handler_unknown_raises():
    registry = ExternalToolRegistry()
    with pytest.raises(ToolNotFoundError, match="not in registry"):
        await registry.build_handler("DefinitelyNotThere")


@pytest.mark.asyncio
async def test_registry_build_handler_unseeded_raises():
    registry = ExternalToolRegistry()
    # Don't seed.
    with pytest.raises(ToolNotFoundError, match="not yet seeded"):
        await registry.build_handler("VisionParse")


@pytest.mark.asyncio
async def test_registry_build_handler_disabled_raises():
    registry = ExternalToolRegistry()
    await registry.seed_default_bindings()
    binding = await registry.store.get_binding("VisionParse")
    assert binding is not None
    binding.enabled = False
    await registry.store.upsert_binding(binding)
    with pytest.raises(ToolNotEnabledError, match="kill-switch"):
        await registry.build_handler("VisionParse")


@pytest.mark.asyncio
async def test_registry_build_handler_returns_correct_class():
    registry = ExternalToolRegistry()
    await registry.seed_default_bindings()
    handler = await registry.build_handler("KiCadMCP")
    assert isinstance(handler, DockerMCPHandler)
    handler = await registry.build_handler("Altium2KiCad")
    assert isinstance(handler, SubprocessHandler)
    handler = await registry.build_handler("OdbDesign")
    assert isinstance(handler, DockerSidecarHandler)
    handler = await registry.build_handler("VisionParse")
    assert isinstance(handler, PythonLibHandler)


@pytest.mark.asyncio
async def test_registry_list_for_task_kind_passes_through_built_ins():
    registry = ExternalToolRegistry()
    await registry.seed_default_bindings()
    tools = await registry.list_for_task_kind("hd_parse_kicad")
    # Read is a Claude Code built-in, not in external registry, must pass through.
    assert "Read" in tools
    assert "KiCadMCP" in tools
    assert "SKILL_HD_PARSE" in tools  # also passes through (HD skill, not external)


@pytest.mark.asyncio
async def test_registry_list_for_task_kind_filters_disabled():
    registry = ExternalToolRegistry()
    await registry.seed_default_bindings()
    binding = await registry.store.get_binding("KiCadMCP")
    assert binding is not None
    binding.enabled = False
    await registry.store.upsert_binding(binding)

    tools = await registry.list_for_task_kind("hd_parse_kicad")
    assert "KiCadMCP" not in tools
    assert "Read" in tools  # built-in still passes through


@pytest.mark.asyncio
async def test_registry_list_for_task_kind_only_enabled_false_keeps_disabled():
    registry = ExternalToolRegistry()
    await registry.seed_default_bindings()
    binding = await registry.store.get_binding("KiCadMCP")
    assert binding is not None
    binding.enabled = False
    await registry.store.upsert_binding(binding)

    tools = await registry.list_for_task_kind("hd_parse_kicad", only_enabled=False)

    assert tools == ["Read", "KiCadMCP", "SKILL_HD_PARSE"]


@pytest.mark.asyncio
async def test_registry_set_health_writes_to_store():
    store = InMemoryExternalToolRegistryStore()
    registry = ExternalToolRegistry(store=store)
    await registry.seed_default_bindings()
    now = datetime.now(timezone.utc)
    await store.set_health("VisionParse", "healthy", now)
    binding = await store.get_binding("VisionParse")
    assert binding is not None
    assert binding.health_status == "healthy"
    assert binding.last_health_check == now


def test_registry_definitions_property_returns_copy():
    registry = ExternalToolRegistry()
    defs = registry.definitions
    defs["evil"] = None  # type: ignore[assignment]
    # Mutating returned dict should NOT affect registry internals.
    assert "evil" not in registry.definitions


@pytest.mark.asyncio
async def test_in_memory_store_list_bindings_enabled_only_filters_disabled():
    store = InMemoryExternalToolRegistryStore()
    await store.upsert_binding(_binding(_vision_def(), module="vision_parse"))
    await store.upsert_binding(
        ExternalToolBinding(
            definition=_kicad_def(),
            config={"docker_image": "ghcr.io/example/kicad-mcp:latest"},
            enabled=False,
        )
    )

    all_bindings = await store.list_bindings()
    enabled_bindings = await store.list_bindings(enabled_only=True)

    assert {b.definition.tool_name for b in all_bindings} == {
        "VisionParse",
        "KiCadMCP",
    }
    assert [b.definition.tool_name for b in enabled_bindings] == ["VisionParse"]
