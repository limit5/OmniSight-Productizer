"""BP.W3.12 Phase T -- Tier 3 Hardware Bridge Daemon.

The daemon runs on the EVK-connected host and exposes only high-level
JSON RPC endpoints. Agents never receive SSH or shell access to this
host; each action maps to one configured argv command.

SOP module-global audit: module constants are immutable defaults derived
from source/env per worker; no mutable cache or singleton is shared
across uvicorn workers. Hardware operations are intentionally per-worker
subprocess calls against the same host devices.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator


MAX_OUTPUT_CHARS = 12_000
DEFAULT_TIMEOUT_S = 120
DEFAULT_ARTIFACT_ROOT = "/var/lib/omnisight/hardware-bridge/artifacts"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_SAFE_SERIAL_RE = re.compile(r"^/dev/(tty[A-Za-z0-9_.-]+|serial/by-id/[A-Za-z0-9_.:-]+)$")


class CommandResult(BaseModel):
    """JSON-only command result returned by every action endpoint."""

    action: str
    ok: bool
    returncode: int | None
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_ms: int
    started_at: str
    finished_at: str


class FlashBoardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board_id: str = Field(min_length=1, max_length=80)
    firmware_url: str | None = Field(default=None, max_length=2048)
    artifact_path: str | None = Field(default=None, max_length=512)
    reset_after_flash: bool = True
    timeout_s: int = Field(default=DEFAULT_TIMEOUT_S, ge=1, le=900)

    @field_validator("board_id")
    @classmethod
    def _safe_board_id(cls, value: str) -> str:
        if not _SAFE_ID_RE.fullmatch(value):
            raise ValueError("board_id contains unsupported characters")
        return value

    @field_validator("firmware_url")
    @classmethod
    def _safe_firmware_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.startswith(("https://", "http://")):
            raise ValueError("firmware_url must be http(s)")
        return value

    @field_validator("artifact_path")
    @classmethod
    def _safe_artifact_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value.startswith("/") or ".." in Path(value).parts:
            raise ValueError("artifact_path must stay under artifact root")
        return value


class ReadUartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: str = Field(min_length=1, max_length=160)
    baud_rate: int = Field(default=115200, ge=1200, le=4_000_000)
    duration_ms: int = Field(default=1000, ge=100, le=60_000)
    max_bytes: int = Field(default=4096, ge=1, le=131_072)
    timeout_s: int = Field(default=30, ge=1, le=120)

    @field_validator("port")
    @classmethod
    def _safe_port(cls, value: str) -> str:
        if not _SAFE_SERIAL_RE.fullmatch(value):
            raise ValueError("port must be /dev/tty* or /dev/serial/by-id/*")
        return value


class CaptureSignalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bus: Literal["i2c", "spi", "gpio"]
    channel: str = Field(min_length=1, max_length=80)
    duration_ms: int = Field(default=1000, ge=100, le=60_000)
    sample_rate_hz: int = Field(default=100_000, ge=1, le=50_000_000)
    output_format: Literal["json", "text"] = "json"
    timeout_s: int = Field(default=30, ge=1, le=120)

    @field_validator("channel")
    @classmethod
    def _safe_channel(cls, value: str) -> str:
        if not _SAFE_ID_RE.fullmatch(value):
            raise ValueError("channel contains unsupported characters")
        return value


def _env_argv(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default).strip()
    return [part for part in raw.split() if part]


def _artifact_root() -> Path:
    return Path(os.environ.get("OMNISIGHT_HW_BRIDGE_ARTIFACT_ROOT", DEFAULT_ARTIFACT_ROOT))


def _resolve_artifact_path(rel_path: str) -> Path:
    root = _artifact_root().resolve()
    target = (root / rel_path).resolve()
    if root != target and root not in target.parents:
        raise HTTPException(status_code=400, detail="artifact_path escapes artifact root")
    return target


def _require_json(request: Request) -> None:
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("application/json"):
        raise HTTPException(status_code=415, detail="hardware bridge accepts JSON only")


def _require_token(x_omnisight_bridge_token: str | None) -> None:
    expected = os.environ.get("OMNISIGHT_HW_BRIDGE_TOKEN", "").strip()
    if expected and x_omnisight_bridge_token != expected:
        raise HTTPException(status_code=401, detail="invalid hardware bridge token")


def _tail(value: bytes) -> str:
    return value.decode(errors="replace")[-MAX_OUTPUT_CHARS:]


async def _run_action(action: str, argv: list[str], timeout_s: int) -> CommandResult:
    started = datetime.now(UTC)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        finished = datetime.now(UTC)
        return CommandResult(
            action=action,
            ok=False,
            returncode=-9,
            stderr_tail=f"timed out after {timeout_s}s",
            duration_ms=int((finished - started).total_seconds() * 1000),
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
        )
    finished = datetime.now(UTC)
    return CommandResult(
        action=action,
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
        duration_ms=int((finished - started).total_seconds() * 1000),
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
    )


app = FastAPI(
    title="OmniSight Hardware Bridge Daemon",
    version="0.1.0",
)


@app.middleware("http")
async def reject_non_json_posts(request: Request, call_next):
    """Reject non-JSON action calls before FastAPI body parsing."""
    if request.method == "POST":
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("application/json"):
            return JSONResponse(
                {"detail": "hardware bridge accepts JSON only"},
                status_code=415,
            )
    return await call_next(request)


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe; no hardware I/O."""
    return {"status": "ok", "tier": "t3", "service": "hardware-bridge"}


