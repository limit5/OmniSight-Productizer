"""C19 — L4-CORE-19 Imaging / document pipeline tests (#240).

Covers:
  - Scanner ISP: sensor types, color modes, ISP stages, pipeline execution
  - ISP stages: dark frame subtraction, white balance, gamma, CCM, edge enhance,
    noise reduction, binarization, deskew
  - OCR: engine listing, engine detail, preprocessing, OCR run (all 3 engines)
  - TWAIN: capability listing, state machine transitions, driver generation
  - SANE: option listing, API functions, backend generation
  - ICC profiles: profile listing, profile generation (sRGB, Adobe RGB, grey),
    embedding (TIFF, JPEG, PNG, PDF), rendering intents, profile classes
  - Test recipes: listing, execution
  - SoC compatibility
  - Artifact definitions
  - Gate validation (pass + fail)
  - Cert generation + registry
  - Edge cases (unknown IDs, invalid params)
  - REST endpoint smoke tests
"""

from __future__ import annotations


import pytest

from backend.imaging_pipeline import (
    BitDepth,
    ColorMode,
    GateVerdict,
    ICCProfileClass,
    ISPStageId,
    ImagingDomain,
    OCREngine,
    OCROutputFormat,
    OutputFormat,
    RenderingIntent,
    SANEStatus,
    SensorType,
    TWAINState,
    TestStatus,
    clear_imaging_certs,
    embed_icc_profile,
    generate_cert_artifacts,
    generate_icc_profile_binary,
    generate_sane_backend,
    generate_twain_driver,
    get_artifact_definition,
    get_color_mode,
    get_compatible_soc,
    get_icc_embedding_format,
    get_icc_profile,
    get_isp_stage,
    get_ocr_engine,
    get_sane_option,
    get_sensor_type,
    get_test_recipe,
    get_twain_capability,
    get_imaging_certs,
    list_artifact_definitions,
    list_color_modes,
    list_compatible_socs,
    list_icc_embedding_formats,
    list_icc_profile_classes,
    list_icc_profiles,
    list_isp_stages,
    list_ocr_engines,
    list_ocr_preprocessing,
    list_output_formats,
    list_rendering_intents,
    list_sane_api_functions,
    list_sane_options,
    list_sensor_types,
    list_test_recipes,
    list_twain_capabilities,
    list_twain_states,
    run_isp_pipeline,
    run_ocr,
    run_test_recipe,
    twain_transition,
    validate_imaging_gate,
)


# ═══════════════════════════════════════════════════════════════════════
#  Enums
# ═══════════════════════════════════════════════════════════════════════

class TestEnums:
    def test_imaging_domain_values(self):
        assert ImagingDomain.scanner_isp.value == "scanner_isp"
        assert ImagingDomain.ocr.value == "ocr"
        assert ImagingDomain.twain.value == "twain"
        assert ImagingDomain.sane.value == "sane"
        assert ImagingDomain.icc_profiles.value == "icc_profiles"
        assert ImagingDomain.integration.value == "integration"

    def test_sensor_type_values(self):
        assert SensorType.cis.value == "cis"
        assert SensorType.ccd.value == "ccd"

    def test_color_mode_values(self):
        assert ColorMode.grey_8bit.value == "grey_8bit"
        assert ColorMode.grey_16bit.value == "grey_16bit"
        assert ColorMode.rgb_24bit.value == "rgb_24bit"
        assert ColorMode.rgb_48bit.value == "rgb_48bit"

    def test_ocr_engine_values(self):
        assert OCREngine.tesseract.value == "tesseract"
        assert OCREngine.paddleocr.value == "paddleocr"
        assert OCREngine.vendor_sdk.value == "vendor_sdk"

    def test_twain_state_values(self):
        assert TWAINState.pre_session.value == 1
        assert TWAINState.transferring.value == 7

    def test_sane_status_values(self):
        assert SANEStatus.good.value == "SANE_STATUS_GOOD"
        assert SANEStatus.eof.value == "SANE_STATUS_EOF"

    def test_icc_profile_class_values(self):
        assert ICCProfileClass.input_profile.value == "scnr"
        assert ICCProfileClass.display.value == "mntr"
        assert ICCProfileClass.output_profile.value == "prtr"

    def test_rendering_intent_values(self):
        assert RenderingIntent.perceptual.value == "perceptual"
        assert RenderingIntent.absolute_colorimetric.value == "absolute_colorimetric"

    def test_output_format_values(self):
        assert OutputFormat.tiff.value == "tiff"
        assert OutputFormat.pdf.value == "pdf"

    def test_bit_depth_values(self):
        assert BitDepth.eight.value == "8bit"
        assert BitDepth.sixteen.value == "16bit"

    def test_isp_stage_id_values(self):
        assert ISPStageId.dark_frame_subtraction.value == "dark_frame_subtraction"
        assert ISPStageId.binarization.value == "binarization"

    def test_test_status_values(self):
        assert TestStatus.passed.value == "passed"
        assert TestStatus.error.value == "error"

    def test_gate_verdict_values(self):
        assert GateVerdict.passed.value == "passed"
        assert GateVerdict.failed.value == "failed"

    def test_ocr_output_format_values(self):
        assert OCROutputFormat.text.value == "text"
        assert OCROutputFormat.hocr.value == "hocr"


