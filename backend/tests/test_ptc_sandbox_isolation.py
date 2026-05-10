"""OP-828 (B1) — PTC sandbox isolation contract tests.

Locks the AC #2 / AC #3 / AC #7 invariants for the
``code_execution_20260120`` Programmatic Tool Calling sandbox surface:

* AC #2 — sandbox launch refuses if ``OMNISIGHT_WORKTREE_PATH`` is unset
  in the runner's environment, and the launched sandbox's ``cwd`` equals
  the env value.
* AC #3 — ``allowed_callers`` whitelists ``text_editor_20250728`` and
  ``bash_20250124``; HTTP / DB calls raise ``sandbox_boundary_violation``.
* AC #7 — sandbox writes inside the worktree succeed, writes outside the
  worktree are rejected with ``sandbox_boundary_violation``, and HTTP
  requests are rejected with the same code.

The hosted sandbox runs server-side at Anthropic; this test exercises the
runner-side ``PTCSandbox`` boundary validator that mirrors that contract
locally so unit-test failure surfaces well before live API spend.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.agents.tool_dispatcher import (
    PTCSandbox,
    StructuredToolError,
    ToolDispatcher,
    WORKTREE_ENV_VAR,
    bind_built_in_tools,
)


# ─── AC #2 — launch precondition + cwd binding ──────────────────────


def test_sandbox_launch_refuses_when_worktree_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(WORKTREE_ENV_VAR, raising=False)
    with pytest.raises(StructuredToolError) as exc:
        PTCSandbox.launch()
    assert exc.value.error_code == "ptc_creds_missing"


def test_sandbox_launch_uses_worktree_env_value_for_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(WORKTREE_ENV_VAR, str(tmp_path))
    sandbox = PTCSandbox.launch()
    assert sandbox.cwd == tmp_path.resolve()


def test_sandbox_launch_refuses_when_worktree_path_is_not_a_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    not_a_dir = tmp_path / "missing"
    monkeypatch.setenv(WORKTREE_ENV_VAR, str(not_a_dir))
    with pytest.raises(StructuredToolError) as exc:
        PTCSandbox.launch()
    assert exc.value.error_code == "ptc_creds_missing"


# ─── AC #3 — allowed_callers whitelist ──────────────────────────────


def test_allowed_callers_default_to_text_editor_and_bash(
    tmp_path: Path,
) -> None:
    sandbox = PTCSandbox(
        cwd=tmp_path,
        allowed_callers=("text_editor_20250728", "bash_20250124"),
    )
    # Ascend through write_file via each allowed caller — both succeed.
    target_a = sandbox.write_file(
        "from-text-editor.txt", "ok", caller="text_editor_20250728"
    )
    target_b = sandbox.write_file(
        "from-bash.txt", "ok", caller="bash_20250124"
    )
    assert target_a.read_text() == "ok"
    assert target_b.read_text() == "ok"


def test_unknown_caller_raises_sandbox_boundary_violation(
    tmp_path: Path,
) -> None:
    sandbox = PTCSandbox(
        cwd=tmp_path,
        allowed_callers=("text_editor_20250728", "bash_20250124"),
    )
    with pytest.raises(StructuredToolError) as exc:
        sandbox.write_file(
            "x.txt", "y", caller="WebFetch"  # not on the whitelist
        )
    assert exc.value.error_code == "sandbox_boundary_violation"


def test_http_request_raises_sandbox_boundary_violation(
    tmp_path: Path,
) -> None:
    sandbox = PTCSandbox(
        cwd=tmp_path,
        allowed_callers=("text_editor_20250728", "bash_20250124"),
    )
    with pytest.raises(StructuredToolError) as exc:
        sandbox.http_request("https://example.com")
    assert exc.value.error_code == "sandbox_boundary_violation"


def test_db_query_raises_sandbox_boundary_violation(
    tmp_path: Path,
) -> None:
    sandbox = PTCSandbox(
        cwd=tmp_path,
        allowed_callers=("text_editor_20250728", "bash_20250124"),
    )
    with pytest.raises(StructuredToolError) as exc:
        sandbox.db_query("SELECT 1")
    assert exc.value.error_code == "sandbox_boundary_violation"


# ─── AC #7 — write boundary ────────────────────────────────────────


def test_sandbox_writes_inside_worktree_succeed(tmp_path: Path) -> None:
    sandbox = PTCSandbox(
        cwd=tmp_path,
        allowed_callers=("text_editor_20250728", "bash_20250124"),
    )
    written = sandbox.write_file("nested/inside.txt", "hello")
    assert written.is_file()
    assert written.read_text() == "hello"
    assert tmp_path in written.parents


def test_sandbox_writes_outside_worktree_raise_boundary_violation(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    sandbox = PTCSandbox(
        cwd=tmp_path,
        allowed_callers=("text_editor_20250728", "bash_20250124"),
    )
    with pytest.raises(StructuredToolError) as exc:
        sandbox.write_file(str(outside / "leak.txt"), "no")
    assert exc.value.error_code == "sandbox_boundary_violation"


def test_sandbox_relative_traversal_rejected(tmp_path: Path) -> None:
    sandbox = PTCSandbox(
        cwd=tmp_path,
        allowed_callers=("text_editor_20250728", "bash_20250124"),
    )
    with pytest.raises(StructuredToolError) as exc:
        sandbox.write_file("../escape.txt", "no")
    assert exc.value.error_code == "sandbox_boundary_violation"


# ─── Dispatcher round-trip ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_routes_code_execution_http_to_boundary_violation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(WORKTREE_ENV_VAR, str(tmp_path))
    dispatcher = ToolDispatcher()
    bind_built_in_tools(dispatcher, worktree_root=tmp_path)
    result = await dispatcher.execute(
        tool_use_id="tu_http",
        tool_name="code_execution",
        tool_input={
            "caller": "text_editor_20250728",
            "action": "http",
            "url": "https://example.com",
        },
    )
    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["error"] == "sandbox_boundary_violation"


@pytest.mark.asyncio
async def test_dispatcher_routes_code_execution_write_inside_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(WORKTREE_ENV_VAR, str(tmp_path))
    dispatcher = ToolDispatcher()
    bind_built_in_tools(dispatcher, worktree_root=tmp_path)
    result = await dispatcher.execute(
        tool_use_id="tu_write",
        tool_name="code_execution",
        tool_input={
            "caller": "bash_20250124",
            "action": "write",
            "path": "wrote.txt",
            "content": "hi",
        },
    )
    assert result.is_error is False
    assert (tmp_path / "wrote.txt").read_text() == "hi"


@pytest.mark.asyncio
async def test_dispatcher_routes_code_execution_launch_refuses_without_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(WORKTREE_ENV_VAR, raising=False)
    dispatcher = ToolDispatcher()
    bind_built_in_tools(dispatcher, worktree_root=tmp_path)
    result = await dispatcher.execute(
        tool_use_id="tu_no_env",
        tool_name="code_execution",
        tool_input={
            "caller": "bash_20250124",
            "action": "write",
            "path": "wrote.txt",
            "content": "hi",
        },
    )
    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["error"] == "ptc_creds_missing"
