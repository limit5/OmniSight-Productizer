"""OP-1655 — DAG executor entrypoint skeleton (inert, Dim 2 / Phase 1).

Covers the acceptance criteria for the inert skeleton:

  * disabled (flag unset) == no-op: returns immediately, claims nothing,
    writes no heartbeat;
  * enabled-in-harness == loop ticks + heartbeats under the distinct
    dag-exec namespace;
  * SIGTERM == clean shutdown (loop exits, heartbeat key cleared);
  * the heartbeat reuses the shared store but under a DISTINCT namespace
    that never pollutes the worker registry;
  * instance ids live in the dag-exec-* namespace.

Local harness only — no host state is touched.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any

import pytest

from backend import dag_executor as dx
from backend import worker


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test double: records every heartbeat write so we can assert on the
#  enabled / disabled split without depending on Redis.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _RecordingHeartbeat:
    def __init__(self) -> None:
        self.beats: list[tuple[str, dict[str, Any], int]] = []
        self.cleared: list[str] = []
        self._live: dict[str, dict[str, Any]] = {}

    def beat(self, instance_id: str, info: dict[str, Any], ttl_s: int) -> None:
        self.beats.append((instance_id, dict(info), ttl_s))
        self._live[instance_id] = dict(info)

    def clear(self, instance_id: str) -> None:
        self.cleared.append(instance_id)
        self._live.pop(instance_id, None)

    def is_alive(self, instance_id: str) -> bool:
        return instance_id in self._live

    def get_info(self, instance_id: str) -> dict[str, Any] | None:
        return self._live.get(instance_id)

    def list_active(self) -> list[str]:
        return sorted(self._live)


# ─── enable flag / namespace helpers ─────────────────────────────────


def test_is_enabled_only_true_for_exact_one(monkeypatch):
    monkeypatch.delenv(dx.ENABLE_ENV, raising=False)
    assert dx.is_enabled() is False
    monkeypatch.setenv(dx.ENABLE_ENV, "0")
    assert dx.is_enabled() is False
    monkeypatch.setenv(dx.ENABLE_ENV, "true")
    assert dx.is_enabled() is False
    monkeypatch.setenv(dx.ENABLE_ENV, "1")
    assert dx.is_enabled() is True


def test_instance_id_is_dag_exec_namespaced():
    assert dx.new_instance_id().startswith("dag-exec-")
    # a bare / mis-prefixed id is normalised into the namespace
    assert dx.ensure_dag_exec_namespace("1") == "dag-exec-1"
    assert dx.ensure_dag_exec_namespace("wkr-host-abcd") == "dag-exec-wkr-host-abcd"
    assert dx.ensure_dag_exec_namespace("dag-exec-host-ff") == "dag-exec-host-ff"
    # config normalises on construction
    cfg = dx.DagExecutorConfig(instance_id="7")
    assert cfg.instance_id == "dag-exec-7"


# ─── AC: disabled == no-op, returns immediately, claims nothing ──────


async def test_disabled_is_noop(monkeypatch):
    monkeypatch.delenv(dx.ENABLE_ENV, raising=False)
    hb = _RecordingHeartbeat()
    ex = dx.DagExecutor(
        dx.DagExecutorConfig(instance_id="dag-exec-test-off"),
        heartbeat=hb,
    )
    result = await ex.run()

    assert result.enabled is False
    assert result.ticks == 0
    assert result.heartbeats == 0
    # claimed nothing / no heartbeat written / nothing to clear
    assert hb.beats == []
    assert hb.cleared == []
    assert hb.list_active() == []


# ─── AC: enabled-in-harness == loop ticks + heartbeats ───────────────


async def test_enabled_ticks_and_heartbeats(monkeypatch):
    monkeypatch.setenv(dx.ENABLE_ENV, "1")
    hb = _RecordingHeartbeat()
    ex = dx.DagExecutor(
        dx.DagExecutorConfig(
            instance_id="dag-exec-test-on",
            poll_interval_s=0.0,
            heartbeat_interval_s=0.0,  # beat on every tick
            max_ticks=3,
        ),
        heartbeat=hb,
    )
    result = await ex.run()

    assert result.enabled is True
    assert result.ticks == 3
    assert result.heartbeats >= 1
    # first beat is the "starting" registration
    assert hb.beats[0][0] == "dag-exec-test-on"
    assert hb.beats[0][1]["status"] == "starting"
    # every beat advertises the dag-executor kind + the dag-exec id
    assert all(b[1]["kind"] == "dag-executor" for b in hb.beats)
    assert all(b[0] == "dag-exec-test-on" for b in hb.beats)
    # graceful exit clears the heartbeat key
    assert hb.cleared == ["dag-exec-test-on"]


async def test_enabled_override_bypasses_env(monkeypatch):
    # Even with the env flag unset, an explicit enabled=True arms the loop
    # (this is the test/harness seam; production always reads the env).
    monkeypatch.delenv(dx.ENABLE_ENV, raising=False)
    hb = _RecordingHeartbeat()
    ex = dx.DagExecutor(
        dx.DagExecutorConfig(
            instance_id="dag-exec-test-override",
            poll_interval_s=0.0, heartbeat_interval_s=0.0, max_ticks=1,
        ),
        heartbeat=hb, enabled=True,
    )
    result = await ex.run()
    assert result.enabled is True
    assert result.ticks == 1


# ─── AC: SIGTERM clean shutdown ──────────────────────────────────────


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX SIGTERM + loop.add_signal_handler required",
)
async def test_sigterm_clean_shutdown(monkeypatch):
    monkeypatch.setenv(dx.ENABLE_ENV, "1")
    hb = _RecordingHeartbeat()
    ex = dx.DagExecutor(
        dx.DagExecutorConfig(
            instance_id="dag-exec-test-sigterm",
            poll_interval_s=0.01,
            heartbeat_interval_s=0.0,
            # no max_ticks: runs until signalled
        ),
        heartbeat=hb,
    )

    loop = asyncio.get_running_loop()
    ex.install_signal_handlers(loop)
    task = asyncio.create_task(ex.run())
    # let it tick + heartbeat at least once
    await asyncio.sleep(0.05)
    os.kill(os.getpid(), signal.SIGTERM)

    result = await asyncio.wait_for(task, timeout=5.0)

    assert result.enabled is True
    assert result.ticks >= 1
    assert result.heartbeats >= 1
    # clean shutdown deregistered the heartbeat
    assert hb.cleared == ["dag-exec-test-sigterm"]
    assert hb.list_active() == []


async def test_request_stop_breaks_loop(monkeypatch):
    monkeypatch.setenv(dx.ENABLE_ENV, "1")
    hb = _RecordingHeartbeat()
    ex = dx.DagExecutor(
        dx.DagExecutorConfig(
            instance_id="dag-exec-test-stop",
            poll_interval_s=0.01, heartbeat_interval_s=0.0,
        ),
        heartbeat=hb,
    )
    task = asyncio.create_task(ex.run())
    await asyncio.sleep(0.03)
    ex.request_stop()
    result = await asyncio.wait_for(task, timeout=5.0)
    assert result.enabled is True
    assert hb.cleared == ["dag-exec-test-stop"]


# ─── heartbeat namespace isolation (does not pollute worker registry) ─


def test_heartbeat_distinct_namespace_over_memory_store():
    store = worker._MemoryHeartbeatStore()
    hb = dx.DagExecHeartbeat(store=store)

    hb.beat("dag-exec-x", {"kind": "dag-executor"}, ttl_s=30)

    # visible through the dag-exec view
    assert hb.list_active() == ["dag-exec-x"]
    assert hb.is_alive("dag-exec-x") is True
    assert hb.get_info("dag-exec-x")["kind"] == "dag-executor"

    # the only key in the shared store is fully namespaced — a bare
    # "dag-exec-x" worker id was never registered, so the worker registry
    # is not polluted.
    raw_keys = store.list_active()
    assert raw_keys == [f"{dx.DAG_EXEC_HEARTBEAT_PREFIX}dag-exec-x:alive"]
    assert all(k.startswith(dx.DAG_EXEC_HEARTBEAT_PREFIX) for k in raw_keys)

    hb.clear("dag-exec-x")
    assert hb.list_active() == []
    assert store.list_active() == []


def test_heartbeat_defaults_to_shared_store(monkeypatch):
    # Force the memory fallback (no Redis) and confirm default construction
    # reuses worker._get_default_store() without raising.
    worker.set_heartbeat_store_for_tests(worker._MemoryHeartbeatStore())
    try:
        hb = dx.DagExecHeartbeat()
        assert hb._store is worker._get_default_store()
    finally:
        worker.set_heartbeat_store_for_tests(None)
