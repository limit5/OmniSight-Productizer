"""KS.2.13 -- CMEK Phase 2 integration regression tests."""

from __future__ import annotations

import os
import json
from dataclasses import dataclass

import pytest

from backend import auth
from backend import secret_store
from backend.security import cmek_revoke_detector as detector
from backend.security import cmek_upgrade
from backend.security import envelope
from backend.security import kms_adapters as kms


TENANT = "t-acme"
TIER1_TENANT = "t-basic"
AWS_KEY_ID = (
    "arn:aws:kms:us-east-1:111122223333:key/"
    "00000000-0000-0000-0000-000000000000"
)
GCP_KEY_ID = "projects/acme-prod/locations/us/keyRings/r/cryptoKeys/k"


@dataclass
class _LiveProvider:
    provider: str
    prefix: str
    required: tuple[str, ...]

    def configured(self) -> bool:
        return all(os.environ.get(f"{self.prefix}_{name}", "").strip() for name in self.required)

    def adapter(self) -> kms.KMSAdapter:
        if self.provider == "aws-kms":
            return kms.AWSKMSAdapter.from_environment(prefix=self.prefix)
        if self.provider == "gcp-kms":
            return kms.GCPKMSAdapter.from_environment(prefix=self.prefix)
        if self.provider == "vault-transit":
            return kms.VaultTransitKMSAdapter.from_environment(prefix=self.prefix)
        raise AssertionError(f"unexpected live provider {self.provider}")


LIVE_PROVIDERS = (
    _LiveProvider("aws-kms", "OMNISIGHT_TEST_AWS_KMS", ("KEY_ID",)),
    _LiveProvider("gcp-kms", "OMNISIGHT_TEST_GCP_KMS", ("KEY_ID",)),
    _LiveProvider("vault-transit", "OMNISIGHT_TEST_VAULT_TRANSIT", ("KEY_ID", "URL", "TOKEN")),
)


class _FakeTenantCMK:
    def __init__(
        self,
        *,
        provider: str = "aws-kms",
        key_id: str = AWS_KEY_ID,
        revoked: bool = False,
    ):
        self.provider = provider
        self.key_id = key_id
        self.revoked = revoked
        self.wrap_calls = []
        self.unwrap_calls = []
        self.describe_calls = 0

    def wrap_dek(self, plaintext_dek, *, encryption_context=None):
        self.wrap_calls.append(dict(encryption_context or {}))
        if self.revoked:
            raise kms.KMSOperationError("AccessDeniedException: key revoked")
        return kms.WrappedDEK(
            provider=self.provider,
            key_id=self.key_id,
            ciphertext=b"cmek:" + bytes(plaintext_dek),
            key_version="cmk-v1",
            algorithm="fake-cmek",
            encryption_context=dict(encryption_context or {}),
        )

    def unwrap_dek(self, wrapped_dek, *, encryption_context=None):
        self.unwrap_calls.append(dict(encryption_context or {}))
        if self.revoked:
            raise kms.KMSOperationError("AccessDeniedException: key revoked")
        return wrapped_dek.ciphertext.removeprefix(b"cmek:")

    def describe_key(self):
        self.describe_calls += 1
        if self.revoked:
            raise kms.KMSOperationError(
                "AccessDeniedException: not authorized to DescribeKey",
                provider=self.provider,
                key_id=self.key_id,
            )
        return {"KeyMetadata": {"KeyState": "Enabled"}}


@pytest.fixture(autouse=True)
def _reset_cmek_state(monkeypatch):
    detector._reset_for_tests()
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-2-13-regression")
    secret_store._reset_for_tests()
    yield
    detector._reset_for_tests()
    secret_store._reset_for_tests()


def _actor() -> auth.User:
    return auth.User(
        id="u-admin",
        email="admin@example.com",
        name="Admin",
        role="super_admin",
    )


