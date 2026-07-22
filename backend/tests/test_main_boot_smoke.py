"""Boot smoke — the whole app module must IMPORT (name resolution included).

Regression lock for the 2026-07-22 staging crash: router registration lines
were added without their import lines; py_compile passes (syntax-only) and
no local test booted backend.main, so NameError shipped to staging. This
test imports the real app and asserts the memory-lane routes registered.
"""
from __future__ import annotations


def test_backend_main_imports_and_registers_memory_routes():
    import backend.main as m

    routes = {getattr(r, "path", "") for r in m.app.routes}
    for path in (
        "/api/v1/learned-items/block",
        "/api/v1/claude-memories/ingest",
        "/api/v1/claude-memories/pending",
        "/api/v1/claude-memories/{slug}/publish",
        "/api/v1/memory-promotions/pending",
        "/api/v1/memory-promotions/{version_id}/revoke",
    ):
        assert path in routes, path
