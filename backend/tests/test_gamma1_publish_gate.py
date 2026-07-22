"""γ-1 (leg-3) — lint profile + human publish gate."""
from __future__ import annotations

import pytest

from backend.claude_memory_lint import lint_body


def test_lint_detects_real_secret_shapes():
    out = lint_body("token: ATATT3xFfGF0abcdefghijklmnopqrstuv")
    assert "secret:atlassian_token" in out["blocking"]


def test_lint_localhost_dsn_allowlisted():
    out = lint_body("dsn = postgresql://u6test:u6test@localhost:5455/db")
    assert out["blocking"] == []


def test_lint_advisory_flags():
    out = lint_body("x" * (49 * 1024) + " SUPERSEDED [[nonexistent_slug]]",
                    known_slugs={"real_slug"})
    assert "oversize_body" in out["advisory"]
    assert "stale_marker" in out["advisory"]
    assert any(a.startswith("dead_links:") for a in out["advisory"])


def test_lint_clean_body():
    out = lint_body("Normal memory about [[real_slug]] workflows.",
                    known_slugs={"real_slug"})
    assert out == {"blocking": [], "advisory": []}


def test_live_corpus_has_zero_blocking():
    """Integrity-audit MINOR-5 verified at build time: the real store must
    produce ZERO blocking hits (else first publish wave jams)."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from claude_memory_ingest import collect_items, _DEFAULT_STORE

    if not _DEFAULT_STORE.exists():
        pytest.skip("no local store")
    items = collect_items(_DEFAULT_STORE)
    blocked = {
        x["slug"]: lint_body(x["body"])["blocking"]
        for x in items if lint_body(x["body"])["blocking"]
    }
    assert blocked == {}, blocked


def test_publish_rate_cap():
    from backend.routers import claude_memories as cm

    cm._publish_window.clear()
    for _ in range(cm._PUBLISH_CAP_PER_HOUR):
        assert cm._rate_ok() is True
    assert cm._rate_ok() is False
    cm._publish_window.clear()


def test_routes_exist():
    from backend.routers.claude_memories import router

    paths = {r.path for r in router.routes}
    assert {"/claude-memories/ingest", "/claude-memories/pending",
            "/claude-memories/{slug}/publish",
            "/claude-memories/{slug}/revoke"} <= paths
