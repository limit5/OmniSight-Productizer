"""Shared fixtures for OmniSight backend tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# Ensure workspace root points to a temp directory for all tool tests
_tmp = tempfile.mkdtemp(prefix="omnisight_test_")
os.environ["OMNISIGHT_WORKSPACE"] = _tmp


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
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture()
async def client():
    """Provide an async HTTP test client against the FastAPI app."""
    from backend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
