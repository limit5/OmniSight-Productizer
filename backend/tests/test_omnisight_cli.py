"""V9 #2 / #325 — contract tests for the ``omnisight`` CLI MVP.

The tests substitute a stub :class:`OmniSightClient` via Click's
context object so no HTTP traffic is generated. Coverage:

* Group-level wiring (``--help``, ``--version``, env-var resolution).
* Each operator-visible command group (status / workspace list / skills
  list / skills resolve / run / inspect / inject), human + ``--json`` paths.
* Pure formatters and the SSE frame decoder.
* HTTP failure → user-visible :class:`click.ClickException` message.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from backend.cli import formatters
from backend.cli.client import (
    CliConfig,
    DEFAULT_BASE_URL,
    OmniSightCliError,
    _iter_sse_frames,
)
from backend.cli.main import cli


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Stub client + helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _StubClient:
    """Test double that records calls and returns canned payloads."""

    def __init__(
        self,
        *,
        status: dict[str, Any] | None = None,
        workspaces: list[dict[str, Any]] | None = None,
        agents: dict[str, dict[str, Any]] | None = None,
        agent_workspaces: dict[str, dict[str, Any] | None] | None = None,
        run_frames: list[tuple[str, dict[str, Any]]] | None = None,
        inject_payload: dict[str, Any] | None = None,
        raise_on: dict[str, Exception] | None = None,
    ):
        self._status = status or {}
        self._workspaces = workspaces if workspaces is not None else []
        self._agents = agents or {}
        self._agent_workspaces = agent_workspaces or {}
        self._run_frames = run_frames or []
        self._inject_payload = inject_payload or {"ok": True, "hint": {}}
        self._raise = raise_on or {}
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))
        if name in self._raise:
            raise self._raise[name]

    def status(self) -> dict[str, Any]:
        self._record("status")
        return self._status

    def list_workspaces(self) -> list[dict[str, Any]]:
        self._record("list_workspaces")
        return self._workspaces

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        self._record("get_agent", agent_id)
        if agent_id not in self._agents:
            raise OmniSightCliError(f"GET /agents/{agent_id} → HTTP 404: not found")
        return self._agents[agent_id]

    def get_workspace(self, agent_id: str) -> dict[str, Any] | None:
        self._record("get_workspace", agent_id)
        return self._agent_workspaces.get(agent_id)

    def inject_hint(self, agent_id: str, text: str, author: str = "cli") -> dict[str, Any]:
        self._record("inject_hint", agent_id, text, author=author)
        return self._inject_payload

    def run_stream(self, command: str) -> Iterator[tuple[str, dict[str, Any]]]:
        self._record("run_stream", command)
        for frame in self._run_frames:
            yield frame


def _runner_obj(client: _StubClient) -> dict[str, Any]:
    return {
        "config": CliConfig(base_url="http://test", token="t", timeout=5.0),
        "client": client,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CliConfig + env resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCliConfig:
    def test_default_base_url_when_no_env_no_flag(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("OMNISIGHT_BASE_URL", raising=False)
        monkeypatch.delenv("OMNISIGHT_TOKEN", raising=False)
        cfg = CliConfig.resolve(None, None)
        assert cfg.base_url == DEFAULT_BASE_URL
        assert cfg.token == ""

    def test_flag_beats_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OMNISIGHT_BASE_URL", "http://from-env:9000")
        cfg = CliConfig.resolve("http://flag:1234", None)
        assert cfg.base_url == "http://flag:1234"

    def test_token_env_picked_up(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OMNISIGHT_TOKEN", "tok-from-env")
        cfg = CliConfig.resolve(None, None)
        assert cfg.token == "tok-from-env"

    def test_trailing_slash_stripped(self):
        cfg = CliConfig.resolve("http://api/", None)
        assert cfg.base_url == "http://api"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Group plumbing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGroupPlumbing:
    def test_help_lists_commands(self):
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        for cmd in ("status", "workspace", "skills", "run", "inspect", "inject"):
            assert cmd in result.output

    def test_version_flag(self):
        result = CliRunner().invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "omnisight" in result.output.lower()

    def test_workspace_subcommand_help(self):
        result = CliRunner().invoke(cli, ["workspace", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  skills list / resolve
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _write_skill(path: Path, name: str, description: str, body: str = "body\n") -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        f"{body}",
        encoding="utf-8",
    )


def _skills_obj(project: Path, home: Path) -> dict[str, Any]:
    return {
        "config": CliConfig(base_url="http://test", token="t", timeout=5.0),
        "project_root": project,
        "skills_home": home,
    }


class TestSkillsCommand:
    def test_list_prints_effective_source_paths(self, tmp_path: Path):
        project = tmp_path / "project"
        home = tmp_path / "home"
        _write_skill(
            project / "omnisight" / "agents" / "skills" / "alpha" / "SKILL.md",
            "alpha",
            "bundled alpha",
        )
        _write_skill(
            project / ".omnisight" / "skills" / "shared" / "SKILL.md",
            "shared",
            "project shared",
        )
        _write_skill(
            project / "configs" / "skills" / "shared" / "SKILL.md",
            "shared",
            "bundled shared",
        )
        home.mkdir()

        result = CliRunner().invoke(
            cli, ["skills", "list"], obj=_skills_obj(project, home),
        )

        assert result.exit_code == 0, result.output
        assert "NAME" in result.output
        assert "SOURCE_PATH" in result.output
        assert "alpha" in result.output
        assert "shared" in result.output
        assert ".omnisight/skills/shared/SKILL.md" in result.output
        assert "configs/skills/shared/SKILL.md" not in result.output

    def test_list_json_includes_provider_rank(self, tmp_path: Path):
        project = tmp_path / "project"
        home = tmp_path / "home"
        _write_skill(
            project / ".claude" / "skills" / "projected" / "SKILL.md",
            "projected",
            "project skill",
        )
        home.mkdir()

        result = CliRunner().invoke(
            cli, ["--json", "skills", "list"], obj=_skills_obj(project, home),
        )

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed == [
            {
                "description": "project skill",
                "keywords": [],
                "name": "projected",
                "provider_rank": 310,
                "scope": "project",
                "source_path": str(
                    project / ".claude" / "skills" / "projected" / "SKILL.md"
                ),
            }
        ]

    def test_resolve_prints_single_effective_source(self, tmp_path: Path):
        project = tmp_path / "project"
        home = tmp_path / "home"
        _write_skill(
            home / ".omnisight" / "skills" / "shared" / "SKILL.md",
            "shared",
            "home shared",
        )
        _write_skill(
            project / "configs" / "skills" / "shared" / "SKILL.md",
            "shared",
            "bundled shared",
        )

        result = CliRunner().invoke(
            cli, ["skills", "resolve", "shared"], obj=_skills_obj(project, home),
        )

        assert result.exit_code == 0, result.output
        assert "Skill: shared" in result.output
        assert "Scope: home" in result.output
        assert "Provider rank: 220" in result.output
        assert str(home / ".omnisight" / "skills" / "shared" / "SKILL.md") in result.output

    def test_resolve_unknown_skill_is_click_error(self, tmp_path: Path):
        project = tmp_path / "project"
        home = tmp_path / "home"
        home.mkdir()
        result = CliRunner().invoke(
            cli, ["skills", "resolve", "missing"], obj=_skills_obj(project, home),
        )
        assert result.exit_code != 0
        assert "missing" in result.output


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  status
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStatusCommand:
    def test_human_output(self):
        client = _StubClient(status={
            "tasks_completed": 3,
            "tasks_total": 5,
            "agents_running": 2,
            "wsl_status": "OK",
            "usb_status": "1 USB device(s)",
            "cpu_summary": "8 cores",
            "memory_summary": "1024/8000MB (12%)",
            "workspaces_active": 2,
            "containers_active": 1,
        })
        result = CliRunner().invoke(cli, ["status"], obj=_runner_obj(client))
        assert result.exit_code == 0, result.output
        assert "OmniSight system status" in result.output
        assert "3/5" in result.output
        assert "8 cores" in result.output
        assert client.calls[0][0] == "status"

    def test_json_output(self):
        client = _StubClient(status={"tasks_completed": 1, "tasks_total": 1})
        result = CliRunner().invoke(cli, ["--json", "status"], obj=_runner_obj(client))
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["tasks_completed"] == 1

    def test_http_error_surfaces_as_click_exception(self):
        client = _StubClient(raise_on={"status": OmniSightCliError("HTTP 503: down")})
        result = CliRunner().invoke(cli, ["status"], obj=_runner_obj(client))
        assert result.exit_code != 0
        assert "503" in result.output or "down" in result.output


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  workspace list
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestWorkspaceListCommand:
    def test_empty_list_is_friendly(self):
        client = _StubClient(workspaces=[])
        result = CliRunner().invoke(cli, ["workspace", "list"], obj=_runner_obj(client))
        assert result.exit_code == 0
        assert "No active workspaces" in result.output

    def test_table_includes_columns_and_count(self):
        client = _StubClient(workspaces=[
            {"agent_id": "fw-aaa", "task_id": "t-1", "branch": "feat/x",
             "status": "active", "commit_count": 3},
            {"agent_id": "sw-bbb", "task_id": "t-2", "branch": "feat/y",
             "status": "active", "commit_count": 0},
        ])
        result = CliRunner().invoke(cli, ["workspace", "list"], obj=_runner_obj(client))
        assert result.exit_code == 0, result.output
        assert "AGENT_ID" in result.output
        assert "fw-aaa" in result.output
        assert "sw-bbb" in result.output
        assert "2 workspace(s)" in result.output

    def test_json_output_round_trips(self):
        rows = [{"agent_id": "fw-aaa", "task_id": "t-1", "branch": "main",
                 "status": "active", "commit_count": 1}]
        client = _StubClient(workspaces=rows)
        result = CliRunner().invoke(cli, ["--json", "workspace", "list"], obj=_runner_obj(client))
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed[0]["agent_id"] == "fw-aaa"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  run
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRunCommand:
    def test_streams_each_event_line_human(self):
        client = _StubClient(run_frames=[
            ("analysis", {"agents_total": 4, "tasks_completed": 2}),
            ("execute", {"action": "spawn", "agent": "fw-1"}),
            ("done", {"message": "ok"}),
        ])
        result = CliRunner().invoke(
            cli, ["run", "ship", "the", "thing"], obj=_runner_obj(client),
        )
        assert result.exit_code == 0, result.output
        assert "[analysis]" in result.output
        assert "[execute]" in result.output
        assert "[done] ok" in result.output
        assert client.calls[0] == ("run_stream", ("ship the thing",), {})

    def test_quiet_only_emits_last_frame(self):
        client = _StubClient(run_frames=[
            ("analysis", {"x": 1}),
            ("execute", {"y": 2}),
            ("done", {"message": "final"}),
        ])
        result = CliRunner().invoke(cli, ["run", "--quiet", "do x"], obj=_runner_obj(client))
        assert result.exit_code == 0
        assert "[analysis]" not in result.output
        assert "[done] final" in result.output

    def test_json_emits_one_object_per_line(self):
        client = _StubClient(run_frames=[
            ("analysis", {"a": 1}),
            ("done", {"message": "ok"}),
        ])
        result = CliRunner().invoke(cli, ["--json", "run", "go"], obj=_runner_obj(client))
        assert result.exit_code == 0
        # Each frame is its own indented JSON object — split on "}\n{" or
        # parse by reading whole-file as a JSON stream.
        decoder = json.JSONDecoder()
        idx = 0
        frames = []
        text = result.output
        while idx < len(text):
            while idx < len(text) and text[idx].isspace():
                idx += 1
            if idx >= len(text):
                break
            obj, end = decoder.raw_decode(text, idx)
            frames.append(obj)
            idx = end
        assert len(frames) == 2
        assert frames[0]["event"] == "analysis"
        assert frames[1]["data"]["message"] == "ok"

    def test_empty_prompt_rejected(self):
        client = _StubClient()
        result = CliRunner().invoke(cli, ["run", "   "], obj=_runner_obj(client))
        assert result.exit_code != 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  inspect
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestInspectCommand:
    def _agent(self, **over: Any) -> dict[str, Any]:
        base = {
            "id": "fw-aaa",
            "name": "Firmware Alpha",
            "type": "firmware",
            "sub_type": "bsp",
            "status": "running",
            "progress": {"current": 4, "total": 10},
            "thought_chain": "Step 1: scan registers\nStep 2: build",
            "ai_model": "anthropic/claude-sonnet-4-6",
        }
        base.update(over)
        return base

    def test_renders_agent_thought_chain_and_workspace(self):
        client = _StubClient(
            agents={"fw-aaa": self._agent()},
            agent_workspaces={"fw-aaa": {
                "branch": "feat/isp", "status": "active",
                "commit_count": 7, "path": "/tmp/ws/fw-aaa",
            }},
        )
        result = CliRunner().invoke(cli, ["inspect", "fw-aaa"], obj=_runner_obj(client))
        assert result.exit_code == 0, result.output
        assert "fw-aaa" in result.output
        assert "Firmware Alpha" in result.output
        assert "Step 1: scan registers" in result.output
        assert "feat/isp" in result.output
        assert "7 commit" in result.output

    def test_no_workspace_branch(self):
        client = _StubClient(
            agents={"fw-aaa": self._agent(thought_chain="")},
            agent_workspaces={"fw-aaa": None},
        )
        result = CliRunner().invoke(cli, ["inspect", "fw-aaa"], obj=_runner_obj(client))
        assert result.exit_code == 0
        assert "No active workspace" in result.output

    def test_unknown_agent_404_surfaces(self):
        client = _StubClient(agents={})
        result = CliRunner().invoke(cli, ["inspect", "ghost-1"], obj=_runner_obj(client))
        assert result.exit_code != 0
        assert "404" in result.output

    def test_json_payload_includes_both_blocks(self):
        client = _StubClient(
            agents={"fw-aaa": self._agent()},
            agent_workspaces={"fw-aaa": {"branch": "main", "status": "active",
                                          "commit_count": 0, "path": "/tmp"}},
        )
        result = CliRunner().invoke(cli, ["--json", "inspect", "fw-aaa"], obj=_runner_obj(client))
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["agent"]["id"] == "fw-aaa"
        assert parsed["workspace"]["branch"] == "main"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  inject
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestInjectCommand:
    def test_happy_path_emits_chars_count(self):
        client = _StubClient(inject_payload={
            "ok": True,
            "hint": {"agent_id": "fw-aaa", "text": "look at register 0x12",
                     "author": "cli"},
        })
        result = CliRunner().invoke(
            cli, ["inject", "fw-aaa", "look", "at", "register", "0x12"],
            obj=_runner_obj(client),
        )
        assert result.exit_code == 0, result.output
        assert "fw-aaa" in result.output
        assert "21 chars" in result.output
        # Multi-word hint preserved verbatim
        call = client.calls[-1]
        assert call[0] == "inject_hint"
        assert call[1] == ("fw-aaa", "look at register 0x12")
        assert call[2]["author"] == "cli"

    def test_author_flag_passes_through(self):
        client = _StubClient(inject_payload={"ok": True, "hint": {}})
        result = CliRunner().invoke(
            cli, ["inject", "--author", "ops@team", "fw-aaa", "do thing"],
            obj=_runner_obj(client),
        )
        assert result.exit_code == 0
        assert client.calls[-1][2]["author"] == "ops@team"

    def test_blank_hint_rejected_locally(self):
        client = _StubClient()
        result = CliRunner().invoke(cli, ["inject", "fw-aaa", "  "], obj=_runner_obj(client))
        assert result.exit_code != 0

    def test_rate_limit_error_surfaces(self):
        client = _StubClient(raise_on={
            "inject_hint": OmniSightCliError("HTTP 429: rate-limit exceeded"),
        })
        result = CliRunner().invoke(
            cli, ["inject", "fw-aaa", "hint"], obj=_runner_obj(client),
        )
        assert result.exit_code != 0
        assert "429" in result.output


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure formatters
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFormatters:
    def test_status_aligns_columns(self):
        out = formatters.format_status({
            "tasks_completed": 1, "tasks_total": 1,
            "agents_running": 0, "wsl_status": "OK",
            "cpu_summary": "4 cores",
        })
        assert "Tasks completed" in out
        assert "1/1" in out
        assert "4 cores" in out

    def test_workspace_list_alignment_by_widest_cell(self):
        out = formatters.format_workspace_list([
            {"agent_id": "a", "task_id": "tt", "branch": "main",
             "status": "active", "commit_count": 0},
            {"agent_id": "longer-id", "task_id": "x", "branch": "feat/looong",
             "status": "active", "commit_count": 99},
        ])
        # AGENT_ID column should be at least as wide as longest cell
        for line in out.splitlines()[:1]:
            assert "AGENT_ID" in line
        assert "longer-id" in out

    def test_inspect_truncates_very_long_thought_chain(self):
        agent = {
            "id": "x", "name": "n", "type": "t",
            "status": "running", "progress": {"current": 0, "total": 1},
            "thought_chain": "x" * 5000,
        }
        out = formatters.format_inspect(agent, None)
        assert "…" in out
        # Snippet should not exceed cap + ellipsis
        assert "x" * 1500 not in out

    def test_run_event_uses_message_field_when_present(self):
        line = formatters.format_run_event("done", {"message": "ok!"})
        assert line == "[done] ok!"

    def test_run_event_falls_back_to_compact_json(self):
        line = formatters.format_run_event("info", {"k": 1})
        assert line.startswith("[info] ")
        assert '"k":1' in line

    def test_inject_result_includes_char_count(self):
        out = formatters.format_inject_result({
            "hint": {"agent_id": "a", "text": "hello world", "author": "x"},
        })
        assert "11 chars" in out
        assert "author=x" in out

    def test_inject_result_when_hint_missing(self):
        out = formatters.format_inject_result({})
        # Don't crash; emit a generic confirmation.
        assert "?" in out or "injected" in out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SSE frame decoder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSseDecoder:
    def test_two_complete_frames(self):
        lines = [
            "event: analysis",
            'data: {"a": 1}',
            "",
            "event: done",
            'data: {"message": "ok"}',
            "",
        ]
        frames = list(_iter_sse_frames(iter(lines)))
        assert frames == [("analysis", {"a": 1}), ("done", {"message": "ok"})]

    def test_default_event_is_message(self):
        lines = ['data: {"k": "v"}', ""]
        frames = list(_iter_sse_frames(iter(lines)))
        assert frames == [("message", {"k": "v"})]

    def test_keepalive_comment_skipped(self):
        lines = [": keep-alive", "event: ping", "data: {}", ""]
        frames = list(_iter_sse_frames(iter(lines)))
        assert frames == [("ping", {})]

    def test_non_json_data_falls_back_to_message_wrap(self):
        lines = ["event: text", "data: hello there", ""]
        frames = list(_iter_sse_frames(iter(lines)))
        assert frames == [("text", {"message": "hello there"})]

    def test_unterminated_final_frame_still_emitted(self):
        lines = ["event: tail", 'data: {"x": 9}']
        frames = list(_iter_sse_frames(iter(lines)))
        assert frames == [("tail", {"x": 9})]