# ═══════════════════════════════════════════════════════════════════════
#  Scanner ISP
# ═══════════════════════════════════════════════════════════════════════

class TestScannerISP:
    def test_list_sensor_types(self):
        sensors = list_sensor_types()
        assert len(sensors) == 2
        ids = {s.sensor_id for s in sensors}
        assert "cis" in ids
        assert "ccd" in ids

    def test_get_sensor_type_cis(self):
        s = get_sensor_type("cis")
        assert s is not None
        assert s.name == "Contact Image Sensor"
        assert s.calibration_required is True
        assert "grey_8bit" in s.color_modes

    def test_get_sensor_type_ccd(self):
        s = get_sensor_type("ccd")
        assert s is not None
        assert s.name == "Charge-Coupled Device"
        assert 4800 in s.typical_resolution_dpi

    def test_get_sensor_type_unknown(self):
        assert get_sensor_type("lidar") is None

    def test_list_color_modes(self):
        modes = list_color_modes()
        assert len(modes) == 4
        ids = {m.mode_id for m in modes}
        assert "grey_8bit" in ids
        assert "rgb_48bit" in ids

    def test_get_color_mode_grey_8bit(self):
        m = get_color_mode("grey_8bit")
        assert m is not None
        assert m.channels == 1
        assert m.bits_per_channel == 8
        assert m.total_bits_per_pixel == 8

    def test_get_color_mode_rgb_24bit(self):
        m = get_color_mode("rgb_24bit")
        assert m is not None
        assert m.channels == 3
        assert m.bits_per_channel == 8
        assert m.total_bits_per_pixel == 24

    def test_get_color_mode_unknown(self):
        assert get_color_mode("cmyk") is None

    def test_list_isp_stages(self):
        stages = list_isp_stages()
        assert len(stages) >= 8
        orders = [s.order for s in stages]
        assert orders == sorted(orders)

    def test_get_isp_stage_dark_frame(self):
        s = get_isp_stage("dark_frame_subtraction")
        assert s is not None
        assert s.required is True
        assert s.order == 1

    def test_get_isp_stage_color_matrix(self):
        s = get_isp_stage("color_matrix")
        assert s is not None
        assert s.required is False
        assert "rgb_24bit" in s.applies_to

    def test_get_isp_stage_unknown(self):
        assert get_isp_stage("hdr_merge") is None

    def test_list_output_formats(self):
        fmts = list_output_formats()
        assert len(fmts) >= 6
        ids = {f.format_id for f in fmts}
        assert "tiff" in ids
        assert "pdf" in ids

    def test_run_isp_pipeline_grey_8bit(self):
        result = run_isp_pipeline("cis", "grey_8bit")
        assert result.success is True
        assert "dark_frame_subtraction" in result.stages_applied
        assert "white_balance" in result.stages_applied
        assert "gamma_correction" in result.stages_applied
        assert result.output_bit_depth == 8
        assert result.output_channels == 1

    def test_run_isp_pipeline_rgb_24bit(self):
        result = run_isp_pipeline("ccd", "rgb_24bit")
        assert result.success is True
        assert "color_matrix" in result.stages_applied
        assert result.output_channels == 3

    def test_run_isp_pipeline_grey_16bit(self):
        result = run_isp_pipeline("ccd", "grey_16bit")
        assert result.success is True
        assert result.output_bit_depth == 16

    def test_run_isp_pipeline_rgb_48bit(self):
        result = run_isp_pipeline("ccd", "rgb_48bit")
        assert result.success is True
        assert result.output_bit_depth == 16
        assert result.output_channels == 3

    def test_run_isp_pipeline_custom_stages(self):
        result = run_isp_pipeline(
            "cis", "grey_8bit",
            stage_ids=["dark_frame_subtraction", "white_balance", "binarization"],
        )
        assert result.success is True
        assert "binarization" in result.stages_applied

    def test_run_isp_pipeline_with_raw_data(self):
        raw = [128, 130, 125, 140, 135, 120, 110, 150, 145, 132]
        result = run_isp_pipeline("cis", "grey_8bit", raw_pixels=raw)
        assert result.success is True
        assert result.input_pixels == 10

    def test_run_isp_pipeline_unknown_sensor(self):
        result = run_isp_pipeline("unknown", "grey_8bit")
        assert result.success is False
        assert "Unknown sensor type" in result.error

    def test_run_isp_pipeline_unknown_color_mode(self):
        result = run_isp_pipeline("cis", "cmyk_32bit")
        assert result.success is False
        assert "Unknown color mode" in result.error

    def test_run_isp_pipeline_incompatible_mode(self):
        result = run_isp_pipeline("cis", "rgb_48bit")
        assert result.success is False
        assert "not supported" in result.error

    def test_run_isp_pipeline_unknown_stage(self):
        result = run_isp_pipeline("cis", "grey_8bit", stage_ids=["nonexistent"])
        assert result.success is False
        assert "Unknown ISP stage" in result.error

    def test_run_isp_pipeline_edge_enhancement(self):
        result = run_isp_pipeline(
            "cis", "grey_8bit",
            stage_ids=["dark_frame_subtraction", "edge_enhancement"],
        )
        assert result.success is True
        assert "edge_enhancement" in result.stages_applied

    def test_run_isp_pipeline_noise_reduction(self):
        result = run_isp_pipeline(
            "cis", "grey_8bit",
            stage_ids=["dark_frame_subtraction", "noise_reduction"],
        )
        assert result.success is True
        assert "noise_reduction" in result.stages_applied

    def test_run_isp_pipeline_elapsed_ms(self):
        result = run_isp_pipeline("cis", "grey_8bit")
        assert result.elapsed_ms >= 0

    def test_run_isp_pipeline_color_matrix_skipped_for_grey(self):
        result = run_isp_pipeline(
            "cis", "grey_8bit",
            stage_ids=["dark_frame_subtraction", "color_matrix"],
        )
        assert result.success is True
        assert "color_matrix" not in result.stages_applied


