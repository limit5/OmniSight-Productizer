"""Z.3 (#292) checkbox 3 + 4 + 5 + 6 — fallback, reload endpoint, snapshot
endpoint, and YAML-corrupt boot-resilience / cross-worker reload tests.

Scope is the behaviour added by Z.3:
    - checkbox 3: fallback chain + throttled WARNING emission per arm.
    - checkbox 4: `POST /runtime/pricing/reload` endpoint + the
      cross-worker `pricing_reload` event callback that clears each
      peer worker's local cache.
    - checkbox 5: `GET /runtime/pricing` read-only snapshot helper.
    - checkbox 6: corrupt / malformed YAML does not crash startup or
      reload (falls back to the pre-Z.3 hard-coded dict); POST reload
      invokes `publish_cross_worker` with the documented payload and
      the Redis-pubsub dispatch path clears peer-worker caches
      end-to-end.
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Z.3 checkbox 5 (#292) — GET /runtime/pricing snapshot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPricingTableHelper:
    """The `get_pricing_table()` helper that backs the GET endpoint."""

    def test_returns_full_provider_map(self):
        table = pricing.get_pricing_table()
        # Live YAML ships 9 providers (anthropic / openai / google / xai
        # / groq / deepseek / together / openrouter / ollama).
        assert "providers" in table
        assert "anthropic" in table["providers"]
        assert "openai" in table["providers"]
        assert "ollama" in table["providers"]

    def test_rate_pairs_serialize_as_input_output_dicts(self):
        # Tuples in the cache must surface as labelled dicts so the JSON
        # wire format is self-documenting (a bare 2-element list would
        # lose the in/out distinction).
        opus = pricing.get_pricing_table()["providers"]["anthropic"]["claude-opus-4-7"]
        assert opus == {"input": 5.0, "output": 25.0}

    def test_provider_default_row_included(self):
        # `_default` rows are part of the lookup chain (provider known +
        # model unknown), so the snapshot must surface them too.
        anthropic = pricing.get_pricing_table()["providers"]["anthropic"]
        assert "_default" in anthropic
        assert anthropic["_default"] == {"input": 3.0, "output": 15.0}

    def test_global_defaults_present(self):
        defaults = pricing.get_pricing_table()["defaults"]
        assert defaults == {"input": 1.0, "output": 3.0}

    def test_metadata_carries_updated_at_and_source(self):
        meta = pricing.get_pricing_table()["metadata"]
        assert "updated_at" in meta
        assert "source" in meta
        # source is documented as a URL pointing at the upstream issue
        # / vendor page; locking the issue URL specifically would be too
        # tight, but the substring is a reasonable smoke check.
        assert "github.com" in str(meta["source"]) or "http" in str(meta["source"])

    def test_loaded_from_yaml_true_under_normal_boot(self):
        assert pricing.get_pricing_table()["loaded_from_yaml"] is True

    def test_loaded_from_yaml_false_when_yaml_missing(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope.yaml"
        monkeypatch.setattr(pricing, "_PRICING_PATH", missing)
        reset_cache_for_tests()
        table = pricing.get_pricing_table()
        assert table["loaded_from_yaml"] is False
        # YAML failed → providers map is empty (the hard-coded fallback
        # is reachable via get_pricing(), but the snapshot intentionally
        # surfaces the empty cache so a dashboard can flag the degraded
        # state instead of silently showing only 8 pre-Z.3 models).
        assert table["providers"] == {}
        # Boot-safety global default still present.
        assert table["defaults"] == {"input": 1.0, "output": 3.0}


class TestPricingSnapshotEndpoint:
    """`GET /runtime/pricing` — read-only snapshot for dashboards / operator."""

    @pytest.mark.asyncio
    async def test_endpoint_returns_table_and_metadata(self, client):
        resp = await client.get("/api/v1/runtime/pricing")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # All four top-level keys present.
        assert set(body.keys()) >= {
            "providers", "defaults", "metadata", "loaded_from_yaml",
        }
        # Live YAML loaded successfully under the test fixture.
        assert body["loaded_from_yaml"] is True

        # Spot-check that the live table is in the response.
        assert "anthropic" in body["providers"]
        assert body["providers"]["anthropic"]["claude-opus-4-7"] == {
            "input": 5.0, "output": 25.0,
        }

        # The headline `updated_at` + `source` fields the spec calls out.
        assert "updated_at" in body["metadata"]
        assert "source" in body["metadata"]

    @pytest.mark.asyncio
    async def test_endpoint_reflects_post_reload_changes(
        self, client, tmp_path, monkeypatch,
    ):
        # GET-after-POST: edit the YAML behind the scenes, POST the
        # reload endpoint, then GET must return the new table — locks
        # the operator workflow ("edit YAML → POST reload → GET to
        # verify") end-to-end.
        fake = tmp_path / "fake_pricing.yaml"
        fake.write_text(
            "providers:\n"
            "  testvendor:\n"
            "    test-model-v2:\n"
            "      input: 42.0\n"
            "      output: 84.0\n"
            "defaults:\n"
            "  input: 2.0\n"
            "  output: 4.0\n"
            "metadata:\n"
            "  updated_at: 2099-01-01\n"
            "  source: https://example.invalid/pricing\n"
            "  schema_version: 1\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(pricing, "_PRICING_PATH", fake)

        reload_resp = await client.post("/api/v1/runtime/pricing/reload")
        assert reload_resp.status_code == 200, reload_resp.text

        get_resp = await client.get("/api/v1/runtime/pricing")
        assert get_resp.status_code == 200, get_resp.text
        body = get_resp.json()
        assert body["providers"] == {
            "testvendor": {"test-model-v2": {"input": 42.0, "output": 84.0}},
        }
        assert body["defaults"] == {"input": 2.0, "output": 4.0}
        assert body["metadata"]["updated_at"] == "2099-01-01"
        assert body["metadata"]["source"] == "https://example.invalid/pricing"

    @pytest.mark.asyncio
    async def test_endpoint_surfaces_degraded_state_when_yaml_missing(
        self, client, tmp_path, monkeypatch,
    ):
        # If the YAML disappears (operator typo, deploy slipping a file
        # out from under the worker), the snapshot must still return 200
        # and surface `loaded_from_yaml: False` so a dashboard can render
        # a banner instead of silently showing the boot-safety fallback.
        missing = tmp_path / "definitely_not_there.yaml"
        monkeypatch.setattr(pricing, "_PRICING_PATH", missing)
        reset_cache_for_tests()

        resp = await client.get("/api/v1/runtime/pricing")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["loaded_from_yaml"] is False
        assert body["providers"] == {}
        # Boot-safety global default still rendered so the dashboard
        # has something to display in the "global" row.
        assert body["defaults"] == {"input": 1.0, "output": 3.0}

    def test_endpoint_route_does_not_require_admin(self):
        # Lock the dependency wiring directly — pricing is non-sensitive
        # informational data; the GET endpoint must NOT stack the admin
        # gate (matches peer GETs like /runtime/info, /runtime/status).
        # The router-level current_user dep must still be attached.
        from backend.routers.system import router
        match = [
            r for r in router.routes
            if getattr(r, "path", None) == "/runtime/pricing"
        ]
        assert len(match) == 1, "endpoint not registered exactly once"
        assert "GET" in match[0].methods
        dep_names = [
            getattr(d.dependency, "__name__", str(d.dependency))
            for d in match[0].dependencies
        ]
        # `current_user` is the router-level baseline (every route gets
        # it). `_dep` is the closure produced by `require_role("admin")`
        # in `_REQUIRE_ADMIN`; the GET endpoint must NOT carry it.
        assert "current_user" in dep_names
        assert "_dep" not in dep_names


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Z.3 checkbox 6 (#292) — YAML-corrupt boot resilience +
#                          cross-worker reload integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Three-part contract:
#
#   1. YAML 格式錯 → 啟動時不 crash 退回硬寫 dict
#      A malformed config/llm_pricing.yaml must never bring the worker
#      down. `_load_pricing()` catches every YAML/OS error and falls
#      back to `_HARD_CODED_FALLBACK` (bit-identical to the pre-Z.3
#      dict that lived at routers/system.py:1094-1103) so the 8 legacy
#      models keep billing at their historical rates. Anything else
#      gets `_HARD_CODED_GLOBAL_DEFAULT = (1.0, 3.0)`.
#
#   2. Reload endpoint correctness under corrupt YAML
#      `POST /api/v1/runtime/pricing/reload` must return 200 with
#      `loaded_from_yaml: False` and `providers: []` when the YAML on
#      disk is unparseable, and the next `get_pricing()` call must
#      still resolve to a hard-coded rate.
#
#   3. 跨 worker reload 同步 (Redis pub/sub end-to-end)
#      POST reload must invoke `publish_cross_worker(PRICING_RELOAD_EVENT,
#      {"origin_worker": str(pid)})`. The dispatch path matching
#      shared_state.start_pubsub_listener — JSON decode of the pubsub
#      payload, for-loop over `_pubsub_callbacks` — must deliver the
#      event to `_on_pricing_reload_event` and clear the local cache.


class TestYamlCorruptBootResilience:
    """Z.3 checkbox 6, part 1.

    Corrupt / malformed YAML must not crash the worker at boot OR at
    reload time. The hard-coded fallback dict keeps billing alive for
    the 8 pre-Z.3 models and the global default keeps unknown-model
    billing visible in dashboards rather than silently charging zero.
    """

    @pytest.mark.parametrize(
        "label,body",
        [
            # yaml.ScannerError — embedded tab in block scope is a
            # classic operator typo when hand-editing indentation.
            ("scanner_error_tab_indent",
             "providers:\n\tanthropic: bad-tab"),
            # yaml.ParserError — unclosed flow mapping.
            ("parser_error_unclosed_brace",
             "providers: {anthropic: {input: 5, output: 25"),
            # yaml.ScannerError — garbage tokens that fail the tokenizer.
            ("scanner_error_garbage",
             "@@@ !!! ???\nnot: : valid"),
            # Root is a scalar, not a dict — load succeeds but shape is
            # wrong. The loader must reject rather than IndexError on
            # subsequent `.get()` calls.
            ("root_is_scalar",
             "just a pricing string"),
            # Root is a list, not a dict. Same as above.
            ("root_is_list",
             "- claude-opus-4-7\n- claude-sonnet-4"),
            # `providers` is a list, not a mapping. Individual model
            # rows can't be coerced; coerce-step must skip silently.
            ("providers_is_list",
             "providers:\n  - anthropic\n  - openai"),
            # Rate pair values are strings — `_coerce_rate_pair` must
            # reject without propagating a ValueError up the stack.
            ("rate_values_are_strings",
             "providers:\n  anthropic:\n    claude-opus-4-7:\n"
             "      input: 'five'\n      output: 'twenty-five'\n"),
            # Empty file → yaml.safe_load returns None → loader sees
            # a non-dict root.
            ("empty_file", ""),
            # Truncated mid-mapping.
            ("truncated",
             "providers:\n  anthropic:\n    claude-opus-4-7:\n"
             "      input: 5\n      output:"),
        ],
    )
    def test_corrupt_yaml_does_not_crash_and_falls_back_to_hardcoded_dict(
        self, label, body, tmp_path, monkeypatch, caplog,
    ):
        # Simulate the "startup" path: patch the YAML path to a corrupt
        # file, then make the first `get_pricing()` call. This mirrors
        # what each uvicorn worker would do on cold start if someone
        # shipped a broken config.
        broken = tmp_path / f"broken_{label}.yaml"
        broken.write_bytes(body.encode("utf-8"))
        monkeypatch.setattr(pricing, "_PRICING_PATH", broken)
        reset_cache_for_tests()

        caplog.set_level(logging.WARNING, logger="backend.pricing")

        # Every pre-Z.3 model must still bill at its historical rate.
        for model, expected in TestPriceNeutralityAcrossFallbacks.EXPECT.items():
            assert get_pricing(None, model) == expected, (
                f"{label}: model {model} did not bit-identically resolve "
                f"through _HARD_CODED_FALLBACK after corrupt YAML"
            )

        # An unknown model under an unknown provider falls through to
        # the hard-coded global default, not zero-zero.
        assert get_pricing("no-such-vendor", "no-such-model") == (1.0, 3.0)
        # provider=None + unknown model: same fallback.
        assert get_pricing(None, "no-such-model-either") == (1.0, 3.0)

    def test_corrupt_yaml_leaves_loaded_from_yaml_false(
        self, tmp_path, monkeypatch,
    ):
        broken = tmp_path / "broken.yaml"
        broken.write_text(
            "providers: {anthropic: {input: 5, output: 25",
            encoding="utf-8",
        )
        monkeypatch.setattr(pricing, "_PRICING_PATH", broken)
        reset_cache_for_tests()
        table = pricing.get_pricing_table()
        # Degraded state must be observable on the snapshot so a
        # dashboard can flag it instead of silently rendering the
        # hard-coded 8-model fallback.
        assert table["loaded_from_yaml"] is False
        assert table["providers"] == {}
        assert table["defaults"] == {"input": 1.0, "output": 3.0}

    def test_corrupt_yaml_load_does_not_raise(
        self, tmp_path, monkeypatch,
    ):
        # Explicit negative: `_load_pricing` must return cleanly rather
        # than letting a yaml.YAMLError or AttributeError bubble up
        # (either would crash the first `track_tokens` call on boot).
        broken = tmp_path / "broken.yaml"
        broken.write_text("@@@ !!! ???\nnot: : valid", encoding="utf-8")
        monkeypatch.setattr(pricing, "_PRICING_PATH", broken)
        reset_cache_for_tests()
        # Would raise on master before checkbox 2's defensive load.
        cache = pricing._load_pricing()
        assert cache["_loaded_from_yaml"] is False
        assert cache["providers"] == {}
        # Loader also logs a WARNING describing the failure so an
        # operator scraping logs knows the YAML needs fixing.

    @pytest.mark.asyncio
    async def test_reload_endpoint_survives_corrupt_yaml(
        self, client, tmp_path, monkeypatch,
    ):
        # Existing `test_endpoint_survives_yaml_missing_at_reload` covers
        # MISSING; this covers CORRUPT (operator ssh'd in, typo'd the
        # YAML, now POSTs reload). Must return 200 with the degraded
        # status instead of 500-ing out.
        broken = tmp_path / "broken.yaml"
        broken.write_text(
            "providers:\n\tanthropic: bad-tab",  # embedded tab → ScannerError
            encoding="utf-8",
        )
        monkeypatch.setattr(pricing, "_PRICING_PATH", broken)

        resp = await client.post("/api/v1/runtime/pricing/reload")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "reloaded"
        assert body["loaded_from_yaml"] is False
        assert body["providers"] == []
        # After reload, get_pricing must still serve the hard-coded rate
        # for pre-Z.3 models — this is the "退回硬寫 dict" contract.
        assert get_pricing(None, "claude-opus-4-7") == (5.0, 25.0)
        assert get_pricing(None, "deepseek-chat") == (0.14, 0.28)


class TestCrossWorkerReloadIntegration:
    """Z.3 checkbox 6, part 3.

    POST /runtime/pricing/reload must fan out to peer workers via
    Redis pub/sub. We verify three surfaces:
        - the endpoint calls `publish_cross_worker(PRICING_RELOAD_EVENT,
          {"origin_worker": str(pid)})`;
        - the response's `broadcast` field reflects publish success;
        - the shared_state listener dispatch path (JSON-decode →
          `_pubsub_callbacks` fan-out) delivers the event to
          `_on_pricing_reload_event` and clears the peer worker's cache.
    """

    @pytest.mark.asyncio
    async def test_post_reload_invokes_publish_with_documented_payload(
        self, client, monkeypatch,
    ):
        # Capture the args that the endpoint passes to publish_cross_worker
        # so a contract regression (wrong event name, missing origin
        # worker field) would surface immediately.
        calls: list[tuple[str, dict]] = []

        def fake_publish(event, data):
            calls.append((event, dict(data)))
            return True  # pretend Redis accepted the publish

        from backend.routers import system as _system
        monkeypatch.setattr(_system, "publish_cross_worker", fake_publish, raising=False)
        # The endpoint imports `publish_cross_worker` locally inside the
        # handler (`from backend.shared_state import publish_cross_worker`)
        # so we also patch the source module for the import to find.
        from backend import shared_state as _sst
        monkeypatch.setattr(_sst, "publish_cross_worker", fake_publish)

        resp = await client.post("/api/v1/runtime/pricing/reload")
        assert resp.status_code == 200, resp.text
        assert resp.json()["broadcast"] == "redis_pubsub"

        assert len(calls) == 1, "endpoint should publish exactly once per POST"
        event, data = calls[0]
        assert event == pricing.PRICING_RELOAD_EVENT == "pricing_reload"
        # origin_worker is documented as str(os.getpid()) so peer
        # workers can correlate reload events with log lines.
        assert "origin_worker" in data
        assert data["origin_worker"].isdigit(), (
            f"origin_worker should be str(pid); got {data['origin_worker']!r}"
        )

    @pytest.mark.asyncio
    async def test_post_reload_reports_local_only_when_publish_fails(
        self, client, monkeypatch,
    ):
        # If Redis is unavailable / publish fails, the endpoint must
        # still return 200 (the local worker reloaded fine) but the
        # `broadcast` field must surface "local_only" so operators
        # know peer workers did NOT invalidate and a rolling restart
        # may be required to complete the rollout.
        from backend import shared_state as _sst
        monkeypatch.setattr(_sst, "publish_cross_worker", lambda e, d: False)

        resp = await client.post("/api/v1/runtime/pricing/reload")
        assert resp.status_code == 200, resp.text
        assert resp.json()["broadcast"] == "local_only"

    def test_listener_dispatch_path_clears_peer_cache_end_to_end(self):
        # End-to-end simulation of shared_state.start_pubsub_listener:
        # (1) worker A publishes JSON payload → (2) listener on worker B
        # decodes and fans out to _pubsub_callbacks → (3) pricing's
        # registered callback invalidates B's local cache.
        import json as _json

        from backend import shared_state

        # Prime peer-worker cache.
        get_pricing("anthropic", "claude-opus-4-7")
        assert pricing._PRICING_CACHE is not None

        # Build the exact payload shape that `publish_cross_worker`
        # writes to Redis (see shared_state.publish_cross_worker: body
        # is `json.dumps({"event": event, "data": data})`).
        wire_payload = _json.dumps({
            "event": pricing.PRICING_RELOAD_EVENT,
            "data": {"origin_worker": "99999"},
        })

        # Replay the listener's decode-and-dispatch block verbatim.
        # (shared_state.start_pubsub_listener lines ~554-563.)
        decoded = _json.loads(wire_payload)
        for cb in shared_state._pubsub_callbacks:
            cb(decoded["event"], decoded["data"])

        # Peer worker's cache is invalidated; next lookup re-reads.
        assert pricing._PRICING_CACHE is None

    def test_listener_dispatch_ignores_other_event_types(self):
        # Confirm the listener fan-out path itself does not clear the
        # pricing cache for unrelated events (e.g. "sse" events used by
        # the events bus). Guards against accidentally widening the
        # trigger if a future contributor removes the event-name check
        # in `_on_pricing_reload_event`.
        import json as _json

        from backend import shared_state

        get_pricing("anthropic", "claude-opus-4-7")
        before = pricing._PRICING_CACHE
        assert before is not None

        wire_payload = _json.dumps({
            "event": "sse",
            "data": {"type": "heartbeat"},
        })
        decoded = _json.loads(wire_payload)
        for cb in shared_state._pubsub_callbacks:
            cb(decoded["event"], decoded["data"])

        assert pricing._PRICING_CACHE is before

    @pytest.mark.asyncio
    async def test_round_trip_post_reload_then_listener_dispatch(
        self, client, tmp_path, monkeypatch,
    ):
        # Full two-worker simulation: POST hits worker A, publish is
        # intercepted (no real Redis in tests), payload is fed through
        # the listener dispatch on "worker B" (same process, same
        # callback list). B's cache must be invalidated AND its next
        # get_pricing() call must read from the temp YAML that A
        # reloaded from.
        import json as _json

        from backend import shared_state

        published: list[str] = []

        def capturing_publish(event, data):
            published.append(_json.dumps({"event": event, "data": data}))
            return True

        monkeypatch.setattr(shared_state, "publish_cross_worker", capturing_publish)

        # Point both "workers" at a new YAML with a recognisable rate.
        new_yaml = tmp_path / "new_rates.yaml"
        new_yaml.write_text(
            "providers:\n"
            "  roundtripvendor:\n"
            "    rt-model-v1:\n"
            "      input: 123.0\n"
            "      output: 456.0\n"
            "defaults:\n"
            "  input: 1.0\n"
            "  output: 3.0\n"
            "metadata:\n"
            "  schema_version: 1\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(pricing, "_PRICING_PATH", new_yaml)

        # Pre-populate "worker B" cache with an old state.
        get_pricing("anthropic", "claude-opus-4-7")  # cached under old path
        # Worker A posts reload.
        resp = await client.post("/api/v1/runtime/pricing/reload")
        assert resp.status_code == 200, resp.text
        assert len(published) == 1, "exactly one broadcast per POST"

        # Simulate listener on "worker B": decode + fan out.
        decoded = _json.loads(published[0])
        assert decoded["event"] == pricing.PRICING_RELOAD_EVENT
        for cb in shared_state._pubsub_callbacks:
            cb(decoded["event"], decoded["data"])

        # Worker B's cache was invalidated; next lookup re-reads the
        # new YAML and sees the rt-model-v1 rate. This is the
        # operator-visible contract: edit YAML → POST reload → all
        # workers bill at new rate without a rolling restart.
        assert get_pricing("roundtripvendor", "rt-model-v1") == (123.0, 456.0)
