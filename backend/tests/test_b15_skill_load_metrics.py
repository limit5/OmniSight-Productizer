"""B15 (#350) row 264 — skill-loading metrics regression tests.

Verifies that `build_system_prompt` / `build_skill_injection` bump the
three Prometheus series introduced by TODO.md row 264:

  * ``skill_load_total{mode,phase,result}``
  * ``skill_token_saved_total{mode}``
  * ``skill_load_latency_ms{mode,phase}`` (histogram)

These tests only run when ``prometheus_client`` is installed — the
no-op stubs are exercised indirectly through metrics availability
guards everywhere else.
"""

from __future__ import annotations

import pytest


def _require_metrics():
    from backend import metrics as m
    if not m.is_available():
        pytest.skip("prometheus_client not installed")
    m.reset_for_tests()
    return m


def _counter_map(series) -> dict[tuple, float]:
    """Flatten a prometheus_client counter `.collect()` into
    ``{labels_tuple: value}`` for easy assertions."""
    out: dict[tuple, float] = {}
    for sample in series.collect()[0].samples:
        if not sample.name.endswith("_total"):
            continue
        key = tuple(sorted(sample.labels.items()))
        out[key] = sample.value
    return out


def _histogram_count(series) -> float:
    """Return the _count sum across all label sets of a histogram."""
    total = 0.0
    for sample in series.collect()[0].samples:
        if sample.name.endswith("_count"):
            total += sample.value
    return total


def test_eager_build_system_prompt_increments_skill_load_total():
    m = _require_metrics()
    from backend.prompt_loader import build_system_prompt

    prompt = build_system_prompt(
        agent_type="firmware", sub_type="bsp", mode="eager",
    )
    assert prompt, "eager build should still return a prompt"

    loads = _counter_map(m.skill_load_total)
    eager_key = (("mode", "eager"), ("phase", "inline_full"), ("result", "loaded"))
    assert loads.get(eager_key, 0) == 1, loads
    # Eager mode never increments token-savings.
    assert _counter_map(m.skill_token_saved_total) == {}
    assert _histogram_count(m.skill_load_latency_ms) >= 1


def test_lazy_phase1_increments_catalog_counter(monkeypatch):
    m = _require_metrics()
    from backend.prompt_loader import build_system_prompt

    prompt = build_system_prompt(
        agent_type="firmware", sub_type="bsp", mode="lazy",
    )
    assert "Available Skills (on-demand)" in prompt

    loads = _counter_map(m.skill_load_total)
    lazy_key = (
        ("mode", "lazy"),
        ("phase", "phase1_catalog"),
        ("result", "loaded"),
    )
    assert loads.get(lazy_key, 0) == 1, loads


def test_lazy_phase1_records_positive_tokens_saved_for_large_role(monkeypatch):
    """When a role body is larger than the catalog, the lazy build
    must record the delta into ``skill_token_saved_total``.

    Uses a monkey-patched large body so the test is independent of the
    (currently small) shipped role-skill markdown — the metric's math
    is what's under test, not the fixture sizes."""
    m = _require_metrics()
    import backend.prompt_loader as _pl

    big_body = "BIG_ROLE_BODY " * 5000  # ~70K chars, ~17.5K tokens
    monkeypatch.setattr(_pl, "load_role_skill", lambda *a, **kw: big_body)

    _pl.build_system_prompt(
        agent_type="firmware", sub_type="bsp", mode="lazy",
    )

    saved = _counter_map(m.skill_token_saved_total)
    lazy_saved_key = (("mode", "lazy"),)
    assert saved.get(lazy_saved_key, 0) > 0, (
        f"lazy mode should record positive token savings, got {saved}"
    )


def test_phase2_explicit_skill_injection_records_loaded_result():
    m = _require_metrics()
    from backend.prompt_loader import build_skill_injection

    text = build_skill_injection(explicit_skills=["android-kotlin"])
    assert text, "expected non-empty injection for android-kotlin"

    loads = _counter_map(m.skill_load_total)
    key = (
        ("mode", "lazy"),
        ("phase", "phase2_explicit"),
        ("result", "loaded"),
    )
    assert loads.get(key, 0) == 1, loads
    assert _histogram_count(m.skill_load_latency_ms) >= 1


def test_phase2_miss_records_miss_result():
    m = _require_metrics()
    from backend.prompt_loader import build_skill_injection

    # No domain_context, no user_prompt → no matches → miss.
    text = build_skill_injection(domain_context="", user_prompt="")
    assert text == ""

    loads = _counter_map(m.skill_load_total)
    miss_key = (
        ("mode", "lazy"),
        ("phase", "phase2_matched"),
        ("result", "miss"),
    )
    assert loads.get(miss_key, 0) == 1, loads


def test_phase2_matched_records_loaded_for_keyword_match():
    m = _require_metrics()
    from backend.prompt_loader import build_skill_injection

    # Android-flavoured context is enough to match at least one skill.
    text = build_skill_injection(
        domain_context="Android Kotlin Jetpack Compose mobile",
        user_prompt="fix login layout",
    )
    assert text, "expected non-empty injection for android context match"

    loads = _counter_map(m.skill_load_total)
    matched_key = (
        ("mode", "lazy"),
        ("phase", "phase2_matched"),
        ("result", "loaded"),
    )
    assert loads.get(matched_key, 0) >= 1, loads
