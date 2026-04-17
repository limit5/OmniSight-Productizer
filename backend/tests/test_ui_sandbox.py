"""V2 #1 (issue #318) — ui_sandbox contract tests.

Pins ``backend/ui_sandbox.py`` against:

  * structural invariants (``__all__`` membership, schema version,
    default constants, frozen dataclasses, JSON-safe ``to_dict``);
  * :class:`SandboxConfig` validation (session id charset, abs
    workspace path, workdir absolute, env key/value types, positive
    timeouts, tuple-normalised command);
  * :class:`SandboxInstance` state-transition correctness — ``created
    → starting → running → stopping → stopped`` via
    :meth:`SandboxManager.create/start/mark_ready/stop`;
  * graceful docker failure paths (``run_detached`` raises → sandbox
    marked ``failed`` rather than propagating);
  * deterministic :func:`build_docker_run_spec` (byte-identical dicts
    across calls);
  * volume-mount bind (workspace → /app);
  * port allocation deterministic per session id;
  * ready-line detection across Next.js / Vite banners;
  * compile-error parsing with file/line extraction;
  * one-per-session invariant (create raises
    :class:`SandboxAlreadyExists`);
  * event callback emission on every state transition;
  * concurrency: 20 worker threads touching 20 sessions do not
    corrupt manager state.

The ``FakeDockerClient`` fixture records every call and returns
deterministic container ids — no real docker daemon is touched.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from backend import ui_sandbox as us
from backend.ui_sandbox import (
    DEFAULT_CONTAINER_PORT,
    DEFAULT_DEV_COMMAND,
    DEFAULT_HOST_PORT_RANGE,
    DEFAULT_IDLE_LIMIT_S,
    DEFAULT_NODE_ENV,
    DEFAULT_PREVIEW_HOST,
    DEFAULT_SANDBOX_IMAGE,
    DEFAULT_STARTUP_TIMEOUT_S,
    DEFAULT_STOP_TIMEOUT_S,
    DEFAULT_WORKDIR,
    MAX_LOG_CHARS,
    READY_PATTERNS,
    UI_SANDBOX_SCHEMA_VERSION,
    CompileError,
    DockerClient,
    SandboxAlreadyExists,
    SandboxConfig,
    SandboxError,
    SandboxInstance,
    SandboxManager,
    SandboxNotFound,
    SandboxStatus,
    allocate_host_port,
    build_docker_run_spec,
    build_preview_url,
    detect_dev_server_ready,
    format_container_name,
    parse_compile_error,
    render_sandbox_status_markdown,
    validate_workspace,
)


# ── Module invariants ────────────────────────────────────────────────


EXPECTED_ALL = {
    "UI_SANDBOX_SCHEMA_VERSION",
    "DEFAULT_SANDBOX_IMAGE",
    "DEFAULT_DEV_COMMAND",
    "DEFAULT_CONTAINER_PORT",
    "DEFAULT_HOST_PORT_RANGE",
    "DEFAULT_WORKDIR",
    "DEFAULT_STARTUP_TIMEOUT_S",
    "DEFAULT_STOP_TIMEOUT_S",
    "DEFAULT_IDLE_LIMIT_S",
    "DEFAULT_NODE_ENV",
    "DEFAULT_PREVIEW_HOST",
    "MAX_LOG_CHARS",
    "READY_PATTERNS",
    "SandboxStatus",
    "SandboxConfig",
    "SandboxInstance",
    "CompileError",
    "DockerClient",
    "SubprocessDockerClient",
    "SandboxManager",
    "SandboxError",
    "SandboxAlreadyExists",
    "SandboxNotFound",
    "build_docker_run_spec",
    "build_preview_url",
    "detect_dev_server_ready",
    "parse_compile_error",
    "validate_workspace",
    "allocate_host_port",
    "format_container_name",
    "render_sandbox_status_markdown",
}


def test_all_exports_match():
    assert set(us.__all__) == EXPECTED_ALL


@pytest.mark.parametrize("name", sorted(EXPECTED_ALL))
def test_each_export_exists(name: str):
    assert hasattr(us, name)


def test_schema_version_is_semver():
    parts = UI_SANDBOX_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_default_sandbox_image_is_node_alpine():
    # Changing this is a visible ops event — requires schema bump.
    assert DEFAULT_SANDBOX_IMAGE == "node:22-alpine"


def test_default_dev_command_runs_npm_dev_on_0_0_0_0():
    joined = " ".join(DEFAULT_DEV_COMMAND)
    assert "npm run dev" in joined
    assert "0.0.0.0" in joined
    assert str(DEFAULT_CONTAINER_PORT) in joined


def test_default_container_port_is_3000():
    assert DEFAULT_CONTAINER_PORT == 3000


def test_idle_limit_is_15_minutes():
    # V2 row 2 spec: idle 15 min 自動回收
    assert DEFAULT_IDLE_LIMIT_S == 900.0


def test_workdir_is_absolute():
    assert DEFAULT_WORKDIR.startswith("/")


def test_preview_host_is_local_loopback_by_default():
    assert DEFAULT_PREVIEW_HOST == "127.0.0.1"


def test_stop_timeout_positive():
    assert DEFAULT_STOP_TIMEOUT_S > 0


def test_startup_timeout_positive():
    assert DEFAULT_STARTUP_TIMEOUT_S > 0


def test_max_log_chars_reasonable():
    assert MAX_LOG_CHARS >= 10_000


def test_host_port_range_valid():
    lo, hi = DEFAULT_HOST_PORT_RANGE
    assert 1 <= lo <= hi <= 65535
    assert hi - lo + 1 >= 100  # enough slots for multi-session use


def test_ready_patterns_tuple_of_compiled():
    import re as _re
    assert isinstance(READY_PATTERNS, tuple)
    assert all(isinstance(p, _re.Pattern) for p in READY_PATTERNS)
    assert len(READY_PATTERNS) >= 3


def test_node_env_is_development():
    # DEV sandbox must not leak production flag — HMR relies on dev mode.
    assert DEFAULT_NODE_ENV == "development"


def test_sandbox_status_enum_values():
    assert SandboxStatus.pending.value == "pending"
    assert SandboxStatus.starting.value == "starting"
    assert SandboxStatus.running.value == "running"
    assert SandboxStatus.stopping.value == "stopping"
    assert SandboxStatus.stopped.value == "stopped"
    assert SandboxStatus.failed.value == "failed"


def test_sandbox_status_is_str_enum():
    # Allows direct JSON-serialisation via json.dumps.
    assert isinstance(SandboxStatus.running, str)
    assert SandboxStatus.running == "running"


# ── format_container_name ────────────────────────────────────────────


@pytest.mark.parametrize(
    "session_id, expected_suffix",
    [
        ("abc", "abc"),
        ("sess-001", "sess-001"),
        ("Sess.02", "sess.02"),
        ("bad/chars*here", "bad-chars-here"),
    ],
)
def test_format_container_name_lowercased_safe(session_id: str, expected_suffix: str):
    name = format_container_name(session_id)
    assert name.startswith("omnisight-ui-")
    assert name.endswith(expected_suffix)


def test_format_container_name_truncated_to_63():
    name = format_container_name("x" * 200)
    assert len(name) <= 63


def test_format_container_name_rejects_blank():
    with pytest.raises(ValueError):
        format_container_name("")
    with pytest.raises(ValueError):
        format_container_name("   ")


def test_format_container_name_fallback_when_all_stripped():
    # All special chars strip to empty → use fallback "sess".
    name = format_container_name("----")
    assert "sess" in name


# ── build_preview_url ────────────────────────────────────────────────


def test_build_preview_url_default_host():
    assert build_preview_url(3000) == "http://127.0.0.1:3000/"


def test_build_preview_url_custom_host_and_path():
    url = build_preview_url(40123, host="preview.example.com", path="/pricing")
    assert url == "http://preview.example.com:40123/pricing"


def test_build_preview_url_injects_leading_slash():
    url = build_preview_url(40123, path="dashboard")
    assert url.endswith(":40123/dashboard")


@pytest.mark.parametrize("port", [0, -1, 70000, 99999])
def test_build_preview_url_rejects_invalid_port(port: int):
    with pytest.raises(ValueError):
        build_preview_url(port)


def test_build_preview_url_rejects_blank_host():
    with pytest.raises(ValueError):
        build_preview_url(3000, host="")


def test_build_preview_url_rejects_non_string_path():
    with pytest.raises(ValueError):
        build_preview_url(3000, path=None)  # type: ignore[arg-type]


# ── validate_workspace ───────────────────────────────────────────────


def test_validate_workspace_accepts_existing_dir(tmp_path: Path):
    result = validate_workspace(str(tmp_path))
    assert result == tmp_path


def test_validate_workspace_rejects_missing(tmp_path: Path):
    with pytest.raises(ValueError):
        validate_workspace(str(tmp_path / "does-not-exist"))


def test_validate_workspace_rejects_file(tmp_path: Path):
    f = tmp_path / "hello.txt"
    f.write_text("hi")
    with pytest.raises(ValueError):
        validate_workspace(str(f))


def test_validate_workspace_rejects_relative():
    with pytest.raises(ValueError):
        validate_workspace("relative/path")


def test_validate_workspace_rejects_blank():
    with pytest.raises(ValueError):
        validate_workspace("")
    with pytest.raises(ValueError):
        validate_workspace("   ")


# ── allocate_host_port ──────────────────────────────────────────────


def test_allocate_host_port_deterministic_by_session():
    a = allocate_host_port("session-alpha")
    b = allocate_host_port("session-alpha")
    assert a == b


def test_allocate_host_port_different_session_different_port():
    a = allocate_host_port("session-alpha")
    b = allocate_host_port("session-beta")
    assert a != b


def test_allocate_host_port_in_range():
    p = allocate_host_port("session-x")
    lo, hi = DEFAULT_HOST_PORT_RANGE
    assert lo <= p <= hi


def test_allocate_host_port_avoids_in_use():
    # Fill all but one slot in a tiny range.
    port_range = (40100, 40103)
    chosen = allocate_host_port("sid", port_range=port_range)
    assert chosen == allocate_host_port("sid", port_range=port_range)
    # With all slots taken except 40102, must pick 40102.
    in_use = [40100, 40101, 40103]
    assert allocate_host_port("anything", in_use=in_use, port_range=port_range) == 40102


def test_allocate_host_port_raises_when_exhausted():
    with pytest.raises(SandboxError):
        allocate_host_port(
            "sid",
            in_use=[40100, 40101, 40102, 40103],
            port_range=(40100, 40103),
        )


def test_allocate_host_port_rejects_invalid_range():
    with pytest.raises(ValueError):
        allocate_host_port("sid", port_range=(5000, 4000))  # reversed
    with pytest.raises(ValueError):
        allocate_host_port("sid", port_range=(0, 100))  # lo < 1


# ── SandboxConfig validation ────────────────────────────────────────


def _sample_config(tmp_path: Path, **overrides: Any) -> SandboxConfig:
    kwargs: dict[str, Any] = dict(
        session_id="sess-1",
        workspace_path=str(tmp_path),
        host_port=40123,
    )
    kwargs.update(overrides)
    return SandboxConfig(**kwargs)


def test_sandbox_config_defaults(tmp_path: Path):
    c = _sample_config(tmp_path)
    assert c.image == DEFAULT_SANDBOX_IMAGE
    assert c.container_port == DEFAULT_CONTAINER_PORT
    assert c.workdir == DEFAULT_WORKDIR
    assert c.command == DEFAULT_DEV_COMMAND
    assert c.node_env == DEFAULT_NODE_ENV


def test_sandbox_config_is_frozen(tmp_path: Path):
    c = _sample_config(tmp_path)
    with pytest.raises(Exception):
        c.image = "other"  # type: ignore[misc]


def test_sandbox_config_env_is_readonly(tmp_path: Path):
    c = _sample_config(tmp_path, env={"A": "B"})
    with pytest.raises(TypeError):
        c.env["A"] = "C"  # type: ignore[index]


@pytest.mark.parametrize(
    "bad", ["", "   ", "has space", "has/slash", "has*star", "x" * 65]
)
def test_sandbox_config_rejects_bad_session_id(bad: str, tmp_path: Path):
    with pytest.raises(ValueError):
        SandboxConfig(session_id=bad, workspace_path=str(tmp_path))


def test_sandbox_config_rejects_blank_workspace():
    with pytest.raises(ValueError):
        SandboxConfig(session_id="ok", workspace_path="")


def test_sandbox_config_rejects_blank_image(tmp_path: Path):
    with pytest.raises(ValueError):
        _sample_config(tmp_path, image="")


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_sandbox_config_rejects_bad_container_port(port: int, tmp_path: Path):
    with pytest.raises(ValueError):
        _sample_config(tmp_path, container_port=port)


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_sandbox_config_rejects_bad_host_port(port: int, tmp_path: Path):
    with pytest.raises(ValueError):
        _sample_config(tmp_path, host_port=port)


def test_sandbox_config_rejects_relative_workdir(tmp_path: Path):
    with pytest.raises(ValueError):
        _sample_config(tmp_path, workdir="relative/path")


def test_sandbox_config_rejects_non_positive_timeouts(tmp_path: Path):
    with pytest.raises(ValueError):
        _sample_config(tmp_path, startup_timeout_s=0)
    with pytest.raises(ValueError):
        _sample_config(tmp_path, stop_timeout_s=-1)


def test_sandbox_config_rejects_non_string_env(tmp_path: Path):
    with pytest.raises(ValueError):
        _sample_config(tmp_path, env={"K": 123})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        _sample_config(tmp_path, env={123: "v"})  # type: ignore[dict-item]


def test_sandbox_config_command_tuple_normalises(tmp_path: Path):
    c = _sample_config(tmp_path, command=["echo", "ok"])
    assert c.command == ("echo", "ok")
    assert isinstance(c.command, tuple)


def test_sandbox_config_rejects_empty_command(tmp_path: Path):
    with pytest.raises(ValueError):
        _sample_config(tmp_path, command=())


def test_sandbox_config_rejects_non_sequence_command(tmp_path: Path):
    with pytest.raises(ValueError):
        _sample_config(tmp_path, command="npm run dev")  # type: ignore[arg-type]


def test_sandbox_config_to_dict_roundtrips_json(tmp_path: Path):
    c = _sample_config(tmp_path, env={"X": "1"})
    d = c.to_dict()
    assert d["schema_version"] == UI_SANDBOX_SCHEMA_VERSION
    assert d["session_id"] == c.session_id
    dumped = json.dumps(d)
    assert "sess-1" in dumped


# ── build_docker_run_spec ───────────────────────────────────────────


def test_build_docker_run_spec_deterministic(tmp_path: Path):
    c = _sample_config(tmp_path)
    a = build_docker_run_spec(c)
    b = build_docker_run_spec(c)
    assert a == b
    # byte-identical when serialised (exercises key-order determinism).
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_build_docker_run_spec_mounts_workspace_to_app(tmp_path: Path):
    c = _sample_config(tmp_path)
    spec = build_docker_run_spec(c)
    assert len(spec["mounts"]) == 1
    m = spec["mounts"][0]
    assert m["source"] == str(tmp_path)
    assert m["target"] == DEFAULT_WORKDIR
    assert m["read_only"] is False


def test_build_docker_run_spec_maps_host_port(tmp_path: Path):
    c = _sample_config(tmp_path, host_port=40555)
    spec = build_docker_run_spec(c)
    assert spec["ports"] == {40555: DEFAULT_CONTAINER_PORT}


def test_build_docker_run_spec_omits_ports_when_unassigned(tmp_path: Path):
    c = _sample_config(tmp_path, host_port=None)
    spec = build_docker_run_spec(c)
    assert spec["ports"] == {}


def test_build_docker_run_spec_env_has_defaults(tmp_path: Path):
    c = _sample_config(tmp_path)
    spec = build_docker_run_spec(c)
    assert spec["env"]["NODE_ENV"] == DEFAULT_NODE_ENV
    assert spec["env"]["HOST"] == "0.0.0.0"
    assert spec["env"]["PORT"] == str(DEFAULT_CONTAINER_PORT)


def test_build_docker_run_spec_user_env_wins(tmp_path: Path):
    c = _sample_config(tmp_path, env={"NODE_ENV": "test", "EXTRA": "1"})
    spec = build_docker_run_spec(c)
    assert spec["env"]["NODE_ENV"] == "test"
    assert spec["env"]["EXTRA"] == "1"


def test_build_docker_run_spec_env_is_sorted(tmp_path: Path):
    c = _sample_config(tmp_path, env={"Z": "1", "A": "2"})
    spec = build_docker_run_spec(c)
    keys = list(spec["env"].keys())
    assert keys == sorted(keys)


def test_build_docker_run_spec_container_name_matches_helper(tmp_path: Path):
    c = _sample_config(tmp_path)
    spec = build_docker_run_spec(c)
    assert spec["container_name"] == format_container_name(c.session_id)


def test_build_docker_run_spec_rejects_non_config():
    with pytest.raises(TypeError):
        build_docker_run_spec({"session_id": "x"})  # type: ignore[arg-type]


def test_build_docker_run_spec_schema_version_embedded(tmp_path: Path):
    c = _sample_config(tmp_path)
    assert build_docker_run_spec(c)["schema_version"] == UI_SANDBOX_SCHEMA_VERSION


# ── detect_dev_server_ready ─────────────────────────────────────────


@pytest.mark.parametrize(
    "banner",
    [
        "  ▲ Next.js 14.2.0\n  - Local:        http://localhost:3000\n  ✓ Ready in 2.3s",
        "VITE v5.0.0  ready in 432 ms",
        "  Local:   http://localhost:3000/",
        "compiled successfully",
        "Started server on 0.0.0.0:3000",
        "listening on port 3000",
    ],
)
def test_detect_dev_server_ready_hits_known_banners(banner: str):
    assert detect_dev_server_ready(banner)


@pytest.mark.parametrize(
    "not_ready",
    [
        "",
        "starting dev server...",
        "installing dependencies",
        "webpack compiling",
    ],
)
def test_detect_dev_server_ready_rejects_startup_noise(not_ready: str):
    assert not detect_dev_server_ready(not_ready)


def test_detect_dev_server_ready_case_insensitive():
    assert detect_dev_server_ready("READY IN 2S")
    assert detect_dev_server_ready("LOCAL: http://localhost:3000")


# ── parse_compile_error ─────────────────────────────────────────────


def test_parse_compile_error_empty_returns_empty():
    assert parse_compile_error("") == ()
    assert parse_compile_error("no errors here") == ()


def test_parse_compile_error_extracts_file_line_col():
    stderr = (
        "Failed to compile.\n"
        "./pages/index.tsx:12:5\n"
        "SyntaxError: Unexpected token\n"
    )
    errors = parse_compile_error(stderr)
    assert len(errors) >= 1
    # At least one error carries the file/line/col hint.
    hits = [e for e in errors if e.file and e.line == 12 and e.column == 5]
    assert hits, f"no structured error: {[e.to_dict() for e in errors]}"


def test_parse_compile_error_handles_module_not_found():
    stderr = "Module not found: Error: Can't resolve 'foo' in '/app'"
    errors = parse_compile_error(stderr)
    assert errors
    assert errors[0].error_type == "module_not_found"


def test_parse_compile_error_dedups_duplicate_messages():
    stderr = (
        "SyntaxError at ./src/app.tsx:3:1\n"
        "SyntaxError at ./src/app.tsx:3:1\n"
    )
    errors = parse_compile_error(stderr)
    assert len(errors) == 1


def test_parse_compile_error_never_raises_on_garbage():
    parse_compile_error("🔥" * 200)  # should not raise
    parse_compile_error(None)  # type: ignore[arg-type]


def test_compile_error_to_dict_json_safe():
    err = CompileError(
        message="boom", file="x.tsx", line=1, column=2, error_type="syntaxerror"
    )
    dumped = json.dumps(err.to_dict())
    assert "boom" in dumped


# ── SandboxInstance basic invariants ────────────────────────────────


def _instance_for(tmp_path: Path, **overrides: Any) -> SandboxInstance:
    c = _sample_config(tmp_path, **overrides.pop("config_overrides", {}))
    return SandboxInstance(
        session_id=c.session_id,
        container_name=format_container_name(c.session_id),
        config=c,
        **overrides,
    )


def test_sandbox_instance_is_frozen(tmp_path: Path):
    inst = _instance_for(tmp_path)
    with pytest.raises(Exception):
        inst.status = SandboxStatus.running  # type: ignore[misc]


def test_sandbox_instance_is_running_property(tmp_path: Path):
    inst = _instance_for(tmp_path, status=SandboxStatus.running)
    assert inst.is_running
    assert not inst.is_terminal


def test_sandbox_instance_is_terminal_property(tmp_path: Path):
    assert _instance_for(tmp_path, status=SandboxStatus.stopped).is_terminal
    assert _instance_for(tmp_path, status=SandboxStatus.failed).is_terminal
    assert not _instance_for(tmp_path, status=SandboxStatus.running).is_terminal


def test_sandbox_instance_idle_seconds(tmp_path: Path):
    inst = _instance_for(tmp_path, last_active_at=1000.0)
    assert inst.idle_seconds(now=1010.0) == 10.0
    assert inst.idle_seconds(now=900.0) == 0.0  # clamped to >= 0


def test_sandbox_instance_fresh_has_zero_idle(tmp_path: Path):
    inst = _instance_for(tmp_path, last_active_at=0.0)
    assert inst.idle_seconds(now=time.time()) == 0.0


def test_sandbox_instance_to_dict_json_safe(tmp_path: Path):
    inst = _instance_for(tmp_path, status=SandboxStatus.running, host_port=40123,
                         preview_url="http://127.0.0.1:40123/")
    d = inst.to_dict()
    dumped = json.dumps(d)
    assert inst.session_id in dumped
    assert d["status"] == "running"
    assert d["config"]["schema_version"] == UI_SANDBOX_SCHEMA_VERSION


def test_sandbox_instance_rejects_bad_timestamps(tmp_path: Path):
    with pytest.raises(ValueError):
        _instance_for(tmp_path, last_active_at=-1)


def test_sandbox_instance_rejects_non_enum_status(tmp_path: Path):
    c = _sample_config(tmp_path)
    with pytest.raises(ValueError):
        SandboxInstance(
            session_id=c.session_id,
            container_name="x",
            config=c,
            status="running",  # type: ignore[arg-type]
        )


# ── FakeDockerClient fixture ────────────────────────────────────────


class FakeDockerClient:
    """Deterministic, in-memory DockerClient for tests."""

    def __init__(
        self,
        *,
        run_error: Exception | None = None,
        stop_error: Exception | None = None,
        remove_error: Exception | None = None,
        canned_logs: str = "",
    ) -> None:
        self.run_error = run_error
        self.stop_error = stop_error
        self.remove_error = remove_error
        self.canned_logs = canned_logs
        self.run_calls: list[dict[str, Any]] = []
        self.stop_calls: list[dict[str, Any]] = []
        self.remove_calls: list[dict[str, Any]] = []
        self._next_id = 0
        self._lock = threading.Lock()

    def run_detached(
        self,
        *,
        image: str,
        name: str,
        command: Sequence[str],
        mounts: Sequence[Mapping[str, str]],
        ports: Mapping[int, int],
        env: Mapping[str, str],
        workdir: str,
    ) -> str:
        if self.run_error is not None:
            raise self.run_error
        with self._lock:
            self._next_id += 1
            cid = f"fake-cid-{self._next_id:04d}"
        self.run_calls.append(
            {
                "image": image,
                "name": name,
                "command": list(command),
                "mounts": [dict(m) for m in mounts],
                "ports": dict(ports),
                "env": dict(env),
                "workdir": workdir,
                "container_id": cid,
            }
        )
        return cid

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        self.stop_calls.append({"container_id": container_id, "timeout_s": timeout_s})
        if self.stop_error is not None:
            raise self.stop_error

    def remove(self, container_id: str, *, force: bool = False) -> None:
        self.remove_calls.append({"container_id": container_id, "force": force})
        if self.remove_error is not None:
            raise self.remove_error

    def logs(self, container_id: str, *, tail: int | None = None) -> str:
        return self.canned_logs

    def inspect(self, container_id: str) -> Mapping[str, Any]:
        return {"State": {"Running": True}, "Id": container_id}


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        v = self._t
        self._t += 1.0
        return v


class RecordingEventCallback:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.events.append((event_type, dict(payload)))


def _manager_for(
    tmp_path: Path,
    *,
    docker: FakeDockerClient | None = None,
    clock: FakeClock | None = None,
    event_cb: RecordingEventCallback | None = None,
    port_range: tuple[int, int] = DEFAULT_HOST_PORT_RANGE,
) -> tuple[SandboxManager, FakeDockerClient, FakeClock, RecordingEventCallback]:
    docker = docker or FakeDockerClient()
    clock = clock or FakeClock()
    event_cb = event_cb or RecordingEventCallback()
    mgr = SandboxManager(
        docker_client=docker,
        clock=clock,
        event_cb=event_cb,
        port_range=port_range,
    )
    return mgr, docker, clock, event_cb


# ── SandboxManager lifecycle ────────────────────────────────────────


def test_manager_create_then_start_transitions_states(tmp_path: Path):
    mgr, docker, _, events = _manager_for(tmp_path)
    config = _sample_config(tmp_path, session_id="sess-a", host_port=40111)
    inst = mgr.create(config)
    assert inst.status is SandboxStatus.pending
    assert inst.container_id is None
    assert events.events[0][0] == "ui_sandbox.created"

    started = mgr.start("sess-a")
    assert started.status is SandboxStatus.starting
    assert started.container_id == "fake-cid-0001"
    assert started.host_port == 40111
    assert started.preview_url == "http://127.0.0.1:40111/"
    assert events.events[-1][0] == "ui_sandbox.starting"

    ready = mgr.mark_ready("sess-a")
    assert ready.status is SandboxStatus.running
    assert ready.ready_at is not None
    assert events.events[-1][0] == "ui_sandbox.ready"


def test_manager_create_rejects_non_existent_workspace(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    config = SandboxConfig(
        session_id="miss", workspace_path=str(tmp_path / "nope"), host_port=40111
    )
    with pytest.raises(ValueError):
        mgr.create(config)


def test_manager_one_sandbox_per_session(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-x", host_port=40120))
    with pytest.raises(SandboxAlreadyExists):
        mgr.create(_sample_config(tmp_path, session_id="sess-x", host_port=40121))


def test_manager_start_is_idempotent_for_running(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-y", host_port=40130))
    mgr.start("sess-y")
    mgr.mark_ready("sess-y")
    # calling start again on running returns the same instance.
    same = mgr.start("sess-y")
    assert same.status is SandboxStatus.running


def test_manager_start_rejects_unknown(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    with pytest.raises(SandboxNotFound):
        mgr.start("ghost")


def test_manager_start_docker_error_marks_failed(tmp_path: Path):
    docker = FakeDockerClient(run_error=RuntimeError("boom"))
    mgr, _, _, events = _manager_for(tmp_path, docker=docker)
    mgr.create(_sample_config(tmp_path, session_id="sess-f", host_port=40140))
    failed = mgr.start("sess-f")
    assert failed.status is SandboxStatus.failed
    assert "boom" in (failed.error or "")
    assert events.events[-1][0] == "ui_sandbox.failed"


def test_manager_allocates_host_port_when_unassigned(tmp_path: Path):
    mgr, docker, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-p", host_port=None))
    started = mgr.start("sess-p")
    assert started.host_port is not None
    lo, hi = DEFAULT_HOST_PORT_RANGE
    assert lo <= started.host_port <= hi
    # Allocator must have reached docker with the mapped port.
    assert docker.run_calls[0]["ports"] == {started.host_port: DEFAULT_CONTAINER_PORT}


def test_manager_allocation_avoids_inflight_sessions(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-1", host_port=None))
    mgr.start("sess-1")
    mgr.create(_sample_config(tmp_path, session_id="sess-2", host_port=None))
    mgr.start("sess-2")
    ports = {inst.host_port for inst in mgr.list()}
    assert len(ports) == 2 and None not in ports


def test_manager_stop_transitions_and_calls_docker(tmp_path: Path):
    mgr, docker, _, events = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-s", host_port=40150))
    mgr.start("sess-s")
    stopped = mgr.stop("sess-s")
    assert stopped.status is SandboxStatus.stopped
    assert stopped.stopped_at is not None
    assert docker.stop_calls and docker.stop_calls[0]["container_id"] == "fake-cid-0001"
    assert docker.remove_calls  # remove=True default
    assert events.events[-1][0] == "ui_sandbox.stopped"


def test_manager_stop_captures_docker_error_as_warning(tmp_path: Path):
    docker = FakeDockerClient(stop_error=RuntimeError("stop failed"))
    mgr, _, _, _ = _manager_for(tmp_path, docker=docker)
    mgr.create(_sample_config(tmp_path, session_id="sess-w", host_port=40160))
    mgr.start("sess-w")
    stopped = mgr.stop("sess-w")
    assert stopped.status is SandboxStatus.stopped
    assert any("stop_failed" in w for w in stopped.warnings)


def test_manager_stop_is_idempotent_on_terminal(tmp_path: Path):
    mgr, docker, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-t", host_port=40170))
    mgr.start("sess-t")
    first = mgr.stop("sess-t")
    calls_before = len(docker.stop_calls)
    second = mgr.stop("sess-t")
    assert second.status is SandboxStatus.stopped
    assert len(docker.stop_calls) == calls_before  # no extra docker call


def test_manager_remove_requires_terminal(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-r", host_port=40180))
    with pytest.raises(SandboxError):
        mgr.remove("sess-r")
    mgr.start("sess-r")
    mgr.stop("sess-r")
    final = mgr.remove("sess-r")
    assert final.status is SandboxStatus.stopped
    assert mgr.get("sess-r") is None


def test_manager_touch_updates_last_active(tmp_path: Path):
    clock = FakeClock(start=500.0)
    mgr, _, _, _ = _manager_for(tmp_path, clock=clock)
    mgr.create(_sample_config(tmp_path, session_id="sess-tch", host_port=40190))
    mgr.start("sess-tch")
    before = mgr.get("sess-tch")
    assert before is not None
    touched = mgr.touch("sess-tch")
    assert touched.last_active_at > before.last_active_at


def test_manager_touch_is_noop_on_terminal(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-tn", host_port=40200))
    mgr.start("sess-tn")
    mgr.stop("sess-tn")
    stopped = mgr.get("sess-tn")
    assert stopped is not None
    again = mgr.touch("sess-tn")
    assert again.last_active_at == stopped.last_active_at  # not bumped


def test_manager_mark_ready_rejects_wrong_status(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-mr", host_port=40210))
    # still in pending — mark_ready should refuse.
    with pytest.raises(SandboxError):
        mgr.mark_ready("sess-mr")


def test_manager_mark_ready_is_idempotent(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-mri", host_port=40220))
    mgr.start("sess-mri")
    mgr.mark_ready("sess-mri")
    again = mgr.mark_ready("sess-mri")
    assert again.status is SandboxStatus.running


def test_manager_logs_passes_through_docker(tmp_path: Path):
    docker = FakeDockerClient(canned_logs="compiled successfully\n")
    mgr, _, _, _ = _manager_for(tmp_path, docker=docker)
    mgr.create(_sample_config(tmp_path, session_id="sess-lg", host_port=40230))
    mgr.start("sess-lg")
    assert "compiled successfully" in mgr.logs("sess-lg")


def test_manager_logs_cap(tmp_path: Path):
    big = "x" * (MAX_LOG_CHARS + 500)
    docker = FakeDockerClient(canned_logs=big)
    mgr, _, _, _ = _manager_for(tmp_path, docker=docker)
    mgr.create(_sample_config(tmp_path, session_id="sess-lg2", host_port=40240))
    mgr.start("sess-lg2")
    assert len(mgr.logs("sess-lg2")) == MAX_LOG_CHARS


def test_manager_poll_ready_true_on_banner(tmp_path: Path):
    docker = FakeDockerClient(canned_logs="✓ Ready in 2.3s")
    mgr, _, _, _ = _manager_for(tmp_path, docker=docker)
    mgr.create(_sample_config(tmp_path, session_id="sess-pr", host_port=40250))
    mgr.start("sess-pr")
    assert mgr.poll_ready("sess-pr") is True


def test_manager_snapshot_is_json_safe(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-snap", host_port=40260))
    mgr.start("sess-snap")
    snap = mgr.snapshot()
    dumped = json.dumps(snap)
    assert "sess-snap" in dumped
    assert snap["count"] == 1
    assert snap["schema_version"] == UI_SANDBOX_SCHEMA_VERSION


def test_manager_list_returns_tuple(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-ls", host_port=40270))
    out = mgr.list()
    assert isinstance(out, tuple) and len(out) == 1


def test_manager_event_callback_receives_lifecycle_events(tmp_path: Path):
    mgr, _, _, events = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-ev", host_port=40280))
    mgr.start("sess-ev")
    mgr.mark_ready("sess-ev")
    mgr.stop("sess-ev")
    event_types = [e[0] for e in events.events]
    assert event_types == [
        "ui_sandbox.created",
        "ui_sandbox.starting",
        "ui_sandbox.ready",
        "ui_sandbox.stopped",
    ]


def test_manager_event_callback_errors_are_swallowed(tmp_path: Path):
    def bad_cb(event_type: str, payload: Mapping[str, Any]) -> None:
        raise RuntimeError("callback boom")

    mgr = SandboxManager(docker_client=FakeDockerClient(), event_cb=bad_cb)
    # must NOT raise — callback failure doesn't kill the agent loop.
    mgr.create(_sample_config(tmp_path, session_id="sess-cb", host_port=40290))


def test_manager_is_thread_safe_under_concurrent_create(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    count = 20

    def worker(i: int) -> None:
        sid = f"th-{i:03d}"
        mgr.create(_sample_config(tmp_path, session_id=sid, host_port=40300 + i))
        mgr.start(sid)
        mgr.mark_ready(sid)
        mgr.touch(sid)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(mgr.list()) == count
    assert {inst.status for inst in mgr.list()} == {SandboxStatus.running}


# ── render_sandbox_status_markdown ──────────────────────────────────


def test_render_sandbox_status_markdown_deterministic(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-md", host_port=40400))
    mgr.start("sess-md")
    inst = mgr.get("sess-md")
    assert inst is not None
    a = render_sandbox_status_markdown(inst)
    b = render_sandbox_status_markdown(inst)
    assert a == b


def test_render_sandbox_status_markdown_contains_key_fields(tmp_path: Path):
    mgr, _, _, _ = _manager_for(tmp_path)
    mgr.create(_sample_config(tmp_path, session_id="sess-md2", host_port=40410))
    mgr.start("sess-md2")
    inst = mgr.get("sess-md2")
    assert inst is not None
    md = render_sandbox_status_markdown(inst)
    assert "sess-md2" in md
    assert "40410" in md
    assert inst.container_name in md
    assert inst.preview_url in md
    assert DEFAULT_SANDBOX_IMAGE in md


def test_render_sandbox_status_markdown_rejects_non_instance():
    with pytest.raises(TypeError):
        render_sandbox_status_markdown({"session_id": "x"})  # type: ignore[arg-type]


# ── SubprocessDockerClient smoke-tests ──────────────────────────────


def test_subprocess_docker_client_builds_run_argv(tmp_path: Path):
    """Inject a stub runner — exercises the argv builder without touching docker."""

    captured: dict[str, Any] = {}

    import subprocess as _sp

    def fake_runner(
        argv: list[str], **_: Any
    ) -> _sp.CompletedProcess[str]:
        captured["argv"] = argv
        return _sp.CompletedProcess(argv, 0, stdout="fake-cid-001\n", stderr="")

    client = us.SubprocessDockerClient(runner=fake_runner)
    cid = client.run_detached(
        image="node:22-alpine",
        name="omnisight-ui-sess",
        command=["sh", "-c", "npm run dev"],
        mounts=[{"source": str(tmp_path), "target": "/app"}],
        ports={40000: 3000},
        env={"NODE_ENV": "development"},
        workdir="/app",
    )
    assert cid == "fake-cid-001"
    argv = captured["argv"]
    assert argv[0:3] == ["docker", "run", "-d"]
    assert "--rm" in argv
    assert "--name" in argv and "omnisight-ui-sess" in argv
    assert "-w" in argv and "/app" in argv
    assert f"{tmp_path}:/app" in argv
    assert "40000:3000" in argv
    assert "NODE_ENV=development" in argv
    assert argv[-3:] == ["node:22-alpine", "sh", "-c"] or argv[-1] == "npm run dev"


def test_subprocess_docker_client_handles_missing_binary():
    def raiser(*_: Any, **__: Any) -> Any:
        raise FileNotFoundError("docker not installed")

    client = us.SubprocessDockerClient(runner=raiser)
    with pytest.raises(SandboxError):
        client.run_detached(
            image="node",
            name="x",
            command=["sh"],
            mounts=[],
            ports={},
            env={},
            workdir="/app",
        )


def test_subprocess_docker_client_raises_on_nonzero_exit():
    import subprocess as _sp

    def fake_runner(argv: list[str], **_: Any) -> _sp.CompletedProcess[str]:
        return _sp.CompletedProcess(argv, 1, stdout="", stderr="bad image")

    client = us.SubprocessDockerClient(runner=fake_runner)
    with pytest.raises(SandboxError):
        client.run_detached(
            image="bad",
            name="x",
            command=["sh"],
            mounts=[],
            ports={},
            env={},
            workdir="/app",
        )


def test_subprocess_docker_client_logs_returns_empty_on_error():
    import subprocess as _sp

    def fake_runner(argv: list[str], **_: Any) -> _sp.CompletedProcess[str]:
        return _sp.CompletedProcess(argv, 1, stdout="", stderr="nope")

    client = us.SubprocessDockerClient(runner=fake_runner)
    # logs tolerates error → returns "".
    assert client.logs("cid") == ""


# ── Sibling alignment ───────────────────────────────────────────────


def test_sibling_alignment_preview_port_default():
    # Dev server inside container is port 3000 — Next.js default.  Changing
    # this requires updating docs/sop + the UI Designer skill page.
    assert DEFAULT_CONTAINER_PORT == 3000


def test_sibling_alignment_schema_version_embedded_in_dicts(tmp_path: Path):
    c = _sample_config(tmp_path)
    assert c.to_dict()["schema_version"] == UI_SANDBOX_SCHEMA_VERSION
    inst = _instance_for(tmp_path)
    assert inst.to_dict()["schema_version"] == UI_SANDBOX_SCHEMA_VERSION
    assert build_docker_run_spec(c)["schema_version"] == UI_SANDBOX_SCHEMA_VERSION
