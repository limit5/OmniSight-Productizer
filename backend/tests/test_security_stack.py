"""C15 — Security stack tests.

Covers: config loading, secure boot chain queries, boot chain verification,
TEE binding queries, TEE session simulation, attestation provider queries,
attestation quote generation/verification, SBOM signer queries, SBOM signing,
threat model queries, threat coverage evaluation, security test recipes,
SoC compatibility, cert artifacts, and REST endpoint smoke tests.
"""

import pytest

from backend.security_stack import (
    AttestationStatus,
    BootStageStatus,
    SecurityDomain,
    SecurityTestStatus,
    SigningMode,
    TEESessionState,
    ThreatCategory,
    SBOMFormat,
    check_soc_security_support,
    clear_security_certs,
    evaluate_threat_coverage,
    generate_attestation_quote,
    generate_cert_artifacts,
    get_attestation_provider,
    get_boot_chain,
    get_recipes_by_domain,
    get_sbom_signer,
    get_security_stack_certs,
    get_security_test_recipe,
    get_tee_binding,
    get_threat_model,
    list_artifact_definitions,
    list_attestation_providers,
    list_boot_chains,
    list_sbom_signers,
    list_security_test_recipes,
    list_tee_bindings,
    list_threat_models,
    register_security_cert,
    reload_security_config_for_tests,
    run_security_test,
    sign_sbom,
    simulate_tee_session,
    verify_attestation_quote,
    verify_boot_chain,
)


