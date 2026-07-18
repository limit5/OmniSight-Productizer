"""OP-2596 — U6-0 T3 tool-metadata registry + resolver (dormant).

Offline unit tests for ``backend.agents.tool_registry``:

  (a) PARITY guard — every LangChain ``TOOL_MAP`` key MUST have a
      classification. A new @tool that isn't classified here fails the
      test loudly (anti-drift). If parity fails, ADD the tool to
      ``TOOL_METADATA`` (read/write by docstring, FAIL-CLOSED to
      ``mutating`` for anything single-name multi-op). The SDK
      ``RUNNER_TOOLS`` are NOT parity-checked — there is no single
      enumerable Python registry for them — they ARE classified in
      ``TOOL_METADATA`` (spot-checked below) and T6 wires the SDK
      adapter against the same table.

  (b) UNKNOWN ⇒ DENY — ``resolve("some_unlisted_tool")`` returns a
      synthetic descriptor with ``effect=="mutating"`` and
      ``family=="__unknown_deny__"``. The kernel must never fall
      through to a permissive default.

  (c) Spot-checks — anchor a few classifications end-to-end so a
      copy-paste rename in ``TOOL_METADATA`` cannot silently flip a
      family (e.g. ``git_push`` from ``code_write`` to ``read_only``).

Dormant confirmation:
  grep -rn "tool_registry\\|OperationDescriptor\\|TOOL_METADATA" \\
      backend/ --include=*.py
returns ONLY the module + this test.
"""

from __future__ import annotations

import dataclasses

import pytest

from backend.agents.tool_registry import (
    KNOWN_EFFECTS,
    KNOWN_FAMILIES,
    KNOWN_OPERATION_CLASSES,
    OperationDescriptor,
    TOOL_METADATA,
    resolve,
)


# ── (a) PARITY guard ────────────────────────────────────────────────────
def test_tool_map_keys_are_subset_of_tool_metadata() -> None:
    """Every LangChain TOOL_MAP entry must be classified.

    If this fails: a new @tool landed in backend/agents/tools.py without
    a matching TOOL_METADATA entry. ADD it — read/write from the
    docstring, FAIL-CLOSED to ``mutating`` for anything single-name
    multi-op. Do NOT relax this assertion.
    """
    from backend.agents.tools import TOOL_MAP

    tool_map_keys = set(TOOL_MAP.keys())
    classified = set(TOOL_METADATA.keys())
    missing = tool_map_keys - classified
    assert not missing, (
        f"Unclassified TOOL_MAP tools (add to TOOL_METADATA): {sorted(missing)}"
    )


# ── (b) UNKNOWN ⇒ DENY ──────────────────────────────────────────────────
def test_resolve_unknown_tool_returns_fail_closed_descriptor() -> None:
    desc = resolve("some_unlisted_tool")
    assert isinstance(desc, OperationDescriptor)
    assert desc.tool_name == "some_unlisted_tool"
    assert desc.effect == "mutating"
    assert desc.family == "__unknown_deny__"


def test_resolve_empty_name_still_fail_closes() -> None:
    """An empty / whitespace tool name is still an unlisted tool — the
    resolver must not permissively pass it through as read_only."""
    desc = resolve("")
    assert desc.effect == "mutating"
    assert desc.family == "__unknown_deny__"


# ── (c) Spot-checks (AC-required) ───────────────────────────────────────
def test_resolve_read_file_is_read_only() -> None:
    assert resolve("read_file").effect == "read_only"


def test_resolve_web_search_registered_name_is_read_only() -> None:
    """The @tool("WebSearch") decorator overrides the Python function
    name (``web_search``) — the TOOL_MAP key is ``"WebSearch"``. The
    registry MUST key on the registered name, not the function name."""
    assert resolve("WebSearch").effect == "read_only"
    # And the Python function name is NOT a registered key — resolve
    # should fail-closed on it, proving the parity direction.
    assert resolve("web_search").family == "__unknown_deny__"


def test_resolve_git_push_is_vcs_write() -> None:
    # B-split: git/VCS tools carry external side effects -> their own family (NOT the workspace-contained code_write).
    assert resolve("git_push").family == "vcs_write"


def test_resolve_save_solution_is_memory_write() -> None:
    """save_solution is classified even though it is currently unbound
    from every guild (H0.5a, OP-2592) — the kernel must already know
    how to classify it when a later ticket re-authorises it."""
    assert resolve("save_solution").family == "memory_write"


