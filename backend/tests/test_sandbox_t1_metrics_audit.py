"""Phase 64-A S5 — launch metric + audit hooks.

Pure unit tests on `start_container` mocked at the docker boundary.
We exercise three paths: success, image-rejected, docker-run failure.
Each path must produce the expected metric label combo + (where
appropriate) an audit log entry.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from backend import container as ct


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
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
    ct._containers.clear()
    ct._reset_runtime_cache_for_tests()
    yield
    ct._containers.clear()


def _install_docker_stubs(monkeypatch, *, runsc_available=True,
                          run_rc=0, run_out="abcdef123456"):
    """Stub out everything start_container shells out to. Returns the
    list of commands that were invoked, for assertions."""
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Success path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_successful_launch_emits_metric_and_audit(db_for_audit, monkeypatch):
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")

    _install_docker_stubs(monkeypatch)
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests", "", raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.sandbox_lifetime_s", 0, raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runsc", raising=False,
    )

    info = await ct.start_container("a-good", Path("/tmp"))
    try:
        assert info.container_id == "abcdef123456"
        # Metric
        samples = list(m.sandbox_launch_total.collect()[0].samples)
        success = [s for s in samples
                   if s.labels.get("tier") == "t1"
                   and s.labels.get("runtime") == "runsc"
                   and s.labels.get("result") == "success"
                   and s.name.endswith("_total")]
        assert success and success[0].value >= 1
        # Audit
        from backend import audit as _a
        rows = await _a.query(actor="agent:a-good", limit=10)
        assert any(r.get("action") == "sandbox_launched" for r in rows)
    finally:
        ct._containers.pop("a-good", None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Image-rejected path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_image_rejection_emits_metric_and_audit(db_for_audit, monkeypatch):
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")

    _install_docker_stubs(monkeypatch)
    # Set an allow-list that does NOT include the image's actual digest.
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests",
        "sha256:" + "b" * 64, raising=False,
    )

    with pytest.raises(ct.ImageNotTrusted):
        await ct.start_container("a-untrusted", Path("/tmp"))

    samples = list(m.sandbox_launch_total.collect()[0].samples)
    rejected = [s for s in samples
                if s.labels.get("result") == "image_rejected"
                and s.name.endswith("_total")]
    assert rejected and rejected[0].value >= 1

    from backend import audit as _a
    rows = await _a.query(actor="agent:a-untrusted", limit=10)
    assert any(r.get("action") == "sandbox_image_rejected" for r in rows)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  docker run failure path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_docker_run_failure_emits_error_metric(monkeypatch):
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")

    _install_docker_stubs(monkeypatch, run_rc=125)
    monkeypatch.setattr(
        "backend.config.settings.docker_image_allowed_digests", "", raising=False,
    )
    monkeypatch.setattr(
        "backend.config.settings.docker_runtime", "runc", raising=False,
    )

    with pytest.raises(RuntimeError, match="Failed to start container"):
        await ct.start_container("a-fail", Path("/tmp"))

    samples = list(m.sandbox_launch_total.collect()[0].samples)
    err = [s for s in samples
           if s.labels.get("tier") == "t1"
           and s.labels.get("runtime") == "runc"
           and s.labels.get("result") == "error"
           and s.name.endswith("_total")]
    assert err and err[0].value >= 1
