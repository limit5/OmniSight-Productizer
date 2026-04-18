"""L5 Step 4 — ``GET /api/v1/bootstrap/service-tick`` SSE stream tests.

Covers the wizard's live-log SSE stream that accompanies the
``start-services`` dispatch:

  * ``dev`` mode emits a single info tick + ``done`` event with reason
    ``dev_noop`` and never exec's the tailer.
  * ``systemd`` mode builds ``journalctl --follow -u …`` for both unit
    files and streams every stdout/stderr line as a
    ``bootstrap.service.tick`` event with monotonically increasing
    ``seq`` + stream source.
  * ``docker-compose`` mode builds ``docker compose -f <file> logs
    --follow`` and honours the ``compose_file`` override.
  * Invalid ``mode`` query param is rejected with HTTP 422 before any
    exec.
  * Missing launcher binary surfaces an ``error`` event and closes the
    stream with ``reason=launcher_missing`` instead of raising 500.
  * ``max_seconds`` caps the stream even when the tailer keeps
    producing output (heartbeat-free safety net).
"""

from __future__ import annotations

import asyncio
import json
from typing import Iterable

import pytest


@pytest.fixture(autouse=True)
def _reset_sse_app_status():
    """Clear ``sse_starlette``'s module-level exit event between tests.

    ``sse_starlette.sse.AppStatus.should_exit_event`` is an ``anyio.Event``
    that binds to the event loop that first touches it. Each test gets a
    fresh loop, so without this reset the second streaming test in a run
    blows up with ``bound to a different event loop``.
    """
    try:
        from sse_starlette.sse import AppStatus

        AppStatus.should_exit_event = None
        AppStatus.should_exit = False
    except Exception:  # pragma: no cover — sse_starlette always present
        pass
    yield


# ── tiny fake Process + stream helpers ────────────────────────────────


class _FakeStream:
    """Minimal async byte stream backed by an in-memory deque of lines.

    Yields each queued line from ``readline`` then returns ``b""`` on
    exhaustion so the production drain task cleanly hits EOF.
    """

    def __init__(self, lines: Iterable[bytes], *, hold_open: bool = False):
        self._lines: list[bytes] = list(lines)
        self._hold_open = hold_open
        self._event = asyncio.Event()

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        if self._hold_open:
            # Simulate a long-running tailer that keeps the pipe open.
            await self._event.wait()
            return b""
        return b""

    def release(self) -> None:
        self._event.set()


class _FakeTickProc:
    """Stand-in for ``asyncio.subprocess.Process`` returned by the fake exec."""

    def __init__(self, stdout_lines, stderr_lines, *, hold_open=False, pid=4321):
        self.stdout = _FakeStream(stdout_lines, hold_open=hold_open)
        self.stderr = _FakeStream(stderr_lines, hold_open=hold_open)
        self.pid = pid
        self.returncode: int | None = None
        self._terminated = False

    def terminate(self) -> None:
        self._terminated = True
        self.stdout.release()
        self.stderr.release()
        self.returncode = 0

    def kill(self) -> None:  # pragma: no cover — reached only on stuck kill paths
        self.terminate()
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _patch_tick_exec(monkeypatch, *, proc=None, raise_fnf=False, captured=None):
    """Swap ``asyncio.create_subprocess_exec`` inside the bootstrap router."""
    from backend.routers import bootstrap as _br

    cap = captured if captured is not None else []

    async def fake_exec(*args, **_kwargs):
        cap.append(list(args))
        if raise_fnf:
            raise FileNotFoundError(
                2, "No such file or directory", args[0] if args else "<?>",
            )
        return proc

    monkeypatch.setattr(_br.asyncio, "create_subprocess_exec", fake_exec)
    return cap


# ── SSE parsing helper ────────────────────────────────────────────────


