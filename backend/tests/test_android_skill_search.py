"""P12.4 #352 — End-to-end test for ``android_skill_search`` tool.

Acceptance: agent searches the Android Skills MCP for ``"navigation3"`` →
gets the correct skill content → that content is usable as a code-generation
context for an android-kotlin agent.

The test mocks ``asyncio.create_subprocess_exec`` with a fake stdio
subprocess that speaks the MCP JSON-RPC handshake
(``initialize`` → ``notifications/initialized`` → ``tools/call``). This
exercises the *real* :func:`backend.agents.tools._call_mcp_tool` wire
protocol — only the subprocess itself is faked. We deliberately avoid
spawning the real ``npx -y android-skills-mcp`` binary because (a) it
requires Node + npm cache + network, (b) the contract under test is the
agent ↔ MCP boundary, not the upstream skill catalogue.

Module-global state audit (SOP Step 1): none — ``_call_mcp_tool`` keeps
no module-level state; each invocation spawns its own subprocess and
holds local StreamReader/Writer pairs. Tests therefore do not need any
``_reset_for_tests``-style hook between cases.
"""

from __future__ import annotations

import asyncio
import json
from typing import Iterable

import pytest

from backend.agents import tools as agent_tools
from backend.agents.tools import (
    AGENT_TOOLS,
    MCP_TOOLS,
    TOOL_MAP,
    android_skill_search,
)


# ─── Fake MCP subprocess ──────────────────────────────────────────────


_NAV3_SEARCH_PAYLOAD = (
    "id: navigation3\n"
    "title: Navigation 3 — type-safe destinations\n"
    "summary: Use NavController + composable destinations with @Serializable "
    "route classes for compile-time safe navigation.\n"
    "snippet: |\n"
    "  @Serializable data object Home\n"
    "  @Serializable data class Detail(val id: String)\n"
    "  val navController = rememberNavController()\n"
    "  NavHost(navController, startDestination = Home) {\n"
    "    composable<Home> { HomeScreen(onOpen = { navController.navigate(Detail(it)) }) }\n"
    "    composable<Detail> { backStackEntry ->\n"
    "      val args = backStackEntry.toRoute<Detail>()\n"
    "      DetailScreen(args.id)\n"
    "    }\n"
    "  }\n"
)

_NAV3_GET_PAYLOAD = (
    "# Navigation 3\n\n"
    "Navigation 3 introduces type-safe destinations using @Serializable\n"
    "route objects/classes consumed by NavHost / composable<T>.\n\n"
    "Migration from Navigation 2:\n"
    "* Replace string routes with @Serializable types.\n"
    "* Replace navigate(\"detail/$id\") with navigate(Detail(id)).\n"
    "* Read args via backStackEntry.toRoute<Detail>().\n"
)