# ═══════════════════════════════════════════════════════════════════════
#  OCR
# ═══════════════════════════════════════════════════════════════════════

class TestOCR:
    def test_list_ocr_engines(self):
        engines = list_ocr_engines()
        assert len(engines) == 3
        ids = {e.engine_id for e in engines}
        assert "tesseract" in ids
        assert "paddleocr" in ids
        assert "vendor_sdk" in ids

    def test_get_ocr_engine_tesseract(self):
        e = get_ocr_engine("tesseract")
        assert e is not None
        assert e.name == "Tesseract OCR"
        assert "eng" in e.languages_builtin
        assert "text" in e.output_formats
        assert "page_segmentation" in e.capabilities

    def test_get_ocr_engine_paddleocr(self):
        e = get_ocr_engine("paddleocr")
        assert e is not None
        assert "table_recognition" in e.capabilities

    def test_get_ocr_engine_vendor_sdk(self):
        e = get_ocr_engine("vendor_sdk")
        assert e is not None
        assert "handwriting_recognition" in e.capabilities

    def test_get_ocr_engine_unknown(self):
        assert get_ocr_engine("abbyy") is None

    def test_list_ocr_preprocessing(self):
        steps = list_ocr_preprocessing()
        assert len(steps) >= 4
        ids = [s["id"] for s in steps]
        assert "deskew" in ids
        assert "rescale" in ids

    def test_run_ocr_tesseract(self):
        result = run_ocr("tesseract")
        assert result.success is True
        assert result.engine_id == "tesseract"
        assert len(result.text) > 0
        assert result.confidence > 0.0
        assert len(result.regions) > 0

    def test_run_ocr_paddleocr(self):
        result = run_ocr("paddleocr", language="ch", output_format="json")
        assert result.success is True
        assert result.engine_id == "paddleocr"

    def test_run_ocr_vendor_sdk(self):
        result = run_ocr("vendor_sdk", output_format="text")
        assert result.success is True

    def test_run_ocr_with_image_data(self):
        result = run_ocr("tesseract", image_data=b"fake_image_data")
        assert result.success is True
        assert "tesseract" in result.text

    def test_run_ocr_unknown_engine(self):
        result = run_ocr("nonexistent")
        assert result.success is False
        assert "Unknown OCR engine" in result.error

    def test_run_ocr_unsupported_format(self):
        result = run_ocr("tesseract", output_format="docx")
        assert result.success is False
        assert "not supported" in result.error

    def test_run_ocr_elapsed_ms(self):
        result = run_ocr("tesseract")
        assert result.elapsed_ms >= 0