def _parse_sse(body: str) -> list[dict]:
    """Parse the wire-format SSE body into a list of ``{event, data}`` dicts.

    The server uses ``sse-starlette`` which emits blank-line-separated
    events; each event block has ``event: <name>`` and ``data: <json>``
    lines. We only care about those two fields for the assertions here.
    """
    events: list[dict] = []
    for block in body.split("\r\n\r\n"):
        block = block.strip()
        if not block:
            continue
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if not name:
            continue
        parsed: dict = {"event": name}
        try:
            parsed["data"] = json.loads(data) if data else {}
        except (json.JSONDecodeError, TypeError):
            parsed["data"] = data
        events.append(parsed)
    return events


async def _collect_stream(client, url: str, *, timeout: float = 3.0) -> list[dict]:
    """Open the SSE endpoint and parse every event until the server closes."""
    async with client.stream("GET", url, timeout=timeout) as resp:
        assert resp.status_code == 200, await resp.aread()
        assert resp.headers["content-type"].startswith("text/event-stream")
        chunks: list[str] = []
        async for chunk in resp.aiter_text():
            chunks.append(chunk)
        return _parse_sse("".join(chunks))


# ── dev mode: no-op ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_tick_dev_mode_is_noop(client, monkeypatch):
    """``dev`` mode emits one info tick + done, never exec's a tailer."""
    captured = _patch_tick_exec(monkeypatch, proc=None)

    events = await _collect_stream(
        client, "/api/v1/bootstrap/service-tick?mode=dev",
    )

    kinds = [e["event"] for e in events]
    assert kinds[0] == "start"
    assert "bootstrap.service.tick" in kinds
    assert kinds[-1] == "done"
    assert captured == []  # no exec in dev mode

    start = next(e for e in events if e["event"] == "start")
    assert start["data"]["mode"] == "dev"
    assert start["data"]["command"] == []

    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["reason"] == "dev_noop"
    assert done["data"]["returncode"] == 0

    tick = next(e for e in events if e["event"] == "bootstrap.service.tick")
    assert tick["data"]["stream"] == "info"
    assert "dev mode" in tick["data"]["line"]


# ── systemd mode ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_tick_systemd_streams_stdout_lines(client, monkeypatch):
    """Each stdout line surfaces as a ``bootstrap.service.tick`` event."""
    proc = _FakeTickProc(
        stdout_lines=[
            b"2026-04-17T10:00:00 omnisight-backend: started\n",
            b"2026-04-17T10:00:01 omnisight-backend: listening on :8000\n",
        ],
        stderr_lines=[],
    )
    captured = _patch_tick_exec(monkeypatch, proc=proc)

    events = await _collect_stream(
        client, "/api/v1/bootstrap/service-tick?mode=systemd&tail=20",
    )

    # argv shape: journalctl -u <unit1> -u <unit2> --follow --lines 20 …
    assert captured == [[
        "journalctl",
        "-u", "omnisight-backend.service",
        "-u", "omnisight-frontend.service",
        "--follow",
        "--lines", "20",
        "--output", "short-iso",
        "--no-pager",
    ]]

    ticks = [e for e in events if e["event"] == "bootstrap.service.tick"]
    assert len(ticks) == 2
    assert ticks[0]["data"]["line"].endswith("started")
    assert ticks[0]["data"]["stream"] == "stdout"
    assert ticks[0]["data"]["seq"] == 1
    assert ticks[1]["data"]["seq"] == 2

    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["reason"] == "eof"
    assert done["data"]["lines"] == 2


@pytest.mark.asyncio
async def test_service_tick_systemd_interleaves_stderr(client, monkeypatch):
    """stderr lines are tagged ``stream=stderr`` with their own seq space."""
    proc = _FakeTickProc(
        stdout_lines=[b"ok\n"],
        stderr_lines=[b"WARN: unit restart\n"],
    )
    _patch_tick_exec(monkeypatch, proc=proc)

    events = await _collect_stream(
        client, "/api/v1/bootstrap/service-tick?mode=systemd",
    )
    ticks = [e for e in events if e["event"] == "bootstrap.service.tick"]
    streams = {t["data"]["stream"] for t in ticks}
    assert streams == {"stdout", "stderr"}
    # Seq is monotonically increasing across both sources.
    seqs = [t["data"]["seq"] for t in ticks]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


