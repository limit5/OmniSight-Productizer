"""C18 — L4-CORE-18 Payment / PCI compliance framework tests (#239).

Covers:
  - PCI-DSS control mapping (req 1-12, levels L1-L4)
  - PCI-DSS gate validation (pass + fail paths)
  - PCI-PTS physical security rule set
  - PCI-PTS gate validation
  - EMV L1/L2/L3 test stubs
  - EMV gate validation
  - P2PE key injection flow (DUKPT)
  - HSM integration (Thales / Utimaco / SafeNet)
  - HSM session lifecycle
  - HSM key generation + encrypt/decrypt
  - Cert artifact generator
  - Test recipe runner
  - SoC compatibility
  - Doc suite generator integration (get_payment_certs)
  - Edge cases (unknown IDs, missing data)
  - REST endpoint smoke tests
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from backend.dag_schema import DAG, Task
from backend.payment_compliance import (
    ArtifactDefinition,
    CertArtifactBundle,
    CertArtifactStatus,
    EMVLevel,
    EMVLevelDef,
    EMVTestResult,
    GateFinding,
    GateVerdict,
    HSMSession,
    HSMSessionStatus,
    HSMVendor,
    HSMVendorDef,
    KeyInjectionResult,
    KeyInjectionStatus,
    P2PEDomainDef,
    PCIDSSLevel,
    PCIDSSLevelDef,
    PCIDSSRequirement,
    PCIPTSModule,
    PCIPTSRule,
    PaymentDomain,
    PaymentGateResult,
    TestRecipe,
    TestStatus,
    clear_hsm_sessions_for_tests,
    clear_payment_certs,
    close_hsm_session,
    create_hsm_session,
    generate_cert_artifacts,
    get_artifact_definition,
    get_compatible_soc,
    get_emv_level,
    get_hsm_vendor,
    get_p2pe_domain,
    get_payment_certs,
    get_pci_dss_level,
    get_pci_dss_requirement,
    get_pci_pts_module,
    get_test_recipe,
    hsm_decrypt,
    hsm_encrypt,
    hsm_generate_key,
    list_active_hsm_sessions,
    list_artifact_definitions,
    list_compatible_socs,
    list_emv_levels,
    list_hsm_vendors,
    list_p2pe_domains,
    list_pci_dss_levels,
    list_pci_dss_requirements,
    list_pci_pts_modules,
    list_test_recipes,
    log_payment_gate_result,
    log_payment_gate_result_sync,
    register_payment_cert,
    reload_config_for_tests,
    run_emv_test_stub,
    run_p2pe_key_injection,
    run_test_recipe,
    validate_emv_gate,
    validate_pci_dss_gate,
    validate_pci_pts_gate,
    _extract_task_types,
    _normalize_pci_dss_level,
    _normalize_emv_level,
)


# ── Fixtures ─────────────────────────────────────────────────────────

def _make_dag(tasks: list[Task] | None = None, dag_id: str = "test-dag") -> DAG:
    if tasks is None:
        tasks = [
            Task(
                task_id="placeholder",
                description="placeholder task",
                required_tier="t1",
                toolchain="cmake",
                expected_output="build/out.bin",
            ),
        ]
    return DAG(dag_id=dag_id, tasks=tasks)


def _make_pci_dss_dag() -> DAG:
    return DAG(
        dag_id="pci-dss-full",
        tasks=[
            Task(task_id="net-seg", description="Run network_segmentation_test",
                 required_tier="t1", toolchain="cmake", expected_output="reports/net.xml"),
            Task(task_id="vuln-scan", description="Execute vulnerability_scan",
                 required_tier="t1", toolchain="cmake", expected_output="reports/vuln.xml"),
            Task(task_id="pentest", description="Run penetration_test",
                 required_tier="t1", toolchain="cmake", expected_output="reports/pentest.xml"),
            Task(task_id="access", description="Conduct access_review",
                 required_tier="t1", toolchain="cmake", expected_output="reports/access.xml"),
            Task(task_id="crypto", description="Run encryption_validation",
                 required_tier="t1", toolchain="cmake", expected_output="reports/crypto.xml"),
            Task(task_id="logs", description="Perform log_review",
                 required_tier="t1", toolchain="cmake", expected_output="reports/logs.xml"),
            Task(task_id="code", description="Secure code_review",
                 required_tier="t1", toolchain="cmake", expected_output="reports/code.xml"),
        ],
    )


@pytest.fixture(autouse=True)
def _reset():
    reload_config_for_tests()
    clear_payment_certs()
    clear_hsm_sessions_for_tests()
    yield
    clear_payment_certs()
    clear_hsm_sessions_for_tests()


# ── Enum tests ───────────────────────────────────────────────────────

class TestEnums:
    def test_payment_domain_values(self):
        assert PaymentDomain.pci_dss.value == "pci_dss"
        assert PaymentDomain.pci_pts.value == "pci_pts"
        assert PaymentDomain.emv.value == "emv"
        assert PaymentDomain.p2pe.value == "p2pe"
        assert PaymentDomain.hsm.value == "hsm"
        assert PaymentDomain.certification.value == "certification"

    def test_pci_dss_level_values(self):
        assert PCIDSSLevel.L1.value == "L1"
        assert PCIDSSLevel.L4.value == "L4"

    def test_emv_level_values(self):
        assert EMVLevel.L1.value == "L1"
        assert EMVLevel.L2.value == "L2"
        assert EMVLevel.L3.value == "L3"

    def test_gate_verdict_values(self):
        assert GateVerdict.passed.value == "passed"
        assert GateVerdict.failed.value == "failed"
        assert GateVerdict.error.value == "error"

    def test_hsm_vendor_values(self):
        assert HSMVendor.thales.value == "thales"
        assert HSMVendor.utimaco.value == "utimaco"
        assert HSMVendor.safenet.value == "safenet"

    def test_key_injection_status_values(self):
        assert KeyInjectionStatus.success.value == "success"
        assert KeyInjectionStatus.failed.value == "failed"

    def test_test_status_values(self):
        assert TestStatus.passed.value == "passed"
        assert TestStatus.error.value == "error"


# ── PCI-DSS config loading ──────────────────────────────────────────

class TestPCIDSSConfig:
    def test_list_levels(self):
        levels = list_pci_dss_levels()
        assert len(levels) == 4
        ids = [lv.level_id for lv in levels]
        assert "L1" in ids
        assert "L4" in ids

    def test_get_level_l1(self):
        lv = get_pci_dss_level("L1")
        assert lv is not None
        assert lv.name == "Level 1"
        assert lv.validation_type == "roc"
        assert "network_diagram" in lv.required_artifacts
        assert "penetration_test" in lv.required_dag_tasks

    def test_get_level_l4(self):
        lv = get_pci_dss_level("L4")
        assert lv is not None
        assert lv.validation_type == "saq"
        assert len(lv.required_artifacts) < len(get_pci_dss_level("L1").required_artifacts)

    def test_get_unknown_level(self):
        assert get_pci_dss_level("L99") is None

    def test_list_requirements(self):
        reqs = list_pci_dss_requirements()
        assert len(reqs) == 12
        ids = [r.req_id for r in reqs]
        assert "req_1" in ids
        assert "req_12" in ids

    def test_get_requirement(self):
        req = get_pci_dss_requirement("req_3")
        assert req is not None
        assert "encryption_inventory" in req.artifacts
        assert req.title.startswith("Protect stored")

    def test_get_unknown_requirement(self):
        assert get_pci_dss_requirement("req_99") is None


# ── PCI-DSS level normalisation ─────────────────────────────────────

class TestPCIDSSNormalization:
    def test_numeric(self):
        assert _normalize_pci_dss_level("1") == "L1"
        assert _normalize_pci_dss_level("4") == "L4"

    def test_lowercase(self):
        assert _normalize_pci_dss_level("l1") == "L1"
        assert _normalize_pci_dss_level("l3") == "L3"

    def test_level_prefix(self):
        assert _normalize_pci_dss_level("level1") == "L1"
        assert _normalize_pci_dss_level("level_2") == "L2"

    def test_passthrough(self):
        assert _normalize_pci_dss_level("L1") == "L1"
        assert _normalize_pci_dss_level("unknown") == "unknown"


# ── PCI-DSS gate validation ─────────────────────────────────────────

class TestPCIDSSGateValidation:
    def test_pass_l4_with_all_artifacts(self):
        lv = get_pci_dss_level("L4")
        dag = _make_pci_dss_dag()
        result = validate_pci_dss_gate(dag, "L4", lv.required_artifacts)
        assert result.passed
        assert result.verdict == GateVerdict.passed
        assert result.total_issues == 0 or len(result.missing_artifacts) == 0

    def test_fail_l1_missing_artifacts(self):
        dag = _make_pci_dss_dag()
        result = validate_pci_dss_gate(dag, "L1", ["network_diagram"])
        assert not result.passed
        assert result.verdict == GateVerdict.failed
        assert len(result.missing_artifacts) > 0

    def test_fail_empty_dag_missing_tasks(self):
        dag = _make_dag()
        lv = get_pci_dss_level("L1")
        result = validate_pci_dss_gate(dag, "L1", lv.required_artifacts)
        assert not result.passed
        assert len(result.missing_tasks) > 0

    def test_error_unknown_level(self):
        dag = _make_dag()
        result = validate_pci_dss_gate(dag, "L99", [])
        assert result.verdict == GateVerdict.error

    def test_normalised_level(self):
        dag = _make_pci_dss_dag()
        lv = get_pci_dss_level("L4")
        result = validate_pci_dss_gate(dag, "4", lv.required_artifacts)
        assert result.level == "L4"

    def test_findings_per_requirement(self):
        dag = _make_pci_dss_dag()
        result = validate_pci_dss_gate(dag, "L1", [])
        req_findings = [f for f in result.findings if f.category == "requirement"]
        assert len(req_findings) > 0

    def test_result_summary(self):
        dag = _make_dag()
        result = validate_pci_dss_gate(dag, "L1", [])
        assert "FAILED" in result.summary()

    def test_result_to_dict(self):
        dag = _make_dag()
        result = validate_pci_dss_gate(dag, "L1", [])
        d = result.to_dict()
        assert "standard" in d
        assert "verdict" in d
        assert "missing_artifacts" in d
        assert d["standard"] == "pci_dss"


# ── Task type extraction ────────────────────────────────────────────

class TestTaskTypeExtraction:
    def test_extract_from_full_dag(self):
        dag = _make_pci_dss_dag()
        types = _extract_task_types(dag)
        assert "vulnerability_scan" in types
        assert "penetration_test" in types
        assert "access_review" in types
        assert "encryption_validation" in types
        assert "code_review" in types

    def test_extract_empty_dag(self):
        dag = _make_dag()
        types = _extract_task_types(dag)
        assert len(types) == 0


# ── PCI-PTS ──────────────────────────────────────────────────────────

class TestPCIPTS:
    def test_list_modules(self):
        modules = list_pci_pts_modules()
        assert len(modules) >= 3
        ids = [m.module_id for m in modules]
        assert "core" in ids
        assert "sred" in ids

    def test_get_core_module(self):
        mod = get_pci_pts_module("core")
        assert mod is not None
        assert len(mod.rules) >= 4
        rule_ids = [r.rule_id for r in mod.rules]
        assert "pts_core_1" in rule_ids

    def test_get_unknown_module(self):
        assert get_pci_pts_module("nonexistent") is None

    def test_core_tamper_rule(self):
        mod = get_pci_pts_module("core")
        tamper_rule = next(r for r in mod.rules if r.rule_id == "pts_core_1")
        assert tamper_rule.severity == "critical"
        assert "tamper_detection_design" in tamper_rule.required_artifacts

    def test_pts_gate_pass(self):
        all_artifacts = []
        for mod in list_pci_pts_modules():
            for rule in mod.rules:
                all_artifacts.extend(rule.required_artifacts)
        result = validate_pci_pts_gate(list(set(all_artifacts)))
        assert result.passed

    def test_pts_gate_fail_missing(self):
        result = validate_pci_pts_gate([])
        assert not result.passed
        assert len(result.missing_artifacts) > 0
        assert len(result.findings) > 0


# ── EMV ──────────────────────────────────────────────────────────────

class TestEMV:
    def test_list_levels(self):
        levels = list_emv_levels()
        assert len(levels) == 3
        ids = [lv.level_id for lv in levels]
        assert "L1" in ids
        assert "L2" in ids
        assert "L3" in ids

    def test_get_l1(self):
        lv = get_emv_level("L1")
        assert lv is not None
        assert "contact_interface" in lv.test_categories
        assert "contactless_interface" in lv.test_categories

    def test_get_l2(self):
        lv = get_emv_level("L2")
        assert lv is not None
        assert "transaction_flow" in lv.test_categories
        assert "cardholder_verification" in lv.test_categories

    def test_get_l3(self):
        lv = get_emv_level("L3")
        assert lv is not None
        assert "brand_acceptance" in lv.test_categories

    def test_get_unknown_level(self):
        assert get_emv_level("L99") is None

    def test_normalize_emv_level(self):
        assert _normalize_emv_level("1") == "L1"
        assert _normalize_emv_level("l2") == "L2"
        assert _normalize_emv_level("L3") == "L3"

    def test_emv_gate_pass(self):
        lv = get_emv_level("L1")
        result = validate_emv_gate("L1", lv.required_artifacts)
        assert result.passed

    def test_emv_gate_fail(self):
        result = validate_emv_gate("L1", [])
        assert not result.passed

    def test_emv_gate_error_unknown(self):
        result = validate_emv_gate("L99", [])
        assert result.verdict == GateVerdict.error


# ── EMV test stubs ───────────────────────────────────────────────────

class TestEMVTestStubs:
    def test_l1_all_categories(self):
        results = run_emv_test_stub("L1")
        assert len(results) >= 4
        for r in results:
            assert r.status == TestStatus.passed
            assert len(r.test_cases) > 0

    def test_l1_single_category(self):
        results = run_emv_test_stub("L1", "contact_interface")
        assert len(results) == 1
        assert results[0].test_category == "contact_interface"
        assert results[0].status == TestStatus.passed

    def test_l2_kernel_tests(self):
        results = run_emv_test_stub("L2", "transaction_flow")
        assert len(results) == 1
        assert len(results[0].test_cases) >= 4

    def test_l2_cvm_tests(self):
        results = run_emv_test_stub("L2", "cardholder_verification")
        assert len(results) == 1
        cases = results[0].test_cases
        case_names = [c["name"] for c in cases]
        assert "Online PIN" in case_names

    def test_l3_brand_tests(self):
        results = run_emv_test_stub("L3", "brand_acceptance")
        assert len(results) == 1
        cases = results[0].test_cases
        assert any("Visa" in c["name"] for c in cases)
        assert any("Mastercard" in c["name"] for c in cases)

    def test_l3_host_integration(self):
        results = run_emv_test_stub("L3", "host_integration")
        assert len(results) == 1

    def test_unknown_level(self):
        results = run_emv_test_stub("L99")
        assert len(results) == 1
        assert results[0].status == TestStatus.error

    def test_unknown_category(self):
        results = run_emv_test_stub("L1", "nonexistent")
        assert len(results) == 1
        assert results[0].status == TestStatus.error

    def test_result_to_dict(self):
        results = run_emv_test_stub("L1", "contact_interface")
        d = results[0].to_dict()
        assert "level" in d
        assert "test_cases" in d
        assert d["status"] == "passed"

    def test_normalised_level(self):
        results = run_emv_test_stub("1")
        assert len(results) >= 4
        for r in results:
            assert r.level == "L1"


# ── P2PE ─────────────────────────────────────────────────────────────

class TestP2PE:
    def test_list_domains(self):
        domains = list_p2pe_domains()
        assert len(domains) >= 3
        ids = [d.domain_id for d in domains]
        assert "encryption" in ids
        assert "decryption" in ids
        assert "key_injection" in ids

    def test_get_encryption_domain(self):
        d = get_p2pe_domain("encryption")
        assert d is not None
        assert len(d.controls) >= 3

    def test_get_unknown_domain(self):
        assert get_p2pe_domain("nonexistent") is None

    def test_key_injection_success(self):
        result = run_p2pe_key_injection("thales", "device-001")
        assert result.status == KeyInjectionStatus.success
        assert result.key_serial_number != ""
        assert result.ipek_check_value != ""
        assert len(result.steps_completed) >= 8
        assert "hsm_session_established" in result.steps_completed
        assert "bdk_generated_in_hsm" in result.steps_completed
        assert "ipek_derived_from_bdk" in result.steps_completed
        assert "ipek_injected_to_device" in result.steps_completed

    def test_key_injection_with_utimaco(self):
        result = run_p2pe_key_injection("utimaco", "device-002")
        assert result.status == KeyInjectionStatus.success
        assert result.hsm_vendor == "utimaco"

    def test_key_injection_remote(self):
        result = run_p2pe_key_injection("thales", "device-003", "remote_key_injection")
        assert result.status == KeyInjectionStatus.success
        assert any("remote_key_injection" in s for s in result.steps_completed)

    def test_key_injection_unknown_vendor(self):
        result = run_p2pe_key_injection("unknown_hsm", "device-004")
        assert result.status == KeyInjectionStatus.failed
        assert "Unknown HSM vendor" in result.error_message

    def test_key_injection_to_dict(self):
        result = run_p2pe_key_injection("thales", "device-005")
        d = result.to_dict()
        assert d["status"] == "success"
        assert "key_serial_number" in d
        assert "steps_completed" in d

    def test_unique_ksn_per_injection(self):
        r1 = run_p2pe_key_injection("thales", "d1")
        r2 = run_p2pe_key_injection("thales", "d2")
        assert r1.key_serial_number != r2.key_serial_number


# ── HSM integration ──────────────────────────────────────────────────

class TestHSMIntegration:
    def test_list_vendors(self):
        vendors = list_hsm_vendors()
        assert len(vendors) == 3
        ids = [v.vendor_id for v in vendors]
        assert "thales" in ids
        assert "utimaco" in ids
        assert "safenet" in ids

    def test_get_thales(self):
        v = get_hsm_vendor("thales")
        assert v is not None
        assert v.name == "Thales payShield 10K"
        assert "FIPS 140-2 Level 3" in v.fips_level
        assert v.pci_pts_certified is True
        assert "AES-256" in v.supported_algorithms

    def test_get_utimaco(self):
        v = get_hsm_vendor("utimaco")
        assert v is not None
        assert "FIPS 140-2 Level 4" in v.fips_level

    def test_get_safenet(self):
        v = get_hsm_vendor("safenet")
        assert v is not None
        assert v.pci_pts_certified is False

    def test_get_unknown_vendor(self):
        assert get_hsm_vendor("nonexistent") is None

    def test_create_session(self):
        session = create_hsm_session("thales")
        assert session.status == HSMSessionStatus.connected
        assert session.vendor == "thales"
        assert session.session_id.startswith("hsm-thales-")
        assert len(session.capabilities) > 0

    def test_create_session_unknown_vendor(self):
        session = create_hsm_session("unknown")
        assert session.status == HSMSessionStatus.error

    def test_list_active_sessions(self):
        create_hsm_session("thales")
        create_hsm_session("utimaco")
        sessions = list_active_hsm_sessions()
        assert len(sessions) == 2

    def test_close_session(self):
        session = create_hsm_session("thales")
        assert close_hsm_session(session.session_id) is True
        assert len(list_active_hsm_sessions()) == 0

    def test_close_nonexistent_session(self):
        assert close_hsm_session("fake-session") is False

    def test_session_to_dict(self):
        session = create_hsm_session("thales")
        d = session.to_dict()
        assert "session_id" in d
        assert "vendor" in d
        assert d["status"] == "connected"

    def test_generate_key(self):
        session = create_hsm_session("thales")
        result = hsm_generate_key(session.session_id, "bdk", "AES-256")
        assert result["status"] == "success"
        assert "key_id" in result
        assert result["algorithm"] == "AES-256"
        assert result["command_used"] == "A0"

    def test_generate_key_unsupported_algorithm(self):
        session = create_hsm_session("thales")
        result = hsm_generate_key(session.session_id, "test", "ChaCha20-Poly1305")
        assert result["status"] == "failed"

    def test_generate_key_invalid_session(self):
        result = hsm_generate_key("fake", "bdk", "AES-256")
        assert result["status"] == "failed"

    def test_encrypt(self):
        session = create_hsm_session("thales")
        result = hsm_encrypt(session.session_id, "4111111111111111", "key-123")
        assert result["status"] == "success"
        assert "ciphertext" in result
        assert result["ciphertext"] != "4111111111111111"

    def test_encrypt_invalid_session(self):
        result = hsm_encrypt("fake", "data", "key")
        assert result["status"] == "failed"

    def test_decrypt(self):
        session = create_hsm_session("utimaco")
        result = hsm_decrypt(session.session_id, "encrypted_data", "key-123")
        assert result["status"] == "success"
        assert "plaintext" in result

    def test_decrypt_invalid_session(self):
        result = hsm_decrypt("fake", "data", "key")
        assert result["status"] == "failed"

    def test_utimaco_commands(self):
        session = create_hsm_session("utimaco")
        result = hsm_generate_key(session.session_id, "bdk", "AES-256")
        assert result["command_used"] == "CXI_KEY_GENERATE"

    def test_safenet_commands(self):
        session = create_hsm_session("safenet")
        result = hsm_generate_key(session.session_id, "aes", "AES-256")
        assert result["command_used"] == "C_GenerateKey"


# ── Cert artifact generator ─────────────────────────────────────────

class TestCertArtifactGenerator:
    def test_pci_dss_l1_full(self):
        bundle = generate_cert_artifacts("pci_dss", "L1")
        assert bundle.status == CertArtifactStatus.generated
        assert bundle.standard == "pci_dss"
        assert len(bundle.artifacts) > 0
        assert len(bundle.gap_analysis) > 0

    def test_pci_dss_l4_with_existing(self):
        lv = get_pci_dss_level("L4")
        bundle = generate_cert_artifacts("pci_dss", "L4", lv.required_artifacts)
        assert bundle.status == CertArtifactStatus.generated
        existing = [a for a in bundle.artifacts if a["status"] == "exists"]
        assert len(existing) == len(lv.required_artifacts)
        assert len(bundle.gap_analysis) == 0

    def test_emv_l1(self):
        bundle = generate_cert_artifacts("emv", "L1")
        assert bundle.status == CertArtifactStatus.generated
        assert len(bundle.artifacts) > 0

    def test_emv_l2(self):
        bundle = generate_cert_artifacts("emv", "L2")
        assert bundle.status == CertArtifactStatus.generated

    def test_pci_pts(self):
        bundle = generate_cert_artifacts("pci_pts", "")
        assert bundle.status == CertArtifactStatus.generated
        assert bundle.level == "all"
        assert len(bundle.artifacts) > 0

    def test_unknown_standard(self):
        bundle = generate_cert_artifacts("unknown", "L1")
        assert bundle.status == CertArtifactStatus.error

    def test_unknown_pci_dss_level(self):
        bundle = generate_cert_artifacts("pci_dss", "L99")
        assert bundle.status == CertArtifactStatus.error

    def test_bundle_to_dict(self):
        bundle = generate_cert_artifacts("pci_dss", "L1")
        d = bundle.to_dict()
        assert "standard" in d
        assert "artifacts" in d
        assert "gap_analysis" in d

    def test_partial_existing_artifacts(self):
        bundle = generate_cert_artifacts("pci_dss", "L1", ["network_diagram", "aoc"])
        existing = [a for a in bundle.artifacts if a["status"] == "exists"]
        templates = [a for a in bundle.artifacts if a["status"] == "template_generated"]
        assert len(existing) == 2
        assert len(templates) > 0


# ── Test recipes ─────────────────────────────────────────────────────

class TestTestRecipes:
    def test_list_recipes(self):
        recipes = list_test_recipes()
        assert len(recipes) >= 10
        ids = [r.recipe_id for r in recipes]
        assert "pci_dss_control_audit" in ids
        assert "emv_l2_kernel_test" in ids
        assert "hsm_integration_test" in ids

    def test_get_recipe(self):
        recipe = get_test_recipe("p2pe_key_injection_test")
        assert recipe is not None
        assert recipe.domain == "p2pe"
        assert len(recipe.steps) >= 5

    def test_get_unknown_recipe(self):
        assert get_test_recipe("nonexistent") is None

    def test_run_recipe(self):
        result = run_test_recipe("pci_dss_control_audit")
        assert result["status"] == "passed"
        assert result["total_steps"] > 0
        assert result["passed_steps"] == result["total_steps"]

    def test_run_hsm_recipe(self):
        result = run_test_recipe("hsm_integration_test")
        assert result["status"] == "passed"
        assert len(result["step_results"]) >= 6

    def test_run_unknown_recipe(self):
        result = run_test_recipe("nonexistent")
        assert result["status"] == "error"


# ── Artifact definitions ─────────────────────────────────────────────

class TestArtifactDefinitions:
    def test_list_definitions(self):
        defs = list_artifact_definitions()
        assert len(defs) > 30
        ids = [d.artifact_id for d in defs]
        assert "network_diagram" in ids
        assert "tamper_detection_design" in ids
        assert "emv_l1_test_plan" in ids

    def test_get_definition(self):
        d = get_artifact_definition("network_diagram")
        assert d is not None
        assert d.name == "Network Diagram"
        assert "pci" in d.file_pattern

    def test_get_unknown_definition(self):
        assert get_artifact_definition("nonexistent") is None


# ── SoC compatibility ────────────────────────────────────────────────

class TestSoCCompatibility:
    def test_list_socs(self):
        socs = list_compatible_socs()
        assert len(socs) >= 5
        soc_ids = [s["soc_id"] for s in socs]
        assert "imx8m" in soc_ids
        assert "x86_64" in soc_ids

    def test_get_soc(self):
        soc = get_compatible_soc("imx8m")
        assert soc is not None
        assert "tee_support" in soc["payment_capabilities"]

    def test_get_x86_soc(self):
        soc = get_compatible_soc("x86_64")
        assert soc is not None
        assert "sgx_support" in soc["payment_capabilities"]
        assert "pcie_for_hsm" in soc["payment_capabilities"]

    def test_get_unknown_soc(self):
        assert get_compatible_soc("nonexistent") is None


# ── Cert registry (doc_suite_generator integration) ──────────────────

class TestCertRegistry:
    def test_empty_initially(self):
        assert get_payment_certs() == []

    def test_register_cert(self):
        register_payment_cert("PCI-DSS v4.0", "Active", "PCI-123")
        certs = get_payment_certs()
        assert len(certs) == 1
        assert certs[0]["standard"] == "PCI-DSS v4.0"
        assert certs[0]["status"] == "Active"
        assert certs[0]["cert_id"] == "PCI-123"

    def test_register_multiple(self):
        register_payment_cert("PCI-DSS v4.0", "Active", "PCI-001")
        register_payment_cert("EMV L2", "Pending", "EMV-002")
        register_payment_cert("PCI-PTS v6", "Active", "PTS-003")
        certs = get_payment_certs()
        assert len(certs) == 3

    def test_clear_certs(self):
        register_payment_cert("test", "test")
        clear_payment_certs()
        assert get_payment_certs() == []


# ── Audit log integration ───────────────────────────────────────────

class TestAuditLogIntegration:
    def test_sync_log(self):
        dag = _make_dag()
        result = validate_pci_dss_gate(dag, "L1", [])
        log_payment_gate_result_sync(result)

    def test_async_log_no_audit_module(self):
        dag = _make_dag()
        result = validate_pci_dss_gate(dag, "L1", [])
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(log_payment_gate_result(result))
        finally:
            loop.close()

    def test_async_log_with_mock(self):
        dag = _make_dag()
        result = validate_pci_dss_gate(dag, "L1", [])
        mock_append = AsyncMock()
        with patch("backend.payment_compliance.logger"):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(log_payment_gate_result(result))
            finally:
                loop.close()


# ── Data model tests ─────────────────────────────────────────────────

class TestDataModels:
    def test_payment_gate_result_passed_summary(self):
        r = PaymentGateResult(standard="pci_dss", level="L1", verdict=GateVerdict.passed)
        assert "PASSED" in r.summary()

    def test_payment_gate_result_failed_summary(self):
        r = PaymentGateResult(
            standard="pci_dss", level="L1", verdict=GateVerdict.failed,
            missing_artifacts=["a1"], missing_tasks=["t1"],
            findings=[GateFinding(category="test", item="i1", message="m1")],
        )
        assert "FAILED" in r.summary()
        assert "missing artifact" in r.summary()
        assert "missing task" in r.summary()
        assert "finding" in r.summary()
        assert r.total_issues == 3

    def test_emv_test_result_to_dict(self):
        r = EMVTestResult(level="L1", test_category="contact", status=TestStatus.passed)
        d = r.to_dict()
        assert d["status"] == "passed"

    def test_key_injection_result_to_dict(self):
        r = KeyInjectionResult(device_id="d1", hsm_vendor="thales", status=KeyInjectionStatus.success)
        d = r.to_dict()
        assert d["device_id"] == "d1"

    def test_hsm_session_to_dict(self):
        s = HSMSession(session_id="s1", vendor="thales", status=HSMSessionStatus.connected)
        d = s.to_dict()
        assert d["status"] == "connected"

    def test_cert_bundle_to_dict(self):
        b = CertArtifactBundle(standard="pci_dss", level="L1", status=CertArtifactStatus.generated)
        d = b.to_dict()
        assert d["status"] == "generated"


# ── REST endpoint smoke tests ────────────────────────────────────────

class TestRESTEndpoints:
    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.routers.payment import router
        from backend import auth as _au
        app = FastAPI()
        app.dependency_overrides[_au.require_operator] = lambda: None
        app.include_router(router)
        return TestClient(app)

    def test_list_pci_dss_levels(self, client):
        resp = client.get("/payment/pci-dss/levels")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 4

    def test_list_pci_dss_requirements(self, client):
        resp = client.get("/payment/pci-dss/requirements")
        assert resp.status_code == 200
        assert resp.json()["count"] == 12

    def test_get_pci_dss_level(self, client):
        resp = client.get("/payment/pci-dss/levels/L1")
        assert resp.status_code == 200
        assert resp.json()["level_id"] == "L1"

    def test_get_pci_dss_level_404(self, client):
        resp = client.get("/payment/pci-dss/levels/L99")
        assert resp.status_code == 404

    def test_list_pci_pts_modules(self, client):
        resp = client.get("/payment/pci-pts/modules")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 3

    def test_list_emv_levels(self, client):
        resp = client.get("/payment/emv/levels")
        assert resp.status_code == 200
        assert resp.json()["count"] == 3

    def test_list_hsm_vendors(self, client):
        resp = client.get("/payment/hsm/vendors")
        assert resp.status_code == 200
        assert resp.json()["count"] == 3

    def test_list_p2pe_domains(self, client):
        resp = client.get("/payment/p2pe/domains")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 3

    def test_list_test_recipes(self, client):
        resp = client.get("/payment/test-recipes")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 10

    def test_list_artifacts(self, client):
        resp = client.get("/payment/artifacts")
        assert resp.status_code == 200
        assert resp.json()["count"] > 30

    def test_list_socs(self, client):
        resp = client.get("/payment/socs")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 5

    def test_list_certs(self, client):
        resp = client.get("/payment/certs")
        assert resp.status_code == 200


# ── Full integration scenario ────────────────────────────────────────

class TestFullIntegrationScenario:
    def test_payment_terminal_certification_flow(self):
        """Simulate a full payment terminal certification workflow."""
        artifacts = generate_cert_artifacts("pci_dss", "L1")
        assert artifacts.status == CertArtifactStatus.generated
        assert len(artifacts.gap_analysis) > 0

        pts_result = validate_pci_pts_gate([])
        assert not pts_result.passed

        emv_results = run_emv_test_stub("L1")
        assert all(r.status == TestStatus.passed for r in emv_results)

        emv_l2_results = run_emv_test_stub("L2")
        assert all(r.status == TestStatus.passed for r in emv_l2_results)

        emv_l3_results = run_emv_test_stub("L3")
        assert all(r.status == TestStatus.passed for r in emv_l3_results)

        session = create_hsm_session("thales")
        assert session.status == HSMSessionStatus.connected

        key_result = hsm_generate_key(session.session_id, "bdk", "AES-256")
        assert key_result["status"] == "success"

        injection = run_p2pe_key_injection("thales", "terminal-001")
        assert injection.status == KeyInjectionStatus.success

        enc_result = hsm_encrypt(session.session_id, "4111111111111111", key_result["key_id"])
        assert enc_result["status"] == "success"

        register_payment_cert("PCI-DSS v4.0", "Active", "PCI-2026-001")
        register_payment_cert("EMV L1+L2+L3", "Active", "EMV-2026-001")
        register_payment_cert("PCI-PTS v6", "Pending", "PTS-2026-001")
        certs = get_payment_certs()
        assert len(certs) == 3

        close_hsm_session(session.session_id)
        assert len(list_active_hsm_sessions()) == 0

    def test_pos_terminal_multi_hsm_scenario(self):
        """Test POS terminal with multiple HSM vendors."""
        for vendor in ["thales", "utimaco", "safenet"]:
            session = create_hsm_session(vendor)
            assert session.status == HSMSessionStatus.connected
            key = hsm_generate_key(session.session_id, "bdk", "AES-256")
            assert key["status"] == "success"
            close_hsm_session(session.session_id)

        assert len(list_active_hsm_sessions()) == 0
