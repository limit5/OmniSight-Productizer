"""OP-2598 — U6-0 P-REG tool-metadata registry coverage parity (dormant).

Anti-drift guard for the WHOLE production model-callable surface. The
existing ``test_tool_map_keys_are_subset_of_tool_metadata`` only covered
the LangChain ``TOOL_MAP`` slice; the SDK / runner dispatcher assembles
its surface programmatically (``bind_built_in_tools`` / ``bind_memory_tool``
/ ``make_runner_dispatcher`` / the Skill+Agent registrations both
launchers add / the module-level ``_default_dispatcher``) so no static
subset guard could see those names.

This test assembles the same surface by calling the narrow binder
functions DIRECTLY (per the ticket: do NOT import
``scripts/run_s1_via_anthropic_sdk.py`` or ``auto-runner-sdk.py`` — the
latter has a hyphenated filename and a bare ``spec_from_file_location``
load fails; the former imports would drag in dotenv / API-key plumbing
this test does not need). It then asserts:

  1. Every name in each dispatcher's ``registered_tools()`` resolves to a
     REAL family, not the fail-closed ``__unknown_deny__`` synthetic.
  2. The five previously-``__unknown_deny__`` names are classified by
     the intended family — asserted by NAME directly (not only via
     ``registered_tools()``), because ``bind_memory_tool`` returns
     ``None`` when the standalone spike fails or the kill-switch
     ``OMNISIGHT_MEMORY_TOOL_REQUIRES_MA`` is set, so a
     ``registered_tools()``-only pass could silently skip the exact
     tool this ticket cares about.
  3. The dynamic ``external_agent:<clean_agent_id>`` prefix resolves to
     ``delegation`` — with a colon-in-id case exercised too
     (``external_agent:team:bot``), because the outbound A2A node
     (``backend/agents/nodes.py::external_agent_node``) synthesises
     ``f"external_agent:{clean_agent_id}"`` and ``clean_agent_id`` is a
     free-form registry key that MAY itself contain ``:`` — a
     ``split(":")`` fallthrough would truncate that.
  4. ``Skill`` classifies as ``skill_exec`` (NOT bare ``delegation``) —
     ``skills_loader._run_executable_skill`` runs a manifest-pinned,
     hash-verified ``*.skill`` file as a subprocess with the full parent
     environment (P-SKILL containment); sharing the ``delegation`` family
     with ``Agent`` would let an enforce flip on ``code_write`` /
     ``deploy`` be silently bypassed via a skill.
  5. The pre-existing LangChain ``TOOL_MAP`` parity assertion also
     stays green (belt + suspenders — the older test enforces it too).

This module is DORMANT. It does not reference the token names the
kernel-dormancy guard in ``test_authorization_kernel.py`` scans for; it
touches only ``tool_registry.resolve`` + ``TOOL_METADATA``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.agents.tool_dispatcher import (
    ToolDispatcher,
    _default_dispatcher,
    bind_built_in_tools,
)
from backend.agents.tool_registry import TOOL_METADATA, resolve


# ── Fixtures: reproduce the assembled production dispatcher surface ─────


@pytest.fixture()
def runner_dispatcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolDispatcher:
    """Rebuild the SDK/runner dispatcher surface without importing launchers.

    Steps mirror ``scripts/run_s1_via_anthropic_sdk.py`` (SDK runner) +
    ``auto-runner-sdk.py`` (CLI runner) but call the narrow binder
    functions directly:

      * ``make_runner_dispatcher()`` — Read/Write/Edit/Bash/Grep/Glob +
        KnowledgeRetrieval (via ``runner_handlers._HANDLERS``).
      * ``bind_built_in_tools`` — ``str_replace_based_edit_tool`` /
        ``bash`` / ``code_execution`` (same 3 names the SDK launcher's
        ``bind_built_in_tools_with_static_analysis`` registers; we skip
        the static-analysis wrapper here because it only decorates the
        text_editor handler and does not change the REGISTERED name
        surface this test guards).
      * Memory Tool — via ``build_memory_tool_handler`` directly (the
        SDK launcher's ``bind_memory_tool`` is a thin wrapper around it).
      * Skill + Agent — via ``make_skill_handler`` /
        ``make_agent_tool_handler`` on an empty ``SkillRegistry`` /
        stub client, matching what BOTH launchers do after client
        construction.
    """
    from backend.agents.memory_tool_handler import (
        MEMORY_TOOL_NAME,
        build_memory_tool_handler,
    )
    from backend.agents.runner_handlers import make_runner_dispatcher
    from backend.agents.skills_loader import SkillRegistry, make_skill_handler
    from backend.agents.sub_agent import make_agent_tool_handler

    # PTCSandbox.launch (code_execution) requires OMNISIGHT_WORKTREE_PATH
    # at CALL time — registration itself does not touch it, but keep the
    # env in a known state for parity with the launcher precondition.
    monkeypatch.setenv("OMNISIGHT_WORKTREE_PATH", str(tmp_path))
    # Point the Memory Tool storage root at a tmp dir so the handler's
    # writability probe (MemoryDirNotWritable) never fails in the
    # sandbox — /var/omnisight/memory is not writable in test.
    monkeypatch.setenv("OMNISIGHT_MEMORY_TOOL_ROOT", str(tmp_path / "memory"))
    # Ensure the kill-switch is NOT set, so build_memory_tool_handler
    # returns a real handler (and thus registers the ``memory`` name).
    monkeypatch.delenv("OMNISIGHT_MEMORY_TOOL_REQUIRES_MA", raising=False)

    dispatcher = make_runner_dispatcher()
    bind_built_in_tools(dispatcher, worktree_root=tmp_path)

    memory_handler = build_memory_tool_handler(
        fleet_id="test-fleet",
        seed_dir=None,
    )
    # In this test env the spike must pass — assert it did so the test
    # itself is not a silent no-op on the exact tool we care about.
    assert memory_handler is not None, (
        "build_memory_tool_handler returned None in the test env — did "
        "OMNISIGHT_MEMORY_TOOL_REQUIRES_MA leak from another test?"
    )
    dispatcher.register(MEMORY_TOOL_NAME, memory_handler)

    dispatcher.register("Skill", make_skill_handler(SkillRegistry()))
    # make_agent_tool_handler stores the client in a closure and only
    # dereferences it inside the coroutine at CALL time — the mock is
    # never invoked during registration or during resolve() lookups.
    stub_client: Any = MagicMock()
    dispatcher.register("Agent", make_agent_tool_handler(client=stub_client))

    return dispatcher


# ── (1) Every registered name on every dispatcher resolves to a REAL family ──


def test_runner_dispatcher_surface_is_fully_classified(
    runner_dispatcher: ToolDispatcher,
) -> None:
    """No name on the assembled SDK/runner dispatcher may resolve to the
    fail-closed ``__unknown_deny__`` synthetic — that would let the kernel
    (T7+) treat a legitimate production tool as unclassified. If this
    fails: add the missing name to ``TOOL_METADATA`` with the classification
    matching its real side effect (see the CLAUDE.md family vocabulary).
    """
    unknown = [
        name
        for name in runner_dispatcher.registered_tools()
        if resolve(name).family == "__unknown_deny__"
    ]
    assert not unknown, (
        f"Runner dispatcher exposes unclassified tools "
        f"(add to TOOL_METADATA): {sorted(unknown)}"
    )


def test_default_dispatcher_surface_is_fully_classified() -> None:
    """The module-level ``_default_dispatcher`` (used by
    ``anthropic_native_client.run_with_tools`` when no dispatcher is
    injected) currently registers ``SlackPostMessage``. It must also be
    fully classified — same rationale as the runner dispatcher above.
    """
    unknown = [
        name
        for name in _default_dispatcher.registered_tools()
        if resolve(name).family == "__unknown_deny__"
    ]
    assert not unknown, (
        f"Default dispatcher exposes unclassified tools "
        f"(add to TOOL_METADATA): {sorted(unknown)}"
    )


# ── (2) Direct name-level assertions for the 5 target tools ─────────────


def test_memory_tool_resolves_to_memory_write() -> None:
    """Anchor by NAME — ``bind_memory_tool`` returns None (does NOT
    register the ``memory`` name) when the C1 standalone spike fails or
    the operator kill-switch ``OMNISIGHT_MEMORY_TOOL_REQUIRES_MA`` is
    set. A ``registered_tools()``-only pass could silently skip this
    exact tool, so we assert by direct ``resolve()`` too.
    """
    desc = resolve("memory")
    assert desc.effect == "mutating"
    assert desc.family == "memory_write"


def test_code_execution_resolves_to_code_write() -> None:
    """This classifies the DISPATCHER-REGISTERED emulation of
    ``code_execution`` (``tool_dispatcher.ptc_sandbox_handler``). The
    LIVE ``code_execution_20260120`` runs PROVIDER-SIDE inside
    Anthropic's PTC sandbox — provider-side containment is a separate
    ticket (P-PROV); this metadata only governs the local name.
    """
    desc = resolve("code_execution")
    assert desc.effect == "mutating"
    assert desc.family == "code_write"


def test_knowledge_retrieval_is_read_only() -> None:
    """Bound via ``runner_handlers.bind_to_dispatcher`` from the
    ``_HANDLERS`` table — a read-only knowledge lookup, no side effect."""
    desc = resolve("KnowledgeRetrieval")
    assert desc.effect == "read_only"
    assert desc.family == "read_only"


def test_slack_post_message_is_external_comms() -> None:
    """Registered on the module-level ``_default_dispatcher``. Outbound
    message to Slack → ``external_comms``."""
    desc = resolve("SlackPostMessage")
    assert desc.effect == "mutating"
    assert desc.family == "external_comms"


def test_skill_is_skill_exec_not_bare_delegation() -> None:
    """``Skill`` runs ``*.skill`` executable files as arbitrary
    subprocesses with the full parent environment
    (``skills_loader._run_executable_skill`` →
    ``subprocess.run(..., env=os.environ.copy())``). It MUST NOT share
    the ``delegation`` family with ``Agent`` — a plain sub-agent spawn —
    or an enforce flip on ``code_write`` / ``deploy`` could be silently
    bypassed via a skill call.
    """
    desc = resolve("Skill")
    assert desc.effect == "mutating"
    assert desc.family == "skill_exec"
    # And ``Agent`` STAYS ``delegation`` — spot-check so a copy-paste
    # rename does not sweep it into ``skill_exec`` too.
    assert resolve("Agent").family == "delegation"


# ── (3) Dynamic external_agent:<id> prefix (delegation fallthrough) ─────


def test_external_agent_prefix_resolves_to_delegation() -> None:
    """The A2A outbound node names each call ``external_agent:<id>``
    where ``<id>`` is ``agent_id.strip()`` at factory time. Resolver
    fallthrough #1 recognises the prefix and classifies the call as
    ``delegation`` (cross-authority handoff)."""
    desc = resolve("external_agent:demo-agent")
    assert desc.effect == "mutating"
    assert desc.family == "delegation"


def test_external_agent_prefix_tolerates_colon_in_id() -> None:
    """The registry id is a free-form string that MAY itself contain
    ``:`` (e.g. namespaced ids like ``team:bot``). The resolver MUST
    match on the prefix + non-empty remainder — NOT ``split(":")``,
    which would silently truncate the id and misroute the classification.
    """
    desc = resolve("external_agent:team:bot")
    assert desc.effect == "mutating"
    assert desc.family == "delegation"


def test_external_agent_prefix_alone_is_not_delegation() -> None:
    """The bare prefix without an id is NOT a real A2A call — should
    still fail-closed as unknown_deny so a typo doesn't slip through."""
    desc = resolve("external_agent:")
    assert desc.family == "__unknown_deny__"


# ── (4) Belt-and-suspenders LangChain parity (retained from T3) ─────────


def test_tool_map_langchain_parity_still_holds() -> None:
    """Re-assert the T3 parity guard here so a failure in either module's
    test file surfaces the same information — the kernel resolves the
    LangChain surface against this same table."""
    from backend.agents.tools import TOOL_MAP

    missing = set(TOOL_MAP.keys()) - set(TOOL_METADATA.keys())
    assert not missing, (
        f"Unclassified TOOL_MAP tools (add to TOOL_METADATA): {sorted(missing)}"
    )


# ── Sanity: probe env leakage doesn't hide the memory tool ─────────────


def test_memory_env_defaults_do_not_hide_target_from_fixture(
    runner_dispatcher: ToolDispatcher,
) -> None:
    """Guard against a future refactor where the fixture stops
    registering ``memory`` — that would make
    ``test_runner_dispatcher_surface_is_fully_classified`` pass
    trivially. Assert the fixture actually surfaces the target name."""
    assert "memory" in runner_dispatcher.registered_tools()
    assert "code_execution" in runner_dispatcher.registered_tools()
    assert "KnowledgeRetrieval" in runner_dispatcher.registered_tools()
    assert "Skill" in runner_dispatcher.registered_tools()
    assert "Agent" in runner_dispatcher.registered_tools()