# ── docker-compose mode ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_tick_docker_compose_default_file(client, monkeypatch):
    """``docker-compose`` mode defaults to the prod compose file."""
    proc = _FakeTickProc(
        stdout_lines=[b"backend_1 | boot\n"],
        stderr_lines=[],
    )
    captured = _patch_tick_exec(monkeypatch, proc=proc)

    events = await _collect_stream(
        client, "/api/v1/bootstrap/service-tick?mode=docker-compose",
    )
    assert captured[0] == [
        "docker", "compose", "-f", "docker-compose.prod.yml", "logs",
        "--follow", "--tail", "50", "--no-color",
    ]
    tick = next(e for e in events if e["event"] == "bootstrap.service.tick")
    assert tick["data"]["line"] == "backend_1 | boot"


@pytest.mark.asyncio
async def test_service_tick_docker_compose_custom_file(client, monkeypatch):
    """``compose_file`` query param overrides the default prod compose."""
    proc = _FakeTickProc(stdout_lines=[b"x\n"], stderr_lines=[])
    captured = _patch_tick_exec(monkeypatch, proc=proc)

    await _collect_stream(
        client,
        "/api/v1/bootstrap/service-tick"
        "?mode=docker-compose&compose_file=docker-compose.edge.yml&tail=5",
    )
    assert captured[0] == [
        "docker", "compose", "-f", "docker-compose.edge.yml", "logs",
        "--follow", "--tail", "5", "--no-color",
    ]


# ── error paths ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_tick_invalid_mode_returns_422(client):
    """Unknown mode rejected before any exec, no SSE payload opened."""
    r = await client.get(
        "/api/v1/bootstrap/service-tick?mode=kubernetes",
        follow_redirects=False,
    )
    assert r.status_code == 422, r.text
    assert "mode must be one of" in r.json()["detail"]


@pytest.mark.asyncio
async def test_service_tick_binary_missing_emits_error_event(client, monkeypatch):
    """Missing tailer binary → single ``error`` event + ``done`` close."""
    _patch_tick_exec(monkeypatch, raise_fnf=True)

    events = await _collect_stream(
        client, "/api/v1/bootstrap/service-tick?mode=systemd",
    )
    kinds = [e["event"] for e in events]
    assert "error" in kinds
    assert kinds[-1] == "done"
    err = next(e for e in events if e["event"] == "error")
    assert "not found" in err["data"]["detail"]
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["reason"] == "launcher_missing"


@pytest.mark.asyncio
async def test_service_tick_respects_max_seconds(client, monkeypatch):
    """``max_seconds=0`` (clamped to 1s) closes the stream on time."""
    from backend.routers import bootstrap as _br

    # Shrink the heartbeat so the cap path is reachable in <2s.
    monkeypatch.setattr(_br, "_TICK_HEARTBEAT_SECS", 0.2)

    # Hold the tailer pipe open forever — only ``max_seconds`` can close.
    proc = _FakeTickProc(stdout_lines=[], stderr_lines=[], hold_open=True)
    _patch_tick_exec(monkeypatch, proc=proc)

    events = await _collect_stream(
        client,
        "/api/v1/bootstrap/service-tick?mode=systemd&max_seconds=1",
        timeout=5.0,
    )
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["reason"] == "max_seconds"


# ── argv builder (unit-level, no subprocess) ──────────────────────────


def test_tick_command_dev_is_empty():
    from backend.routers import bootstrap as _br

    assert _br._tick_command("dev", "", 0) == []


def test_tick_command_clamps_negative_tail():
    from backend.routers import bootstrap as _br

    cmd = _br._tick_command("systemd", "", -10)
    assert "--lines" in cmd
    assert cmd[cmd.index("--lines") + 1] == "0"
