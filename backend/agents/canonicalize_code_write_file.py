"""U6-0 T9/T10 code-write file canonicalizers (dormant)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from backend.agents.action_canonicalize import (
    CanonicalizationContext,
    CanonicalizationError,
    PreparedAction,
    register_canonicalizer,
)
from backend.agents.tool_registry import resolve


def _require_str(raw_args: Mapping[str, object], key: str) -> str:
    """Return one required string argument, or fail closed."""
    value = raw_args.get(key)
    if not isinstance(value, str):
        raise CanonicalizationError(f"missing_or_invalid_arg:{key}")
    return value


def _resolve_in_workspace(
    root: str,
    raw_path: str,
    *,
    expanduser: bool,
) -> tuple[str, str]:
    """Return workspace-relative and absolute paths, failing closed on escape."""
    if not raw_path:
        raise CanonicalizationError("empty_path")

    root_p = Path(root).resolve()
    path = Path(raw_path)
    if expanduser:
        path = path.expanduser()
    if not path.is_absolute():
        path = root_p / path
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root_p)
    except ValueError as exc:
        raise CanonicalizationError("path_escapes_workspace") from exc
    return relative.as_posix(), str(resolved)


def _canon_write_file(
    context: CanonicalizationContext,
    raw_args: Mapping[str, object],
) -> PreparedAction:
    path = _require_str(raw_args, "path")
    content = _require_str(raw_args, "content")
    relative_path, resolved = _resolve_in_workspace(
        context.require_workspace(),
        path,
        expanduser=False,
    )
    return PreparedAction(
        operation_descriptor=resolve("write_file"),
        canonical_target=relative_path,
        executable_args={
            "workspace_id": context.workspace_id,
            "relative_path": relative_path,
            "resolved_at_prepare": resolved,
            "content": content,
        },
        human_rendering={
            "tool": "write_file",
            "action": "write",
            "target": relative_path,
            "summary": f"{len(content.encode('utf-8'))} bytes",
        },
    )


def _canon_write_yaml(
    context: CanonicalizationContext,
    raw_args: Mapping[str, object],
) -> PreparedAction:
    path = _require_str(raw_args, "path")
    content = _require_str(raw_args, "content")
    relative_path, resolved = _resolve_in_workspace(
        context.require_workspace(),
        path,
        expanduser=False,
    )
    return PreparedAction(
        operation_descriptor=resolve("write_yaml"),
        canonical_target=relative_path,
        executable_args={
            "workspace_id": context.workspace_id,
            "relative_path": relative_path,
            "resolved_at_prepare": resolved,
            "content": content,
        },
        human_rendering={
            "tool": "write_yaml",
            "action": "write",
            "target": relative_path,
            "summary": f"{len(content.encode('utf-8'))} bytes",
        },
    )


def _canon_sdk_write(
    context: CanonicalizationContext,
    raw_args: Mapping[str, object],
) -> PreparedAction:
    file_path = _require_str(raw_args, "file_path")
    content = _require_str(raw_args, "content")
    relative_path, resolved = _resolve_in_workspace(
        context.require_workspace(),
        file_path,
        expanduser=True,
    )
    return PreparedAction(
        operation_descriptor=resolve("Write"),
        canonical_target=relative_path,
        executable_args={
            "workspace_id": context.workspace_id,
            "relative_path": relative_path,
            "resolved_at_prepare": resolved,
            "content": content,
        },
        human_rendering={
            "tool": "Write",
            "action": "write",
            "target": relative_path,
            "summary": f"{len(content.encode('utf-8'))} bytes",
        },
    )


def _canon_sdk_edit(
    context: CanonicalizationContext,
    raw_args: Mapping[str, object],
) -> PreparedAction:
    file_path = _require_str(raw_args, "file_path")
    old_string = _require_str(raw_args, "old_string")
    new_string = _require_str(raw_args, "new_string")
    relative_path, resolved = _resolve_in_workspace(
        context.require_workspace(),
        file_path,
        expanduser=True,
    )
    if old_string == new_string:
        raise CanonicalizationError("edit_noop")
    replace_all = bool(raw_args.get("replace_all", False))
    return PreparedAction(
        operation_descriptor=resolve("Edit"),
        canonical_target=relative_path,
        executable_args={
            "workspace_id": context.workspace_id,
            "relative_path": relative_path,
            "resolved_at_prepare": resolved,
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
        },
        human_rendering={
            "tool": "Edit",
            "action": "edit",
            "target": relative_path,
            "summary": f"replace {'all' if replace_all else 'first'}",
        },
    )


def _canon_str_replace(
    context: CanonicalizationContext,
    raw_args: Mapping[str, object],
) -> PreparedAction:
    command = _require_str(raw_args, "command")
    if command == "undo_edit":
        raise CanonicalizationError("uncanonicalizable_undo_edit")
    if command not in {"view", "create", "str_replace", "insert"}:
        raise CanonicalizationError(f"unknown_text_editor_command:{command}")

    path = _require_str(raw_args, "path")
    relative_path, resolved = _resolve_in_workspace(
        context.require_workspace(),
        path,
        expanduser=True,
    )

    cmd_args: dict[str, object]
    if command == "view":
        cmd_args = {}
        if "view_range" in raw_args:
            view_range = raw_args["view_range"]
            if not isinstance(view_range, (list, tuple)) or len(view_range) != 2:
                raise CanonicalizationError("invalid_view_range")
            if any(isinstance(entry, bool) for entry in view_range):
                raise CanonicalizationError("invalid_view_range")
            if any(not isinstance(entry, int) for entry in view_range):
                raise CanonicalizationError("invalid_view_range")
            start, end = view_range
            if start < 1:
                raise CanonicalizationError("invalid_view_range")
            cmd_args["view_range"] = [start, end]
        summary = "view"
    elif command == "create":
        file_text = _require_str(raw_args, "file_text")
        cmd_args = {"file_text": file_text}
        summary = f"{len(file_text.encode('utf-8'))} bytes"
    elif command == "str_replace":
        old_str = _require_str(raw_args, "old_str")
        if old_str == "":
            raise CanonicalizationError("empty_old_str")
        new_str = raw_args.get("new_str", "")
        if not isinstance(new_str, str):
            raise CanonicalizationError("missing_or_invalid_arg:new_str")
        cmd_args = {"old_str": old_str, "new_str": new_str}
        summary = "replace unique"
    else:
        insert_line = raw_args.get("insert_line")
        if isinstance(insert_line, bool) or not isinstance(insert_line, int):
            raise CanonicalizationError("missing_or_invalid_arg:insert_line")
        insert_text = raw_args.get("insert_text", "")
        if not isinstance(insert_text, str):
            raise CanonicalizationError("missing_or_invalid_arg:insert_text")
        cmd_args = {"insert_line": insert_line, "insert_text": insert_text}
        summary = f"at line {insert_line}"

    return PreparedAction(
        operation_descriptor=resolve("str_replace_based_edit_tool"),
        canonical_target=relative_path,
        executable_args={
            "workspace_id": context.workspace_id,
            "relative_path": relative_path,
            "resolved_at_prepare": resolved,
            "command": command,
            **cmd_args,
        },
        human_rendering={
            "tool": "str_replace_based_edit_tool",
            "action": command,
            "target": relative_path,
            "summary": summary,
        },
    )


def register_code_write_file_canonicalizers() -> None:
    """Register the five code-write file adapters."""
    register_canonicalizer("specialist", "write_file", "v1", _canon_write_file)
    register_canonicalizer("specialist", "write_yaml", "v1", _canon_write_yaml)
    register_canonicalizer("runner_sdk", "Write", "v1", _canon_sdk_write)
    register_canonicalizer("runner_sdk", "Edit", "v1", _canon_sdk_edit)
    register_canonicalizer(
        "runner_sdk",
        "str_replace_based_edit_tool",
        "v1",
        _canon_str_replace,
    )
