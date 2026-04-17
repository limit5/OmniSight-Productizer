"""P10 #295 — Tests for the shared mobile observability base + factory."""

from __future__ import annotations

import pytest

from backend.mobile_observability import (
    GOOD_RENDER_MS,
    HANG_KINDS,
    HangEvent,
    KNOWN_PLATFORMS,
    KNOWN_RENDER_METRICS,
    MobileCrash,
    MobileObservabilityAdapter,
    MobileObservabilityError,
    POOR_RENDER_MS,
    RenderMetric,
    classify_render,
    derive_fingerprint,
    dsn_fingerprint,
    get_mobile_adapter,
    list_providers,
)


# ── Provider factory ────────────────────────────────────────────


class TestProviderFactory:

    def test_list_providers_enumerates_two(self):
        assert list_providers() == ["firebase-crashlytics", "sentry-mobile"]

    @pytest.mark.parametrize(
        "key,cls_name",
        [
            ("firebase-crashlytics", "FirebaseCrashlyticsAdapter"),
            ("crashlytics", "FirebaseCrashlyticsAdapter"),
            ("firebase", "FirebaseCrashlyticsAdapter"),
            ("FIREBASE", "FirebaseCrashlyticsAdapter"),
            ("google_firebase_crashlytics", "FirebaseCrashlyticsAdapter"),
            ("sentry-mobile", "SentryMobileAdapter"),
            ("sentry", "SentryMobileAdapter"),
            ("Sentry-Mobile", "SentryMobileAdapter"),
        ],
    )
    def test_get_mobile_adapter_resolves_known(self, key, cls_name):
        cls = get_mobile_adapter(key)
        assert cls.__name__ == cls_name
        assert issubclass(cls, MobileObservabilityAdapter)

    def test_get_mobile_adapter_rejects_unknown(self):
        with pytest.raises(ValueError) as ei:
            get_mobile_adapter("bugsnag")
        assert "Unknown mobile observability provider" in str(ei.value)
        for p in list_providers():
            assert p in str(ei.value)

    def test_every_adapter_has_unique_provider_classvar(self):
        seen = set()
        for p in list_providers():
            cls = get_mobile_adapter(p)
            assert cls.provider, f"{cls.__name__} missing provider classvar"
            assert cls.provider not in seen
            seen.add(cls.provider)


# ── Render thresholds ───────────────────────────────────────────


class TestRenderClassification:

    def test_known_metric_count(self):
        assert KNOWN_RENDER_METRICS == ("frame_draw", "frame_total", "ttid", "ttfd", "hang")

    @pytest.mark.parametrize(
        "name,value,expected",
        [
            ("frame_draw", 10, "good"),
            ("frame_draw", 16, "good"),
            ("frame_draw", 20, "needs-improvement"),
            ("frame_draw", 33, "needs-improvement"),
            ("frame_draw", 50, "poor"),
            ("hang", 100, "good"),
            ("hang", 500, "needs-improvement"),
            ("hang", 1500, "poor"),
            ("ttid", 800, "good"),
            ("ttid", 1500, "needs-improvement"),
            ("ttid", 5000, "poor"),
        ],
    )
    def test_classify_render_buckets_correctly(self, name, value, expected):
        assert classify_render(name, value) == expected

    def test_unknown_metric_returns_unknown(self):
        assert classify_render("not_a_thing", 1.0) == "unknown"

    def test_thresholds_monotonic(self):
        for k in KNOWN_RENDER_METRICS:
            assert GOOD_RENDER_MS[k] < POOR_RENDER_MS[k]


# ── Fingerprinting ──────────────────────────────────────────────


class TestFingerprint:

    def test_fingerprint_stable_across_line_col_shifts(self):
        a = derive_fingerprint(release="1.0", message="boom",
                               stack="at app.kt:12:5")
        b = derive_fingerprint(release="1.0", message="boom",
                               stack="at app.kt:99:7")
        assert a == b

    def test_fingerprint_drops_hex_addresses(self):
        a = derive_fingerprint(release="1.0", message="boom",
                               stack="0xdeadbeef foo()")
        b = derive_fingerprint(release="1.0", message="boom",
                               stack="0xcafebabe foo()")
        assert a == b

    def test_fingerprint_changes_with_release(self):
        a = derive_fingerprint(release="1.0", message="boom", stack="frame")
        b = derive_fingerprint(release="2.0", message="boom", stack="frame")
        assert a != b

    def test_fingerprint_changes_with_message(self):
        a = derive_fingerprint(release="1.0", message="boom", stack="frame")
        b = derive_fingerprint(release="1.0", message="bang", stack="frame")
        assert a != b

    def test_fingerprint_handles_empty_stack(self):
        # No frame is fine.
        f = derive_fingerprint(release="1.0", message="boom", stack="")
        assert isinstance(f, str) and len(f) == 40

    def test_dsn_fingerprint_redacts(self):
        assert dsn_fingerprint(None) == "****"
        assert dsn_fingerprint("") == "****"
        assert dsn_fingerprint("short") == "****"
        assert dsn_fingerprint("some-long-token-1234") == "…1234"


# ── MobileCrash ─────────────────────────────────────────────────


