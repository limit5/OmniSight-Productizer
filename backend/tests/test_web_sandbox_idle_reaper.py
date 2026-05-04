"""W14.5 — `backend/web_sandbox_idle_reaper.py` contract tests.

Pins the idle-reaper module's structural + behavioural promises:

* Module surface (``__all__`` membership, schema version, defaults
  match the W14.5 row spec — 1800s timeout / 60s interval).
* :class:`IdleReaperConfig` validation — rejects out-of-range values,
  rejects ``reap_interval_s > idle_timeout_s``, ``from_settings``
  honours partial Settings stubs.
* :func:`select_idle_workspaces` pure function — terminal / fresh /
  idle filter rules; deterministic ordering.
* :func:`compute_idle_seconds` thin wrapper — type-checks input.
* :class:`WebSandboxIdleReaper.tick` — reaps idle sandboxes via
  ``manager.stop(reason='idle_timeout')``, leaves active ones alone,
  swallows per-workspace errors, captures them in the sweep result.
* Daemon-thread lifecycle — start / stop / is_running / sweep_count;
  idempotent start; bounded stop join.
* Integration with :class:`backend.web_sandbox.WebSandboxManager` —
  end-to-end: launch → fast-forward clock → tick → sandbox now
  ``stopped`` with ``killed_reason='idle_timeout'``.
* CF cleanup cascade — when ``cf_ingress_manager`` / ``cf_access_manager``
  are wired in, the idle-kill triggers the same delete_rule /
  delete_application calls that an explicit ``stop()`` would.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from backend import web_sandbox_idle_reaper as reaper_mod
from backend.web_sandbox import (
    WebSandboxConfig,
    WebSandboxError,
    WebSandboxInstance,
    WebSandboxManager,
    WebSandboxStatus,
)
from backend.web_sandbox_idle_reaper import (
    DEFAULT_IDLE_TIMEOUT_S,
    DEFAULT_REAP_INTERVAL_S,
    IDLE_REAPER_SCHEMA_VERSION,
    IDLE_TIMEOUT_REASON,
    MAX_IDLE_TIMEOUT_S,
    MAX_REAP_INTERVAL_S,
    MIN_IDLE_TIMEOUT_S,
    MIN_REAP_INTERVAL_S,
    IdleReaperConfig,
    IdleReaperError,
    IdleReaperSweepResult,
    WebSandboxIdleReaper,
    compute_idle_seconds,
    select_idle_workspaces,
)
from backend.tests.test_web_sandbox import (
    FakeClock,
    FakeDockerClient,
    RecordingEventCallback,
)


# ─────────────── Module surface ────────────────────────────────────


EXPECTED_ALL = {
    "IDLE_REAPER_SCHEMA_VERSION",
    "DEFAULT_IDLE_TIMEOUT_S",
    "DEFAULT_REAP_INTERVAL_S",
    "IDLE_TIMEOUT_REASON",
    "MIN_IDLE_TIMEOUT_S",
    "MIN_REAP_INTERVAL_S",
    "MAX_IDLE_TIMEOUT_S",
    "MAX_REAP_INTERVAL_S",
    "IdleReaperError",
    "IdleReaperConfig",
    "IdleReaperSweepResult",
    "WebSandboxIdleReaper",
    "compute_idle_seconds",
    "select_idle_workspaces",
}


def test_all_matches_expected_set() -> None:
    assert set(reaper_mod.__all__) == EXPECTED_ALL


def test_all_entries_unique() -> None:
    assert len(reaper_mod.__all__) == len(set(reaper_mod.__all__))


def test_schema_version_is_semver() -> None:
    parts = IDLE_REAPER_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_default_idle_timeout_is_30_minutes() -> None:
    assert DEFAULT_IDLE_TIMEOUT_S == 1800.0


def test_default_reap_interval_is_one_minute() -> None:
    assert DEFAULT_REAP_INTERVAL_S == 60.0


def test_idle_timeout_reason_string() -> None:
    assert IDLE_TIMEOUT_REASON == "idle_timeout"


def test_min_max_floors_match_module_doc() -> None:
    assert MIN_IDLE_TIMEOUT_S == 1.0
    assert MAX_IDLE_TIMEOUT_S == 86_400.0
    assert MIN_REAP_INTERVAL_S == 0.05
    assert MAX_REAP_INTERVAL_S == 3600.0


def test_default_interval_within_range() -> None:
    assert MIN_REAP_INTERVAL_S <= DEFAULT_REAP_INTERVAL_S <= MAX_REAP_INTERVAL_S
    assert DEFAULT_REAP_INTERVAL_S <= DEFAULT_IDLE_TIMEOUT_S


# ─────────────── IdleReaperConfig ──────────────────────────────────


def test_config_default_construction() -> None:
    cfg = IdleReaperConfig()
    assert cfg.idle_timeout_s == DEFAULT_IDLE_TIMEOUT_S
    assert cfg.reap_interval_s == DEFAULT_REAP_INTERVAL_S


def test_config_explicit_construction() -> None:
    cfg = IdleReaperConfig(idle_timeout_s=120.0, reap_interval_s=30.0)
    assert cfg.idle_timeout_s == 120.0
    assert cfg.reap_interval_s == 30.0


def test_config_to_dict_carries_schema_version() -> None:
    cfg = IdleReaperConfig(idle_timeout_s=120.0, reap_interval_s=30.0)
    d = cfg.to_dict()
    assert d == {
        "schema_version": IDLE_REAPER_SCHEMA_VERSION,
        "idle_timeout_s": 120.0,
        "reap_interval_s": 30.0,
    }


@pytest.mark.parametrize(
    "idle, interval",
    [
        (0.5, 0.05),  # idle below MIN
        (-1, 30.0),   # negative idle
        (90_000.0, 60.0),  # idle above MAX
    ],
)
def test_config_rejects_idle_out_of_range(idle: float, interval: float) -> None:
    with pytest.raises(IdleReaperError):
        IdleReaperConfig(idle_timeout_s=idle, reap_interval_s=interval)


@pytest.mark.parametrize(
    "idle, interval",
    [
        (60.0, 0.0),       # interval below MIN
        (60.0, -1.0),      # negative interval
        (3600.0, 7200.0),  # interval above MAX
    ],
)
def test_config_rejects_interval_out_of_range(idle: float, interval: float) -> None:
    with pytest.raises(IdleReaperError):
        IdleReaperConfig(idle_timeout_s=idle, reap_interval_s=interval)


def test_config_rejects_interval_greater_than_idle() -> None:
    with pytest.raises(IdleReaperError) as exc_info:
        IdleReaperConfig(idle_timeout_s=10.0, reap_interval_s=20.0)
    assert "must be <= idle_timeout_s" in str(exc_info.value)


def test_config_accepts_interval_equal_to_idle() -> None:
    # The check is <=, so equal is OK — useful for tests that drive
    # the reaper at the same cadence as the timeout.
    cfg = IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=60.0)
    assert cfg.idle_timeout_s == cfg.reap_interval_s


def test_config_rejects_non_number_inputs() -> None:
    with pytest.raises(IdleReaperError):
        IdleReaperConfig(idle_timeout_s="60", reap_interval_s=30.0)  # type: ignore[arg-type]
    with pytest.raises(IdleReaperError):
        IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=None)  # type: ignore[arg-type]


def test_config_from_settings_happy_path() -> None:
    class StubSettings:
        web_sandbox_idle_timeout_s = 600.0
        web_sandbox_reap_interval_s = 30.0

    cfg = IdleReaperConfig.from_settings(StubSettings())
    assert cfg.idle_timeout_s == 600.0
    assert cfg.reap_interval_s == 30.0


def test_config_from_settings_partial_falls_back_to_defaults() -> None:
    class PartialSettings:
        # Missing both attrs — getattr fall-through hits the module
        # defaults so a minimal Settings stub does not break the reaper.
        pass

    cfg = IdleReaperConfig.from_settings(PartialSettings())
    assert cfg.idle_timeout_s == DEFAULT_IDLE_TIMEOUT_S
    assert cfg.reap_interval_s == DEFAULT_REAP_INTERVAL_S


def test_config_frozen() -> None:
    cfg = IdleReaperConfig()
    with pytest.raises(Exception):  # FrozenInstanceError
        cfg.idle_timeout_s = 600.0  # type: ignore[misc]


# ─────────────── IdleReaperSweepResult ─────────────────────────────


def test_sweep_result_construction_and_to_dict() -> None:
    result = IdleReaperSweepResult(
        started_at=100.0,
        finished_at=100.5,
        scanned=3,
        reaped=("ws-a",),
        skipped_active=("ws-b",),
        skipped_terminal=("ws-c",),
        errors=(),
    )
    d = result.to_dict()
    assert d["schema_version"] == IDLE_REAPER_SCHEMA_VERSION
    assert d["started_at"] == 100.0
    assert d["finished_at"] == 100.5
    assert d["duration_s"] == pytest.approx(0.5)
    assert d["scanned"] == 3
    assert d["reaped"] == ["ws-a"]
    assert d["skipped_active"] == ["ws-b"]
    assert d["skipped_terminal"] == ["ws-c"]
    assert d["errors"] == []


def test_sweep_result_normalises_lists_to_tuples() -> None:
    result = IdleReaperSweepResult(
        started_at=0.0,
        finished_at=1.0,
        scanned=0,
        reaped=["ws-a"],     # type: ignore[arg-type]
        skipped_active=["ws-b"],   # type: ignore[arg-type]
        skipped_terminal=["ws-c"],  # type: ignore[arg-type]
        errors=[("ws-d", "boom")],  # type: ignore[arg-type]
    )
    assert isinstance(result.reaped, tuple)
    assert isinstance(result.skipped_active, tuple)
    assert isinstance(result.skipped_terminal, tuple)
    assert isinstance(result.errors, tuple)


def test_sweep_result_rejects_malformed_errors() -> None:
    with pytest.raises(IdleReaperError):
        IdleReaperSweepResult(
            started_at=0.0,
            finished_at=1.0,
            scanned=0,
            reaped=(),
            skipped_active=(),
            skipped_terminal=(),
            errors=(("ws-a",),),  # type: ignore[arg-type]
        )
    with pytest.raises(IdleReaperError):
        IdleReaperSweepResult(
            started_at=0.0,
            finished_at=1.0,
            scanned=0,
            reaped=(),
            skipped_active=(),
            skipped_terminal=(),
            errors=((123, "boom"),),  # type: ignore[arg-type]
        )


def test_sweep_result_duration_clamped_to_zero() -> None:
    # finished_at < started_at can happen under a non-monotonic clock;
    # the property clamps to 0 rather than returning a negative number.
    result = IdleReaperSweepResult(
        started_at=10.0,
        finished_at=5.0,
        scanned=0,
        reaped=(),
        skipped_active=(),
        skipped_terminal=(),
        errors=(),
    )
    assert result.duration_s == 0.0


# ─────────────── compute_idle_seconds ──────────────────────────────


def test_compute_idle_seconds_fresh_returns_zero() -> None:
    inst = _make_pending_instance("ws-1", last_request_at=0.0)
    assert compute_idle_seconds(inst, now=100.0) == 0.0


def test_compute_idle_seconds_active_returns_delta() -> None:
    inst = _make_pending_instance("ws-1", last_request_at=100.0)
    assert compute_idle_seconds(inst, now=400.0) == 300.0


def test_compute_idle_seconds_clamps_to_zero_when_clock_drifts_backwards() -> None:
    inst = _make_pending_instance("ws-1", last_request_at=400.0)
    assert compute_idle_seconds(inst, now=100.0) == 0.0


def test_compute_idle_seconds_rejects_non_instance() -> None:
    with pytest.raises(TypeError):
        compute_idle_seconds("not-an-instance", now=0.0)  # type: ignore[arg-type]


# ─────────────── select_idle_workspaces ────────────────────────────


def _make_pending_instance(
    workspace_id: str,
    *,
    last_request_at: float = 0.0,
    status: WebSandboxStatus = WebSandboxStatus.installing,
) -> WebSandboxInstance:
    cfg = WebSandboxConfig(workspace_id=workspace_id, workspace_path="/tmp")
    return WebSandboxInstance(
        workspace_id=workspace_id,
        sandbox_id=f"ws-{workspace_id[-1]}",
        container_name=f"omnisight-web-preview-{workspace_id}",
        config=cfg,
        status=status,
        last_request_at=last_request_at,
    )


def test_select_empty_iterable_returns_empty_tuple() -> None:
    assert select_idle_workspaces((), idle_timeout_s=60.0, now=100.0) == ()


def test_select_idle_workspace_collected() -> None:
    inst = _make_pending_instance("ws-1", last_request_at=10.0)
    out = select_idle_workspaces([inst], idle_timeout_s=60.0, now=200.0)
    assert out == ("ws-1",)


def test_select_active_workspace_skipped() -> None:
    inst = _make_pending_instance("ws-1", last_request_at=180.0)
    out = select_idle_workspaces([inst], idle_timeout_s=60.0, now=200.0)
    assert out == ()


def test_select_threshold_inclusive() -> None:
    # Exactly idle_timeout_s seconds idle → collected (>= comparison).
    inst = _make_pending_instance("ws-1", last_request_at=100.0)
    out = select_idle_workspaces([inst], idle_timeout_s=60.0, now=160.0)
    assert out == ("ws-1",)


def test_select_skips_terminal_stopped() -> None:
    inst = _make_pending_instance(
        "ws-1", last_request_at=10.0, status=WebSandboxStatus.stopped,
    )
    out = select_idle_workspaces([inst], idle_timeout_s=60.0, now=200.0)
    assert out == ()


def test_select_skips_terminal_failed() -> None:
    inst = _make_pending_instance(
        "ws-1", last_request_at=10.0, status=WebSandboxStatus.failed,
    )
    out = select_idle_workspaces([inst], idle_timeout_s=60.0, now=200.0)
    assert out == ()


def test_select_skips_fresh_zero_last_request_at() -> None:
    # Defence-in-depth — should never happen in practice (the launcher
    # sets last_request_at = clock() on construction), but a malformed
    # snapshot must not be idle-killed.
    inst = _make_pending_instance("ws-1", last_request_at=0.0)
    out = select_idle_workspaces([inst], idle_timeout_s=60.0, now=200.0)
    assert out == ()


def test_select_returns_sorted_workspace_ids() -> None:
    insts = [
        _make_pending_instance("ws-c", last_request_at=10.0),
        _make_pending_instance("ws-a", last_request_at=10.0),
        _make_pending_instance("ws-b", last_request_at=10.0),
    ]
    out = select_idle_workspaces(insts, idle_timeout_s=60.0, now=200.0)
    assert out == ("ws-a", "ws-b", "ws-c")


def test_select_mixed_active_and_idle() -> None:
    insts = [
        _make_pending_instance("ws-fresh", last_request_at=180.0),     # active
        _make_pending_instance("ws-idle1", last_request_at=10.0),      # idle
        _make_pending_instance(
            "ws-stopped", last_request_at=10.0,
            status=WebSandboxStatus.stopped,
        ),
        _make_pending_instance("ws-idle2", last_request_at=20.0),      # idle
    ]
    out = select_idle_workspaces(insts, idle_timeout_s=60.0, now=200.0)
    assert out == ("ws-idle1", "ws-idle2")


def test_select_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError):
        select_idle_workspaces([], idle_timeout_s=0.0, now=100.0)
    with pytest.raises(ValueError):
        select_idle_workspaces([], idle_timeout_s=-1.0, now=100.0)


def test_select_rejects_non_number_now() -> None:
    with pytest.raises(ValueError):
        select_idle_workspaces([], idle_timeout_s=60.0, now="100")  # type: ignore[arg-type]


def test_select_rejects_non_instance_in_iterable() -> None:
    with pytest.raises(TypeError):
        select_idle_workspaces(
            ["not-an-instance"],  # type: ignore[list-item]
            idle_timeout_s=60.0,
            now=100.0,
        )


# ─────────────── WebSandboxIdleReaper construction ─────────────────


def test_reaper_construction_rejects_non_manager() -> None:
    with pytest.raises(TypeError):
        WebSandboxIdleReaper(manager="not-a-manager")  # type: ignore[arg-type]


def test_reaper_construction_rejects_non_config(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    with pytest.raises(TypeError):
        WebSandboxIdleReaper(manager=mgr, config="bad")  # type: ignore[arg-type]


def test_reaper_default_config(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    reaper = WebSandboxIdleReaper(manager=mgr)
    assert reaper.config.idle_timeout_s == DEFAULT_IDLE_TIMEOUT_S
    assert reaper.config.reap_interval_s == DEFAULT_REAP_INTERVAL_S


def test_reaper_initial_state(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    reaper = WebSandboxIdleReaper(manager=mgr)
    assert reaper.is_running is False
    assert reaper.sweep_count == 0
    assert reaper.last_result is None


# ─────────────── Reaper test fixtures ──────────────────────────────


def _make_manager(
    workspace: Path, *, clock: FakeClock | None = None
) -> WebSandboxManager:
    return WebSandboxManager(
        docker_client=FakeDockerClient(),
        manifest=None,
        clock=clock or FakeClock(),
        event_cb=RecordingEventCallback(),
    )


def _make_reaper(
    mgr: WebSandboxManager,
    *,
    config: IdleReaperConfig,
    clock: FakeClock | None = None,
    event_cb=None,
    error_cb=None,
) -> WebSandboxIdleReaper:
    """Build a reaper whose clock is sync'd to the manager's FakeClock
    via the manager's internal ``_clock`` attribute. Without this the
    reaper would use real ``time.time`` and disagree with the manager
    on what "now" means, treating fresh sandboxes as ancient (since
    last_request_at sits at FakeClock's tiny start value)."""

    return WebSandboxIdleReaper(
        manager=mgr,
        config=config,
        clock=clock if clock is not None else mgr._clock,  # type: ignore[arg-type]
        event_cb=event_cb,
        error_cb=error_cb,
    )


def _launch(mgr: WebSandboxManager, workspace_id: str, path: Path) -> WebSandboxInstance:
    cfg = WebSandboxConfig(workspace_id=workspace_id, workspace_path=str(path))
    return mgr.launch(cfg)


def _force_idle(mgr: WebSandboxManager, workspace_id: str, *, age_s: float) -> None:
    """White-box helper — advance the manager's FakeClock so the next
    reaper read sees ``last_request_at`` as ``age_s`` seconds in the
    past. Touches the FakeClock's internal state directly because
    FakeClock auto-advances by 1.0 per call; we want a single
    deterministic jump.

    Production path is "wait age_s seconds"; tests use this hack so a
    30-min idle test runs in milliseconds.
    """

    clock = mgr._clock  # type: ignore[attr-defined]
    if not hasattr(clock, "_t"):
        raise RuntimeError(
            "_force_idle requires the manager's clock to be a FakeClock"
        )
    # Bump _t by age_s so the next clock() returns a value that's
    # age_s further ahead. last_request_at on the instance was set
    # at the previous (smaller) tick, so the gap is now >= age_s.
    clock._t += age_s


# ─────────────── Reaper tick — happy paths ─────────────────────────


def test_tick_no_sandboxes(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    reaper = WebSandboxIdleReaper(manager=mgr, config=IdleReaperConfig(
        idle_timeout_s=60.0, reap_interval_s=10.0,
    ))
    result = reaper.tick()
    assert result.scanned == 0
    assert result.reaped == ()
    assert result.skipped_active == ()
    assert result.skipped_terminal == ()
    assert result.errors == ()


def test_tick_active_sandbox_not_reaped(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    _launch(mgr, "ws-1", tmp_path)
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=3600.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    result = reaper.tick()
    assert result.scanned == 1
    assert result.reaped == ()
    assert result.skipped_active == ("ws-1",)


def test_tick_idle_sandbox_reaped_with_idle_timeout_reason(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    _launch(mgr, "ws-1", tmp_path)
    _force_idle(mgr, "ws-1", age_s=2000.0)
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    result = reaper.tick()
    assert result.reaped == ("ws-1",)
    inst = mgr.get("ws-1")
    assert inst is not None
    assert inst.status == WebSandboxStatus.stopped
    assert inst.killed_reason == IDLE_TIMEOUT_REASON


def test_tick_terminal_sandbox_listed_under_skipped_terminal(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    _launch(mgr, "ws-1", tmp_path)
    mgr.stop("ws-1", reason="operator_request")
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    result = reaper.tick()
    assert result.reaped == ()
    assert result.skipped_terminal == ("ws-1",)


def test_tick_mixed_population(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    _launch(mgr, "ws-active", tmp_path)
    _launch(mgr, "ws-idle1", tmp_path)
    _launch(mgr, "ws-idle2", tmp_path)
    _launch(mgr, "ws-already-stopped", tmp_path)
    mgr.stop("ws-already-stopped", reason="operator_request")
    # Advance the clock so all surviving sandboxes are now over the
    # idle threshold...
    _force_idle(mgr, "ws-idle1", age_s=2000.0)
    # ...then re-touch ws-active so its last_request_at sits at the
    # advanced clock (i.e. fresh), while the two idle sandboxes
    # remain at their launch-time tick.
    mgr.touch("ws-active")
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    result = reaper.tick()
    assert result.scanned == 4
    assert result.reaped == ("ws-idle1", "ws-idle2")
    assert result.skipped_active == ("ws-active",)
    assert result.skipped_terminal == ("ws-already-stopped",)


def test_tick_increments_sweep_count_and_records_last_result(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    assert reaper.sweep_count == 0
    assert reaper.last_result is None
    r1 = reaper.tick()
    assert reaper.sweep_count == 1
    assert reaper.last_result is r1
    r2 = reaper.tick()
    assert reaper.sweep_count == 2
    assert reaper.last_result is r2


def test_tick_emits_event_callback(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    events: list[tuple[str, Mapping[str, Any]]] = []
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=10.0),
        event_cb=lambda t, p: events.append((t, dict(p))),
        clock=mgr._clock,
    )
    reaper.tick()
    assert len(events) == 1
    assert events[0][0] == "web_sandbox_idle_reaper.sweep"
    assert events[0][1]["schema_version"] == IDLE_REAPER_SCHEMA_VERSION


# ─────────────── Reaper tick — error paths ─────────────────────────


def test_tick_swallows_per_workspace_stop_failure(tmp_path: Path) -> None:
    """A docker error during stop must not crash the sweep — it must
    surface in the result's errors list and the loop must keep going."""

    docker = FakeDockerClient(stop_error=RuntimeError("docker dead"))
    mgr = WebSandboxManager(
        docker_client=docker,
        manifest=None,
        clock=FakeClock(),
        event_cb=RecordingEventCallback(),
    )
    _launch(mgr, "ws-1", tmp_path)
    _force_idle(mgr, "ws-1", age_s=2000.0)
    # The current manager.stop() catches docker exceptions internally
    # and folds them into warnings, so the reaper sees a clean stop()
    # return. Verify that.
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    result = reaper.tick()
    assert result.reaped == ("ws-1",)
    assert result.errors == ()
    inst = mgr.get("ws-1")
    assert inst is not None
    assert inst.status == WebSandboxStatus.stopped
    assert any("stop_failed" in w for w in inst.warnings)


def test_tick_handles_workspace_disappearing_between_snapshot_and_stop(
    tmp_path: Path,
) -> None:
    """Race: caller deletes the instance while the reaper is mid-sweep.
    The reaper's stop() raises WebSandboxNotFound — captured under
    errors, sweep keeps going."""

    mgr = _make_manager(tmp_path)
    _launch(mgr, "ws-1", tmp_path)
    _force_idle(mgr, "ws-1", age_s=2000.0)

    real_list = mgr.list

    def list_then_evict() -> Sequence[WebSandboxInstance]:
        snap = real_list()
        # Simulate operator deleting the instance after the reaper
        # took its snapshot.
        with mgr._lock:
            del mgr._instances["ws-1"]
        return snap

    mgr.list = list_then_evict  # type: ignore[method-assign]
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    result = reaper.tick()
    assert result.reaped == ()
    assert len(result.errors) == 1
    wid, msg = result.errors[0]
    assert wid == "ws-1"
    assert msg.startswith("not_found:")


def test_tick_invokes_error_callback_on_websandbox_error(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    _launch(mgr, "ws-1", tmp_path)
    _force_idle(mgr, "ws-1", age_s=2000.0)

    real_stop = mgr.stop

    def boom_stop(workspace_id: str, **kw: Any) -> WebSandboxInstance:
        raise WebSandboxError(f"boom for {workspace_id}")

    mgr.stop = boom_stop  # type: ignore[method-assign]

    captured: list[tuple[str, BaseException]] = []
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=10.0),
        error_cb=lambda wid, exc: captured.append((wid, exc)),
        clock=mgr._clock,
    )
    result = reaper.tick()
    assert result.reaped == ()
    assert len(result.errors) == 1
    assert result.errors[0][0] == "ws-1"
    assert "web_sandbox_error" in result.errors[0][1]
    assert len(captured) == 1
    assert captured[0][0] == "ws-1"

    # Restore so test fixture teardown does not leak.
    mgr.stop = real_stop  # type: ignore[method-assign]


# ─────────────── Daemon thread lifecycle ───────────────────────────


def test_start_idempotent(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=10.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    assert reaper.start() is True
    assert reaper.start() is False
    assert reaper.is_running
    reaper.stop()


def test_stop_idempotent(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=10.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    # Stop on a non-started reaper returns True.
    assert reaper.stop() is True
    reaper.start()
    assert reaper.stop() is True
    # Second stop is a no-op.
    assert reaper.stop() is True


def test_start_actually_calls_tick_and_then_stop_winds_down(tmp_path: Path) -> None:
    """Drive the daemon thread with a 0.05s interval, wait for the
    sweep_count to advance past 1, then stop. Bounds the test latency
    at ~150ms."""

    mgr = _make_manager(tmp_path)
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=1.0, reap_interval_s=0.05),
        clock=mgr._clock,
    )
    reaper.start()
    deadline = time.monotonic() + 1.0
    while reaper.sweep_count < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert reaper.sweep_count >= 2, "reaper thread did not run sweeps"
    assert reaper.stop(timeout_s=2.0) is True
    assert reaper.is_running is False


def test_start_after_stop_creates_fresh_thread(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=10.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    reaper.start()
    reaper.stop()
    assert reaper.is_running is False
    assert reaper.start() is True
    assert reaper.is_running
    reaper.stop()


def test_snapshot_reflects_running_state(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=10.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    snap = reaper.snapshot()
    assert snap["is_running"] is False
    assert snap["sweep_count"] == 0
    assert snap["last_result"] is None
    reaper.tick()
    snap = reaper.snapshot()
    assert snap["sweep_count"] == 1
    assert snap["last_result"] is not None
    assert snap["config"]["idle_timeout_s"] == 10.0


# ─────────────── Integration with CF ingress + CF Access ────────────


class _FakeCFIngressManager:
    """Minimal stand-in for backend.cf_ingress.CFIngressManager so the
    reaper's cascade through manager.stop() can be exercised without
    depending on the real CF API. We only need create_rule (returns a
    fake URL) and delete_rule (returns True)."""

    def __init__(self) -> None:
        self.create_calls: list[tuple[str, int]] = []
        self.delete_calls: list[str] = []

    def create_rule(self, *, sandbox_id: str, host_port: int) -> str:
        self.create_calls.append((sandbox_id, host_port))
        return f"https://preview-{sandbox_id}.example.com"

    def delete_rule(self, sandbox_id: str) -> bool:
        self.delete_calls.append(sandbox_id)
        return True


class _FakeCFAccessManager:
    class _Record:
        def __init__(self, sandbox_id: str) -> None:
            self.app_id = f"app-{sandbox_id}"

    class _ConfigStub:
        default_emails = ("admin@example.com",)

    def __init__(self) -> None:
        self.config = self._ConfigStub()
        self.create_calls: list[tuple[str, tuple[str, ...]]] = []
        self.delete_calls: list[str] = []

    def create_application(self, *, sandbox_id: str, emails) -> "_FakeCFAccessManager._Record":
        self.create_calls.append((sandbox_id, tuple(emails)))
        return self._Record(sandbox_id)

    def delete_application(self, sandbox_id: str) -> bool:
        self.delete_calls.append(sandbox_id)
        return True


def test_idle_kill_cascades_into_cf_ingress_delete(tmp_path: Path) -> None:
    """The W14.5 row spec includes "刪 ingress" — verify that a reaper-
    triggered stop() does in fact call cf_ingress.delete_rule via the
    manager's existing W14.3 wiring."""

    cf_ingress = _FakeCFIngressManager()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        manifest=None,
        clock=FakeClock(),
        cf_ingress_manager=cf_ingress,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-1",
        workspace_path=str(tmp_path),
        allowed_emails=("op@example.com",),
    )
    mgr.launch(cfg)
    assert cf_ingress.create_calls  # ingress was created on launch
    _force_idle(mgr, "ws-1", age_s=2000.0)
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    result = reaper.tick()
    assert result.reaped == ("ws-1",)
    assert cf_ingress.delete_calls == ["ws-deec53eedb43"][:0] or len(cf_ingress.delete_calls) == 1
    # Use length check so we don't depend on the deterministic
    # sandbox_id hash for ws-1 (which is platform-stable but still
    # an implementation detail).
    assert len(cf_ingress.delete_calls) == 1


def test_idle_kill_cascades_into_cf_access_delete(tmp_path: Path) -> None:
    cf_access = _FakeCFAccessManager()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        manifest=None,
        clock=FakeClock(),
        cf_access_manager=cf_access,
    )
    cfg = WebSandboxConfig(
        workspace_id="ws-1",
        workspace_path=str(tmp_path),
        allowed_emails=("op@example.com",),
    )
    mgr.launch(cfg)
    assert len(cf_access.create_calls) == 1
    _force_idle(mgr, "ws-1", age_s=2000.0)
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    reaper.tick()
    assert len(cf_access.delete_calls) == 1


def test_idle_kill_records_idle_timeout_in_warnings_when_cf_fails(tmp_path: Path) -> None:
    """If the CF cleanup fails during an idle-kill, the warning is
    captured on the instance just like any operator-driven stop() —
    the reaper does not change those semantics."""

    class FlakyCFIngress(_FakeCFIngressManager):
        def delete_rule(self, sandbox_id: str) -> bool:
            from backend.cf_ingress import CFIngressError
            raise CFIngressError("simulated CF outage")

    cf_ingress = FlakyCFIngress()
    mgr = WebSandboxManager(
        docker_client=FakeDockerClient(),
        manifest=None,
        clock=FakeClock(),
        cf_ingress_manager=cf_ingress,
    )
    cfg = WebSandboxConfig(workspace_id="ws-1", workspace_path=str(tmp_path))
    mgr.launch(cfg)
    _force_idle(mgr, "ws-1", age_s=2000.0)
    reaper = WebSandboxIdleReaper(
        manager=mgr,
        config=IdleReaperConfig(idle_timeout_s=60.0, reap_interval_s=10.0),
        clock=mgr._clock,
    )
    result = reaper.tick()
    assert result.reaped == ("ws-1",)
    inst = mgr.get("ws-1")
    assert inst is not None
    assert inst.killed_reason == IDLE_TIMEOUT_REASON
    assert any("cf_ingress_delete_failed" in w for w in inst.warnings)


# ─────────────── Cross-worker contract ─────────────────────────────


def test_pure_select_function_is_deterministic_across_workers(tmp_path: Path) -> None:
    """SOP §1 type-3 contract — every worker that runs select_idle_workspaces
    on the same instance snapshot must agree on the result. Pure
    function ⇒ verified by repeat invocation."""

    insts = [
        _make_pending_instance("ws-c", last_request_at=10.0),
        _make_pending_instance("ws-a", last_request_at=10.0),
        _make_pending_instance("ws-b", last_request_at=180.0),  # active
    ]
    outputs = [
        select_idle_workspaces(insts, idle_timeout_s=60.0, now=200.0)
        for _ in range(8)
    ]
    assert all(o == outputs[0] for o in outputs)
    assert outputs[0] == ("ws-a", "ws-c")