# ═══════════════════════════════════════════════════════════════════════
#  TWAIN
# ═══════════════════════════════════════════════════════════════════════

class TestTWAIN:
    def test_list_twain_capabilities(self):
        caps = list_twain_capabilities()
        assert len(caps) >= 6
        mandatory = [c for c in caps if c.mandatory]
        assert len(mandatory) >= 6

    def test_get_twain_capability(self):
        c = get_twain_capability("ICAP_PIXELTYPE")
        assert c is not None
        assert "TWPT_RGB" in c.values

    def test_get_twain_capability_optional(self):
        c = get_twain_capability("CAP_FEEDERENABLED")
        assert c is not None
        assert c.mandatory is False

    def test_get_twain_capability_unknown(self):
        assert get_twain_capability("FAKE_CAP") is None

    def test_list_twain_states(self):
        states = list_twain_states()
        assert len(states) == 7
        state_nums = {s.state for s in states}
        assert state_nums == {1, 2, 3, 4, 5, 6, 7}

    def test_twain_transition_valid_forward(self):
        ok, msg = twain_transition(1, 2)
        assert ok is True

    def test_twain_transition_valid_backward(self):
        ok, msg = twain_transition(2, 1)
        assert ok is True

    def test_twain_transition_full_cycle(self):
        state = 1
        for target in [2, 3, 4, 5, 6, 7]:
            ok, msg = twain_transition(state, target)
            assert ok is True, f"Failed {state}→{target}: {msg}"
            state = target
        for target in [6, 5, 4, 3, 2, 1]:
            ok, msg = twain_transition(state, target)
            assert ok is True, f"Failed {state}→{target}: {msg}"
            state = target

    def test_twain_transition_invalid_skip(self):
        ok, msg = twain_transition(1, 4)
        assert ok is False

    def test_twain_transition_invalid_state(self):
        ok, msg = twain_transition(0, 1)
        assert ok is False

    def test_twain_transition_invalid_target(self):
        ok, msg = twain_transition(1, 8)
        assert ok is False

    def test_generate_twain_driver_default(self):
        t = generate_twain_driver("TestScanner 3000")
        assert t.device_name == "TestScanner 3000"
        assert len(t.capabilities) >= 6
        assert "DS_Entry" in t.source_code
        assert "TWAIN_DS_" in t.header_code
        assert t.generated_at != ""

    def test_generate_twain_driver_custom_caps(self):
        t = generate_twain_driver("My Scanner", capabilities=["ICAP_PIXELTYPE", "ICAP_XRESOLUTION"])
        assert len(t.capabilities) == 2
        assert "ICAP_PIXELTYPE" in t.source_code

    def test_generate_twain_driver_contains_state_machine(self):
        t = generate_twain_driver("Scanner X")
        assert "g_state" in t.source_code
        assert "DS_Image_NativeXfer" in t.source_code
        assert "DS_Image_MemXfer" in t.source_code

    def test_generate_twain_driver_header_guard(self):
        t = generate_twain_driver("My Device")
        assert "#ifndef" in t.header_code
        assert "#endif" in t.header_code


# ═══════════════════════════════════════════════════════════════════════
#  SANE
# ═══════════════════════════════════════════════════════════════════════