@pytest.mark.asyncio
async def test_cmek_wizard_five_step_backend_e2e(monkeypatch):
    from backend.routers import cmek_wizard

    async def allow_guard(_tenant_id, _actor, **_kwargs):
        return None

    monkeypatch.setattr(cmek_wizard, "_guard", allow_guard)

    providers = await cmek_wizard.list_cmek_wizard_providers(TENANT, None, _actor())
    provider_body = json.loads(providers.body)
    assert providers.status_code == 200
    assert [p["provider"] for p in provider_body["providers"]] == [
        "aws-kms",
        "gcp-kms",
        "vault-transit",
    ]

    policy = await cmek_wizard.generate_cmek_wizard_policy(
        TENANT,
        cmek_wizard.GeneratePolicyRequest(
            provider="aws-kms",
            principal="arn:aws:iam::444455556666:role/OmniSightCMEKAccess",
            key_id=AWS_KEY_ID,
        ),
        None,
        _actor(),
    )
    policy_body = json.loads(policy.body)
    assert policy.status_code == 200
    assert policy_body["policy"]["Statement"][1]["Action"] == [
        "kms:Encrypt",
        "kms:Decrypt",
    ]
    assert policy_body["policy"]["Statement"][1]["Condition"]["StringEquals"] == {
        "kms:EncryptionContext:tenant_id": TENANT,
        "kms:EncryptionContext:schema": "ks.1.2",
    }

    key_id = await cmek_wizard.save_cmek_wizard_key_id(
        TENANT,
        cmek_wizard.KeyIdCMEKRequest(provider="aws-kms", key_id=f" {AWS_KEY_ID} "),
        None,
        _actor(),
    )
    key_id_body = json.loads(key_id.body)
    assert key_id.status_code == 200
    assert key_id_body["accepted"] is True
    assert key_id_body["key_id"] == AWS_KEY_ID

    verify = await cmek_wizard.verify_cmek_wizard_connection(
        TENANT,
        cmek_wizard.VerifyCMEKRequest(provider="aws-kms", key_id=AWS_KEY_ID),
        None,
        _actor(),
    )
    verify_body = json.loads(verify.body)
    assert verify.status_code == 200
    assert verify_body["ok"] is True
    assert verify_body["operation"] == "encrypt-decrypt"
    assert verify_body["verification_id"].startswith("cmekv_")
    assert "plaintext" not in verify_body
    assert "ciphertext" not in verify_body

    complete = await cmek_wizard.complete_cmek_wizard(
        TENANT,
        cmek_wizard.CompleteCMEKRequest(
            provider="aws-kms",
            key_id=AWS_KEY_ID,
            verification_id=verify_body["verification_id"],
        ),
        None,
        _actor(),
    )
    complete_body = json.loads(complete.body)
    assert complete.status_code == 200
    assert complete_body["security_tier"] == "tier-2"
    assert complete_body["provider"] == "aws-kms"
    assert complete_body["config_status"] == "draft"
    assert complete_body["persisted"] is False


@pytest.mark.parametrize("live", LIVE_PROVIDERS, ids=[p.provider for p in LIVE_PROVIDERS])
def test_three_kms_adapters_live_describe_encrypt_decrypt_round_trip(live: _LiveProvider):
    if not live.configured():
        pytest.skip(f"{live.prefix} sandbox env is not configured")
    adapter = live.adapter()
    metadata = adapter.describe_key()
    assert metadata

    context = {
        "tenant_id": "ci-sandbox",
        "dek_id": f"ks-2-13-live:{live.provider}",
        "purpose": f"cmek-phase2-live:{live.provider}",
        "schema": "ks.1.2",
    }
    plaintext = f"ks-2-13-live-dek:{live.provider}".encode("ascii")
    wrapped = adapter.wrap_dek(plaintext, encryption_context=context)

    assert wrapped.provider == live.provider
    assert wrapped.ciphertext != plaintext
    assert adapter.unwrap_dek(wrapped, encryption_context=context) == plaintext


