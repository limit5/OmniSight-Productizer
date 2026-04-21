"""Tests for slash command system (Phase 29)."""

import pytest


class TestSlashCommandHandler:

    @pytest.mark.asyncio
    async def test_status_command(self):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "status", "")
        assert result is not None
        assert "System Status" in result

    @pytest.mark.asyncio
    async def test_help_command(self):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "help", "")
        assert result is not None
        assert "/status" in result

    @pytest.mark.asyncio
    async def test_logs_command(self):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "logs", "5")
        assert result is not None

    @pytest.mark.asyncio
    async def test_agents_command(self):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "agents", "")
        assert result is not None
        assert "Agents" in result or "No agents" in result

    @pytest.mark.asyncio
    async def test_tasks_command(self):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "tasks", "")
        assert result is not None

    @pytest.mark.asyncio
    async def test_sdks_command(self):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "sdks", "")
        assert result is not None
        assert "SDK" in result or "platform" in result.lower()

    @pytest.mark.asyncio
    async def test_unknown_command_returns_error(self):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "nonexistent_xyz", "")
        assert result is not None
        assert "Unknown command" in result

    @pytest.mark.asyncio
    async def test_empty_command_returns_help_hint(self):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "", "")
        assert result is not None
        assert "/help" in result


class TestSlashCommandsWithDB:

    @pytest.mark.asyncio
    async def test_debug_command(self, client):
        """Debug needs DB — test via chat endpoint."""
        resp = await client.post("/api/v1/chat", json={"message": "/debug"})
        assert resp.status_code == 200
        assert "Debug State" in resp.json()["message"]["content"]

    @pytest.mark.asyncio
    async def test_npi_command(self, client):
        resp = await client.post("/api/v1/chat", json={"message": "/npi"})
        assert resp.status_code == 200


class TestChatSlashInterception:

    @pytest.mark.asyncio
    async def test_slash_command_intercepted(self, client):
        resp = await client.post("/api/v1/chat", json={"message": "/status"})
        assert resp.status_code == 200
        data = resp.json()
        assert "System Status" in data["message"]["content"]

    @pytest.mark.asyncio
    async def test_non_slash_not_intercepted(self, client):
        # Non-slash messages should go through normally (may fail without LLM but that's OK)
        resp = await client.post("/api/v1/chat", json={"message": "hello"})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_slash_help_in_chat(self, client):
        resp = await client.post("/api/v1/chat", json={"message": "/help"})
        assert resp.status_code == 200
        assert "/status" in resp.json()["message"]["content"]


class TestSlashCommandRegistry:

    def test_frontend_registry_file_exists(self):
        from pathlib import Path
        registry = Path(__file__).resolve().parent.parent.parent / "lib" / "slash-commands.ts"
        assert registry.exists()
        content = registry.read_text()
        assert "SLASH_COMMANDS" in content
        assert "matchCommands" in content
        assert "parseSlashCommand" in content

    def test_backend_handler_count(self):
        from backend.slash_commands import _HANDLERS
        assert len(_HANDLERS) >= 10

    def test_all_handlers_are_async(self):
        import asyncio
        from backend.slash_commands import _HANDLERS
        for name, handler in _HANDLERS.items():
            assert asyncio.iscoroutinefunction(handler), f"/{name} handler is not async"