class TestSANE:
    def test_list_sane_options(self):
        opts = list_sane_options()
        assert len(opts) >= 5
        mandatory = [o for o in opts if o.mandatory]
        assert len(mandatory) >= 5

    def test_get_sane_option_mode(self):
        o = get_sane_option("mode")
        assert o is not None
        assert "Color" in o.values

    def test_get_sane_option_resolution(self):
        o = get_sane_option("resolution")
        assert o is not None
        assert o.unit == "SANE_UNIT_DPI"

    def test_get_sane_option_optional(self):
        o = get_sane_option("source")
        assert o is not None
        assert o.mandatory is False
        assert "Flatbed" in o.values

    def test_get_sane_option_unknown(self):
        assert get_sane_option("nonexistent") is None

    def test_list_sane_api_functions(self):
        funcs = list_sane_api_functions()
        assert len(funcs) >= 11
        names = [f["name"] for f in funcs]
        assert "sane_init" in names
        assert "sane_read" in names
        assert "sane_exit" in names

    def test_generate_sane_backend_default(self):
        t = generate_sane_backend("TestScanner")
        assert t.device_name == "TestScanner"
        assert len(t.options) >= 5
        assert "sane_init" in t.source_code
        assert "sane_read" in t.source_code
        assert "SANE_BACKEND_" in t.header_code
        assert t.generated_at != ""

    def test_generate_sane_backend_custom_options(self):
        t = generate_sane_backend("MyScan", options=["mode", "resolution"])
        assert len(t.options) == 2

    def test_generate_sane_backend_option_descriptors(self):
        t = generate_sane_backend("Scanner Y")
        assert "SANE_TYPE_STRING" in t.source_code or "mode" in t.source_code
        assert "sane_get_option_descriptor" in t.source_code

    def test_generate_sane_backend_header_guard(self):
        t = generate_sane_backend("My Device")
        assert "#ifndef" in t.header_code
        assert "#endif" in t.header_code

    def test_generate_sane_backend_device_info(self):
        t = generate_sane_backend("Acme Scanner 9000")
        assert "Acme Scanner 9000" in t.source_code
        assert "OmniSight" in t.source_code


# ═══════════════════════════════════════════════════════════════════════
#  ICC Profiles
# ═══════════════════════════════════════════════════════════════════════

class TestICCProfiles:
    def test_list_icc_profiles(self):
        profiles = list_icc_profiles()
        assert len(profiles) == 3
        ids = {p.profile_id for p in profiles}
        assert "srgb" in ids
        assert "adobe_rgb" in ids
        assert "grey_gamma22" in ids

    def test_get_icc_profile_srgb(self):
        p = get_icc_profile("srgb")
        assert p is not None
        assert p.name == "sRGB IEC61966-2.1"
        assert p.gamma == 2.2
        assert p.illuminant == "D65"
        assert len(p.white_point) == 3
        assert len(p.red_primary) == 3

    def test_get_icc_profile_adobe_rgb(self):
        p = get_icc_profile("adobe_rgb")
        assert p is not None
        assert "Adobe" in p.name

    def test_get_icc_profile_grey(self):
        p = get_icc_profile("grey_gamma22")
        assert p is not None
        assert len(p.red_primary) == 0

    def test_get_icc_profile_unknown(self):
        assert get_icc_profile("prophoto") is None

    def test_list_icc_profile_classes(self):
        classes = list_icc_profile_classes()
        assert len(classes) >= 3
        ids = [c["id"] for c in classes]
        assert "input" in ids
        assert "display" in ids
        assert "output" in ids

    def test_list_icc_embedding_formats(self):
        fmts = list_icc_embedding_formats()
        assert len(fmts) >= 4
        fmt_ids = {f.format_id for f in fmts}
        assert "tiff" in fmt_ids
        assert "jpeg" in fmt_ids
        assert "png" in fmt_ids
        assert "pdf" in fmt_ids

    def test_get_icc_embedding_format_tiff(self):
        f = get_icc_embedding_format("tiff")
        assert f is not None
        assert f.method == "binary_blob"
        assert f.tag_id == 34675

    def test_get_icc_embedding_format_jpeg(self):
        f = get_icc_embedding_format("jpeg")
        assert f is not None
        assert f.method == "icc_profile_chunks"
        assert f.max_chunk_size == 65519

    def test_get_icc_embedding_format_unknown(self):
        assert get_icc_embedding_format("webp") is None

    def test_list_rendering_intents(self):
        intents = list_rendering_intents()
        assert len(intents) == 4
        values = {i["value"] for i in intents}
        assert values == {0, 1, 2, 3}

    def test_generate_icc_profile_srgb(self):
        p = generate_icc_profile_binary("srgb")
        assert p.profile_id == "srgb"
        assert p.profile_class == "scnr"
        assert len(p.data) > 0
        assert p.size > 128
        assert p.checksum != ""
        assert p.data[:4] != b"\x00\x00\x00\x00"

    def test_generate_icc_profile_adobe_rgb(self):
        p = generate_icc_profile_binary("adobe_rgb")
        assert p.profile_id == "adobe_rgb"
        assert p.size > 128

    def test_generate_icc_profile_grey(self):
        p = generate_icc_profile_binary("grey_gamma22")
        assert p.profile_id == "grey_gamma22"
        assert p.profile_class == "mntr"
        assert p.size > 128

    def test_generate_icc_profile_unknown(self):
        p = generate_icc_profile_binary("nonexistent")
        assert p.data == b""
        assert p.size == 0

    def test_generate_icc_profiles_different(self):
        srgb = generate_icc_profile_binary("srgb")
        adobe = generate_icc_profile_binary("adobe_rgb")
        assert srgb.checksum != adobe.checksum

    def test_icc_profile_header_structure(self):
        p = generate_icc_profile_binary("srgb")
        import struct
        size = struct.unpack(">I", p.data[:4])[0]
        assert size == p.size
        assert p.data[36:40] == b"acsp"

    def test_embed_icc_profile_tiff(self):
        r = embed_icc_profile(b"fake_tiff", "tiff", b"fake_icc")
        assert r.success is True
        assert r.format_id == "tiff"
        assert r.method == "binary_blob"
        assert r.embedded_size > 0

    def test_embed_icc_profile_jpeg(self):
        r = embed_icc_profile(b"fake_jpeg", "jpeg", b"fake_icc")
        assert r.success is True
        assert r.method == "icc_profile_chunks"

    def test_embed_icc_profile_png(self):
        r = embed_icc_profile(b"fake_png", "png", b"fake_icc")
        assert r.success is True
        assert r.method == "compressed_profile"

    def test_embed_icc_profile_pdf(self):
        r = embed_icc_profile(b"fake_pdf", "pdf", b"fake_icc")
        assert r.success is True
        assert r.method == "stream_object"

    def test_embed_icc_profile_unsupported_format(self):
        r = embed_icc_profile(b"data", "webp", b"icc")
        assert r.success is False
        assert "Unsupported" in r.error

    def test_embed_icc_profile_empty_profile(self):
        r = embed_icc_profile(b"data", "tiff", b"")
        assert r.success is False
        assert "Empty profile" in r.error


