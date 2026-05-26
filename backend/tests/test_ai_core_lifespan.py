"""OP-1757 contract tests for ai-core probe lifespan wiring."""

from __future__ import annotations

import pathlib


def test_main_lifespan_starts_ai_core_probe_task() -> None:
    source = pathlib.Path("backend/main.py").read_text(encoding="utf-8")

    assert "from backend.agents import ai_core_probe as _ai_core_probe" in source
    assert "ai_core_probe = _ai_core_probe.AiCoreProbe()" in source
    assert "ai_core_probe_task = asyncio.create_task" in source
    assert "asyncio.to_thread(ai_core_probe.run_forever" in source


def test_main_lifespan_cancels_ai_core_probe_task() -> None:
    source = pathlib.Path("backend/main.py").read_text(encoding="utf-8")
    shutdown_source = source.split("for t in", 1)[1]

    assert "ai_core_probe_stop_event.set()" in source
    assert "ai_core_probe_task" in shutdown_source