class _FakeMCPProcess:
    """Stand-in for ``asyncio.subprocess.Process`` that speaks MCP JSON-RPC.

    The fake reads JSON-RPC line-delimited messages off stdin, tracks the
    handshake state, and writes pre-canned responses for the request IDs
    the real client sends (1 = initialize, 2 = tools/call). Notifications
    (no ``id``) are accepted silently.

    Each instance owns asyncio in-memory StreamReader/StreamWriter pairs
    so the production code's ``proc.stdin.write`` / ``proc.stdout.readline``
    calls behave exactly as they do against a real subprocess.
    """

    def __init__(
        self,
        *,
        tool_responses: dict[str, dict],
        captured_calls: list[dict],
        returncode_after_close: int = 0,
    ) -> None:
        self._tool_responses = tool_responses
        self._captured_calls = captured_calls
        self._returncode_after_close = returncode_after_close
        self.returncode: int | None = None

        loop = asyncio.get_event_loop()

        # Pipe 1: production code writes to stdin → server reads.
        self._stdin_reader = asyncio.StreamReader(loop=loop)
        stdin_protocol = asyncio.StreamReaderProtocol(self._stdin_reader, loop=loop)
        stdin_transport = _LoopbackTransport(self._stdin_reader)
        self.stdin = asyncio.StreamWriter(
            stdin_transport, stdin_protocol, self._stdin_reader, loop
        )

        # Pipe 2: server writes to stdout → production code reads.
        self.stdout = asyncio.StreamReader(loop=loop)
        stdout_protocol = asyncio.StreamReaderProtocol(self.stdout, loop=loop)
        stdout_transport = _LoopbackTransport(self.stdout)
        self._stdout_writer = asyncio.StreamWriter(
            stdout_transport, stdout_protocol, self.stdout, loop
        )

        self.stderr = asyncio.StreamReader(loop=loop)
        stderr_protocol = asyncio.StreamReaderProtocol(self.stderr, loop=loop)
        stderr_transport = _LoopbackTransport(self.stderr)
        self._stderr_writer = asyncio.StreamWriter(
            stderr_transport, stderr_protocol, self.stderr, loop
        )

        self._server_task = loop.create_task(self._run_server())

    async def _run_server(self) -> None:
        try:
            while True:
                raw = await self._stdin_reader.readline()
                if not raw:
                    return
                try:
                    msg = json.loads(raw.decode("utf-8").strip())
                except Exception:
                    continue

                method = msg.get("method")
                msg_id = msg.get("id")

                if method == "initialize":
                    await self._send({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake-android-skills", "version": "0"},
                        },
                    })
                elif method == "notifications/initialized":
                    # No response per MCP spec.
                    continue
                elif method == "tools/call":
                    params = msg.get("params") or {}
                    tool_name = params.get("name")
                    arguments = params.get("arguments") or {}
                    self._captured_calls.append({
                        "name": tool_name,
                        "arguments": arguments,
                    })
                    response = self._tool_responses.get(tool_name)
                    if response is None:
                        await self._send({
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "error": {"code": -32601, "message": f"unknown tool {tool_name}"},
                        })
                    else:
                        await self._send({
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": response,
                        })
        except asyncio.CancelledError:
            return

    async def _send(self, payload: dict) -> None:
        line = (json.dumps(payload) + "\n").encode("utf-8")
        self._stdout_writer.write(line)
        await self._stdout_writer.drain()

    def kill(self) -> None:
        self.returncode = -9
        self._server_task.cancel()

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._returncode_after_close
        try:
            await self._server_task
        except (asyncio.CancelledError, Exception):
            pass
        return self.returncode


class _LoopbackTransport(asyncio.Transport):
    """Asyncio transport that feeds writes straight into a StreamReader.

    Used so a single ``StreamWriter`` test fake can pipe bytes into the
    same-process ``StreamReader`` the code-under-test reads from. We only
    need ``write``/``close`` semantics — flow control is uninteresting
    because the buffers are in-process.
    """

    def __init__(self, reader: asyncio.StreamReader) -> None:
        super().__init__()
        self._reader = reader
        self._closed = False

    def write(self, data: bytes) -> None:
        if not self._closed:
            self._reader.feed_data(data)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._reader.feed_eof()

    def is_closing(self) -> bool:
        return self._closed

    def get_write_buffer_size(self) -> int:
        return 0


def _mcp_text_result(text: str) -> dict:
    """Build a successful MCP ``tools/call`` result envelope."""
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _install_fake_subprocess(
    monkeypatch,
    *,
    tool_responses: dict[str, dict],
    captured_calls: list[dict],
    expected_command: str = "npx",
    expected_args_contain: Iterable[str] = ("android-skills-mcp",),
) -> list[tuple[str, tuple[str, ...]]]:
    """Patch ``asyncio.create_subprocess_exec`` with a JSON-RPC fake server.

    Records every spawn in the returned list so a test can assert that
    the registry-derived ``command`` / ``args`` actually fired.
    """
    spawned: list[tuple[str, tuple[str, ...]]] = []

    async def fake_exec(*args, **kwargs):  # noqa: ARG001
        cmd = args[0] if args else ""
        rest = tuple(str(a) for a in args[1:])
        spawned.append((cmd, rest))
        assert cmd == expected_command, f"unexpected MCP launcher: {cmd!r}"
        for needle in expected_args_contain:
            assert any(needle in piece for piece in rest), (
                f"expected MCP args to contain {needle!r}, got {rest!r}"
            )
        return _FakeMCPProcess(
            tool_responses=tool_responses,
            captured_calls=captured_calls,
        )

    monkeypatch.setattr(agent_tools.asyncio, "create_subprocess_exec", fake_exec)
    return spawned


