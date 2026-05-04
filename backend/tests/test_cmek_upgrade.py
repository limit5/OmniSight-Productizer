"""KS.2.8 -- Tier 1 to Tier 2 CMEK upgrade contract tests."""

from __future__ import annotations

import inspect
import json
import re

import pytest

from backend import secret_store
from backend.security import envelope
from backend.security import kms_adapters as kms


TENANT = "t-acme"
AWS_KEY_ID = (
    "arn:aws:kms:us-east-1:111122223333:key/"
    "00000000-0000-0000-0000-000000000000"
)


class FakeCMEKAdapter:
    provider = "aws-kms"

    def wrap_dek(self, plaintext_dek, *, encryption_context=None):
        return kms.WrappedDEK(
            provider=self.provider,
            key_id=AWS_KEY_ID,
            ciphertext=b"tier2:" + plaintext_dek,
            key_version="cmk-v1",
            algorithm="aws-kms",
            encryption_context=dict(encryption_context or {}),
        )

    def unwrap_dek(self, wrapped_dek, *, encryption_context=None):
        return wrapped_dek.ciphertext.removeprefix(b"tier2:")


def _tier1_ref(monkeypatch: pytest.MonkeyPatch, value: str = "payload"):
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-2-8-upgrade")
    secret_store._reset_for_tests()
    return envelope.encrypt(value, TENANT)


def test_plan_rewraps_every_tenant_dek_without_changing_ciphertext(monkeypatch) -> None:
    from backend.security import cmek_upgrade

    ciphertext_a, dek_ref_a = _tier1_ref(monkeypatch, "payload-a")
    ciphertext_b, dek_ref_b = envelope.encrypt("payload-b", TENANT)
    target = FakeCMEKAdapter()

    result = cmek_upgrade.plan_tier1_to_tier2_upgrade(
        tenant_id=TENANT,
        provider="aws-kms",
        key_id=AWS_KEY_ID,
        dek_refs=[dek_ref_a.to_dict(), dek_ref_b.to_dict()],
        target_kms_adapter=target,
    )
    body = result.to_dict()

    assert body["status"] == "completed"
    assert body["from_security_tier"] == "tier-1"
    assert body["to_security_tier"] == "tier-2"
    assert body["progress_percent"] == 100
    assert body["completed_deks"] == 2
    assert body["failed_deks"] == 0
    assert body["persisted"] is False
    assert body["ui"]["steps"][1] == {
        "id": "rewrap",
        "label": "Rewrap DEKs with customer CMK",
        "status": "completed",
        "completed": 2,
        "failed": 0,
        "total": 2,
    }

    replacements = [
        envelope.TenantDEKRef.from_dict(i["replacement_dek_ref"])
        for i in body["items"]
    ]
    assert [r.dek_id for r in replacements] == [dek_ref_a.dek_id, dek_ref_b.dek_id]
    assert all(r.provider == "aws-kms" for r in replacements)
    assert replacements[0].wrapped_dek_b64 != dek_ref_a.wrapped_dek_b64
    assert envelope.decrypt(ciphertext_a, replacements[0], kms_adapter=target) == "payload-a"
    assert envelope.decrypt(ciphertext_b, replacements[1], kms_adapter=target) == "payload-b"


def test_plan_marks_tenant_mismatch_as_failed(monkeypatch) -> None:
    from backend.security import cmek_upgrade

    _, dek_ref = _tier1_ref(monkeypatch)
    bad = dek_ref.to_dict()
    bad["tenant_id"] = "t-other"

    result = cmek_upgrade.plan_tier1_to_tier2_upgrade(
        tenant_id=TENANT,
        provider="aws-kms",
        key_id=AWS_KEY_ID,
        dek_refs=[bad],
        target_kms_adapter=FakeCMEKAdapter(),
    )
    body = result.to_dict()

    assert body["status"] == "failed"
    assert body["progress_percent"] == 0
    assert body["failed_deks"] == 1
    assert body["items"][0]["replacement_dek_ref"] is None
    assert "tenant_id does not match" in body["items"][0]["error"]
    assert body["ui"]["current_step"] == "failed"


@pytest.mark.asyncio
async def test_upgrade_endpoint_returns_progress_payload(monkeypatch) -> None:
    from backend.routers import cmek_wizard

    class FakeResult:
        def to_dict(self):
            return {
                "upgrade_id": "cmeku_test",
                "tenant_id": TENANT,
                "status": "completed",
                "progress_percent": 100,
                "persisted": False,
                "items": [],
                "ui": {"current_step": "complete"},
            }

    async def allow_guard(_tenant_id, _actor):
        return None

    def fake_plan(**kwargs):
        assert kwargs["tenant_id"] == TENANT
        assert kwargs["provider"] == "aws-kms"
        assert kwargs["key_id"] == AWS_KEY_ID
        assert kwargs["dek_refs"] == []
        return FakeResult()

    monkeypatch.setattr(cmek_wizard, "_guard", allow_guard)
    monkeypatch.setattr(
        cmek_wizard._cmek_upgrade,
        "plan_tier1_to_tier2_upgrade",
        fake_plan,
    )

    response = await cmek_wizard.start_tier1_to_tier2_upgrade(
        TENANT,
        cmek_wizard.TierUpgradeRequest(
            provider="aws-kms",
            key_id=AWS_KEY_ID,
            dek_refs=[],
        ),
        None,
        None,
    )
    body = json.loads(response.body)

    assert body["upgrade_id"] == "cmeku_test"
    assert body["status"] == "completed"
    assert body["progress_percent"] == 100
    assert body["ui"]["current_step"] == "complete"


def test_router_exposes_tier_upgrade_endpoint():
    from backend.routers.cmek_wizard import router

    paths = {(tuple(sorted(r.methods)), r.path) for r in router.routes}

    assert (("POST",), "/tenants/{tenant_id}/cmek/tier-upgrade") in paths


def test_main_app_mounts_tier_upgrade_route():
    from backend.main import app

    paths = {
        (tuple(sorted(r.methods or [])), r.path)
        for r in app.routes
        if hasattr(r, "path")
    }

    assert (("POST",), "/api/v1/tenants/{tenant_id}/cmek/tier-upgrade") in paths


def test_module_global_state_and_source_fingerprint_clean():
    from backend.security import cmek_upgrade

    source = inspect.getsource(cmek_upgrade)
    assert "_LATEST" not in source
    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    assert not fingerprint.search(source)
