"""P5 (#290) — Unit tests for backend.internal_distribution."""

from __future__ import annotations

import pytest

from backend import app_store_connect as asc
from backend import google_play_developer as gpd
from backend import internal_distribution as ind


_VALID_ASC_PEM = (
    "-----BEGIN EC PRIVATE KEY-----\n"
    "MHcCAQEEIB0J+offlineFakeKeyMaterial\n"
    "-----END EC PRIVATE KEY-----\n"
)
_VALID_PLAY_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA+OfflinePlaceholderRSAKeyMaterial\n"
    "-----END RSA PRIVATE KEY-----\n"
)


def _fake_ec_signer(m, pem):
    return (b"ec-" + m[:28]).ljust(64, b"\x00")[:64]


def _fake_rsa_signer(m, pem):
    return (b"rs-" + m[:28]).ljust(256, b"\x00")[:256]


def _clock():
    return 1_700_000_000.0


def _make_tf_client() -> ind.TestFlightClient:
    creds = asc.AppStoreCredentials(
        issuer_id="12345678-1234-1234-1234-123456789012",
        key_id="ABCDE12345",
        private_key_pem=_VALID_ASC_PEM,
        bundle_id="com.acme.consumer",
        app_id="9876543210",
    )
    return ind.TestFlightClient(
        creds,
        transport=asc.FakeTransport(),
        signer=_fake_ec_signer,
        clock=_clock,
    )


def _make_firebase_client() -> ind.FirebaseAppDistributionClient:
    creds = gpd.GooglePlayCredentials(
        client_email="ci@omnisight.iam.gserviceaccount.com",
        private_key_pem=_VALID_PLAY_PEM,
        package_name="com.acme.consumer",
    )
    return ind.FirebaseAppDistributionClient(
        creds,
        firebase_app_id="1:123:android:abc",
        transport=asc.FakeTransport(),
        signer=_fake_rsa_signer,
        clock=_clock,
    )


class _Ctx:
    allow = True
    reason = "ok"
    audit_entry = {"curr_hash": "abcd"}


