"""WP.1.6 -- Block model migration strategy documentation contract."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WP_ADR = PROJECT_ROOT / "docs" / "design" / "wp-warp-inspired-patterns.md"


def _wp16_section() -> str:
    body = WP_ADR.read_text(encoding="utf-8")
    start = body.index("### WP.1.6 Migration Strategy")
    end = body.index("### WP.2 Skills Loader", start)
    return body[start:end]


def test_wp16_pins_surface_migration_order() -> None:
    section = _wp16_section()
    orchestrator = section.index("ORCHESTRATOR / TokenUsageStats")
    bp = section.index("**BP**")
    hd = section.index("**HD**")

    assert orchestrator < bp < hd


def test_wp16_pins_feature_flags_dual_write_and_30_day_fallback() -> None:
    section = _wp16_section()
    required = {
        "wp.block_model.orchestrator_token_usage",
        "wp.block_model.bp",
        "wp.block_model.hd",
        "Dual-write invariant",
        "Block projection",
        "舊 ad-hoc surface",
        "30 天",
        "OMNISIGHT_WP_BLOCK_MODEL_ENABLED=false",
        "block_model_fallback",
    }

    missing = sorted(term for term in required if term not in section)
    assert not missing, f"WP.1.6 strategy missing terms: {missing}"
