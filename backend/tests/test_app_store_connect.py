"""P5 (#290) — Unit tests for backend.app_store_connect.

Offline-only: every test uses :class:`FakeTransport` + an injected
deterministic signer so the JWT / HTTP contract is exercised without
dialling apple.com.
"""

from __future__ import annotations

import json

import pytest

from backend import app_store_connect as asc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_VALID_PEM = (
    "-----BEGIN EC PRIVATE KEY-----\n"
    "MHcCAQEEIB0J+offlineFakeKeyMaterial+RequiredForTests\n"
    "-----END EC PRIVATE KEY-----\n"
)


def _fake_signer(message: bytes, pem: str) -> bytes:
    # Deterministic 64-byte signature with pem fingerprint so tests can
    # assert the signer received the right key.
    return (b"sig-" + message[:28] + b"-" + pem[-8:].encode("utf-8"))[:64].ljust(
        64, b"\x00",
    )


@pytest.fixture()
def creds() -> asc.AppStoreCredentials:
    return asc.AppStoreCredentials(
        issuer_id="12345678-1234-1234-1234-123456789012",
        key_id="ABCDE12345",
        private_key_pem=_VALID_PEM,
        bundle_id="com.acme.consumer",
        app_id="9876543210",
    )


@pytest.fixture()
def client(creds):
    clock = {"t": 1_700_000_000.0}

    def _clock():
        return clock["t"]

    ft = asc.FakeTransport()
    c = asc.AppStoreConnectClient(
        creds, transport=ft, signer=_fake_signer, clock=_clock,
    )
    return c, ft, clock


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Credentials
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCredentials:
    def test_valid_inputs(self, creds):
        assert creds.key_id == "ABCDE12345"
        assert creds.bundle_id == "com.acme.consumer"

    def test_redacted_never_exposes_pem(self, creds):
        view = creds.redacted()
        assert _VALID_PEM not in json.dumps(view)
        assert view["private_key_fingerprint"].startswith("sha256:")

    @pytest.mark.parametrize("bad_issuer", [
        "",
        "not-a-uuid",
        "1234-1234-1234-1234-123456789012",
    ])
    def test_rejects_bad_issuer_id(self, bad_issuer):
        with pytest.raises(asc.InvalidCredentialsError):
            asc.AppStoreCredentials(
                issuer_id=bad_issuer,
                key_id="ABCDE12345",
                private_key_pem=_VALID_PEM,
                bundle_id="com.acme.consumer",
            )

    @pytest.mark.parametrize("bad_key_id", ["abc", "ABCDE1234", "ABCDE123456"])
    def test_rejects_bad_key_id(self, bad_key_id):
        with pytest.raises(asc.InvalidCredentialsError):
            asc.AppStoreCredentials(
                issuer_id="12345678-1234-1234-1234-123456789012",
                key_id=bad_key_id,
                private_key_pem=_VALID_PEM,
                bundle_id="com.acme.consumer",
            )

    def test_rejects_non_pem_key(self):
        with pytest.raises(asc.InvalidCredentialsError):
            asc.AppStoreCredentials(
                issuer_id="12345678-1234-1234-1234-123456789012",
                key_id="ABCDE12345",
                private_key_pem="plain-text-not-pem",
                bundle_id="com.acme.consumer",
            )

    def test_rejects_bad_bundle_id(self):
        with pytest.raises(asc.InvalidCredentialsError):
            asc.AppStoreCredentials(
                issuer_id="12345678-1234-1234-1234-123456789012",
                key_id="ABCDE12345",
                private_key_pem=_VALID_PEM,
                bundle_id="!!not-a-bundle!!",
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. JWT issuance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestJWT:
    def test_issue_jwt_shape(self, creds):
        bundle = asc.issue_jwt(creds, now=1_700_000_000.0, signer=_fake_signer)
        assert set(bundle.keys()) == {"token", "expires_at"}
        token = bundle["token"]
        assert token.count(".") == 2
        header_b64, payload_b64, _sig = token.split(".")
        # decode header
        import base64
        header = json.loads(base64.urlsafe_b64decode(header_b64 + "==="))
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "==="))
        assert header == {
            "alg": "ES256", "kid": "ABCDE12345", "typ": "JWT",
        }
        assert payload["iss"] == "12345678-1234-1234-1234-123456789012"
        assert payload["aud"] == "appstoreconnect-v1"
        assert payload["bid"] == "com.acme.consumer"
        assert payload["exp"] - payload["iat"] == 20 * 60

    def test_issue_jwt_without_cryptography_raises(self, creds):
        # The default signer requires cryptography; we already inject
        # our fake, so this test just pins the error when no signer is
        # provided AND cryptography is missing.  We approximate by
        # monkey-patching the import inside the module.
        from backend import app_store_connect as mod
        orig = mod._default_es256_signer

        def _broken(message, pem):
            raise asc.JWTSigningError(
                "cryptography is required to sign ASC JWTs; inject a "
                "test signer via issue_jwt(signer=...) to bypass in unit tests",
            )

        mod._default_es256_signer = _broken
        try:
            with pytest.raises(asc.JWTSigningError):
                asc.issue_jwt(creds, now=1.0)
        finally:
            mod._default_es256_signer = orig


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Transport layer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTransportBase:
    def test_base_transport_requires_request_implementation(self):
        class MissingTransport(asc.Transport):
            pass

        with pytest.raises(TypeError, match="request"):
            MissingTransport()


