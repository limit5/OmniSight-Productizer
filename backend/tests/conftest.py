"""Shared fixtures for OmniSight backend tests.

A8/C9 note: several tests import modules that keep state in
module-level globals (decision_engine._pending, decision_rules._RULES,
pipeline._active_pipeline, etc.). The reset hooks (`_reset_for_tests`,
`clear`) exist so this file can put those singletons back in a known
state between runs — they are NOT a supported production API. A
future refactor pass should dependency-inject these stores so
pytest-xdist can run tests in parallel safely; until then the serial
runner plus these reset hooks is the contract.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Ensure workspace root points to a temp directory for all tool tests
_tmp = tempfile.mkdtemp(prefix="omnisight_test_")
os.environ["OMNISIGHT_WORKSPACE"] = _tmp


def pytest_sessionfinish(session, exitstatus):
    """Clean up the module-level temp directory after all tests."""
    shutil.rmtree(_tmp, ignore_errors=True)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Provide a fresh temporary workspace and activate it for tools."""
    from backend.agents.tools import set_active_workspace

    set_active_workspace(tmp_path)
    yield tmp_path
    set_active_workspace(None)


@pytest.fixture()
def sample_files(workspace: Path) -> Path:
    """Create a small tree of sample files inside the workspace."""
    (workspace / "src").mkdir()
    (workspace / "src" / "main.c").write_text('#include "driver.h"\nint main() { return 0; }\n')
    (workspace / "src" / "driver.h").write_text("#pragma once\nvoid init_sensor(void);\n")
    (workspace / "README.md").write_text("# Test project\n")
    (workspace / "config.yaml").write_text("sensor:\n  model: IMX335\n  bus: i2c\n")
    return workspace


@pytest.fixture()
async def client(tmp_path, monkeypatch):
    """Provide an async HTTP test client against the FastAPI app.

    Each test gets a fresh per-test sqlite file so state never leaks
    across tests. Previously every test hit the real `data/omnisight.db`
    and rows accumulated forever — the audit flagged this as the root
    cause of `test_list_plan_chain` seeing 8+ plans when it expected 2.

    L1 #2 note: the bootstrap gate middleware would normally 307 every
    non-exempt request on a fresh install (nothing configured). For the
    shared client fixture we pin the gate to "finalized" so existing
    tests don't suddenly have to care about bootstrap state. Tests that
    explicitly exercise the gate reset the cache themselves.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db_path))
    # Re-resolve path on the module (loaded at import time from the real
    # data/ dir) so `init()` opens the fresh tmp file.
    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()

    from backend.main import app
    from httpx import ASGITransport, AsyncClient
    from backend import bootstrap as _boot

    # Pin bootstrap to finalized so non-gate tests see a normal app.
    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )
    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    await db.init()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        await db.close()
        _boot._gate_cache_reset()


@pytest.fixture(autouse=True)
def _reset_bootstrap_gate_between_tests():
    """Reset the bootstrap gate cache between every test.

    Without this, a test that sets _gate_cache["finalized"] = True
    (e.g. via the shared `client` fixture) leaks that state into
    subsequent tests — causing flaky failures where bootstrap_gate
    tests expect the gate to be "red" but find it "green" due to
    a prior test's leftover cache. The in-process cache has no TTL
    short enough to prevent this in a fast test run.
    """
    from backend import bootstrap as _boot
    _boot._gate_cache_reset()
    yield
    _boot._gate_cache_reset()
