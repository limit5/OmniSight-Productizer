"""OP-2632 code-write file canonicalizer tests (offline)."""

from __future__ import annotations

import ast
import pathlib
import sys
from collections.abc import Iterator

import pytest

from backend.agents import canonicalize_code_write_file
from backend.agents.action_canonicalize import (
    CanonicalizationContext,
    CanonicalizationError,
    _registry_restore,
    _registry_snapshot,
    canonicalize,
)
from backend.agents.canonicalize_code_write_file import (
    register_code_write_file_canonicalizers,
)
from backend.agents.tool_registry import resolve


@pytest.fixture(autouse=True)
def restore_canonicalizer_registry() -> Iterator[None]:
    """Prevent registrations in one test from leaking into another."""
    snapshot = _registry_snapshot()
    try:
        yield
    finally:
        _registry_restore(snapshot)


def _context(
    tmp_path: pathlib.Path,
    adapter_namespace: str,
) -> CanonicalizationContext:
    return CanonicalizationContext(
        workspace_id="ws-1",
        workspace_root=str(tmp_path),
        adapter_namespace=adapter_namespace,
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
        _context(tmp_path, adapter),
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
            _context(tmp_path, adapter),
            adapter,
            tool,
            "v1",
            {path_key: path, "content": "blocked"},
        )

    assert caught.value.reason == "path_escapes_workspace"


def test_empty_path_fails_closed() -> None:
    register_code_write_file_canonicalizers()
    context = CanonicalizationContext(
        workspace_id="ws-1",
        workspace_root="/workspace",
        adapter_namespace="specialist",
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
            _context(tmp_path, "specialist"),
            "specialist",
            "write_file",
            "v1",
            raw_args,
        )

    assert caught.value.reason == reason


def test_edit_noop_fails_closed(tmp_path: pathlib.Path) -> None:
    register_code_write_file_canonicalizers()

    with pytest.raises(CanonicalizationError) as caught:
        canonicalize(
            _context(tmp_path, "runner_sdk"),
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


def test_edit_replace_all_defaults_false(tmp_path: pathlib.Path) -> None:
    register_code_write_file_canonicalizers()

    prepared = canonicalize(
        _context(tmp_path, "runner_sdk"),
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


def test_require_workspace_fails_closed_through_canonicalize() -> None:
    register_code_write_file_canonicalizers()
    context = CanonicalizationContext(
        workspace_id="ws-1",
        workspace_root="",
        adapter_namespace="specialist",
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
        _context(tmp_path, "specialist"),
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


def test_registration_adds_exact_keys_and_rejects_duplicate() -> None:
    before = _registry_snapshot()
    expected = {
        ("specialist", "write_file", "v1"),
        ("specialist", "write_yaml", "v1"),
        ("runner_sdk", "Write", "v1"),
        ("runner_sdk", "Edit", "v1"),
    }

    register_code_write_file_canonicalizers()

    after = _registry_snapshot()
    assert set(after) - set(before) == expected
    with pytest.raises(ValueError, match="duplicate canonicalizer"):
        register_code_write_file_canonicalizers()
