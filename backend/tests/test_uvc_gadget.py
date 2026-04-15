"""D1 — SKILL-UVC: UVC gadget tests (#218).

Covers: descriptor scaffold, gadget-fs binding, UVCH264 payload generation,
still image capture, extension unit controls, REST API endpoints,
compliance validation, and error handling.
"""

from __future__ import annotations

import struct
from dataclasses import asdict
from unittest.mock import patch, MagicMock

import pytest

from backend import uvc_gadget as uvc


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def default_config():
    return uvc.GadgetConfig()


@pytest.fixture
def custom_config():
    return uvc.GadgetConfig(
        gadget_name="g_test",
        vendor_id=0xCAFE,
        product_id=0xBEEF,
        manufacturer="TestCorp",
        product="Test Camera",
        serial="TEST001",
        max_resolution=(1280, 720),
        max_fps=60,
        formats=[uvc.StreamFormat.H264, uvc.StreamFormat.MJPEG],
        still_method=uvc.StillMethod.METHOD_3,
    )


@pytest.fixture
def manager(default_config):
    return uvc.UVCGadgetManager(default_config)


@pytest.fixture
def custom_manager(custom_config):
    return uvc.UVCGadgetManager(custom_config)


@pytest.fixture
def descriptor_tree(default_config):
    return uvc.build_descriptor_tree(default_config)


@pytest.fixture
def payload_gen():
    return uvc.UVCH264PayloadGenerator(max_payload_size=3072)


@pytest.fixture
def created_manager(default_config):
    mgr = uvc.UVCGadgetManager(default_config)
    with patch.object(uvc.ConfigFSGadgetBinder, "create", return_value=True):
        mgr.create_gadget()
    return mgr


@pytest.fixture
def bound_manager(created_manager):
    with patch.object(uvc.ConfigFSGadgetBinder, "bind", return_value=True):
        created_manager.bind_udc("test_udc")
    return created_manager


@pytest.fixture
def streaming_manager(bound_manager):
    bound_manager.start_stream(uvc.StreamFormat.H264, 1920, 1080, 30)
    return bound_manager


# ═══════════════════════════════════════════════════════════════════════
# 1. Descriptor scaffold (marker: descriptor)
# ═══════════════════════════════════════════════════════════════════════


