"""BP.W3.13 Phase U — gVisor production adoption drift guards."""

from __future__ import annotations

import os
import json
import stat
import subprocess
from pathlib import Path

import pytest

from backend import container as ct


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BENCHMARK_SCRIPT = REPO_ROOT / "scripts" / "benchmark_gvisor_runtime.sh"
ARCH_DOC = REPO_ROOT / "docs" / "design" / "tiered-sandbox-architecture.md"
SANDBOX_OPS_DOC = REPO_ROOT / "docs" / "operations" / "sandbox.md"


@pytest.fixture(autouse=True)
def _reset_container_state():
    ct._reset_runtime_cache_for_tests()
    ct._containers.clear()
    yield
    ct._reset_runtime_cache_for_tests()
    ct._containers.clear()


def test_gvisor_benchmark_script_is_executable_and_syntax_clean() -> None:
    mode = os.stat(BENCHMARK_SCRIPT).st_mode
    assert mode & stat.S_IXUSR, "operator benchmark script must be executable"

    result = subprocess.run(
        ["bash", "-n", str(BENCHMARK_SCRIPT)],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_gvisor_benchmark_script_compares_runc_and_runsc() -> None:
    text = BENCHMARK_SCRIPT.read_text(encoding="utf-8")
    assert "for runtime in runc runsc" in text
    assert "--runtime=\"$runtime\"" in text
    assert "--network none" in text
    assert "runtime,iteration,elapsed_ms" in text


def _install_start_container_stubs(monkeypatch, *, inspected_runtime: str = "runsc"):
    async def fake_run(cmd, timeout=60):
        if "docker info" in cmd:
            return (
                0,
                json.dumps({"runc": {"path": "runc"}, "runsc": {"path": "runsc"}}),
                "",
            )
        if "network ls" in cmd:
            return (0, "", "")
        if "image inspect" in cmd:
            return (0, "sha256:" + "a" * 64, "")
        if "docker run" in cmd:
            return (0, "abcdef123456", "")
        if "docker inspect --format" in cmd:
            return (0, inspected_runtime, "")
        if "docker exec" in cmd:
            return (0, "", "")
        if "docker rm -f" in cmd or "docker rm" in cmd:
            return (0, "", "")
        return (0, "", "")

    async def fake_ensure_image():
        return True

    monkeypatch.setattr(ct, "_run", fake_run)
    monkeypatch.setattr(ct, "ensure_image", fake_ensure_image)


@pytest.mark.asyncio
async def test_production_launch_asserts_inspected_runtime(monkeypatch) -> None:
    _install_start_container_stubs(monkeypatch, inspected_runtime="runsc")
    monkeypatch.setattr("backend.config.settings.env", "production", raising=False)
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests", "", raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.sandbox_lifetime_s", 0, raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runsc", raising=False,
    )

    info = await ct.start_container("a-prod-runtime", Path("/tmp"))
    try:
        assert info.container_id == "abcdef123456"
    finally:
        if info.oom_task:
            info.oom_task.cancel()
        ct._containers.pop("a-prod-runtime", None)


@pytest.mark.asyncio
async def test_production_launch_fails_on_runtime_mismatch(monkeypatch) -> None:
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()

    _install_start_container_stubs(monkeypatch, inspected_runtime="runc")
    monkeypatch.setattr("backend.config.settings.env", "production", raising=False)
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests", "", raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runsc", raising=False,
    )

    with pytest.raises(RuntimeError, match="runtime assertion failed"):
        await ct.start_container("a-prod-mismatch", Path("/tmp"))

    samples = list(m.sandbox_launch_total.collect()[0].samples)
    mismatch = [
        s for s in samples
        if s.labels.get("runtime") == "runsc"
        and s.labels.get("result") == "runtime_mismatch"
        and s.name.endswith("_total")
    ]
    assert mismatch and mismatch[0].value >= 1


def test_tier1_architecture_documents_phase_u_runtime_gate() -> None:
    text = ARCH_DOC.read_text(encoding="utf-8")
    assert "Phase U 落地狀態 (BP.W3.13, 2026-05-06)" in text
    assert "ENV=production" in text
    assert "docker inspect --format '{{.HostConfig.Runtime}}'" in text
    assert "scripts/benchmark_gvisor_runtime.sh" in text


def test_operator_runbook_documents_gvisor_benchmark() -> None:
    text = SANDBOX_OPS_DOC.read_text(encoding="utf-8")
    assert "scripts/benchmark_gvisor_runtime.sh" in text
    assert "OMNISIGHT_GVISOR_BENCH_REPEATS" in text
    assert "runtime,iteration,elapsed_ms" in text