@pytest.mark.asyncio
async def test_cmek_revoke_blocks_new_wizard_requests_and_keeps_tier1_clear():
    from backend.routers import cmek_wizard

    active_cmk = _FakeTenantCMK()
    revoked_cmk = _FakeTenantCMK(revoked=True)
    ciphertext, tier1_ref = envelope.encrypt("enterprise secret", TENANT)
    upgraded = cmek_upgrade.plan_tier1_to_tier2_upgrade(
        tenant_id=TENANT,
        provider="aws-kms",
        key_id=AWS_KEY_ID,
        dek_refs=[tier1_ref.to_dict()],
        target_kms_adapter=active_cmk,
    )
    tier2_ref = envelope.TenantDEKRef.from_dict(
        upgraded.to_dict()["items"][0]["replacement_dek_ref"]
    )

    revoked = await detector.check_cmek_key_health(
        detector.CMEKKeyCheck(TENANT, revoked_cmk)
    )
    detector.record_cmek_health_result(revoked)

    blocked = await cmek_wizard.list_cmek_wizard_providers(TENANT, None, _actor())
    blocked_body = json.loads(blocked.body)
    tier1 = await cmek_wizard.get_cmek_settings_status(TIER1_TENANT, None, _actor())
    tier1_body = json.loads(tier1.body)

    assert blocked.status_code == 403
    assert blocked_body["error_code"] == "cmek_revoked"
    assert blocked_body["retryable"] is False
    assert tier1.status_code == 200
    assert tier1_body["security_tier"] == "tier-1"
    assert tier1_body["kms_health"] == "not_configured"
    with pytest.raises(kms.KMSOperationError, match="AccessDeniedException"):
        envelope.decrypt(ciphertext, tier2_ref, kms_adapter=revoked_cmk)


def test_tier_upgrade_and_downgrade_rewrap_without_changing_tenant_ciphertext():
    source_ciphertext, tier1_ref = envelope.encrypt("enterprise secret", TENANT)
    cmk = _FakeTenantCMK()

    upgraded = cmek_upgrade.plan_tier1_to_tier2_upgrade(
        tenant_id=TENANT,
        provider="aws-kms",
        key_id=AWS_KEY_ID,
        dek_refs=[tier1_ref.to_dict()],
        target_kms_adapter=cmk,
    ).to_dict()
    tier2_ref = envelope.TenantDEKRef.from_dict(
        upgraded["items"][0]["replacement_dek_ref"]
    )
    downgraded = cmek_upgrade.plan_tier2_to_tier1_downgrade(
        tenant_id=TENANT,
        provider="aws-kms",
        key_id=AWS_KEY_ID,
        dek_refs=[tier2_ref.to_dict()],
        source_kms_adapter=cmk,
    ).to_dict()
    tier1_restored_ref = envelope.TenantDEKRef.from_dict(
        downgraded["items"][0]["replacement_dek_ref"]
    )

    assert upgraded["status"] == "completed"
    assert downgraded["status"] == "completed"
    assert tier2_ref.dek_id == tier1_ref.dek_id == tier1_restored_ref.dek_id
    assert tier2_ref.provider == "aws-kms"
    assert tier1_restored_ref.provider == "local-fernet"
    assert source_ciphertext
    assert envelope.decrypt(source_ciphertext, tier2_ref, kms_adapter=cmk) == "enterprise secret"
    assert envelope.decrypt(source_ciphertext, tier1_restored_ref) == "enterprise secret"


@pytest.mark.asyncio
async def test_tier1_customer_has_zero_behavior_change_under_cmek_phase2_events():
    from backend.routers import cmek_wizard

    ciphertext, dek_ref = envelope.encrypt("tier1 stable secret", TIER1_TENANT)
    detector.record_cmek_health_result(
        detector.CMEKHealthResult(
            tenant_id=TENANT,
            provider="gcp-kms",
            key_id=GCP_KEY_ID,
            ok=False,
            revoked=True,
            reason="key_disabled",
            checked_at=2.0,
            elapsed_ms=1.0,
            raw_state="DISABLED",
            detail={"primary_state": "DISABLED"},
        )
    )

    status = await cmek_wizard.get_cmek_settings_status(TIER1_TENANT, None, _actor())
    body = json.loads(status.body)

    assert status.status_code == 200
    assert body["security_tier"] == "tier-1"
    assert body["revoke_status"] == "clear"
    assert body["available_security_tiers"] == ["tier-1", "tier-2", "tier-3"]
    assert envelope.decrypt(ciphertext, dek_ref) == "tier1 stable secret"
    assert dek_ref.provider == "local-fernet"
    assert detector.latest_cmek_health_results()[0]["tenant_id"] == TENANT