# ═══════════════════════════════════════════════════════════════════════
#  Test Recipes
# ═══════════════════════════════════════════════════════════════════════

class TestRecipes:
    def test_list_test_recipes(self):
        recipes = list_test_recipes()
        assert len(recipes) >= 10
        ids = {r.recipe_id for r in recipes}
        assert "isp_grey_8bit" in ids
        assert "full_scan_pipeline" in ids

    def test_get_test_recipe(self):
        r = get_test_recipe("isp_grey_8bit")
        assert r is not None
        assert r.domain == "scanner_isp"
        assert len(r.steps) >= 5

    def test_get_test_recipe_unknown(self):
        assert get_test_recipe("nonexistent") is None

    def test_run_test_recipe_isp_grey(self):
        r = run_test_recipe("isp_grey_8bit")
        assert r.status == "passed"
        assert r.steps_completed == r.steps_total
        assert r.elapsed_ms >= 0

    def test_run_test_recipe_ocr_tesseract(self):
        r = run_test_recipe("ocr_tesseract_basic")
        assert r.status == "passed"

    def test_run_test_recipe_twain(self):
        r = run_test_recipe("twain_state_machine")
        assert r.status == "passed"

    def test_run_test_recipe_sane(self):
        r = run_test_recipe("sane_lifecycle")
        assert r.status == "passed"

    def test_run_test_recipe_icc_tiff(self):
        r = run_test_recipe("icc_embed_tiff")
        assert r.status == "passed"

    def test_run_test_recipe_icc_jpeg(self):
        r = run_test_recipe("icc_embed_jpeg")
        assert r.status == "passed"

    def test_run_test_recipe_icc_png(self):
        r = run_test_recipe("icc_embed_png")
        assert r.status == "passed"

    def test_run_test_recipe_full_pipeline(self):
        r = run_test_recipe("full_scan_pipeline")
        assert r.status == "passed"

    def test_run_test_recipe_unknown(self):
        r = run_test_recipe("nonexistent")
        assert r.status == "error"

    def test_run_test_recipe_details(self):
        r = run_test_recipe("isp_grey_8bit")
        assert len(r.details) == r.steps_total
        for d in r.details:
            assert d["status"] == "passed"
            assert "description" in d


# ═══════════════════════════════════════════════════════════════════════
#  SoC Compatibility
# ═══════════════════════════════════════════════════════════════════════

