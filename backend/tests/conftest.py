"""Shared fixtures for OmniSight backend tests."""

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
async def client():
    """Provide an async HTTP test client against the FastAPI app.

    Initializes the database so lifespan dependencies are met.
    """
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient

    await db.init()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        await db.close()