class _Deny:
    allow = False
    reason = "denied"
    audit_entry = {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TesterGroup validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTesterGroup:
    def test_valid_ios_group(self):
        g = ind.TesterGroup(
            group_id="ios-qa",
            name="iOS QA",
            platform=ind.DistributionPlatform.ios,
            emails=("qa1@acme.com", "qa2@acme.com"),
        )
        assert g.emails == ("qa1@acme.com", "qa2@acme.com")

    def test_valid_android_group(self):
        g = ind.TesterGroup(
            group_id="and-qa",
            name="Android QA",
            platform=ind.DistributionPlatform.android,
            alias="qa-internal",
        )
        assert g.alias == "qa-internal"

    def test_ios_group_requires_emails(self):
        with pytest.raises(ind.InternalDistributionError):
            ind.TesterGroup(
                group_id="x",
                name="x",
                platform=ind.DistributionPlatform.ios,
                emails=(),
            )

    def test_android_group_requires_alias(self):
        with pytest.raises(ind.InternalDistributionError):
            ind.TesterGroup(
                group_id="x",
                name="x",
                platform=ind.DistributionPlatform.android,
                alias="",
            )

    def test_invalid_email_rejected(self):
        with pytest.raises(ind.InternalDistributionError):
            ind.TesterGroup(
                group_id="x",
                name="x",
                platform=ind.DistributionPlatform.ios,
                emails=("not-an-email",),
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TestFlight client
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTestFlightClient:
    def test_create_beta_group_happy(self):
        tf = _make_tf_client()
        tf.transport.push(asc.TransportResponse(
            201, {"data": {"id": "bg-1"}},
        ))
        out = tf.create_beta_group(group_name="Internal QA")
        assert out["group_id"] == "bg-1"
        assert tf.transport.calls[0]["url"].endswith("/v1/betaGroups")

    def test_distribute_to_group_requires_dual_sign(self):
        tf = _make_tf_client()
        with pytest.raises(ind.InternalDistributionError):
            tf.distribute_to_group(
                build_id="b-1",
                group_id="g-1",
                what_to_test="Nightly",
                dual_sign_context=None,
            )

    def test_distribute_to_group_posts_two_calls(self):
        tf = _make_tf_client()
        tf.transport.push(asc.TransportResponse(201, {"data": {"id": "loc-1"}}))
        tf.transport.push(asc.TransportResponse(204, {}))
        result = tf.distribute_to_group(
            build_id="b-1",
            group_id="g-1",
            what_to_test="Nightly iteration; smoke test signup.",
            dual_sign_context=_Ctx(),
        )
        assert result["what_to_test"].startswith("Nightly")
        urls = [c["url"] for c in tf.transport.calls]
        assert any("betaBuildLocalizations" in u for u in urls)
        assert any("betaGroups/g-1/relationships/builds" in u for u in urls)

    def test_blank_whats_new_rejected(self):
        tf = _make_tf_client()
        with pytest.raises(ind.InternalDistributionError):
            tf.distribute_to_group(
                build_id="b-1",
                group_id="g-1",
                what_to_test="   ",
                dual_sign_context=_Ctx(),
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Firebase client
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFirebaseClient:
    def test_distribute_happy(self):
        fb = _make_firebase_client()
        fb.transport.push(asc.TransportResponse(200, {"name": "ok"}))
        fb.transport.push(asc.TransportResponse(200, {}))
        out = fb.distribute(
            release_id="rel-1",
            group_aliases=["qa-internal"],
            tester_emails=["qa1@acme.com"],
            release_notes="Nightly — signup flow",
            dual_sign_context=_Ctx(),
        )
        assert out["release_id"] == "rel-1"
        assert "qa-internal" in out["group_aliases"]

    def test_distribute_requires_aliases_or_emails(self):
        fb = _make_firebase_client()
        with pytest.raises(ind.InternalDistributionError):
            fb.distribute(
                release_id="rel-1",
                group_aliases=[],
                tester_emails=[],
                release_notes="x",
                dual_sign_context=_Ctx(),
            )

    def test_distribute_rejects_bad_email(self):
        fb = _make_firebase_client()
        with pytest.raises(ind.InternalDistributionError):
            fb.distribute(
                release_id="rel-1",
                group_aliases=[],
                tester_emails=["not-an-email"],
                release_notes="x",
                dual_sign_context=_Ctx(),
            )

    def test_distribute_rejects_denied_context(self):
        fb = _make_firebase_client()
        with pytest.raises(ind.InternalDistributionError):
            fb.distribute(
                release_id="rel-1",
                group_aliases=["qa-internal"],
                tester_emails=[],
                release_notes="x",
                dual_sign_context=_Deny(),
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Unified manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestManager:
    def _make_manager(self):
        tf = _make_tf_client()
        fb = _make_firebase_client()
        return ind.InternalDistributionManager(testflight=tf, firebase=fb)

    def test_registers_and_distributes_ios(self):
        m = self._make_manager()
        m.register_group(ind.TesterGroup(
            group_id="ios-qa",
            name="iOS QA",
            platform=ind.DistributionPlatform.ios,
            emails=("qa@acme.com",),
        ))
        m.testflight.transport.push(asc.TransportResponse(201, {"data": {"id": "loc"}}))
        m.testflight.transport.push(asc.TransportResponse(204, {}))
        out = m.distribute(
            platform=ind.DistributionPlatform.ios,
            build_id="b-1",
            group_ids=["ios-qa"],
            release_notes="Nightly",
            dual_sign_context=_Ctx(),
        )
        assert out.platform is ind.DistributionPlatform.ios
        assert out.build_id == "b-1"
        assert "ios-qa" in out.group_ids

    def test_register_duplicate_rejected(self):
        m = self._make_manager()
        g = ind.TesterGroup(
            group_id="x",
            name="x",
            platform=ind.DistributionPlatform.android,
            alias="qa",
        )
        m.register_group(g)
        with pytest.raises(ind.InternalDistributionError):
            m.register_group(g)

    def test_platform_mismatch_blocks(self):
        m = self._make_manager()
        m.register_group(ind.TesterGroup(
            group_id="ios-qa",
            name="x",
            platform=ind.DistributionPlatform.ios,
            emails=("qa@acme.com",),
        ))
        with pytest.raises(ind.InternalDistributionError):
            m.distribute(
                platform=ind.DistributionPlatform.android,
                build_id="b-1",
                group_ids=["ios-qa"],
                release_notes="Nightly",
                dual_sign_context=_Ctx(),
            )

    def test_distributes_android(self):
        m = self._make_manager()
        m.register_group(ind.TesterGroup(
            group_id="and-qa",
            name="and",
            platform=ind.DistributionPlatform.android,
            alias="qa-internal",
            emails=("a@acme.com",),
        ))
        m.firebase.transport.push(asc.TransportResponse(200, {"name": "ok"}))
        m.firebase.transport.push(asc.TransportResponse(200, {}))
        out = m.distribute(
            platform=ind.DistributionPlatform.android,
            build_id="rel-42",
            group_ids=["and-qa"],
            release_notes="Nightly",
            dual_sign_context=_Ctx(),
        )
        assert out.platform is ind.DistributionPlatform.android
        assert out.build_id == "rel-42"

    def test_missing_ios_client_raises(self):
        m = ind.InternalDistributionManager(testflight=None, firebase=None)
        m.register_group(ind.TesterGroup(
            group_id="ios",
            name="x",
            platform=ind.DistributionPlatform.ios,
            emails=("a@acme.com",),
        ))
        with pytest.raises(ind.InternalDistributionError):
            m.distribute(
                platform=ind.DistributionPlatform.ios,
                build_id="b-1",
                group_ids=["ios"],
                release_notes="x",
                dual_sign_context=_Ctx(),
            )

    def test_distribute_internal_convenience(self):
        m = self._make_manager()
        m.register_group(ind.TesterGroup(
            group_id="ios-qa",
            name="x",
            platform=ind.DistributionPlatform.ios,
            emails=("qa@acme.com",),
        ))
        m.testflight.transport.push(asc.TransportResponse(201, {"data": {"id": "loc"}}))
        m.testflight.transport.push(asc.TransportResponse(204, {}))
        out = ind.distribute_internal(
            manager=m,
            platform="ios",
            build_id="b-1",
            group_ids=["ios-qa"],
            release_notes="Nightly",
            dual_sign_context=_Ctx(),
        )
        assert out.build_id == "b-1"
