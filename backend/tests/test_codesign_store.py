"""P3 (#288) — Unit tests for backend.codesign_store.

Covers each sub-component in isolation so a regression in one (HSM
validator, cert store, audit chain, expiry scanner, sign attestation)
fails exactly one test class and doesn't blast the matrix.

Tests never touch the default ``data/codesign_store.json`` — each
test gets a scratch store bound to ``tmp_path`` via ``_reset_for_tests``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend import codesign_store as cs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
def scratch_store(tmp_path: Path) -> cs.CodesignStore:
    path = tmp_path / "codesign_store.json"
    store = cs.CodesignStore(path=path)
    return store


@pytest.fixture()
def scratch_chain() -> cs.CodeSignAuditChain:
    return cs.CodeSignAuditChain(persist=False)


def _ts(offset_days: float = 0.0, base: float | None = None) -> float:
    return (base if base is not None else time.time()) + offset_days * 86400.0


def _valid_apple_inputs(**overrides):
    base = {
        "cert_id": "apple.dev.team",
        "kind": cs.CertKind.apple_developer_id,
        "team_id": "A1B2C3D4E5",
        "subject_cn": "Developer ID Application: ACME Corp",
        "serial": "DEADBEEF1234",
        "not_before": _ts(-30),
        "not_after": _ts(365),
        "pem_bytes": b"-----BEGIN CERTIFICATE-----\nMIIBkTCC...\n-----END CERTIFICATE-----\n",
    }
    base.update(overrides)
    return base


def _valid_android_inputs(**overrides):
    base = {
        "cert_id": "android.acme.consumer",
        "package_name": "com.acme.consumer",
        "alias": "release",
        "keystore_bytes": b"FAKE-JKS-BYTES-0123456789",
        "keystore_password": "ks-pass-supersecret",
        "key_password": "key-pass-supersecret",
        "subject_cn": "CN=ACME Release,O=ACME",
        "not_before": _ts(-30),
        "not_after": _ts(365 * 25),  # Android signing certs can be 25y+
    }
    base.update(overrides)
    return base


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. HSM layer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestHSMLayer:
    def test_vendor_enum_has_exactly_four_options(self):
        assert {v.value for v in cs.HSMVendor} == {
            "none", "aws_kms", "gcp_kms", "yubihsm",
        }

    def test_none_vendor_accepts_empty_key_ref(self):
        cs.validate_hsm_key_ref(cs.HSMVendor.none, "")

    def test_none_vendor_rejects_non_empty_key_ref(self):
        with pytest.raises(cs.InvalidHSMKeyRefError, match="none"):
            cs.validate_hsm_key_ref(cs.HSMVendor.none, "anything")

    def test_aws_kms_arn_accepted(self):
        cs.validate_hsm_key_ref(
            cs.HSMVendor.aws_kms,
            "arn:aws:kms:us-east-1:123456789012:key/"
            "12345678-1234-1234-1234-123456789012",
        )

    def test_aws_kms_govcloud_arn_accepted(self):
        cs.validate_hsm_key_ref(
            cs.HSMVendor.aws_kms,
            "arn:aws-us-gov:kms:us-gov-west-1:123456789012:key/"
            "12345678-1234-1234-1234-123456789012",
        )

    def test_aws_kms_rejects_alias(self):
        # Alias ARNs are a legitimate KMS concept but we pin *keys* not
        # aliases: aliases can be repointed silently and break the
        # signing chain.
        with pytest.raises(cs.InvalidHSMKeyRefError):
            cs.validate_hsm_key_ref(
                cs.HSMVendor.aws_kms,
                "arn:aws:kms:us-east-1:123456789012:alias/my-signing-key",
            )

    def test_gcp_kms_resource_name_accepted(self):
        cs.validate_hsm_key_ref(
            cs.HSMVendor.gcp_kms,
            "projects/acme-prod/locations/global/"
            "keyRings/mobile/cryptoKeys/ios-release",
        )

    def test_gcp_kms_version_pinned_accepted(self):
        cs.validate_hsm_key_ref(
            cs.HSMVendor.gcp_kms,
            "projects/acme-prod/locations/global/"
            "keyRings/mobile/cryptoKeys/ios-release/cryptoKeyVersions/42",
        )

    def test_gcp_kms_rejects_junk(self):
        with pytest.raises(cs.InvalidHSMKeyRefError):
            cs.validate_hsm_key_ref(cs.HSMVendor.gcp_kms, "not-a-gcp-path")

    def test_yubihsm_uri_accepted(self):
        cs.validate_hsm_key_ref(
            cs.HSMVendor.yubihsm,
            "yubihsm://1234567/slot/12#label=ios-signing",
        )

    def test_yubihsm_rejects_missing_slot(self):
        with pytest.raises(cs.InvalidHSMKeyRefError):
            cs.validate_hsm_key_ref(
                cs.HSMVendor.yubihsm, "yubihsm://1234567",
            )

    def test_resolve_hsm_provider_returns_opaque_handle(self):
        p = cs.resolve_hsm_provider(
            "aws_kms",
            "arn:aws:kms:us-east-1:123456789012:key/"
            "abcdef01-2345-6789-abcd-ef0123456789",
        )
        assert p.vendor is cs.HSMVendor.aws_kms
        assert p.key_ref.startswith("arn:aws:kms:")

    def test_describe_does_not_echo_key_ref(self):
        sentinel = (
            "arn:aws:kms:us-east-1:123456789012:key/"
            "abcdef01-2345-6789-abcd-ef0123456789"
        )
        p = cs.resolve_hsm_provider("aws_kms", sentinel)
        d = p.describe()
        assert sentinel not in json.dumps(d)
        assert d["key_ref_fingerprint"].startswith("sha256:")

    def test_unknown_vendor_string_raises(self):
        with pytest.raises(cs.UnknownHSMVendorError, match="unknown"):
            cs.resolve_hsm_provider("thales", "whatever")

    def test_vendor_coercion_is_case_insensitive(self):
        p = cs.resolve_hsm_provider(
            "AWS_KMS",
            "arn:aws:kms:us-east-1:123456789012:key/"
            "abcdef01-2345-6789-abcd-ef0123456789",
        )
        assert p.vendor is cs.HSMVendor.aws_kms


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. CodesignStore — Apple certs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStoreAppleCerts:
    def test_register_developer_id_and_retrieve(self, scratch_store):
        rec = scratch_store.register_apple_cert(**_valid_apple_inputs())
        assert rec.kind is cs.CertKind.apple_developer_id
        retrieved = scratch_store.get("apple.dev.team")
        assert retrieved.serial == "DEADBEEF1234"
        assert retrieved.team_id == "A1B2C3D4E5"
        assert retrieved.fingerprint_sha256  # not empty

    def test_register_app_store_distribution(self, scratch_store):
        rec = scratch_store.register_apple_cert(
            **_valid_apple_inputs(
                cert_id="apple.asc.team",
                kind=cs.CertKind.apple_app_store_distribution,
            ),
        )
        assert rec.kind is cs.CertKind.apple_app_store_distribution

    def test_rejects_non_apple_kind(self, scratch_store):
        with pytest.raises(cs.InvalidCertError, match="Apple cert kind"):
            scratch_store.register_apple_cert(
                **_valid_apple_inputs(kind=cs.CertKind.android_keystore),
            )

    def test_rejects_invalid_team_id(self, scratch_store):
        with pytest.raises(cs.InvalidCertError, match="team_id"):
            scratch_store.register_apple_cert(
                **_valid_apple_inputs(team_id="abc"),
            )

    def test_rejects_inverted_validity_window(self, scratch_store):
        with pytest.raises(cs.InvalidCertError, match="not_after"):
            scratch_store.register_apple_cert(
                **_valid_apple_inputs(not_before=_ts(10), not_after=_ts(5)),
            )

    def test_rejects_duplicate_cert_id(self, scratch_store):
        scratch_store.register_apple_cert(**_valid_apple_inputs())
        with pytest.raises(cs.DuplicateCertError):
            scratch_store.register_apple_cert(**_valid_apple_inputs())

    def test_rejects_weird_cert_id(self, scratch_store):
        with pytest.raises(cs.InvalidCertError, match="cert_id"):
            scratch_store.register_apple_cert(
                **_valid_apple_inputs(cert_id="has spaces!"),
            )

    def test_register_hsm_backed_cert_stores_no_material(self, scratch_store):
        inputs = _valid_apple_inputs(
            cert_id="apple.hsm.team",
            hsm_vendor="aws_kms",
            hsm_key_ref=(
                "arn:aws:kms:us-east-1:123456789012:key/"
                "12345678-1234-1234-1234-123456789012"
            ),
        )
        rec = scratch_store.register_apple_cert(**inputs)
        assert rec.hsm_vendor is cs.HSMVendor.aws_kms
        assert rec.encrypted_material == ""
        # Even though caller passed pem_bytes, HSM path discards it:
        assert (
            scratch_store.get("apple.hsm.team").encrypted_material == ""
        )

    def test_register_hsm_with_bad_key_ref_rejected(self, scratch_store):
        with pytest.raises(cs.InvalidHSMKeyRefError):
            scratch_store.register_apple_cert(
                **_valid_apple_inputs(
                    hsm_vendor="aws_kms",
                    hsm_key_ref="not-a-valid-arn",
                ),
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. CodesignStore — Provisioning profiles
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestProvisioningProfiles:
    def test_register_and_retrieve(self, scratch_store):
        rec = scratch_store.register_provisioning_profile(
            cert_id="profile.acme.app",
            team_id="A1B2C3D4E5",
            app_id="com.acme.app",
            profile_uuid="AAAA-BBBB-CCCC-DDDD",
            profile_bytes=b"mobileprovision-bytes-blob",
            not_before=_ts(-1),
            not_after=_ts(365),
            associated_cert_id="apple.dev.team",
        )
        assert rec.kind is cs.CertKind.apple_provisioning_profile
        assert rec.extra["app_id"] == "com.acme.app"
        assert rec.extra["associated_cert_id"] == "apple.dev.team"
        assert rec.encrypted_material

    def test_rejects_missing_app_id(self, scratch_store):
        with pytest.raises(cs.InvalidCertError, match="app_id"):
            scratch_store.register_provisioning_profile(
                cert_id="p1",
                team_id="A1B2C3D4E5",
                app_id="",
                profile_uuid="uuid",
                profile_bytes=b"x",
                not_before=_ts(0),
                not_after=_ts(365),
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. CodesignStore — Android keystore
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAndroidKeystore:
    def test_register_software_keystore(self, scratch_store):
        rec = scratch_store.register_android_keystore(
            **_valid_android_inputs(),
        )
        assert rec.kind is cs.CertKind.android_keystore
        assert rec.extra["package_name"] == "com.acme.consumer"
        assert rec.extra["alias"] == "release"
        assert rec.encrypted_material
        # Passwords must be encrypted at-rest:
        assert rec.extra["encrypted_keystore_password"]
        assert rec.extra["encrypted_key_password"]
        assert "supersecret" not in json.dumps(cs.redacted_view(rec))

    def test_decrypt_passwords_roundtrips(self, scratch_store):
        scratch_store.register_android_keystore(**_valid_android_inputs())
        ks_pw, k_pw = scratch_store.decrypt_android_passwords(
            "android.acme.consumer",
        )
        assert ks_pw == "ks-pass-supersecret"
        assert k_pw == "key-pass-supersecret"

    def test_decrypt_material_roundtrips(self, scratch_store):
        scratch_store.register_android_keystore(**_valid_android_inputs())
        material = scratch_store.decrypt_material("android.acme.consumer")
        assert material == b"FAKE-JKS-BYTES-0123456789"

    def test_rejects_missing_keystore_when_no_hsm(self, scratch_store):
        with pytest.raises(cs.InvalidCertError, match="keystore_bytes"):
            scratch_store.register_android_keystore(
                **_valid_android_inputs(keystore_bytes=None),
            )

    def test_rejects_missing_password_when_no_hsm(self, scratch_store):
        with pytest.raises(cs.InvalidCertError, match="password"):
            scratch_store.register_android_keystore(
                **_valid_android_inputs(keystore_password=""),
            )

    def test_hsm_backed_android_allows_no_keystore(self, scratch_store):
        rec = scratch_store.register_android_keystore(
            **_valid_android_inputs(
                cert_id="android.hsm.app",
                keystore_bytes=None,
                keystore_password=None,
                key_password=None,
                hsm_vendor="gcp_kms",
                hsm_key_ref=(
                    "projects/p/locations/global/"
                    "keyRings/android/cryptoKeys/release"
                ),
            ),
        )
        assert rec.hsm_vendor is cs.HSMVendor.gcp_kms
        assert rec.encrypted_material == ""

    def test_decrypt_material_refuses_hsm_backed(self, scratch_store):
        scratch_store.register_android_keystore(
            **_valid_android_inputs(
                cert_id="android.hsm.app",
                keystore_bytes=None,
                keystore_password=None,
                key_password=None,
                hsm_vendor="yubihsm",
                hsm_key_ref="yubihsm://9999/slot/3#label=release",
            ),
        )
        with pytest.raises(cs.SigningChainError, match="HSM-backed"):
            scratch_store.decrypt_material("android.hsm.app")

    def test_decrypt_passwords_refuses_non_android(self, scratch_store):
        scratch_store.register_apple_cert(**_valid_apple_inputs())
        with pytest.raises(cs.SigningChainError, match="not an android"):
            scratch_store.decrypt_android_passwords("apple.dev.team")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Store — listing / delete / persistence
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStoreLifecycle:
    def test_list_redacted_contains_no_secrets(self, scratch_store):
        scratch_store.register_android_keystore(**_valid_android_inputs())
        listing = scratch_store.list_redacted()
        assert len(listing) == 1
        blob = json.dumps(listing)
        assert "supersecret" not in blob
        assert "FAKE-JKS-BYTES" not in blob

    def test_redacted_view_masks_extra_password_keys(self, scratch_store):
        scratch_store.register_apple_cert(
            **_valid_apple_inputs(
                extra={"private_key": "oops", "passphrase": "nope"},
            ),
        )
        rec = scratch_store.get("apple.dev.team")
        view = cs.redacted_view(rec)
        assert view["extra"]["private_key"] == "(redacted)"
        assert view["extra"]["passphrase"] == "(redacted)"

    def test_delete(self, scratch_store):
        scratch_store.register_apple_cert(**_valid_apple_inputs())
        assert scratch_store.delete("apple.dev.team") is True
        with pytest.raises(cs.UnknownCertError):
            scratch_store.get("apple.dev.team")
        assert scratch_store.delete("nope") is False

    def test_persistence_round_trip(self, tmp_path: Path):
        p = tmp_path / "store.json"
        s1 = cs.CodesignStore(path=p)
        s1.register_apple_cert(**_valid_apple_inputs())
        s1.register_android_keystore(**_valid_android_inputs())
        # Reload from disk:
        s2 = cs.CodesignStore(path=p)
        assert len(s2.list_records()) == 2
        roundtripped = s2.get("apple.dev.team")
        assert roundtripped.team_id == "A1B2C3D4E5"
        assert roundtripped.fingerprint_sha256  # preserved

    def test_persistence_file_permission_is_owner_only(self, tmp_path: Path):
        import stat
        p = tmp_path / "store.json"
        s = cs.CodesignStore(path=p)
        s.register_apple_cert(**_valid_apple_inputs())
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600, f"expected 0o600 got {mode:o}"

    def test_persistence_survives_corrupt_file(self, tmp_path: Path, caplog):
        p = tmp_path / "store.json"
        p.write_text("{not valid json")
        # Should log a warning and start empty rather than crashing:
        s = cs.CodesignStore(path=p)
        assert s.list_records() == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. Hash-chain sign audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCodeSignAuditChain:
    def _append(self, chain, **overrides):
        base = {
            "cert_id": "apple.dev.team",
            "cert_fingerprint": "a" * 64,
            "artifact_path": "dist/app.ipa",
            "artifact_sha256": "0" * 64,
            "actor": "signer-bot",
            "hsm_vendor": "none",
        }
        base.update(overrides)
        return chain.append(**base)

    def test_linear_chain_verifies(self, scratch_chain):
        self._append(scratch_chain)
        self._append(scratch_chain, artifact_sha256="1" * 64)
        self._append(scratch_chain, artifact_sha256="2" * 64)
        ok, bad = scratch_chain.verify()
        assert ok is True and bad is None

    def test_chain_tamper_detected(self, scratch_chain):
        self._append(scratch_chain)
        self._append(scratch_chain, artifact_sha256="1" * 64)
        self._append(scratch_chain, artifact_sha256="2" * 64)
        # flip actor on entry 1 without recomputing hash
        scratch_chain.entries[1]["actor"] = "attacker"
        ok, bad = scratch_chain.verify()
        assert ok is False
        assert bad == 1

    def test_head_changes_per_append(self, scratch_chain):
        assert scratch_chain.head() == ""
        self._append(scratch_chain)
        h1 = scratch_chain.head()
        self._append(scratch_chain, artifact_sha256="1" * 64)
        h2 = scratch_chain.head()
        assert h1 and h2 and h1 != h2

    def test_reject_missing_cert_id(self, scratch_chain):
        with pytest.raises(ValueError, match="cert_id"):
            self._append(scratch_chain, cert_id="")

    def test_reject_invalid_artifact_sha256(self, scratch_chain):
        with pytest.raises(ValueError, match="artifact_sha256"):
            self._append(scratch_chain, artifact_sha256="not-a-hash")

    def test_reject_missing_actor(self, scratch_chain):
        with pytest.raises(ValueError, match="actor"):
            self._append(scratch_chain, actor="")

    def test_for_cert_filter(self, scratch_chain):
        self._append(scratch_chain, cert_id="A")
        self._append(scratch_chain, cert_id="B", artifact_sha256="1" * 64)
        self._append(scratch_chain, cert_id="A", artifact_sha256="2" * 64)
        assert len(scratch_chain.for_cert("A")) == 2
        assert len(scratch_chain.for_cert("B")) == 1

    def test_for_artifact_filter(self, scratch_chain):
        self._append(scratch_chain, artifact_sha256="a" * 64)
        self._append(scratch_chain, artifact_sha256="b" * 64, cert_id="B")
        self._append(scratch_chain, artifact_sha256="a" * 64, cert_id="C")
        assert len(scratch_chain.for_artifact("a" * 64)) == 2
        assert len(scratch_chain.for_artifact("b" * 64)) == 1

    def test_global_chain_singleton(self):
        cs.reset_global_audit_chain_for_tests()
        c1 = cs.get_global_audit_chain()
        c2 = cs.get_global_audit_chain()
        assert c1 is c2
        cs.reset_global_audit_chain_for_tests()
        c3 = cs.get_global_audit_chain()
        assert c3 is not c1
        cs.reset_global_audit_chain_for_tests()

    def test_entity_kind_is_stable_string(self):
        assert cs.CODESIGN_AUDIT_ENTITY_KIND == "codesign_chain_sign"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. attest_sign facade
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAttestSign:
    def test_attest_sign_writes_chain_entry(self, scratch_store, scratch_chain):
        scratch_store.register_apple_cert(**_valid_apple_inputs())
        ctx = cs.attest_sign(
            cert_id="apple.dev.team",
            artifact_path="dist/app.ipa",
            artifact_sha256="abcd" * 16,
            actor="builder-runner-7",
            store=scratch_store,
            chain=scratch_chain,
        )
        assert ctx.cert.cert_id == "apple.dev.team"
        assert ctx.audit_entry["cert_id"] == "apple.dev.team"
        assert ctx.audit_entry["actor"] == "builder-runner-7"
        assert ctx.audit_entry["cert_fingerprint"] == ctx.cert.fingerprint_sha256
        assert len(scratch_chain.entries) == 1

    def test_attest_sign_refuses_expired_cert(self, scratch_store, scratch_chain):
        scratch_store.register_apple_cert(
            **_valid_apple_inputs(
                not_before=_ts(-400),
                not_after=_ts(-10),  # expired 10 days ago
            ),
        )
        with pytest.raises(cs.SigningChainError, match="expired"):
            cs.attest_sign(
                cert_id="apple.dev.team",
                artifact_path="dist/app.ipa",
                artifact_sha256="a" * 64,
                actor="signer",
                store=scratch_store,
                chain=scratch_chain,
            )

    def test_attest_sign_surfaces_hsm_provider(self, scratch_store, scratch_chain):
        scratch_store.register_apple_cert(
            **_valid_apple_inputs(
                cert_id="apple.hsm.team",
                hsm_vendor="gcp_kms",
                hsm_key_ref=(
                    "projects/p/locations/global/"
                    "keyRings/ios/cryptoKeys/release"
                ),
            ),
        )
        ctx = cs.attest_sign(
            cert_id="apple.hsm.team",
            artifact_path="dist/app.ipa",
            artifact_sha256="b" * 64,
            actor="builder-runner-7",
            store=scratch_store,
            chain=scratch_chain,
        )
        assert ctx.hsm_provider is not None
        assert ctx.hsm_provider.vendor is cs.HSMVendor.gcp_kms

    def test_attest_sign_no_hsm_returns_none_provider(self, scratch_store, scratch_chain):
        scratch_store.register_apple_cert(**_valid_apple_inputs())
        ctx = cs.attest_sign(
            cert_id="apple.dev.team",
            artifact_path="x",
            artifact_sha256="c" * 64,
            actor="signer",
            store=scratch_store,
            chain=scratch_chain,
        )
        assert ctx.hsm_provider is None

    def test_attest_sign_propagates_extra(self, scratch_store, scratch_chain):
        scratch_store.register_apple_cert(**_valid_apple_inputs())
        cs.attest_sign(
            cert_id="apple.dev.team",
            artifact_path="dist/app.ipa",
            artifact_sha256="d" * 64,
            actor="runner",
            store=scratch_store,
            chain=scratch_chain,
            extra={"build_id": "run-1234"},
        )
        assert scratch_chain.entries[0]["extra"]["build_id"] == "run-1234"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. Expiry scanning
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestExpiryScanning:
    def test_severity_for_days_buckets(self):
        assert cs.severity_for_days(-5) == "critical"
        assert cs.severity_for_days(0) == "critical"
        assert cs.severity_for_days(0.5) == "critical"
        assert cs.severity_for_days(1) == "critical"
        assert cs.severity_for_days(3) == "warn"
        assert cs.severity_for_days(7) == "warn"
        assert cs.severity_for_days(15) == "notice"
        assert cs.severity_for_days(30) == "notice"
        assert cs.severity_for_days(31) is None
        assert cs.severity_for_days(365) is None

    def test_check_cert_expiries_returns_30d_and_7d_and_1d(self, scratch_store):
        base = time.time()
        # 60d → skip
        scratch_store.register_apple_cert(
            **_valid_apple_inputs(
                cert_id="far", not_after=_ts(60, base=base),
            ),
        )
        # 20d → notice
        scratch_store.register_apple_cert(
            **_valid_apple_inputs(
                cert_id="near", not_after=_ts(20, base=base),
            ),
        )
        # 3d → warn
        scratch_store.register_apple_cert(
            **_valid_apple_inputs(
                cert_id="warn", not_after=_ts(3, base=base),
            ),
        )
        # 0.5d → critical
        scratch_store.register_apple_cert(
            **_valid_apple_inputs(
                cert_id="crit", not_after=_ts(0.5, base=base),
            ),
        )
        findings = cs.check_cert_expiries(now=base, store=scratch_store)
        by_cert = {f.cert_id: f for f in findings}
        assert "far" not in by_cert
        assert by_cert["near"].severity == "notice"
        assert by_cert["near"].threshold_days == 30
        assert by_cert["warn"].severity == "warn"
        assert by_cert["warn"].threshold_days == 7
        assert by_cert["crit"].severity == "critical"
        assert by_cert["crit"].threshold_days == 1

    def test_check_cert_expiries_includes_already_expired(self, scratch_store):
        base = time.time()
        scratch_store.register_apple_cert(
            **_valid_apple_inputs(
                cert_id="dead", not_after=_ts(-5, base=base),
            ),
        )
        findings = cs.check_cert_expiries(now=base, store=scratch_store)
        assert len(findings) == 1
        assert findings[0].severity == "critical"
        assert findings[0].days_left < 0

    def test_fire_expiry_alerts_publishes_one_per_finding(self, scratch_store):
        base = time.time()
        scratch_store.register_apple_cert(
            **_valid_apple_inputs(cert_id="x", not_after=_ts(2, base=base)),
        )
        scratch_store.register_apple_cert(
            **_valid_apple_inputs(cert_id="y", not_after=_ts(25, base=base)),
        )
        captured: list[dict] = []

        def _cap(**kwargs):
            captured.append(kwargs)

        findings = cs.fire_expiry_alerts(
            now=base, store=scratch_store, publisher=_cap,
        )
        assert len(findings) == 2
        assert len(captured) == 2
        severities = {c["severity"] for c in captured}
        assert severities == {"warn", "notice"}

    def test_fire_expiry_alerts_payload_has_no_secrets(self, scratch_store):
        base = time.time()
        scratch_store.register_android_keystore(
            **_valid_android_inputs(not_after=_ts(5, base=base)),
        )
        captured: list[dict] = []
        cs.fire_expiry_alerts(
            now=base,
            store=scratch_store,
            publisher=lambda **k: captured.append(k),
        )
        assert captured
        blob = json.dumps(captured)
        assert "supersecret" not in blob
        assert "FAKE-JKS-BYTES" not in blob

    def test_expiry_thresholds_are_ordered_ascending(self):
        assert cs.EXPIRY_THRESHOLD_DAYS == (1, 7, 30)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. Store singleton + reset
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStoreSingleton:
    def test_reset_for_tests_swaps_singleton(self, tmp_path: Path):
        scratch = tmp_path / "scratch.json"
        cs._reset_for_tests(path=scratch)
        s1 = cs.get_store()
        s2 = cs.get_store()
        assert s1 is s2
        s1.register_apple_cert(**_valid_apple_inputs())
        assert len(s1.list_records()) == 1
        cs._reset_for_tests()  # back to default path (lazy)

    def test_reset_with_path_isolates_tests(self, tmp_path: Path):
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        cs._reset_for_tests(path=a)
        cs.get_store().register_apple_cert(**_valid_apple_inputs())
        cs._reset_for_tests(path=b)
        # Fresh store, no records carried across:
        assert cs.get_store().list_records() == []
        cs._reset_for_tests()
