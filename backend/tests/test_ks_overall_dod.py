"""KS.DOD.Overall -- final rollout evidence and three-knob guard.

The deliverable is mostly operational documentation. This guard mirrors
the existing KS evidence-index tests and adds one narrow runtime smoke:
all three KS knobs can be disabled without allowing Tier 1 envelope
helpers to regress to legacy single-Fernet behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import auth, secret_store
from backend.security import cmek_revoke_detector as detector
from backend.security import cmek_wizard
from backend.security import envelope


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OVERALL = PROJECT_ROOT / "docs" / "ops" / "ks_overall_rollout_evidence.md"
OPERATOR = PROJECT_ROOT / "docs" / "ops" / "ks_operator_runbook.md"
ONBOARDING = PROJECT_ROOT / "docs" / "ops" / "ks_customer_onboarding.md"
ADR = PROJECT_ROOT / "docs" / "security" / "ks-multi-tenant-secret-management.md"
README = PROJECT_ROOT / "README.md"

TENANT = "t-acme"


@pytest.fixture(autouse=True)
def _reset_knobs(monkeypatch):
    detector._reset_for_tests()
    monkeypatch.delenv(envelope.ENVELOPE_ENABLED_ENV, raising=False)
    monkeypatch.delenv(cmek_wizard.CMEK_ENABLED_ENV, raising=False)
    monkeypatch.delenv(cmek_wizard.BYOG_ENABLED_ENV, raising=False)
    yield
    detector._reset_for_tests()
    secret_store._reset_for_tests()


def _read(path: Path) -> str:
    assert path.is_file(), f"missing KS overall DoD file: {path}"
    return path.read_text(encoding="utf-8")


def _normalized_lower(path: Path) -> str:
    return " ".join(_read(path).lower().split())


def _actor() -> auth.User:
    return auth.User(
        id="u-admin",
        email="admin@example.com",
        name="Admin",
        role="super_admin",
    )


def test_overall_evidence_doc_exists_and_defines_scope() -> None:
    body = _normalized_lower(OVERALL)

    required = [
        "ks overall rollout evidence index",
        "final ks definition of done evidence index",
        "envelope, cmek, and byog knobs disabled independently",
        "existing tier 1 / as / oauth / customer-secret behavior",
        "operator runbook coverage is complete",
        "customer onboarding material is complete for tier 1, tier 2, and tier 3",
        "current status is `dev-only`",
        "next gate",
    ]

    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"KS overall evidence doc missing scope terms: {missing}"


def test_three_knob_matrix_pins_runtime_guards() -> None:
    body = _read(OVERALL)

    for phrase in [
        "OMNISIGHT_KS_ENVELOPE_ENABLED=false",
        "OMNISIGHT_KS_CMEK_ENABLED=false",
        "OMNISIGHT_KS_BYOG_ENABLED=false",
        "Tier 1 envelope helper still encrypts/decrypts",
        "Tier 2 wizard, Tier 1 -> Tier 2 upgrade",
        "Tier 3 BYOG proxy registration",
        "backend/tests/test_security_envelope.py",
        "backend/tests/test_cmek_single_knob.py",
        "backend/tests/test_byog_single_knob.py",
        "backend/tests/test_byog_proxy_fail_fast.py",
    ]:
        assert phrase in body


@pytest.mark.asyncio
async def test_all_ks_knobs_disabled_keeps_tier1_envelope_smoke_green(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-overall-all-knobs-disabled")
    monkeypatch.setenv(envelope.ENVELOPE_ENABLED_ENV, "false")
    monkeypatch.setenv(cmek_wizard.CMEK_ENABLED_ENV, "false")
    monkeypatch.setenv(cmek_wizard.BYOG_ENABLED_ENV, "false")
    secret_store._reset_for_tests()

    ciphertext, dek_ref = envelope.encrypt("sk-ant-overall", TENANT)
    payload = json.loads(ciphertext)

    assert envelope.is_enabled() is False
    assert cmek_wizard.is_enabled() is False
    assert cmek_wizard.is_byog_enabled() is False
    assert payload["fmt"] == envelope.ENVELOPE_FORMAT_VERSION
    assert payload["tid"] == TENANT
    assert envelope.decrypt(ciphertext, dek_ref) == "sk-ant-overall"

    from backend.routers import cmek_wizard as router

    response = await router.get_cmek_settings_status(TENANT, None, _actor())
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["security_tier"] == "tier-1"
    assert body["cmek_enabled"] is False
    assert body["byog_enabled"] is False
    assert body["tier2_available"] is False
    assert body["tier3_available"] is False
    assert body["available_security_tiers"] == ["tier-1"]


@pytest.mark.asyncio
async def test_later_tier_knobs_are_independent_status_surfaces(
    monkeypatch,
) -> None:
    from backend.routers import cmek_wizard as router

    monkeypatch.setenv(cmek_wizard.CMEK_ENABLED_ENV, "true")
    monkeypatch.setenv(cmek_wizard.BYOG_ENABLED_ENV, "false")
    tier3_off = json.loads(
        (await router.get_cmek_settings_status(TENANT, None, _actor())).body
    )
    assert tier3_off["cmek_enabled"] is True
    assert tier3_off["byog_enabled"] is False
    assert tier3_off["available_security_tiers"] == ["tier-1", "tier-2"]

    monkeypatch.setenv(cmek_wizard.CMEK_ENABLED_ENV, "false")
    monkeypatch.setenv(cmek_wizard.BYOG_ENABLED_ENV, "true")
    tier2_off = json.loads(
        (await router.get_cmek_settings_status(TENANT, None, _actor())).body
    )
    assert tier2_off["cmek_enabled"] is False
    assert tier2_off["byog_enabled"] is True
    assert tier2_off["available_security_tiers"] == ["tier-1"]


def test_operator_runbook_covers_rollout_rollback_and_evidence() -> None:
    body = _read(OPERATOR)

    for phrase in [
        "KS Operator Runbook",
        "Production image and env readiness",
        "Three-knob rollback",
        "Tier 1 envelope",
        "Tier 2 CMEK",
        "Tier 3 BYOG proxy",
        "cmek_revoke_recovery.md",
        "cmek_siem_ingest.md",
        "tier2_to_tier3_byog_proxy_upgrade.md",
        "self_hosted_byog_proxy_alignment.md",
        "Final KS rollout ledger row",
        "Per-tier operator packet",
        "ks-operator-<tenant-id>-tier-<1|2|3>-<YYYYMMDD>.md",
        "provider key write/read, OAuth refresh/revoke",
        "Tier 1 -> Tier 2 rewrap, CMEK verify",
        "proxy health, one proxied provider request",
        "tenant smoke SHA-256, operator",
        "**Production status:** dev-only",
    ]:
        assert phrase in body


def test_customer_onboarding_is_complete_per_tier() -> None:
    body = _read(ONBOARDING)

    for phrase in [
        "KS Customer Onboarding Guide",
        "Tier 1 envelope",
        "Tier 2 CMEK",
        "Tier 3 BYOG proxy",
        "Choose a Tier",
        "Tier 1 Envelope Onboarding",
        "Tier 2 CMEK Onboarding",
        "Tier 3 BYOG Proxy Onboarding",
        "Completion criteria",
        "Escalation",
        "Per-tier customer handoff packet",
        "customer-facing counterpart to the operator packet",
        "Customer-owned assets",
        "launch checklist result and the exit / recovery behavior",
        "customer approval, approval timestamp",
        "Proxy unreachable or removed means OmniSight fails closed",
        "**Production status:** dev-only",
    ]:
        assert phrase in body


def test_adr_and_readme_link_overall_material() -> None:
    adr = _read(ADR)
    readme = _read(README)

    for phrase in [
        "ks_overall_rollout_evidence.md",
        "ks_operator_runbook.md",
        "ks_customer_onboarding.md",
    ]:
        assert phrase in adr
        assert phrase in readme

    for phrase in [
        "Overall Definition of Done",
        "operator runbook",
        "per-tier customer onboarding",
    ]:
        assert phrase in adr


def test_adr_completion_closure_pins_final_ks_dod() -> None:
    adr = _read(ADR)

    for phrase in [
        "ADR Completion Closure",
        "architectural source of truth + evidence index links",
        "Phase 1（Tier 1 envelope）",
        "priority_i_multi_tenancy_readiness.md",
        "backend/tests/test_ks_priority_i_readiness.py",
        "Phase 2（Tier 2 CMEK）",
        "backend/tests/test_cmek_single_knob.py",
        "Phase 3（Tier 3 BYOG proxy）",
        "backend/tests/test_byog_proxy_fail_fast.py",
        "Cross-cutting",
        "backend/tests/test_ks_cross_cutting_evidence.py",
        "Overall rollout",
        "backend/tests/test_ks_overall_dod.py",
        "Repository completion is `dev-only`",
        "Production completion is separate",
        "Accepted + complete for repository DoD",
    ]:
        assert phrase in adr
