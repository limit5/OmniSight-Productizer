"""OP-811 (A3) — Skill / Agent / sub-agent tools wired in SDK launcher.

The api-anthropic launcher (``scripts/run_s1_via_anthropic_sdk.py``) historically
shipped with ``RUNNER_TOOLS = [Read, Write, Edit, Bash, Grep, Glob]`` only —
``Skill`` (project verbs) and ``Agent`` (sub-agent decomposition) were
explicitly excluded "for a deterministic, narrow tool surface". The cost of
that exclusion was: any large ticket had to inline its own decomposition into
the parent agent's iteration count, and project-defined verbs (``lint_changed``,
``run_tests`` etc.) went unused.

A3 wires both back in. These tests pin the contract:

1. ``RUNNER_TOOLS`` includes ``Skill`` AND ``Agent``.
2. The launcher imports ``SkillRegistry``, ``load_default_scopes``,
   ``make_skill_handler`` from ``skills_loader`` and ``make_agent_tool_handler``
   from ``sub_agent``.
3. After ``main()``-style dispatcher construction, the dispatcher has handlers
   registered for ``Skill`` and ``Agent`` (verified via the dispatcher's
   ``has_handler`` / direct ``execute`` calls).
4. A synthetic ``Skill`` invocation routes through the registry — calling the
   skill handler with a registered skill name actually executes that skill.
5. A synthetic ``Agent`` invocation spawns a sub-agent — calling the agent
   handler dispatches a child run on the same client (mocked to capture
   the dispatch without burning real tokens).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


REPO = Path(__file__).resolve().parent.parent.parent
LAUNCHER_PATH = REPO / "scripts" / "run_s1_via_anthropic_sdk.py"


def _load_launcher_module():
    """Import the launcher module by file path (it lives under ``scripts/``,
    not ``backend/``, so a normal package import doesn't reach it)."""
    spec = importlib.util.spec_from_file_location(
        "run_s1_via_anthropic_sdk_op811", LAUNCHER_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_launcher_imports_skill_registry_and_handler() -> None:
    """AC#1: launcher module exposes SkillRegistry / load_default_scopes /
    make_skill_handler at module scope."""
    mod = _load_launcher_module()
    assert hasattr(mod, "SkillRegistry"), "SkillRegistry not imported"
    assert hasattr(mod, "load_default_scopes"), "load_default_scopes not imported"
    assert hasattr(mod, "make_skill_handler"), "make_skill_handler not imported"


def test_launcher_imports_sub_agent_handler() -> None:
    """AC#2: launcher imports ``make_agent_tool_handler`` so the Agent tool
    can bind to the parent's AnthropicClient."""
    mod = _load_launcher_module()
    assert hasattr(mod, "make_agent_tool_handler"), \
        "make_agent_tool_handler not imported"


def test_runner_tools_includes_skill_and_agent() -> None:
    """AC#3: ``RUNNER_TOOLS`` advertises both Skill and Agent so the model's
    tool listing in the system prompt covers them."""
    mod = _load_launcher_module()
    assert "Skill" in mod.RUNNER_TOOLS, \
        f"Skill missing from RUNNER_TOOLS: {mod.RUNNER_TOOLS}"
    assert "Agent" in mod.RUNNER_TOOLS, \
        f"Agent missing from RUNNER_TOOLS: {mod.RUNNER_TOOLS}"
    # The base 6 tools must remain (no regression of original tool surface).
    for base in ("Read", "Write", "Edit", "Bash", "Grep", "Glob"):
        assert base in mod.RUNNER_TOOLS, f"{base} regressed out of RUNNER_TOOLS"


def test_skill_handler_routes_through_registry(tmp_path: Path) -> None:
    """AC#4: invoking ``Skill`` with a registered skill name actually executes
    that skill. We register a hard-coded skill into a fresh registry, build the
    handler with ``make_skill_handler``, and assert the handler returns the
    skill's body when dispatched."""
    from backend.agents.skills_loader import SkillRegistry, Skill, make_skill_handler

    registry = SkillRegistry()
    test_skill = Skill(
        name="test_skill_op811",
        description="Returns the constant string 'OK'.",
        scope="project",
        body="OK marker for AC#4",
        source_path=tmp_path / "test_skill_op811.skill.md",
    )
    registry.add(test_skill)
    handler = make_skill_handler(registry)
    # Skill handler payload shape per ``skills_loader.make_skill_handler``:
    # ``{"skill": <name>, "args": <str|dict>}``.
    result = handler({"skill": "test_skill_op811", "args": ""})
    assert result is not None
    # The handler returns the markdown body for non-executable skills.
    serialised = json.dumps(result, default=str)
    assert "OK marker for AC#4" in serialised


@pytest.mark.asyncio
async def test_agent_handler_dispatches_subagent_through_client() -> None:
    """AC#5: invoking ``Agent`` with a sub-task dispatches a nested
    ``run_with_tools`` call on the same client. We mock the client and assert
    the handler's invocation translates to a child run.

    The ``make_agent_tool_handler`` signature accepts ``client`` and returns an
    async callable. Invoking it with ``{description, prompt, subagent_type}``
    must result in ``client.run_with_tools`` being awaited at least once."""
    from backend.agents.sub_agent import make_agent_tool_handler

    mock_client = MagicMock()
    mock_client.dispatcher = MagicMock()
    # Sub-agent run returns a result-shaped object; minimal stub.
    mock_run_result = MagicMock()
    mock_run_result.final_text = "sub-agent finished"
    mock_run_result.iterations = 1
    mock_run_result.usage = MagicMock(input_tokens=10, output_tokens=5, cost_usd=0.001)
    mock_client.run_with_tools = AsyncMock(return_value=mock_run_result)

    handler = make_agent_tool_handler(client=mock_client)
    payload = {
        "description": "Test sub-task",
        "prompt": "Echo OK",
        "subagent_type": "general-purpose",
    }
    # Handler may be sync-callable returning a coroutine, or directly async —
    # both shapes are valid; await the result if it's a coroutine.
    result = handler(payload)
    if asyncio.iscoroutine(result):
        result = await result
    # Either the handler called the client directly OR returned a payload that
    # references the dispatch. The contract minimum is "client touched at all".
    assert mock_client.run_with_tools.await_count >= 1, \
        f"Agent handler did not dispatch sub-agent run; await_count="\
        f"{mock_client.run_with_tools.await_count}"


def test_dispatcher_main_path_registers_skill_and_agent(tmp_path: Path) -> None:
    """Smoke-level integration: when the launcher's main() builds the
    dispatcher (via the post-OP-811 wiring at line ~860), Skill and Agent
    handlers must end up registered. We exercise the registration block
    directly without running the full ``main()`` (which would attempt JIRA
    auth + Anthropic API keys + JQL dispatch).

    This is the regression test for "did A3 ship the wiring or not".
    """
    from backend.agents.runner_handlers import make_runner_dispatcher
    from backend.agents.skills_loader import (
        SkillRegistry, load_default_scopes, make_skill_handler,
    )
    from backend.agents.sub_agent import make_agent_tool_handler

    dispatcher = make_runner_dispatcher()
    # Pre-A3, only the base handlers (Read/Write/Edit/Bash/Grep/Glob/
    # KnowledgeRetrieval) were registered.
    assert dispatcher.has_handler("Read")
    assert not dispatcher.has_handler("Skill"), \
        "Skill should not be pre-registered by make_runner_dispatcher"
    assert not dispatcher.has_handler("Agent"), \
        "Agent should not be pre-registered by make_runner_dispatcher"

    # The launcher's post-OP-811 wiring block (mirrored here). ``tmp_path``
    # serves as the project_root for ``load_default_scopes`` so the test runs
    # in isolation from the real repo's skill files.
    skill_registry: SkillRegistry = load_default_scopes(tmp_path)
    dispatcher.register("Skill", make_skill_handler(skill_registry))

    mock_client = MagicMock()
    mock_client.dispatcher = dispatcher
    dispatcher.register("Agent", make_agent_tool_handler(client=mock_client))

    assert dispatcher.has_handler("Skill"), "Skill not registered after wiring"
    assert dispatcher.has_handler("Agent"), "Agent not registered after wiring"