class TestDescriptorScaffold:
    @pytest.mark.descriptor
    def test_build_descriptor_tree(self, default_config):
        tree = uvc.build_descriptor_tree(default_config)
        assert tree is not None
        assert tree.camera_terminal.terminal_id == 1
        assert tree.processing_unit.unit_id == 2
        assert tree.output_terminal.terminal_id == 3

    @pytest.mark.descriptor
    def test_descriptor_tree_has_formats(self, descriptor_tree):
        assert len(descriptor_tree.formats) == 3
        fmt_ids = {f.format_id for f in descriptor_tree.formats}
        assert uvc.StreamFormat.H264 in fmt_ids
        assert uvc.StreamFormat.MJPEG in fmt_ids
        assert uvc.StreamFormat.YUY2 in fmt_ids

    @pytest.mark.descriptor
    def test_each_format_has_frames(self, descriptor_tree):
        for fmt in descriptor_tree.formats:
            assert len(fmt.frames) > 0
            for frame in fmt.frames:
                assert frame.width > 0
                assert frame.height > 0
                assert frame.max_fps > 0

    @pytest.mark.descriptor
    def test_format_guids_are_16_bytes(self, descriptor_tree):
        for fmt in descriptor_tree.formats:
            assert len(fmt.guid) == 16

    @pytest.mark.descriptor
    def test_h264_format_guid(self):
        guid = uvc._FORMAT_GUIDS[uvc.StreamFormat.H264]
        assert len(guid) == 16
        assert guid[:4] == b"H264"

    @pytest.mark.descriptor
    def test_mjpeg_format_guid(self):
        guid = uvc._FORMAT_GUIDS[uvc.StreamFormat.MJPEG]
        assert len(guid) == 16
        assert guid[:4] == b"MJPG"

    @pytest.mark.descriptor
    def test_yuy2_format_guid(self):
        guid = uvc._FORMAT_GUIDS[uvc.StreamFormat.YUY2]
        assert len(guid) == 16
        assert guid[:4] == b"YUY2"

    @pytest.mark.descriptor
    def test_camera_terminal_type(self, descriptor_tree):
        assert descriptor_tree.camera_terminal.terminal_type == uvc.UVCTerminalType.ITT_CAMERA

    @pytest.mark.descriptor
    def test_output_terminal_type(self, descriptor_tree):
        assert descriptor_tree.output_terminal.terminal_type == uvc.UVCTerminalType.TT_STREAMING

    @pytest.mark.descriptor
    def test_ct_pu_ot_chain(self, descriptor_tree):
        assert descriptor_tree.processing_unit.source_id == descriptor_tree.camera_terminal.terminal_id
        assert descriptor_tree.output_terminal.source_id == descriptor_tree.processing_unit.unit_id

    @pytest.mark.descriptor
    def test_still_image_descriptor(self, descriptor_tree):
        still = descriptor_tree.still_image
        assert still.width == 1920
        assert still.height == 1080
        assert still.method == uvc.StillMethod.METHOD_2

    @pytest.mark.descriptor
    def test_custom_resolution_limits(self, custom_config):
        tree = uvc.build_descriptor_tree(custom_config)
        for fmt in tree.formats:
            for frame in fmt.frames:
                assert frame.width <= 1280
                assert frame.height <= 720

    @pytest.mark.descriptor
    def test_extension_unit_in_tree(self, descriptor_tree):
        xu = descriptor_tree.extension_unit
        assert xu.unit_id == 6
        assert xu.num_controls > 0
        assert len(xu.controls) == xu.num_controls

    @pytest.mark.descriptor
    def test_validate_valid_tree(self, descriptor_tree):
        errors = uvc.validate_descriptors(descriptor_tree)
        assert errors == []

    @pytest.mark.descriptor
    def test_validate_missing_formats(self):
        tree = uvc.DescriptorTree(formats=[])
        errors = uvc.validate_descriptors(tree)
        assert any("At least one format" in e for e in errors)

    @pytest.mark.descriptor
    def test_validate_bad_chain(self):
        tree = uvc.DescriptorTree(
            processing_unit=uvc.ProcessingUnitDescriptor(source_id=99),
        )
        errors = uvc.validate_descriptors(tree)
        assert any("PU source_id" in e for e in errors)

    @pytest.mark.descriptor
    def test_validate_duplicate_xu_selector(self):
        tree = uvc.DescriptorTree(
            extension_unit=uvc.ExtensionUnitDescriptor(
                controls=[
                    uvc.XUControl(selector=1, name="A"),
                    uvc.XUControl(selector=1, name="B"),
                ]
            ),
            formats=[uvc.FormatDescriptor(
                format_id=uvc.StreamFormat.MJPEG,
                frames=[uvc.FrameDescriptor(width=640, height=480)],
            )],
        )
        errors = uvc.validate_descriptors(tree)
        assert any("Duplicate XU selector" in e for e in errors)

    @pytest.mark.descriptor
    def test_frame_descriptor_auto_bitrate(self):
        frame = uvc.FrameDescriptor(width=1920, height=1080, max_fps=30)
        assert frame.max_bitrate > 0
        assert frame.min_bitrate > 0
        assert frame.max_frame_size > 0

    @pytest.mark.descriptor
    def test_frame_descriptor_auto_frame_size(self):
        frame = uvc.FrameDescriptor(width=640, height=480)
        assert frame.max_frame_size == 640 * 480 * 2


# ═══════════════════════════════════════════════════════════════════════
# 2. GadgetFS / ConfigFS binding (marker: gadgetfs)
# ═══════════════════════════════════════════════════════════════════════