# ─── Tests ────────────────────────────────────────────────────────────


class TestRegistryWiring:
    """``android_skill_search`` is reachable via the agent tool registry."""

    def test_tool_in_global_map(self):
        assert "android_skill_search" in TOOL_MAP, (
            "android_skill_search must be in TOOL_MAP so the executor can dispatch it"
        )

    def test_tool_in_mcp_tools_list(self):
        assert android_skill_search in MCP_TOOLS

    @pytest.mark.parametrize("role", ["software", "general", "custom"])
    def test_tool_attached_to_android_writing_roles(self, role):
        """Roles that write Android code must have access to the skill search."""
        assert android_skill_search in AGENT_TOOLS[role], (
            f"role {role!r} writes Android code and must carry android_skill_search"
        )

    @pytest.mark.parametrize(
        "role", ["firmware", "validator", "reviewer", "reporter", "devops"]
    )
    def test_tool_not_attached_to_non_android_roles(self, role):
        """Non-Android roles should not carry the tool."""
        assert android_skill_search not in AGENT_TOOLS[role]


class TestNavigation3SearchEndToEnd:
    """The headline acceptance: search ``navigation3`` → usable code context."""

    @pytest.mark.asyncio
    async def test_search_navigation3_returns_code_generation_context(
        self, monkeypatch
    ):
        """Agent searches ``navigation3`` → MCP returns Navigation 3 content
        whose snippet is shaped for code generation (Compose + NavHost +
        @Serializable destinations)."""
        captured_calls: list[dict] = []
        spawned = _install_fake_subprocess(
            monkeypatch,
            tool_responses={"search_skills": _mcp_text_result(_NAV3_SEARCH_PAYLOAD)},
            captured_calls=captured_calls,
        )

        result = await android_skill_search.ainvoke(
            {"action": "search", "query": "navigation3", "limit": 5}
        )

        # 1) The MCP server was actually spawned via the registry-declared command.
        assert len(spawned) == 1
        cmd, args = spawned[0]
        assert cmd == "npx"
        assert "android-skills-mcp" in " ".join(args)

        # 2) The right MCP tool was called with the right arguments.
        assert captured_calls == [
            {"name": "search_skills", "arguments": {"query": "navigation3", "limit": 5}}
        ]

        # 3) The result is a successful payload (no [ERROR] prefix).
        assert result.startswith("[OK] android-skills/search_skills"), result

        # 4) The payload carries Navigation 3 identity markers.
        for marker in ("navigation3", "Navigation 3", "NavHost"):
            assert marker in result, f"missing marker {marker!r} in result"

        # 5) The payload contains code-generation-shaped guidance: a
        # Compose snippet that an android-kotlin agent could paste into
        # a generated file (NavHost + composable<T> + @Serializable
        # destinations + toRoute<>).
        for codegen_token in (
            "@Serializable",
            "rememberNavController",
            "composable<",
            "toRoute<",
        ):
            assert codegen_token in result, (
                f"navigation3 payload missing codegen token {codegen_token!r} — "
                "agent could not turn this into compilable Kotlin"
            )

    @pytest.mark.asyncio
    async def test_get_full_navigation3_skill_body(self, monkeypatch):
        """Follow-up: after search, agent calls ``action=get`` to pull the
        full skill body. Verifies the ``get_skill`` MCP wiring."""
        captured_calls: list[dict] = []
        _install_fake_subprocess(
            monkeypatch,
            tool_responses={"get_skill": _mcp_text_result(_NAV3_GET_PAYLOAD)},
            captured_calls=captured_calls,
        )

        result = await android_skill_search.ainvoke(
            {"action": "get", "skill_id": "navigation3"}
        )

        assert captured_calls == [
            {"name": "get_skill", "arguments": {"skill_id": "navigation3"}}
        ]
        assert result.startswith("[OK] android-skills/get_skill")
        # Migration guidance an agent would lean on while rewriting Navigation 2 code.
        assert "Migration from Navigation 2" in result
        assert "navigate(Detail(" in result

    @pytest.mark.asyncio
    async def test_action_list_enumerates_skills(self, monkeypatch):
        """``action=list`` calls ``list_skills`` with no arguments."""
        list_payload = _mcp_text_result(
            "navigation3\nagp9-migration\nr8-shrinking\nbaseline-profile\n"
        )
        captured_calls: list[dict] = []
        _install_fake_subprocess(
            monkeypatch,
            tool_responses={"list_skills": list_payload},
            captured_calls=captured_calls,
        )

        result = await android_skill_search.ainvoke({"action": "list"})

        assert captured_calls == [{"name": "list_skills", "arguments": {}}]
        assert "navigation3" in result
        assert result.startswith("[OK] android-skills/list_skills")


