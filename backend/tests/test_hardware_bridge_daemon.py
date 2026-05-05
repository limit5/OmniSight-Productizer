"""BP.W3.12 Phase T -- Hardware Bridge Daemon contract tests.

Locks the Tier 3 JSON-only RPC boundary for ``flash_board``,
``read_uart`` and ``capture_signal``. The tests fake subprocesses and do
not touch real EVK hardware.

SOP module-global audit: tests mutate process env per test via
monkeypatch; daemon module state is immutable source/env-derived config.
Read-after-write timing audit: no DB, Redis, cache or parallel writer is
introduced.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tools.hardware_daemon import app as hbd


class _FakeProc:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"ok\n",
        stderr: bytes = b"",
        hang: bool = False,
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(60)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return self.returncode


@pytest.fixture()
async def client():
    transport = httpx.ASGITransport(app=hbd.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://hardware-bridge.test",
    ) as ac:
        yield ac


def _patch_exec(monkeypatch, captured: list[list[str]], proc: _FakeProc | None = None):
    proc = proc or _FakeProc()

    async def _fake_exec(*args, **kwargs):
        captured.append(list(args))
        assert kwargs["stdout"] is asyncio.subprocess.PIPE
        assert kwargs["stderr"] is asyncio.subprocess.PIPE
        return proc

    monkeypatch.setattr(hbd.asyncio, "create_subprocess_exec", _fake_exec)
    return proc


@pytest.mark.asyncio
async def test_healthz_is_json_liveness(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "status": "ok",
        "tier": "t3",
        "service": "hardware-bridge",
    }


@pytest.mark.asyncio
async def test_flash_board_runs_configured_argv_without_shell(client, monkeypatch):
    captured: list[list[str]] = []
    _patch_exec(monkeypatch, captured)
    monkeypatch.setenv("OMNISIGHT_HW_BRIDGE_FLASH_CMD", "/bin/flash-safe --mode evk")

    response = await client.post(
        "/flash_board",
        json={
            "board_id": "rk3588-evk:1",
            "firmware_url": "https://artifacts.example/fw.bin",
            "reset_after_flash": True,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert captured == [[
        "/bin/flash-safe",
        "--mode",
        "evk",
        "--board-id",
        "rk3588-evk:1",
        "--firmware",
        "https://artifacts.example/fw.bin",
        "--reset",
    ]]


@pytest.mark.asyncio
async def test_flash_board_rejects_ambiguous_firmware_source(client, monkeypatch):
    _patch_exec(monkeypatch, [])

    response = await client.post(
        "/flash_board",
        json={
            "board_id": "rk3588-evk",
            "firmware_url": "https://artifacts.example/fw.bin",
            "artifact_path": "fw.bin",
        },
    )

    assert response.status_code == 400
    assert "exactly one" in response.json()["detail"]


@pytest.mark.asyncio
async def test_flash_board_resolves_artifact_under_root(client, monkeypatch, tmp_path):
    captured: list[list[str]] = []
    _patch_exec(monkeypatch, captured)
    monkeypatch.setenv("OMNISIGHT_HW_BRIDGE_ARTIFACT_ROOT", str(tmp_path))

    response = await client.post(
        "/flash_board",
        json={"board_id": "board-a", "artifact_path": "firmware/fw.bin"},
    )

    assert response.status_code == 200, response.text
    assert captured[0][-3:] == [
        "--firmware",
        str(tmp_path / "firmware" / "fw.bin"),
        "--reset",
    ]


@pytest.mark.asyncio
async def test_read_uart_validates_device_path(client, monkeypatch):
    _patch_exec(monkeypatch, [])

    response = await client.post(
        "/read_uart",
        json={"port": "/tmp/not-serial", "baud_rate": 115200},
    )

    assert response.status_code == 422
    assert "port must be" in response.text


@pytest.mark.asyncio
async def test_read_uart_runs_bounded_command(client, monkeypatch):
    captured: list[list[str]] = []
    _patch_exec(monkeypatch, captured, _FakeProc(stdout=b"boot ok\n"))

    response = await client.post(
        "/read_uart",
        json={
            "port": "/dev/ttyUSB0",
            "baud_rate": 921600,
            "duration_ms": 500,
            "max_bytes": 2048,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["stdout_tail"] == "boot ok\n"
    assert captured == [[
        "/usr/local/bin/omni-read-uart",
        "--port",
        "/dev/ttyUSB0",
        "--baud-rate",
        "921600",
        "--duration-ms",
        "500",
        "--max-bytes",
        "2048",
    ]]


@pytest.mark.asyncio
async def test_capture_signal_rejects_unknown_bus(client, monkeypatch):
    _patch_exec(monkeypatch, [])

    response = await client.post(
        "/capture_signal",
        json={"bus": "can", "channel": "i2c-1"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_capture_signal_runs_whitelisted_bus(client, monkeypatch):
    captured: list[list[str]] = []
    _patch_exec(monkeypatch, captured)

    response = await client.post(
        "/capture_signal",
        json={
            "bus": "i2c",
            "channel": "i2c-1",
            "duration_ms": 250,
            "sample_rate_hz": 400000,
            "output_format": "json",
        },
    )

    assert response.status_code == 200, response.text
    assert captured == [[
        "/usr/local/bin/omni-capture-signal",
        "--bus",
        "i2c",
        "--channel",
        "i2c-1",
        "--duration-ms",
        "250",
        "--sample-rate-hz",
        "400000",
        "--output-format",
        "json",
    ]]


@pytest.mark.asyncio
async def test_post_endpoints_reject_non_json(client, monkeypatch):
    _patch_exec(monkeypatch, [])

    response = await client.post(
        "/read_uart",
        content="port=/dev/ttyUSB0",
        headers={"content-type": "text/plain"},
    )

    assert response.status_code == 415
    assert response.json()["detail"] == "hardware bridge accepts JSON only"


@pytest.mark.asyncio
async def test_optional_token_gate(client, monkeypatch):
    _patch_exec(monkeypatch, [])
    monkeypatch.setenv("OMNISIGHT_HW_BRIDGE_TOKEN", "secret")

    missing = await client.post(
        "/read_uart",
        json={"port": "/dev/ttyUSB0"},
    )
    assert missing.status_code == 401

    allowed = await client.post(
        "/read_uart",
        headers={"X-Omnisight-Bridge-Token": "secret"},
        json={"port": "/dev/ttyUSB0"},
    )
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_action_timeout_kills_process(client, monkeypatch):
    captured: list[list[str]] = []
    proc = _patch_exec(monkeypatch, captured, _FakeProc(hang=True))

    response = await client.post(
        "/read_uart",
        json={"port": "/dev/ttyUSB0", "timeout_s": 1},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["returncode"] == -9
    assert "timed out after 1s" in body["stderr_tail"]
    assert proc.killed is True
    assert proc.waited is True

