"""Z.3 (#292) checkbox 3 — fallback-strategy + throttled warning tests.

Scope is strictly the behavior added by this checkbox:
    - provider known + model unknown → `providers[<provider>]._default`
    - both unknown (or missing) → global `defaults`
    - each fallback arm emits a `WARNING` once per (arm, provider) key
      per 24 h in this worker, does not log on repeats, and does not log
      on exact-hit / `provider=None` scan-hit paths.

Future checkboxes (Z.3 checkbox 6 = YAML-corrupt boot resilience + reload
endpoint; Z.5 = cross-worker sync) will extend this file.
"""

from __future__ import annotations

import logging

import pytest

from backend import pricing
from backend.pricing import get_pricing, reset_cache_for_tests


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


def _fallback_warnings(caplog):
    """Return only the WARNING records emitted by backend.pricing that
    describe a fallback (matches the `llm_pricing fallback:` prefix)."""
    return [
        r for r in caplog.records
        if r.name == "backend.pricing"
        and r.levelno == logging.WARNING
        and r.getMessage().startswith("llm_pricing fallback:")
    ]


class TestFallbackStrategy:
    """The lookup chain itself: exact → provider._default → global defaults."""

    def test_exact_hit_returns_model_rate(self):
        assert get_pricing("anthropic", "claude-opus-4-7") == (5.0, 25.0)

    def test_provider_known_model_unknown_uses_provider_default(self):
        # anthropic._default is Sonnet-tier (3, 15) per config/llm_pricing.yaml
        assert get_pricing("anthropic", "claude-future-model-v99") == (3.0, 15.0)

    def test_both_unknown_uses_global_defaults(self):
        # YAML `defaults: {input: 1, output: 3}` deliberately higher than
        # the cheapest real provider so "unknown" looks expensive.
        assert get_pricing("some-vendor-nobody-ships", "any-model") == (1.0, 3.0)

    def test_provider_none_with_known_model_scans_and_finds(self):
        assert get_pricing(None, "claude-opus-4-7") == (5.0, 25.0)

    def test_provider_none_with_unknown_model_uses_global_defaults(self):
        assert get_pricing(None, "totally-unknown-model") == (1.0, 3.0)

    def test_provider_case_insensitive(self):
        assert get_pricing("Anthropic", "claude-opus-4-7") == (5.0, 25.0)
        assert get_pricing("ANTHROPIC", "claude-opus-4-7") == (5.0, 25.0)


class TestFallbackWarningEmission:
    """Each fallback arm emits a single WARNING the first time it is hit."""

    def test_exact_hit_does_not_warn(self, caplog):
        caplog.set_level(logging.WARNING, logger="backend.pricing")
        get_pricing("anthropic", "claude-opus-4-7")
        assert _fallback_warnings(caplog) == []

    def test_provider_none_scan_hit_does_not_warn(self, caplog):
        # Scan-hit is a successful disambiguation, not a fallback.
        caplog.set_level(logging.WARNING, logger="backend.pricing")
        get_pricing(None, "claude-opus-4-7")
        assert _fallback_warnings(caplog) == []

    def test_provider_default_arm_logs_once(self, caplog):
        caplog.set_level(logging.WARNING, logger="backend.pricing")
        get_pricing("anthropic", "claude-unknown-model")
        warns = _fallback_warnings(caplog)
        assert len(warns) == 1
        msg = warns[0].getMessage()
        assert "provider='anthropic'" in msg
        assert "model='claude-unknown-model'" in msg
        assert "providers[anthropic]._default" in msg

    def test_global_default_arm_logs_once(self, caplog):
        caplog.set_level(logging.WARNING, logger="backend.pricing")
        get_pricing("some-vendor-nobody-ships", "any-model")
        warns = _fallback_warnings(caplog)
        assert len(warns) == 1
        msg = warns[0].getMessage()
        assert "global defaults=(1.0, 3.0)" in msg

    def test_provider_none_miss_logs_global_default_arm(self, caplog):
        caplog.set_level(logging.WARNING, logger="backend.pricing")
        get_pricing(None, "unknown-model")
        warns = _fallback_warnings(caplog)
        assert len(warns) == 1
        assert "provider=None" in warns[0].getMessage()


