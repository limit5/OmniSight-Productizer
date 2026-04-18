"""C16 — OTA framework tests.

Covers: config loading, A/B slot scheme queries, slot switching, delta engine
queries, delta generation/application, rollback policy queries, rollback
evaluation, signature scheme queries, firmware signing/verification, rollout
strategy queries, update manifest creation/validation, phased rollout
evaluation, OTA test recipes, SoC compatibility, cert artifacts, and REST
endpoint smoke tests.
"""

import pytest

from backend.ota_framework import (
    DeltaOperationStatus,
    ManifestValidationStatus,
    OTADomain,
    OTATestStatus,
    RollbackAction,
    RolloutPhaseStatus,
    SignatureVerifyStatus,
    SlotLabel,
    SlotSwitchStatus,
    apply_delta,
    check_soc_ota_support,
    clear_ota_certs,
    create_update_manifest,
    evaluate_rollback,
    evaluate_rollout_phase,
    generate_cert_artifacts,
    generate_delta,
    get_ab_slot_scheme,
    get_artifact_definition,
    get_delta_engine,
    get_ota_framework_certs,
    get_ota_test_recipe,
    get_recipes_by_domain,
    get_rollback_policy,
    get_rollout_strategy,
    get_signature_scheme,
    list_ab_slot_schemes,
    list_artifact_definitions,
    list_delta_engines,
    list_ota_test_recipes,
    list_rollback_policies,
    list_rollout_strategies,
    list_signature_schemes,
    reload_ota_config_for_tests,
    run_ota_test,
    sign_firmware,
    switch_ab_slot,
    validate_manifest,
    verify_firmware_signature,
)


