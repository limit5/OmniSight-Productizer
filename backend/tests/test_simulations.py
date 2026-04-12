"""Tests for dual-track simulation system."""

import json
import uuid
from pathlib import Path

import pytest


class TestSimulationDB:

    @pytest.mark.asyncio
    async def test_insert_and_list(self):
        from backend import db
        await db.init()
        try:
            sim_id = f"sim-test-{uuid.uuid4().hex[:6]}"
            await db.insert_simulation({
                "id": sim_id, "task_id": "t-1", "agent_id": "a-1",
                "track": "algo", "module": "core_algorithm", "status": "pass",
                "tests_total": 5, "tests_passed": 5, "tests_failed": 0,
                "coverage_pct": 100.0, "valgrind_errors": 0, "duration_ms": 1234,
                "report_json": json.dumps({"status": "pass"}),
                "artifact_id": None, "created_at": "2026-01-01T00:00:00",
            })
            sims = await db.list_simulations(task_id="t-1")
            assert any(s["id"] == sim_id for s in sims)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_get_simulation(self):
        from backend import db
        await db.init()
        try:
            sim_id = f"sim-get-{uuid.uuid4().hex[:6]}"
            await db.insert_simulation({
                "id": sim_id, "task_id": "", "agent_id": "",
                "track": "hw", "module": "gpio_pwm", "status": "running",
                "tests_total": 0, "tests_passed": 0, "tests_failed": 0,
                "coverage_pct": 0.0, "valgrind_errors": 0, "duration_ms": 0,
                "report_json": "{}", "artifact_id": None,
                "created_at": "2026-01-01T00:00:00",
            })
            sim = await db.get_simulation(sim_id)
            assert sim is not None
            assert sim["track"] == "hw"
            assert sim["module"] == "gpio_pwm"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_update_simulation(self):
        from backend import db
        await db.init()
        try:
            sim_id = f"sim-upd-{uuid.uuid4().hex[:6]}"
            await db.insert_simulation({
                "id": sim_id, "task_id": "", "agent_id": "",
                "track": "algo", "module": "test_mod", "status": "running",
                "tests_total": 0, "tests_passed": 0, "tests_failed": 0,
                "coverage_pct": 0.0, "valgrind_errors": 0, "duration_ms": 0,
                "report_json": "{}", "artifact_id": None,
                "created_at": "2026-01-01T00:00:00",
            })
            await db.update_simulation(sim_id, {
                "status": "pass", "tests_total": 3, "tests_passed": 3,
            })
            sim = await db.get_simulation(sim_id)
            assert sim["status"] == "pass"
            assert sim["tests_total"] == 3
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self):
        from backend import db
        await db.init()
        try:
            for status in ("pass", "fail"):
                await db.insert_simulation({
                    "id": f"sim-filt-{status}-{uuid.uuid4().hex[:4]}",
                    "task_id": "", "agent_id": "",
                    "track": "algo", "module": "m", "status": status,
                    "tests_total": 1, "tests_passed": 1 if status == "pass" else 0,
                    "tests_failed": 0 if status == "pass" else 1,
                    "coverage_pct": 100.0, "valgrind_errors": 0, "duration_ms": 100,
                    "report_json": "{}", "artifact_id": None,
                    "created_at": "2026-01-01T00:00:00",
                })
            passed = await db.list_simulations(status="pass")
            assert all(s["status"] == "pass" for s in passed)
        finally:
            await db.close()