class TestGadgetFSBinding:
    @pytest.mark.gadgetfs
    def test_binder_creation(self, default_config):
        binder = uvc.ConfigFSGadgetBinder(default_config)
        assert binder.gadget_path.name == "g_uvc"
        assert not binder.is_bound

    @pytest.mark.gadgetfs
    def test_binder_gadget_path(self, custom_config):
        binder = uvc.ConfigFSGadgetBinder(custom_config)
        assert binder.gadget_path.name == "g_test"

    @pytest.mark.gadgetfs
    @patch("backend.uvc_gadget._configfs_mkdir", return_value=True)
    @patch("backend.uvc_gadget._configfs_write", return_value=True)
    def test_create_gadget(self, mock_write, mock_mkdir, default_config):
        binder = uvc.ConfigFSGadgetBinder(default_config)
        assert binder.create() is True
        assert mock_mkdir.call_count > 0
        assert mock_write.call_count > 0

    @pytest.mark.gadgetfs
    @patch("backend.uvc_gadget._configfs_mkdir", return_value=False)
    def test_create_gadget_mkdir_fail(self, mock_mkdir, default_config):
        binder = uvc.ConfigFSGadgetBinder(default_config)
        assert binder.create() is False

    @pytest.mark.gadgetfs
    @patch("backend.uvc_gadget._configfs_write", return_value=True)
    def test_bind_udc(self, mock_write, default_config):
        binder = uvc.ConfigFSGadgetBinder(default_config)
        assert binder.bind("test_udc") is True
        assert binder.is_bound

    @pytest.mark.gadgetfs
    @patch("backend.uvc_gadget._configfs_write", return_value=True)
    def test_unbind_udc(self, mock_write, default_config):
        binder = uvc.ConfigFSGadgetBinder(default_config)
        binder._bound = True
        assert binder.unbind() is True
        assert not binder.is_bound

    @pytest.mark.gadgetfs
    @patch("backend.uvc_gadget._detect_udc", return_value="")
    def test_bind_no_udc(self, mock_detect, default_config):
        binder = uvc.ConfigFSGadgetBinder(default_config)
        assert binder.bind() is False

    @pytest.mark.gadgetfs
    def test_detect_udc_no_path(self):
        with patch("backend.uvc_gadget.Path.exists", return_value=False):
            assert uvc._detect_udc() == ""

    @pytest.mark.gadgetfs
    @patch("shutil.rmtree")
    def test_destroy_gadget(self, mock_rmtree, default_config):
        binder = uvc.ConfigFSGadgetBinder(default_config)
        assert binder.destroy() is True
        mock_rmtree.assert_called_once()

    @pytest.mark.gadgetfs
    @patch("shutil.rmtree")
    @patch("backend.uvc_gadget._configfs_write", return_value=True)
    def test_destroy_unbinds_first(self, mock_write, mock_rmtree, default_config):
        binder = uvc.ConfigFSGadgetBinder(default_config)
        binder._bound = True
        binder.destroy()
        assert not binder.is_bound


# ═══════════════════════════════════════════════════════════════════════
# 3. UVCH264 payload generator (marker: payload)
# ═══════════════════════════════════════════════════════════════════════