class TestSlashPipelineDispatch:
    """Regression coverage for the 2026-04-22 fix where seven slash
    commands (``/build``, ``/test``, ``/simulate``, ``/review``,
    ``/assign``, ``/deploy`` with args, ``/release upload``) used to
    return a ``"[ROUTE TO LLM] ..."`` literal string — which text
    CLAIMED the agent pipeline would run but
    ``backend/routers/chat.py::_try_slash_command`` treats any non-None
    handler result as the final reply, so the pipeline was never
    invoked. Each test here monkey-patches ``_run_pipeline`` to a spy
    and verifies the handler:
      (a) hands off to the pipeline (spy called exactly once),
      (b) passes a concrete intent string that contains user args,
      (c) returns the pipeline reply's ``.content`` verbatim (not the
          old ``[ROUTE TO LLM] ...`` literal).

    Without these tests, a future refactor could silently re-introduce
    the short-circuit bug; the earlier sole assertion
    ``assert "ROUTE TO LLM" in result`` (test_hardware_deploy.py:219)
    actually locked the bug in place.
    """

    @pytest.fixture
    def pipeline_spy(self, monkeypatch):
        """Replace ``_run_pipeline`` with a spy returning a sentinel reply."""
        from backend.routers import chat as _chat_router
        from backend.models import OrchestratorMessage, MessageRole

        captured: dict[str, str] = {}

        async def _fake(msg: str) -> OrchestratorMessage:
            captured["intent"] = msg
            captured["call_count"] = captured.get("call_count", 0) + 1
            return OrchestratorMessage(
                id="msg-spy",
                role=MessageRole.orchestrator,
                content=f"[FAKE-PIPELINE] handled: {msg[:40]}",
                timestamp="2026-04-22T00:00:00",
            )

        monkeypatch.setattr(_chat_router, "_run_pipeline", _fake)
        return captured

    @pytest.mark.asyncio
    async def test_build_dispatches_with_module_in_intent(self, pipeline_spy):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "build", "firmware")
        assert pipeline_spy["call_count"] == 1
        assert "firmware" in pipeline_spy["intent"]
        assert "Build" in pipeline_spy["intent"]
        assert result.startswith("[FAKE-PIPELINE]")
        assert "ROUTE TO LLM" not in result

    @pytest.mark.asyncio
    async def test_build_default_module_when_empty(self, pipeline_spy):
        """Empty args should still route — default module 'firmware'."""
        from backend.slash_commands import handle_slash_command
        await handle_slash_command(None, "build", "")
        assert pipeline_spy["call_count"] == 1
        assert "firmware" in pipeline_spy["intent"]

    @pytest.mark.asyncio
    async def test_test_dispatches_with_module_in_intent(self, pipeline_spy):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "test", "auth")
        assert pipeline_spy["call_count"] == 1
        assert "auth" in pipeline_spy["intent"]
        assert "test" in pipeline_spy["intent"].lower()
        assert result.startswith("[FAKE-PIPELINE]")

    @pytest.mark.asyncio
    async def test_test_all_scope_when_empty(self, pipeline_spy):
        """Empty args → 'entire test suite' intent scope."""
        from backend.slash_commands import handle_slash_command
        await handle_slash_command(None, "test", "")
        assert pipeline_spy["call_count"] == 1
        assert "entire" in pipeline_spy["intent"].lower() or "all" in pipeline_spy["intent"].lower()

    @pytest.mark.asyncio
    async def test_simulate_dispatches_with_module(self, pipeline_spy):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "simulate", "camera-isp")
        assert pipeline_spy["call_count"] == 1
        assert "camera-isp" in pipeline_spy["intent"]
        assert "run_simulation" in pipeline_spy["intent"]
        assert result.startswith("[FAKE-PIPELINE]")

    @pytest.mark.asyncio
    async def test_simulate_rejects_empty_args(self, pipeline_spy):
        """Empty args → USAGE error WITHOUT invoking pipeline."""
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "simulate", "")
        assert pipeline_spy.get("call_count", 0) == 0, (
            "pipeline should not be invoked when /simulate has no args"
        )
        assert "[ERROR]" in result
        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_review_dispatches_to_pipeline(self, pipeline_spy):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "review", "")
        assert pipeline_spy["call_count"] == 1
        assert "Gerrit" in pipeline_spy["intent"]
        assert "review" in pipeline_spy["intent"].lower()
        assert result.startswith("[FAKE-PIPELINE]")

    @pytest.mark.asyncio
    async def test_review_with_scope_embeds_target(self, pipeline_spy):
        from backend.slash_commands import handle_slash_command
        await handle_slash_command(None, "review", "change-12345")
        assert pipeline_spy["call_count"] == 1
        assert "change-12345" in pipeline_spy["intent"]

    @pytest.mark.asyncio
    async def test_assign_dispatches_with_raw_args(self, pipeline_spy):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "assign", "task-42 firmware-alpha")
        assert pipeline_spy["call_count"] == 1
        assert "task-42" in pipeline_spy["intent"]
        assert "firmware-alpha" in pipeline_spy["intent"]
        assert result.startswith("[FAKE-PIPELINE]")

    @pytest.mark.asyncio
    async def test_assign_rejects_empty_args(self, pipeline_spy):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "assign", "")
        assert pipeline_spy.get("call_count", 0) == 0
        assert "[ERROR]" in result
        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_deploy_dispatches_with_platform_and_module(self, pipeline_spy):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "deploy", "vendor-example sensor")
        assert pipeline_spy["call_count"] == 1
        assert "vendor-example" in pipeline_spy["intent"]
        assert "sensor" in pipeline_spy["intent"]
        assert result.startswith("[FAKE-PIPELINE]")

    @pytest.mark.asyncio
    async def test_deploy_no_module_returns_usage(self, pipeline_spy):
        """``/deploy vendor-example`` (platform only, no module) → usage error."""
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "deploy", "vendor-example")
        assert pipeline_spy.get("call_count", 0) == 0
        assert "[ERROR]" in result
        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_release_upload_dispatches(self, pipeline_spy):
        """``/release upload [target]`` routes to pipeline now (was ROUTE TO LLM)."""
        # Note: ``/release`` with empty args / 'create' action reads the
        # DB via ``db.list_artifacts`` / ``create_release_bundle`` which
        # needs a real conn — not covered here. Upload action is pure
        # intent-dispatch and works with ``conn=None``.
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "release", "upload github")
        assert pipeline_spy["call_count"] == 1
        assert "github" in pipeline_spy["intent"].lower()
        assert "upload" in pipeline_spy["intent"].lower()
        assert result.startswith("[FAKE-PIPELINE]")

    @pytest.mark.asyncio
    async def test_pipeline_exception_is_caught_by_run_pipeline(
        self, monkeypatch,
    ):
        """``_run_pipeline`` already has its own try/except that converts
        agent pipeline errors into a ``[ORCHESTRATOR] Pipeline error:``
        content string. Handler just unwraps ``.content``. Verify this
        contract by having the spy return an error-style reply and
        confirming the handler propagates the content verbatim.
        """
        from backend.routers import chat as _chat_router
        from backend.models import OrchestratorMessage, MessageRole
        from backend.slash_commands import handle_slash_command

        async def _fake_pipeline_with_error(msg: str) -> OrchestratorMessage:
            return OrchestratorMessage(
                id="msg-err",
                role=MessageRole.orchestrator,
                content="[ORCHESTRATOR] Pipeline error: simulated LLM provider 5xx",
                timestamp="2026-04-22T00:00:00",
            )

        monkeypatch.setattr(
            _chat_router, "_run_pipeline", _fake_pipeline_with_error,
        )
        result = await handle_slash_command(None, "build", "firmware")
        assert "Pipeline error" in result
        assert "ROUTE TO LLM" not in result  # never leak the old misleading text

    def test_no_handler_still_returns_route_to_llm_literal(self):
        """Final guard: verify no production slash handler ever has a
        ``return`` statement with a ``[ROUTE TO LLM]`` literal again.
        Without this assertion a future refactor could accidentally
        reintroduce the misleading short-circuit. Scans with a regex
        tight enough to allow the explanatory docstring / comment that
        describes why the old pattern was buggy, but fail if the text
        appears in a ``return`` statement.
        """
        import inspect
        import re
        from backend import slash_commands as _sc

        source = inspect.getsource(_sc)
        # Match lines where ``return`` (possibly prefixed by whitespace)
        # is followed by an f-string / normal string starting with
        # ``[ROUTE TO LLM]``. Matches both ``return "[ROUTE TO LLM]..."``
        # and ``return f"[ROUTE TO LLM]..."``. Does NOT match comments
        # or docstrings containing the phrase.
        pattern = re.compile(r'^\s*return\s+f?"\[ROUTE TO LLM\]', re.MULTILINE)
        matches = pattern.findall(source)
        assert len(matches) == 0, (
            f"Found {len(matches)} handler(s) with ``return \"[ROUTE TO "
            f"LLM] ...\"`` short-circuit — these claim to route to the "
            f"agent pipeline but chat.py never calls _run_pipeline when "
            f"a handler returns a string. Route through "
            f"``_dispatch_to_pipeline(intent)`` instead."
        )
