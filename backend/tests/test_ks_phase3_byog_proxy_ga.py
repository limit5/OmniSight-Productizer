"""KS.DOD.Phase3 -- BYOG proxy GA evidence drift guard.

This test aggregates the already-landed KS.3 proxy contracts. It mirrors
the documentation-heavy KS.3.8/KS.3.9 tests: source files remain the
source of truth, and this guard fails when the Phase 3 DoD evidence index
drifts away from the runtime / CI / runbook artifacts.
"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GA_DOC = PROJECT_ROOT / "docs" / "ops" / "ks_phase3_byog_proxy_ga.md"
README = PROJECT_ROOT / "README.md"
PROXY_DOCKERFILE = PROJECT_ROOT / "Dockerfile.omnisight-proxy"
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "docker-publish.yml"
PROXY_SERVER_TEST = PROJECT_ROOT / "omnisight-proxy" / "internal" / "server" / "server_test.go"
PROXY_AUTH_TEST = PROJECT_ROOT / "omnisight-proxy" / "internal" / "auth" / "auth_test.go"
SAAS_CLIENT = PROJECT_ROOT / "backend" / "byog_proxy_client.py"
SAAS_CLIENT_TEST = PROJECT_ROOT / "backend" / "tests" / "test_byog_proxy_fail_fast.py"
UPGRADE_RUNBOOK = PROJECT_ROOT / "docs" / "ops" / "tier2_to_tier3_byog_proxy_upgrade.md"
SELF_HOSTED_SOP = PROJECT_ROOT / "docs" / "ops" / "self_hosted_byog_proxy_alignment.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing KS Phase 3 evidence file: {path}"
    return path.read_text(encoding="utf-8")


def _normalized_lower(path: Path) -> str:
    return " ".join(_read(path).lower().split())


def test_phase3_ga_evidence_doc_exists_and_defines_scope() -> None:
    body = _normalized_lower(GA_DOC)

    required = [
        "phase 3 definition of done",
        "tier 3 byog proxy",
        "evidence index",
        "does not cover ks.4 cross-cutting mitigations",
        "does not cover",
        "final all-ks three-knob rollout",
        "current status is `dev-only`",
        "next gate is `deployed-active`",
    ]

    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"Phase 3 GA evidence doc missing scope terms: {missing}"


def test_ga_doc_pins_proxy_image_size_and_publish_evidence() -> None:
    doc = _read(GA_DOC)
    dockerfile = _read(PROXY_DOCKERFILE)
    publish = _read(PUBLISH_WORKFLOW)

    for phrase in [
        "Dockerfile.omnisight-proxy",
        "gcr.io/distroless/static-debian12:nonroot",
        "OMNISIGHT_TEST_PROXY_IMAGE=1",
        "< 100 MB",
    ]:
        assert phrase in doc
    assert "CGO_ENABLED=0" in dockerfile
    assert 'go build -trimpath -ldflags "-s -w -buildid="' in dockerfile
    assert "USER nonroot:nonroot" in dockerfile
    assert "omnisight-proxy" in publish
    assert "Dockerfile.omnisight-proxy" in publish


def test_ga_doc_pins_latency_budget_and_ci_proxy_tests() -> None:
    doc = _read(GA_DOC)
    server_test = _read(PROXY_SERVER_TEST)
    ci = _read(CI_WORKFLOW)

    assert "p95 latency overhead < 50 ms" in doc
    assert "mTLS server" in doc
    assert "connection reuse" in doc
    assert "proxy-hop p95" in doc
    assert "const budget = 50 * time.Millisecond" in server_test
    assert "if p95 >= budget" in server_test
    assert "GotConnInfo" in server_test
    assert "if !reused" in server_test
    assert "proxy-tests:" in ci
    assert "working-directory: omnisight-proxy" in ci
    assert "go test ./..." in ci


def test_ga_doc_pins_mtls_matrix_and_replay_protection() -> None:
    doc = _read(GA_DOC)
    auth_test = _read(PROXY_AUTH_TEST)
    saas_test = _read(SAAS_CLIENT_TEST)

    for phrase in [
        "mTLS handshake matrix",
        "valid client cert",
        "pinned-cert mismatch",
        "expired cert",
        "self-signed cert",
        "Replay protection",
        "signed nonces differ across requests",
    ]:
        assert phrase in doc

    for phrase in [
        'name: "valid"',
        'name: "pinned-cert-mismatch"',
        'name: "expired"',
        'name: "self-signed"',
        "TestKS314VerifyRejectsNonceReplay",
        "TestVerifyRejectsBadSignatureWithoutConsumingNonce",
    ]:
        assert phrase in auth_test

    assert "test_signed_nonce_changes_per_request_under_fixed_timestamp" in saas_test


def test_ga_doc_pins_self_hosted_shared_image_alignment() -> None:
    doc = _read(GA_DOC)
    sop = _read(SELF_HOSTED_SOP)

    for phrase in [
        "HD.21.5 self-hosted edition shared image",
        "same GHCR `omnisight-proxy` image",
        "digest match evidence",
        "no self-hosted proxy fork",
        "mode-specific heartbeat/audit URLs",
    ]:
        assert phrase in doc

    assert "ghcr.io/${OMNISIGHT_GHCR_NAMESPACE:-your-org}/omnisight-proxy" in sop
    assert "mirrored digest must match the GHCR release digest" in sop
    assert "Do not publish `omnisight-proxy-self-hosted`" in sop
    assert "OMNISIGHT_PROXY_SAAS_HEARTBEAT_URL=http://backend-a:8000" in sop


def test_ga_doc_pins_zero_trust_no_fallback_boundary() -> None:
    doc = _normalized_lower(GA_DOC)
    client = _normalized_lower(SAAS_CLIENT)
    client_test = _read(SAAS_CLIENT_TEST)
    runbook = _normalized_lower(UPGRADE_RUNBOOK)
    sop = _normalized_lower(SELF_HOSTED_SOP)

    for phrase in [
        "strict zero-trust",
        "proxy unreachable does not fallback",
        "no direct-provider fallback hook",
        "no-fallback smoke",
        "instead of direct provider egress",
    ]:
        assert phrase in doc

    assert "must not fall back to omnisight-hosted direct provider egress" in client
    assert "no direct-provider fallback hook" in client
    assert "byogproxyunavailable" in client
    assert "byogproxyrejected" in client
    assert "direct provider fallback must not be called" in client_test
    assert "do not silently fall back to direct provider egress" in runbook
    assert "zero-trust exit boundary" in runbook
    assert "do not route provider calls out to omnisight saas as a fallback" in sop


def test_readme_links_phase3_ga_evidence() -> None:
    body = _read(README)

    assert "ks_phase3_byog_proxy_ga.md" in body
    assert "KS Phase 3 BYOG proxy GA evidence" in body