class TestUVCH264PayloadGenerator:
    @pytest.mark.payload
    def test_create_generator(self, payload_gen):
        assert payload_gen.max_payload_size == 3072
        assert payload_gen.frame_count == 0

    @pytest.mark.payload
    def test_min_payload_size(self):
        with pytest.raises(ValueError, match="max_payload_size must be >= 64"):
            uvc.UVCH264PayloadGenerator(max_payload_size=32)

    @pytest.mark.payload
    def test_generate_single_chunk(self, payload_gen):
        nal = b"\x00\x00\x00\x01\x65" + b"\xAB" * 100
        payloads = payload_gen.generate(nal)
        assert len(payloads) == 1
        assert len(payloads[0]) == 12 + len(nal)

    @pytest.mark.payload
    def test_generate_multi_chunk(self, payload_gen):
        nal = b"\x00\x00\x00\x01\x65" + b"\xAB" * 5000
        payloads = payload_gen.generate(nal)
        assert len(payloads) > 1
        for p in payloads:
            assert len(p) <= 3072

    @pytest.mark.payload
    def test_fid_toggles(self, payload_gen):
        nal1 = b"\xAB" * 100
        payloads1 = payload_gen.generate(nal1)
        fid1 = payloads1[0][1] & 0x01

        nal2 = b"\xCD" * 100
        payloads2 = payload_gen.generate(nal2)
        fid2 = payloads2[0][1] & 0x01

        assert fid1 != fid2

    @pytest.mark.payload
    def test_eof_set_on_last_chunk(self, payload_gen):
        nal = b"\xAB" * 5000
        payloads = payload_gen.generate(nal)
        last_header_bf = payloads[-1][1]
        assert last_header_bf & 0x02

    @pytest.mark.payload
    def test_eof_not_on_intermediate(self, payload_gen):
        nal = b"\xAB" * 5000
        payloads = payload_gen.generate(nal)
        if len(payloads) > 1:
            for p in payloads[:-1]:
                assert not (p[1] & 0x02)

    @pytest.mark.payload
    def test_pts_present(self, payload_gen):
        nal = b"\xAB" * 100
        payloads = payload_gen.generate(nal)
        bf = payloads[0][1]
        assert bf & 0x04

    @pytest.mark.payload
    def test_scr_present(self, payload_gen):
        nal = b"\xAB" * 100
        payloads = payload_gen.generate(nal)
        bf = payloads[0][1]
        assert bf & 0x08

    @pytest.mark.payload
    def test_header_length_12(self, payload_gen):
        nal = b"\xAB" * 100
        payloads = payload_gen.generate(nal)
        assert payloads[0][0] == 12

    @pytest.mark.payload
    def test_empty_nal(self, payload_gen):
        payloads = payload_gen.generate(b"")
        assert payloads == []

    @pytest.mark.payload
    def test_frame_count_increments(self, payload_gen):
        payload_gen.generate(b"\xAB" * 100)
        assert payload_gen.frame_count == 1
        payload_gen.generate(b"\xCD" * 100)
        assert payload_gen.frame_count == 2

    @pytest.mark.payload
    def test_reset(self, payload_gen):
        payload_gen.generate(b"\xAB" * 100)
        payload_gen.reset()
        assert payload_gen.frame_count == 0

    @pytest.mark.payload
    def test_pts_monotonic(self, payload_gen):
        p1 = payload_gen.generate(b"\xAB" * 100)
        p2 = payload_gen.generate(b"\xCD" * 100)
        pts1 = struct.unpack_from("<I", p1[0], 2)[0]
        pts2 = struct.unpack_from("<I", p2[0], 2)[0]
        assert pts2 > pts1

    @pytest.mark.payload
    def test_total_data_preserved(self, payload_gen):
        nal = b"\xAB" * 5000
        payloads = payload_gen.generate(nal)
        total_data = b""
        for p in payloads:
            total_data += p[12:]
        assert total_data == nal


# ═══════════════════════════════════════════════════════════════════════
# 4. Still image capture (marker: still_image)
# ═══════════════════════════════════════════════════════════════════════


class TestStillImageCapture:
    @pytest.mark.still_image
    def test_capture_when_bound(self, bound_manager):
        capture = bound_manager.capture_still()
        assert capture.path != ""
        assert capture.width == 1920
        assert capture.height == 1080
        assert capture.size > 0

    @pytest.mark.still_image
    def test_capture_when_streaming(self, streaming_manager):
        capture = streaming_manager.capture_still()
        assert capture.path != ""
        assert capture.timestamp > 0

    @pytest.mark.still_image
    def test_capture_when_unconfigured(self, manager):
        capture = manager.capture_still()
        assert capture.path == ""

    @pytest.mark.still_image
    def test_still_image_descriptor_defaults(self):
        still = uvc.StillImageDescriptor()
        assert still.width == 1920
        assert still.height == 1080
        assert still.method == uvc.StillMethod.METHOD_2

    @pytest.mark.still_image
    def test_still_method_3(self, custom_config):
        tree = uvc.build_descriptor_tree(custom_config)
        assert tree.still_image.method == uvc.StillMethod.METHOD_3

    @pytest.mark.still_image
    def test_capture_path_unique(self, bound_manager):
        c1 = bound_manager.capture_still()
        c2 = bound_manager.capture_still()
        assert c1.path != c2.path


# ═══════════════════════════════════════════════════════════════════════
# 5. Extension unit (marker: extension_unit)
# ═══════════════════════════════════════════════════════════════════════


