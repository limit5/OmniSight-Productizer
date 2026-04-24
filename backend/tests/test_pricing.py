"""Z.3 (#292) checkbox 3 + checkbox 4 — fallback-strategy, throttled
warning, and `POST /runtime/pricing/reload` tests.

Scope is the behaviour added by Z.3:
    - checkbox 3: fallback chain + throttled WARNING emission per arm.
    - checkbox 4: `POST /runtime/pricing/reload` endpoint + the
      cross-worker `pricing_reload` event callback that clears each
      peer worker's local cache.

Z.3 checkbox 6 (YAML-corrupt boot resilience + cross-worker reload
under live Redis) will extend this file further.
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Z.3 checkbox 4 (#292) — POST /runtime/pricing/reload + cross-worker fan-out
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCrossWorkerCallbackRegistration:
    """The pricing module registers `_on_pricing_reload_event` against
    `shared_state._pubsub_callbacks` at import time so peer workers
    receive reload signals via the shared Redis pub/sub channel."""

    def test_callback_is_registered_at_import(self):
        from backend import shared_state
        assert pricing._on_pricing_reload_event in shared_state._pubsub_callbacks

    def test_event_constant_is_stable(self):
        # Wire-format string — changing it would silently break already
        # deployed peer workers listening for the old name during a
        # rolling restart, so it's a public contract.
        assert pricing.PRICING_RELOAD_EVENT == "pricing_reload"

    def test_callback_clears_local_cache(self):
        # Populate the cache then deliver a synthetic event.
        get_pricing("anthropic", "claude-opus-4-7")
        assert pricing._PRICING_CACHE is not None
        pricing._on_pricing_reload_event(pricing.PRICING_RELOAD_EVENT, {
            "origin_worker": "12345",
        })
        assert pricing._PRICING_CACHE is None

    def test_callback_ignores_unrelated_events(self):
        # The cross-worker bus carries multiple event types (e.g. "sse").
        # Pricing's callback must filter — receiving an "sse" event must
        # NOT clear the pricing cache.
        get_pricing("anthropic", "claude-opus-4-7")
        before = pricing._PRICING_CACHE
        assert before is not None
        pricing._on_pricing_reload_event("sse", {"origin_worker": "x"})
        assert pricing._PRICING_CACHE is before

    def test_callback_tolerates_non_dict_data(self):
        # Defensive: pubsub payload should always be a dict, but the
        # callback should not crash if a malformed message arrives.
        get_pricing("anthropic", "claude-opus-4-7")
        pricing._on_pricing_reload_event(pricing.PRICING_RELOAD_EVENT, None)  # type: ignore[arg-type]
        assert pricing._PRICING_CACHE is None


class TestReloadPricingEndpoint:
    """`POST /runtime/pricing/reload` reloads YAML on the calling worker
    and returns the loader status. Auth defaults to "open" mode in the
    test conftest so the admin gate is satisfied implicitly; the route
    object's dependencies are asserted separately so a future auth-mode
    change in the test harness does not silently drop the gate."""

    @pytest.mark.asyncio
    async def test_endpoint_returns_reload_status(self, client):
        # Prime cache with one lookup so we can verify the endpoint
        # actually invalidates it.
        get_pricing("anthropic", "claude-opus-4-7")
        assert pricing._PRICING_CACHE is not None

        resp = await client.post("/api/v1/runtime/pricing/reload")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "reloaded"
        assert body["loaded_from_yaml"] is True
        assert "anthropic" in body["providers"]
        assert "openai" in body["providers"]
        # metadata block from config/llm_pricing.yaml should round-trip.
        assert "schema_version" in body["metadata"]
        # No Redis in the test env, so the broadcast falls back to local.
        assert body["broadcast"] in ("redis_pubsub", "local_only")

    @pytest.mark.asyncio
    async def test_endpoint_picks_up_yaml_edits(self, client, tmp_path, monkeypatch):
        # Point pricing at a temporary YAML, post reload, observe the
        # new rates take effect for the next get_pricing() call.
        fake = tmp_path / "fake_pricing.yaml"
        fake.write_text(
            "providers:\n"
            "  testvendor:\n"
            "    test-model-v1:\n"
            "      input: 99.0\n"
            "      output: 999.0\n"
            "defaults:\n"
            "  input: 7.0\n"
            "  output: 11.0\n"
            "metadata:\n"
            "  schema_version: 1\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(pricing, "_PRICING_PATH", fake)

        resp = await client.post("/api/v1/runtime/pricing/reload")
        assert resp.status_code == 200, resp.text
        assert resp.json()["providers"] == ["testvendor"]
        assert get_pricing("testvendor", "test-model-v1") == (99.0, 999.0)
        assert get_pricing("unknown", "any") == (7.0, 11.0)

    @pytest.mark.asyncio
    async def test_endpoint_survives_yaml_missing_at_reload(
        self, client, tmp_path, monkeypatch,
    ):
        # If the YAML is removed between reloads (operator typo, deploy
        # slipping a file out from under the worker), the endpoint must
        # still return 200 — billing falls back to the hard-coded table.
        missing = tmp_path / "definitely_not_there.yaml"
        monkeypatch.setattr(pricing, "_PRICING_PATH", missing)

        resp = await client.post("/api/v1/runtime/pricing/reload")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "reloaded"
        assert body["loaded_from_yaml"] is False
        assert body["providers"] == []
        # Pre-Z.3 hard-coded fallback still serves Opus 4.7 at $5/$25.
        assert get_pricing(None, "claude-opus-4-7") == (5.0, 25.0)

    def test_endpoint_route_requires_admin(self):
        # Lock the dependency wiring directly — the test conftest
        # disables auth-mode enforcement so a request-level 401/403
        # won't appear, but the route definition must still attach the
        # admin gate so production deployments (auth-mode=session/
        # strict) reject non-admin callers.
        from backend.routers.system import router
        match = [
            r for r in router.routes
            if getattr(r, "path", None) == "/runtime/pricing/reload"
        ]
        assert len(match) == 1, "endpoint not registered exactly once"
        assert "POST" in match[0].methods
        dep_names = [
            getattr(d.dependency, "__name__", str(d.dependency))
            for d in match[0].dependencies
        ]
        # _REQUIRE_ADMIN at system.py:63 builds Depends(require_role("admin"))
        # whose inner is named `_dep`; current_user is the router-level
        # baseline. Both must be present.
        assert "current_user" in dep_names
        assert "_dep" in dep_names