class TestErrorHandling:
    """Failure modes return ``[ERROR]`` strings (agent-visible) — not raises."""

    @pytest.mark.asyncio
    async def test_search_without_query_errors(self):
        result = await android_skill_search.ainvoke(
            {"action": "search", "query": ""}
        )
        assert result.startswith("[ERROR]")
        assert "query" in result.lower()

    @pytest.mark.asyncio
    async def test_get_without_skill_id_errors(self):
        result = await android_skill_search.ainvoke(
            {"action": "get", "skill_id": ""}
        )
        assert result.startswith("[ERROR]")
        assert "skill_id" in result.lower()

    @pytest.mark.asyncio
    async def test_unknown_action_errors(self):
        result = await android_skill_search.ainvoke(
            {"action": "delete", "query": "x"}
        )
        assert result.startswith("[ERROR]")
        assert "action" in result.lower()

    @pytest.mark.asyncio
    async def test_npx_missing_returns_graceful_error(self, monkeypatch):
        """If ``npx`` is not on PATH, the tool should return an ``[ERROR]``
        string the agent can read and fall back from — not raise."""

        async def fake_exec(*args, **kwargs):  # noqa: ARG001
            raise FileNotFoundError(2, "No such file or directory", args[0])

        monkeypatch.setattr(
            agent_tools.asyncio, "create_subprocess_exec", fake_exec
        )

        result = await android_skill_search.ainvoke(
            {"action": "search", "query": "navigation3"}
        )
        assert result.startswith("[ERROR] android-skills MCP:"), result
        assert "PATH" in result or "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_mcp_tool_isError_propagates(self, monkeypatch):
        """If the MCP server reports ``isError: true``, surface as ``[ERROR]``."""
        captured_calls: list[dict] = []
        _install_fake_subprocess(
            monkeypatch,
            tool_responses={
                "search_skills": {
                    "content": [{"type": "text", "text": "skill index unavailable"}],
                    "isError": True,
                }
            },
            captured_calls=captured_calls,
        )

        result = await android_skill_search.ainvoke(
            {"action": "search", "query": "navigation3"}
        )
        assert result.startswith("[ERROR] android-skills MCP:"), result
        assert "skill index unavailable" in result

    @pytest.mark.asyncio
    async def test_registry_missing_returns_error(self, monkeypatch, tmp_path):
        """If ``configs/mcp_servers.json`` does not exist, return ``[ERROR]``."""
        missing = tmp_path / "no_such_registry.json"
        monkeypatch.setattr(agent_tools, "_MCP_REGISTRY_PATH", missing)

        result = await android_skill_search.ainvoke(
            {"action": "search", "query": "navigation3"}
        )
        assert result.startswith("[ERROR] android-skills MCP:"), result
        assert "android-skills" in result


class TestRegistryConfig:
    """The ``android-skills`` entry in ``configs/mcp_servers.json`` is wired
    correctly so the loader can find it without the test having to fake
    the registry."""

    def test_registry_has_android_skills_entry(self):
        spec = agent_tools._load_mcp_server_spec("android-skills")
        assert spec is not None, (
            "configs/mcp_servers.json must contain mcpServers.android-skills"
        )
        assert spec.get("command") == "npx"
        # `-y` keeps npx non-interactive; without it the runtime would block on
        # the "Ok to proceed?" prompt.
        assert "-y" in (spec.get("args") or [])
        assert "android-skills-mcp" in (spec.get("args") or [])