class TestExtensionUnit:
    @pytest.mark.extension_unit
    def test_xu_default_controls(self):
        controls = uvc.list_xu_controls()
        assert len(controls) == 8
        assert controls[0]["name"] == "Firmware Version"
        assert controls[0]["read_only"] is True

    @pytest.mark.extension_unit
    def test_xu_get_default(self, manager):
        value = manager.xu_get(2)
        assert value == 128

    @pytest.mark.extension_unit
    def test_xu_set(self, manager):
        manager.xu_set(2, 200)
        assert manager.xu_get(2) == 200

    @pytest.mark.extension_unit
    def test_xu_set_read_only(self, manager):
        with pytest.raises(ValueError, match="read-only"):
            manager.xu_set(1, 42)

    @pytest.mark.extension_unit
    def test_xu_set_out_of_range(self, manager):
        with pytest.raises(ValueError, match="out of range"):
            manager.xu_set(2, 999)

    @pytest.mark.extension_unit
    def test_xu_get_unknown_selector(self, manager):
        with pytest.raises(ValueError, match="Unknown XU selector"):
            manager.xu_get(99)

    @pytest.mark.extension_unit
    def test_xu_set_unknown_selector(self, manager):
        with pytest.raises(ValueError, match="Unknown XU selector"):
            manager.xu_set(99, 42)

    @pytest.mark.extension_unit
    def test_xu_descriptor_guid(self, descriptor_tree):
        assert len(descriptor_tree.extension_unit.guid) == 16

    @pytest.mark.extension_unit
    def test_xu_all_selectors_unique(self, descriptor_tree):
        selectors = [c.selector for c in descriptor_tree.extension_unit.controls]
        assert len(selectors) == len(set(selectors))

    @pytest.mark.extension_unit
    def test_xu_control_info_flags(self):
        ro = uvc.XUControl(selector=1, name="RO", read_only=True)
        assert ro.info_flags == 0x01
        rw = uvc.XUControl(selector=2, name="RW")
        assert rw.info_flags == 0x03


# ═══════════════════════════════════════════════════════════════════════
# 6. UVC Gadget Manager lifecycle (marker: lifecycle)
# ═══════════════════════════════════════════════════════════════════════


