"""W9 #283 — Tests for the shared CMS adapter base + factory."""

from __future__ import annotations

import pytest

from backend import secret_store
from backend.cms import (
    CMSEntry,
    CMSError,
    CMSSource,
    CMSWebhookEvent,
    constant_time_equals,
    get_cms_source,
    hmac_sha256_hex,
    list_providers,
    token_fingerprint,
)


class TestProviderFactory:

    def test_list_providers_enumerates_four(self):
        assert list_providers() == ["sanity", "strapi", "contentful", "directus"]

    @pytest.mark.parametrize(
        "key,cls_name",
        [
            ("sanity", "SanityCMSSource"),
            ("sanity.io", "SanityCMSSource"),
            ("strapi", "StrapiCMSSource"),
            ("contentful", "ContentfulCMSSource"),
            ("cf", "ContentfulCMSSource"),
            ("directus", "DirectusCMSSource"),
            ("DIRECTUS", "DirectusCMSSource"),
            ("Strapi", "StrapiCMSSource"),
        ],
    )
    def test_get_cms_source_resolves_known(self, key, cls_name):
        cls = get_cms_source(key)
        assert cls.__name__ == cls_name
        assert issubclass(cls, CMSSource)

    def test_get_cms_source_rejects_unknown(self):
        with pytest.raises(ValueError) as excinfo:
            get_cms_source("wordpress")
        assert "Unknown CMS provider" in str(excinfo.value)
        for p in list_providers():
            assert p in str(excinfo.value)

    def test_every_adapter_has_unique_provider_classvar(self):
        seen = set()
        for p in list_providers():
            cls = get_cms_source(p)
            assert cls.provider, f"{cls.__name__} missing provider classvar"
            assert cls.provider not in seen
            seen.add(cls.provider)


class TestTokenFingerprint:

    def test_masks_short_tokens(self):
        assert token_fingerprint("") == "****"
        assert token_fingerprint(None) == "****"
        assert token_fingerprint("abcd1234") == "****"

    def test_shows_last_four_for_long_tokens(self):
        token = "sk_" + "x" * 40 + "WXYZ"
        fp = token_fingerprint(token)
        assert fp.endswith("WXYZ")
        assert token not in fp


class TestHmacAndEquals:

    def test_hmac_sha256_hex_is_lowercase_hex_length_64(self):
        out = hmac_sha256_hex("key", "body")
        assert len(out) == 64
        assert out == out.lower()
        assert all(c in "0123456789abcdef" for c in out)
        # Bytes-input equivalence with str-input.
        assert hmac_sha256_hex(b"key", b"body") == out

    def test_hmac_is_deterministic(self):
        assert hmac_sha256_hex("s", "b") == hmac_sha256_hex("s", "b")

    def test_constant_time_equals_handles_none(self):
        assert constant_time_equals(None, "x") is False
        assert constant_time_equals("x", None) is False

    def test_constant_time_equals_rejects_length_mismatch(self):
        assert constant_time_equals("abc", "abcd") is False

    def test_constant_time_equals_matches(self):
        assert constant_time_equals("abcd", "abcd") is True
        assert constant_time_equals("abcd", "abce") is False


class TestSignatureSchemes:

    def _make_source(self, **kw):
        # Use Sanity as the concrete adapter — verify_signature is on the base.
        cls = get_cms_source("sanity")
        return cls(
            token="tok_xxx0000",
            webhook_secret="shhhhh",
            project_id="proj_1",
            dataset="production",
            **kw,
        )

    def test_hmac_sha256_scheme_ok(self):
        src = self._make_source()
        body = '{"_id":"x"}'
        sig = hmac_sha256_hex("shhhhh", body)
        assert src.verify_signature(sig, body, scheme="hmac-sha256") is True

    def test_hmac_sha256_scheme_bad_signature(self):
        src = self._make_source()
        assert src.verify_signature("nope", "{}", scheme="hmac-sha256") is False

    def test_hmac_sha256_scheme_missing_secret(self):
        cls = get_cms_source("sanity")
        src = cls(token="t", webhook_secret=None, project_id="p", dataset="production")
        assert src.verify_signature("any", "{}", scheme="hmac-sha256") is False

    def test_shared_secret_scheme(self):
        src = self._make_source()
        assert src.verify_signature("shhhhh", b"", scheme="shared-secret") is True
        assert src.verify_signature("wrong", b"", scheme="shared-secret") is False

    def test_unknown_scheme_raises(self):
        src = self._make_source()
        with pytest.raises(ValueError):
            src.verify_signature("x", "", scheme="rsa-sha1")


class TestEncryptedTokenFactory:

    def test_from_encrypted_token_decrypts_via_secret_store(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-fixture-key-w9")
        secret_store._reset_for_tests()

        plaintext = "sk_live_ABCD1234567890ZYXW"
        ciphertext = secret_store.encrypt(plaintext)
        wh_plain = "whsec_webhook_ABC123"
        wh_cipher = secret_store.encrypt(wh_plain)

        cls = get_cms_source("sanity")
        src = cls.from_encrypted_token(
            ciphertext,
            webhook_secret_ciphertext=wh_cipher,
            project_id="p1",
            dataset="production",
        )
        assert isinstance(src, CMSSource)
        fp = src.token_fp()
        assert fp.endswith("ZYXW")
        assert plaintext not in fp
        # Webhook secret was also decrypted into the adapter.
        wh_fp = src.webhook_secret_fp()
        assert wh_fp.endswith("C123")

    def test_from_plaintext_token(self):
        cls = get_cms_source("strapi")
        src = cls.from_plaintext_token(
            "bearer_pt_ABCD1234",
            webhook_secret="wh",
            base_url="https://cms.example.com",
        )
        assert src.provider == "strapi"


class TestResultDataclasses:

    def test_cms_entry_to_dict(self):
        e = CMSEntry(
            id="x1", content_type="post",
            fields={"title": "hi"}, created_at="2026-01-01",
            updated_at="2026-01-02", locale="en-US",
        )
        d = e.to_dict()
        assert d["id"] == "x1"
        assert d["content_type"] == "post"
        assert d["fields"] == {"title": "hi"}
        assert d["locale"] == "en-US"

    def test_cms_webhook_event_to_dict(self):
        ev = CMSWebhookEvent(
            provider="sanity", action="publish",
            entry_id="doc-123", content_type="post",
        )
        assert ev.to_dict() == {
            "provider": "sanity", "action": "publish",
            "entry_id": "doc-123", "content_type": "post",
        }


class TestInterfaceContract:

    @pytest.mark.parametrize("provider", ["sanity", "strapi", "contentful", "directus"])
    def test_required_methods_present(self, provider):
        cls = get_cms_source(provider)
        for name in ("fetch", "webhook_handler"):
            assert callable(getattr(cls, name)), f"{cls.__name__} missing {name}"

    def test_cannot_instantiate_base_directly(self):
        with pytest.raises(TypeError):
            CMSSource(token="t", webhook_secret="s")  # type: ignore[abstract]

    def test_missing_provider_classvar_rejected(self):

        class Broken(CMSSource):
            provider = ""

            async def fetch(self, query, *, params=None, content_type=None):
                return []

            async def webhook_handler(self, payload, *, headers=None):
                return CMSWebhookEvent(provider="broken", action="other")

        with pytest.raises(ValueError):
            Broken(token="t")


class TestCMSErrorTaxonomy:

    def test_error_carries_status_and_provider(self):
        exc = CMSError("boom", status=500, provider="sanity")
        assert exc.status == 500
        assert exc.provider == "sanity"
        assert "boom" in str(exc)
