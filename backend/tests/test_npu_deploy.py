"""Tests for Edge AI NPU Deploy (Phase 36).

Covers:
- Platform config NPU field extraction
- SimulationTrack npu enum
- NPU simulation DB columns
- NPU skill kit loading and matching
- simulate.sh npu track execution
"""

from __future__ import annotations

import json
import subprocess

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Platform Config NPU Fields
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPlatformConfigNPU:

    @pytest.mark.asyncio
    async def test_vendor_example_no_npu(self):
        """vendor-example has npu_enabled=false, should not emit NPU fields."""
        from backend.agents.tools import get_platform_config
        result = await get_platform_config.ainvoke({"platform": "vendor-example"})
        assert "NPU_ENABLED" not in result
        assert "NPU_TYPE" not in result

    @pytest.mark.asyncio
    async def test_npu_fields_conditional(self):
        """NPU fields only emitted when npu_enabled is true."""
        from backend.agents.tools import get_platform_config
        # aarch64 has no NPU config
        result = await get_platform_config.ainvoke({"platform": "aarch64"})
        assert "NPU_ENABLED" not in result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Simulation Track Enum
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSimulationTrackEnum:

    def test_npu_track_exists(self):
        from backend.models import SimulationTrack
        assert SimulationTrack.npu == "npu"

    def test_all_tracks(self):
        from backend.models import SimulationTrack
        tracks = {t.value for t in SimulationTrack}
        assert tracks == {"algo", "hw", "npu"}

    def test_simulation_request_accepts_npu(self):
        from backend.models import SimulationRequest
        req = SimulationRequest(
            track="npu", module="detect",
            model_path="model.rknn", framework="rknn",
        )
        assert req.track == "npu"
        assert req.model_path == "model.rknn"
        assert req.framework == "rknn"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NPU Simulation DB Columns
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNPUSimulationDB:

    @pytest.mark.asyncio
    async def test_npu_columns_exist(self, pg_test_pool):
        """NPU columns should be added by migration.

        Phase-3 Step C.1 (2026-04-21): swapped ``PRAGMA
        table_info(simulations)`` (SQLite-only) for an
        ``information_schema.columns`` query against the pool —
        identical semantics, portable across dialects.
        """
        async with pg_test_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'simulations'"
            )
        columns = {row["column_name"] for row in rows}
        assert "npu_latency_ms" in columns
        assert "npu_throughput_fps" in columns
        assert "accuracy_delta" in columns
        assert "model_size_kb" in columns
        assert "npu_framework" in columns

    @pytest.mark.asyncio
    async def test_npu_simulation_insert(self, pg_test_conn):
        # SP-3.8 (2026-04-20): migrated from client fixture to
        # pg_test_conn savepoint fixture — the same conn covers the
        # insert / update / get sequence, and the TRUNCATE on teardown
        # keeps the table clean for sibling tests.
        from backend import db
        await db.insert_simulation(pg_test_conn, {
            "id": "sim-npu-test",
            "task_id": "",
            "agent_id": "",
            "track": "npu",
            "module": "detect",
            "status": "pass",
            "tests_total": 3,
            "tests_passed": 3,
            "tests_failed": 0,
            "coverage_pct": 100.0,
            "valgrind_errors": 0,
            "duration_ms": 50,
            "report_json": "{}",
            "artifact_id": None,
            "created_at": "2026-04-13T00:00:00",
        })
        await db.update_simulation(pg_test_conn, "sim-npu-test", {
            "npu_latency_ms": 12.3,
            "npu_throughput_fps": 81.3,
            "accuracy_delta": 0.015,
            "model_size_kb": 4096,
            "npu_framework": "rknn",
        })
        sim = await db.get_simulation(pg_test_conn, "sim-npu-test")
        assert sim is not None
        assert sim["track"] == "npu"
        assert sim["npu_framework"] == "rknn"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NPU Skill Kits
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNPUSkillKits:

    def test_all_npu_skills_loaded(self):
        from backend.prompt_loader import list_available_task_skills
        skills = list_available_task_skills()
        npu_names = {s["name"] for s in skills if "npu" in s["name"]}
        assert npu_names == {"npu-detection", "npu-recognition", "npu-pose", "npu-barcode"}

    def test_keyword_match_detection(self):
        from backend.prompt_loader import match_task_skill
        assert match_task_skill("Deploy YOLO detection model to NPU") == "npu-detection"

    def test_keyword_match_recognition(self):
        from backend.prompt_loader import match_task_skill
        assert match_task_skill("Implement face recognition on NPU") == "npu-recognition"

    def test_keyword_match_pose(self):
        from backend.prompt_loader import match_task_skill
        assert match_task_skill("Deploy pose estimation keypoint model") == "npu-pose"

    def test_keyword_match_barcode(self):
        from backend.prompt_loader import match_task_skill
        assert match_task_skill("Optimize barcode QR scanning") == "npu-barcode"

    def test_skill_content_loaded(self):
        from backend.prompt_loader import load_task_skill
        content = load_task_skill("npu-detection")
        assert "YOLO" in content or "detection" in content.lower()
        assert "mAP" in content


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  simulate.sh NPU Track
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSimulateShNPU:

    def test_npu_track_produces_valid_json(self):
        result = subprocess.run(
            ["bash", "scripts/simulate.sh",
             "--type=npu", "--module=detect",
             "--npu-model=test.rknn", "--framework=rknn"],
            capture_output=True, text=True, timeout=30,
        )
        # Parse stdout as JSON
        report = json.loads(result.stdout.strip())
        assert report["track"] == "npu"
        assert report["status"] in ("pass", "fail", "error")
        assert "npu" in report
        assert "latency_ms" in report["npu"]
        assert "throughput_fps" in report["npu"]
        assert "accuracy_delta" in report["npu"]

    def test_npu_track_pass_metrics(self):
        result = subprocess.run(
            ["bash", "scripts/simulate.sh",
             "--type=npu", "--module=face",
             "--npu-model=face.rknn", "--framework=rknn"],
            capture_output=True, text=True, timeout=30,
        )
        report = json.loads(result.stdout.strip())
        assert report["status"] == "pass"
        assert report["npu"]["latency_ms"] > 0
        assert report["npu"]["throughput_fps"] > 0
        assert report["npu"]["accuracy_delta"] <= 0.02  # Within threshold

    def test_npu_missing_model_error(self):
        """NPU track without --npu-model should produce error."""
        result = subprocess.run(
            ["bash", "scripts/simulate.sh",
             "--type=npu", "--module=detect"],
            capture_output=True, text=True, timeout=30,
        )
        report = json.loads(result.stdout.strip())
        # Should have error about missing model
        assert report["tests"]["failed"] > 0 or len(report.get("errors", [])) > 0

    def test_npu_framework_in_output(self):
        result = subprocess.run(
            ["bash", "scripts/simulate.sh",
             "--type=npu", "--module=detect",
             "--npu-model=m.tflite", "--framework=tflite"],
            capture_output=True, text=True, timeout=30,
        )
        report = json.loads(result.stdout.strip())
        assert report["npu"]["framework"] == "tflite"
