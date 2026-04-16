"""W10 #284 — Tests for the shared RUM adapter base + factory."""

from __future__ import annotations

import pytest

from backend import secret_store
from backend.observability import (
    ErrorEvent,
    GOOD_THRESHOLDS,
    KNOWN_VITALS,
    POOR_THRESHOLDS,
    RUMAdapter,
    RUMError,
    WebVital,
    classify_vital,
    derive_fingerprint,
    dsn_fingerprint,
    get_rum_adapter,
    list_providers,
)


class TestProviderFactory:

    def test_list_providers_enumerates_two(self):
        assert list_providers() == ["sentry", "datadog"]

    @pytest.mark.parametrize(
        "key,cls_name",
        [
            ("sentry", "SentryRUMAdapter"),
            ("sentry.io", "SentryRUMAdapter"),
            ("Sentry", "SentryRUMAdapter"),
            ("datadog", "DatadogRUMAdapter"),
            ("dd", "DatadogRUMAdapter"),
            ("datadog-rum", "DatadogRUMAdapter"),
            ("DATADOG", "DatadogRUMAdapter"),
        ],
    )
    def test_get_rum_adapter_resolves_known(self, key, cls_name):
        cls = get_rum_adapter(key)
        assert cls.__name__ == cls_name
        assert issubclass(cls, RUMAdapter)

    def test_get_rum_adapter_rejects_unknown(self):
        with pytest.raises(ValueError) as ei:
            get_rum_adapter("newrelic")
        assert "Unknown RUM provider" in str(ei.value)
        for p in list_providers():
            assert p in str(ei.value)

    def test_every_adapter_has_unique_provider_classvar(self):
        seen = set()
        for p in list_providers():
            cls = get_rum_adapter(p)
            assert cls.provider, f"{cls.__name__} missing provider classvar"
            assert cls.provider not in seen
            seen.add(cls.provider)


class TestDsnFingerprint:

    def test_short_secret_masks_completely(self):
        assert dsn_fingerprint("") == "****"
        assert dsn_fingerprint(None) == "****"
        assert dsn_fingerprint("abcd1234") == "****"

    def test_long_secret_shows_last_four(self):
        dsn = "https://abc123@sentry.io/0123WXYZ"
        fp = dsn_fingerprint(dsn)
        assert fp.endswith("WXYZ")
        assert dsn not in fp


class TestClassifyVital:

    @pytest.mark.parametrize(
        "name,value,expected",
        [
            # LCP — good ≤ 2500, poor > 4000
            ("LCP", 1500, "good"),
            ("LCP", 2500, "good"),
            ("LCP", 3000, "needs-improvement"),
            ("LCP", 4000, "needs-improvement"),
            ("LCP", 5500, "poor"),
            # INP — good ≤ 200, poor > 500
            ("INP", 100, "good"),
            ("INP", 350, "needs-improvement"),
            ("INP", 800, "poor"),
            # CLS — unitless
            ("CLS", 0.05, "good"),
            ("CLS", 0.15, "needs-improvement"),
            ("CLS", 0.5, "poor"),
            # TTFB
            ("TTFB", 500, "good"),
            ("TTFB", 1200, "needs-improvement"),
            ("TTFB", 2500, "poor"),
            # FCP
            ("FCP", 1500, "good"),
            ("FCP", 2500, "needs-improvement"),
            ("FCP", 4500, "poor"),
            # Unknown metric — bucketed as 'unknown'
            ("FOO", 0, "unknown"),
            # Lowercase still classified
            ("lcp", 1500, "good"),
        ],
    )
    def test_classify_vital_buckets_at_thresholds(self, name, value, expected):
        assert classify_vital(name, value) == expected

    def test_known_vitals_set_complete(self):
        assert set(KNOWN_VITALS) == set(GOOD_THRESHOLDS)
        assert set(KNOWN_VITALS) == set(POOR_THRESHOLDS)
        assert "FID" not in KNOWN_VITALS, "FID should be replaced by INP per Google 2024-03-12"


class TestDeriveFingerprint:

    def test_same_inputs_same_fingerprint(self):
        fp1 = derive_fingerprint(release="1.0.0", message="boom", stack="a.js:1:2")
        fp2 = derive_fingerprint(release="1.0.0", message="boom", stack="a.js:1:2")
        assert fp1 == fp2

    def test_different_release_different_fingerprint(self):
        fp1 = derive_fingerprint(release="1.0.0", message="boom", stack="a.js:1:2")
        fp2 = derive_fingerprint(release="1.0.1", message="boom", stack="a.js:1:2")
        assert fp1 != fp2

    def test_line_col_shift_does_not_change_fingerprint(self):
        fp1 = derive_fingerprint(release="1.0.0", message="boom", stack="a.js:1:2")
        fp2 = derive_fingerprint(release="1.0.0", message="boom", stack="a.js:42:100")
        assert fp1 == fp2

    def test_different_message_different_fingerprint(self):
        fp1 = derive_fingerprint(release="1.0.0", message="a", stack="x.js:1:2")
        fp2 = derive_fingerprint(release="1.0.0", message="b", stack="x.js:1:2")
        assert fp1 != fp2

    def test_empty_stack_still_returns_hash(self):
        fp = derive_fingerprint(release="1.0.0", message="boom", stack="")
        assert len(fp) == 40  # sha1 hex


