"""Tests for L4-CORE-06 — Document suite generator (#215).

Covers:
  - Per-product-class template selection
  - Template rendering for all 7 templates
  - Compliance-cert merging
  - PDF export (mock-based)
  - Suite generation per product class
  - DocSuiteContext building from ParsedSpec
  - Edge cases
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from backend.doc_suite_generator import (
    ALL_TEMPLATE_NAMES,
    PRODUCT_CLASS_TEMPLATES,
    SUITE_VERSION,
    ComplianceCert,
    DocSuiteContext,
    GeneratedDoc,
    collect_compliance_certs,
    export_suite_to_dir,
    from_parsed_spec,
    generate_suite,
    render_doc_pdf,
    render_single,
    templates_for_class,
)
from backend.hardware_profile import HardwareProfile
from backend.models import ProjectClass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def sample_hw_profile() -> dict[str, Any]:
    return HardwareProfile(
        soc="Hi3516DV300",
        mcu="STM32F405",
        dsp="C66x",
        npu="NNIE",
        sensor=["IMX307", "OV2718"],
        codec=["H.264", "H.265"],
        usb=["USB 2.0 Host", "USB 2.0 Device"],
        display="7-inch TFT 1024x600",
        peripherals=[],
    ).model_dump()


@pytest.fixture
def sample_parsed_spec() -> dict[str, Any]:
    return {
        "project_type": {"value": "embedded_firmware", "confidence": 0.9},
        "project_class": {"value": "embedded_product", "confidence": 0.95},
        "runtime_model": {"value": "batch", "confidence": 0.8},
        "target_arch": {"value": "aarch64", "confidence": 0.95},
        "target_os": {"value": "linux", "confidence": 0.9},
        "framework": {"value": "custom", "confidence": 0.7},
        "persistence": {"value": "sqlite", "confidence": 0.6},
        "deploy_target": {"value": "edge_device", "confidence": 0.85},
    }


@pytest.fixture
def full_context(sample_hw_profile, sample_parsed_spec) -> DocSuiteContext:
    return DocSuiteContext(
        product_name="OmniCam Pro",
        product_version="2.0.0",
        product_description="AI-powered IP camera with edge inference",
        project_class="embedded_product",
        hardware_profile=sample_hw_profile,
        parsed_spec=sample_parsed_spec,
        compliance_certs=[
            {"standard": "FCC Part 15B", "status": "Passed", "cert_id": "FCC-123"},
            {"standard": "CE EN 55032", "status": "Pending"},
        ],
    )


@pytest.fixture
def minimal_context() -> DocSuiteContext:
    return DocSuiteContext(product_name="MinimalProduct")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Template selection per product class
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTemplateSelection:

    def test_all_project_classes_have_templates(self):
        for pc in ProjectClass:
            tpls = templates_for_class(pc.value)
            assert len(tpls) > 0, f"No templates for {pc.value}"

    def test_embedded_product_has_all_seven(self):
        tpls = templates_for_class("embedded_product")
        assert len(tpls) == 7
        assert set(tpls) == set(ALL_TEMPLATE_NAMES)

    def test_algo_sim_subset(self):
        tpls = templates_for_class("algo_sim")
        assert "datasheet" not in tpls
        assert "compliance" not in tpls
        assert "security" not in tpls
        assert "api_doc" in tpls
        assert "sbom" in tpls

    def test_enterprise_web_has_security(self):
        tpls = templates_for_class("enterprise_web")
        assert "security" in tpls
        assert "api_doc" in tpls
        assert "datasheet" not in tpls

    def test_iso_standard_has_compliance(self):
        tpls = templates_for_class("iso_standard")
        assert "compliance" in tpls
        assert "security" in tpls

    def test_factory_tool_has_datasheet(self):
        tpls = templates_for_class("factory_tool")
        assert "datasheet" in tpls
        assert "compliance" in tpls

    def test_unknown_class_falls_back_to_embedded(self):
        tpls = templates_for_class("nonexistent_class")
        expected = templates_for_class("embedded_product")
        assert tpls == expected

    def test_all_templates_constant(self):
        assert len(ALL_TEMPLATE_NAMES) == 7
        assert "datasheet" in ALL_TEMPLATE_NAMES
        assert "sbom" in ALL_TEMPLATE_NAMES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Single template rendering
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRenderSingle:

    def test_render_datasheet(self, full_context):
        doc = render_single("datasheet", full_context.to_template_vars())
        assert doc.format == "markdown"
        assert "Hi3516DV300" in doc.content
        assert "OmniCam Pro" in doc.content
        assert "Technical Datasheet" in doc.content

    def test_render_user_manual(self, full_context):
        doc = render_single("user_manual", full_context.to_template_vars())
        assert "User Manual" in doc.content
        assert "OmniCam Pro" in doc.content

    def test_render_compliance(self, full_context):
        doc = render_single("compliance", full_context.to_template_vars())
        assert "Compliance" in doc.content

    def test_render_api_doc(self, full_context):
        doc = render_single("api_doc", full_context.to_template_vars())
        assert "API Reference" in doc.content

    def test_render_sbom_json(self, full_context):
        doc = render_single("sbom", full_context.to_template_vars())
        assert doc.format == "json"
        parsed = json.loads(doc.content)
        assert parsed["bomFormat"] == "CycloneDX"
        assert parsed["specVersion"] == "1.5"
        assert "OmniCam Pro" in parsed["metadata"]["component"]["name"]

    def test_render_eula(self, full_context):
        doc = render_single("eula", full_context.to_template_vars())
        assert "End-User License Agreement" in doc.content
        assert "OmniCam Pro" in doc.content

    def test_render_security(self, full_context):
        doc = render_single("security", full_context.to_template_vars())
        assert "Security Assessment" in doc.content
        assert "Threat Model" in doc.content

    def test_render_with_minimal_context(self, minimal_context):
        doc = render_single("datasheet", minimal_context.to_template_vars())
        assert "MinimalProduct" in doc.content

    def test_render_nonexistent_template_raises(self):
        with pytest.raises(FileNotFoundError):
            render_single("nonexistent_template_xyz", {})

    def test_hardware_profile_fields_in_datasheet(self, full_context):
        doc = render_single("datasheet", full_context.to_template_vars())
        assert "STM32F405" in doc.content
        assert "IMX307" in doc.content
        assert "H.264" in doc.content

    def test_parsed_spec_fields_in_datasheet(self, full_context):
        doc = render_single("datasheet", full_context.to_template_vars())
        assert "aarch64" in doc.content
        assert "linux" in doc.content


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Compliance cert merging
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestComplianceMerging:

    def test_extra_certs_only(self):
        certs = collect_compliance_certs([
            {"standard": "FCC", "status": "Passed"},
            {"standard": "CE", "status": "Pending"},
        ])
        assert len(certs) == 2
        assert certs[0].standard == "FCC"
        assert certs[1].status == "Pending"

    def test_no_certs(self):
        certs = collect_compliance_certs()
        assert certs == []

    def test_empty_list(self):
        certs = collect_compliance_certs([])
        assert certs == []

    def test_cert_with_details(self):
        certs = collect_compliance_certs([
            {"standard": "ISO 26262", "status": "ASIL-B", "cert_id": "ISO-42",
             "details": {"asil_level": "B"}},
        ])
        assert certs[0].cert_id == "ISO-42"
        assert certs[0].details["asil_level"] == "B"

    @patch("backend.doc_suite_generator._try_safety_certs")
    def test_safety_certs_merged(self, mock_safety):
        mock_safety.return_value = [
            ComplianceCert(standard="IEC 61508", status="SIL-2"),
        ]
        certs = collect_compliance_certs([{"standard": "FCC", "status": "OK"}])
        standards = [c.standard for c in certs]
        assert "IEC 61508" in standards
        assert "FCC" in standards

    @patch("backend.doc_suite_generator._try_radio_certs")
    def test_radio_certs_merged(self, mock_radio):
        mock_radio.return_value = [
            ComplianceCert(standard="FCC Part 15", status="Submitted"),
        ]
        certs = collect_compliance_certs()
        assert len(certs) == 1
        assert certs[0].standard == "FCC Part 15"

    @patch("backend.doc_suite_generator._try_payment_certs")
    def test_payment_certs_merged(self, mock_pay):
        mock_pay.return_value = [
            ComplianceCert(standard="PCI-DSS", status="Level 1"),
        ]
        certs = collect_compliance_certs()
        assert certs[0].standard == "PCI-DSS"

    def test_compliance_in_rendered_doc(self, full_context):
        tv = full_context.to_template_vars()
        tv["standards"] = [{"name": "FCC Part 15B", "description": "Class B"}]
        doc = render_single("compliance", tv)
        assert "FCC Part 15B" in doc.content


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Suite generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSuiteGeneration:

    def test_embedded_product_suite(self, full_context):
        docs = generate_suite(full_context)
        assert len(docs) == 7
        names = {d.name for d in docs}
        assert names == set(ALL_TEMPLATE_NAMES)

    def test_algo_sim_suite(self, sample_parsed_spec):
        ctx = DocSuiteContext(
            product_name="AlgoRunner",
            project_class="algo_sim",
            parsed_spec=sample_parsed_spec,
        )
        docs = generate_suite(ctx)
        assert len(docs) == 4
        names = {d.name for d in docs}
        assert "datasheet" not in names
        assert "api_doc" in names

    def test_enterprise_web_suite(self):
        ctx = DocSuiteContext(
            product_name="WebPortal",
            project_class="enterprise_web",
        )
        docs = generate_suite(ctx)
        names = {d.name for d in docs}
        assert "security" in names
        assert "api_doc" in names
        assert "datasheet" not in names

    def test_iso_standard_suite(self):
        ctx = DocSuiteContext(
            product_name="ISO Validator",
            project_class="iso_standard",
        )
        docs = generate_suite(ctx)
        names = {d.name for d in docs}
        assert "compliance" in names
        assert "security" in names

    def test_test_tool_suite(self):
        ctx = DocSuiteContext(
            product_name="TestHarness",
            project_class="test_tool",
        )
        docs = generate_suite(ctx)
        assert len(docs) == len(templates_for_class("test_tool"))

    def test_factory_tool_suite(self):
        ctx = DocSuiteContext(
            product_name="JigController",
            project_class="factory_tool",
        )
        docs = generate_suite(ctx)
        names = {d.name for d in docs}
        assert "datasheet" in names
        assert "compliance" in names

    def test_optical_sim_suite(self):
        ctx = DocSuiteContext(
            product_name="LensDesigner",
            project_class="optical_sim",
        )
        docs = generate_suite(ctx)
        assert len(docs) == len(templates_for_class("optical_sim"))

    def test_custom_template_list(self, minimal_context):
        docs = generate_suite(minimal_context, templates=("eula", "sbom"))
        assert len(docs) == 2
        assert docs[0].name == "eula"
        assert docs[1].name == "sbom"

    def test_all_docs_have_content(self, full_context):
        docs = generate_suite(full_context)
        for doc in docs:
            assert len(doc.content) > 50, f"Doc {doc.name} is too short"

    def test_sbom_is_valid_json(self, full_context):
        docs = generate_suite(full_context)
        sbom_doc = next(d for d in docs if d.name == "sbom")
        parsed = json.loads(sbom_doc.content)
        assert "bomFormat" in parsed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PDF export
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPDFExport:

    @patch("backend.report_generator.render_pdf", return_value=b"%PDF-mock")
    def test_render_markdown_doc_pdf(self, mock_pdf):
        doc = GeneratedDoc(name="eula", template="eula.md.j2",
                           content="# EULA\n\nSome terms.", format="markdown")
        result = render_doc_pdf(doc)
        assert result == b"%PDF-mock"
        mock_pdf.assert_called_once_with("# EULA\n\nSome terms.")

    @patch("backend.report_generator.render_pdf", return_value=b"%PDF-sbom")
    def test_render_json_doc_pdf(self, mock_pdf):
        content = json.dumps({"bomFormat": "CycloneDX"})
        doc = GeneratedDoc(name="sbom", template="sbom.json.j2",
                           content=content, format="json")
        result = render_doc_pdf(doc)
        assert result == b"%PDF-sbom"
        call_arg = mock_pdf.call_args[0][0]
        assert "SBOM" in call_arg
        assert "CycloneDX" in call_arg

    @patch("backend.report_generator.render_pdf", return_value=b"%PDF-data")
    def test_export_suite_to_dir(self, mock_pdf, full_context, tmp_path):
        docs = generate_suite(full_context)
        manifest = export_suite_to_dir(docs, tmp_path / "output")
        assert len(manifest) == 7
        for entry in manifest:
            assert Path(entry["markdown_path"]).exists()
            assert entry.get("pdf_path") is not None
            assert entry["pdf_size"] == len(b"%PDF-data")

    @patch("backend.report_generator.render_pdf", side_effect=ImportError("no weasyprint"))
    def test_export_graceful_pdf_failure(self, mock_pdf, minimal_context, tmp_path):
        docs = generate_suite(minimal_context, templates=("eula",))
        manifest = export_suite_to_dir(docs, tmp_path / "out")
        assert manifest[0]["pdf_path"] is None
        assert "weasyprint" in manifest[0]["pdf_error"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  from_parsed_spec convenience
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFromParsedSpec:

    def test_basic(self, sample_parsed_spec, sample_hw_profile):
        hw = HardwareProfile(**sample_hw_profile)
        ctx = from_parsed_spec(
            sample_parsed_spec,
            hardware_profile=hw,
            product_name="TestCam",
        )
        assert ctx.product_name == "TestCam"
        assert ctx.project_class == "embedded_product"
        assert ctx.hardware_profile is not None

    def test_unknown_class_defaults_to_embedded(self):
        spec = {"project_class": {"value": "unknown", "confidence": 0.0}}
        ctx = from_parsed_spec(spec)
        assert ctx.project_class == "embedded_product"

    def test_string_project_class(self):
        spec = {"project_class": "enterprise_web"}
        ctx = from_parsed_spec(spec)
        assert ctx.project_class == "enterprise_web"

    def test_hw_from_spec_dict(self):
        hw_dict = HardwareProfile(soc="RK3588").model_dump()
        spec = {"project_class": {"value": "embedded_product", "confidence": 0.9},
                "hardware_profile": hw_dict}
        ctx = from_parsed_spec(spec)
        assert ctx.hardware_profile is not None
        assert ctx.hardware_profile["soc"] == "RK3588"

    def test_extra_passed_through(self, sample_parsed_spec):
        ctx = from_parsed_spec(
            sample_parsed_spec,
            extra={"custom_field": "hello"},
        )
        tv = ctx.to_template_vars()
        assert tv["custom_field"] == "hello"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DocSuiteContext
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDocSuiteContext:

    def test_date_auto_set(self):
        ctx = DocSuiteContext()
        assert ctx.date != ""
        assert "UTC" in ctx.date

    def test_explicit_date(self):
        ctx = DocSuiteContext(date="2026-01-01")
        assert ctx.date == "2026-01-01"

    def test_template_vars_basic(self, minimal_context):
        tv = minimal_context.to_template_vars()
        assert tv["product_name"] == "MinimalProduct"
        assert tv["project_class"] == "embedded_product"
        assert "sbom_uuid" in tv

    def test_template_vars_with_hw(self, full_context):
        tv = full_context.to_template_vars()
        assert tv["hardware_profile"]["soc"] == "Hi3516DV300"
        assert "soc" in tv["hardware_spec"]

    def test_template_vars_with_parsed_spec(self, full_context):
        tv = full_context.to_template_vars()
        assert tv["parsed_spec"]["target_arch"] == "aarch64"

    def test_compliance_certs_in_vars(self, full_context):
        tv = full_context.to_template_vars()
        assert len(tv["compliance_certs"]) >= 2
        standards = [c["standard"] for c in tv["compliance_certs"]]
        assert "FCC Part 15B" in standards


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEdgeCases:

    def test_empty_hw_profile(self):
        ctx = DocSuiteContext(hardware_profile={})
        docs = generate_suite(ctx, templates=("datasheet",))
        assert len(docs) == 1
        assert "Technical Datasheet" in docs[0].content

    def test_none_hw_profile(self):
        ctx = DocSuiteContext(hardware_profile=None)
        docs = generate_suite(ctx, templates=("datasheet",))
        assert "No hardware profile available" in docs[0].content

    def test_suite_version(self):
        assert SUITE_VERSION == "1.0.0"

    def test_product_class_map_completeness(self):
        for pc in ProjectClass:
            assert pc.value in PRODUCT_CLASS_TEMPLATES

    def test_all_templates_renderable(self):
        ctx = DocSuiteContext(product_name="RenderTest")
        for tname in ALL_TEMPLATE_NAMES:
            doc = render_single(tname, ctx.to_template_vars())
            assert doc.content, f"Empty content for {tname}"

    def test_generated_doc_fields(self):
        doc = GeneratedDoc(name="test", template="test.md.j2",
                           content="hello", format="markdown")
        assert doc.name == "test"
        assert doc.template == "test.md.j2"
        assert doc.content == "hello"
        assert doc.format == "markdown"
