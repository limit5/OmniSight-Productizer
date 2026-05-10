"""OP-828 (B1) — built-in tools dispatch contract tests.

Locks the runner-side contract for Anthropic's built-in tool surface:

* ``str_replace_based_edit_tool`` (text_editor_20250728) — view / create /
  str_replace / insert / undo_edit, with structured errors for invalid
  input, missing matches, and worktree escapes (AC #4).
* ``bash`` (bash_20250124) — persistent shell state across calls; the
  ``restart`` action recycles the underlying session (AC #5).
* ``BUILT_IN_TOOLS_SPEC`` shape — exactly the payload Anthropic expects
  for the Phase 1 launcher (AC #1), including ``allowed_callers`` on the
  PTC code-execution tool (AC #3 boundary surface).

Companion test file ``test_ptc_sandbox_isolation.py`` covers the sandbox
boundary invariants (AC #2 / #3 / #7).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# Pull the spec from the runner script so this test file double-locks the
# schema-less Anthropic shape: any drift between the runner constant and
# the AC #1 spec breaks this assertion before it can break the live API
# call.
def _import_built_in_spec() -> list[dict[str, object]]:
    from scripts.run_s1_via_anthropic_sdk import BUILT_IN_TOOLS_SPEC

    return BUILT_IN_TOOLS_SPEC


# ─── Built-in tools spec shape (AC #1) ──────────────────────────────


def test_built_in_tools_spec_matches_anthropic_phase_1_contract() -> None:
    spec = _import_built_in_spec()
    assert spec == [
        {
            "type": "text_editor_20250728",
            "name": "str_replace_based_edit_tool",
        },
        {
            "type": "bash_20250124",
            "name": "bash",
        },
        {
            "type": "code_execution_20260120",
            "name": "code_execution",
            "allowed_callers": [
                "text_editor_20250728",
                "bash_20250124",
            ],
        },
    ]


# ─── TextEditorHandler (AC #4) ──────────────────────────────────────


@pytest.fixture
def text_editor_handler(tmp_path: Path):
    from backend.agents.tool_dispatcher import TextEditorHandler

    return TextEditorHandler(worktree_root=tmp_path), tmp_path


def test_text_editor_view_returns_numbered_lines(
    text_editor_handler: tuple,
) -> None:
    handler, root = text_editor_handler
    target = root / "hello.txt"
    target.write_text("alpha\nbeta\ngamma\n")
    out = handler({"command": "view", "path": str(target)})
    assert "1\talpha" in out
    assert "2\tbeta" in out
    assert "3\tgamma" in out


def test_text_editor_view_supports_view_range(
    text_editor_handler: tuple,
) -> None:
    handler, root = text_editor_handler
    target = root / "many.txt"
    target.write_text("\n".join(f"line{i}" for i in range(10)))
    out = handler(
        {"command": "view", "path": str(target), "view_range": [3, 5]}
    )
    assert "3\tline2" in out
    assert "5\tline4" in out
    # Lines outside the requested range must not bleed in.
    assert "1\tline0" not in out
    assert "6\tline5" not in out


def test_text_editor_create_writes_file_and_pushes_undo(
    text_editor_handler: tuple,
) -> None:
    handler, root = text_editor_handler
    target = root / "new.txt"
    out = handler(
        {"command": "create", "path": str(target), "file_text": "fresh"}
    )
    assert target.read_text() == "fresh"
    assert "created" in out
    # Undo of a create should remove the file again (AC #4 undo_edit
    # roundtrip — locks the snapshot vs delete behaviour).
    handler({"command": "undo_edit", "path": str(target)})
    assert not target.exists()


def test_text_editor_str_replace_unique_match(
    text_editor_handler: tuple,
) -> None:
    handler, root = text_editor_handler
    target = root / "x.py"
    target.write_text("foo = 1\nbar = 2\n")
    handler(
        {
            "command": "str_replace",
            "path": str(target),
            "old_str": "foo = 1",
            "new_str": "foo = 99",
        }
    )
    assert target.read_text() == "foo = 99\nbar = 2\n"


def test_text_editor_str_replace_no_match_emits_structured_error(
    text_editor_handler: tuple,
) -> None:
    from backend.agents.tool_dispatcher import StructuredToolError

    handler, root = text_editor_handler
    target = root / "x.py"
    target.write_text("foo = 1\n")
    with pytest.raises(StructuredToolError) as exc:
        handler(
            {
                "command": "str_replace",
                "path": str(target),
                "old_str": "missing-pattern",
                "new_str": "x",
            }
        )
    assert exc.value.error_code == "text_editor_no_match"


def test_text_editor_insert_at_line(text_editor_handler: tuple) -> None:
    handler, root = text_editor_handler
    target = root / "i.txt"
    target.write_text("a\nb\nc\n")
    handler(
        {
            "command": "insert",
            "path": str(target),
            "insert_line": 2,
            "insert_text": "INS",
        }
    )
    # split("\n") of "a\nb\nc\n" → ['a','b','c','']
    # insert at index 2 → ['a','b','INS','c','']
    assert target.read_text().splitlines() == ["a", "b", "INS", "c"]


def test_text_editor_undo_edit_restores_previous_content(
    text_editor_handler: tuple,
) -> None:
    handler, root = text_editor_handler
    target = root / "u.txt"
    target.write_text("v1")
    handler(
        {
            "command": "str_replace",
            "path": str(target),
            "old_str": "v1",
            "new_str": "v2",
        }
    )
    assert target.read_text() == "v2"
    handler({"command": "undo_edit", "path": str(target)})
    assert target.read_text() == "v1"


def test_text_editor_path_outside_worktree_raises(
    text_editor_handler: tuple,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    from backend.agents.tool_dispatcher import StructuredToolError

    handler, _root = text_editor_handler
    outside = tmp_path_factory.mktemp("outside")
    leaked = outside / "secret.txt"
    leaked.write_text("nope")
    with pytest.raises(StructuredToolError) as exc:
        handler({"command": "view", "path": str(leaked)})
    assert exc.value.error_code == "text_editor_path_outside_worktree"


def test_text_editor_unknown_command_emits_tool_input_invalid(
    text_editor_handler: tuple,
) -> None:
    from backend.agents.tool_dispatcher import StructuredToolError

    handler, root = text_editor_handler
    with pytest.raises(StructuredToolError) as exc:
        handler({"command": "nuke", "path": str(root / "x")})
    assert exc.value.error_code == "tool_input_invalid"


# ─── BashHandlerV2 (AC #5) ──────────────────────────────────────────


@pytest.fixture
def bash_handler_v2(tmp_path: Path):
    from backend.agents.tool_dispatcher import BashHandlerV2

    handler = BashHandlerV2(cwd=tmp_path)
    yield handler, tmp_path
    handler.close()


def test_bash_v2_executes_simple_command(bash_handler_v2: tuple) -> None:
    handler, _root = bash_handler_v2
    out = handler({"command": "echo hello-world"})
    assert "hello-world" in out
    assert "EXIT_CODE: 0" in out


def test_bash_v2_persistent_state_across_calls(
    bash_handler_v2: tuple,
) -> None:
    """AC #5 — ``export`` survives between calls because the underlying
    bash process is kept alive."""
    handler, _root = bash_handler_v2
    handler({"command": "export OP_828_VAR=hello"})
    out = handler({"command": "echo $OP_828_VAR"})
    assert "hello" in out


def test_bash_v2_persistent_cwd_across_calls(
    bash_handler_v2: tuple,
) -> None:
    handler, root = bash_handler_v2
    sub = root / "subdir"
    sub.mkdir()
    handler({"command": f"cd {sub}"})
    out = handler({"command": "pwd"})
    assert str(sub.resolve()) in out


def test_bash_v2_restart_resets_state(bash_handler_v2: tuple) -> None:
    handler, _root = bash_handler_v2
    handler({"command": "export OP_828_VAR=before"})
    handler({"restart": True})
    out = handler({"command": "echo \"[$OP_828_VAR]\""})
    # After restart the env var is gone, so the echo prints empty brackets.
    assert "[]" in out


def test_bash_v2_rejects_missing_command_and_restart(
    bash_handler_v2: tuple,
) -> None:
    from backend.agents.tool_dispatcher import StructuredToolError

    handler, _root = bash_handler_v2
    with pytest.raises(StructuredToolError) as exc:
        handler({})
    assert exc.value.error_code == "tool_input_invalid"


# ─── Dispatcher integration ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_routes_text_editor_view(tmp_path: Path) -> None:
    from backend.agents.tool_dispatcher import (
        ToolDispatcher,
        bind_built_in_tools,
    )

    target = tmp_path / "f.txt"
    target.write_text("xyz")
    dispatcher = ToolDispatcher()
    bind_built_in_tools(dispatcher, worktree_root=tmp_path)
    result = await dispatcher.execute(
        tool_use_id="tu_te",
        tool_name="str_replace_based_edit_tool",
        tool_input={"command": "view", "path": str(target)},
    )
    assert result.is_error is False
    assert "xyz" in result.content


@pytest.mark.asyncio
async def test_dispatcher_text_editor_no_match_is_structured(
    tmp_path: Path,
) -> None:
    from backend.agents.tool_dispatcher import (
        ToolDispatcher,
        bind_built_in_tools,
    )

    target = tmp_path / "f.txt"
    target.write_text("abc")
    dispatcher = ToolDispatcher()
    bind_built_in_tools(dispatcher, worktree_root=tmp_path)
    result = await dispatcher.execute(
        tool_use_id="tu_nm",
        tool_name="str_replace_based_edit_tool",
        tool_input={
            "command": "str_replace",
            "path": str(target),
            "old_str": "ZZZ",
            "new_str": "Y",
        },
    )
    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["error"] == "text_editor_no_match"


@pytest.mark.asyncio
async def test_dispatcher_registers_three_built_in_names(
    tmp_path: Path,
) -> None:
    from backend.agents.tool_dispatcher import (
        ToolDispatcher,
        bind_built_in_tools,
    )

    dispatcher = ToolDispatcher()
    bind_built_in_tools(dispatcher, worktree_root=tmp_path)
    registered = dispatcher.registered_tools()
    assert "str_replace_based_edit_tool" in registered
    assert "bash" in registered
    assert "code_execution" in registered
