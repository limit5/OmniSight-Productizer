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