class TestGadgetManagerLifecycle:
    @pytest.mark.lifecycle
    def test_initial_state(self, manager):
        assert manager.state == uvc.GadgetState.UNCONFIGURED

    @pytest.mark.lifecycle
    def test_create_gadget(self, default_config):
        mgr = uvc.UVCGadgetManager(default_config)
        with patch.object(uvc.ConfigFSGadgetBinder, "create", return_value=True):
            assert mgr.create_gadget() is True
        assert mgr.state == uvc.GadgetState.CREATED
        assert mgr.descriptor_tree is not None

    @pytest.mark.lifecycle
    def test_create_twice_fails(self, created_manager):
        assert created_manager.create_gadget() is False

    @pytest.mark.lifecycle
    def test_bind_udc(self, created_manager):
        with patch.object(uvc.ConfigFSGadgetBinder, "bind", return_value=True):
            assert created_manager.bind_udc("test_udc") is True
        assert created_manager.state == uvc.GadgetState.BOUND

    @pytest.mark.lifecycle
    def test_bind_before_create_fails(self, manager):
        assert manager.bind_udc("test") is False

    @pytest.mark.lifecycle
    def test_start_stream(self, bound_manager):
        assert bound_manager.start_stream(uvc.StreamFormat.H264, 1920, 1080, 30) is True
        assert bound_manager.state == uvc.GadgetState.STREAMING

    @pytest.mark.lifecycle
    def test_start_stream_before_bind_fails(self, created_manager):
        assert created_manager.start_stream() is False

    @pytest.mark.lifecycle
    def test_stop_stream(self, streaming_manager):
        assert streaming_manager.stop_stream() is True
        assert streaming_manager.state == uvc.GadgetState.BOUND

    @pytest.mark.lifecycle
    def test_stop_when_not_streaming(self, bound_manager):
        assert bound_manager.stop_stream() is False

    @pytest.mark.lifecycle
    def test_unbind(self, bound_manager):
        with patch.object(uvc.ConfigFSGadgetBinder, "unbind", return_value=True):
            assert bound_manager.unbind_udc() is True
        assert bound_manager.state == uvc.GadgetState.CREATED

    @pytest.mark.lifecycle
    def test_unbind_stops_stream(self, streaming_manager):
        with patch.object(uvc.ConfigFSGadgetBinder, "unbind", return_value=True):
            assert streaming_manager.unbind_udc() is True
        assert streaming_manager.state == uvc.GadgetState.CREATED

    @pytest.mark.lifecycle
    def test_destroy(self, created_manager):
        with patch.object(uvc.ConfigFSGadgetBinder, "destroy", return_value=True):
            assert created_manager.destroy_gadget() is True
        assert created_manager.state == uvc.GadgetState.UNCONFIGURED

    @pytest.mark.lifecycle
    def test_destroy_from_streaming(self, streaming_manager):
        with patch.object(uvc.ConfigFSGadgetBinder, "unbind", return_value=True):
            with patch.object(uvc.ConfigFSGadgetBinder, "destroy", return_value=True):
                assert streaming_manager.destroy_gadget() is True
        assert streaming_manager.state == uvc.GadgetState.UNCONFIGURED

    @pytest.mark.lifecycle
    def test_send_payload(self, streaming_manager):
        assert streaming_manager.send_payload(b"\xAB" * 100) is True
        assert streaming_manager.stream_status.frames_sent == 1
        assert streaming_manager.stream_status.bytes_sent == 100

    @pytest.mark.lifecycle
    def test_send_payload_not_streaming(self, bound_manager):
        assert bound_manager.send_payload(b"\xAB") is False

    @pytest.mark.lifecycle
    def test_get_status(self, manager):
        status = manager.get_status()
        assert status["state"] == "unconfigured"
        assert status["gadget_name"] == "g_uvc"

    @pytest.mark.lifecycle
    def test_get_status_streaming(self, streaming_manager):
        status = streaming_manager.get_status()
        assert status["state"] == "streaming"
        assert status["stream"]["format"] == "h264"
        assert status["stream"]["fps"] == 30


# ═══════════════════════════════════════════════════════════════════════
# 7. REST API endpoints (marker: rest_api)
# ═══════════════════════════════════════════════════════════════════════


class TestRESTEndpoints:
    @pytest.mark.rest_api
    def test_list_formats(self):
        formats = uvc.list_stream_formats()
        assert len(formats) == 3
        ids = {f["id"] for f in formats}
        assert "h264" in ids
        assert "mjpeg" in ids
        assert "yuy2" in ids

    @pytest.mark.rest_api
    def test_list_resolutions(self):
        resolutions = uvc.list_resolutions()
        assert len(resolutions) > 0
        assert resolutions[0]["width"] == 1920
        assert resolutions[0]["height"] == 1080

    @pytest.mark.rest_api
    def test_list_xu_controls(self):
        controls = uvc.list_xu_controls()
        assert len(controls) == 8
        for ctrl in controls:
            assert "selector" in ctrl
            assert "name" in ctrl
            assert "size" in ctrl

    @pytest.mark.rest_api
    def test_format_has_guid(self):
        formats = uvc.list_stream_formats()
        for f in formats:
            assert "guid" in f
            assert len(f["guid"]) == 32


# ═══════════════════════════════════════════════════════════════════════
# 8. Compliance check (marker: compliance)
# ═══════════════════════════════════════════════════════════════════════


