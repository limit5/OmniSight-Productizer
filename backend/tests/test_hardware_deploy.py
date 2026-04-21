"""Tests for Hardware Integration (Phase 30).

Covers:
- Deploy models
- Platform config deploy fields
- Deploy tools (mock mode)
- simulate.sh deploy track
- EVK API endpoint
- Slash commands /deploy /evk /stream
"""

from __future__ import annotations

import json
import subprocess

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Deploy Models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDeployModels:

    def test_deploy_method_enum(self):
        from backend.models import DeployMethod
        assert DeployMethod.ssh == "ssh"
        assert DeployMethod.adb == "adb"
        assert DeployMethod.fastboot == "fastboot"

    def test_evk_device_model(self):
        from backend.models import EVKDevice
        evk = EVKDevice(platform="vendor-example", deploy_target_ip="192.168.1.100")
        assert evk.deploy_method == "ssh"
        assert evk.deploy_user == "root"
        assert evk.reachable is False

    def test_deploy_request_model(self):
        from backend.models import DeployRequest
        req = DeployRequest(platform="vendor-example", module="sensor")
        assert req.run_after_deploy is True
        assert req.binary_path == ""

    def test_deploy_result_model(self):
        from backend.models import DeployResult
        res = DeployResult(status="success", platform="vendor-example", target_ip="10.0.0.1")
        assert res.artifacts_copied == []
        assert res.duration_ms == 0

    def test_uvc_device_model(self):
        from backend.models import UVCDevice
        uvc = UVCDevice(device_path="/dev/video0", name="USB Camera")
        assert uvc.formats == []
        assert uvc.resolutions == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Platform Config Deploy Fields
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPlatformConfigDeploy:

    @pytest.mark.asyncio
    async def test_vendor_example_has_deploy_fields(self):
        from backend.agents.tools import get_platform_config
        result = await get_platform_config.ainvoke({"platform": "vendor-example"})
        assert "DEPLOY_METHOD=ssh" in result
        assert "DEPLOY_USER=root" in result
        assert "DEPLOY_PATH=/opt/app" in result

    @pytest.mark.asyncio
    async def test_generic_platform_no_deploy(self):
        from backend.agents.tools import get_platform_config
        result = await get_platform_config.ainvoke({"platform": "aarch64"})
        assert "DEPLOY_METHOD" not in result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Deploy Tools (mock mode)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDeployTools:

    @pytest.mark.asyncio
    async def test_check_evk_no_config(self):
        from backend.agents.tools import check_evk_connection
        result = await check_evk_connection.ainvoke({"platform": "aarch64"})
        # aarch64 has no deploy config
        assert "ERROR" in result or "NOT_CONFIGURED" in result

    @pytest.mark.asyncio
    async def test_check_evk_empty_ip(self):
        from backend.agents.tools import check_evk_connection
        result = await check_evk_connection.ainvoke({"platform": "vendor-example"})
        # In test env, platform YAML may not be reachable from test workspace
        assert "NOT_CONFIGURED" in result or "ERROR" in result or "empty" in result.lower()

    @pytest.mark.asyncio
    async def test_list_uvc_no_devices(self):
        from backend.agents.tools import list_uvc_devices
        result = await list_uvc_devices.ainvoke({})
        # In CI/test env, likely no /dev/video* and no v4l2-ctl
        assert "NOT_FOUND" in result or "OK" in result

    def test_deploy_tools_in_registry(self):
        from backend.agents.tools import TOOL_MAP
        assert "check_evk_connection" in TOOL_MAP
        assert "deploy_to_evk" in TOOL_MAP
        assert "list_uvc_devices" in TOOL_MAP

    def test_firmware_has_deploy_tools(self):
        from backend.agents.tools import AGENT_TOOLS
        fw_tools = {t.name for t in AGENT_TOOLS["firmware"]}
        assert "deploy_to_evk" in fw_tools
        assert "check_evk_connection" in fw_tools
        assert "list_uvc_devices" in fw_tools


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  simulate.sh deploy track
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSimulateShDeploy:

    def test_deploy_mock_mode(self):
        """Deploy track without IP should run in mock mode."""
        result = subprocess.run(
            ["bash", "scripts/simulate.sh",
             "--type=deploy", "--module=sensor", "--platform=aarch64"],
            capture_output=True, text=True, timeout=30,
        )
        report = json.loads(result.stdout.strip())
        assert report["track"] == "deploy"
        assert report["status"] == "pass"
        assert report["deploy"]["status"] == "mock"
        assert "MOCK" in report["deploy"]["remote_output"]

    def test_deploy_json_has_all_fields(self):
        result = subprocess.run(
            ["bash", "scripts/simulate.sh",
             "--type=deploy", "--module=driver", "--platform=aarch64"],
            capture_output=True, text=True, timeout=30,
        )
        report = json.loads(result.stdout.strip())
        deploy = report["deploy"]
        assert "status" in deploy
        assert "target_ip" in deploy
        assert "deploy_user" in deploy
        assert "deploy_path" in deploy
        assert "remote_output" in deploy


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EVK API Endpoint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEVKEndpoint:

    @pytest.mark.asyncio
    async def test_evk_endpoint_returns_list(self, client):
        resp = await client.get("/api/v1/runtime/evk")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # vendor-example has deploy_method so should appear
        platforms = [e["platform"] for e in data]
        assert "vendor-example" in platforms

    @pytest.mark.asyncio
    async def test_evk_entry_has_fields(self, client):
        resp = await client.get("/api/v1/runtime/evk")
        data = resp.json()
        vex = next((e for e in data if e["platform"] == "vendor-example"), None)
        assert vex is not None
        assert vex["deploy_method"] == "ssh"
        assert "reachable" in vex
        assert "board_name" in vex


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Slash Commands
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestHardwareSlashCommands:
    # 2026-04-22: SP-3.1 (#74) changed ``handle_slash_command`` signature
    # from ``(command, args)`` to ``(conn, command, args)`` so handlers
    # can write to the pool-backed DB without re-signaturing dispatch.
    # These four tests were not migrated at the time — they were raising
    # ``TypeError: missing 1 required positional argument: 'args'`` and
    # silently red in CI. Fixed here while we're in-file fixing the
    # adjacent ``[ROUTE TO LLM]`` misleading-text bug on ``/deploy``.
    #
    # ``/deploy`` / ``/evk`` / ``/stream`` handlers don't touch the
    # ``conn`` parameter (they call agent tools that don't do DB work
    # directly), so these tests pass ``None`` rather than acquiring a
    # pool connection — keeps the tests lightweight + avoids the
    # broader pool-lifespan-not-initialised failure mode that
    # ``test_release.py::TestReleaseSlashCommand`` currently hits.

    @pytest.mark.asyncio
    async def test_deploy_no_args(self):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "deploy", "")
        assert result is not None
        assert "EVK" in result or "ERROR" in result or "NOT_CONFIGURED" in result

    @pytest.mark.asyncio
    async def test_evk_command(self):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "evk", "")
        assert result is not None
        assert "EVK" in result or "ERROR" in result or "NOT_CONFIGURED" in result

    @pytest.mark.asyncio
    async def test_stream_command(self):
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "stream", "")
        assert result is not None
        assert "UVC" in result or "NOT_FOUND" in result

    @pytest.mark.asyncio
    async def test_deploy_with_args(self, monkeypatch):
        # 2026-04-22: ``/deploy`` used to short-circuit with a
        # ``"[ROUTE TO LLM] ..."`` lie — the old assertion literally
        # validated that the handler misled the user. Now the handler
        # actually dispatches to ``_run_pipeline`` (the agent graph),
        # so we monkey-patch the pipeline helper to avoid hitting a
        # live LLM from this unit test and assert the intent string
        # was constructed + handed off correctly. End-to-end pipeline
        # routing is verified separately in
        # ``test_slash_commands.py::TestSlashPipelineDispatch``.
        from backend.slash_commands import handle_slash_command
        from backend.routers import chat as _chat_router
        from backend.models import OrchestratorMessage, MessageRole

        captured: dict[str, str] = {}

        async def _fake_pipeline(msg: str) -> OrchestratorMessage:
            captured["intent"] = msg
            return OrchestratorMessage(
                id="msg-test",
                role=MessageRole.orchestrator,
                content="[FAKE] deploy acknowledged",
                timestamp="2026-04-22T00:00:00",
            )

        monkeypatch.setattr(_chat_router, "_run_pipeline", _fake_pipeline)

        result = await handle_slash_command(None, "deploy", "vendor-example sensor")

        assert result == "[FAKE] deploy acknowledged"
        assert "vendor-example" in captured["intent"]
        assert "sensor" in captured["intent"]
        assert "Deploy" in captured["intent"] or "deploy" in captured["intent"]
