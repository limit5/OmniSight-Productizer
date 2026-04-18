"""C20 — L4-CORE-20 Print pipeline tests (#241).

Covers:
  - IPP/CUPS: operations, attributes, backends, job states, job lifecycle
    (submit, cancel, hold, release), invalid format rejection
  - PDL Interpreters: language listing, PCL generation (single/multi-page,
    duplex), PostScript generation (DSC compliance, duplex), Ghostscript
    devices, PDF→raster rendering, raster formats
  - Color Management: paper profiles, ink sets, rendering intents, color
    spaces, profile selection (valid + invalid), ICC binary generation
  - Print Queue: policies, priority levels, spooler config, lifecycle states,
    enqueue, FIFO/priority/shortest-first ordering, hold/release/cancel,
    oversize rejection, error/requeue, advance to completion
  - Test recipes: listing, execution, unknown recipe
  - SoC compatibility
  - Artifact definitions
  - Gate validation (pass + partial + fail)
  - Cert generation + registry
  - REST endpoint smoke tests
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.print_pipeline import (
    DuplexMode,
    GateVerdict,
    IPPJobState,
    InkChannel,
    MediaSize,
    PCLStream,
    PCLVersion,
    PDFVersion,
    PDLLanguage,
    PSLevel,
    PostScriptDocument,
    PrintColorSpace,
    PrintDomain,
    PrintQuality,
    PrintProfileSelection,
    PrintRenderingIntent,
    PriorityLevel,
    QueueJob,
    QueuePolicy,
    RasterFormat,
    RasterOutput,
    SpoolerConfig,
    SpoolerJobState,
    TestRecipeResult,
    TestStatus,
    advance_queue_job_to_completion,
    cancel_ipp_job,
    cancel_queue_job,
    clear_print_certs,
    enqueue_print_job,
    error_queue_job,
    generate_cert_artifacts,
    generate_pcl,
    generate_postscript,
    generate_print_icc_binary,
    get_artifact_definition,
    get_color_space,
    get_compatible_soc,
    get_cups_backend,
    get_ghostscript_device,
    get_ink_set,
    get_ipp_attribute,
    get_ipp_job,
    get_ipp_job_state,
    get_ipp_operation,
    get_job_lifecycle_state,
    get_paper_profile,
    get_pcl_command,
    get_pdl_language,
    get_print_certs,
    get_print_rendering_intent,
    get_priority_level,
    get_ps_operator,
    get_queue_job,
    get_queue_policy,
    get_raster_format,
    get_spooler_config,
    get_test_recipe,
    hold_ipp_job,
    hold_queue_job,
    list_artifact_definitions,
    list_color_spaces,
    list_compatible_socs,
    list_cups_backends,
    list_ghostscript_devices,
    list_ink_sets,
    list_ipp_attributes,
    list_ipp_job_states,
    list_ipp_jobs,
    list_ipp_operations,
    list_job_lifecycle_states,
    list_paper_profiles,
    list_pcl_commands,
    list_pdl_languages,
    list_print_rendering_intents,
    list_priority_levels,
    list_ps_operators,
    list_queue_jobs,
    list_queue_policies,
    list_raster_formats,
    list_test_recipes,
    release_ipp_job,
    release_queue_job,
    render_pdf_to_raster,
    requeue_error_job,
    run_test_recipe,
    select_print_profile,
    submit_ipp_job,
    validate_print_gate,
    _reset_config,
    _reset_ipp_jobs,
    _reset_queue,
)


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_state():
    _reset_config()
    _reset_ipp_jobs()
    _reset_queue()
    clear_print_certs()
    yield
    _reset_config()
    _reset_ipp_jobs()
    _reset_queue()
    clear_print_certs()


# ══════════════════════════════════════════════════════════════════════
#  IPP / CUPS
# ══════════════════════════════════════════════════════════════════════

class TestIPPOperations:
    def test_list_operations(self):
        ops = list_ipp_operations()
        assert len(ops) >= 7
        ids = [o.id for o in ops]
        assert "print_job" in ids
        assert "get_printer_attributes" in ids

    def test_get_operation(self):
        op = get_ipp_operation("print_job")
        assert op is not None
        assert op.name == "Print-Job"
        assert op.code == 0x0002
        assert op.required is True

    def test_get_operation_not_found(self):
        assert get_ipp_operation("nonexistent") is None

    def test_required_operations(self):
        ops = list_ipp_operations()
        required = [o for o in ops if o.required]
        assert len(required) >= 7


class TestIPPAttributes:
    def test_list_attributes(self):
        attrs = list_ipp_attributes()
        assert len(attrs) >= 8
        ids = [a.id for a in attrs]
        assert "printer_uri" in ids
        assert "document_format" in ids

    def test_get_attribute(self):
        attr = get_ipp_attribute("document_format")
        assert attr is not None
        assert attr.required is True
        assert "application/pdf" in attr.values

    def test_get_attribute_not_found(self):
        assert get_ipp_attribute("nonexistent") is None

    def test_media_sizes(self):
        attr = get_ipp_attribute("media")
        assert attr is not None
        assert "iso_a4_210x297mm" in attr.values
        assert "na_letter_8.5x11in" in attr.values


class TestCUPSBackends:
    def test_list_backends(self):
        backends = list_cups_backends()
        assert len(backends) >= 5
        ids = [b.id for b in backends]
        assert "usb" in ids
        assert "ipp" in ids
        assert "socket" in ids

    def test_get_backend(self):
        b = get_cups_backend("ipp")
        assert b is not None
        assert b.uri_scheme == "ipp://"

    def test_get_backend_not_found(self):
        assert get_cups_backend("nonexistent") is None


class TestIPPJobStates:
    def test_list_job_states(self):
        states = list_ipp_job_states()
        assert len(states) >= 7
        ids = [s.id for s in states]
        assert "pending" in ids
        assert "completed" in ids

    def test_get_job_state(self):
        s = get_ipp_job_state("completed")
        assert s is not None
        assert s.code == 9


class TestIPPJobLifecycle:
    def test_submit_job(self):
        job = submit_ipp_job(
            printer_uri="ipp://test/print",
            document_format="application/pdf",
        )
        assert job.job_id.startswith("ipp-job-")
        assert job.state == IPPJobState.completed.value
        assert IPPJobState.pending.value in job.state_history
        assert IPPJobState.processing.value in job.state_history
        assert IPPJobState.completed.value in job.state_history

    def test_submit_job_invalid_format(self):
        with pytest.raises(ValueError, match="Unsupported document format"):
            submit_ipp_job(
                printer_uri="ipp://test/print",
                document_format="application/invalid",
            )

    def test_list_jobs(self):
        submit_ipp_job("ipp://test/print", "application/pdf")
        submit_ipp_job("ipp://test/print", "application/pdf")
        jobs = list_ipp_jobs()
        assert len(jobs) == 2

    def test_get_job(self):
        job = submit_ipp_job("ipp://test/print", "application/pdf")
        found = get_ipp_job(job.job_id)
        assert found is not None
        assert found.job_id == job.job_id

    def test_get_job_not_found(self):
        assert get_ipp_job("nonexistent") is None

    def test_cancel_completed_job_fails(self):
        job = submit_ipp_job("ipp://test/print", "application/pdf")
        with pytest.raises(ValueError, match="Cannot cancel"):
            cancel_ipp_job(job.job_id)

    def test_cancel_nonexistent_job(self):
        assert cancel_ipp_job("nonexistent") is None

    def test_hold_release_job(self):
        job = submit_ipp_job("ipp://test/print", "application/pdf")
        # Job auto-completes, so we need a fresh pending job
        _reset_ipp_jobs()
        # Create a job that stays pending (mock _advance)
        with patch("backend.print_pipeline._advance_ipp_job"):
            job = submit_ipp_job("ipp://test/print", "application/pdf")
        assert job.state == IPPJobState.pending.value

        held = hold_ipp_job(job.job_id)
        assert held.state == IPPJobState.pending_held.value

        released = release_ipp_job(job.job_id)
        assert released.state == IPPJobState.completed.value

    def test_hold_nonpending_fails(self):
        job = submit_ipp_job("ipp://test/print", "application/pdf")
        with pytest.raises(ValueError, match="Can only hold pending"):
            hold_ipp_job(job.job_id)

    def test_release_nonheld_fails(self):
        job = submit_ipp_job("ipp://test/print", "application/pdf")
        with pytest.raises(ValueError, match="Can only release held"):
            release_ipp_job(job.job_id)


# ══════════════════════════════════════════════════════════════════════
#  PDL Interpreters
# ══════════════════════════════════════════════════════════════════════

class TestPDLLanguages:
    def test_list_languages(self):
        langs = list_pdl_languages()
        assert len(langs) == 3
        ids = [l.id for l in langs]
        assert "pcl" in ids
        assert "postscript" in ids
        assert "pdf" in ids

    def test_get_language_pcl(self):
        lang = get_pdl_language("pcl")
        assert lang is not None
        assert lang.mime_type == "application/vnd.hp-PCL"
        assert "pcl5e" in lang.versions

    def test_get_language_postscript(self):
        lang = get_pdl_language("postscript")
        assert lang is not None
        assert lang.mime_type == "application/postscript"

    def test_get_language_pdf(self):
        lang = get_pdl_language("pdf")
        assert lang is not None
        assert lang.mime_type == "application/pdf"

    def test_get_language_not_found(self):
        assert get_pdl_language("nonexistent") is None


class TestPCLCommands:
    def test_list_commands(self):
        cmds = list_pcl_commands()
        assert len(cmds) >= 10
        ids = [c.id for c in cmds]
        assert "reset" in ids
        assert "raster_start" in ids
        assert "form_feed" in ids

    def test_get_command(self):
        cmd = get_pcl_command("reset")
        assert cmd is not None
        assert cmd.sequence == "\\x1bE"

    def test_get_command_not_found(self):
        assert get_pcl_command("nonexistent") is None


class TestPSOperators:
    def test_list_operators(self):
        ops = list_ps_operators()
        assert len(ops) >= 11
        ids = [o.id for o in ops]
        assert "showpage" in ids
        assert "gsave" in ids
        assert "stroke" in ids

    def test_get_operator(self):
        op = get_ps_operator("showpage")
        assert op is not None

    def test_get_operator_not_found(self):
        assert get_ps_operator("nonexistent") is None


class TestGhostscriptDevices:
    def test_list_devices(self):
        devs = list_ghostscript_devices()
        assert len(devs) >= 11
        ids = [d.id for d in devs]
        assert "pwgraster" in ids
        assert "pxlcolor" in ids
        assert "tiff24nc" in ids
        assert "urf" in ids

    def test_get_device(self):
        d = get_ghostscript_device("pwgraster")
        assert d is not None
        assert "PWG Raster" in d.description

    def test_get_device_not_found(self):
        assert get_ghostscript_device("nonexistent") is None


class TestRasterFormats:
    def test_list_formats(self):
        fmts = list_raster_formats()
        assert len(fmts) >= 3
        ids = [f.id for f in fmts]
        assert "pwg_raster" in ids
        assert "urf" in ids
        assert "cups_raster" in ids

    def test_get_format(self):
        f = get_raster_format("pwg_raster")
        assert f is not None
        assert f.header_size == 1796
        assert "packbits" in f.compression


class TestPCLGeneration:
    def test_generate_default(self):
        pcl = generate_pcl()
        assert isinstance(pcl, PCLStream)
        assert pcl.page_count == 1
        assert pcl.resolution_dpi == 300
        assert pcl.page_size == "a4"
        assert pcl.duplex == "simplex"
        assert len(pcl.data) > 0
        assert len(pcl.checksum) == 64

    def test_generate_multipage(self):
        pcl = generate_pcl(pages=3)
        assert pcl.page_count == 3
        # Should contain 3 form feeds
        assert pcl.data.count(b"\x0c") == 3

    def test_generate_duplex_long(self):
        pcl = generate_pcl(duplex="duplex_long")
        assert pcl.duplex == "duplex_long"
        # Duplex code 1 (long edge)
        assert b"\x1b&l1S" in pcl.data

    def test_generate_duplex_short(self):
        pcl = generate_pcl(duplex="duplex_short")
        assert b"\x1b&l2S" in pcl.data

    def test_generate_letter_size(self):
        pcl = generate_pcl(page_size="letter")
        assert pcl.page_size == "letter"
        assert b"\x1b&l2A" in pcl.data

    def test_generate_a4_size(self):
        pcl = generate_pcl(page_size="a4")
        assert b"\x1b&l26A" in pcl.data

    def test_generate_high_resolution(self):
        pcl = generate_pcl(resolution_dpi=600)
        assert pcl.resolution_dpi == 600
        assert b"\x1b*t600R" in pcl.data

    def test_generate_with_raster_data(self):
        data = bytes([0x55] * 1000)
        pcl = generate_pcl(raster_data=data)
        assert len(pcl.data) > 0

    def test_pcl_has_reset_commands(self):
        pcl = generate_pcl()
        # Should start and end with reset
        assert pcl.data[:2] == b"\x1bE"
        assert pcl.data[-2:] == b"\x1bE"

    def test_pcl_has_raster_markers(self):
        pcl = generate_pcl()
        assert b"\x1b*r1A" in pcl.data  # raster start
        assert b"\x1b*rB" in pcl.data   # raster end


class TestPostScriptGeneration:
    def test_generate_default(self):
        ps = generate_postscript()
        assert isinstance(ps, PostScriptDocument)
        assert ps.page_count == 1
        assert ps.dsc_compliant is True
        assert ps.level == "level2"
        assert len(ps.data) > 0
        assert len(ps.checksum) == 64

    def test_dsc_comments(self):
        ps = generate_postscript()
        assert "%!PS-Adobe-3.0" in ps.data
        assert "%%Pages: 1" in ps.data
        assert "%%EOF" in ps.data
        assert "%%BoundingBox:" in ps.data

    def test_multipage(self):
        ps = generate_postscript(pages=3)
        assert ps.page_count == 3
        assert "%%Pages: 3" in ps.data
        assert ps.data.count("showpage") == 3

    def test_a4_bounding_box(self):
        ps = generate_postscript(page_size="a4")
        assert ps.bounding_box == (0, 0, 595, 842)
        assert "%%BoundingBox: 0 0 595 842" in ps.data

    def test_letter_bounding_box(self):
        ps = generate_postscript(page_size="letter")
        assert ps.bounding_box == (0, 0, 612, 792)

    def test_duplex_long(self):
        ps = generate_postscript(duplex="duplex_long")
        assert "/Duplex true" in ps.data
        assert "/Tumble false" in ps.data

    def test_duplex_short(self):
        ps = generate_postscript(duplex="duplex_short")
        assert "/Duplex true" in ps.data
        assert "/Tumble true" in ps.data

    def test_simplex(self):
        ps = generate_postscript(duplex="simplex")
        assert "/Duplex false" in ps.data

    def test_level3(self):
        ps = generate_postscript(level="level3")
        assert ps.level == "level3"
        assert "%%LanguageLevel: 3" in ps.data

    def test_gsave_grestore(self):
        ps = generate_postscript()
        assert "gsave" in ps.data
        assert "grestore" in ps.data

    def test_colorimage(self):
        ps = generate_postscript()
        assert "colorimage" in ps.data
        assert "/DeviceRGB setcolorspace" in ps.data


class TestPDFToRaster:
    def test_render_default(self):
        raster = render_pdf_to_raster()
        assert isinstance(raster, RasterOutput)
        assert raster.dpi == 300
        assert raster.color_space == "RGB"
        assert raster.bits_per_pixel == 24
        assert raster.page_count == 1
        assert raster.device == "pwgraster"
        assert len(raster.data) > 0

    def test_render_greyscale(self):
        raster = render_pdf_to_raster(color_bits=8)
        assert raster.color_space == "Grayscale"
        assert raster.bits_per_pixel == 8

    def test_render_various_devices(self):
        for dev_id in ["pwgraster", "urf", "tiff24nc", "png16m"]:
            raster = render_pdf_to_raster(device=dev_id)
            assert raster.device == dev_id

    def test_render_unknown_device(self):
        with pytest.raises(ValueError, match="Unknown Ghostscript device"):
            render_pdf_to_raster(device="nonexistent")

    def test_render_letter_size(self):
        raster = render_pdf_to_raster(page_size="letter")
        assert raster.width == int(8.5 * 300)

    def test_render_high_dpi(self):
        raster = render_pdf_to_raster(dpi=600)
        assert raster.dpi == 600
        assert raster.width == int(8.27 * 600)

    def test_render_with_pdf_data(self):
        fake_pdf = b"%PDF-1.4 /Type /Page endobj /Type /Page endobj"
        raster = render_pdf_to_raster(pdf_data=fake_pdf)
        assert raster.page_count == 2


# ══════════════════════════════════════════════════════════════════════
#  Color Management
# ══════════════════════════════════════════════════════════════════════

class TestPaperProfiles:
    def test_list_profiles(self):
        slots = list_paper_profiles()
        assert len(slots) >= 5
        ids = [s.id for s in slots]
        assert "paper_plain" in ids
        assert "paper_glossy" in ids
        assert "paper_matte" in ids

    def test_get_profile(self):
        p = get_paper_profile("paper_plain")
        assert p is not None
        assert p.paper_type == "Plain Paper"
        assert len(p.profiles) >= 2

    def test_get_profile_not_found(self):
        assert get_paper_profile("nonexistent") is None


class TestInkSets:
    def test_list_ink_sets(self):
        inks = list_ink_sets()
        assert len(inks) >= 4
        ids = [i.id for i in inks]
        assert "cmyk_standard" in ids
        assert "cmyk_photo" in ids
        assert "cmyk_6color" in ids
        assert "mono_black" in ids

    def test_get_ink_set(self):
        ink = get_ink_set("cmyk_standard")
        assert ink is not None
        assert ink.channel_count == 4
        assert "cyan" in ink.channels
        assert "black" in ink.channels

    def test_6color_ink(self):
        ink = get_ink_set("cmyk_6color")
        assert ink is not None
        assert ink.channel_count == 6
        assert "light_cyan" in ink.channels

    def test_mono_ink(self):
        ink = get_ink_set("mono_black")
        assert ink is not None
        assert ink.channel_count == 1

    def test_get_ink_not_found(self):
        assert get_ink_set("nonexistent") is None


class TestRenderingIntents:
    def test_list_intents(self):
        intents = list_print_rendering_intents()
        assert len(intents) == 4
        ids = [i.id for i in intents]
        assert "perceptual" in ids
        assert "saturation" in ids

    def test_get_intent(self):
        i = get_print_rendering_intent("perceptual")
        assert i is not None
        assert i.code == 0


class TestColorSpaces:
    def test_list_spaces(self):
        spaces = list_color_spaces()
        assert len(spaces) >= 4
        ids = [s.id for s in spaces]
        assert "srgb" in ids
        assert "cmyk" in ids

    def test_get_space(self):
        s = get_color_space("srgb")
        assert s is not None
        assert s.type == "input"


class TestProfileSelection:
    def test_select_plain_cmyk(self):
        sel = select_print_profile("paper_plain", "cmyk_standard")
        assert isinstance(sel, PrintProfileSelection)
        assert sel.icc_file == "plain_cmyk.icc"
        assert sel.rendering_intent == "relative_colorimetric"
        assert sel.paper_type == "Plain Paper"

    def test_select_glossy_photo(self):
        sel = select_print_profile("paper_glossy", "cmyk_photo")
        assert sel.icc_file == "glossy_cmyk_photo.icc"
        assert sel.rendering_intent == "perceptual"

    def test_select_glossy_6color(self):
        sel = select_print_profile("paper_glossy", "cmyk_6color")
        assert sel.icc_file == "glossy_6color.icc"

    def test_select_envelope_mono(self):
        sel = select_print_profile("paper_envelope", "mono_black")
        assert sel.icc_file == "envelope_mono.icc"

    def test_select_label_cmyk(self):
        sel = select_print_profile("paper_label", "cmyk_standard")
        assert sel.rendering_intent == "saturation"

    def test_select_invalid_paper(self):
        with pytest.raises(ValueError, match="Unknown paper profile"):
            select_print_profile("nonexistent", "cmyk_standard")

    def test_select_invalid_ink(self):
        with pytest.raises(ValueError, match="Unknown ink set"):
            select_print_profile("paper_plain", "nonexistent")

    def test_select_mismatched_combo(self):
        with pytest.raises(ValueError, match="No profile for"):
            select_print_profile("paper_envelope", "cmyk_6color")


class TestICCBinaryGeneration:
    def test_generate_plain_cmyk(self):
        data = generate_print_icc_binary("paper_plain", "cmyk_standard")
        assert isinstance(data, bytes)
        assert len(data) > 128
        # Check ICC signature
        assert data[36:40] == b"acsp"
        # Check device class (prtr)
        assert data[12:16] == b"prtr"
        # Check color space (CMYK)
        assert data[16:20] == b"CMYK"

    def test_generate_glossy_photo(self):
        data = generate_print_icc_binary("paper_glossy", "cmyk_photo")
        assert data[36:40] == b"acsp"

    def test_generate_different_combos(self):
        combos = [
            ("paper_plain", "cmyk_standard"),
            ("paper_plain", "mono_black"),
            ("paper_glossy", "cmyk_photo"),
            ("paper_matte", "cmyk_standard"),
        ]
        checksums = set()
        for paper, ink in combos:
            data = generate_print_icc_binary(paper, ink)
            import hashlib
            checksums.add(hashlib.sha256(data).hexdigest())
        assert len(checksums) == len(combos)


# ══════════════════════════════════════════════════════════════════════
#  Print Queue / Spooler
# ══════════════════════════════════════════════════════════════════════

class TestQueuePolicies:
    def test_list_policies(self):
        policies = list_queue_policies()
        assert len(policies) >= 3
        ids = [p.id for p in policies]
        assert "fifo" in ids
        assert "priority" in ids
        assert "shortest_first" in ids

    def test_get_policy(self):
        p = get_queue_policy("fifo")
        assert p is not None
        assert p.default is True


class TestPriorityLevels:
    def test_list_levels(self):
        levels = list_priority_levels()
        assert len(levels) == 4

    def test_get_level(self):
        l = get_priority_level("critical")
        assert l is not None
        assert l.value == 100


class TestSpoolerConfig:
    def test_get_config(self):
        cfg = get_spooler_config()
        assert isinstance(cfg, SpoolerConfig)
        assert cfg.max_concurrent_jobs == 4
        assert cfg.max_queue_depth == 1000
        assert cfg.max_job_size_mb == 500
        assert cfg.compression == "zlib"


class TestJobLifecycle:
    def test_list_states(self):
        states = list_job_lifecycle_states()
        assert len(states) >= 11
        state_names = [s.state for s in states]
        assert "submitted" in state_names
        assert "completed" in state_names
        assert "error" in state_names

    def test_get_state(self):
        s = get_job_lifecycle_state("queued")
        assert s is not None
        assert "spooling" in s.transitions
        assert "held" in s.transitions
        assert "canceled" in s.transitions


class TestQueueEnqueue:
    def test_enqueue_basic(self):
        job = enqueue_print_job(
            document_name="test.pdf",
            printer_uri="ipp://test/print",
        )
        assert isinstance(job, QueueJob)
        assert job.state == SpoolerJobState.queued.value
        assert "submitted" in job.state_history
        assert "queued" in job.state_history

    def test_enqueue_with_priority(self):
        job = enqueue_print_job("test.pdf", "ipp://test/print", priority=75)
        assert job.priority == 75

    def test_enqueue_oversize_rejected(self):
        job = enqueue_print_job(
            document_name="huge.pdf",
            printer_uri="ipp://test/print",
            size_bytes=600 * 1024 * 1024,  # 600 MB > 500 MB limit
        )
        assert job.state == SpoolerJobState.rejected.value
        assert "exceeds max" in job.error_message


class TestQueueOrdering:
    def test_fifo_ordering(self):
        import time
        j1 = enqueue_print_job("a.pdf", "ipp://test/print")
        time.sleep(0.01)
        j2 = enqueue_print_job("b.pdf", "ipp://test/print")
        time.sleep(0.01)
        j3 = enqueue_print_job("c.pdf", "ipp://test/print")
        jobs = list_queue_jobs(policy="fifo")
        assert jobs[0].job_id == j1.job_id
        assert jobs[1].job_id == j2.job_id
        assert jobs[2].job_id == j3.job_id

    def test_priority_ordering(self):
        j_low = enqueue_print_job("low.pdf", "ipp://test/print", priority=25)
        j_high = enqueue_print_job("high.pdf", "ipp://test/print", priority=75)
        j_normal = enqueue_print_job("normal.pdf", "ipp://test/print", priority=50)
        jobs = list_queue_jobs(policy="priority")
        assert jobs[0].job_id == j_high.job_id
        assert jobs[1].job_id == j_normal.job_id
        assert jobs[2].job_id == j_low.job_id

    def test_shortest_first_ordering(self):
        j_big = enqueue_print_job("big.pdf", "ipp://test/print", size_bytes=10000)
        j_small = enqueue_print_job("small.pdf", "ipp://test/print", size_bytes=100)
        j_med = enqueue_print_job("med.pdf", "ipp://test/print", size_bytes=5000)
        jobs = list_queue_jobs(policy="shortest_first")
        assert jobs[0].job_id == j_small.job_id
        assert jobs[1].job_id == j_med.job_id
        assert jobs[2].job_id == j_big.job_id


class TestQueueJobOperations:
    def test_hold_and_release(self):
        job = enqueue_print_job("test.pdf", "ipp://test/print")
        held = hold_queue_job(job.job_id)
        assert held.state == SpoolerJobState.held.value
        released = release_queue_job(job.job_id)
        assert released.state == SpoolerJobState.queued.value

    def test_cancel_job(self):
        job = enqueue_print_job("test.pdf", "ipp://test/print")
        canceled = cancel_queue_job(job.job_id)
        assert canceled.state == SpoolerJobState.canceled.value

    def test_cancel_completed_fails(self):
        job = enqueue_print_job("test.pdf", "ipp://test/print")
        advance_queue_job_to_completion(job.job_id)
        with pytest.raises(ValueError, match="Cannot cancel"):
            cancel_queue_job(job.job_id)

    def test_advance_to_completion(self):
        job = enqueue_print_job("test.pdf", "ipp://test/print")
        completed = advance_queue_job_to_completion(job.job_id)
        assert completed.state == SpoolerJobState.completed.value
        expected_states = ["submitted", "queued", "spooling", "rendering", "sending", "printing", "completed"]
        for s in expected_states:
            assert s in completed.state_history

    def test_error_and_requeue(self):
        job = enqueue_print_job("test.pdf", "ipp://test/print")
        # Advance to spooling first
        from backend.print_pipeline import _transition_queue_job
        _transition_queue_job(job, "spooling")
        errored = error_queue_job(job.job_id, "Paper jam")
        assert errored.state == SpoolerJobState.error.value
        assert errored.error_message == "Paper jam"
        requeued = requeue_error_job(job.job_id)
        assert requeued.state == SpoolerJobState.queued.value
        assert requeued.error_message == ""

    def test_requeue_non_error_fails(self):
        job = enqueue_print_job("test.pdf", "ipp://test/print")
        with pytest.raises(ValueError, match="Can only requeue error"):
            requeue_error_job(job.job_id)

    def test_get_queue_job(self):
        job = enqueue_print_job("test.pdf", "ipp://test/print")
        found = get_queue_job(job.job_id)
        assert found is not None
        assert found.job_id == job.job_id

    def test_get_queue_job_not_found(self):
        assert get_queue_job("nonexistent") is None

    def test_hold_nonexistent(self):
        assert hold_queue_job("nonexistent") is None

    def test_release_nonexistent(self):
        assert release_queue_job("nonexistent") is None

    def test_cancel_nonexistent(self):
        assert cancel_queue_job("nonexistent") is None


# ══════════════════════════════════════════════════════════════════════
#  Test Recipes
# ══════════════════════════════════════════════════════════════════════

class TestTestRecipes:
    def test_list_recipes(self):
        recipes = list_test_recipes()
        assert len(recipes) >= 10
        ids = [r.id for r in recipes]
        assert "print_pdf_raster_roundtrip" in ids
        assert "print_ipp_job_lifecycle" in ids
        assert "print_color_profile_match" in ids
        assert "print_queue_ordering" in ids

    def test_get_recipe(self):
        r = get_test_recipe("print_pdf_raster_roundtrip")
        assert r is not None
        assert r.domain == "integration"
        assert len(r.steps) >= 7

    def test_get_recipe_not_found(self):
        assert get_test_recipe("nonexistent") is None

    def test_run_recipe(self):
        result = run_test_recipe("print_pdf_raster_roundtrip")
        assert isinstance(result, TestRecipeResult)
        assert result.recipe_id == "print_pdf_raster_roundtrip"
        assert result.status == TestStatus.passed.value
        assert result.steps_passed == result.steps_total
        assert result.duration_ms >= 0

    def test_run_all_recipes(self):
        recipes = list_test_recipes()
        for recipe in recipes:
            result = run_test_recipe(recipe.id)
            assert result.status == TestStatus.passed.value, f"Recipe {recipe.id} failed"

    def test_run_unknown_recipe(self):
        with pytest.raises(ValueError, match="Unknown test recipe"):
            run_test_recipe("nonexistent")


# ══════════════════════════════════════════════════════════════════════
#  SoC Compatibility
# ══════════════════════════════════════════════════════════════════════

class TestSoCCompatibility:
    def test_list_socs(self):
        socs = list_compatible_socs()
        assert len(socs) >= 5
        ids = [s.id for s in socs]
        assert "x86_64" in ids
        assert "rk3566" in ids

    def test_get_soc(self):
        s = get_compatible_soc("x86_64")
        assert s is not None
        assert "CUPS" in s.notes

    def test_get_soc_not_found(self):
        assert get_compatible_soc("nonexistent") is None


# ══════════════════════════════════════════════════════════════════════
#  Artifact Definitions
# ══════════════════════════════════════════════════════════════════════

class TestArtifactDefinitions:
    def test_list_artifacts(self):
        arts = list_artifact_definitions()
        assert len(arts) >= 7
        ids = [a.id for a in arts]
        assert "ipp_backend_config" in ids
        assert "pcl_output_stream" in ids
        assert "icc_print_profile" in ids

    def test_get_artifact(self):
        a = get_artifact_definition("cups_backend_module")
        assert a is not None
        assert "printing/" in a.pattern

    def test_get_artifact_not_found(self):
        assert get_artifact_definition("nonexistent") is None


# ══════════════════════════════════════════════════════════════════════
#  Gate Validation
# ══════════════════════════════════════════════════════════════════════

class TestGateValidation:
    def test_gate_pass(self):
        all_arts = [
            "ipp_backend_config", "cups_backend_module",
            "pcl_output_stream", "postscript_output", "gs_render_output",
            "icc_print_profile",
            "print_test_report",
        ]
        result = validate_print_gate(all_arts)
        assert result.verdict == GateVerdict.pass_.value
        assert result.domains_passed == result.domains_checked

    def test_gate_fail(self):
        result = validate_print_gate([])
        assert result.verdict == GateVerdict.fail.value
        assert result.domains_passed == 0

    def test_gate_partial(self):
        result = validate_print_gate(["ipp_backend_config", "cups_backend_module"])
        assert result.verdict == GateVerdict.partial.value
        assert result.domains_passed >= 1

    def test_gate_specific_domains(self):
        result = validate_print_gate(
            ["ipp_backend_config", "cups_backend_module"],
            required_domains=["ipp_cups"],
        )
        assert result.verdict == GateVerdict.pass_.value
        assert result.domains_checked == 1

    def test_gate_findings(self):
        result = validate_print_gate(["ipp_backend_config"])
        assert len(result.findings) > 0
        statuses = [f["status"] for f in result.findings]
        assert "present" in statuses
        assert "missing" in statuses


# ══════════════════════════════════════════════════════════════════════
#  Cert Registry
# ══════════════════════════════════════════════════════════════════════

class TestCertRegistry:
    def test_generate_certs_all(self):
        bundle = generate_cert_artifacts("all")
        assert bundle["domain"] == "all"
        assert bundle["total_artifacts"] >= 7
        assert len(get_print_certs()) == 1

    def test_generate_certs_single_domain(self):
        bundle = generate_cert_artifacts("ipp_cups")
        assert bundle["domain"] == "ipp_cups"
        assert "ipp_cups" in bundle["artifacts"]

    def test_clear_certs(self):
        generate_cert_artifacts("all")
        assert len(get_print_certs()) > 0
        count = clear_print_certs()
        assert count > 0
        assert len(get_print_certs()) == 0


# ══════════════════════════════════════════════════════════════════════
#  Round-trip Integration: PDF → Raster → PDL → Verify
# ══════════════════════════════════════════════════════════════════════

class TestRoundTrip:
    def test_pdf_raster_pcl_roundtrip(self):
        raster = render_pdf_to_raster(dpi=300, page_size="a4")
        assert raster.page_count >= 1
        assert len(raster.data) > 0

        pcl = generate_pcl(
            raster_data=raster.data,
            page_size="a4",
            resolution_dpi=300,
            pages=raster.page_count,
        )
        assert pcl.page_count == raster.page_count
        assert len(pcl.data) > 0
        assert pcl.data[:2] == b"\x1bE"
        assert b"\x1b*r1A" in pcl.data
        assert b"\x1b*rB" in pcl.data

    def test_pdf_raster_ps_roundtrip(self):
        raster = render_pdf_to_raster(dpi=300, page_size="a4")
        ps = generate_postscript(
            raster_data=raster.data,
            page_size="a4",
            resolution_dpi=300,
            pages=raster.page_count,
        )
        assert ps.page_count == raster.page_count
        assert ps.dsc_compliant is True
        assert "%!PS-Adobe-3.0" in ps.data
        assert "%%EOF" in ps.data
        assert "showpage" in ps.data

    def test_pdf_raster_pcl_multipage(self):
        fake_pdf = b"%PDF-1.4 /Type /Page one /Type /Page two /Type /Page three"
        raster = render_pdf_to_raster(pdf_data=fake_pdf, dpi=300)
        assert raster.page_count == 3

        pcl = generate_pcl(
            raster_data=raster.data,
            pages=raster.page_count,
        )
        assert pcl.page_count == 3

    def test_full_pipeline_ipp_to_output(self):
        job = submit_ipp_job("ipp://test/print", "application/pdf")
        assert job.state == IPPJobState.completed.value

        raster = render_pdf_to_raster()
        pcl = generate_pcl(raster_data=raster.data)
        assert len(pcl.data) > 0

        sel = select_print_profile("paper_plain", "cmyk_standard")
        assert sel.icc_file == "plain_cmyk.icc"

        q_job = enqueue_print_job("report.pdf", "ipp://test/print")
        completed = advance_queue_job_to_completion(q_job.job_id)
        assert completed.state == SpoolerJobState.completed.value


# ══════════════════════════════════════════════════════════════════════
#  Enum value coverage
# ══════════════════════════════════════════════════════════════════════

class TestEnumCoverage:
    def test_print_domain_values(self):
        assert len(PrintDomain) == 5

    def test_pdl_language_values(self):
        assert len(PDLLanguage) == 3

    def test_pcl_version_values(self):
        assert len(PCLVersion) == 3

    def test_ps_level_values(self):
        assert len(PSLevel) == 3

    def test_pdf_version_values(self):
        assert len(PDFVersion) == 3

    def test_raster_format_values(self):
        assert len(RasterFormat) == 3

    def test_ink_channel_values(self):
        assert len(InkChannel) == 6

    def test_rendering_intent_values(self):
        assert len(PrintRenderingIntent) == 4

    def test_color_space_values(self):
        assert len(PrintColorSpace) == 4

    def test_queue_policy_values(self):
        assert len(QueuePolicy) == 3

    def test_priority_level_values(self):
        assert len(PriorityLevel) == 4

    def test_ipp_job_state_values(self):
        assert len(IPPJobState) == 7

    def test_spooler_job_state_values(self):
        assert len(SpoolerJobState) == 11

    def test_print_quality_values(self):
        assert len(PrintQuality) == 3

    def test_media_size_values(self):
        assert len(MediaSize) == 5

    def test_duplex_mode_values(self):
        assert len(DuplexMode) == 3

    def test_test_status_values(self):
        assert len(TestStatus) == 4

    def test_gate_verdict_values(self):
        assert len(GateVerdict) == 3


# ══════════════════════════════════════════════════════════════════════
#  REST endpoint smoke tests
# ══════════════════════════════════════════════════════════════════════

class TestRESTEndpoints:
    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.routers.print_pipeline import router
        from backend import auth as _au
        app = FastAPI()
        app.dependency_overrides[_au.require_operator] = lambda: None
        app.include_router(router)
        return TestClient(app)

    def test_get_ipp_operations(self, client):
        resp = client.get("/printing/ipp/operations")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 7

    def test_get_cups_backends(self, client):
        resp = client.get("/printing/cups/backends")
        assert resp.status_code == 200
        assert len(resp.json()) >= 5

    def test_get_pdl_languages(self, client):
        resp = client.get("/printing/pdl/languages")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_get_pcl_commands(self, client):
        resp = client.get("/printing/pdl/pcl/commands")
        assert resp.status_code == 200
        assert len(resp.json()) >= 10

    def test_get_gs_devices(self, client):
        resp = client.get("/printing/pdl/ghostscript/devices")
        assert resp.status_code == 200
        assert len(resp.json()) >= 11

    def test_get_paper_profiles(self, client):
        resp = client.get("/printing/color/papers")
        assert resp.status_code == 200
        assert len(resp.json()) >= 5

    def test_get_ink_sets(self, client):
        resp = client.get("/printing/color/inks")
        assert resp.status_code == 200
        assert len(resp.json()) >= 4

    def test_get_queue_policies(self, client):
        resp = client.get("/printing/queue/policies")
        assert resp.status_code == 200
        assert len(resp.json()) >= 3

    def test_get_spooler_config(self, client):
        resp = client.get("/printing/queue/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["max_concurrent_jobs"] == 4

    def test_get_test_recipes(self, client):
        resp = client.get("/printing/test-recipes")
        assert resp.status_code == 200
        assert len(resp.json()) >= 10

    def test_get_socs(self, client):
        resp = client.get("/printing/socs")
        assert resp.status_code == 200
        assert len(resp.json()) >= 5

    def test_get_artifacts(self, client):
        resp = client.get("/printing/artifacts")
        assert resp.status_code == 200
        assert len(resp.json()) >= 7

    def test_generate_pcl_endpoint(self, client):
        resp = client.post("/printing/pdl/pcl/generate", json={
            "page_size": "a4",
            "resolution_dpi": 300,
            "pages": 2,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["page_count"] == 2
        assert data["data_size"] > 0

    def test_generate_ps_endpoint(self, client):
        resp = client.post("/printing/pdl/ps/generate", json={
            "page_size": "a4",
            "pages": 1,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["dsc_compliant"] is True

    def test_render_pdf_endpoint(self, client):
        resp = client.post("/printing/pdl/render", json={
            "device": "pwgraster",
            "dpi": 300,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["device"] == "pwgraster"
        assert data["data_size"] > 0

    def test_select_profile_endpoint(self, client):
        resp = client.post("/printing/color/select", json={
            "paper_id": "paper_plain",
            "ink_id": "cmyk_standard",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["icc_file"] == "plain_cmyk.icc"

    def test_enqueue_job_endpoint(self, client):
        resp = client.post("/printing/queue/jobs", json={
            "document_name": "test.pdf",
            "printer_uri": "ipp://test/print",
            "priority": 50,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "queued"

    def test_validate_gate_endpoint(self, client):
        resp = client.post("/printing/validate", json={
            "artifacts": ["ipp_backend_config", "cups_backend_module"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] in ["pass", "partial", "fail"]

    def test_generate_certs_endpoint(self, client):
        resp = client.post("/printing/certs/generate", json={
            "domain": "all",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_artifacts"] >= 7
