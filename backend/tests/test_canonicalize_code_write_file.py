"""OP-2632/OP-2633/OP-2648 code-write canonicalizer tests (offline)."""

from __future__ import annotations

import ast
import pathlib
import sys
from collections.abc import Iterator

import pytest

from backend.agents import canonicalize_code_write_file
from backend.agents.action_canonicalize import (
    CanonOutcome,
    CanonicalizationContext,
    CanonicalizationError,
    _restore_state_for_tests,
    _registry_snapshot,
    _snapshot_state_for_tests,
    canonicalize,
    register_canonicalizer,
    reset_for_tests,
)
from backend.agents.canonicalize_code_write_file import (
    _VIEW_REFINED_DESCRIPTOR,
    _canon_sdk_write,
    register_code_write_file_canonicalizers,
)
from backend.agents.tool_registry import OperationDescriptor, resolve


@pytest.fixture(autouse=True)
def restore_canonicalizer_registry() -> Iterator[None]:
    """Prevent registrations in one test from leaking into another."""
    prior = _snapshot_state_for_tests()
    reset_for_tests()
    try:
        yield
    finally:
        _restore_state_for_tests(prior)


def _context(
    tmp_path: pathlib.Path,
    adapter_namespace: str,
    tool_name: str,
    schema_version: str,
) -> CanonicalizationContext:
    return CanonicalizationContext(
        workspace_id="ws-1",
        workspace_root=str(tmp_path),
        adapter_namespace=adapter_namespace,
        tool_name=tool_name,
        schema_version=schema_version,
    )


@pytest.mark.parametrize(
    ("adapter", "tool", "raw_args", "expected_specific", "action", "summary"),
    [
        (
            "specialist",
            "write_file",
            {"path": "src/main.py", "content": "print('ok')\n"},
            {"content": "print('ok')\n"},
            "write",
            "12 bytes",
        ),
        (
            "specialist",
            "write_yaml",
            {"path": "config/app.yaml", "content": "enabled: true\n"},
            {"content": "enabled: true\n"},
            "write",
            "14 bytes",
        ),
        (
            "runner_sdk",
            "Write",
            {"file_path": "notes/output.txt", "content": "ready\n"},
            {"content": "ready\n"},
            "write",
            "6 bytes",
        ),
        (
            "runner_sdk",
            "Edit",
            {
                "file_path": "src/main.py",
                "old_string": "before",
                "new_string": "after",
                "replace_all": True,
            },
            {
                "old_string": "before",
                "new_string": "after",
                "replace_all": True,
            },
            "edit",
            "replace all",
        ),
    ],
    ids=["write_file", "write_yaml", "sdk_write", "sdk_edit"],
)
def test_each_writer_returns_workspace_relative_structured_executable_args(
    tmp_path: pathlib.Path,
    adapter: str,
    tool: str,
    raw_args: dict[str, object],
    expected_specific: dict[str, object],
    action: str,
    summary: str,
) -> None:
    register_code_write_file_canonicalizers()
    raw_path = raw_args.get("path", raw_args.get("file_path"))
    assert isinstance(raw_path, str)
    relative_path = pathlib.Path(raw_path).as_posix()

    prepared = canonicalize(
        _context(tmp_path, adapter, tool, "v1"),
        adapter,
        tool,
        "v1",
        raw_args,
    )

    assert prepared.operation_descriptor == resolve(tool)
    assert prepared.canonical_target == relative_path
    assert prepared.executable_args == {
        "workspace_id": "ws-1",
        "relative_path": relative_path,
        "resolved_at_prepare": str((tmp_path / relative_path).resolve()),
        **expected_specific,
    }
    assert prepared.human_rendering == {
        "tool": tool,
        "action": action,
        "target": relative_path,
        "summary": summary,
    }