class TestSimulationAPI:

    @pytest.mark.asyncio
    async def test_list_endpoint(self, client):
        resp = await client.get("/api/v1/system/simulations")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_get_not_found(self, client):
        resp = await client.get("/api/v1/system/simulations/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_existing(self, client):
        from backend import db
        sim_id = f"sim-api-{uuid.uuid4().hex[:6]}"
        await db.insert_simulation({
            "id": sim_id, "task_id": "", "agent_id": "",
            "track": "algo", "module": "test", "status": "pass",
            "tests_total": 1, "tests_passed": 1, "tests_failed": 0,
            "coverage_pct": 100.0, "valgrind_errors": 0, "duration_ms": 50,
            "report_json": json.dumps({"status": "pass"}),
            "artifact_id": None, "created_at": "2026-01-01T00:00:00",
        })
        resp = await client.get(f"/api/v1/system/simulations/{sim_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["track"] == "algo"
        assert data["status"] == "pass"


class TestSimulationTool:

    def test_run_simulation_in_tool_map(self):
        from backend.agents.tools import TOOL_MAP
        assert "run_simulation" in TOOL_MAP

    def test_simulation_tools_in_firmware(self):
        from backend.agents.tools import AGENT_TOOLS
        tool_names = [t.name for t in AGENT_TOOLS.get("firmware", [])]
        assert "run_simulation" in tool_names

    def test_simulation_tools_in_validator(self):
        from backend.agents.tools import AGENT_TOOLS
        tool_names = [t.name for t in AGENT_TOOLS.get("validator", [])]
        assert "run_simulation" in tool_names

    @pytest.mark.asyncio
    async def test_run_bash_redirects_simulate_sh(self):
        """run_bash should redirect simulate.sh calls."""
        from backend.agents.tools import run_bash
        result = await run_bash.ainvoke({"command": "./simulate.sh --type=algo --module=test"})
        assert "[REDIRECT]" in result


class TestSimulateScript:

    def test_script_exists_and_executable(self):
        from pathlib import Path
        script = Path(__file__).resolve().parent.parent.parent / "scripts" / "simulate.sh"
        assert script.exists()
        assert script.stat().st_mode & 0o111  # executable

    def test_missing_args_returns_error_json(self):
        import subprocess
        script = str(Path(__file__).resolve().parent.parent.parent / "scripts" / "simulate.sh")
        result = subprocess.run(
            ["bash", script], capture_output=True, text=True, timeout=10
        )
        assert result.returncode != 0
        output = json.loads(result.stdout)
        assert output["status"] == "error"

    def test_invalid_type_returns_error(self):
        import subprocess
        script = str(Path(__file__).resolve().parent.parent.parent / "scripts" / "simulate.sh")
        result = subprocess.run(
            ["bash", script, "--type=invalid", "--module=test"],
            capture_output=True, text=True, timeout=10
        )
        output = json.loads(result.stdout)
        assert output["status"] == "error"
        assert any("must be algo or hw" in e for e in output["errors"])

    def test_module_injection_rejected(self):
        import subprocess
        script = str(Path(__file__).resolve().parent.parent.parent / "scripts" / "simulate.sh")
        result = subprocess.run(
            ["bash", script, "--type=algo", "--module=../etc/passwd"],
            capture_output=True, text=True, timeout=10
        )
        output = json.loads(result.stdout)
        assert output["status"] == "error"
        assert any("Invalid module" in e for e in output["errors"])

    def test_nonexistent_module_returns_valid_json(self):
        import subprocess
        script = str(Path(__file__).resolve().parent.parent.parent / "scripts" / "simulate.sh")
        result = subprocess.run(
            ["bash", script, "--type=algo", "--module=nonexistent"],
            capture_output=True, text=True, timeout=10
        )
        output = json.loads(result.stdout)  # Must be valid JSON
        assert output["status"] == "fail"
        assert output["track"] == "algo"
        assert output["module"] == "nonexistent"
        assert any("not found" in e.lower() for e in output["errors"])


class TestPlatformProfiles:

    def test_aarch64_profile_exists(self):
        profile = Path(__file__).resolve().parent.parent.parent / "configs" / "platforms" / "aarch64.yaml"
        assert profile.exists()
        content = profile.read_text()
        assert "aarch64-linux-gnu-gcc" in content

    def test_armv7_profile_exists(self):
        profile = Path(__file__).resolve().parent.parent.parent / "configs" / "platforms" / "armv7.yaml"
        assert profile.exists()

    def test_riscv64_profile_exists(self):
        profile = Path(__file__).resolve().parent.parent.parent / "configs" / "platforms" / "riscv64.yaml"
        assert profile.exists()