@app.post("/flash_board", response_model=CommandResult)
async def flash_board(
    body: FlashBoardRequest,
    request: Request,
    x_omnisight_bridge_token: str | None = Header(default=None),
) -> CommandResult:
    """Flash firmware to an EVK through the configured board flasher."""
    _require_json(request)
    _require_token(x_omnisight_bridge_token)
    if bool(body.firmware_url) == bool(body.artifact_path):
        raise HTTPException(
            status_code=400,
            detail="provide exactly one of firmware_url or artifact_path",
        )
    firmware = body.firmware_url or str(_resolve_artifact_path(body.artifact_path or ""))
    argv = [
        *_env_argv("OMNISIGHT_HW_BRIDGE_FLASH_CMD", "/usr/local/bin/omni-flash-board"),
        "--board-id",
        body.board_id,
        "--firmware",
        firmware,
    ]
    if body.reset_after_flash:
        argv.append("--reset")
    return await _run_action("flash_board", argv, body.timeout_s)


@app.post("/read_uart", response_model=CommandResult)
async def read_uart(
    body: ReadUartRequest,
    request: Request,
    x_omnisight_bridge_token: str | None = Header(default=None),
) -> CommandResult:
    """Read bounded UART output from an attached serial device."""
    _require_json(request)
    _require_token(x_omnisight_bridge_token)
    argv = [
        *_env_argv("OMNISIGHT_HW_BRIDGE_UART_CMD", "/usr/local/bin/omni-read-uart"),
        "--port",
        body.port,
        "--baud-rate",
        str(body.baud_rate),
        "--duration-ms",
        str(body.duration_ms),
        "--max-bytes",
        str(body.max_bytes),
    ]
    return await _run_action("read_uart", argv, body.timeout_s)


@app.post("/capture_signal", response_model=CommandResult)
async def capture_signal(
    body: CaptureSignalRequest,
    request: Request,
    x_omnisight_bridge_token: str | None = Header(default=None),
) -> CommandResult:
    """Capture a bounded I2C/SPI/GPIO signal sample from the EVK host."""
    _require_json(request)
    _require_token(x_omnisight_bridge_token)
    argv = [
        *_env_argv("OMNISIGHT_HW_BRIDGE_SIGNAL_CMD", "/usr/local/bin/omni-capture-signal"),
        "--bus",
        body.bus,
        "--channel",
        body.channel,
        "--duration-ms",
        str(body.duration_ms),
        "--sample-rate-hz",
        str(body.sample_rate_hz),
        "--output-format",
        body.output_format,
    ]
    return await _run_action("capture_signal", argv, body.timeout_s)