class TestComplianceCheck:
    @pytest.mark.compliance
    def test_compliance_all_pass(self, created_manager):
        report = uvc.run_compliance_check(created_manager)
        assert report.all_passed
        assert report.fail_count == 0
        assert report.pass_count > 0

    @pytest.mark.compliance
    def test_compliance_no_tree(self, manager):
        report = uvc.run_compliance_check(manager)
        assert not report.all_passed

    @pytest.mark.compliance
    def test_compliance_has_chapter9(self, created_manager):
        report = uvc.run_compliance_check(created_manager)
        ch9_tests = [r for r in report.results if r.chapter == "Chapter 9"]
        assert len(ch9_tests) > 0

    @pytest.mark.compliance
    def test_compliance_has_uvc15(self, created_manager):
        report = uvc.run_compliance_check(created_manager)
        uvc_tests = [r for r in report.results if r.chapter == "UVC 1.5"]
        assert len(uvc_tests) > 0

    @pytest.mark.compliance
    def test_compliance_h264_check(self, created_manager):
        report = uvc.run_compliance_check(created_manager)
        h264_result = next(r for r in report.results if "H.264" in r.test_name)
        assert h264_result.passed

    @pytest.mark.compliance
    def test_compliance_still_image_check(self, created_manager):
        report = uvc.run_compliance_check(created_manager)
        still_result = next(r for r in report.results if "Still" in r.test_name)
        assert still_result.passed

    @pytest.mark.compliance
    def test_compliance_iad_check(self, created_manager):
        report = uvc.run_compliance_check(created_manager)
        iad_result = next(r for r in report.results if "IAD" in r.test_name)
        assert iad_result.passed

    @pytest.mark.compliance
    def test_compliance_report_fields(self, created_manager):
        report = uvc.run_compliance_check(created_manager)
        assert report.gadget_name == "g_uvc"
        assert report.timestamp > 0


# ═══════════════════════════════════════════════════════════════════════
# 9. Error handling (marker: error_handling)
# ═══════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    @pytest.mark.error_handling
    def test_invalid_format_enum(self):
        with pytest.raises(ValueError):
            uvc.StreamFormat("invalid")

    @pytest.mark.error_handling
    def test_gadget_state_transitions(self, manager):
        assert manager.bind_udc("test") is False
        assert manager.start_stream() is False
        assert manager.stop_stream() is False

    @pytest.mark.error_handling
    def test_validate_empty_format(self):
        tree = uvc.DescriptorTree(
            formats=[uvc.FormatDescriptor(format_id=uvc.StreamFormat.H264, frames=[])],
        )
        errors = uvc.validate_descriptors(tree)
        assert any("no frame descriptors" in e for e in errors)

    @pytest.mark.error_handling
    def test_validate_bad_frame_size(self):
        tree = uvc.DescriptorTree(
            formats=[uvc.FormatDescriptor(
                format_id=uvc.StreamFormat.H264,
                frames=[uvc.FrameDescriptor(width=0, height=0)],
            )],
        )
        errors = uvc.validate_descriptors(tree)
        assert any("Invalid frame size" in e for e in errors)

    @pytest.mark.error_handling
    def test_validate_bad_fps(self):
        tree = uvc.DescriptorTree(
            formats=[uvc.FormatDescriptor(
                format_id=uvc.StreamFormat.H264,
                frames=[uvc.FrameDescriptor(width=640, height=480, max_fps=0)],
            )],
        )
        errors = uvc.validate_descriptors(tree)
        assert any("Invalid max_fps" in e for e in errors)

    @pytest.mark.error_handling
    def test_payload_header_pack(self):
        hdr = uvc.UVCPayloadHeader(
            header_length=12, bit_field=0x0D,
            pts=100000, scr_stc=100000, scr_sof=42,
        )
        packed = hdr.pack()
        assert len(packed) == 12
        assert packed[0] == 12
        assert packed[1] == 0x0D

    @pytest.mark.error_handling
    def test_payload_header_flags(self):
        hdr = uvc.UVCPayloadHeader(bit_field=0x0F)
        assert hdr.has_pts is True
        assert hdr.has_scr is True
        assert hdr.eof is True
        assert hdr.fid is True

    @pytest.mark.error_handling
    def test_configfs_write_error(self):
        from pathlib import Path
        result = uvc._configfs_write(Path("/nonexistent/path"), "test")
        assert result is False

    @pytest.mark.error_handling
    def test_configfs_read_error(self):
        from pathlib import Path
        result = uvc._configfs_read(Path("/nonexistent/path"))
        assert result == ""

    @pytest.mark.error_handling
    def test_xu_control_size_zero(self):
        tree = uvc.DescriptorTree(
            extension_unit=uvc.ExtensionUnitDescriptor(
                controls=[uvc.XUControl(selector=1, name="Bad", size=0)]
            ),
            formats=[uvc.FormatDescriptor(
                format_id=uvc.StreamFormat.MJPEG,
                frames=[uvc.FrameDescriptor(width=640, height=480)],
            )],
        )
        errors = uvc.validate_descriptors(tree)
        assert any("size must be >= 1" in e for e in errors)

    @pytest.mark.error_handling
    def test_create_binder_fail_propagates(self, default_config):
        mgr = uvc.UVCGadgetManager(default_config)
        with patch.object(uvc.ConfigFSGadgetBinder, "create", return_value=False):
            assert mgr.create_gadget() is False
        assert mgr.state == uvc.GadgetState.ERROR

    @pytest.mark.error_handling
    def test_bind_fail_propagates(self, created_manager):
        with patch.object(uvc.ConfigFSGadgetBinder, "bind", return_value=False):
            assert created_manager.bind_udc("test") is False
        assert created_manager.state == uvc.GadgetState.ERROR