@pytest.fixture(autouse=True)
def _reload_config():
    reload_ota_config_for_tests()
    clear_ota_certs()
    yield
    reload_ota_config_for_tests()
    clear_ota_certs()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config loading
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfigLoading:
    def test_ab_slot_schemes_loaded(self):
        schemes = list_ab_slot_schemes()
        assert len(schemes) == 3

    def test_delta_engines_loaded(self):
        engines = list_delta_engines()
        assert len(engines) == 3

    def test_rollback_policies_loaded(self):
        policies = list_rollback_policies()
        assert len(policies) == 2

    def test_signature_schemes_loaded(self):
        schemes = list_signature_schemes()
        assert len(schemes) == 3

    def test_rollout_strategies_loaded(self):
        strategies = list_rollout_strategies()
        assert len(strategies) == 3

    def test_test_recipes_loaded(self):
        recipes = list_ota_test_recipes()
        assert len(recipes) == 12

    def test_artifact_definitions_loaded(self):
        defs = list_artifact_definitions()
        assert len(defs) == 10


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  A/B slot scheme queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestABSlotSchemes:
    def test_linux_ab_scheme(self):
        scheme = get_ab_slot_scheme("linux_ab")
        assert scheme is not None
        assert scheme.name == "Linux A/B Partition"
        assert scheme.slot_count == 2
        assert len(scheme.partitions) == 5
        assert "hi3516" in scheme.compatible_socs

    def test_mcuboot_ab_scheme(self):
        scheme = get_ab_slot_scheme("mcuboot_ab")
        assert scheme is not None
        assert scheme.name == "MCUboot A/B Slot"
        assert len(scheme.partitions) == 3
        assert "nrf52840" in scheme.compatible_socs

    def test_android_ab_scheme(self):
        scheme = get_ab_slot_scheme("android_ab")
        assert scheme is not None
        assert scheme.name == "Android A/B (Seamless)"
        assert len(scheme.partitions) == 7

    def test_unknown_scheme_returns_none(self):
        assert get_ab_slot_scheme("nonexistent") is None

    def test_partition_details(self):
        scheme = get_ab_slot_scheme("linux_ab")
        boot_a = next(p for p in scheme.partitions if p.partition_id == "boot_a")
        assert boot_a.type == "boot"
        assert boot_a.slot == "A"
        assert boot_a.filesystem == "vfat"
        assert boot_a.typical_size_mb == 64

    def test_scheme_to_dict_roundtrip(self):
        scheme = get_ab_slot_scheme("linux_ab")
        d = scheme.to_dict()
        assert d["scheme_id"] == "linux_ab"
        assert len(d["partitions"]) == 5
        assert isinstance(d["compatible_socs"], list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Slot switching
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSlotSwitching:
    def test_switch_to_slot_b(self):
        result = switch_ab_slot("linux_ab", "B")
        assert result.status == SlotSwitchStatus.success
        assert result.from_slot == "A"
        assert result.to_slot == "B"

    def test_switch_to_slot_a(self):
        result = switch_ab_slot("linux_ab", "A")
        assert result.status == SlotSwitchStatus.success
        assert result.from_slot == "B"
        assert result.to_slot == "A"

    def test_switch_unknown_scheme(self):
        result = switch_ab_slot("nonexistent", "B")
        assert result.status == SlotSwitchStatus.failed

    def test_switch_invalid_slot(self):
        result = switch_ab_slot("linux_ab", "C")
        assert result.status == SlotSwitchStatus.failed

    def test_switch_result_to_dict(self):
        result = switch_ab_slot("linux_ab", "B")
        d = result.to_dict()
        assert d["status"] == "success"
        assert "timestamp" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Delta engine queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDeltaEngines:
    def test_bsdiff_engine(self):
        engine = get_delta_engine("bsdiff")
        assert engine is not None
        assert engine.name == "bsdiff/bspatch"
        assert "binary_diff" in engine.features
        assert engine.compression == "bzip2"

    def test_zchunk_engine(self):
        engine = get_delta_engine("zchunk")
        assert engine is not None
        assert "resume_capable" in engine.features
        assert engine.compression == "zstd"

    def test_rauc_engine(self):
        engine = get_delta_engine("rauc")
        assert engine is not None
        assert "bundle_verification" in engine.features

    def test_unknown_engine_returns_none(self):
        assert get_delta_engine("nonexistent") is None

    def test_engine_commands(self):
        engine = get_delta_engine("bsdiff")
        assert "generate" in engine.commands
        assert "apply" in engine.commands

    def test_engine_to_dict(self):
        engine = get_delta_engine("bsdiff")
        d = engine.to_dict()
        assert d["engine_id"] == "bsdiff"
        assert isinstance(d["features"], list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Delta generation & application
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDeltaOperations:
    def test_generate_delta_bsdiff(self):
        result = generate_delta("bsdiff", "/old/fw.img", "/new/fw.img")
        assert result.status == DeltaOperationStatus.success
        assert result.operation == "generate"
        assert result.patch_size_bytes > 0
        assert result.old_hash
        assert result.new_hash

    def test_generate_delta_zchunk(self):
        result = generate_delta("zchunk", "/old/rootfs.img", "/new/rootfs.img")
        assert result.status == DeltaOperationStatus.success

    def test_generate_delta_unknown_engine(self):
        result = generate_delta("fake", "/old/fw.img", "/new/fw.img")
        assert result.status == DeltaOperationStatus.failed

    def test_apply_delta_bsdiff(self):
        result = apply_delta("bsdiff", "/old/fw.img", "/delta.patch")
        assert result.status == DeltaOperationStatus.success
        assert result.operation == "apply"

    def test_apply_delta_unknown_engine(self):
        result = apply_delta("fake", "/old/fw.img", "/delta.patch")
        assert result.status == DeltaOperationStatus.failed

    def test_generate_custom_output_path(self):
        result = generate_delta("bsdiff", "/old/fw.img", "/new/fw.img", "/out/patch.bin")
        assert result.patch_path == "/out/patch.bin"

    def test_delta_result_to_dict(self):
        result = generate_delta("bsdiff", "/old/fw.img", "/new/fw.img")
        d = result.to_dict()
        assert d["status"] == "success"
        assert d["operation"] == "generate"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rollback policy queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRollbackPolicies:
    def test_watchdog_bootcount_policy(self):
        policy = get_rollback_policy("watchdog_bootcount")
        assert policy is not None
        assert policy.max_boot_attempts == 3
        assert policy.watchdog_timeout_s == 120
        assert len(policy.triggers) == 4

    def test_mcuboot_confirm_policy(self):
        policy = get_rollback_policy("mcuboot_confirm")
        assert policy is not None
        assert policy.max_boot_attempts == 1

    def test_unknown_policy_returns_none(self):
        assert get_rollback_policy("nonexistent") is None

    def test_policy_triggers(self):
        policy = get_rollback_policy("watchdog_bootcount")
        trigger_ids = [t.trigger_id for t in policy.triggers]
        assert "watchdog_timeout" in trigger_ids
        assert "boot_count_exceeded" in trigger_ids
        assert "health_check_fail" in trigger_ids

    def test_policy_bootloader_vars(self):
        policy = get_rollback_policy("watchdog_bootcount")
        var_names = [v.name for v in policy.bootloader_vars]
        assert "bootcount" in var_names
        assert "active_slot" in var_names

    def test_policy_health_check(self):
        policy = get_rollback_policy("watchdog_bootcount")
        assert policy.health_check is not None
        assert policy.health_check.endpoint == "/api/health"
        assert policy.health_check.retries == 3

    def test_policy_to_dict(self):
        policy = get_rollback_policy("watchdog_bootcount")
        d = policy.to_dict()
        assert d["policy_id"] == "watchdog_bootcount"
        assert d["health_check"] is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rollback evaluation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRollbackEvaluation:
    def test_healthy_boot_no_rollback(self):
        result = evaluate_rollback("watchdog_bootcount", boot_count=1, watchdog_fired=False, health_ok=True)
        assert result.action == RollbackAction.none
        assert result.health_ok is True

    def test_watchdog_fired_triggers_reboot(self):
        result = evaluate_rollback("watchdog_bootcount", boot_count=1, watchdog_fired=True, health_ok=True)
        assert result.action == RollbackAction.reboot
        assert result.triggered_by == "watchdog_timeout"

    def test_bootcount_exceeded_triggers_rollback(self):
        result = evaluate_rollback("watchdog_bootcount", boot_count=3, watchdog_fired=False, health_ok=True)
        assert result.action == RollbackAction.rollback
        assert result.triggered_by == "boot_count_exceeded"

    def test_bootcount_below_max_no_rollback(self):
        result = evaluate_rollback("watchdog_bootcount", boot_count=2, watchdog_fired=False, health_ok=True)
        assert result.action == RollbackAction.none

    def test_health_fail_triggers_rollback(self):
        result = evaluate_rollback("watchdog_bootcount", boot_count=1, watchdog_fired=False, health_ok=False)
        assert result.action == RollbackAction.mark_bad_and_rollback
        assert result.triggered_by == "health_check_fail"

    def test_mcuboot_unconfirmed_reverts(self):
        result = evaluate_rollback("mcuboot_confirm", boot_count=1, watchdog_fired=False, health_ok=True)
        assert result.action == RollbackAction.revert
        assert result.triggered_by == "unconfirmed_reboot"

    def test_mcuboot_watchdog_reboot_and_revert(self):
        result = evaluate_rollback("mcuboot_confirm", boot_count=0, watchdog_fired=True, health_ok=True)
        assert result.action == RollbackAction.reboot_and_revert

    def test_unknown_policy(self):
        result = evaluate_rollback("nonexistent", boot_count=5)
        assert result.action == RollbackAction.none
        assert "Unknown" in result.message

    def test_watchdog_priority_over_bootcount(self):
        result = evaluate_rollback("watchdog_bootcount", boot_count=5, watchdog_fired=True, health_ok=False)
        assert result.triggered_by == "watchdog_timeout"

    def test_rollback_eval_to_dict(self):
        result = evaluate_rollback("watchdog_bootcount", boot_count=3)
        d = result.to_dict()
        assert d["action"] == "rollback"
        assert d["boot_count"] == 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Signature scheme queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSignatureSchemes:
    def test_ed25519_scheme(self):
        scheme = get_signature_scheme("ed25519_direct")
        assert scheme is not None
        assert scheme.algorithm == "ed25519"
        assert scheme.signature_size_bytes == 64
        assert "fast_verify" in scheme.features

    def test_x509_chain_scheme(self):
        scheme = get_signature_scheme("x509_chain")
        assert scheme is not None
        assert scheme.algorithm == "ecdsa-p256"
        assert "certificate_chain" in scheme.features
        assert len(scheme.verification_flow) > 3

    def test_mcuboot_ecdsa_scheme(self):
        scheme = get_signature_scheme("mcuboot_ecdsa")
        assert scheme is not None
        assert "tlv_metadata" in scheme.features

    def test_unknown_scheme_returns_none(self):
        assert get_signature_scheme("nonexistent") is None

    def test_verification_flow_steps(self):
        scheme = get_signature_scheme("ed25519_direct")
        step_names = [s.step for s in scheme.verification_flow]
        assert "compute_hash" in step_names
        assert "verify_signature" in step_names
        assert "check_version" in step_names

    def test_key_management_info(self):
        scheme = get_signature_scheme("ed25519_direct")
        assert "generation" in scheme.key_management
        assert "public_extract" in scheme.key_management

    def test_scheme_to_dict(self):
        scheme = get_signature_scheme("ed25519_direct")
        d = scheme.to_dict()
        assert d["algorithm"] == "ed25519"
        assert isinstance(d["verification_flow"], list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Firmware signing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFirmwareSigning:
    def test_sign_ed25519(self):
        result = sign_firmware("ed25519_direct", "/firmware/v2.0.img", "/keys/ota.key")
        assert result.success is True
        assert "ed25519" in result.signature
        assert result.signature_path == "/firmware/v2.0.img.sig"

    def test_sign_x509(self):
        result = sign_firmware("x509_chain", "/firmware/v2.0.img")
        assert result.success is True
        assert "ecdsa-p256" in result.signature

    def test_sign_mcuboot(self):
        result = sign_firmware("mcuboot_ecdsa", "/firmware/app.bin")
        assert result.success is True

    def test_sign_unknown_scheme(self):
        result = sign_firmware("nonexistent", "/firmware/v2.0.img")
        assert result.success is False
        assert "Unknown" in result.message

    def test_sign_result_to_dict(self):
        result = sign_firmware("ed25519_direct", "/firmware/v2.0.img")
        d = result.to_dict()
        assert d["success"] is True
        assert "signature" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Firmware verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFirmwareVerification:
    def test_verify_valid_ed25519(self):
        result = verify_firmware_signature("ed25519_direct", "/firmware/v2.0.img")
        assert result.status == SignatureVerifyStatus.valid
        assert len(result.steps_passed) > 0
        assert len(result.steps_failed) == 0

    def test_verify_tampered_ed25519(self):
        result = verify_firmware_signature("ed25519_direct", "/firmware/v2.0.img", tampered=True)
        assert result.status == SignatureVerifyStatus.invalid
        assert len(result.steps_failed) > 0
        assert "verify_signature" in result.steps_failed

    def test_verify_valid_x509(self):
        result = verify_firmware_signature("x509_chain", "/firmware/v2.0.img")
        assert result.status == SignatureVerifyStatus.valid

    def test_verify_tampered_x509(self):
        result = verify_firmware_signature("x509_chain", "/firmware/v2.0.img", tampered=True)
        assert result.status == SignatureVerifyStatus.invalid

    def test_verify_valid_mcuboot(self):
        result = verify_firmware_signature("mcuboot_ecdsa", "/firmware/app.bin")
        assert result.status == SignatureVerifyStatus.valid

    def test_verify_tampered_mcuboot(self):
        result = verify_firmware_signature("mcuboot_ecdsa", "/firmware/app.bin", tampered=True)
        assert result.status == SignatureVerifyStatus.invalid

    def test_verify_unknown_scheme(self):
        result = verify_firmware_signature("nonexistent", "/firmware/v2.0.img")
        assert result.status == SignatureVerifyStatus.error

    def test_verify_result_to_dict(self):
        result = verify_firmware_signature("ed25519_direct", "/firmware/v2.0.img")
        d = result.to_dict()
        assert d["status"] == "valid"
        assert isinstance(d["steps_passed"], list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rollout strategy queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRolloutStrategies:
    def test_immediate_strategy(self):
        strategy = get_rollout_strategy("immediate")
        assert strategy is not None
        assert strategy.name == "Immediate"
        assert len(strategy.phases) == 0

    def test_canary_strategy(self):
        strategy = get_rollout_strategy("canary")
        assert strategy is not None
        assert len(strategy.phases) == 3
        canary = strategy.phases[0]
        assert canary.phase_id == "canary"
        assert canary.percentage == 1
        assert canary.health_gate is not None

    def test_staged_strategy(self):
        strategy = get_rollout_strategy("staged")
        assert strategy is not None
        assert len(strategy.phases) == 3
        internal = strategy.phases[0]
        assert internal.selector == "group=internal"

    def test_unknown_strategy_returns_none(self):
        assert get_rollout_strategy("nonexistent") is None

    def test_canary_health_gate(self):
        strategy = get_rollout_strategy("canary")
        canary = strategy.phases[0]
        assert canary.health_gate.max_crash_rate_pct == 0.5
        assert canary.health_gate.min_success_rate_pct == 99.0

    def test_general_phase_no_gate(self):
        strategy = get_rollout_strategy("canary")
        general = strategy.phases[2]
        assert general.phase_id == "general"
        assert general.health_gate is None

    def test_strategy_to_dict(self):
        strategy = get_rollout_strategy("canary")
        d = strategy.to_dict()
        assert d["strategy_id"] == "canary"
        assert len(d["phases"]) == 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Manifest creation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestManifestCreation:
    def test_create_basic_manifest(self):
        images = [{"image_id": "rootfs", "sha256": "abc123", "url": "https://example.com/fw.img", "size_bytes": 1024}]
        manifest = create_update_manifest("2.0.0", images)
        assert manifest.firmware_version == "2.0.0"
        assert manifest.manifest_id
        assert manifest.signature
        assert manifest.created_at

    def test_create_manifest_with_rollout(self):
        images = [{"image_id": "rootfs", "sha256": "abc123", "url": "https://example.com/fw.img", "size_bytes": 1024}]
        manifest = create_update_manifest("2.0.0", images, rollout_strategy="canary")
        assert manifest.rollout_strategy == "canary"
        assert len(manifest.rollout_phases) == 3

    def test_create_manifest_with_notes(self):
        images = [{"image_id": "rootfs", "sha256": "abc123", "url": "https://example.com/fw.img", "size_bytes": 1024}]
        manifest = create_update_manifest("2.0.0", images, release_notes="Bug fixes")
        assert manifest.release_notes == "Bug fixes"

    def test_manifest_to_dict(self):
        images = [{"image_id": "rootfs", "sha256": "abc123", "url": "https://example.com/fw.img", "size_bytes": 1024}]
        manifest = create_update_manifest("2.0.0", images)
        d = manifest.to_dict()
        assert "manifest_id" in d
        assert d["firmware_version"] == "2.0.0"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Manifest validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestManifestValidation:
    def test_valid_manifest(self):
        data = {
            "manifest_id": "test-123",
            "firmware_version": "2.0.0",
            "images": [{"sha256": "abc123", "url": "https://example.com/fw.img"}],
            "signature": "sig-data",
            "signature_scheme": "ed25519_direct",
        }
        result = validate_manifest(data)
        assert result.status == ManifestValidationStatus.valid
        assert len(result.errors) == 0

    def test_missing_firmware_version(self):
        data = {
            "manifest_id": "test-123",
            "images": [{"sha256": "abc123"}],
            "signature": "sig-data",
            "signature_scheme": "ed25519_direct",
        }
        result = validate_manifest(data)
        assert result.status == ManifestValidationStatus.invalid
        assert any("firmware_version" in e for e in result.errors)

    def test_missing_images(self):
        data = {
            "manifest_id": "test-123",
            "firmware_version": "2.0.0",
            "images": [],
            "signature": "sig-data",
            "signature_scheme": "ed25519_direct",
        }
        result = validate_manifest(data)
        assert result.status == ManifestValidationStatus.invalid

    def test_image_missing_hash(self):
        data = {
            "manifest_id": "test-123",
            "firmware_version": "2.0.0",
            "images": [{"url": "https://example.com/fw.img"}],
            "signature": "sig-data",
            "signature_scheme": "ed25519_direct",
        }
        result = validate_manifest(data)
        assert result.status == ManifestValidationStatus.invalid

    def test_unknown_signature_scheme_warning(self):
        data = {
            "manifest_id": "test-123",
            "firmware_version": "2.0.0",
            "images": [{"sha256": "abc123", "url": "https://example.com/fw.img"}],
            "signature": "sig-data",
            "signature_scheme": "unknown_algo",
        }
        result = validate_manifest(data)
        assert result.status == ManifestValidationStatus.valid
        assert len(result.warnings) > 0

    def test_validation_to_dict(self):
        data = {
            "manifest_id": "test-123",
            "firmware_version": "2.0.0",
            "images": [{"sha256": "abc123", "url": "https://example.com/fw.img"}],
            "signature": "sig-data",
            "signature_scheme": "ed25519_direct",
        }
        result = validate_manifest(data)
        d = result.to_dict()
        assert d["status"] == "valid"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phased rollout evaluation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRolloutEvaluation:
    def test_canary_phase_passes(self):
        metrics = {"crash_rate_pct": 0.1, "rollback_rate_pct": 0.2, "success_rate_pct": 99.8}
        result = evaluate_rollout_phase("canary", "canary", metrics)
        assert result.status == RolloutPhaseStatus.passed
        assert result.gate_passed is True

    def test_canary_phase_fails_high_crash(self):
        metrics = {"crash_rate_pct": 2.0, "rollback_rate_pct": 0.1, "success_rate_pct": 98.0}
        result = evaluate_rollout_phase("canary", "canary", metrics)
        assert result.status == RolloutPhaseStatus.failed
        assert result.gate_passed is False

    def test_canary_phase_fails_low_success(self):
        metrics = {"crash_rate_pct": 0.1, "rollback_rate_pct": 0.1, "success_rate_pct": 90.0}
        result = evaluate_rollout_phase("canary", "canary", metrics)
        assert result.status == RolloutPhaseStatus.failed

    def test_general_phase_no_gate(self):
        result = evaluate_rollout_phase("canary", "general", {})
        assert result.status == RolloutPhaseStatus.passed
        assert result.gate_passed is True

    def test_unknown_strategy(self):
        result = evaluate_rollout_phase("nonexistent", "canary", {})
        assert result.status == RolloutPhaseStatus.failed

    def test_unknown_phase(self):
        result = evaluate_rollout_phase("canary", "nonexistent", {})
        assert result.status == RolloutPhaseStatus.failed

    def test_staged_internal_passes(self):
        metrics = {"crash_rate_pct": 0.05, "rollback_rate_pct": 0.1, "success_rate_pct": 99.9}
        result = evaluate_rollout_phase("staged", "internal", metrics)
        assert result.status == RolloutPhaseStatus.passed

    def test_staged_internal_fails_high_rollback(self):
        metrics = {"crash_rate_pct": 0.05, "rollback_rate_pct": 1.0, "success_rate_pct": 99.0}
        result = evaluate_rollout_phase("staged", "internal", metrics)
        assert result.status == RolloutPhaseStatus.failed

    def test_rollout_eval_to_dict(self):
        metrics = {"crash_rate_pct": 0.1, "rollback_rate_pct": 0.2, "success_rate_pct": 99.8}
        result = evaluate_rollout_phase("canary", "canary", metrics)
        d = result.to_dict()
        assert d["gate_passed"] is True
        assert d["status"] == "passed"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test recipes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestOTATestRecipes:
    def test_get_slot_switch_recipe(self):
        recipe = get_ota_test_recipe("ota-ab-slot-switch")
        assert recipe is not None
        assert recipe.category == "partition"

    def test_get_delta_recipe(self):
        recipe = get_ota_test_recipe("ota-delta-generate-apply")
        assert recipe is not None
        assert recipe.category == "delta"

    def test_get_rollback_recipe(self):
        recipe = get_ota_test_recipe("ota-rollback-watchdog")
        assert recipe is not None
        assert recipe.category == "rollback"

    def test_get_signature_recipe(self):
        recipe = get_ota_test_recipe("ota-signature-ed25519")
        assert recipe is not None
        assert recipe.category == "signature"

    def test_get_full_cycle_recipe(self):
        recipe = get_ota_test_recipe("ota-full-cycle")
        assert recipe is not None
        assert recipe.category == "integration"

    def test_unknown_recipe(self):
        assert get_ota_test_recipe("nonexistent") is None

    def test_recipes_by_domain(self):
        sig_recipes = get_recipes_by_domain("signature")
        assert len(sig_recipes) == 3

    def test_recipes_by_domain_integration(self):
        int_recipes = get_recipes_by_domain("integration")
        assert len(int_recipes) == 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OTA test runner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestOTATestRunner:
    def test_run_partition_test(self):
        result = run_ota_test("ota-ab-slot-switch")
        assert result.passed
        assert result.measurements["slot_switch_time_ms"] > 0

    def test_run_delta_test(self):
        result = run_ota_test("ota-delta-generate-apply")
        assert result.passed
        assert "delta_size_ratio" in result.measurements

    def test_run_rollback_test(self):
        result = run_ota_test("ota-rollback-watchdog")
        assert result.passed
        assert result.measurements["rollback_triggered"] is True

    def test_run_signature_test(self):
        result = run_ota_test("ota-signature-ed25519")
        assert result.passed
        assert result.measurements["verify_time_ms"] > 0

    def test_run_server_test(self):
        result = run_ota_test("ota-manifest-parse")
        assert result.passed
        assert result.measurements["manifest_valid"] is True

    def test_run_integration_test(self):
        result = run_ota_test("ota-full-cycle")
        assert result.passed
        assert "total_cycle_time_ms" in result.measurements

    def test_run_unknown_recipe(self):
        result = run_ota_test("nonexistent")
        assert result.status == OTATestStatus.error

    def test_run_with_custom_device(self):
        result = run_ota_test("ota-ab-slot-switch", target_device="rk3566_board")
        assert result.target_device == "rk3566_board"

    def test_run_result_to_dict(self):
        result = run_ota_test("ota-full-cycle")
        d = result.to_dict()
        assert d["status"] == "passed"
        assert d["ota_domain"] == "integration"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SoC OTA compatibility
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSoCCompatibility:
    def test_hi3516_supported(self):
        result = check_soc_ota_support("hi3516")
        assert result["supported"] is True
        assert "linux_ab" in result["compatible_ab_schemes"]

    def test_nrf52840_supported(self):
        result = check_soc_ota_support("nrf52840")
        assert result["supported"] is True
        assert "mcuboot_ab" in result["compatible_ab_schemes"]

    def test_rk3566_multiple_schemes(self):
        result = check_soc_ota_support("rk3566")
        assert result["supported"] is True
        assert len(result["compatible_ab_schemes"]) >= 2

    def test_unknown_soc_not_supported(self):
        result = check_soc_ota_support("unknown_chip")
        assert result["supported"] is False
        assert len(result["compatible_ab_schemes"]) == 0

    def test_signature_schemes_always_present(self):
        result = check_soc_ota_support("hi3516")
        assert len(result["signature_schemes"]) == 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Artifact definitions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestArtifactDefinitions:
    def test_list_all_artifacts(self):
        defs = list_artifact_definitions()
        assert len(defs) == 10
        ids = [d["artifact_id"] for d in defs]
        assert "ota_partition_layout" in ids
        assert "ota_signing_keypair" in ids
        assert "ota_update_manifest" in ids

    def test_get_specific_artifact(self):
        defn = get_artifact_definition("ota_signing_keypair")
        assert defn is not None
        assert defn["name"] == "OTA Signing Keypair"

    def test_unknown_artifact(self):
        assert get_artifact_definition("nonexistent") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cert artifact generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCertArtifacts:
    def test_generate_cert_artifacts(self):
        certs = generate_cert_artifacts()
        assert len(certs) == 10
        assert certs[0].status == "generated"

    def test_certs_registered_after_generate(self):
        generate_cert_artifacts()
        certs = get_ota_framework_certs()
        assert len(certs) == 10

    def test_clear_certs(self):
        generate_cert_artifacts()
        clear_ota_certs()
        assert len(get_ota_framework_certs()) == 0

    def test_cert_to_dict(self):
        certs = generate_cert_artifacts()
        d = certs[0].to_dict()
        assert "artifact_id" in d
        assert "ota_domain" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Enum coverage
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEnums:
    def test_ota_domain_values(self):
        assert OTADomain.ab_slot.value == "ab_slot"
        assert OTADomain.delta_update.value == "delta_update"
        assert OTADomain.rollback.value == "rollback"
        assert OTADomain.signature.value == "signature"
        assert OTADomain.server.value == "server"
        assert OTADomain.integration.value == "integration"

    def test_slot_label_values(self):
        assert SlotLabel.A.value == "A"
        assert SlotLabel.B.value == "B"
        assert SlotLabel.shared.value == "shared"

    def test_rollback_action_values(self):
        assert RollbackAction.none.value == "none"
        assert RollbackAction.rollback.value == "rollback"
        assert RollbackAction.mark_bad_and_rollback.value == "mark_bad_and_rollback"
        assert RollbackAction.revert.value == "revert"

    def test_signature_verify_status(self):
        assert SignatureVerifyStatus.valid.value == "valid"
        assert SignatureVerifyStatus.invalid.value == "invalid"
        assert SignatureVerifyStatus.error.value == "error"

    def test_manifest_validation_status(self):
        assert ManifestValidationStatus.valid.value == "valid"
        assert ManifestValidationStatus.invalid.value == "invalid"

    def test_rollout_phase_status(self):
        assert RolloutPhaseStatus.pending.value == "pending"
        assert RolloutPhaseStatus.passed.value == "passed"
        assert RolloutPhaseStatus.failed.value == "failed"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  REST endpoint smoke tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRESTEndpoints:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from backend.routers.ota_framework import router
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_list_ab_schemes(self, client):
        resp = client.get("/ota/ab-schemes")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3

    def test_get_ab_scheme(self, client):
        resp = client.get("/ota/ab-schemes/linux_ab")
        assert resp.status_code == 200
        assert resp.json()["scheme_id"] == "linux_ab"

    def test_get_ab_scheme_404(self, client):
        resp = client.get("/ota/ab-schemes/nonexistent")
        assert resp.status_code == 404

    def test_list_delta_engines(self, client):
        resp = client.get("/ota/delta-engines")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_list_rollback_policies(self, client):
        resp = client.get("/ota/rollback-policies")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_list_signature_schemes(self, client):
        resp = client.get("/ota/signature-schemes")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_list_rollout_strategies(self, client):
        resp = client.get("/ota/rollout-strategies")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_list_test_recipes(self, client):
        resp = client.get("/ota/test/recipes")
        assert resp.status_code == 200
        assert len(resp.json()) == 12

    def test_switch_slot(self, client):
        resp = client.post("/ota/ab-schemes/switch", json={"scheme_id": "linux_ab", "target_slot": "B"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    def test_delta_generate(self, client):
        resp = client.post("/ota/delta/generate", json={
            "engine_id": "bsdiff",
            "old_image_path": "/old/fw.img",
            "new_image_path": "/new/fw.img",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    def test_rollback_evaluate(self, client):
        resp = client.post("/ota/rollback/evaluate", json={
            "policy_id": "watchdog_bootcount",
            "boot_count": 3,
        })
        assert resp.status_code == 200
        assert resp.json()["action"] == "rollback"

    def test_firmware_sign(self, client):
        resp = client.post("/ota/firmware/sign", json={
            "scheme_id": "ed25519_direct",
            "image_path": "/firmware/v2.0.img",
        })
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_firmware_verify(self, client):
        resp = client.post("/ota/firmware/verify", json={
            "scheme_id": "ed25519_direct",
            "image_path": "/firmware/v2.0.img",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "valid"

    def test_firmware_verify_tampered(self, client):
        resp = client.post("/ota/firmware/verify", json={
            "scheme_id": "ed25519_direct",
            "image_path": "/firmware/v2.0.img",
            "tampered": True,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "invalid"

    def test_create_manifest(self, client):
        resp = client.post("/ota/manifest/create", json={
            "firmware_version": "2.0.0",
            "images": [{"sha256": "abc123", "url": "https://example.com/fw.img", "size_bytes": 1024}],
        })
        assert resp.status_code == 200
        assert resp.json()["firmware_version"] == "2.0.0"

    def test_validate_manifest(self, client):
        resp = client.post("/ota/manifest/validate", json={
            "manifest_data": {
                "manifest_id": "test-123",
                "firmware_version": "2.0.0",
                "images": [{"sha256": "abc123", "url": "https://example.com/fw.img"}],
                "signature": "sig",
                "signature_scheme": "ed25519_direct",
            }
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "valid"

    def test_rollout_evaluate(self, client):
        resp = client.post("/ota/rollout/evaluate", json={
            "strategy_id": "canary",
            "phase_id": "canary",
            "fleet_metrics": {"crash_rate_pct": 0.1, "rollback_rate_pct": 0.1, "success_rate_pct": 99.9},
        })
        assert resp.status_code == 200
        assert resp.json()["gate_passed"] is True

    def test_run_test(self, client):
        resp = client.post("/ota/test/run", json={"recipe_id": "ota-full-cycle"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "passed"

    def test_list_artifacts(self, client):
        resp = client.get("/ota/artifacts")
        assert resp.status_code == 200
        assert len(resp.json()) == 10
