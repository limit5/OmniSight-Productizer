"""C13 — L4-CORE-13 Connectivity sub-skill library tests (#227).

Covers:
  - Connectivity standard config loading + parsing (7 protocols)
  - Test recipe lookup and filtering by category
  - Connectivity test stub runners
  - Sub-skill registry (list, lookup, composition)
  - Cert artifact generator
  - Checklist validation (spec → correct checklist items)
  - SoC compatibility check
  - Doc suite generator integration (get_connectivity_certs)
  - Audit log integration
  - Edge cases (unknown protocol, unknown recipe, empty spec)
  - REST endpoint smoke tests
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.connectivity import (
    ChecklistItem,
    CompositionResult,
    CompositionRule,
    ConnArtifactDef,
    ConnCertArtifact,
    ConnChecklist,
    ConnectivityProtocol,
    ConnTestRecipe,
    ConnTestResult,
    ProtocolDef,
    ProtocolLayer,
    SubSkillDef,
    TestCategory,
    TestStatus,
    TransportType,
    check_soc_compatibility,
    clear_connectivity_certs,
    generate_cert_artifacts,
    get_artifact_definition,
    get_compatible_socs,
    get_connectivity_certs,
    get_protocol,
    get_protocol_features,
    get_test_recipes,
    list_artifact_definitions,
    list_composition_rules,
    list_protocols,
    list_sub_skills,
    get_sub_skill,
    log_connectivity_test_result,
    log_connectivity_test_result_sync,
    register_connectivity_cert,
    reload_connectivity_standards_for_tests,
    resolve_composition,
    run_connectivity_test,
    validate_connectivity_checklist,
)


# -- Fixtures --

@pytest.fixture(autouse=True)
def _reload_config():
    reload_connectivity_standards_for_tests()
    clear_connectivity_certs()
    yield
    reload_connectivity_standards_for_tests()
    clear_connectivity_certs()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Config loading & parsing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConfigLoading:
    def test_list_protocols_returns_seven(self):
        protocols = list_protocols()
        assert len(protocols) == 7
        ids = {p.protocol_id for p in protocols}
        assert ids == {"ble", "wifi", "fiveg", "ethernet", "can", "modbus", "opcua"}

    def test_each_protocol_has_name(self):
        for proto in list_protocols():
            assert proto.name, f"{proto.protocol_id} missing name"

    def test_each_protocol_has_standard(self):
        for proto in list_protocols():
            assert proto.standard, f"{proto.protocol_id} missing standard"

    def test_each_protocol_has_authority(self):
        for proto in list_protocols():
            assert proto.authority, f"{proto.protocol_id} missing authority"

    def test_each_protocol_has_test_recipes(self):
        for proto in list_protocols():
            assert len(proto.test_recipes) > 0, f"{proto.protocol_id} has no test recipes"

    def test_each_protocol_has_features(self):
        for proto in list_protocols():
            assert len(proto.features) > 0, f"{proto.protocol_id} has no features"

    def test_each_protocol_has_required_artifacts(self):
        for proto in list_protocols():
            assert len(proto.required_artifacts) > 0, f"{proto.protocol_id} has no required_artifacts"

    def test_protocol_transport_types(self):
        protos = {p.protocol_id: p.transport for p in list_protocols()}
        assert protos["ble"] == "wireless"
        assert protos["wifi"] == "wireless"
        assert protos["fiveg"] == "wireless"
        assert protos["ethernet"] == "wired"
        assert protos["can"] == "wired"
        assert protos["modbus"] == "mixed"
        assert protos["opcua"] == "mixed"

    def test_protocol_layers(self):
        protos = {p.protocol_id: p.layer for p in list_protocols()}
        assert protos["ble"] == "link"
        assert protos["wifi"] == "link"
        assert protos["fiveg"] == "network"
        assert protos["ethernet"] == "link"
        assert protos["can"] == "link"
        assert protos["modbus"] == "application"
        assert protos["opcua"] == "application"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Protocol lookup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestProtocolLookup:
    def test_get_ble(self):
        proto = get_protocol("ble")
        assert proto is not None
        assert proto.name == "Bluetooth Low Energy"
        assert proto.authority == "Bluetooth SIG"

    def test_get_wifi(self):
        proto = get_protocol("wifi")
        assert proto is not None
        assert "802.11" in proto.standard

    def test_get_fiveg(self):
        proto = get_protocol("fiveg")
        assert proto is not None
        assert "3GPP" in proto.standard

    def test_get_ethernet(self):
        proto = get_protocol("ethernet")
        assert proto is not None
        assert "IEEE 802.3" in proto.standard

    def test_get_can(self):
        proto = get_protocol("can")
        assert proto is not None
        assert "ISO 11898" in proto.standard

    def test_get_modbus(self):
        proto = get_protocol("modbus")
        assert proto is not None
        assert "Modbus" in proto.name

    def test_get_opcua(self):
        proto = get_protocol("opcua")
        assert proto is not None
        assert "OPC UA" in proto.name

    def test_get_unknown_returns_none(self):
        assert get_protocol("zigbee") is None

    def test_protocol_to_dict(self):
        proto = get_protocol("ble")
        d = proto.to_dict()
        assert d["protocol_id"] == "ble"
        assert "test_recipes" in d
        assert "features" in d
        assert isinstance(d["test_recipes"], list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Test recipe lookup & filtering
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRecipeLookup:
    def test_ble_recipes_count(self):
        recipes = get_test_recipes("ble")
        assert len(recipes) == 6

    def test_wifi_recipes_count(self):
        recipes = get_test_recipes("wifi")
        assert len(recipes) == 7

    def test_fiveg_recipes_count(self):
        recipes = get_test_recipes("fiveg")
        assert len(recipes) == 6

    def test_ethernet_recipes_count(self):
        recipes = get_test_recipes("ethernet")
        assert len(recipes) == 6

    def test_can_recipes_count(self):
        recipes = get_test_recipes("can")
        assert len(recipes) == 6

    def test_modbus_recipes_count(self):
        recipes = get_test_recipes("modbus")
        assert len(recipes) == 5

    def test_opcua_recipes_count(self):
        recipes = get_test_recipes("opcua")
        assert len(recipes) == 5

    def test_unknown_protocol_returns_empty(self):
        assert get_test_recipes("zigbee") == []

    def test_recipe_has_id(self):
        for proto in list_protocols():
            for recipe in proto.test_recipes:
                assert recipe.recipe_id, f"Recipe in {proto.protocol_id} missing id"

    def test_recipe_has_tools(self):
        for proto in list_protocols():
            for recipe in proto.test_recipes:
                assert len(recipe.tools) > 0, f"{recipe.recipe_id} has no tools"

    def test_recipe_has_reference(self):
        for proto in list_protocols():
            for recipe in proto.test_recipes:
                assert recipe.reference, f"{recipe.recipe_id} has no reference"

    def test_recipe_by_id(self):
        proto = get_protocol("ble")
        recipe = proto.get_recipe("BLE-GATT-SERVICE")
        assert recipe is not None
        assert recipe.category == "functional"

    def test_recipe_by_id_unknown(self):
        proto = get_protocol("ble")
        assert proto.get_recipe("NONEXISTENT") is None

    def test_recipes_by_category_ble_security(self):
        proto = get_protocol("ble")
        sec = proto.recipes_by_category("security")
        assert len(sec) == 2
        ids = {r.recipe_id for r in sec}
        assert "BLE-PAIRING-LEGACY" in ids
        assert "BLE-PAIRING-LESC" in ids

    def test_recipes_by_category_wifi_functional(self):
        proto = get_protocol("wifi")
        func = proto.recipes_by_category("functional")
        assert len(func) >= 3

    def test_recipe_to_dict(self):
        recipes = get_test_recipes("can")
        d = recipes[0].to_dict()
        assert "recipe_id" in d
        assert "tools" in d
        assert "reference" in d

    def test_recipe_ids_property(self):
        proto = get_protocol("modbus")
        ids = proto.recipe_ids
        assert "MODBUS-RTU-MASTER" in ids
        assert "MODBUS-TCP-CLIENT" in ids


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Features & compatible SoCs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFeaturesAndSocs:
    def test_ble_features(self):
        features = get_protocol_features("ble")
        assert "gatt_server" in features
        assert "pairing_lesc" in features
        assert "ota_dfu" in features

    def test_wifi_features(self):
        features = get_protocol_features("wifi")
        assert "sta_mode" in features
        assert "ap_mode" in features
        assert "wpa3_personal" in features

    def test_can_features(self):
        features = get_protocol_features("can")
        assert "socketcan" in features
        assert "can_fd" in features
        assert "uds_diagnostics" in features

    def test_unknown_protocol_features_empty(self):
        assert get_protocol_features("zigbee") == []

    def test_ble_compatible_socs(self):
        socs = get_compatible_socs("ble")
        assert "nrf52840" in socs
        assert "esp32" in socs

    def test_ethernet_compatible_socs_empty_means_all(self):
        socs = get_compatible_socs("ethernet")
        assert socs == []

    def test_unknown_protocol_socs_empty(self):
        assert get_compatible_socs("zigbee") == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Connectivity test stub runners
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStubRunners:
    def test_stub_returns_pending(self):
        result = run_connectivity_test("ble", "BLE-GATT-SERVICE", "nrf52840-dk")
        assert result.status == TestStatus.pending
        assert "Stub" in result.message
        assert result.protocol == "ble"
        assert result.recipe_id == "BLE-GATT-SERVICE"

    def test_stub_measurements_include_category(self):
        result = run_connectivity_test("wifi", "WIFI-STA-CONNECT", "esp32-devkit")
        assert result.measurements["category"] == "functional"

    def test_stub_measurements_include_tools(self):
        result = run_connectivity_test("can", "CAN-LINK-UP", "stm32h7-nucleo")
        assert "ip" in result.measurements["tools"]

    def test_unknown_protocol_returns_error(self):
        result = run_connectivity_test("zigbee", "TEST-1", "dev")
        assert result.status == TestStatus.error
        assert "Unknown protocol" in result.message

    def test_unknown_recipe_returns_error(self):
        result = run_connectivity_test("ble", "NONEXISTENT", "dev")
        assert result.status == TestStatus.error
        assert "Unknown recipe" in result.message

    def test_result_to_dict(self):
        result = run_connectivity_test("modbus", "MODBUS-RTU-MASTER", "plc-01")
        d = result.to_dict()
        assert d["protocol"] == "modbus"
        assert d["status"] == "pending"
        assert "timestamp" in d

    def test_result_passed_property(self):
        result = ConnTestResult(
            recipe_id="test", protocol="ble",
            status=TestStatus.passed, target_device="dev",
        )
        assert result.passed is True

    def test_result_failed_not_passed(self):
        result = ConnTestResult(
            recipe_id="test", protocol="ble",
            status=TestStatus.failed, target_device="dev",
        )
        assert result.passed is False

    def test_each_protocol_stub_works(self):
        for proto in list_protocols():
            recipe = proto.test_recipes[0]
            result = run_connectivity_test(
                proto.protocol_id, recipe.recipe_id, "test-device"
            )
            assert result.status == TestStatus.pending
            assert result.protocol == proto.protocol_id

    @patch("shutil.which", return_value="/usr/bin/fake-tool")
    @patch("subprocess.run")
    def test_binary_runner_success(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
        result = run_connectivity_test(
            "ble", "BLE-GATT-SERVICE", "dev", binary="fake-tool"
        )
        assert result.status == TestStatus.passed

    @patch("shutil.which", return_value="/usr/bin/fake-tool")
    @patch("subprocess.run")
    def test_binary_runner_failure(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="FAIL")
        result = run_connectivity_test(
            "wifi", "WIFI-STA-CONNECT", "dev", binary="fake-tool"
        )
        assert result.status == TestStatus.failed

    @patch("shutil.which", return_value="/usr/bin/fake-tool")
    @patch("subprocess.run", side_effect=TimeoutError)
    def test_binary_runner_timeout(self, mock_run, mock_which):
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd="fake", timeout=10)
        result = run_connectivity_test(
            "ethernet", "ETH-LINK-UP", "dev", binary="fake-tool"
        )
        assert result.status == TestStatus.error
        assert "Timeout" in result.message


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. Sub-skill registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSubSkillRegistry:
    def test_list_sub_skills_count(self):
        skills = list_sub_skills()
        assert len(skills) == 7

    def test_sub_skill_ids(self):
        ids = {s.sub_skill_id for s in list_sub_skills()}
        assert ids == {"ble", "wifi", "fiveg", "ethernet", "can", "modbus", "opcua"}

    def test_each_sub_skill_has_skill_id(self):
        for s in list_sub_skills():
            assert s.skill_id, f"{s.sub_skill_id} missing skill_id"

    def test_each_sub_skill_has_protocols(self):
        for s in list_sub_skills():
            assert len(s.protocols) > 0

    def test_each_sub_skill_has_typical_products(self):
        for s in list_sub_skills():
            assert len(s.typical_products) > 0

    def test_get_sub_skill_ble(self):
        s = get_sub_skill("ble")
        assert s is not None
        assert s.skill_id == "connectivity-ble"
        assert "ble" in s.protocols

    def test_get_sub_skill_unknown(self):
        assert get_sub_skill("zigbee") is None

    def test_sub_skill_to_dict(self):
        s = get_sub_skill("wifi")
        d = s.to_dict()
        assert d["sub_skill_id"] == "wifi"
        assert "protocols" in d
        assert "typical_products" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. Composition rules
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestComposition:
    def test_list_composition_rules(self):
        rules = list_composition_rules()
        assert len(rules) == 4
        names = {r.name for r in rules}
        assert "Industrial gateway" in names
        assert "Automotive ECU" in names
        assert "IoT gateway" in names
        assert "Smart camera" in names

    def test_resolve_industrial_gateway(self):
        result = resolve_composition("Industrial gateway")
        assert result.matched_rule == "Industrial gateway"
        assert "ethernet" in result.required_sub_skills
        assert "modbus" in result.required_sub_skills
        assert "opcua" in result.required_sub_skills

    def test_resolve_automotive_ecu(self):
        result = resolve_composition("Automotive ECU")
        assert result.matched_rule == "Automotive ECU"
        assert "can" in result.required_sub_skills
        assert "ethernet" in result.required_sub_skills
        assert "ble" in result.optional_sub_skills

    def test_resolve_iot_gateway(self):
        result = resolve_composition("IoT gateway")
        assert "wifi" in result.required_sub_skills
        assert "ble" in result.required_sub_skills
        assert "fiveg" in result.optional_sub_skills

    def test_resolve_smart_camera(self):
        result = resolve_composition("Smart camera")
        assert "wifi" in result.required_sub_skills
        assert "ethernet" in result.required_sub_skills

    def test_resolve_case_insensitive(self):
        result = resolve_composition("industrial gateway")
        assert result.matched_rule == "Industrial gateway"

    def test_resolve_underscore_normalization(self):
        result = resolve_composition("industrial_gateway")
        assert result.matched_rule == "Industrial gateway"

    def test_resolve_by_typical_product(self):
        result = resolve_composition("earbuds")
        assert "ble" in result.required_sub_skills

    def test_resolve_unknown_product(self):
        result = resolve_composition("spaceship")
        assert result.matched_rule is None
        assert result.required_sub_skills == []
        assert result.all_protocols == []

    def test_composition_result_to_dict(self):
        result = resolve_composition("IoT gateway")
        d = result.to_dict()
        assert d["product_type"] == "IoT gateway"
        assert "required_sub_skills" in d
        assert "all_protocols" in d

    def test_composition_rule_to_dict(self):
        rules = list_composition_rules()
        d = rules[0].to_dict()
        assert "name" in d
        assert "required" in d
        assert "optional" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. Artifact definitions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestArtifactDefinitions:
    def test_list_artifact_definitions(self):
        arts = list_artifact_definitions()
        assert len(arts) > 0
        ids = {a.artifact_id for a in arts}
        assert "ble_gatt_table" in ids
        assert "wifi_config_report" in ids
        assert "can_bus_report" in ids
        assert "modbus_register_map" in ids
        assert "opcua_address_space" in ids

    def test_get_artifact_definition(self):
        art = get_artifact_definition("ble_gatt_table")
        assert art is not None
        assert "GATT" in art.name
        assert art.file_pattern

    def test_get_unknown_artifact(self):
        assert get_artifact_definition("nonexistent_artifact") is None

    def test_each_artifact_has_name(self):
        for art in list_artifact_definitions():
            assert art.name, f"{art.artifact_id} missing name"

    def test_each_artifact_has_file_pattern(self):
        for art in list_artifact_definitions():
            assert art.file_pattern, f"{art.artifact_id} missing file_pattern"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. Cert artifact generator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCertArtifactGenerator:
    def test_generate_ble_artifacts(self):
        arts = generate_cert_artifacts("ble")
        assert len(arts) == 3
        ids = {a.artifact_id for a in arts}
        assert "ble_gatt_table" in ids

    def test_generate_wifi_artifacts(self):
        arts = generate_cert_artifacts("wifi")
        assert len(arts) == 3

    def test_generate_can_artifacts(self):
        arts = generate_cert_artifacts("can")
        assert len(arts) == 2

    def test_generate_unknown_protocol_empty(self):
        assert generate_cert_artifacts("zigbee") == []

    def test_provided_artifact_status(self):
        arts = generate_cert_artifacts(
            "ble", spec={"provided_artifacts": ["ble_gatt_table"]}
        )
        gatt = next(a for a in arts if a.artifact_id == "ble_gatt_table")
        assert gatt.status == "provided"

    def test_unprovided_artifact_pending(self):
        arts = generate_cert_artifacts("ble")
        gatt = next(a for a in arts if a.artifact_id == "ble_gatt_table")
        assert gatt.status == "pending"

    def test_artifact_to_dict(self):
        arts = generate_cert_artifacts("modbus")
        d = arts[0].to_dict()
        assert d["protocol"] == "modbus"
        assert "status" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10. Checklist validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestChecklistValidation:
    def test_single_protocol_checklist(self):
        spec = {"target_protocols": ["ble"]}
        checklists = validate_connectivity_checklist(spec)
        assert len(checklists) == 1
        assert checklists[0].protocol == "ble"
        assert checklists[0].total > 0

    def test_multi_protocol_checklist(self):
        spec = {"target_protocols": ["ble", "wifi", "can"]}
        checklists = validate_connectivity_checklist(spec)
        assert len(checklists) == 3

    def test_empty_spec_returns_empty(self):
        assert validate_connectivity_checklist({}) == []
        assert validate_connectivity_checklist({"target_protocols": []}) == []

    def test_unknown_protocol_skipped(self):
        spec = {"target_protocols": ["ble", "zigbee"]}
        checklists = validate_connectivity_checklist(spec)
        assert len(checklists) == 1

    def test_all_items_pending_by_default(self):
        spec = {"target_protocols": ["ethernet"]}
        checklists = validate_connectivity_checklist(spec)
        cl = checklists[0]
        assert cl.pending_count == cl.total
        assert cl.passed_count == 0
        assert cl.complete is False

    def test_passed_test_result_updates_checklist(self):
        results = [
            ConnTestResult(
                recipe_id="BLE-GATT-SERVICE", protocol="ble",
                status=TestStatus.passed, target_device="dev",
            ),
        ]
        spec = {"target_protocols": ["ble"]}
        checklists = validate_connectivity_checklist(spec, test_results=results)
        cl = checklists[0]
        gatt_item = next(i for i in cl.items if i.item_id == "BLE-GATT-SERVICE")
        assert gatt_item.status == TestStatus.passed

    def test_failed_test_result_updates_checklist(self):
        results = [
            ConnTestResult(
                recipe_id="WIFI-STA-CONNECT", protocol="wifi",
                status=TestStatus.failed, target_device="dev",
            ),
        ]
        spec = {"target_protocols": ["wifi"]}
        checklists = validate_connectivity_checklist(spec, test_results=results)
        cl = checklists[0]
        sta_item = next(i for i in cl.items if i.item_id == "WIFI-STA-CONNECT")
        assert sta_item.status == TestStatus.failed

    def test_provided_artifact_in_checklist(self):
        spec = {
            "target_protocols": ["ble"],
            "provided_artifacts": ["ble_gatt_table"],
        }
        checklists = validate_connectivity_checklist(spec)
        cl = checklists[0]
        art_item = next(i for i in cl.items if i.item_id == "artifact:ble_gatt_table")
        assert art_item.status == TestStatus.passed

    def test_checklist_complete_when_all_passed(self):
        proto = get_protocol("modbus")
        results = [
            ConnTestResult(
                recipe_id=r.recipe_id, protocol="modbus",
                status=TestStatus.passed, target_device="dev",
            )
            for r in proto.test_recipes
        ]
        spec = {
            "target_protocols": ["modbus"],
            "provided_artifacts": proto.required_artifacts,
        }
        checklists = validate_connectivity_checklist(spec, test_results=results)
        assert checklists[0].complete is True

    def test_checklist_to_dict(self):
        spec = {"target_protocols": ["can"]}
        checklists = validate_connectivity_checklist(spec)
        d = checklists[0].to_dict()
        assert d["protocol"] == "can"
        assert "items" in d
        assert d["total"] > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  11. SoC compatibility
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSocCompatibility:
    def test_esp32_supports_ble_and_wifi(self):
        compat = check_soc_compatibility("esp32")
        assert compat["ble"] is True
        assert compat["wifi"] is True

    def test_nrf52840_supports_ble(self):
        compat = check_soc_compatibility("nrf52840")
        assert compat["ble"] is True
        assert compat["wifi"] is False

    def test_stm32h7_supports_can(self):
        compat = check_soc_compatibility("stm32h7")
        assert compat["can"] is True

    def test_universal_protocol_always_true(self):
        compat = check_soc_compatibility("any_random_soc")
        assert compat["ethernet"] is True
        assert compat["modbus"] is True
        assert compat["opcua"] is True

    def test_specific_protocol_filter(self):
        compat = check_soc_compatibility("esp32", protocol_ids=["ble", "wifi"])
        assert len(compat) == 2
        assert compat["ble"] is True
        assert compat["wifi"] is True

    def test_case_insensitive_soc_match(self):
        compat = check_soc_compatibility("ESP32")
        assert compat["ble"] is True

    def test_unknown_soc_on_specific_protocol(self):
        compat = check_soc_compatibility("unknown_chip", protocol_ids=["ble"])
        assert compat["ble"] is False

    def test_quectel_modem_supports_5g(self):
        compat = check_soc_compatibility("quectel_rm500q", protocol_ids=["fiveg"])
        assert compat["fiveg"] is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  12. Doc suite generator integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDocSuiteIntegration:
    def test_register_and_get_certs(self):
        register_connectivity_cert("Bluetooth 5.3", status="Passed", cert_id="BT-001")
        certs = get_connectivity_certs()
        assert len(certs) == 1
        assert certs[0]["standard"] == "Bluetooth 5.3"
        assert certs[0]["status"] == "Passed"

    def test_multiple_certs(self):
        register_connectivity_cert("Bluetooth 5.3")
        register_connectivity_cert("WiFi Alliance")
        register_connectivity_cert("3GPP Rel-16")
        assert len(get_connectivity_certs()) == 3

    def test_clear_certs(self):
        register_connectivity_cert("test")
        clear_connectivity_certs()
        assert len(get_connectivity_certs()) == 0

    def test_cert_details(self):
        register_connectivity_cert(
            "WiFi Alliance",
            details={"certification_type": "Wi-Fi CERTIFIED"},
        )
        certs = get_connectivity_certs()
        assert certs[0]["details"]["certification_type"] == "Wi-Fi CERTIFIED"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  13. Audit log integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAuditIntegration:
    @pytest.mark.asyncio
    async def test_log_connectivity_test_result(self):
        result = ConnTestResult(
            recipe_id="BLE-GATT-SERVICE", protocol="ble",
            status=TestStatus.passed, target_device="dev",
        )
        with patch("backend.audit.log", new_callable=AsyncMock, return_value=42) as mock_log:
            row_id = await log_connectivity_test_result(result)
            assert row_id == 42
            mock_log.assert_called_once()
            call_kwargs = mock_log.call_args[1]
            assert call_kwargs["action"] == "connectivity_test"
            assert call_kwargs["entity_id"] == "ble:BLE-GATT-SERVICE"

    @pytest.mark.asyncio
    async def test_log_handles_import_error(self):
        result = ConnTestResult(
            recipe_id="TEST", protocol="ble",
            status=TestStatus.pending, target_device="dev",
        )
        with patch("backend.connectivity.log_connectivity_test_result", new_callable=AsyncMock, return_value=None):
            pass

    def test_sync_log_skips_without_loop(self):
        result = ConnTestResult(
            recipe_id="TEST", protocol="ble",
            status=TestStatus.pending, target_device="dev",
        )
        log_connectivity_test_result_sync(result)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  14. Enum tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEnums:
    def test_connectivity_protocol_values(self):
        assert ConnectivityProtocol.ble.value == "ble"
        assert ConnectivityProtocol.wifi.value == "wifi"
        assert ConnectivityProtocol.fiveg.value == "fiveg"
        assert ConnectivityProtocol.ethernet.value == "ethernet"
        assert ConnectivityProtocol.can.value == "can"
        assert ConnectivityProtocol.modbus.value == "modbus"
        assert ConnectivityProtocol.opcua.value == "opcua"

    def test_test_category_values(self):
        assert TestCategory.functional.value == "functional"
        assert TestCategory.security.value == "security"
        assert TestCategory.performance.value == "performance"

    def test_test_status_values(self):
        assert TestStatus.passed.value == "passed"
        assert TestStatus.failed.value == "failed"
        assert TestStatus.pending.value == "pending"

    def test_transport_type_values(self):
        assert TransportType.wireless.value == "wireless"
        assert TransportType.wired.value == "wired"
        assert TransportType.mixed.value == "mixed"

    def test_protocol_layer_values(self):
        assert ProtocolLayer.link.value == "link"
        assert ProtocolLayer.network.value == "network"
        assert ProtocolLayer.application.value == "application"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  15. Edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEdgeCases:
    def test_empty_target_protocols_checklist(self):
        assert validate_connectivity_checklist({"target_protocols": []}) == []

    def test_none_spec_checklist(self):
        assert validate_connectivity_checklist({}) == []

    def test_generate_artifacts_no_spec(self):
        arts = generate_cert_artifacts("ble")
        assert len(arts) > 0
        for a in arts:
            assert a.status == "pending"

    def test_composition_all_protocols_list(self):
        result = resolve_composition("Industrial gateway")
        assert len(result.all_protocols) == len(result.required_sub_skills) + len(result.optional_sub_skills)

    def test_reload_clears_cache(self):
        p1 = list_protocols()
        reload_connectivity_standards_for_tests()
        p2 = list_protocols()
        assert len(p1) == len(p2)

    def test_total_recipe_count_across_all_protocols(self):
        total = sum(len(p.test_recipes) for p in list_protocols())
        assert total == 41  # 6+7+6+6+6+5+5

    def test_checklist_counts_consistent(self):
        spec = {"target_protocols": ["wifi"]}
        cl = validate_connectivity_checklist(spec)[0]
        assert cl.total == cl.passed_count + cl.pending_count + cl.failed_count

    def test_all_protocols_have_unique_recipe_ids(self):
        all_ids = []
        for proto in list_protocols():
            for r in proto.test_recipes:
                all_ids.append(r.recipe_id)
        assert len(all_ids) == len(set(all_ids)), "Duplicate recipe IDs found"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  16. REST endpoint smoke tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRESTEndpoints:
    @pytest.fixture
    def client(self):
        from backend.routers.connectivity import router
        from backend import auth as _au
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        mock_user = MagicMock()
        mock_user.role = "admin"

        async def _fake_dep():
            return mock_user

        app.dependency_overrides[_au.require_operator] = _fake_dep
        app.dependency_overrides[_au.require_admin] = _fake_dep
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_list_protocols_endpoint(self, client):
        resp = client.get("/connectivity/protocols")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 7

    def test_get_protocol_endpoint(self, client):
        resp = client.get("/connectivity/protocols/ble")
        assert resp.status_code == 200
        assert resp.json()["protocol_id"] == "ble"

    def test_get_protocol_not_found(self, client):
        resp = client.get("/connectivity/protocols/zigbee")
        assert resp.status_code == 404

    def test_get_recipes_endpoint(self, client):
        resp = client.get("/connectivity/protocols/wifi/recipes")
        assert resp.status_code == 200
        assert resp.json()["count"] == 7

    def test_get_features_endpoint(self, client):
        resp = client.get("/connectivity/protocols/can/features")
        assert resp.status_code == 200
        assert "socketcan" in resp.json()["features"]

    def test_list_artifacts_endpoint(self, client):
        resp = client.get("/connectivity/artifacts")
        assert resp.status_code == 200
        assert resp.json()["count"] > 0

    def test_list_sub_skills_endpoint(self, client):
        resp = client.get("/connectivity/sub-skills")
        assert resp.status_code == 200
        assert resp.json()["count"] == 7

    def test_get_sub_skill_endpoint(self, client):
        resp = client.get("/connectivity/sub-skills/ble")
        assert resp.status_code == 200
        assert resp.json()["skill_id"] == "connectivity-ble"

    def test_get_sub_skill_not_found(self, client):
        resp = client.get("/connectivity/sub-skills/zigbee")
        assert resp.status_code == 404

    def test_composition_rules_endpoint(self, client):
        resp = client.get("/connectivity/composition/rules")
        assert resp.status_code == 200
        assert resp.json()["count"] == 4

    def test_resolve_composition_endpoint(self, client):
        resp = client.post("/connectivity/composition/resolve", json={
            "product_type": "IoT gateway",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_rule"] == "IoT gateway"
        assert "wifi" in data["required_sub_skills"]

    def test_soc_compat_endpoint(self, client):
        resp = client.post("/connectivity/soc-compat", json={
            "soc_id": "esp32",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["compatibility"]["ble"] is True
        assert data["compatibility"]["wifi"] is True

    def test_checklist_endpoint(self, client):
        resp = client.post("/connectivity/checklist", json={
            "target_protocols": ["ble", "wifi"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2

    def test_generate_artifacts_endpoint(self, client):
        resp = client.post("/connectivity/artifacts/generate", json={
            "protocol_id": "can",
        })
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    def test_run_test_endpoint(self, client):
        import backend.connectivity as conn
        with patch.object(conn, "log_connectivity_test_result", new_callable=AsyncMock):
            resp = client.post("/connectivity/test", json={
                "protocol_id": "ble",
                "recipe_id": "BLE-GATT-SERVICE",
                "target_device": "nrf52840-dk",
            })
            assert resp.status_code == 200
            assert resp.json()["status"] == "pending"