class TestWebVital:

    def test_classify_on_construction(self):
        v = WebVital(name="lcp", value=2200)
        assert v.name == "LCP"
        assert v.rating == "good"

    def test_explicit_rating_preserved(self):
        v = WebVital(name="LCP", value=10_000, rating="good")
        assert v.rating == "good"  # don't overwrite caller's choice

    def test_timestamp_defaulted(self):
        v = WebVital(name="LCP", value=2200)
        assert v.timestamp > 0

    def test_to_dict_round_trip(self):
        v = WebVital(name="LCP", value=2200, page="/", session_id="s1")
        d = v.to_dict()
        assert d["name"] == "LCP"
        assert d["value"] == 2200
        assert d["page"] == "/"
        assert d["session_id"] == "s1"
        assert d["rating"] == "good"


class TestErrorEvent:

    def test_fingerprint_derived_on_construction(self):
        e = ErrorEvent(message="boom", release="1.0", stack="a.js:1:2")
        assert e.fingerprint
        assert len(e.fingerprint) == 40

    def test_explicit_fingerprint_preserved(self):
        e = ErrorEvent(message="boom", fingerprint="abc")
        assert e.fingerprint == "abc"

    def test_level_normalised_lowercase(self):
        e = ErrorEvent(message="boom", level="ERROR")
        assert e.level == "error"

    def test_to_dict_round_trip(self):
        e = ErrorEvent(message="boom", page="/x", level="error",
                       release="1.0", stack="a.js:1:2")
        d = e.to_dict()
        assert d["message"] == "boom"
        assert d["page"] == "/x"
        assert d["level"] == "error"
        assert d["release"] == "1.0"
        assert d["fingerprint"]


class TestEncryptedDsnFactory:

    def test_from_encrypted_dsn_decrypts(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-fixture-key-w10")
        secret_store._reset_for_tests()

        plain = "https://pubkey0123ABCD@o42.ingest.sentry.io/777"
        ct = secret_store.encrypt(plain)

        cls = get_rum_adapter("sentry")
        adapter = cls.from_encrypted_dsn(ct, environment="prod")
        assert adapter.provider == "sentry"
        assert adapter.dsn_fp().endswith("/777")  # last 4 chars of DSN
        assert plain not in adapter.dsn_fp()

    def test_from_encrypted_dsn_with_api_key_ciphertext(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-fixture-key-w10b")
        secret_store._reset_for_tests()

        dsn_plain = "ddrum_pub_clienttoken_ABCD"
        api_plain = "dd_apikey_secret_WXYZ"
        cls = get_rum_adapter("datadog")
        adapter = cls.from_encrypted_dsn(
            secret_store.encrypt(dsn_plain),
            api_key_ciphertext=secret_store.encrypt(api_plain),
            application_id="app-uuid-1",
            environment="prod",
        )
        assert adapter.dsn_fp().endswith("ABCD")
        assert adapter.api_key_fp().endswith("WXYZ")

    def test_from_plaintext_dsn(self):
        cls = get_rum_adapter("sentry")
        a = cls.from_plaintext_dsn(
            "https://k@o1.ingest.sentry.io/2",
            environment="prod",
        )
        assert a.provider == "sentry"


class TestSamplingGate:

    def _mk(self, sample_rate):
        cls = get_rum_adapter("sentry")
        return cls(dsn="https://k@o1.ingest.sentry.io/2",
                   environment="prod", sample_rate=sample_rate)

    def test_full_sample_always_emits(self):
        a = self._mk(1.0)
        assert all(a._should_sample() for _ in range(50))

    def test_zero_sample_never_emits(self):
        a = self._mk(0.0)
        assert not any(a._should_sample() for _ in range(50))

    def test_invalid_sample_rate_rejected(self):
        cls = get_rum_adapter("sentry")
        with pytest.raises(ValueError):
            cls(dsn="https://k@o1.ingest.sentry.io/2", sample_rate=-0.1)
        with pytest.raises(ValueError):
            cls(dsn="https://k@o1.ingest.sentry.io/2", sample_rate=1.5)

    def test_partial_sample_eventually_yields_true_and_false(self, monkeypatch):
        # Deterministic using monkeypatched random.
        a = self._mk(0.5)
        import random as _r
        seq = iter([0.1, 0.9, 0.4, 0.6])
        monkeypatch.setattr(_r, "random", lambda: next(seq))
        results = [a._should_sample() for _ in range(4)]
        assert results == [True, False, True, False]


class TestInterfaceContract:

    @pytest.mark.parametrize("provider", ["sentry", "datadog"])
    def test_required_methods_present(self, provider):
        cls = get_rum_adapter(provider)
        for name in ("send_vital", "send_error", "browser_snippet"):
            assert callable(getattr(cls, name)), f"{cls.__name__} missing {name}"

    def test_cannot_instantiate_base_directly(self):
        with pytest.raises(TypeError):
            RUMAdapter(dsn="x")  # type: ignore[abstract]

    def test_missing_provider_classvar_rejected(self):
        class Broken(RUMAdapter):
            provider = ""
            async def send_vital(self, vital):
                return None
            async def send_error(self, event):
                return None
            def browser_snippet(self):
                return ""
        with pytest.raises(ValueError):
            Broken(dsn="x")


class TestRUMErrorTaxonomy:

    def test_error_carries_status_and_provider(self):
        exc = RUMError("boom", status=500, provider="sentry")
        assert exc.status == 500
        assert exc.provider == "sentry"
        assert "boom" in str(exc)