class TestSoCCompatibility:
    def test_list_compatible_socs(self):
        socs = list_compatible_socs()
        assert len(socs) >= 5
        ids = {s.soc_id for s in socs}
        assert "rk3566" in ids
        assert "x86_64" in ids

    def test_get_compatible_soc_rk3566(self):
        s = get_compatible_soc("rk3566")
        assert s is not None
        assert s.usb_host is True
        assert s.parallel_interface is True

    def test_get_compatible_soc_x86(self):
        s = get_compatible_soc("x86_64")
        assert s is not None
        assert s.parallel_interface is False

    def test_get_compatible_soc_unknown(self):
        assert get_compatible_soc("risc_v") is None


# ═══════════════════════════════════════════════════════════════════════
#  Artifact Definitions
# ═══════════════════════════════════════════════════════════════════════

class TestArtifactDefinitions:
    def test_list_artifact_definitions(self):
        defs = list_artifact_definitions()
        assert len(defs) >= 7
        ids = {d.artifact_id for d in defs}
        assert "isp_pipeline_config" in ids
        assert "ocr_engine_config" in ids
        assert "twain_driver_source" in ids
        assert "sane_backend_source" in ids
        assert "icc_profile_binary" in ids

    def test_get_artifact_definition(self):
        a = get_artifact_definition("twain_driver_source")
        assert a is not None
        assert "TWAIN" in a.name
        assert ".c" in a.file_pattern

    def test_get_artifact_definition_unknown(self):
        assert get_artifact_definition("nonexistent") is None


# ═══════════════════════════════════════════════════════════════════════
#  Gate Validation
# ═══════════════════════════════════════════════════════════════════════

class TestGateValidation:
    def test_validate_imaging_gate_pass(self):
        all_ids = [a.artifact_id for a in list_artifact_definitions()]
        result = validate_imaging_gate(all_ids)
        assert result.verdict == "passed"
        assert len(result.findings) == 0
        assert len(result.artifacts_missing) == 0

    def test_validate_imaging_gate_fail(self):
        result = validate_imaging_gate([])
        assert result.verdict == "failed"
        assert len(result.findings) > 0
        assert len(result.artifacts_missing) > 0

    def test_validate_imaging_gate_partial(self):
        result = validate_imaging_gate(["isp_pipeline_config", "ocr_engine_config"])
        assert result.verdict == "failed"
        assert len(result.artifacts_present) == 2
        assert len(result.artifacts_missing) > 0


# ═══════════════════════════════════════════════════════════════════════
#  Cert Registry
# ═══════════════════════════════════════════════════════════════════════

class TestCertRegistry:
    def setup_method(self):
        clear_imaging_certs()

    def test_generate_cert_artifacts(self):
        bundle = generate_cert_artifacts("scanner_isp")
        assert bundle["domain"] == "scanner_isp"
        assert bundle["total"] > 0
        assert bundle["generated_at"] != ""

    def test_get_imaging_certs(self):
        generate_cert_artifacts("all")
        certs = get_imaging_certs()
        assert len(certs) == 1

    def test_clear_imaging_certs(self):
        generate_cert_artifacts("all")
        clear_imaging_certs()
        assert len(get_imaging_certs()) == 0

    def test_multiple_cert_bundles(self):
        generate_cert_artifacts("scanner_isp")
        generate_cert_artifacts("ocr")
        certs = get_imaging_certs()
        assert len(certs) == 2


# ═══════════════════════════════════════════════════════════════════════
#  REST Endpoint Smoke Tests
# ═══════════════════════════════════════════════════════════════════════

