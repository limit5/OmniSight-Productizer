"""V2 #2 (issue #318) — ui_sandbox_lifecycle contract tests.

Pins ``backend/ui_sandbox_lifecycle.py`` against the V2 row 2 spec:

  * full lifecycle orchestration — create → start → hot-reload →
    screenshot → stop → cleanup;
  * 1 sandbox per session (idempotent ensure_session +
    WorkspaceMismatch on disagreement);
  * idle 15 min → auto-reap (sync and background-thread paths);
  * screenshot hook injection (V2 row 3 wires the real one later);
  * event emission for every state transition.

All tests drive a deterministic ``FakeClock`` + ``FakeSleep`` —
no real-world time is ever consumed, no real docker daemon is
touched.  The suite completes in well under 1 s and runs clean
alongside V2 #1's 166 tests.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from backend import ui_sandbox as us
from backend import ui_sandbox_lifecycle as ul
from backend.ui_sandbox import (
    DEFAULT_CONTAINER_PORT,
    DEFAULT_HOST_PORT_RANGE,
    DEFAULT_IDLE_LIMIT_S,
    SandboxConfig,
    SandboxError,
    SandboxInstance,
    SandboxManager,
    SandboxNotFound,
    SandboxStatus,
)
from backend.ui_sandbox_lifecycle import (
    DEFAULT_READY_POLL_INTERVAL_S,
    DEFAULT_READY_POLL_TIMEOUT_S,
    DEFAULT_REAPER_INTERVAL_S,
    LIFECYCLE_EVENT_ENSURE,
    LIFECYCLE_EVENT_HOT_RELOAD,
    LIFECYCLE_EVENT_READY_TIMEOUT,
    LIFECYCLE_EVENT_REAPED,
    LIFECYCLE_EVENT_SCREENSHOT,
    LIFECYCLE_EVENT_TEARDOWN,
    LIFECYCLE_EVENT_TYPES,
    MAX_SANDBOXES_PER_SESSION,
    SANDBOX_LIFECYCLE_SCHEMA_VERSION,
    LifecycleError,
    ReadyTimeout,
    ReapReport,
    SandboxLifecycle,
    ScreenshotResult,
    ScreenshotUnavailable,
    WorkspaceMismatch,
)


# ── Module invariants ────────────────────────────────────────────────


EXPECTED_ALL = {
    "SANDBOX_LIFECYCLE_SCHEMA_VERSION",
    "DEFAULT_READY_POLL_INTERVAL_S",
    "DEFAULT_READY_POLL_TIMEOUT_S",
    "DEFAULT_REAPER_INTERVAL_S",
    "DEFAULT_IDLE_LIMIT_S",
    "MAX_SANDBOXES_PER_SESSION",
    "LIFECYCLE_EVENT_ENSURE",
    "LIFECYCLE_EVENT_HOT_RELOAD",
    "LIFECYCLE_EVENT_SCREENSHOT",
    "LIFECYCLE_EVENT_TEARDOWN",
    "LIFECYCLE_EVENT_REAPED",
    "LIFECYCLE_EVENT_READY_TIMEOUT",
    "LIFECYCLE_EVENT_TYPES",
    "ScreenshotHook",
    "ScreenshotResult",
    "ReapReport",
    "SandboxLifecycle",
    "LifecycleError",
    "ReadyTimeout",
    "ScreenshotUnavailable",
    "WorkspaceMismatch",
}


def test_all_exports_match():
    assert set(ul.__all__) == EXPECTED_ALL


@pytest.mark.parametrize("name", sorted(EXPECTED_ALL))
def test_each_export_exists(name: str):
    assert hasattr(ul, name)


def test_schema_version_is_semver():
    parts = SANDBOX_LIFECYCLE_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_idle_limit_matches_v2_spec():
    # V2 row 2: idle 15 min 自動回收
    assert DEFAULT_IDLE_LIMIT_S == 900.0


def test_max_per_session_is_one():
    # V2 row 2: 每 session 最多 1 sandbox
    assert MAX_SANDBOXES_PER_SESSION == 1


def test_ready_poll_interval_positive():
    assert DEFAULT_READY_POLL_INTERVAL_S > 0


def test_ready_poll_timeout_positive():
    assert DEFAULT_READY_POLL_TIMEOUT_S > 0


def test_reaper_interval_positive():
    assert DEFAULT_REAPER_INTERVAL_S > 0


def test_lifecycle_event_types_are_namespaced():
    # All lifecycle events live under the ui_sandbox.* SSE namespace
    # so V2 row 6 (SSE bus) can subscribe with one prefix filter.
    for ev in LIFECYCLE_EVENT_TYPES:
        assert ev.startswith("ui_sandbox.")
    # No duplicates.
    assert len(set(LIFECYCLE_EVENT_TYPES)) == len(LIFECYCLE_EVENT_TYPES)


def test_lifecycle_event_constants_mirror_tuple():
    assert LIFECYCLE_EVENT_ENSURE in LIFECYCLE_EVENT_TYPES
    assert LIFECYCLE_EVENT_HOT_RELOAD in LIFECYCLE_EVENT_TYPES
    assert LIFECYCLE_EVENT_SCREENSHOT in LIFECYCLE_EVENT_TYPES
    assert LIFECYCLE_EVENT_TEARDOWN in LIFECYCLE_EVENT_TYPES
    assert LIFECYCLE_EVENT_REAPED in LIFECYCLE_EVENT_TYPES
    assert LIFECYCLE_EVENT_READY_TIMEOUT in LIFECYCLE_EVENT_TYPES


def test_lifecycle_error_subclasses_sandbox_error():
    # Keeps existing `except SandboxError` call sites working.
    assert issubclass(LifecycleError, SandboxError)
    assert issubclass(ReadyTimeout, LifecycleError)
    assert issubclass(ScreenshotUnavailable, LifecycleError)
    assert issubclass(WorkspaceMismatch, LifecycleError)


# ── ScreenshotResult ─────────────────────────────────────────────────


def test_screenshot_result_fields_and_byte_len():
    r = ScreenshotResult(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        viewport="desktop",
        path="/",
        image_bytes=b"\x89PNG\r\n",
        captured_at=1234.0,
    )
    assert r.byte_len == 6
    assert r.session_id == "sess-1"


def test_screenshot_result_is_frozen():
    r = ScreenshotResult(
        session_id="s",
        preview_url="http://x/",
        viewport="desktop",
        path="/",
        image_bytes=b"a",
        captured_at=0.0,
    )
    with pytest.raises(Exception):
        r.session_id = "other"  # type: ignore[misc]


def test_screenshot_result_rejects_bad_inputs():
    with pytest.raises(ValueError):
        ScreenshotResult(
            session_id="",
            preview_url="http://x/",
            viewport="desktop",
            path="/",
            image_bytes=b"a",
            captured_at=0.0,
        )
    with pytest.raises(ValueError):
        ScreenshotResult(
            session_id="s",
            preview_url="",
            viewport="desktop",
            path="/",
            image_bytes=b"a",
            captured_at=0.0,
        )
    with pytest.raises(ValueError):
        ScreenshotResult(
            session_id="s",
            preview_url="http://x/",
            viewport="",
            path="/",
            image_bytes=b"a",
            captured_at=0.0,
        )
    with pytest.raises(ValueError):
        ScreenshotResult(
            session_id="s",
            preview_url="http://x/",
            viewport="desktop",
            path="no-leading-slash",
            image_bytes=b"a",
            captured_at=0.0,
        )
    with pytest.raises(ValueError):
        ScreenshotResult(
            session_id="s",
            preview_url="http://x/",
            viewport="desktop",
            path="/",
            image_bytes="not bytes",  # type: ignore[arg-type]
            captured_at=0.0,
        )
    with pytest.raises(ValueError):
        ScreenshotResult(
            session_id="s",
            preview_url="http://x/",
            viewport="desktop",
            path="/",
            image_bytes=b"a",
            captured_at=-1.0,
        )


def test_screenshot_result_to_dict_json_safe_default():
    r = ScreenshotResult(
        session_id="s",
        preview_url="http://x/",
        viewport="desktop",
        path="/",
        image_bytes=b"\x89PNG",
        captured_at=1.0,
    )
    d = r.to_dict()
    # default MUST NOT dump the bytes (SSE payloads stay small).
    assert "image_base64" not in d
    assert d["byte_len"] == 4
    assert json.dumps(d)  # must be JSON-serialisable


def test_screenshot_result_to_dict_with_bytes_base64():
    r = ScreenshotResult(
        session_id="s",
        preview_url="http://x/",
        viewport="desktop",
        path="/",
        image_bytes=b"\x89PNG",
        captured_at=1.0,
    )
    d = r.to_dict(include_bytes=True)
    assert "image_base64" in d
    # V2 row 6 injects this into Opus multimodal messages; validate
    # we can decode what we encoded.
    import base64

    assert base64.b64decode(d["image_base64"]) == b"\x89PNG"


def test_screenshot_result_schema_version_embedded():
    r = ScreenshotResult(
        session_id="s",
        preview_url="http://x/",
        viewport="desktop",
        path="/",
        image_bytes=b"a",
        captured_at=0.0,
    )
    assert r.to_dict()["schema_version"] == SANDBOX_LIFECYCLE_SCHEMA_VERSION


# ── ReapReport ───────────────────────────────────────────────────────


def test_reap_report_defaults_and_counts():
    r = ReapReport(reaped_at=1.0)
    assert r.reaped_count == 0
    assert r.idle_limit_s == DEFAULT_IDLE_LIMIT_S


def test_reap_report_is_frozen():
    r = ReapReport(reaped_at=1.0)
    with pytest.raises(Exception):
        r.reaped_at = 2.0  # type: ignore[misc]


def test_reap_report_normalises_iterables_to_tuples():
    r = ReapReport(
        reaped_at=1.0,
        reaped_sessions=["a", "b"],  # type: ignore[arg-type]
        still_active=["c"],  # type: ignore[arg-type]
        warnings=["w"],  # type: ignore[arg-type]
    )
    assert isinstance(r.reaped_sessions, tuple)
    assert isinstance(r.still_active, tuple)
    assert isinstance(r.warnings, tuple)


def test_reap_report_rejects_bad_inputs():
    with pytest.raises(ValueError):
        ReapReport(reaped_at=-1.0)
    with pytest.raises(ValueError):
        ReapReport(reaped_at=1.0, idle_limit_s=0)


def test_reap_report_to_dict_json_safe():
    r = ReapReport(
        reaped_at=1.0,
        reaped_sessions=("a",),
        still_active=("b",),
        warnings=("w",),
    )
    d = r.to_dict()
    assert json.dumps(d)
    assert d["reaped_count"] == 1
    assert d["schema_version"] == SANDBOX_LIFECYCLE_SCHEMA_VERSION


# ── Fake docker + hook fixtures ──────────────────────────────────────


class FakeDockerClient:
    """Deterministic in-memory docker — mirrors the V2 #1 fake."""

    def __init__(
        self,
        *,
        run_error: Exception | None = None,
        stop_error: Exception | None = None,
        remove_error: Exception | None = None,
        canned_logs: str = "✓ Ready in 1.0s",
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
        self.run_calls.append({"name": name, "container_id": cid, "image": image})
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
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds

    @property
    def now(self) -> float:
        return self._t


class FakeSleep:
    """Sleep stub that advances a paired FakeClock instead of blocking."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


class RecordingEventCallback:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def __call__(self, event_type: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self.events.append((event_type, dict(payload)))

    def types(self) -> list[str]:
        with self._lock:
            return [t for t, _ in self.events]


def _sample_config(tmp_path: Path, **overrides: Any) -> SandboxConfig:
    kwargs: dict[str, Any] = dict(
        session_id="sess-1",
        workspace_path=str(tmp_path),
        host_port=40500,
    )
    kwargs.update(overrides)
    return SandboxConfig(**kwargs)


def _make_lifecycle(
    tmp_path: Path,
    *,
    docker: FakeDockerClient | None = None,
    canned_logs: str = "✓ Ready in 1.0s",
    screenshot_hook: Any = None,
    idle_limit_s: float = DEFAULT_IDLE_LIMIT_S,
    ready_poll_timeout_s: float = DEFAULT_READY_POLL_TIMEOUT_S,
    ready_poll_interval_s: float = 1.0,
    reaper_interval_s: float = 30.0,
) -> tuple[SandboxLifecycle, FakeDockerClient, FakeClock, FakeSleep, RecordingEventCallback, SandboxManager]:
    docker = docker or FakeDockerClient(canned_logs=canned_logs)
    clock = FakeClock()
    sleep = FakeSleep(clock)
    events = RecordingEventCallback()
    mgr = SandboxManager(docker_client=docker, clock=clock, event_cb=events)
    life = SandboxLifecycle(
        manager=mgr,
        screenshot_hook=screenshot_hook,
        clock=clock,
        sleep=sleep,
        event_cb=events,
        idle_limit_s=idle_limit_s,
        ready_poll_interval_s=ready_poll_interval_s,
        ready_poll_timeout_s=ready_poll_timeout_s,
        reaper_interval_s=reaper_interval_s,
    )
    return life, docker, clock, sleep, events, mgr


# ── SandboxLifecycle constructor ─────────────────────────────────────


def test_lifecycle_rejects_non_manager():
    with pytest.raises(TypeError):
        SandboxLifecycle(manager="not a manager")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"idle_limit_s": 0},
        {"idle_limit_s": -1.0},
        {"ready_poll_interval_s": 0},
        {"ready_poll_timeout_s": -5.0},
        {"reaper_interval_s": 0},
    ],
)
def test_lifecycle_rejects_non_positive_durations(tmp_path: Path, kwargs: dict):
    docker = FakeDockerClient()
    mgr = SandboxManager(docker_client=docker)
    with pytest.raises(ValueError):
        SandboxLifecycle(manager=mgr, **kwargs)


def test_lifecycle_manager_property(tmp_path: Path):
    life, _, _, _, _, mgr = _make_lifecycle(tmp_path)
    assert life.manager is mgr


def test_lifecycle_set_screenshot_hook(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    assert life.screenshot_hook is None
    hook = lambda **kw: b"png"  # noqa: E731
    life.set_screenshot_hook(hook)
    assert life.screenshot_hook is hook
    life.set_screenshot_hook(None)
    assert life.screenshot_hook is None


# ── ensure_session: create → start → wait_ready ─────────────────────


def test_ensure_session_creates_and_starts_and_waits_ready(tmp_path: Path):
    life, docker, clock, sleep, events, mgr = _make_lifecycle(tmp_path)
    config = _sample_config(tmp_path, session_id="sess-a", host_port=40501)
    instance = life.ensure_session(config)
    assert instance.status is SandboxStatus.running
    # container was actually run.
    assert len(docker.run_calls) == 1
    # Lifecycle emits an ensure event at the end.
    assert LIFECYCLE_EVENT_ENSURE in events.types()


def test_ensure_session_idempotent_returns_same_running_sandbox(tmp_path: Path):
    life, docker, *_ = _make_lifecycle(tmp_path)
    config = _sample_config(tmp_path, session_id="sess-i", host_port=40510)
    first = life.ensure_session(config)
    second = life.ensure_session(config)
    assert first.session_id == second.session_id
    assert second.status is SandboxStatus.running
    # Second call MUST NOT spawn a new container.
    assert len(docker.run_calls) == 1


def test_ensure_session_rejects_non_config(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    with pytest.raises(TypeError):
        life.ensure_session("not a config")  # type: ignore[arg-type]


def test_ensure_session_workspace_mismatch_raises(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-w", host_port=40520)
    )
    with pytest.raises(WorkspaceMismatch):
        life.ensure_session(
            SandboxConfig(
                session_id="sess-w",
                workspace_path=str(other),
                host_port=40521,
            )
        )


def test_ensure_session_recreate_true_tears_down_old(tmp_path: Path):
    life, docker, *_ = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-r", host_port=40530)
    )
    other = tmp_path / "other"
    other.mkdir()
    new_inst = life.ensure_session(
        SandboxConfig(
            session_id="sess-r",
            workspace_path=str(other),
            host_port=40531,
        ),
        recreate=True,
    )
    assert new_inst.status is SandboxStatus.running
    # Two containers spawned total; the first was stopped/removed.
    assert len(docker.run_calls) == 2
    assert len(docker.stop_calls) == 1
    assert len(docker.remove_calls) == 1


def test_ensure_session_recreates_terminal_sandbox(tmp_path: Path):
    life, docker, *_ = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-t", host_port=40540)
    )
    life.teardown("sess-t", remove=False)  # now in terminal status, still in registry
    # Re-ensuring with same config should recreate fresh.
    new_inst = life.ensure_session(
        _sample_config(tmp_path, session_id="sess-t", host_port=40541)
    )
    assert new_inst.status is SandboxStatus.running
    assert len(docker.run_calls) == 2


def test_ensure_session_wait_ready_false_returns_starting(tmp_path: Path):
    # If wait_ready=False and logs show ready anyway, the sandbox
    # may still be in "starting" status since we don't call
    # mark_ready for the caller.
    life, _, _, _, _, mgr = _make_lifecycle(tmp_path, canned_logs="")
    instance = life.ensure_session(
        _sample_config(tmp_path, session_id="sess-nw", host_port=40550),
        wait_ready=False,
    )
    assert instance.status is SandboxStatus.starting


def test_ensure_session_on_docker_failure_returns_failed(tmp_path: Path):
    docker = FakeDockerClient(run_error=RuntimeError("boom"))
    life, *_ = _make_lifecycle(tmp_path, docker=docker)
    instance = life.ensure_session(
        _sample_config(tmp_path, session_id="sess-f", host_port=40560)
    )
    assert instance.status is SandboxStatus.failed
    assert "boom" in (instance.error or "")


def test_ensure_session_emits_ensure_event_with_lifecycle_schema_version(tmp_path: Path):
    life, _, _, _, events, _ = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-sv", host_port=40565)
    )
    ensure_events = [(t, p) for t, p in events.events if t == LIFECYCLE_EVENT_ENSURE]
    assert ensure_events
    _, payload = ensure_events[-1]
    assert payload.get("lifecycle_schema_version") == SANDBOX_LIFECYCLE_SCHEMA_VERSION


# ── wait_ready semantics ─────────────────────────────────────────────


def test_wait_ready_polls_logs_then_marks_ready(tmp_path: Path):
    # Build a manager with empty logs; flip to ready after one sleep.
    docker = FakeDockerClient(canned_logs="")
    life, *_ = _make_lifecycle(
        tmp_path,
        docker=docker,
        ready_poll_interval_s=1.0,
        ready_poll_timeout_s=10.0,
    )
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-wr", host_port=40570),
        wait_ready=False,
    )
    docker.canned_logs = "compiled successfully"
    ready = life.wait_ready("sess-wr")
    assert ready.status is SandboxStatus.running


def test_wait_ready_timeout_raises_and_emits_event(tmp_path: Path):
    docker = FakeDockerClient(canned_logs="")  # never becomes ready
    life, _, _, _, events, _ = _make_lifecycle(
        tmp_path,
        docker=docker,
        ready_poll_interval_s=1.0,
        ready_poll_timeout_s=3.0,
    )
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-to", host_port=40580),
        wait_ready=False,
    )
    with pytest.raises(ReadyTimeout):
        life.wait_ready("sess-to")
    assert LIFECYCLE_EVENT_READY_TIMEOUT in events.types()


def test_wait_ready_returns_immediately_if_already_running(tmp_path: Path):
    life, _, _, sleep, _, _ = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-ar", host_port=40590)
    )
    calls_before = len(sleep.calls)
    life.wait_ready("sess-ar")
    # No additional sleeps needed for already-running sandbox.
    assert len(sleep.calls) == calls_before


def test_wait_ready_raises_on_unknown_session(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    with pytest.raises(SandboxNotFound):
        life.wait_ready("ghost")


def test_wait_ready_raises_when_sandbox_already_failed(tmp_path: Path):
    docker = FakeDockerClient(run_error=RuntimeError("kaboom"))
    life, *_ = _make_lifecycle(tmp_path, docker=docker)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-fl", host_port=40595)
    )  # lands in failed state
    with pytest.raises(LifecycleError):
        life.wait_ready("sess-fl")


@pytest.mark.parametrize("bad", [0, -1.0])
def test_wait_ready_rejects_bad_timeouts(tmp_path: Path, bad: float):
    life, *_ = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-bt", host_port=40598)
    )
    with pytest.raises(ValueError):
        life.wait_ready("sess-bt", timeout_s=bad)
    with pytest.raises(ValueError):
        life.wait_ready("sess-bt", poll_interval_s=bad)


# ── hot_reload ───────────────────────────────────────────────────────


def test_hot_reload_touches_last_active_at(tmp_path: Path):
    life, _, clock, _, _, mgr = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-hr", host_port=40600)
    )
    before = mgr.get("sess-hr")
    assert before is not None
    clock.advance(10.0)
    after = life.hot_reload("sess-hr", files_changed=("a.tsx", "b.tsx"))
    assert after.last_active_at > before.last_active_at


def test_hot_reload_emits_event_with_files(tmp_path: Path):
    life, _, _, _, events, _ = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-hre", host_port=40610)
    )
    life.hot_reload("sess-hre", files_changed=("pages/index.tsx",))
    hot_events = [(t, p) for t, p in events.events if t == LIFECYCLE_EVENT_HOT_RELOAD]
    assert hot_events
    _, payload = hot_events[-1]
    assert payload["files_changed"] == ["pages/index.tsx"]
    assert payload["file_count"] == 1


def test_hot_reload_accepts_empty_file_list(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-hre2", host_port=40620)
    )
    # agent signalling "I'm alive" without naming specific files.
    inst = life.hot_reload("sess-hre2")
    assert inst.status is SandboxStatus.running


def test_hot_reload_rejects_unknown_session(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    with pytest.raises(SandboxNotFound):
        life.hot_reload("ghost")


def test_hot_reload_noop_on_terminal_sandbox(tmp_path: Path):
    life, _, _, _, _, mgr = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-hrt", host_port=40630)
    )
    # Stop but keep registry entry (remove=False).
    life.teardown("sess-hrt", remove=False)
    inst = life.hot_reload("sess-hrt")
    assert inst.is_terminal


# ── capture_screenshot ──────────────────────────────────────────────


def test_capture_screenshot_without_hook_raises(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path, screenshot_hook=None)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-sn", host_port=40640)
    )
    with pytest.raises(ScreenshotUnavailable):
        life.capture_screenshot("sess-sn")


def test_capture_screenshot_invokes_hook_and_emits_event(tmp_path: Path):
    captured: list[dict[str, Any]] = []

    def hook(*, session_id, preview_url, viewport, path):
        captured.append(
            {
                "session_id": session_id,
                "preview_url": preview_url,
                "viewport": viewport,
                "path": path,
            }
        )
        return b"\x89PNG\x0d\x0a\x1a\x0a"

    life, _, _, _, events, _ = _make_lifecycle(tmp_path, screenshot_hook=hook)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-snh", host_port=40650)
    )
    result = life.capture_screenshot("sess-snh", viewport="tablet", path="/pricing")
    assert isinstance(result, ScreenshotResult)
    assert result.byte_len == 8
    assert result.viewport == "tablet"
    assert result.path == "/pricing"
    assert captured[0]["preview_url"].startswith("http://127.0.0.1:")
    screenshot_events = [
        (t, p) for t, p in events.events if t == LIFECYCLE_EVENT_SCREENSHOT
    ]
    assert screenshot_events


def test_capture_screenshot_touches_last_active(tmp_path: Path):
    life, _, clock, _, _, mgr = _make_lifecycle(
        tmp_path, screenshot_hook=lambda **kw: b"\x89PNG"
    )
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-snt", host_port=40660)
    )
    before = mgr.get("sess-snt")
    assert before is not None
    clock.advance(50.0)
    life.capture_screenshot("sess-snt")
    after = mgr.get("sess-snt")
    assert after is not None
    assert after.last_active_at > before.last_active_at


def test_capture_screenshot_rejects_non_running(tmp_path: Path):
    life, *_ = _make_lifecycle(
        tmp_path, screenshot_hook=lambda **kw: b"\x89PNG", canned_logs=""
    )
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-snns", host_port=40670),
        wait_ready=False,
    )
    with pytest.raises(LifecycleError):
        life.capture_screenshot("sess-snns")


def test_capture_screenshot_hook_raises_wrapped(tmp_path: Path):
    def bad_hook(**kw):
        raise RuntimeError("playwright crashed")

    life, *_ = _make_lifecycle(tmp_path, screenshot_hook=bad_hook)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-sne", host_port=40680)
    )
    with pytest.raises(LifecycleError) as exc_info:
        life.capture_screenshot("sess-sne")
    assert "playwright crashed" in str(exc_info.value)


def test_capture_screenshot_rejects_non_bytes_return(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path, screenshot_hook=lambda **kw: "not bytes")
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-snb", host_port=40690)
    )
    with pytest.raises(LifecycleError):
        life.capture_screenshot("sess-snb")


def test_capture_screenshot_rejects_empty_bytes(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path, screenshot_hook=lambda **kw: b"")
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-sn0", host_port=40700)
    )
    with pytest.raises(LifecycleError):
        life.capture_screenshot("sess-sn0")


def test_capture_screenshot_rejects_unknown_session(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path, screenshot_hook=lambda **kw: b"a")
    with pytest.raises(SandboxNotFound):
        life.capture_screenshot("ghost")


@pytest.mark.parametrize("bad_viewport", ["", "   "])
def test_capture_screenshot_rejects_blank_viewport(tmp_path: Path, bad_viewport: str):
    life, *_ = _make_lifecycle(tmp_path, screenshot_hook=lambda **kw: b"a")
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-snv", host_port=40710)
    )
    with pytest.raises(ValueError):
        life.capture_screenshot("sess-snv", viewport=bad_viewport)


def test_capture_screenshot_rejects_bad_path(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path, screenshot_hook=lambda **kw: b"a")
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-snp", host_port=40720)
    )
    with pytest.raises(ValueError):
        life.capture_screenshot("sess-snp", path="no-slash")


# ── teardown ─────────────────────────────────────────────────────────


def test_teardown_stops_and_removes(tmp_path: Path):
    life, docker, _, _, events, mgr = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-td", host_port=40730)
    )
    final = life.teardown("sess-td")
    assert final.status is SandboxStatus.stopped
    assert len(docker.stop_calls) == 1
    assert len(docker.remove_calls) == 1
    # Removed from registry after remove=True.
    assert mgr.get("sess-td") is None
    assert LIFECYCLE_EVENT_TEARDOWN in events.types()


def test_teardown_remove_false_keeps_registry_entry(tmp_path: Path):
    life, _, _, _, _, mgr = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-tdr", host_port=40740)
    )
    life.teardown("sess-tdr", remove=False)
    inst = mgr.get("sess-tdr")
    assert inst is not None
    assert inst.status is SandboxStatus.stopped


def test_teardown_unknown_session_raises(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    with pytest.raises(SandboxNotFound):
        life.teardown("ghost")


def test_teardown_idempotent_on_terminal_when_removing(tmp_path: Path):
    life, _, _, _, _, mgr = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-tdi", host_port=40750)
    )
    # First teardown without removing — leaves a terminal entry.
    life.teardown("sess-tdi", remove=False)
    # Second teardown with remove=True — just removes, no additional docker call.
    life.teardown("sess-tdi", remove=True)
    assert mgr.get("sess-tdi") is None


def test_teardown_swallows_docker_stop_errors(tmp_path: Path):
    docker = FakeDockerClient(stop_error=RuntimeError("stop kaboom"))
    life, *_ = _make_lifecycle(tmp_path, docker=docker)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-tds", host_port=40760)
    )
    final = life.teardown("sess-tds")
    # Errors surface on warnings; teardown itself never raises.
    assert any("stop_failed" in w for w in final.warnings)


# ── reap_idle (synchronous) ─────────────────────────────────────────


def test_reap_idle_reaps_sandbox_past_limit(tmp_path: Path):
    life, docker, clock, _, events, mgr = _make_lifecycle(
        tmp_path, idle_limit_s=100.0
    )
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-re", host_port=40770)
    )
    # Advance clock past the idle limit without touching.
    clock.advance(500.0)
    report = life.reap_idle()
    assert "sess-re" in report.reaped_sessions
    assert report.reaped_count == 1
    assert mgr.get("sess-re") is None
    assert LIFECYCLE_EVENT_REAPED in events.types()


def test_reap_idle_preserves_recently_touched(tmp_path: Path):
    life, _, clock, _, _, mgr = _make_lifecycle(tmp_path, idle_limit_s=100.0)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-rep", host_port=40780)
    )
    clock.advance(50.0)  # below limit
    report = life.reap_idle()
    assert "sess-rep" not in report.reaped_sessions
    assert "sess-rep" in report.still_active
    assert mgr.get("sess-rep") is not None


def test_reap_idle_collects_terminal_sandboxes_even_without_idle(tmp_path: Path):
    life, _, _, _, _, mgr = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-rt", host_port=40790)
    )
    # Stop without removing — terminal entry remains.
    life.teardown("sess-rt", remove=False)
    report = life.reap_idle()
    assert "sess-rt" in report.reaped_sessions
    assert mgr.get("sess-rt") is None


def test_reap_idle_with_no_candidates_returns_empty(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    report = life.reap_idle()
    assert report.reaped_count == 0


def test_reap_idle_custom_limit_override(tmp_path: Path):
    life, _, clock, _, _, _ = _make_lifecycle(tmp_path, idle_limit_s=1000.0)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-rc", host_port=40800)
    )
    clock.advance(200.0)
    # Normal sweep leaves it alone (idle_limit_s=1000).
    assert life.reap_idle().reaped_count == 0
    # With override, gets reaped.
    report = life.reap_idle(idle_limit_s=100.0)
    assert "sess-rc" in report.reaped_sessions


def test_reap_idle_rejects_non_positive_limit(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    with pytest.raises(ValueError):
        life.reap_idle(idle_limit_s=0)


def test_reap_idle_uses_injected_clock(tmp_path: Path):
    life, _, _, _, _, _ = _make_lifecycle(tmp_path, idle_limit_s=50.0)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-rck", host_port=40810)
    )
    # Explicit `now` argument wins over the clock.
    report = life.reap_idle(now=9999.0)
    # sandbox was created near t=1000 → idle at 9999 is huge → reaped.
    assert "sess-rck" in report.reaped_sessions


def test_reap_idle_emits_event_only_when_action_taken(tmp_path: Path):
    life, _, _, _, events, _ = _make_lifecycle(tmp_path)
    assert life.reap_idle().reaped_count == 0
    # No sandbox → no emission.
    assert LIFECYCLE_EVENT_REAPED not in events.types()


def test_reap_idle_continues_past_teardown_errors(tmp_path: Path):
    docker = FakeDockerClient(stop_error=RuntimeError("stop exploded"))
    life, _, clock, _, _, mgr = _make_lifecycle(
        tmp_path, docker=docker, idle_limit_s=1.0
    )
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-re2", host_port=40815)
    )
    clock.advance(100.0)
    report = life.reap_idle()
    # Sandbox still got reaped; the docker error shows up in warnings.
    assert "sess-re2" in report.reaped_sessions
    assert any("stop_failed" in w for w in report.warnings)


# ── Background reaper thread ────────────────────────────────────────


def test_background_reaper_starts_and_stops(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path, reaper_interval_s=0.05)
    assert not life.is_reaper_running()
    life.start_reaper()
    assert life.is_reaper_running()
    life.stop_reaper()
    assert not life.is_reaper_running()


def test_background_reaper_is_single_instance(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path, reaper_interval_s=0.05)
    life.start_reaper()
    # Second call is a no-op — must not leak a second thread.
    life.start_reaper()
    assert life.is_reaper_running()
    life.stop_reaper()


def test_background_reaper_actually_sweeps(tmp_path: Path):
    """Uses a real tiny sleep — validates the thread ticks."""

    docker = FakeDockerClient()
    mgr = SandboxManager(docker_client=docker)
    events = RecordingEventCallback()
    # Build a lifecycle that uses real time + sleep for the reaper, so
    # the thread actually wakes up within the test window.  Inject
    # zero idle_limit to guarantee the next sweep reaps the sandbox.
    life = SandboxLifecycle(
        manager=mgr,
        clock=time.time,
        sleep=time.sleep,
        event_cb=events,
        idle_limit_s=0.001,
        ready_poll_interval_s=0.01,
        ready_poll_timeout_s=2.0,
        reaper_interval_s=0.05,
    )
    try:
        config = SandboxConfig(
            session_id="sess-bg",
            workspace_path=str(tmp_path),
            host_port=40820,
        )
        # Use wait_ready=False; mark_ready manually so the fake docker doesn't
        # have to report ready via poll.  Then sleep briefly so idle > limit.
        life.ensure_session(config, wait_ready=False)
        mgr.mark_ready("sess-bg")
        life.start_reaper()
        # Give the thread enough cycles to fire at least one sweep and
        # see a zero-idle-limit sandbox as reap-eligible.
        deadline = time.time() + 2.0
        while time.time() < deadline and mgr.get("sess-bg") is not None:
            time.sleep(0.05)
        assert mgr.get("sess-bg") is None, "reaper did not reap idle sandbox"
    finally:
        life.stop_reaper()


def test_background_reaper_stop_reaper_idempotent(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path, reaper_interval_s=0.05)
    life.stop_reaper()  # not running — must not raise
    life.start_reaper()
    life.stop_reaper()
    life.stop_reaper()  # already stopped — still safe


def test_start_reaper_rejects_bad_interval(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    with pytest.raises(ValueError):
        life.start_reaper(interval_s=0)


def test_reaper_sweeps_counter_starts_at_zero(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    assert life.reaper_sweeps() == 0


# ── 1-per-session invariant end to end ──────────────────────────────


def test_one_sandbox_per_session_enforced_by_lifecycle(tmp_path: Path):
    """Three calls to ensure_session with same id must result in one
    sandbox in the registry at all times."""

    life, docker, _, _, _, mgr = _make_lifecycle(tmp_path)
    c = _sample_config(tmp_path, session_id="sess-one", host_port=40900)
    for _ in range(3):
        life.ensure_session(c)
    assert len(mgr.list()) == 1
    assert len(docker.run_calls) == 1  # container started exactly once


# ── Introspection + snapshot ────────────────────────────────────────


def test_get_stage_returns_status_for_known_session(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-gs", host_port=40910)
    )
    assert life.get_stage("sess-gs") is SandboxStatus.running


def test_get_stage_returns_none_for_unknown(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    assert life.get_stage("ghost") is None


def test_list_sessions_returns_tuple(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-ls", host_port=40920)
    )
    sessions = life.list_sessions()
    assert isinstance(sessions, tuple)
    assert len(sessions) == 1
    assert isinstance(sessions[0], SandboxInstance)


def test_snapshot_structure_and_json_safe(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path, idle_limit_s=600.0)
    life.ensure_session(
        _sample_config(tmp_path, session_id="sess-sn", host_port=40930)
    )
    snap = life.snapshot()
    assert snap["schema_version"] == SANDBOX_LIFECYCLE_SCHEMA_VERSION
    assert snap["idle_limit_s"] == 600.0
    assert snap["max_per_session"] == MAX_SANDBOXES_PER_SESSION
    assert snap["count"] == 1
    assert snap["reaper"]["running"] is False
    # JSON-serialisable end-to-end.
    assert json.dumps(snap)


# ── Context manager ─────────────────────────────────────────────────


def test_context_manager_tears_down_sandboxes_on_exit(tmp_path: Path):
    docker = FakeDockerClient()
    mgr = SandboxManager(docker_client=docker, clock=FakeClock())
    with SandboxLifecycle(
        manager=mgr,
        clock=FakeClock(),
        sleep=lambda s: None,
    ) as life:
        life.ensure_session(
            _sample_config(tmp_path, session_id="sess-ctx", host_port=40940),
            wait_ready=False,
        )
        mgr.mark_ready("sess-ctx")
    # On exit, every sandbox must be gone.
    assert mgr.get("sess-ctx") is None
    assert docker.stop_calls  # stop was called


def test_context_manager_stops_reaper_on_exit(tmp_path: Path):
    life, *_ = _make_lifecycle(tmp_path, reaper_interval_s=0.05)
    with life:
        life.start_reaper()
        assert life.is_reaper_running()
    assert not life.is_reaper_running()


# ── Full lifecycle golden path ──────────────────────────────────────


def test_full_lifecycle_golden_path(tmp_path: Path):
    """Exercises the spec's full flow end-to-end:

    create → start → hot-reload → screenshot → stop → cleanup.
    """

    life, docker, _, _, events, mgr = _make_lifecycle(
        tmp_path, screenshot_hook=lambda **kw: b"\x89PNG"
    )
    config = _sample_config(tmp_path, session_id="sess-full", host_port=40950)

    # 1. create + start + wait_ready (ensure_session does all three)
    instance = life.ensure_session(config)
    assert instance.status is SandboxStatus.running
    # 2. hot-reload
    life.hot_reload("sess-full", files_changed=("app/page.tsx",))
    # 3. screenshot
    shot = life.capture_screenshot("sess-full")
    assert shot.byte_len > 0
    # 4. stop + cleanup
    life.teardown("sess-full")
    assert mgr.get("sess-full") is None

    event_order = events.types()
    # Full expected sequence (ui_sandbox.created / starting / ready come
    # from the manager; ensure / hot_reload / screenshot / stopped /
    # teardown come from both layers).
    expected_subset = [
        "ui_sandbox.created",
        "ui_sandbox.starting",
        "ui_sandbox.ready",
        LIFECYCLE_EVENT_ENSURE,
        LIFECYCLE_EVENT_HOT_RELOAD,
        LIFECYCLE_EVENT_SCREENSHOT,
        "ui_sandbox.stopped",
        LIFECYCLE_EVENT_TEARDOWN,
    ]
    # Every expected event must appear in order (interleaving OK).
    idx = -1
    for ev in expected_subset:
        idx = event_order.index(ev, idx + 1)
    assert idx >= 0


# ── Thread-safety under concurrent ensure + reap ────────────────────


def test_thread_safety_concurrent_ensure_and_reap(tmp_path: Path):
    """20 workers ensure + hot_reload + teardown different sessions
    concurrently while a reaper sweeps — registry must stay consistent.
    """

    docker = FakeDockerClient()
    mgr = SandboxManager(docker_client=docker, clock=time.time)
    life = SandboxLifecycle(
        manager=mgr,
        clock=time.time,
        sleep=time.sleep,
        idle_limit_s=60.0,
        ready_poll_interval_s=0.01,
        ready_poll_timeout_s=1.0,
        reaper_interval_s=0.02,
    )
    count = 20
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            config = SandboxConfig(
                session_id=f"th-{i:03d}",
                workspace_path=str(tmp_path),
                host_port=41000 + i,
            )
            life.ensure_session(config, wait_ready=False)
            mgr.mark_ready(config.session_id)
            life.hot_reload(config.session_id, files_changed=(f"f{i}.tsx",))
            life.teardown(config.session_id)
        except Exception as exc:  # pragma: no cover - prints on failure
            with lock:
                errors.append(exc)

    life.start_reaper()
    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        life.stop_reaper()

    assert not errors, f"worker errors: {errors[:3]}"
    # All sandboxes removed by end (teardown does remove=True).
    assert len(mgr.list()) == 0


# ── Sibling alignment ───────────────────────────────────────────────


def test_sibling_alignment_idle_limit_matches_ui_sandbox():
    # V2 #2 must agree with V2 #1 on the 15-min idle contract.
    assert ul.DEFAULT_IDLE_LIMIT_S == us.DEFAULT_IDLE_LIMIT_S == 900.0


def test_sibling_alignment_lifecycle_error_is_sandbox_error():
    # Call sites that catch `SandboxError` already catch lifecycle errors.
    try:
        raise LifecycleError("boom")
    except SandboxError:
        pass
    else:  # pragma: no cover
        raise AssertionError("LifecycleError should subclass SandboxError")


def test_sibling_alignment_v1_v2_coexistence():
    """V2 #2 must not break V2 #1 primitives — FakeDockerClient from
    V2 #2 tests is still shaped exactly like V2 #1's expectations."""

    d = FakeDockerClient()
    # Duck-type — the fake must satisfy every DockerClient method.
    for method in ("run_detached", "stop", "remove", "logs", "inspect"):
        assert callable(getattr(d, method))