# ═══════════════════════════════════════════════════════════════════════
# 10. Enums & data classes (marker: enums)
# ═══════════════════════════════════════════════════════════════════════


class TestEnumsAndDataClasses:
    @pytest.mark.enums
    def test_stream_format_values(self):
        assert uvc.StreamFormat.H264.value == "h264"
        assert uvc.StreamFormat.MJPEG.value == "mjpeg"
        assert uvc.StreamFormat.YUY2.value == "yuy2"

    @pytest.mark.enums
    def test_gadget_state_values(self):
        assert uvc.GadgetState.UNCONFIGURED.value == "unconfigured"
        assert uvc.GadgetState.STREAMING.value == "streaming"

    @pytest.mark.enums
    def test_still_method_values(self):
        assert uvc.StillMethod.NONE == 0
        assert uvc.StillMethod.METHOD_2 == 2
        assert uvc.StillMethod.METHOD_3 == 3

    @pytest.mark.enums
    def test_descriptor_type_values(self):
        assert uvc.DescriptorType.CS_INTERFACE == 0x24

    @pytest.mark.enums
    def test_vs_descriptor_subtypes(self):
        assert uvc.VSDescriptorSubtype.VS_FORMAT_H264 == 0x13
        assert uvc.VSDescriptorSubtype.VS_FORMAT_MJPEG == 0x06

    @pytest.mark.enums
    def test_vc_descriptor_subtypes(self):
        assert uvc.VCDescriptorSubtype.VC_EXTENSION_UNIT == 0x06

    @pytest.mark.enums
    def test_uvc_request_codes(self):
        assert uvc.UVCRequestCode.SET_CUR == 0x01
        assert uvc.UVCRequestCode.GET_CUR == 0x81

    @pytest.mark.enums
    def test_gadget_config_defaults(self):
        cfg = uvc.GadgetConfig()
        assert cfg.vendor_id == 0x1D6B
        assert cfg.device_class == 0xEF
        assert cfg.device_subclass == 0x02
        assert cfg.device_protocol == 0x01

    @pytest.mark.enums
    def test_compliance_result_dataclass(self):
        r = uvc.ComplianceResult(test_name="test", passed=True, details="ok", chapter="ch9")
        assert r.passed
        d = asdict(r)
        assert d["test_name"] == "test"

    @pytest.mark.enums
    def test_stream_status_defaults(self):
        s = uvc.StreamStatus()
        assert s.frames_sent == 0
        assert s.bytes_sent == 0
        assert s.errors == 0


# ═══════════════════════════════════════════════════════════════════════
# 11. Config loading (marker: config)
# ═══════════════════════════════════════════════════════════════════════


class TestConfigLoading:
    @pytest.mark.config
    def test_load_config(self):
        cfg = uvc._load_config()
        assert isinstance(cfg, dict)

    @pytest.mark.config
    def test_config_has_gadget(self):
        cfg = uvc._load_config()
        if cfg:
            assert "gadget" in cfg

    @pytest.mark.config
    def test_config_has_formats(self):
        cfg = uvc._load_config()
        if cfg:
            assert "formats" in cfg

    @pytest.mark.config
    def test_config_has_extension_unit(self):
        cfg = uvc._load_config()
        if cfg:
            assert "extension_unit" in cfg