@pytest.mark.parametrize(
    ("adapter", "tool", "path_key", "path"),
    [
        ("specialist", "write_file", "path", "../../etc/passwd"),
        ("runner_sdk", "Write", "file_path", "/etc/passwd"),
    ],
    ids=["specialist-no-expanduser", "runner-sdk-expanduser"],
)
def test_path_escape_fails_closed(
    tmp_path: pathlib.Path,
    adapter: str,
    tool: str,
    path_key: str,
    path: str,
) -> None:
    register_code_write_file_canonicalizers()

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(
            _context(tmp_path, adapter, tool, "v1"),
            adapter,
            tool,
            "v1",
            {path_key: path, "content": "blocked"},
        )

    assert caught.value.reason == "path_escapes_workspace"
    assert caught.value.category is CanonOutcome.REJECTED


def test_empty_path_fails_closed() -> None:
    register_code_write_file_canonicalizers()
    context = CanonicalizationContext(
        workspace_id="ws-1",
        workspace_root="/workspace",
        adapter_namespace="specialist",
        tool_name="write_yaml",
        schema_version="v1",
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(
            context,
            "specialist",
            "write_yaml",
            "v1",
            {"path": "", "content": "key: value\n"},
        )

    assert caught.value.reason == "empty_path"


@pytest.mark.parametrize(
    ("raw_args", "reason"),
    [
        ({"path": "output.txt"}, "missing_or_invalid_arg:content"),
        ({"path": 42, "content": "value"}, "missing_or_invalid_arg:path"),
    ],
    ids=["missing-content", "invalid-path"],
)
def test_missing_or_invalid_arg_fails_closed(
    tmp_path: pathlib.Path,
    raw_args: dict[str, object],
    reason: str,
) -> None:
    register_code_write_file_canonicalizers()

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(
            _context(tmp_path, "specialist", "write_file", "v1"),
            "specialist",
            "write_file",
            "v1",
            raw_args,
        )

    assert caught.value.reason == reason
    assert caught.value.category is CanonOutcome.REJECTED


def test_edit_noop_fails_closed(tmp_path: pathlib.Path) -> None:
    register_code_write_file_canonicalizers()

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(
            _context(tmp_path, "runner_sdk", "Edit", "v1"),
            "runner_sdk",
            "Edit",
            "v1",
            {
                "file_path": "src/main.py",
                "old_string": "same",
                "new_string": "same",
            },
        )

    assert caught.value.reason == "edit_noop"
    assert caught.value.category is CanonOutcome.REJECTED


def test_edit_replace_all_defaults_false(tmp_path: pathlib.Path) -> None:
    register_code_write_file_canonicalizers()

    prepared = canonicalize(
        _context(tmp_path, "runner_sdk", "Edit", "v1"),
        "runner_sdk",
        "Edit",
        "v1",
        {
            "file_path": "src/main.py",
            "old_string": "before",
            "new_string": "after",
        },
    )

    assert prepared.executable_args["replace_all"] is False
    assert prepared.human_rendering["summary"] == "replace first"


@pytest.mark.parametrize(
    ("raw_args", "expected_specific", "summary"),
    [
        (
            {"command": "view", "path": "src/main.py", "view_range": (1, 5)},
            {"view_range": [1, 5]},
            "view",
        ),
        (
            {"command": "create", "path": "src/main.py", "file_text": "new\n"},
            {"file_text": "new\n"},
            "4 bytes",
        ),
        (
            {
                "command": "str_replace",
                "path": "src/main.py",
                "old_str": "before",
            },
            {"old_str": "before", "new_str": ""},
            "replace unique",
        ),
        (
            {"command": "insert", "path": "src/main.py", "insert_line": 3},
            {"insert_line": 3, "insert_text": ""},
            "at line 3",
        ),
    ],
    ids=["view", "create", "str-replace", "insert"],
)
def test_text_editor_commands_return_structured_executable_args(
    tmp_path: pathlib.Path,
    raw_args: dict[str, object],
    expected_specific: dict[str, object],
    summary: str,
) -> None:
    register_code_write_file_canonicalizers()

    prepared = canonicalize(
        _context(
            tmp_path,
            "runner_sdk",
            "str_replace_based_edit_tool",
            "v1",
        ),
        "runner_sdk",
        "str_replace_based_edit_tool",
        "v1",
        raw_args,
    )

    command = raw_args["command"]
    assert isinstance(command, str)
    if command == "view":
        assert prepared.operation_descriptor == OperationDescriptor(
            "str_replace_based_edit_tool", "read_only", "read_only"
        )
        assert prepared.operation_descriptor.effect == "read_only"
    else:
        assert prepared.operation_descriptor == resolve(
            "str_replace_based_edit_tool"
        )
        assert prepared.operation_descriptor.effect == "mutating"
    assert prepared.canonical_target == "src/main.py"
    assert prepared.executable_args == {
        "workspace_id": "ws-1",
        "relative_path": "src/main.py",
        "resolved_at_prepare": str((tmp_path / "src/main.py").resolve()),
        "command": command,
        **expected_specific,
    }
    assert prepared.human_rendering == {
        "tool": "str_replace_based_edit_tool",
        "action": command,
        "target": "src/main.py",
        "summary": summary,
    }


def test_text_editor_view_refines_to_read_only(tmp_path: pathlib.Path) -> None:
    register_code_write_file_canonicalizers()

    prepared = canonicalize(
        _context(
            tmp_path,
            "runner_sdk",
            "str_replace_based_edit_tool",
            "v1",
        ),
        "runner_sdk",
        "str_replace_based_edit_tool",
        "v1",
        {"command": "view", "path": "src/main.py"},
    )

    assert prepared.operation_descriptor == _VIEW_REFINED_DESCRIPTOR
    assert prepared.operation_descriptor.effect == "read_only"
    assert prepared.operation_descriptor.family == "read_only"


def test_text_editor_undo_edit_fails_closed(tmp_path: pathlib.Path) -> None:
    register_code_write_file_canonicalizers()

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(
            _context(
                tmp_path,
                "runner_sdk",
                "str_replace_based_edit_tool",
                "v1",
            ),
            "runner_sdk",
            "str_replace_based_edit_tool",
            "v1",
            {"command": "undo_edit", "path": "src/main.py"},
        )

    assert caught.value.reason == "uncanonicalizable_undo_edit"
    assert caught.value.category is CanonOutcome.REJECTED


def test_text_editor_unknown_command_fails_closed(tmp_path: pathlib.Path) -> None:
    register_code_write_file_canonicalizers()

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(
            _context(
                tmp_path,
                "runner_sdk",
                "str_replace_based_edit_tool",
                "v1",
            ),
            "runner_sdk",
            "str_replace_based_edit_tool",
            "v1",
            {"command": "delete", "path": "src/main.py"},
        )

    assert caught.value.reason == "unknown_text_editor_command:delete"


@pytest.mark.parametrize(
    ("raw_args", "reason"),
    [
        (
            {"command": "view", "path": "src/main.py", "view_range": [1, 2, 3]},
            "invalid_view_range",
        ),
        (
            {"command": "view", "path": "src/main.py", "view_range": [0, 5]},
            "invalid_view_range",
        ),
        (
            {"command": "view", "path": "src/main.py", "view_range": [True, 2]},
            "invalid_view_range",
        ),
        (
            {"command": "view", "path": "src/main.py", "view_range": None},
            "invalid_view_range",
        ),
        (
            {"command": "create", "path": "src/main.py"},
            "missing_or_invalid_arg:file_text",
        ),
        (
            {
                "command": "str_replace",
                "path": "src/main.py",
                "old_str": "",
            },
            "empty_old_str",
        ),
        (
            {"command": "insert", "path": "src/main.py", "insert_line": True},
            "missing_or_invalid_arg:insert_line",
        ),
    ],
    ids=[
        "view-range-length",
        "view-range-start",
        "view-range-bool",
        "view-range-none",
        "create-file-text",
        "str-replace-empty-old-str",
        "insert-line-bool",
    ],
)
def test_text_editor_bad_command_args_fail_closed(
    tmp_path: pathlib.Path,
    raw_args: dict[str, object],
    reason: str,
) -> None:
    register_code_write_file_canonicalizers()

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(
            _context(
                tmp_path,
                "runner_sdk",
                "str_replace_based_edit_tool",
                "v1",
            ),
            "runner_sdk",
            "str_replace_based_edit_tool",
            "v1",
            raw_args,
        )

    assert caught.value.reason == reason
    assert caught.value.category is CanonOutcome.REJECTED


def test_text_editor_create_path_escape_fails_closed(tmp_path: pathlib.Path) -> None:
    register_code_write_file_canonicalizers()

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(
            _context(
                tmp_path,
                "runner_sdk",
                "str_replace_based_edit_tool",
                "v1",
            ),
            "runner_sdk",
            "str_replace_based_edit_tool",
            "v1",
            {"command": "create", "path": "../../x", "file_text": "blocked"},
        )

    assert caught.value.reason == "path_escapes_workspace"


def test_require_workspace_fails_closed_through_canonicalize() -> None:
    register_code_write_file_canonicalizers()
    context = CanonicalizationContext(
        workspace_id="ws-1",
        workspace_root="",
        adapter_namespace="specialist",
        tool_name="write_file",
        schema_version="v1",
    )

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(
            context,
            "specialist",
            "write_file",
            "v1",
            {"path": "output.txt", "content": "value"},
        )

    assert caught.value.reason == "no_workspace_context"


def test_canonicalize_deep_freezes_writer_input(tmp_path: pathlib.Path) -> None:
    register_code_write_file_canonicalizers()
    raw_args = {"path": "output.txt", "content": "original"}

    prepared = canonicalize(
        _context(tmp_path, "specialist", "write_file", "v1"),
        "specialist",
        "write_file",
        "v1",
        raw_args,
    )
    raw_args["path"] = "changed.txt"
    raw_args["content"] = "changed"

    assert prepared.canonical_target == "output.txt"
    assert prepared.executable_args["relative_path"] == "output.txt"
    assert prepared.executable_args["content"] == "original"


def test_import_direction_is_stdlib_plus_canonicalization_leafs() -> None:
    source = pathlib.Path(canonicalize_code_write_file.__file__).read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    first_party_allowlist = {
        "backend.agents.action_canonicalize",
        "backend.agents.tool_registry",
    }
    for module in imported:
        if module in first_party_allowlist:
            continue
        root_module = module.partition(".")[0]
        assert root_module in sys.stdlib_module_names, f"non-leaf import: {module}"


def test_registration_adds_exact_keys_and_is_idempotent() -> None:
    before = _registry_snapshot()
    expected = {
        ("specialist", "write_file", "v1"),
        ("specialist", "write_yaml", "v1"),
        ("runner_sdk", "Write", "v1"),
        ("runner_sdk", "Edit", "v1"),
        ("runner_sdk", "str_replace_based_edit_tool", "v1"),
    }

    register_code_write_file_canonicalizers()

    after = _registry_snapshot()
    assert set(after) - set(before) == expected
    assert after[("runner_sdk", "Write", "v1")].fn is _canon_sdk_write
    refined_key = ("runner_sdk", "str_replace_based_edit_tool", "v1")
    assert after[refined_key].refinements == frozenset(
        {_VIEW_REFINED_DESCRIPTOR}
    )
    for key in expected - {refined_key}:
        assert after[key].refinements == frozenset()

    register_code_write_file_canonicalizers()

    assert _registry_snapshot() == after


def test_family_registration_mid_batch_conflict_does_not_mutate() -> None:
    register_canonicalizer(
        "runner_sdk",
        "Write",
        "v1",
        lambda _context, _args: _canon_sdk_write(_context, _args),
    )
    before = _registry_snapshot()

    with pytest.raises(
        ValueError,
        match="conflicting_canonicalizer_registration",
    ):
        register_code_write_file_canonicalizers()

    assert _registry_snapshot() == before