@pytest.fixture(autouse=True)
def _reload_config():
    reload_security_config_for_tests()
    clear_security_certs()
    yield
    reload_security_config_for_tests()
    clear_security_certs()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config loading
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfigLoading:
    def test_boot_chains_loaded(self):
        chains = list_boot_chains()
        assert len(chains) == 3

    def test_tee_bindings_loaded(self):
        bindings = list_tee_bindings()
        assert len(bindings) == 3

    def test_attestation_providers_loaded(self):
        providers = list_attestation_providers()
        assert len(providers) == 3

    def test_sbom_signers_loaded(self):
        signers = list_sbom_signers()
        assert len(signers) == 2

    def test_threat_models_loaded(self):
        models = list_threat_models()
        assert len(models) == 4

    def test_test_recipes_loaded(self):
        recipes = list_security_test_recipes()
        assert len(recipes) == 12

    def test_artifact_definitions_loaded(self):
        defs = list_artifact_definitions()
        assert len(defs) == 13


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Secure boot chain queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBootChainQueries:
    def test_arm_trustzone_chain(self):
        chain = get_boot_chain("arm_trustzone")
        assert chain is not None
        assert chain.name == "ARM TrustZone Secure Boot"
        assert len(chain.stages) == 7

    def test_arm_trustzone_stages_order(self):
        chain = get_boot_chain("arm_trustzone")
        stage_ids = [s.stage_id for s in chain.stages]
        assert stage_ids == [
            "bl1_rom", "bl2_loader", "bl31_runtime", "bl32_tee",
            "bl33_uboot", "kernel", "rootfs",
        ]

    def test_mcu_secure_boot_chain(self):
        chain = get_boot_chain("mcu_secure_boot")
        assert chain is not None
        assert len(chain.stages) == 3

    def test_uefi_secure_boot_chain(self):
        chain = get_boot_chain("uefi_secure_boot")
        assert chain is not None
        assert len(chain.stages) == 5

    def test_unknown_chain_returns_none(self):
        assert get_boot_chain("nonexistent") is None

    def test_chain_has_compatible_socs(self):
        chain = get_boot_chain("arm_trustzone")
        assert "hi3516" in chain.compatible_socs
        assert "rk3566" in chain.compatible_socs

    def test_chain_has_required_tools(self):
        chain = get_boot_chain("arm_trustzone")
        assert "arm-trusted-firmware" in chain.required_tools
        assert "veritysetup" in chain.required_tools

    def test_bl1_immutable(self):
        chain = get_boot_chain("arm_trustzone")
        bl1 = chain.stages[0]
        assert bl1.immutable is True

    def test_rollback_protection_on_stages(self):
        chain = get_boot_chain("arm_trustzone")
        for stage in chain.stages[:-1]:  # all except rootfs
            assert stage.rollback_protection is True

    def test_chain_to_dict(self):
        chain = get_boot_chain("arm_trustzone")
        d = chain.to_dict()
        assert d["chain_id"] == "arm_trustzone"
        assert len(d["stages"]) == 7
        assert isinstance(d["stages"][0], dict)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Boot chain verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBootChainVerification:
    def test_verify_all_stages_pass(self):
        chain = get_boot_chain("arm_trustzone")
        stage_results = [
            {"stage_id": s.stage_id, "status": "verified"}
            for s in chain.stages
        ]
        result = verify_boot_chain("arm_trustzone", stage_results)
        assert result.overall_status == BootStageStatus.verified
        assert "7/7 verified" in result.message

    def test_verify_partial_pending(self):
        result = verify_boot_chain("arm_trustzone", [
            {"stage_id": "bl1_rom", "status": "verified"},
            {"stage_id": "bl2_loader", "status": "verified"},
        ])
        assert result.overall_status == BootStageStatus.pending

    def test_verify_with_failure(self):
        result = verify_boot_chain("arm_trustzone", [
            {"stage_id": "bl1_rom", "status": "verified"},
            {"stage_id": "bl2_loader", "status": "failed"},
        ])
        assert result.overall_status == BootStageStatus.failed
        assert "1 stage(s) failed" in result.message

    def test_verify_unknown_chain(self):
        result = verify_boot_chain("nonexistent")
        assert result.overall_status == BootStageStatus.failed
        assert "Unknown boot chain" in result.message

    def test_verify_empty_results(self):
        result = verify_boot_chain("mcu_secure_boot")
        assert result.overall_status == BootStageStatus.pending
        assert "3 stage(s) pending" in result.message

    def test_verify_result_to_dict(self):
        result = verify_boot_chain("mcu_secure_boot")
        d = result.to_dict()
        assert d["chain_id"] == "mcu_secure_boot"
        assert "stage_results" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TEE binding queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTEEBindingQueries:
    def test_optee_binding(self):
        tee = get_tee_binding("optee")
        assert tee is not None
        assert tee.name == "OP-TEE (Open Portable TEE)"
        assert "secure_storage" in tee.features

    def test_optee_api_functions(self):
        tee = get_tee_binding("optee")
        func_names = [f.name for f in tee.api_functions]
        assert "TEEC_InitializeContext" in func_names
        assert "TEEC_OpenSession" in func_names
        assert "TEEC_InvokeCommand" in func_names
        assert "TEEC_CloseSession" in func_names
        assert "TEEC_FinalizeContext" in func_names

    def test_trustzone_m_binding(self):
        tee = get_tee_binding("trustzone_m")
        assert tee is not None
        assert "sau_configuration" in tee.features

    def test_sgx_binding(self):
        tee = get_tee_binding("sgx")
        assert tee is not None
        assert "enclave_isolation" in tee.features

    def test_unknown_tee_returns_none(self):
        assert get_tee_binding("nonexistent") is None

    def test_tee_compatible_socs(self):
        tee = get_tee_binding("optee")
        assert "hi3516" in tee.compatible_socs

    def test_tee_to_dict(self):
        tee = get_tee_binding("optee")
        d = tee.to_dict()
        assert d["tee_id"] == "optee"
        assert isinstance(d["api_functions"], list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TEE session simulation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTEESessionSimulation:
    def test_session_lifecycle_optee(self):
        result = simulate_tee_session("optee")
        assert result["state"] == TEESessionState.closed.value
        assert len(result["steps"]) == 5
        assert "simulated" in result["message"].lower()

    def test_session_lifecycle_sgx(self):
        result = simulate_tee_session("sgx")
        assert result["state"] == TEESessionState.closed.value

    def test_session_custom_uuid(self):
        uuid = "12345678-1234-1234-1234-123456789012"
        result = simulate_tee_session("optee", ta_uuid=uuid)
        assert result["ta_uuid"] == uuid

    def test_session_custom_command(self):
        result = simulate_tee_session("optee", command_id=42)
        invoke_step = result["steps"][2]
        assert invoke_step["command_id"] == 42

    def test_session_unknown_tee(self):
        result = simulate_tee_session("nonexistent")
        assert result["state"] == TEESessionState.error.value


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Attestation provider queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAttestationProviderQueries:
    def test_tpm2_provider(self):
        prov = get_attestation_provider("tpm2")
        assert prov is not None
        assert prov.name == "TPM 2.0 (Trusted Platform Module)"
        assert "pcr_measurement" in prov.features

    def test_tpm2_pcr_banks(self):
        prov = get_attestation_provider("tpm2")
        assert "sha256" in prov.pcr_banks

    def test_tpm2_pcr_assignments(self):
        prov = get_attestation_provider("tpm2")
        pcr_indices = [p.pcr_index for p in prov.pcr_assignments]
        assert 0 in pcr_indices
        assert 7 in pcr_indices

    def test_tpm2_operations(self):
        prov = get_attestation_provider("tpm2")
        op_names = [o.name for o in prov.operations]
        assert "tpm2_quote" in op_names
        assert "tpm2_seal" in op_names

    def test_ftpm_provider(self):
        prov = get_attestation_provider("ftpm")
        assert prov is not None

    def test_secure_element_provider(self):
        prov = get_attestation_provider("secure_element")
        assert prov is not None
        assert "key_generation" in prov.features

    def test_unknown_provider_returns_none(self):
        assert get_attestation_provider("nonexistent") is None

    def test_provider_to_dict(self):
        prov = get_attestation_provider("tpm2")
        d = prov.to_dict()
        assert d["provider_id"] == "tpm2"
        assert "pcr_banks" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Attestation quote generation/verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAttestationQuote:
    def test_generate_quote_tpm2(self):
        quote = generate_attestation_quote("tpm2", nonce="test-nonce")
        assert quote.status == AttestationStatus.trusted
        assert quote.nonce == "test-nonce"
        assert len(quote.pcr_values) == 5  # default [0,1,2,4,7]

    def test_generate_quote_custom_pcrs(self):
        quote = generate_attestation_quote("tpm2", pcr_indices=[0, 7, 14])
        assert len(quote.pcr_values) == 3
        assert 0 in quote.pcr_values
        assert 14 in quote.pcr_values

    def test_generate_quote_ftpm(self):
        quote = generate_attestation_quote("ftpm", nonce="abc")
        assert quote.status == AttestationStatus.trusted

    def test_generate_quote_unknown_provider(self):
        quote = generate_attestation_quote("nonexistent")
        assert quote.status == AttestationStatus.error
        assert "Unknown" in quote.message

    def test_verify_quote_self(self):
        quote = generate_attestation_quote("tpm2", nonce="verify-test")
        result = verify_attestation_quote(quote, quote.pcr_values)
        assert result["verified"] is True

    def test_verify_quote_mismatch(self):
        quote = generate_attestation_quote("tpm2", nonce="test")
        wrong_pcrs = {0: "0000000000000000000000000000000000000000000000000000000000000000"}
        result = verify_attestation_quote(quote, wrong_pcrs)
        assert result["verified"] is False
        assert "mismatch" in result["reason"].lower()

    def test_verify_quote_no_expected(self):
        quote = generate_attestation_quote("tpm2")
        result = verify_attestation_quote(quote)
        assert result["verified"] is True

    def test_quote_to_dict(self):
        quote = generate_attestation_quote("tpm2")
        d = quote.to_dict()
        assert d["provider_id"] == "tpm2"
        assert d["status"] == "trusted"

    def test_quote_deterministic_with_same_nonce(self):
        q1 = generate_attestation_quote("tpm2", nonce="same")
        q2 = generate_attestation_quote("tpm2", nonce="same")
        assert q1.pcr_values == q2.pcr_values

    def test_quote_different_with_different_nonce(self):
        q1 = generate_attestation_quote("tpm2", nonce="aaa")
        q2 = generate_attestation_quote("tpm2", nonce="bbb")
        assert q1.pcr_values != q2.pcr_values


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SBOM signer queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSBOMSignerQueries:
    def test_cosign_signer(self):
        signer = get_sbom_signer("cosign")
        assert signer is not None
        assert len(signer.signing_modes) == 3

    def test_cosign_modes(self):
        signer = get_sbom_signer("cosign")
        mode_ids = [m.mode_id for m in signer.signing_modes]
        assert "keyless" in mode_ids
        assert "key_pair" in mode_ids
        assert "kms" in mode_ids

    def test_cosign_keyless_requires_oidc(self):
        signer = get_sbom_signer("cosign")
        keyless = next(m for m in signer.signing_modes if m.mode_id == "keyless")
        assert keyless.requires_oidc is True
        assert keyless.requires_key is False

    def test_cosign_keypair_requires_key(self):
        signer = get_sbom_signer("cosign")
        kp = next(m for m in signer.signing_modes if m.mode_id == "key_pair")
        assert kp.requires_key is True

    def test_cosign_sbom_formats(self):
        signer = get_sbom_signer("cosign")
        assert "spdx" in signer.sbom_formats
        assert "cyclonedx" in signer.sbom_formats

    def test_intoto_signer(self):
        signer = get_sbom_signer("in_toto")
        assert signer is not None

    def test_unknown_signer_returns_none(self):
        assert get_sbom_signer("nonexistent") is None

    def test_signer_to_dict(self):
        signer = get_sbom_signer("cosign")
        d = signer.to_dict()
        assert d["tool_id"] == "cosign"
        assert len(d["signing_modes"]) == 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SBOM signing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSBOMSigning:
    def test_sign_stub_key_pair(self):
        result = sign_sbom("cosign", "/tmp/sbom.json", mode="key_pair", key_path="/tmp/key.pem")
        assert result.success is True
        assert result.signature_path == "/tmp/sbom.json.sig"
        assert "Stub" in result.message

    def test_sign_stub_keyless(self):
        result = sign_sbom("cosign", "/tmp/sbom.json", mode="keyless")
        assert result.success is True
        assert result.transparency_log_entry.startswith("rekor:")

    def test_sign_invalid_mode(self):
        result = sign_sbom("cosign", "/tmp/sbom.json", mode="invalid_mode")
        assert result.success is False
        assert "Invalid signing mode" in result.message

    def test_sign_key_pair_without_key(self):
        result = sign_sbom("cosign", "/tmp/sbom.json", mode="key_pair")
        assert result.success is False
        assert "requires a key_path" in result.message

    def test_sign_unknown_tool(self):
        result = sign_sbom("nonexistent_tool", "/tmp/sbom.json")
        assert result.success is False
        assert "Unknown SBOM signer" in result.message

    def test_sign_result_to_dict(self):
        result = sign_sbom("cosign", "/tmp/sbom.json", mode="key_pair", key_path="/tmp/k.pem")
        d = result.to_dict()
        assert d["tool_id"] == "cosign"
        assert d["success"] is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Threat model queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestThreatModelQueries:
    def test_embedded_product_model(self):
        model = get_threat_model("embedded_product")
        assert model is not None
        assert len(model.stride_categories) == 6

    def test_embedded_stride_categories(self):
        model = get_threat_model("embedded_product")
        cats = [c.category for c in model.stride_categories]
        assert "spoofing" in cats
        assert "tampering" in cats
        assert "repudiation" in cats
        assert "information_disclosure" in cats
        assert "denial_of_service" in cats
        assert "elevation_of_privilege" in cats

    def test_embedded_threats_have_mitigations(self):
        model = get_threat_model("embedded_product")
        for cat in model.stride_categories:
            assert len(cat.threats) > 0
            assert len(cat.mitigations) > 0

    def test_embedded_required_artifacts(self):
        model = get_threat_model("embedded_product")
        assert "threat_model_document" in model.required_artifacts
        assert "penetration_test_report" in model.required_artifacts

    def test_enterprise_web_model(self):
        model = get_threat_model("enterprise_web")
        assert model is not None
        assert "owasp_top10_checklist" in model.required_artifacts

    def test_algo_sim_model(self):
        model = get_threat_model("algo_sim")
        assert model is not None

    def test_factory_tool_model(self):
        model = get_threat_model("factory_tool")
        assert model is not None

    def test_unknown_model_returns_none(self):
        assert get_threat_model("nonexistent") is None

    def test_model_to_dict(self):
        model = get_threat_model("embedded_product")
        d = model.to_dict()
        assert d["class_id"] == "embedded_product"
        assert len(d["stride_categories"]) == 6


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Threat coverage evaluation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestThreatCoverageEvaluation:
    def test_embedded_coverage_with_defined_mitigations(self):
        result = evaluate_threat_coverage("embedded_product")
        assert result.total_threats > 0
        assert result.coverage_pct == 100.0
        assert result.unmitigated_threats == 0

    def test_enterprise_web_coverage(self):
        result = evaluate_threat_coverage("enterprise_web")
        assert result.total_threats > 0
        assert result.coverage_pct == 100.0

    def test_unknown_class_returns_zero(self):
        result = evaluate_threat_coverage("nonexistent")
        assert result.total_threats == 0
        assert result.coverage_pct == 0.0

    def test_coverage_result_to_dict(self):
        result = evaluate_threat_coverage("embedded_product")
        d = result.to_dict()
        assert d["class_id"] == "embedded_product"
        assert d["total_threats"] > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Security test recipes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSecurityTestRecipes:
    def test_boot_chain_verify_recipe(self):
        recipe = get_security_test_recipe("sec-boot-chain-verify")
        assert recipe is not None
        assert recipe.security_domain == "secure_boot"

    def test_tee_session_recipe(self):
        recipe = get_security_test_recipe("sec-tee-session-lifecycle")
        assert recipe is not None
        assert recipe.security_domain == "tee"

    def test_attestation_recipe(self):
        recipe = get_security_test_recipe("sec-attestation-quote")
        assert recipe is not None
        assert recipe.security_domain == "attestation"

    def test_sbom_sign_recipe(self):
        recipe = get_security_test_recipe("sec-sbom-sign-verify")
        assert recipe is not None
        assert recipe.security_domain == "sbom"

    def test_recipes_by_domain_secure_boot(self):
        recipes = get_recipes_by_domain("secure_boot")
        assert len(recipes) >= 3

    def test_recipes_by_domain_tee(self):
        recipes = get_recipes_by_domain("tee")
        assert len(recipes) >= 2

    def test_recipes_by_domain_attestation(self):
        recipes = get_recipes_by_domain("attestation")
        assert len(recipes) >= 2

    def test_unknown_recipe_returns_none(self):
        assert get_security_test_recipe("nonexistent") is None

    def test_recipe_to_dict(self):
        recipe = get_security_test_recipe("sec-boot-chain-verify")
        d = recipe.to_dict()
        assert d["recipe_id"] == "sec-boot-chain-verify"
        assert d["security_domain"] == "secure_boot"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Security test stub runner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSecurityTestRunner:
    def test_run_stub_test(self):
        result = run_security_test("sec-boot-chain-verify", "target-001")
        assert result.status == SecurityTestStatus.pending
        assert "Stub" in result.message
        assert result.security_domain == "secure_boot"

    def test_run_unknown_recipe(self):
        result = run_security_test("nonexistent", "target-001")
        assert result.status == SecurityTestStatus.error
        assert "Unknown recipe" in result.message

    def test_run_result_to_dict(self):
        result = run_security_test("sec-tee-session-lifecycle", "target-002")
        d = result.to_dict()
        assert d["recipe_id"] == "sec-tee-session-lifecycle"
        assert d["status"] == "pending"

    def test_run_measurements_include_tools(self):
        result = run_security_test("sec-sbom-sign-verify", "target-003")
        assert "tools" in result.measurements
        assert "cosign" in result.measurements["tools"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SoC compatibility
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSoCCompatibility:
    def test_hi3516_full_support(self):
        result = check_soc_security_support("hi3516")
        assert result["has_secure_boot"] is True
        assert result["has_tee"] is True
        assert result["has_attestation"] is True
        assert "arm_trustzone" in result["secure_boot_chains"]
        assert "optee" in result["tee_bindings"]
        assert "ftpm" in result["attestation_providers"]

    def test_x86_64_support(self):
        result = check_soc_security_support("x86_64")
        assert "uefi_secure_boot" in result["secure_boot_chains"]
        assert "sgx" in result["tee_bindings"]
        assert "tpm2" in result["attestation_providers"]

    def test_nrf52840_mcu_support(self):
        result = check_soc_security_support("nrf52840")
        assert "mcu_secure_boot" in result["secure_boot_chains"]
        assert "trustzone_m" not in result["tee_bindings"]  # nrf52840 is not ARMv8-M

    def test_stm32h7_support(self):
        result = check_soc_security_support("stm32h7")
        assert "mcu_secure_boot" in result["secure_boot_chains"]
        assert "trustzone_m" in result["tee_bindings"]

    def test_unknown_soc_no_support(self):
        result = check_soc_security_support("unknown_chip")
        assert result["has_secure_boot"] is False
        assert result["has_tee"] is False

    def test_se_universal_compat(self):
        result = check_soc_security_support("any_chip")
        assert "secure_element" in result["attestation_providers"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cert management
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCertManagement:
    def test_register_and_get_certs(self):
        register_security_cert("Secure Boot Verified", status="Active", cert_id="SB-001")
        certs = get_security_stack_certs()
        assert len(certs) == 1
        assert certs[0]["standard"] == "Secure Boot Verified"
        assert certs[0]["cert_id"] == "SB-001"

    def test_clear_certs(self):
        register_security_cert("Test")
        clear_security_certs()
        assert len(get_security_stack_certs()) == 0

    def test_multiple_certs(self):
        register_security_cert("Boot Chain", cert_id="BC-01")
        register_security_cert("TEE Verified", cert_id="TEE-01")
        register_security_cert("Attestation OK", cert_id="ATT-01")
        certs = get_security_stack_certs()
        assert len(certs) == 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cert artifact generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCertArtifactGeneration:
    def test_generate_artifacts_all_pending(self):
        artifacts = generate_cert_artifacts("secure_boot")
        assert len(artifacts) == 13
        assert all(a.status == "pending" for a in artifacts)

    def test_generate_artifacts_some_provided(self):
        artifacts = generate_cert_artifacts(
            "tee",
            spec={"provided_artifacts": ["threat_model_document", "tee_test_report"]},
        )
        provided = [a for a in artifacts if a.status == "provided"]
        pending = [a for a in artifacts if a.status == "pending"]
        assert len(provided) == 2
        assert len(pending) == 11

    def test_artifact_to_dict(self):
        artifacts = generate_cert_artifacts("attestation")
        d = artifacts[0].to_dict()
        assert "artifact_id" in d
        assert "security_domain" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Enum coverage
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEnums:
    def test_security_domain_values(self):
        assert SecurityDomain.secure_boot.value == "secure_boot"
        assert SecurityDomain.tee.value == "tee"
        assert SecurityDomain.attestation.value == "attestation"
        assert SecurityDomain.sbom.value == "sbom"
        assert SecurityDomain.key_management.value == "key_management"
        assert SecurityDomain.threat_model.value == "threat_model"

    def test_boot_stage_status_values(self):
        assert BootStageStatus.verified.value == "verified"
        assert BootStageStatus.failed.value == "failed"

    def test_attestation_status_values(self):
        assert AttestationStatus.trusted.value == "trusted"
        assert AttestationStatus.untrusted.value == "untrusted"

    def test_sbom_format_values(self):
        assert SBOMFormat.spdx.value == "spdx"
        assert SBOMFormat.cyclonedx.value == "cyclonedx"

    def test_signing_mode_values(self):
        assert SigningMode.keyless.value == "keyless"
        assert SigningMode.key_pair.value == "key_pair"
        assert SigningMode.kms.value == "kms"

    def test_threat_category_values(self):
        assert ThreatCategory.spoofing.value == "spoofing"
        assert ThreatCategory.elevation_of_privilege.value == "elevation_of_privilege"

    def test_security_test_status_values(self):
        assert SecurityTestStatus.passed.value == "passed"
        assert SecurityTestStatus.error.value == "error"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  REST endpoint smoke tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRESTEndpoints:
    @pytest.fixture
    def client(self):
        from backend.routers.security_stack import router
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_list_boot_chains(self, client):
        r = client.get("/security/boot-chains")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 3

    def test_get_boot_chain(self, client):
        r = client.get("/security/boot-chains/arm_trustzone")
        assert r.status_code == 200
        assert r.json()["chain_id"] == "arm_trustzone"

    def test_get_boot_chain_404(self, client):
        r = client.get("/security/boot-chains/nonexistent")
        assert r.status_code == 404

    def test_verify_boot_chain(self, client):
        r = client.post("/security/boot-chains/verify", json={
            "chain_id": "mcu_secure_boot",
            "stage_results": [
                {"stage_id": "rom_boot", "status": "verified"},
                {"stage_id": "mcuboot", "status": "verified"},
                {"stage_id": "app_image", "status": "verified"},
            ],
        })
        assert r.status_code == 200
        assert r.json()["overall_status"] == "verified"

    def test_list_tee_bindings(self, client):
        r = client.get("/security/tee/bindings")
        assert r.status_code == 200
        assert r.json()["count"] == 3

    def test_tee_session(self, client):
        r = client.post("/security/tee/session", json={
            "tee_id": "optee",
        })
        assert r.status_code == 200
        assert r.json()["state"] == "closed"

    def test_list_attestation_providers(self, client):
        r = client.get("/security/attestation/providers")
        assert r.status_code == 200
        assert r.json()["count"] == 3

    def test_attestation_quote(self, client):
        r = client.post("/security/attestation/quote", json={
            "provider_id": "tpm2",
            "nonce": "test-nonce",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "trusted"

    def test_list_sbom_signers(self, client):
        r = client.get("/security/sbom/signers")
        assert r.status_code == 200
        assert r.json()["count"] == 2

    def test_sbom_sign(self, client):
        r = client.post("/security/sbom/sign", json={
            "tool_id": "cosign",
            "sbom_path": "/tmp/sbom.json",
            "mode": "key_pair",
            "key_path": "/tmp/key.pem",
        })
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_list_threat_models(self, client):
        r = client.get("/security/threat-models")
        assert r.status_code == 200
        assert r.json()["count"] == 4

    def test_threat_coverage(self, client):
        r = client.post("/security/threat-models/coverage", json={
            "class_id": "embedded_product",
        })
        assert r.status_code == 200
        assert r.json()["total_threats"] > 0

    def test_list_test_recipes(self, client):
        r = client.get("/security/test/recipes")
        assert r.status_code == 200
        assert r.json()["count"] == 12

    def test_recipes_by_domain(self, client):
        r = client.get("/security/test/recipes/domain/secure_boot")
        assert r.status_code == 200
        assert r.json()["count"] >= 3

    def test_run_security_test(self, client):
        r = client.post("/security/test/run", json={
            "recipe_id": "sec-boot-chain-verify",
            "target_device": "target-001",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "pending"

    def test_soc_compat(self, client):
        r = client.post("/security/soc-compat", json={
            "soc_id": "hi3516",
        })
        assert r.status_code == 200
        assert r.json()["has_secure_boot"] is True

    def test_list_artifacts(self, client):
        r = client.get("/security/artifacts")
        assert r.status_code == 200
        assert r.json()["count"] == 13

    def test_generate_artifacts(self, client):
        r = client.post("/security/artifacts/generate", json={
            "security_domain": "secure_boot",
            "provided_artifacts": ["threat_model_document"],
        })
        assert r.status_code == 200
        artifacts = r.json()["artifacts"]
        provided = [a for a in artifacts if a["status"] == "provided"]
        assert len(provided) == 1