class TestFallbackWarningThrottle:
    """The throttle suppresses repeats within 24 h per (arm, provider)."""

    def test_repeated_provider_default_hits_log_once(self, caplog):
        caplog.set_level(logging.WARNING, logger="backend.pricing")
        get_pricing("anthropic", "unknown-1")
        get_pricing("anthropic", "unknown-2")
        get_pricing("anthropic", "unknown-3")
        assert len(_fallback_warnings(caplog)) == 1

    def test_repeated_global_default_hits_log_once(self, caplog):
        caplog.set_level(logging.WARNING, logger="backend.pricing")
        get_pricing("vendor-x", "m1")
        get_pricing("vendor-x", "m2")
        get_pricing("vendor-x", "m3")
        assert len(_fallback_warnings(caplog)) == 1

    def test_different_providers_log_independently(self, caplog):
        # Two different providers hitting the same fallback arm each get
        # their own 24 h clock.
        caplog.set_level(logging.WARNING, logger="backend.pricing")
        get_pricing("vendor-a", "m1")
        get_pricing("vendor-b", "m1")
        assert len(_fallback_warnings(caplog)) == 2

    def test_different_arms_same_provider_log_independently(self, caplog):
        # provider_default arm vs global_default arm use different keys;
        # both should emit even though both are "anthropic-ish" contexts.
        caplog.set_level(logging.WARNING, logger="backend.pricing")
        get_pricing("anthropic", "unknown-1")      # provider_default arm
        get_pricing("no-such-vendor", "unknown-2")  # global_default arm
        assert len(_fallback_warnings(caplog)) == 2

    def test_throttle_elapses_re_emits(self, caplog, monkeypatch):
        caplog.set_level(logging.WARNING, logger="backend.pricing")
        # Seed: first call logs, second call suppressed under normal time.
        get_pricing("anthropic", "unknown-a")
        get_pricing("anthropic", "unknown-b")
        assert len(_fallback_warnings(caplog)) == 1

        # Advance simulated time past the throttle window and re-hit.
        real_time = pricing.time.time
        monkeypatch.setattr(
            pricing.time, "time",
            lambda: real_time() + pricing._WARN_THROTTLE_SECONDS + 1.0,
        )
        get_pricing("anthropic", "unknown-c")
        assert len(_fallback_warnings(caplog)) == 2

    def test_reset_cache_for_tests_clears_throttle(self, caplog):
        # Ensures the fixture's reset gives each test a clean window even
        # when earlier tests in the same process already tripped the arm.
        caplog.set_level(logging.WARNING, logger="backend.pricing")
        get_pricing("anthropic", "unknown-a")
        assert len(_fallback_warnings(caplog)) == 1
        reset_cache_for_tests()
        caplog.clear()
        get_pricing("anthropic", "unknown-b")
        assert len(_fallback_warnings(caplog)) == 1


class TestPriceNeutralityAcrossFallbacks:
    """Paranoia: the 8 pre-Z.3 models still bill at their historical rates
    when the call site (like backend/routers/system.py::track_tokens) passes
    `provider=None`. This duplicates the checkbox-2 contract but re-asserts
    it under the new warning machinery to catch regressions where the logs
    path accidentally short-circuits the lookup."""

    EXPECT = {
        "claude-opus-4-7": (5.0, 25.0),
        "claude-opus-4-20250514": (15.0, 75.0),
        "claude-sonnet-4-20250514": (3.0, 15.0),
        "gpt-4o": (5.0, 15.0),
        "gemini-1.5-pro": (0.5, 1.5),
        "grok-3-mini": (2.0, 10.0),
        "llama-3.3-70b-versatile": (0.6, 0.6),
        "deepseek-chat": (0.14, 0.28),
    }

    @pytest.mark.parametrize("model,expected", list(EXPECT.items()))
    def test_legacy_model_bit_identical_via_scan(self, model, expected):
        assert get_pricing(None, model) == expected
