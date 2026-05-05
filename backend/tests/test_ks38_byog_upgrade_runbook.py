"""KS.3.8 - Tier 2 to Tier 3 BYOG proxy upgrade runbook tests.

The deliverable is operational documentation, not runtime code. These
tests pin the strict migration sequence: proxy deployment, OmniSight
export, customer import, SaaS-side key purge, and zero-trust rollback
boundary.
"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = PROJECT_ROOT / "docs" / "ops" / "tier2_to_tier3_byog_proxy_upgrade.md"
README = PROJECT_ROOT / "README.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required KS.3.8 file: {path}"
    return path.read_text(encoding="utf-8")


def _normalized_lower(path: Path) -> str:
    return " ".join(_read(path).lower().split())


def test_byog_upgrade_runbook_exists_in_docs_ops() -> None:
    assert RUNBOOK.is_file()
    assert RUNBOOK.parent == PROJECT_ROOT / "docs" / "ops"


def test_byog_upgrade_runbook_pins_strict_migration_sequence() -> None:
    body = _normalized_lower(RUNBOOK)
    required = [
        "omnisight export -> customer imports into proxy -> omnisight clears",
        "preconditions",
        "deploy the customer proxy",
        "export from omnisight",
        "customer import into proxy",
        "clear omnisight key material",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"BYOG upgrade runbook missing sequence: {missing}"


def test_byog_upgrade_runbook_covers_proxy_deployment_and_health() -> None:
    body = _read(RUNBOOK)
    required = [
        "OMNISIGHT_PROXY_AUTH_ENABLED=true",
        "OMNISIGHT_PROXY_PROVIDER_CONFIG_FILE",
        "OMNISIGHT_PROXY_SAAS_HEARTBEAT_URL",
        "OMNISIGHT_PROXY_CUSTOMER_AUDIT_LOG_FILE",
        "/api/v1/byog/proxies/proxy-acme-prod/health",
        "connected=true",
        "stale=false",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"BYOG upgrade runbook missing proxy deployment: {missing}"


def test_byog_upgrade_runbook_covers_export_import_key_sources() -> None:
    body = _normalized_lower(RUNBOOK)
    required = [
        "sealed to a customer migration public key",
        "provider fingerprints",
        "bundle sha-256",
        "`local_file`",
        "`kms`",
        "`vault`",
        "providers.json",
        "provider_count",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"BYOG upgrade runbook missing export/import terms: {missing}"


def test_byog_upgrade_runbook_requires_audit_split_and_zero_trust() -> None:
    body = _normalized_lower(RUNBOOK)
    required = [
        "customer proxy audit logs include full prompt and response",
        "omnisight audit metadata contains only time",
        "no prompt or response payload appears in saas logs",
        "do not silently fall back to direct provider egress",
        "proxy-unreachable no-fallback smoke returned a byog error",
        "showed no direct provider egress",
        "zero-trust exit boundary",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"BYOG upgrade runbook missing audit/zero-trust terms: {missing}"


def test_byog_upgrade_runbook_requires_saas_side_purge_evidence() -> None:
    body = _normalized_lower(RUNBOOK)
    required = [
        "clear every migrated tier 2 credential row",
        "no longer stores encrypted provider keys",
        "wrapped key material",
        "sampled decrypt",
        "failed_as_expected",
        "cleared_provider_count",
        "cleared_credential_ids",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"BYOG upgrade runbook missing purge evidence: {missing}"


def test_readme_links_byog_upgrade_runbook_from_security_ops_section() -> None:
    body = _read(README)
    assert "tier2_to_tier3_byog_proxy_upgrade.md" in body
    assert "KS.3.8" in body
    assert "BYOG proxy" in body
