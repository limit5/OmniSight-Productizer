"""M1 — tenant_budget → cgroup mapping + OOM watchdog tests.

Pure unit tests on `_compute_resource_limits` plus a docker-stubbed
integration test verifying that:

  * the synthesised `docker run` command line carries the right
    `--cpus`, `--memory`, `--cpu-shares` flags and `tenant_id` /
    `tokens` labels;
  * the audit row written for `sandbox_launched` carries the
    kernel-enforced share so an auditor can reproduce who got what;
  * the OOM watchdog records `sandbox.oom` + bumps the metric when
    docker inspect reports `State.OOMKilled=true`.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from backend import container as ct


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _compute_resource_limits unit tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestComputeResourceLimits:
    def test_one_token_maps_to_one_core_512m(self):
        cpus, mem, shares = ct._compute_resource_limits(1.0)
        assert cpus == "1.00"
        assert mem == "512m"
        assert shares == 1024

    def test_four_tokens_maps_to_four_cores_2048m(self):
        cpus, mem, shares = ct._compute_resource_limits(4.0)
        assert cpus == "4.00"
        assert mem == "2048m"
        assert shares == 4096

    def test_fractional_token(self):
        cpus, mem, shares = ct._compute_resource_limits(0.5)
        assert cpus == "0.50"
        assert mem == "256m"
        assert shares == 512

    def test_below_minimum_token_clamps_up(self):
        # 0.05 token would give 51m and 51 shares — too small to schedule.
        # The clamp pulls it up to M1_MIN_TOKENS=0.25.
        cpus, mem, shares = ct._compute_resource_limits(0.05)
        assert cpus == "0.25"
        assert mem == "128m"

    def test_above_max_token_clamps_down(self):
        # 999 tokens would crater the host; clamp at M1_MAX_TOKENS=12.
        cpus, mem, shares = ct._compute_resource_limits(999.0)
        assert cpus == "12.00"
        assert mem == f"{12 * 512}m"
        assert shares == 12 * 1024

    def test_none_falls_back_to_settings(self, monkeypatch):
        monkeypatch.setattr(
            "backend.config.settings.docker_cpu_limit", "3", raising=False,
        )
        monkeypatch.setattr(
            "backend.config.settings.docker_memory_limit", "4g", raising=False,
        )
        cpus, mem, shares = ct._compute_resource_limits(None)
        assert cpus == "3"
        assert mem == "4g"
        assert shares == 1024  # default, no per-tenant weight

    def test_zero_falls_back_to_settings(self, monkeypatch):
        monkeypatch.setattr(
            "backend.config.settings.docker_cpu_limit", "1", raising=False,
        )
        monkeypatch.setattr(
            "backend.config.settings.docker_memory_limit", "1g", raising=False,
        )
        cpus, mem, shares = ct._compute_resource_limits(0)
        assert mem == "1g"
        assert shares == 1024


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures + docker stubs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
async def db_for_audit(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as cfg
        cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        try:
            yield db
        finally:
            await db.close()


@pytest.fixture(autouse=True)
def _reset_state():
    from backend import metrics as m
    if m.is_available():
        m.reset_for_tests()
    # cancel any leftover OOM watchdogs so we don't leak across tests
    for info in list(ct._containers.values()):
        for tname in ("oom_task", "lifetime_task"):
            t = getattr(info, tname, None)
            if t is not None:
                try:
                    t.cancel()
                except Exception:
                    pass
    ct._containers.clear()
    ct._reset_runtime_cache_for_tests()
    yield
    for info in list(ct._containers.values()):
        for tname in ("oom_task", "lifetime_task"):
            t = getattr(info, tname, None)
            if t is not None:
                try:
                    t.cancel()
                except Exception:
                    pass
    ct._containers.clear()


def _install_docker_stubs(monkeypatch, *, runsc_available=False,
                          oom_killed=False, exit_status="running",
                          run_rc=0, run_out="abcdef123456"):
    """Stub the docker CLI surface. `oom_killed` toggles the canned
    `docker inspect .State` response so the OOM watchdog test can
    flip a single boolean to flow into the OOM branch."""
    calls: list[str] = []

    async def fake_run(cmd, timeout=60):
        calls.append(cmd)
        if "docker info" in cmd:
            payload = {"runc": {"path": "runc"}}
            if runsc_available:
                payload["runsc"] = {"path": "runsc"}
            return (0, json.dumps(payload), "")
        if "network ls" in cmd:
            return (0, "", "")
        if "image inspect" in cmd:
            return (0, "sha256:" + "a" * 64, "")
        if "docker run" in cmd:
            return (run_rc, run_out, "" if run_rc == 0 else "boom")
        if "docker inspect --format '{{.State.Status}}" in cmd:
            return (0, f"{exit_status}|{'true' if oom_killed else 'false'}|137",
                    "")
        if "docker inspect" in cmd:
            return (0, "abcdef123456", "")
        if "docker exec" in cmd:
            return (0, "", "")
        if "docker rm" in cmd or "docker stop" in cmd:
            return (0, "", "")
        return (0, "", "")

    async def fake_ensure_image():
        return True

    monkeypatch.setattr(ct, "_run", fake_run)
    monkeypatch.setattr(ct, "ensure_image", fake_ensure_image)
    return calls


def _baseline_settings(monkeypatch):
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests", "",
        raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.sandbox_lifetime_s", 0, raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runc", raising=False,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  start_container with tenant_budget
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_tenant_budget_emits_correct_docker_run_flags(
    db_for_audit, monkeypatch,
):
    calls = _install_docker_stubs(monkeypatch)
    _baseline_settings(monkeypatch)

    info = await ct.start_container(
        "a-budget", Path("/tmp"),
        tenant_id="tenant-alpha", tenant_budget=4.0,
    )
    # Cancel the OOM watchdog now so the test isolation stays clean.
    if info.oom_task is not None:
        info.oom_task.cancel()

    run_cmd = next(c for c in calls if c.startswith("docker run"))
    assert "--cpus=4.00" in run_cmd
    assert "--memory=2048m" in run_cmd
    assert "--cpu-shares=4096" in run_cmd
    assert "--label tenant_id=tenant-alpha" in run_cmd
    assert "--label tokens=4.00" in run_cmd

    # ContainerInfo carries the same numbers for downstream consumers
    # (M4 metrics, OOM attribution).
    assert info.tenant_id == "tenant-alpha"
    assert info.tenant_budget == 4.0
    assert info.cpus == "4.00"
    assert info.memory == "2048m"
    assert info.cpu_shares == 4096


@pytest.mark.asyncio
async def test_tenant_budget_recorded_in_audit(db_for_audit, monkeypatch):
    _install_docker_stubs(monkeypatch)
    _baseline_settings(monkeypatch)

    info = await ct.start_container(
        "a-audit", Path("/tmp"),
        tenant_id="tenant-beta", tenant_budget=2.0,
    )
    if info.oom_task is not None:
        info.oom_task.cancel()

    from backend import audit as _a
    rows = await _a.query(actor="agent:a-audit", limit=10)
    launches = [r for r in rows if r.get("action") == "sandbox_launched"]
    assert launches, "expected an audit row for sandbox_launched"
    after = launches[0].get("after") or {}
    if isinstance(after, str):
        after = json.loads(after)
    assert after.get("tenant_id") == "tenant-beta"
    assert after.get("tenant_budget") == 2.0
    assert after.get("cpus") == "2.00"
    assert after.get("memory") == "1024m"
    assert after.get("cpu_shares") == 2048


@pytest.mark.asyncio
async def test_no_tenant_budget_falls_back_to_legacy_settings(
    db_for_audit, monkeypatch,
):
    calls = _install_docker_stubs(monkeypatch)
    _baseline_settings(monkeypatch)
    monkeypatch.setattr(
        "backend.config.settings.docker_cpu_limit", "2", raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.docker_memory_limit", "1g", raising=False,
    )

    info = await ct.start_container("a-legacy", Path("/tmp"))
    if info.oom_task is not None:
        info.oom_task.cancel()

    run_cmd = next(c for c in calls if c.startswith("docker run"))
    assert "--cpus=2" in run_cmd
    assert "--memory=1g" in run_cmd
    # default tenant id when none supplied + no request context
    assert "--label tenant_id=t-default" in run_cmd
    assert "--label tokens=0" in run_cmd
    assert info.tenant_id == "t-default"


@pytest.mark.asyncio
async def test_tenant_id_pulled_from_request_context_when_omitted(
    db_for_audit, monkeypatch,
):
    """When the caller doesn't pass tenant_id, start_container should
    pull it from the request-scoped ContextVar so older callsites
    automatically pick up the right label."""
    calls = _install_docker_stubs(monkeypatch)
    _baseline_settings(monkeypatch)

    from backend.db_context import set_tenant_id
    set_tenant_id("tenant-from-context")
    try:
        info = await ct.start_container(
            "a-ctx", Path("/tmp"), tenant_budget=1.0,
        )
    finally:
        set_tenant_id(None)
    if info.oom_task is not None:
        info.oom_task.cancel()

    run_cmd = next(c for c in calls if c.startswith("docker run"))
    assert "--label tenant_id=tenant-from-context" in run_cmd
    assert info.tenant_id == "tenant-from-context"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OOM watchdog
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_oom_watchdog_records_sandbox_oom(db_for_audit, monkeypatch):
    """When docker inspect reports State.OOMKilled=true the watchdog
    must:
      * bump sandbox_oom_total{tenant_id, tier};
      * write a sandbox.oom audit row carrying tenant_id + memory limit;
      * mark the ContainerInfo as killed_oom.
    """
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")

    _install_docker_stubs(
        monkeypatch, oom_killed=True, exit_status="exited",
    )
    _baseline_settings(monkeypatch)
    # Drive the watchdog faster than the default 0.5s so the test
    # doesn't wait around.
    monkeypatch.setattr(ct, "OOM_POLL_INTERVAL_S", 0.01, raising=False)

    info = await ct.start_container(
        "a-oom", Path("/tmp"),
        tenant_id="tenant-oom", tenant_budget=1.0,
    )
    # Wait for the watchdog to observe the canned "exited|true" inspect.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if info.status == "killed_oom":
            break

    assert info.status == "killed_oom", (
        f"expected killed_oom, got {info.status!r}"
    )

    samples = list(m.sandbox_oom_total.collect()[0].samples)
    matches = [s for s in samples
               if s.labels.get("tenant_id") == "tenant-oom"
               and s.labels.get("tier") == "t1"
               and s.name.endswith("_total")]
    assert matches and matches[0].value >= 1, (
        f"sandbox_oom_total not incremented for tenant-oom (samples={samples})"
    )

    from backend import audit as _a
    rows = await _a.query(actor="system:oom-watchdog", limit=10)
    oom_rows = [r for r in rows if r.get("action") == "sandbox.oom"]
    assert oom_rows, "expected an audit row for sandbox.oom"
    after = oom_rows[0].get("after") or {}
    if isinstance(after, str):
        after = json.loads(after)
    assert after.get("tenant_id") == "tenant-oom"
    assert after.get("memory_limit") == "512m"


@pytest.mark.asyncio
async def test_oom_watchdog_silent_on_clean_exit(db_for_audit, monkeypatch):
    """A normal exit (OOMKilled=false, exit code 0) must NOT generate
    an OOM audit row or bump the OOM metric — otherwise every sandbox
    teardown would page somebody."""
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")

    _install_docker_stubs(
        monkeypatch, oom_killed=False, exit_status="exited",
    )
    _baseline_settings(monkeypatch)
    monkeypatch.setattr(ct, "OOM_POLL_INTERVAL_S", 0.01, raising=False)

    info = await ct.start_container(
        "a-clean", Path("/tmp"),
        tenant_id="tenant-clean", tenant_budget=1.0,
    )
    await asyncio.sleep(0.1)  # let the watchdog see "exited|false"

    assert info.status != "killed_oom"
    samples = list(m.sandbox_oom_total.collect()[0].samples)
    assert all(s.value == 0 for s in samples
               if s.name.endswith("_total")), \
        "sandbox_oom_total bumped on a clean exit"

    from backend import audit as _a
    rows = await _a.query(actor="system:oom-watchdog", limit=5)
    assert not [r for r in rows if r.get("action") == "sandbox.oom"]


@pytest.mark.asyncio
async def test_stop_container_cancels_oom_watchdog(db_for_audit, monkeypatch):
    """stop_container must cancel the OOM watchdog so it doesn't keep
    polling a name that's no longer in the registry."""
    _install_docker_stubs(monkeypatch)
    _baseline_settings(monkeypatch)
    monkeypatch.setattr(ct, "OOM_POLL_INTERVAL_S", 0.01, raising=False)

    info = await ct.start_container("a-stop", Path("/tmp"), tenant_budget=1.0)
    oom_task = info.oom_task
    assert oom_task is not None and not oom_task.cancelled()

    await ct.stop_container("a-stop")
    # Give the loop one tick to process the cancellation.
    await asyncio.sleep(0.02)
    assert oom_task.cancelled() or oom_task.done()