def test_resolve_propose_action_is_dangerous_propose() -> None:
    assert resolve("propose_action").family == "dangerous_propose"


def test_resolve_create_task_is_task_write() -> None:
    assert resolve("create_task").family == "task_write"


def test_resolve_sdk_agent_is_delegation() -> None:
    """The SDK sub-agent spawn tool — classified as ``delegation`` so
    T6 can treat it as an authority-crossing hop rather than a normal
    write."""
    assert resolve("Agent").family == "delegation"


# ── FAIL-CLOSED single-name multi-op (bash family) ──────────────────────
@pytest.mark.parametrize("name", ["run_bash", "Bash", "bash"])
def test_resolve_bash_family_is_mutating_shell_exec(name: str) -> None:
    """run_bash / Bash / bash could each execute a read-only OR a
    write shell command — conservatively classified ``mutating``. B-split moves them to their OWN family
    ``shell_exec`` (arbitrary command execution is NOT workspace-contained; it must never share code_write's
    containment reasoning / auto-grant path).
    """
    desc = resolve(name)
    assert desc.effect == "mutating"
    assert desc.family == "shell_exec"


# ── Descriptor shape invariants ─────────────────────────────────────────
def test_operation_descriptor_is_frozen() -> None:
    desc = resolve("read_file")
    with pytest.raises(dataclasses.FrozenInstanceError):
        desc.family = "code_write"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        desc.effect = "mutating"  # type: ignore[misc]


def test_every_metadata_entry_tool_name_matches_its_key() -> None:
    """Guard against copy-paste drift where the dict key and the
    descriptor's ``tool_name`` disagree — that would silently break
    the T6 kernel's identity assumption."""
    for key, desc in TOOL_METADATA.items():
        assert desc.tool_name == key, (
            f"TOOL_METADATA[{key!r}].tool_name = {desc.tool_name!r}; "
            f"key and tool_name must match."
        )


def test_every_metadata_effect_is_valid_literal() -> None:
    for key, desc in TOOL_METADATA.items():
        assert desc.effect in ("read_only", "mutating"), (
            f"{key}: effect={desc.effect!r} outside frozen Literal"
        )


def test_closed_operation_class_vocabularies_are_derived() -> None:
    assert KNOWN_EFFECTS == frozenset({"read_only", "mutating"})
    assert KNOWN_FAMILIES == frozenset(
        descriptor.family for descriptor in TOOL_METADATA.values()
    )
    assert "__unknown_deny__" not in KNOWN_FAMILIES
    assert ("read_only", "read_only") in KNOWN_OPERATION_CLASSES
    assert ("mutating", "code_write") in KNOWN_OPERATION_CLASSES
    assert ("read_only", "deploy") not in KNOWN_OPERATION_CLASSES
    assert ("mutating", "deploy_typo") not in KNOWN_OPERATION_CLASSES


def test_every_metadata_family_is_in_declared_set() -> None:
    """The kernel keys authorization on ``family`` — a typo in a new
    entry (e.g. ``"tickets_write"`` vs ``"ticket_write"``) would create
    an unroutable orphan family. Lock the vocabulary here."""
    valid = {
        "read_only",
        "code_write",
        "shell_exec",
        "vcs_write",
        "gerrit_write",
        "ticket_write",
        "task_write",
        "deploy",
        "artifact_write",
        "memory_write",
        "dangerous_propose",
        "delegation",
        "external_comms",
        "skill_exec",
    }
    for key, desc in TOOL_METADATA.items():
        assert desc.family in valid, (
            f"{key}: family={desc.family!r} outside frozen family set {sorted(valid)}"
        )


def test_read_only_effect_iff_read_only_family() -> None:
    """The two axes are not independent for the current classification:
    ``effect == "read_only"`` iff ``family == "read_only"``. If a future
    tool needs a read-only entry under a NEW family, this test must be
    updated together with the T6 policy table — do NOT relax silently."""
    for key, desc in TOOL_METADATA.items():
        if desc.effect == "read_only":
            assert desc.family == "read_only", (
                f"{key}: effect=read_only but family={desc.family!r}"
            )
        else:
            assert desc.family != "read_only", (
                f"{key}: effect=mutating but family=read_only (inconsistent)"
            )