class TestFakeTransport:
    def test_scrubs_authorization_header(self):
        ft = asc.FakeTransport()
        ft.request(
            method="GET", url="https://x/y",
            headers={"Authorization": "Bearer real-token"},
        )
        assert ft.calls[0]["headers"]["Authorization"] == "Bearer ***"

    def test_queued_responses_pop_in_order(self):
        ft = asc.FakeTransport([
            asc.TransportResponse(201, {"data": {"id": "a"}}),
            asc.TransportResponse(201, {"data": {"id": "b"}}),
        ])
        r1 = ft.request(method="POST", url="x", headers={})
        r2 = ft.request(method="POST", url="x", headers={})
        assert r1.body["data"]["id"] == "a"
        assert r2.body["data"]["id"] == "b"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Client — create_version
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCreateVersion:
    def test_happy_path(self, client):
        c, ft, _ = client
        ft.push(asc.TransportResponse(
            201, {"data": {"id": "ver-42", "type": "appStoreVersions"}},
        ))
        v = c.create_version(version_string="2.1.0")
        assert v.version_id == "ver-42"
        assert v.version_string == "2.1.0"
        assert v.platform is asc.Platform.ios
        assert v.app_id == "9876543210"
        # Check outbound payload shape.
        payload = ft.calls[0]["json"]
        assert payload["data"]["attributes"]["versionString"] == "2.1.0"
        assert payload["data"]["attributes"]["platform"] == "IOS"
        assert payload["data"]["relationships"]["app"]["data"]["id"] == "9876543210"

    def test_rejects_bad_version_string(self, client):
        c, _, _ = client
        for bad in ["abc", "1", "", "1.2.3.4.5"]:
            with pytest.raises(asc.AppStoreConnectError):
                c.create_version(version_string=bad)

    def test_reraises_asc_error_payload(self, client):
        c, ft, _ = client
        ft.push(asc.TransportResponse(
            400, {"errors": [{"title": "Bad", "detail": "no"}]},
        ))
        with pytest.raises(asc.AppStoreConnectError) as exc:
            c.create_version(version_string="1.0.0")
        assert "Bad" in str(exc.value)

    def test_strict_mode_requires_dual_sign(self, client, monkeypatch):
        c, ft, _ = client
        monkeypatch.setattr(asc, "_ENFORCE_DUAL_SIGN", True)
        with pytest.raises(asc.MissingDualSignError):
            c.create_version(version_string="1.0.0")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Client — upload_build
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestUploadBuild:
    def test_happy_path(self, client):
        c, ft, _ = client
        sha = "a" * 64
        ft.push(asc.TransportResponse(
            201,
            {
                "data": {
                    "id": "bld-9",
                    "attributes": {
                        "processingState": "PROCESSING",
                        "uploadOperations": [
                            {"method": "PUT", "url": "https://s3/…"},
                        ],
                    },
                },
            },
        ))
        b = c.upload_build(
            bundle_id="com.acme.consumer",
            version="42",
            short_version="2.1.0",
            file_sha256=sha,
            file_size_bytes=1024 * 1024,
        )
        assert b.build_id == "bld-9"
        assert b.processing_state == "PROCESSING"
        assert len(b.upload_operations) == 1

    def test_rejects_bad_sha(self, client):
        c, _, _ = client
        with pytest.raises(asc.AppStoreConnectError):
            c.upload_build(
                bundle_id="com.acme.consumer",
                version="1",
                short_version="1.0.0",
                file_sha256="notasha",
                file_size_bytes=1,
            )

    def test_rejects_zero_size(self, client):
        c, _, _ = client
        with pytest.raises(asc.AppStoreConnectError):
            c.upload_build(
                bundle_id="com.acme.consumer",
                version="1",
                short_version="1.0.0",
                file_sha256="a" * 64,
                file_size_bytes=0,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. Client — submit_for_review  (strict dual-sign)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _StubCtx:
    def __init__(self, allow, audit=None, reason="stub"):
        self.allow = allow
        self.reason = reason
        self.audit_entry = audit or {"curr_hash": "deadbeef"}


class TestSubmitForReview:
    def test_requires_dual_sign_context(self, client):
        c, _, _ = client
        with pytest.raises(asc.MissingDualSignError):
            c.submit_for_review(version_id="ver-1", dual_sign_context=None)

    def test_rejects_non_allow_context(self, client):
        c, _, _ = client
        with pytest.raises(asc.MissingDualSignError):
            c.submit_for_review(
                version_id="ver-1",
                dual_sign_context=_StubCtx(allow=False),
            )

    def test_happy_path_with_allow_ctx(self, client):
        c, ft, _ = client
        ft.push(asc.TransportResponse(
            201, {"data": {"id": "subm-1"}},
        ))
        out = c.submit_for_review(
            version_id="ver-1",
            dual_sign_context=_StubCtx(
                allow=True, audit={"curr_hash": "abcd"},
            ),
            release_notes="First release.",
        )
        assert out.submission_id == "subm-1"
        assert out.version_id == "ver-1"
        assert out.audit_entry["curr_hash"] == "abcd"

    def test_submission_rejection_raises_submission_error(self, client):
        c, ft, _ = client
        ft.push(asc.TransportResponse(
            400,
            {"errors": [{"title": "INVALID_SUBMISSION", "detail": "x"}]},
        ))
        with pytest.raises(asc.SubmissionRejectedError):
            c.submit_for_review(
                version_id="ver-1",
                dual_sign_context=_StubCtx(allow=True),
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. Client — upload_screenshot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestUploadScreenshot:
    def test_happy_path(self, client):
        c, ft, _ = client
        ft.push(asc.TransportResponse(
            201,
            {"data": {
                "id": "scr-1",
                "attributes": {
                    "uploadOperations": [{"method": "PUT"}],
                },
            }},
        ))
        s = c.upload_screenshot(
            device_type=asc.ScreenshotDeviceType.iphone_6_7,
            file_name="hero.png",
            file_sha256="b" * 64,
            file_size_bytes=256 * 1024,
        )
        assert s.screenshot_id == "scr-1"
        assert s.device_type is asc.ScreenshotDeviceType.iphone_6_7
        assert s.upload_operations

    def test_rejects_empty_filename(self, client):
        c, _, _ = client
        with pytest.raises(asc.AppStoreConnectError):
            c.upload_screenshot(
                device_type=asc.ScreenshotDeviceType.iphone_6_7,
                file_name="",
                file_sha256="b" * 64,
                file_size_bytes=1,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. JWT caching behaviour
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestJWTCaching:
    def test_jwt_reissued_near_expiry(self, creds):
        clock = {"t": 1_700_000_000.0}

        def _clock():
            return clock["t"]

        ft = asc.FakeTransport([
            asc.TransportResponse(201, {"data": {"id": "v1"}}),
            asc.TransportResponse(201, {"data": {"id": "v2"}}),
        ])
        signatures = []

        def _signer(msg, pem):
            signatures.append(msg)
            return _fake_signer(msg, pem)

        c = asc.AppStoreConnectClient(
            creds, transport=ft, signer=_signer, clock=_clock,
        )
        c.create_version(version_string="1.0.0")
        # within 20m window, no re-issue
        clock["t"] += 60
        c.create_version(version_string="1.0.1")
        assert len(signatures) == 1
        # skip past expiry minus safety margin
        clock["t"] += 20 * 60
        ft.push(asc.TransportResponse(201, {"data": {"id": "v3"}}))
        c.create_version(version_string="1.0.2")
        assert len(signatures) == 2


def test_set_enforce_dual_sign_toggles_module_flag():
    asc.set_enforce_dual_sign(True)
    assert asc._ENFORCE_DUAL_SIGN is True
    asc.set_enforce_dual_sign(False)
    assert asc._ENFORCE_DUAL_SIGN is False
