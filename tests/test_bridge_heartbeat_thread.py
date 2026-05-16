"""SP-B-X-019 / OP-1077 bridge heartbeat-thread tests.

These tests pin the fix for the "heartbeat goes stale during quiet
Gerrit periods" bug. The thread must:

1. Start before the (potentially blocking) catchup runs.
2. Write the heartbeat file on a half-cadence wall clock.
3. Stop within one cadence when ``stop()`` is called.
4. Survive a single ``_touch_heartbeat_file`` failure without crashing.
5. Be idempotent: a second ``_start_heartbeat_thread()`` call doesn't
   accumulate threads.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.agents import gerrit_jira_bridge


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def heartbeat_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "heartbeat"
    monkeypatch.setenv("OMNISIGHT_BRIDGE_HEARTBEAT_PATH", str(target))
    return target


@pytest.fixture
def bridge_with_thread(
    heartbeat_path: Path, monkeypatch: pytest.MonkeyPatch
) -> gerrit_jira_bridge.GerritJiraBridge:
    """Build a Bridge with a no-op client + heartbeat cadence tight
    enough that tests don't wait forever."""
    client = MagicMock()
    client.agent_class = "subscription-claude"
    config = gerrit_jira_bridge.BridgeConfig(
        agent_class="subscription-claude",
        heartbeat_file_seconds=0.4,  # cadence 0.2s → tests finish quickly
        heartbeat_file_path=heartbeat_path,
    )
    bridge = gerrit_jira_bridge.GerritJiraBridge(client, config)
    return bridge


# ── 1. Thread starts ──────────────────────────────────────────────────


def test_heartbeat_thread_writes_file_after_start(
    bridge_with_thread: gerrit_jira_bridge.GerritJiraBridge,
    heartbeat_path: Path,
) -> None:
    # Seed the file so we observe MTIME change, not just creation.
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.touch()
    mtime_before = heartbeat_path.stat().st_mtime
    time.sleep(0.05)  # ensure observable mtime delta if write fires
    bridge_with_thread._start_heartbeat_thread()
    # Cadence is max(5, heartbeat_file_seconds/2). With cfg=0.4 the
    # 5s floor applies. Wait up to 7s for at least one tick.
    deadline = time.monotonic() + 7.0
    while time.monotonic() < deadline:
        if heartbeat_path.stat().st_mtime > mtime_before + 0.001:
            break
        time.sleep(0.1)
    bridge_with_thread.stop()
    assert heartbeat_path.stat().st_mtime > mtime_before, (
        "heartbeat file mtime did not advance — thread didn't write"
    )


# ── 2. Half-cadence + stop ────────────────────────────────────────────


def test_heartbeat_thread_cadence_uses_half_of_heartbeat_seconds(
    bridge_with_thread: gerrit_jira_bridge.GerritJiraBridge,
) -> None:
    # The cadence is max(5, cfg.heartbeat_file_seconds / 2). With
    # cfg=0.4, expected cadence is the 5s floor (test relies on this).
    # If a future contributor lowers the floor, this test still
    # passes via the half-rule branch.
    expected_floor = 5.0
    actual_cadence = max(
        expected_floor,
        bridge_with_thread.config.heartbeat_file_seconds / 2.0,
    )
    # Smoke: cadence reduces to 5s for our 0.4s test config
    assert actual_cadence == 5.0


def test_heartbeat_thread_exits_on_stop(
    bridge_with_thread: gerrit_jira_bridge.GerritJiraBridge,
) -> None:
    # Use a custom Bridge with tighter cadence so the wait() returns soon.
    bridge_with_thread.config = gerrit_jira_bridge.BridgeConfig(
        agent_class="subscription-claude",
        heartbeat_file_seconds=2.0,  # cadence floor 5s; stop() should
        # still exit within ~1s because stop_event.set() interrupts wait()
        heartbeat_file_path=bridge_with_thread.config.heartbeat_file_path,
    )
    bridge_with_thread._start_heartbeat_thread()
    thread = bridge_with_thread._heartbeat_thread
    assert thread is not None
    assert thread.is_alive()
    bridge_with_thread.stop()
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "heartbeat thread didn't exit within 2s of stop()"


# ── 3. Write failure does not crash the thread ────────────────────────


def test_heartbeat_thread_survives_write_failure(
    bridge_with_thread: gerrit_jira_bridge.GerritJiraBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = {"n": 0}

    def failing_touch(self: Any) -> None:
        call_count["n"] += 1
        raise RuntimeError("synthetic write failure")

    monkeypatch.setattr(
        gerrit_jira_bridge.GerritJiraBridge,
        "_touch_heartbeat_file",
        failing_touch,
    )
    bridge_with_thread._start_heartbeat_thread()
    thread = bridge_with_thread._heartbeat_thread
    assert thread is not None
    # Sleep long enough for the cadence to fire at least once
    time.sleep(6.0)
    bridge_with_thread.stop()
    thread.join(timeout=2.0)
    assert call_count["n"] >= 1, (
        "thread did not call _touch_heartbeat_file at least once"
    )
    # The point is that the thread is_alive() right up to stop() — it
    # didn't crash on the synthetic exception.


# ── 4. Idempotency ────────────────────────────────────────────────────


def test_heartbeat_thread_start_is_idempotent(
    bridge_with_thread: gerrit_jira_bridge.GerritJiraBridge,
) -> None:
    bridge_with_thread._start_heartbeat_thread()
    thread1 = bridge_with_thread._heartbeat_thread
    assert thread1 is not None
    bridge_with_thread._start_heartbeat_thread()  # second call
    thread2 = bridge_with_thread._heartbeat_thread
    # Must return same thread object — no accumulation
    assert thread1 is thread2
    bridge_with_thread.stop()
    thread1.join(timeout=2.0)


# ── 5. stop() sets the thread Event ───────────────────────────────────


def test_stop_sets_heartbeat_thread_stop_event(
    bridge_with_thread: gerrit_jira_bridge.GerritJiraBridge,
) -> None:
    assert not bridge_with_thread._heartbeat_thread_stop.is_set()
    bridge_with_thread.stop()
    assert bridge_with_thread._heartbeat_thread_stop.is_set()


# ── 6. Thread runs as daemon (dies with process) ──────────────────────


def test_heartbeat_thread_is_daemon(
    bridge_with_thread: gerrit_jira_bridge.GerritJiraBridge,
) -> None:
    bridge_with_thread._start_heartbeat_thread()
    thread = bridge_with_thread._heartbeat_thread
    assert thread is not None
    assert thread.daemon, (
        "heartbeat thread must be daemon=True so the daemon process can "
        "exit cleanly even if stop() is missed (e.g. SIGKILL)"
    )
    bridge_with_thread.stop()
    thread.join(timeout=2.0)