class TestRESTEndpoints:
    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.routers.imaging_pipeline import router
        from backend import auth as _au
        app = FastAPI()
        app.dependency_overrides[_au.require_operator] = lambda: None
        app.include_router(router)
        return TestClient(app)

    def test_get_sensors(self, client):
        r = client.get("/imaging/sensors")
        assert r.status_code == 200
        assert len(r.json()) >= 2

    def test_get_sensor_by_id(self, client):
        r = client.get("/imaging/sensors/cis")
        assert r.status_code == 200
        assert r.json()["sensor_id"] == "cis"

    def test_get_sensor_not_found(self, client):
        r = client.get("/imaging/sensors/lidar")
        assert r.status_code == 404

    def test_get_color_modes(self, client):
        r = client.get("/imaging/color-modes")
        assert r.status_code == 200

    def test_get_isp_stages(self, client):
        r = client.get("/imaging/isp/stages")
        assert r.status_code == 200

    def test_get_output_formats(self, client):
        r = client.get("/imaging/output-formats")
        assert r.status_code == 200

    def test_post_isp_run(self, client):
        r = client.post("/imaging/isp/run", json={
            "sensor_type": "cis",
            "color_mode": "grey_8bit",
        })
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_get_ocr_engines(self, client):
        r = client.get("/imaging/ocr/engines")
        assert r.status_code == 200

    def test_get_ocr_engine_by_id(self, client):
        r = client.get("/imaging/ocr/engines/tesseract")
        assert r.status_code == 200

    def test_get_ocr_engine_not_found(self, client):
        r = client.get("/imaging/ocr/engines/fake")
        assert r.status_code == 404

    def test_post_ocr_run(self, client):
        r = client.post("/imaging/ocr/run", json={
            "engine_id": "tesseract",
            "language": "eng",
        })
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_get_twain_capabilities(self, client):
        r = client.get("/imaging/twain/capabilities")
        assert r.status_code == 200

    def test_get_twain_states(self, client):
        r = client.get("/imaging/twain/states")
        assert r.status_code == 200

    def test_post_twain_transition(self, client):
        r = client.post("/imaging/twain/transition", json={
            "current_state": 1,
            "target_state": 2,
        })
        assert r.status_code == 200
        assert r.json()["valid"] is True

    def test_post_twain_generate(self, client):
        r = client.post("/imaging/twain/generate", json={
            "device_name": "Test Scanner",
        })
        assert r.status_code == 200
        assert "source_code" in r.json()

    def test_get_sane_options(self, client):
        r = client.get("/imaging/sane/options")
        assert r.status_code == 200

    def test_get_sane_api_functions(self, client):
        r = client.get("/imaging/sane/api-functions")
        assert r.status_code == 200

    def test_post_sane_generate(self, client):
        r = client.post("/imaging/sane/generate", json={
            "device_name": "Test Scanner",
        })
        assert r.status_code == 200
        assert "source_code" in r.json()

    def test_get_icc_profiles(self, client):
        r = client.get("/imaging/icc/profiles")
        assert r.status_code == 200

    def test_get_icc_profile_by_id(self, client):
        r = client.get("/imaging/icc/profiles/srgb")
        assert r.status_code == 200

    def test_get_icc_profile_not_found(self, client):
        r = client.get("/imaging/icc/profiles/fake")
        assert r.status_code == 404

    def test_get_icc_classes(self, client):
        r = client.get("/imaging/icc/classes")
        assert r.status_code == 200

    def test_get_icc_embedding_formats(self, client):
        r = client.get("/imaging/icc/embedding-formats")
        assert r.status_code == 200

    def test_get_rendering_intents(self, client):
        r = client.get("/imaging/icc/rendering-intents")
        assert r.status_code == 200

    def test_post_icc_generate(self, client):
        r = client.post("/imaging/icc/generate", json={
            "profile_id": "srgb",
        })
        assert r.status_code == 200
        assert r.json()["size"] > 0

    def test_post_icc_generate_not_found(self, client):
        r = client.post("/imaging/icc/generate", json={
            "profile_id": "nonexistent",
        })
        assert r.status_code == 404

    def test_post_icc_embed(self, client):
        r = client.post("/imaging/icc/embed", json={
            "output_format": "tiff",
            "profile_id": "srgb",
        })
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_post_icc_embed_not_found(self, client):
        r = client.post("/imaging/icc/embed", json={
            "output_format": "tiff",
            "profile_id": "nonexistent",
        })
        assert r.status_code == 404

    def test_get_test_recipes(self, client):
        r = client.get("/imaging/test-recipes")
        assert r.status_code == 200

    def test_post_run_test_recipe(self, client):
        r = client.post("/imaging/test-recipes/isp_grey_8bit/run")
        assert r.status_code == 200
        assert r.json()["status"] == "passed"

    def test_post_run_test_recipe_not_found(self, client):
        r = client.post("/imaging/test-recipes/nonexistent/run")
        assert r.status_code == 404

    def test_get_socs(self, client):
        r = client.get("/imaging/socs")
        assert r.status_code == 200

    def test_get_artifacts(self, client):
        r = client.get("/imaging/artifacts")
        assert r.status_code == 200

    def test_post_validate(self, client):
        r = client.post("/imaging/validate", json={
            "artifacts": ["isp_pipeline_config"],
        })
        assert r.status_code == 200

    def test_get_certs(self, client):
        r = client.get("/imaging/certs")
        assert r.status_code == 200

    def test_post_generate_certs(self, client):
        r = client.post("/imaging/certs/generate", json={
            "domain": "all",
        })
        assert r.status_code == 200
        assert r.json()["total"] > 0