class TestMobileCrash:

    def test_default_platform_android(self):
        c = MobileCrash(message="boom")
        assert c.platform == "android"
        assert c.timestamp > 0
        assert len(c.fingerprint) == 40

    def test_known_platforms_accepted(self):
        for p in KNOWN_PLATFORMS:
            c = MobileCrash(message="boom", platform=p)
            assert c.platform == p

    def test_unknown_platform_rejected(self):
        with pytest.raises(ValueError):
            MobileCrash(message="boom", platform="symbian")

    def test_explicit_fingerprint_preserved(self):
        c = MobileCrash(message="boom", fingerprint="custom-fp")
        assert c.fingerprint == "custom-fp"

    def test_to_dict_round_trip(self):
        c = MobileCrash(message="boom", platform="ios", signal="SIGSEGV",
                        app_version="1.0", os_version="17.4",
                        device_model="iPhone 15")
        d = c.to_dict()
        assert d["platform"] == "ios"
        assert d["signal"] == "SIGSEGV"
        assert d["app_version"] == "1.0"
        assert d["fingerprint"] == c.fingerprint


# ── HangEvent ───────────────────────────────────────────────────


class TestHangEvent:

    def test_anr_severity_thresholds(self):
        assert HangEvent(duration_ms=100, kind="anr").severity == "info"
        assert HangEvent(duration_ms=5_000, kind="anr").severity == "warning"
        assert HangEvent(duration_ms=10_000, kind="anr").severity == "critical"
        assert HangEvent(duration_ms=15_000, kind="anr").severity == "critical"

    def test_watchdog_termination_always_critical(self):
        # iOS watchdog kill = process is gone, regardless of duration.
        ev = HangEvent(duration_ms=0, kind="watchdog_termination", platform="ios")
        assert ev.severity == "critical"

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError):
            HangEvent(duration_ms=100, kind="bsod")

    def test_negative_duration_rejected(self):
        with pytest.raises(ValueError):
            HangEvent(duration_ms=-1, kind="anr")

    def test_kind_constants(self):
        assert HANG_KINDS == ("anr", "watchdog_termination")

    def test_fingerprint_includes_kind(self):
        a = HangEvent(duration_ms=5_000, kind="anr", main_thread_stack="frame")
        b = HangEvent(duration_ms=5_000, kind="watchdog_termination",
                      platform="ios", main_thread_stack="frame")
        assert a.fingerprint != b.fingerprint

    def test_to_dict_includes_severity(self):
        ev = HangEvent(duration_ms=6_000, kind="anr")
        d = ev.to_dict()
        assert d["severity"] == "warning"
        assert d["kind"] == "anr"


# ── RenderMetric ────────────────────────────────────────────────


class TestRenderMetric:

    def test_default_rating_derived_from_value(self):
        m = RenderMetric(name="frame_draw", value=20)
        assert m.rating == "needs-improvement"

    def test_explicit_rating_preserved(self):
        m = RenderMetric(name="frame_draw", value=20, rating="custom")
        assert m.rating == "custom"

    def test_unknown_metric_rating_is_unknown(self):
        m = RenderMetric(name="custom_metric", value=1)
        assert m.rating == "unknown"

    def test_unknown_platform_rejected(self):
        with pytest.raises(ValueError):
            RenderMetric(name="frame_draw", value=20, platform="symbian")

    def test_to_dict_round_trip(self):
        m = RenderMetric(name="hang", value=500, platform="ios", screen="/feed")
        d = m.to_dict()
        assert d["name"] == "hang"
        assert d["platform"] == "ios"
        assert d["screen"] == "/feed"
        assert d["rating"] == "needs-improvement"


# ── Adapter base contract ───────────────────────────────────────


class _DummyAdapter(MobileObservabilityAdapter):
    provider = "dummy"

    async def send_crash(self, crash):
        return None

    async def send_hang(self, hang):
        return None

    async def send_render(self, metric):
        return None

    def native_snippet(self, platform):
        return f"// dummy {platform}"


class TestAdapterBase:

    def test_missing_provider_classvar_rejected(self):
        class Bad(MobileObservabilityAdapter):
            async def send_crash(self, c): pass
            async def send_hang(self, h): pass
            async def send_render(self, m): pass
            def native_snippet(self, p): return ""

        with pytest.raises(ValueError):
            Bad()

    def test_invalid_sample_rate_rejected(self):
        with pytest.raises(ValueError):
            _DummyAdapter(sample_rate=1.1)
        with pytest.raises(ValueError):
            _DummyAdapter(sample_rate=-0.1)

    def test_dsn_fp_redacts(self):
        a = _DummyAdapter(dsn="some-dsn-1234")
        assert a.dsn_fp() == "…1234"
        assert a.api_key_fp() == "****"

    def test_should_sample_zero_always_false(self):
        a = _DummyAdapter(sample_rate=0.0)
        assert a._should_sample() is False

    def test_should_sample_one_always_true(self):
        a = _DummyAdapter(sample_rate=1.0)
        assert a._should_sample() is True

    def test_from_plaintext_dsn_passes_dsn_through(self):
        a = _DummyAdapter.from_plaintext_dsn(dsn="x" * 32)
        assert a._dsn == "x" * 32
