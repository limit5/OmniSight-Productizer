"""C10 — L4-CORE-10 Radio certification pre-compliance tests (#224).

Covers:
  - Radio standard config loading + parsing (4 regions)
  - Test recipe lookup and filtering by category
  - Conducted + radiated emissions stub runners
  - SAR test hook (upload, parse, limit check)
  - Per-region cert artifact generator
  - Checklist validation (spec → correct checklist items)
  - Doc suite generator integration (get_radio_certs)
  - Audit log integration
  - Edge cases (unknown region, unknown recipe, missing file)
  - REST endpoint smoke tests
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.radio_compliance import (
    CertArtifact,
    ChecklistItem,
    EmissionsCategory,
    EmissionsTestResult,
    RadioArtifactDef,
    RadioChecklist,
    RadioRegion,
    RadioRegionDef,
    SARResult,
    TestRecipe,
    TestStatus,
    clear_radio_certs,
    generate_cert_artifacts,
    get_artifact_definition,
    get_radio_certs,
    get_region,
    get_test_recipes,
    list_artifact_definitions,
    list_regions,
    log_radio_test_result,
    log_radio_test_result_sync,
    register_radio_cert,
    reload_radio_standards_for_tests,
    run_emissions_test,
    upload_sar_result,
    validate_radio_checklist,
)


# -- Fixtures --

@pytest.fixture(autouse=True)
def _reload_config():
    reload_radio_standards_for_tests()
    clear_radio_certs()
    yield
    reload_radio_standards_for_tests()
    clear_radio_certs()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Config loading & parsing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConfigLoading:
    def test_list_regions_returns_four(self):
        regions = list_regions()
        assert len(regions) == 4
        ids = {r.region_id for r in regions}
        assert ids == {"fcc", "ce_red", "ncc_lpd", "srrc_srd"}

    def test_get_region_fcc(self):
        reg = get_region("fcc")
        assert reg is not None
        assert reg.name == "FCC Part 15"
        assert reg.authority == "Federal Communications Commission"
        assert reg.region == "United States"
        assert len(reg.test_recipes) >= 6

    def test_get_region_ce_red(self):
        reg = get_region("ce_red")
        assert reg is not None
        assert reg.name == "CE RED"
        assert reg.authority == "European Commission"
        assert len(reg.test_recipes) >= 5

    def test_get_region_ncc_lpd(self):
        reg = get_region("ncc_lpd")
        assert reg is not None
        assert reg.name == "NCC LPD"
        assert reg.authority == "National Communications Commission"

    def test_get_region_srrc_srd(self):
        reg = get_region("srrc_srd")
        assert reg is not None
        assert reg.name == "SRRC SRD"
        assert reg.authority == "State Radio Regulation of China"

    def test_get_region_unknown(self):
        assert get_region("nonexistent") is None

    def test_fcc_has_conducted_recipe(self):
        reg = get_region("fcc")
        conducted = reg.recipes_by_category("conducted")
        assert len(conducted) >= 1
        assert any("CONDUCTED" in r.recipe_id for r in conducted)

    def test_fcc_has_radiated_recipe(self):
        reg = get_region("fcc")
        radiated = reg.recipes_by_category("radiated")
        assert len(radiated) >= 2

    def test_fcc_has_sar_recipe(self):
        reg = get_region("fcc")
        sar = reg.recipes_by_category("sar")
        assert len(sar) == 1
        assert sar[0].limits.get("limit_w_kg") == 1.6

    def test_ce_red_sar_limit_is_2(self):
        reg = get_region("ce_red")
        sar = reg.recipes_by_category("sar")
        assert len(sar) == 1
        assert sar[0].limits.get("limit_w_kg") == 2.0

    def test_recipe_has_reference(self):
        reg = get_region("fcc")
        recipe = reg.get_recipe("FCC-15B-CONDUCTED")
        assert recipe is not None
        assert "47 CFR" in recipe.reference

    def test_recipe_has_equipment(self):
        reg = get_region("fcc")
        recipe = reg.get_recipe("FCC-15B-CONDUCTED")
        assert "EMI receiver" in recipe.equipment
        assert "LISN" in recipe.equipment

    def test_recipe_has_frequency_range(self):
        reg = get_region("fcc")
        recipe = reg.get_recipe("FCC-15B-CONDUCTED")
        assert recipe.frequency_range_mhz == [0.15, 30]

    def test_required_artifacts_fcc(self):
        reg = get_region("fcc")
        assert "test_report" in reg.required_artifacts
        assert "equipment_authorization" in reg.required_artifacts
        assert "device_photos" in reg.required_artifacts

    def test_required_artifacts_ce_red(self):
        reg = get_region("ce_red")
        assert "declaration_of_conformity" in reg.required_artifacts
        assert "technical_documentation" in reg.required_artifacts

    def test_artifact_definitions_loaded(self):
        arts = list_artifact_definitions()
        assert len(arts) >= 10
        ids = {a.artifact_id for a in arts}
        assert "test_report" in ids
        assert "sar_report" in ids

    def test_get_artifact_definition(self):
        art = get_artifact_definition("test_report")
        assert art is not None
        assert art.name == "Test Report"
        assert art.file_pattern != ""

    def test_get_artifact_definition_unknown(self):
        assert get_artifact_definition("nonexistent") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Test recipe lookup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRecipeLookup:
    def test_get_test_recipes_fcc(self):
        recipes = get_test_recipes("fcc")
        assert len(recipes) >= 6
        ids = {r.recipe_id for r in recipes}
        assert "FCC-15B-CONDUCTED" in ids
        assert "FCC-15B-RADIATED" in ids
        assert "FCC-SAR" in ids

    def test_get_test_recipes_unknown_region(self):
        recipes = get_test_recipes("nonexistent")
        assert recipes == []

    def test_recipe_to_dict(self):
        recipes = get_test_recipes("fcc")
        d = recipes[0].to_dict()
        assert "recipe_id" in d
        assert "name" in d
        assert "category" in d
        assert "description" in d

    def test_get_recipe_by_id(self):
        reg = get_region("fcc")
        recipe = reg.get_recipe("FCC-15B-RADIATED")
        assert recipe is not None
        assert recipe.category == "radiated"
        assert recipe.name == "Radiated emissions"

    def test_get_recipe_unknown_id(self):
        reg = get_region("fcc")
        assert reg.get_recipe("NONEXISTENT") is None

    def test_recipe_ids_property(self):
        reg = get_region("fcc")
        ids = reg.recipe_ids
        assert isinstance(ids, list)
        assert "FCC-15B-CONDUCTED" in ids


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Emissions stub runners
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEmissionsRunner:
    def test_conducted_stub_returns_pending(self):
        result = run_emissions_test("fcc", "FCC-15B-CONDUCTED", "DUT-001")
        assert result.status == TestStatus.pending
        assert result.recipe_id == "FCC-15B-CONDUCTED"
        assert result.region == "fcc"
        assert result.device_under_test == "DUT-001"
        assert "Stub" in result.message
        assert "Equipment needed" in result.message

    def test_radiated_stub_returns_pending(self):
        result = run_emissions_test("fcc", "FCC-15B-RADIATED", "DUT-001")
        assert result.status == TestStatus.pending
        assert result.measurements["category"] == "radiated"

    def test_ce_red_conducted_stub(self):
        result = run_emissions_test("ce_red", "RED-CONDUCTED", "DUT-002")
        assert result.status == TestStatus.pending
        assert result.region == "ce_red"

    def test_ncc_lpd_stub(self):
        result = run_emissions_test("ncc_lpd", "NCC-LPD-CONDUCTED", "DUT-003")
        assert result.status == TestStatus.pending

    def test_srrc_stub(self):
        result = run_emissions_test("srrc_srd", "SRRC-CONDUCTED", "DUT-004")
        assert result.status == TestStatus.pending

    def test_unknown_region_error(self):
        result = run_emissions_test("nonexistent", "X", "DUT")
        assert result.status == TestStatus.error
        assert "Unknown region" in result.message

    def test_unknown_recipe_error(self):
        result = run_emissions_test("fcc", "NONEXISTENT", "DUT")
        assert result.status == TestStatus.error
        assert "Unknown recipe" in result.message

    def test_sar_recipe_rejected(self):
        result = run_emissions_test("fcc", "FCC-SAR", "DUT")
        assert result.status == TestStatus.error
        assert "SAR" in result.message

    def test_result_to_dict(self):
        result = run_emissions_test("fcc", "FCC-15B-CONDUCTED", "DUT")
        d = result.to_dict()
        assert d["status"] == "pending"
        assert d["recipe_id"] == "FCC-15B-CONDUCTED"
        assert d["region"] == "fcc"
        assert "timestamp" in d

    def test_stub_includes_measurements(self):
        result = run_emissions_test("fcc", "FCC-15B-CONDUCTED", "DUT")
        assert "category" in result.measurements
        assert "frequency_range_mhz" in result.measurements

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/emi_test")
    def test_binary_execution_pass(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="OK", stderr="")
        result = run_emissions_test(
            "fcc", "FCC-15B-CONDUCTED", "DUT",
            binary="/usr/bin/emi_test",
        )
        assert result.status == TestStatus.passed

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/bin/emi_test")
    def test_binary_execution_fail(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="LIMIT EXCEEDED")
        result = run_emissions_test(
            "fcc", "FCC-15B-CONDUCTED", "DUT",
            binary="/usr/bin/emi_test",
        )
        assert result.status == TestStatus.failed

    @patch("subprocess.run", side_effect=FileNotFoundError)
    @patch("shutil.which", return_value="/usr/bin/emi_test")
    def test_binary_not_found(self, mock_which, mock_run):
        result = run_emissions_test(
            "fcc", "FCC-15B-CONDUCTED", "DUT",
            binary="/usr/bin/emi_test",
        )
        assert result.status == TestStatus.error
        assert "not found" in result.message

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=10))
    @patch("shutil.which", return_value="/usr/bin/emi_test")
    def test_binary_timeout(self, mock_which, mock_run):
        result = run_emissions_test(
            "fcc", "FCC-15B-CONDUCTED", "DUT",
            binary="/usr/bin/emi_test", timeout_s=10,
        )
        assert result.status == TestStatus.error
        assert "Timeout" in result.message


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. SAR test hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSARHook:
    def test_sar_pass_fcc(self, tmp_path):
        sar_file = tmp_path / "sar_report.json"
        sar_file.write_text(json.dumps({"peak_sar": 1.2}))
        result = upload_sar_result("fcc", str(sar_file))
        assert result.status == TestStatus.passed
        assert result.peak_sar_w_kg == 1.2
        assert result.limit_w_kg == 1.6
        assert result.within_limit is True
        assert "PASS" in result.message

    def test_sar_fail_fcc(self, tmp_path):
        sar_file = tmp_path / "sar_report.json"
        sar_file.write_text(json.dumps({"peak_sar": 2.0}))
        result = upload_sar_result("fcc", str(sar_file))
        assert result.status == TestStatus.failed
        assert result.peak_sar_w_kg == 2.0
        assert result.within_limit is False
        assert "FAIL" in result.message

    def test_sar_pass_ce_red(self, tmp_path):
        sar_file = tmp_path / "sar.json"
        sar_file.write_text(json.dumps({"peak_sar": 1.8}))
        result = upload_sar_result("ce_red", str(sar_file))
        assert result.status == TestStatus.passed
        assert result.limit_w_kg == 2.0

    def test_sar_with_explicit_value(self, tmp_path):
        sar_file = tmp_path / "sar.txt"
        sar_file.write_text("some content")
        result = upload_sar_result("fcc", str(sar_file), peak_sar_w_kg=0.5)
        assert result.status == TestStatus.passed
        assert result.peak_sar_w_kg == 0.5

    def test_sar_file_not_found(self):
        result = upload_sar_result("fcc", "/nonexistent/path/sar.json")
        assert result.status == TestStatus.error
        assert "not found" in result.message

    def test_sar_unknown_region(self, tmp_path):
        f = tmp_path / "sar.json"
        f.write_text("{}")
        result = upload_sar_result("nonexistent", str(f))
        assert result.status == TestStatus.error
        assert "Unknown region" in result.message

    def test_sar_parse_text_format(self, tmp_path):
        sar_file = tmp_path / "sar_report.txt"
        sar_file.write_text("Test Result\nPeak SAR: 1.35 W/kg\nDate: 2024-01-01")
        result = upload_sar_result("fcc", str(sar_file))
        assert result.status == TestStatus.passed
        assert result.peak_sar_w_kg == 1.35

    def test_sar_parse_json_sar_value_key(self, tmp_path):
        sar_file = tmp_path / "sar.json"
        sar_file.write_text(json.dumps({"sar_value": 1.1}))
        result = upload_sar_result("fcc", str(sar_file))
        assert result.peak_sar_w_kg == 1.1

    def test_sar_unparseable_pending(self, tmp_path):
        sar_file = tmp_path / "sar.dat"
        sar_file.write_bytes(b"\x00\x01\x02\x03")
        result = upload_sar_result("fcc", str(sar_file))
        assert result.status == TestStatus.pending
        assert "could not be extracted" in result.message

    def test_sar_to_dict(self, tmp_path):
        sar_file = tmp_path / "sar.json"
        sar_file.write_text(json.dumps({"peak_sar": 1.0}))
        result = upload_sar_result("fcc", str(sar_file))
        d = result.to_dict()
        assert d["status"] == "passed"
        assert d["within_limit"] is True
        assert d["peak_sar_w_kg"] == 1.0
        assert d["limit_w_kg"] == 1.6

    def test_sar_with_metadata(self, tmp_path):
        sar_file = tmp_path / "sar.json"
        sar_file.write_text(json.dumps({"peak_sar": 1.0}))
        result = upload_sar_result(
            "fcc", str(sar_file),
            metadata={"lab": "TUV", "date": "2024-01-01"},
        )
        assert result.metadata["lab"] == "TUV"

    def test_sar_averaging_mass_fcc(self, tmp_path):
        sar_file = tmp_path / "sar.json"
        sar_file.write_text(json.dumps({"peak_sar": 1.0}))
        result = upload_sar_result("fcc", str(sar_file))
        assert result.averaging_mass_g == 1.0

    def test_sar_averaging_mass_ce_red(self, tmp_path):
        sar_file = tmp_path / "sar.json"
        sar_file.write_text(json.dumps({"peak_sar": 1.0}))
        result = upload_sar_result("ce_red", str(sar_file))
        assert result.averaging_mass_g == 10.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Cert artifact generator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCertArtifactGenerator:
    def test_fcc_artifacts(self):
        artifacts = generate_cert_artifacts("fcc")
        assert len(artifacts) >= 7
        ids = {a.artifact_id for a in artifacts}
        assert "test_report" in ids
        assert "equipment_authorization" in ids
        assert "device_photos" in ids

    def test_ce_red_artifacts(self):
        artifacts = generate_cert_artifacts("ce_red")
        ids = {a.artifact_id for a in artifacts}
        assert "declaration_of_conformity" in ids
        assert "technical_documentation" in ids

    def test_unknown_region_empty(self):
        artifacts = generate_cert_artifacts("nonexistent")
        assert artifacts == []

    def test_provided_artifact_status(self):
        spec = {"provided_artifacts": ["test_report", "device_photos"]}
        artifacts = generate_cert_artifacts("fcc", spec=spec)
        status_map = {a.artifact_id: a.status for a in artifacts}
        assert status_map["test_report"] == "provided"
        assert status_map["device_photos"] == "provided"
        assert status_map["equipment_authorization"] == "pending"

    def test_with_test_results(self):
        results = [
            EmissionsTestResult(
                recipe_id="FCC-15B-CONDUCTED", region="fcc",
                status=TestStatus.passed, device_under_test="DUT",
            ),
            EmissionsTestResult(
                recipe_id="FCC-15B-RADIATED", region="fcc",
                status=TestStatus.passed, device_under_test="DUT",
            ),
        ]
        artifacts = generate_cert_artifacts("fcc", test_results=results)
        report_art = next(a for a in artifacts if a.artifact_id == "test_report")
        assert report_art.status == "complete"

    def test_with_sar_result(self):
        sar = SARResult(
            region="fcc", status=TestStatus.passed,
            file_path="/tmp/sar.pdf",
            peak_sar_w_kg=1.0, limit_w_kg=1.6,
        )
        artifacts = generate_cert_artifacts("fcc", sar_result=sar)
        sar_art = [a for a in artifacts if a.artifact_id == "sar_report"]
        assert len(sar_art) == 1
        assert sar_art[0].status == "provided"

    def test_artifact_to_dict(self):
        artifacts = generate_cert_artifacts("fcc")
        d = artifacts[0].to_dict()
        assert "artifact_id" in d
        assert "name" in d
        assert "region" in d
        assert "status" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. Checklist validation (sample radio spec → correct cert checklist)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestChecklistValidation:
    def test_fcc_checklist(self):
        spec = {"target_regions": ["fcc"]}
        checklists = validate_radio_checklist(spec)
        assert len(checklists) == 1
        cl = checklists[0]
        assert cl.region == "fcc"
        assert cl.region_name == "FCC Part 15"
        assert cl.total >= 13  # recipes + artifacts
        assert cl.pending_count > 0
        assert cl.complete is False

    def test_multi_region_checklist(self):
        spec = {"target_regions": ["fcc", "ce_red", "ncc_lpd", "srrc_srd"]}
        checklists = validate_radio_checklist(spec)
        assert len(checklists) == 4
        regions = {c.region for c in checklists}
        assert regions == {"fcc", "ce_red", "ncc_lpd", "srrc_srd"}

    def test_empty_regions(self):
        spec = {"target_regions": []}
        checklists = validate_radio_checklist(spec)
        assert checklists == []

    def test_unknown_region_skipped(self):
        spec = {"target_regions": ["nonexistent"]}
        checklists = validate_radio_checklist(spec)
        assert checklists == []

    def test_checklist_with_passed_test(self):
        results = [
            EmissionsTestResult(
                recipe_id="FCC-15B-CONDUCTED", region="fcc",
                status=TestStatus.passed, device_under_test="DUT",
            ),
        ]
        spec = {"target_regions": ["fcc"]}
        checklists = validate_radio_checklist(spec, test_results=results)
        cl = checklists[0]
        item = next(i for i in cl.items if i.item_id == "FCC-15B-CONDUCTED")
        assert item.status == TestStatus.passed

    def test_checklist_with_failed_test(self):
        results = [
            EmissionsTestResult(
                recipe_id="FCC-15B-RADIATED", region="fcc",
                status=TestStatus.failed, device_under_test="DUT",
            ),
        ]
        spec = {"target_regions": ["fcc"]}
        checklists = validate_radio_checklist(spec, test_results=results)
        cl = checklists[0]
        item = next(i for i in cl.items if i.item_id == "FCC-15B-RADIATED")
        assert item.status == TestStatus.failed

    def test_checklist_with_sar_result(self):
        sar_results = {
            "fcc": SARResult(
                region="fcc", status=TestStatus.passed,
                peak_sar_w_kg=1.0, limit_w_kg=1.6,
            ),
        }
        spec = {"target_regions": ["fcc"]}
        checklists = validate_radio_checklist(spec, sar_results=sar_results)
        cl = checklists[0]
        sar_item = next(i for i in cl.items if i.item_id == "FCC-SAR")
        assert sar_item.status == TestStatus.passed

    def test_checklist_with_provided_artifacts(self):
        spec = {
            "target_regions": ["fcc"],
            "provided_artifacts": ["test_report", "device_photos"],
        }
        checklists = validate_radio_checklist(spec)
        cl = checklists[0]
        art_items = [i for i in cl.items if i.category == "artifact"]
        provided = [i for i in art_items if i.status == TestStatus.passed]
        assert len(provided) == 2

    def test_checklist_to_dict(self):
        spec = {"target_regions": ["fcc"]}
        checklists = validate_radio_checklist(spec)
        d = checklists[0].to_dict()
        assert "region" in d
        assert "region_name" in d
        assert "total" in d
        assert "passed" in d
        assert "pending" in d
        assert "complete" in d
        assert "items" in d
        assert len(d["items"]) > 0

    def test_checklist_includes_artifacts_and_tests(self):
        spec = {"target_regions": ["fcc"]}
        checklists = validate_radio_checklist(spec)
        cl = checklists[0]
        categories = {i.category for i in cl.items}
        assert "conducted" in categories
        assert "radiated" in categories
        assert "sar" in categories
        assert "artifact" in categories

    def test_complete_checklist(self):
        reg = get_region("fcc")
        passed_results = [
            EmissionsTestResult(
                recipe_id=r.recipe_id, region="fcc",
                status=TestStatus.passed, device_under_test="DUT",
            )
            for r in reg.test_recipes if r.category != "sar"
        ]
        sar_results = {
            "fcc": SARResult(
                region="fcc", status=TestStatus.passed,
                peak_sar_w_kg=1.0, limit_w_kg=1.6,
            ),
        }
        spec = {
            "target_regions": ["fcc"],
            "provided_artifacts": reg.required_artifacts,
        }
        checklists = validate_radio_checklist(
            spec, test_results=passed_results, sar_results=sar_results,
        )
        cl = checklists[0]
        assert cl.complete is True
        assert cl.passed_count == cl.total


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. Doc suite generator integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDocSuiteIntegration:
    def test_register_and_get_certs(self):
        register_radio_cert("FCC Part 15", status="Submitted", cert_id="FCC-2024-001")
        register_radio_cert("CE RED", status="Pending")
        certs = get_radio_certs()
        assert len(certs) == 2
        assert certs[0]["standard"] == "FCC Part 15"
        assert certs[0]["status"] == "Submitted"
        assert certs[0]["cert_id"] == "FCC-2024-001"
        assert certs[1]["standard"] == "CE RED"

    def test_clear_certs(self):
        register_radio_cert("FCC Part 15")
        clear_radio_certs()
        assert get_radio_certs() == []

    def test_empty_certs(self):
        assert get_radio_certs() == []

    def test_cert_with_details(self):
        register_radio_cert(
            "SRRC SRD", status="Approved",
            details={"approval_date": "2024-06-01", "validity_years": 5},
        )
        certs = get_radio_certs()
        assert certs[0]["details"]["approval_date"] == "2024-06-01"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. Audit log integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAuditIntegration:
    @pytest.mark.asyncio
    async def test_log_emissions_result(self):
        result = EmissionsTestResult(
            recipe_id="FCC-15B-CONDUCTED", region="fcc",
            status=TestStatus.passed, device_under_test="DUT",
        )
        with patch("backend.audit.log", new_callable=AsyncMock, return_value=42):
            log_id = await log_radio_test_result(result)
            assert log_id == 42

    @pytest.mark.asyncio
    async def test_log_sar_result(self):
        result = SARResult(
            region="fcc", status=TestStatus.passed,
            peak_sar_w_kg=1.0, limit_w_kg=1.6,
        )
        with patch("backend.audit.log", new_callable=AsyncMock, return_value=99):
            log_id = await log_radio_test_result(result)
            assert log_id == 99

    @pytest.mark.asyncio
    async def test_log_failure_no_raise(self):
        result = EmissionsTestResult(
            recipe_id="X", region="fcc",
            status=TestStatus.error, device_under_test="DUT",
        )
        with patch("backend.audit.log", new_callable=AsyncMock, side_effect=Exception("db error")):
            log_id = await log_radio_test_result(result)
            assert log_id is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. Data model edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDataModels:
    def test_radio_region_enum(self):
        assert RadioRegion.fcc.value == "fcc"
        assert RadioRegion.ce_red.value == "ce_red"
        assert RadioRegion.ncc_lpd.value == "ncc_lpd"
        assert RadioRegion.srrc_srd.value == "srrc_srd"

    def test_emissions_category_enum(self):
        assert EmissionsCategory.conducted.value == "conducted"
        assert EmissionsCategory.radiated.value == "radiated"
        assert EmissionsCategory.sar.value == "sar"

    def test_test_status_enum(self):
        assert TestStatus.passed.value == "passed"
        assert TestStatus.failed.value == "failed"
        assert TestStatus.pending.value == "pending"
        assert TestStatus.error.value == "error"

    def test_emissions_result_passed_property(self):
        r = EmissionsTestResult(
            recipe_id="X", region="fcc", status=TestStatus.passed,
        )
        assert r.passed is True
        r2 = EmissionsTestResult(
            recipe_id="X", region="fcc", status=TestStatus.failed,
        )
        assert r2.passed is False

    def test_sar_result_within_limit(self):
        r = SARResult(
            region="fcc", status=TestStatus.passed,
            peak_sar_w_kg=1.0, limit_w_kg=1.6,
        )
        assert r.within_limit is True
        r2 = SARResult(
            region="fcc", status=TestStatus.failed,
            peak_sar_w_kg=2.0, limit_w_kg=1.6,
        )
        assert r2.within_limit is False

    def test_sar_result_zero_limit(self):
        r = SARResult(
            region="fcc", status=TestStatus.error,
            peak_sar_w_kg=1.0, limit_w_kg=0.0,
        )
        assert r.within_limit is False

    def test_checklist_item_defaults(self):
        item = ChecklistItem(
            item_id="X", description="test", category="conducted",
        )
        assert item.status == TestStatus.pending

    def test_radio_checklist_empty(self):
        cl = RadioChecklist(region="fcc", region_name="FCC")
        assert cl.total == 0
        assert cl.complete is False

    def test_cert_artifact_to_dict(self):
        art = CertArtifact(
            artifact_id="test_report", name="Test Report",
            region="fcc", status="pending",
        )
        d = art.to_dict()
        assert d["artifact_id"] == "test_report"
        assert d["region"] == "fcc"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10. Sample radio spec → correct cert checklist (integration)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSampleRadioSpec:
    """End-to-end: a sample radio spec produces the correct checklist."""

    SAMPLE_SPEC = {
        "target_regions": ["fcc", "ce_red"],
        "radio": {
            "technology": "WiFi 6",
            "frequency_bands_mhz": [2400, 5200],
            "max_tx_power_dbm": 20,
            "antenna_type": "internal PIFA",
        },
        "provided_artifacts": [],
    }

    def test_correct_regions_in_checklist(self):
        checklists = validate_radio_checklist(self.SAMPLE_SPEC)
        assert len(checklists) == 2
        assert checklists[0].region == "fcc"
        assert checklists[1].region == "ce_red"

    def test_fcc_has_correct_test_items(self):
        checklists = validate_radio_checklist(self.SAMPLE_SPEC)
        fcc = checklists[0]
        test_ids = {i.item_id for i in fcc.items if i.category != "artifact"}
        assert "FCC-15B-CONDUCTED" in test_ids
        assert "FCC-15B-RADIATED" in test_ids
        assert "FCC-SAR" in test_ids
        assert "FCC-15C-TX-CONDUCTED" in test_ids
        assert "FCC-15C-TX-RADIATED" in test_ids

    def test_ce_red_has_correct_test_items(self):
        checklists = validate_radio_checklist(self.SAMPLE_SPEC)
        ce = checklists[1]
        test_ids = {i.item_id for i in ce.items if i.category != "artifact"}
        assert "RED-CONDUCTED" in test_ids
        assert "RED-RADIATED" in test_ids
        assert "RED-SAR" in test_ids
        assert "RED-ETSI-TX" in test_ids

    def test_fcc_has_correct_artifact_items(self):
        checklists = validate_radio_checklist(self.SAMPLE_SPEC)
        fcc = checklists[0]
        art_ids = {
            i.item_id.replace("artifact:", "")
            for i in fcc.items if i.category == "artifact"
        }
        assert "test_report" in art_ids
        assert "equipment_authorization" in art_ids
        assert "device_photos" in art_ids
        assert "label_artwork" in art_ids
        assert "user_manual_rf_statement" in art_ids

    def test_ce_red_has_doc_specific_artifacts(self):
        checklists = validate_radio_checklist(self.SAMPLE_SPEC)
        ce = checklists[1]
        art_ids = {
            i.item_id.replace("artifact:", "")
            for i in ce.items if i.category == "artifact"
        }
        assert "declaration_of_conformity" in art_ids
        assert "technical_documentation" in art_ids

    def test_all_items_start_pending(self):
        checklists = validate_radio_checklist(self.SAMPLE_SPEC)
        for cl in checklists:
            for item in cl.items:
                assert item.status == TestStatus.pending

    def test_complete_workflow(self):
        """Full workflow: run all tests → provide all artifacts → checklist complete."""
        fcc_reg = get_region("fcc")
        ce_reg = get_region("ce_red")

        all_results = []
        for reg in [fcc_reg, ce_reg]:
            for recipe in reg.test_recipes:
                if recipe.category != "sar":
                    all_results.append(EmissionsTestResult(
                        recipe_id=recipe.recipe_id, region=reg.region_id,
                        status=TestStatus.passed, device_under_test="DUT",
                    ))

        sar_results = {
            "fcc": SARResult(region="fcc", status=TestStatus.passed,
                             peak_sar_w_kg=1.0, limit_w_kg=1.6),
            "ce_red": SARResult(region="ce_red", status=TestStatus.passed,
                                peak_sar_w_kg=1.5, limit_w_kg=2.0),
        }

        all_artifacts = list(set(
            fcc_reg.required_artifacts + ce_reg.required_artifacts
        ))
        spec = {
            "target_regions": ["fcc", "ce_red"],
            "provided_artifacts": all_artifacts,
        }

        checklists = validate_radio_checklist(
            spec, test_results=all_results, sar_results=sar_results,
        )
        for cl in checklists:
            assert cl.complete is True, f"Region {cl.region} not complete: {cl.to_dict()}"
