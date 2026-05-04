"""KS.3.9 - self-hosted edition alignment for the BYOG proxy image.

The deliverable is operational documentation, not runtime code. These
tests pin the shared `omnisight-proxy` image contract and the deployment
SOP split between hosted BYOG proxy tenants and HD.21.5.2 self-hosted
edition customers.
"""

from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOP = PROJECT_ROOT / "docs" / "ops" / "self_hosted_byog_proxy_alignment.md"
README = PROJECT_ROOT / "README.md"
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "docker-publish.yml"
PROXY_DOCKERFILE = PROJECT_ROOT / "Dockerfile.omnisight-proxy"
KS38_RUNBOOK = PROJECT_ROOT / "docs" / "ops" / "tier2_to_tier3_byog_proxy_upgrade.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required KS.3.9 file: {path}"
    return path.read_text(encoding="utf-8")


def _normalized_lower(path: Path) -> str:
    return " ".join(_read(path).lower().split())


def test_alignment_sop_exists_in_docs_ops() -> None:
    assert SOP.is_file()
    assert SOP.parent == PROJECT_ROOT / "docs" / "ops"


def test_alignment_sop_keeps_byog_and_self_hosted_modes_independent() -> None:
    body = _normalized_lower(SOP)
    required = [
        "ks.3 byog proxy",
        "hd.21.5.2 self-hosted edition",
        "run the whole omnisight stack inside customer vpc",
        "only `omnisight-proxy` runs customer-side",
        "do not couple byog saas registration",
        "air-gapped bundle export",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"alignment SOP missing mode boundary terms: {missing}"


def test_alignment_sop_pins_shared_proxy_image_reference() -> None:
    body = _read(SOP)
    required = [
        "ghcr.io/${OMNISIGHT_GHCR_NAMESPACE:-your-org}/omnisight-proxy:${OMNISIGHT_IMAGE_TAG:-latest}",
        "Dockerfile.omnisight-proxy",
        "no self-hosted forked",
        "Do not publish `omnisight-proxy-self-hosted`",
        "mirrored digest must match the GHCR release digest",
        "Pin `OMNISIGHT_IMAGE_TAG` to an immutable release tag",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"alignment SOP missing shared image contract: {missing}"


def test_alignment_sop_defines_self_hosted_deployment_override() -> None:
    body = _read(SOP)
    required = [
        "omnisight-proxy:",
        "pull_policy: missing",
        "OMNISIGHT_PROXY_AUTH_ENABLED=true",
        "OMNISIGHT_PROXY_SAAS_HEARTBEAT_URL=http://backend-a:8000",
        "OMNISIGHT_PROXY_SAAS_AUDIT_URL=http://backend-a:8000",
        "OMNISIGHT_PROXY_CUSTOMER_AUDIT_LOG_FILE=/var/log/omnisight-proxy/audit.ndjson",
        "./self-hosted/proxy/providers.json:/etc/omnisight-proxy/providers.json:ro",
        "docker load -i omnisight-proxy-${OMNISIGHT_IMAGE_TAG}.tar",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"alignment SOP missing self-hosted deployment terms: {missing}"


def test_alignment_sop_preserves_byog_saas_deployment_path() -> None:
    body = _normalized_lower(SOP)
    required = [
        "docs/ops/tier2_to_tier3_byog_proxy_upgrade.md",
        "https://ai.sora-dev.app/api/v1/byog/proxies/<proxy_id>/heartbeat",
        "https://ai.sora-dev.app/api/v1/byog/proxies/<proxy_id>/audit",
        "do not reuse the self-hosted annual license activation token",
        "mtls plus signed nonce",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"alignment SOP missing BYOG SaaS terms: {missing}"
    assert KS38_RUNBOOK.is_file()


def test_alignment_sop_requires_cutover_evidence_and_no_payload_in_saas_audit() -> None:
    body = _normalized_lower(SOP)
    required = [
        "ghcr digest and customer registry digest match",
        "`local_file`, `kms`, or `vault`",
        "public saas for ks.3, local backend for hd.21.5.2",
        "omnisight-side audit metadata contains no prompt or response payload",
        "ticket records release tag, ghcr digest, mirrored digest",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"alignment SOP missing cutover evidence: {missing}"


def test_alignment_sop_defines_mode_specific_rollback_boundary() -> None:
    body = _normalized_lower(SOP)
    required = [
        "image rollback is allowed",
        "disable proxy mode before saas-side key purge",
        "roll back only the proxy service",
        "do not route provider calls out to omnisight saas as a fallback",
        "must not recreate provider keys from backups",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"alignment SOP missing rollback boundary: {missing}"


def test_publish_workflow_and_sop_agree_on_proxy_image() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    matrix = workflow["jobs"]["publish"]["strategy"]["matrix"]
    proxy_entries = [
        entry for entry in matrix["include"]
        if entry.get("image") == "omnisight-proxy"
    ]
    assert proxy_entries == [
        {
            "name": "proxy",
            "image": "omnisight-proxy",
            "dockerfile": "Dockerfile.omnisight-proxy",
        }
    ]
    assert PROXY_DOCKERFILE.is_file()
    assert "omnisight-proxy:${OMNISIGHT_IMAGE_TAG:-latest}" in _read(SOP)


def test_readme_links_alignment_sop_from_security_ops_section() -> None:
    body = _read(README)
    assert "self_hosted_byog_proxy_alignment.md" in body
    assert "KS.3.9" in body
    assert "HD.21.5.2 self-hosted edition" in body
