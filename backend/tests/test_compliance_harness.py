"""C8 — Unit + smoke tests for the Protocol compliance harness (#217).

Covers:
  - Normalised report schema (ComplianceReport, TestCaseResult)
  - ODTT wrapper (parse_output, run with mocked subprocess)
  - USBCV wrapper (parse_output, run with mocked subprocess)
  - UAC test wrapper (parse_output, run with mocked subprocess)
  - Registry (list, get, register, run)
  - Audit log integration
  - Edge cases (tool not found, invalid profile, timeout, empty output)
  - REST endpoints: /compliance/tools, /compliance/tools/{name},
    /compliance/run/{tool_name}
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.compliance_harness import (
    ComplianceProtocol,
    ComplianceReport,
    ComplianceTool,
    ComplianceToolInfo,
    ODTTWrapper,
    TestCaseResult,
    TestVerdict,
    UACTestWrapper,
    USBCVWrapper,
    get_tool,
    list_tools,
    log_compliance_report,
    register_tool,
    run_tool,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Normalised report schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTestCaseResult:
    def test_pass_verdict(self):
        r = TestCaseResult(
            test_id="T-001", test_name="Descriptor check",
            verdict=TestVerdict.pass_,
        )
        assert r.passed is True
        assert r.verdict == TestVerdict.pass_

    def test_fail_verdict(self):
        r = TestCaseResult(
            test_id="T-002", test_name="Endpoint check",
            verdict=TestVerdict.fail, message="endpoint missing",
        )
        assert r.passed is False

    def test_error_verdict(self):
        r = TestCaseResult(
            test_id="T-003", test_name="Crash",
            verdict=TestVerdict.error, message="segfault",
        )
        assert r.passed is False

    def test_skipped_verdict(self):
        r = TestCaseResult(
            test_id="T-004", test_name="Optional",
            verdict=TestVerdict.skipped,
        )
        assert r.passed is False

    def test_evidence_and_duration(self):
        r = TestCaseResult(
            test_id="T-005", test_name="Latency",
            verdict=TestVerdict.pass_,
            evidence="/tmp/log.txt", duration_s=1.5,
        )
        assert r.evidence == "/tmp/log.txt"
        assert r.duration_s == 1.5


class TestComplianceReport:
    def _make_report(self, verdicts: list[TestVerdict]) -> ComplianceReport:
        results = [
            TestCaseResult(
                test_id=f"T-{i:03d}", test_name=f"Test {i}",
                verdict=v,
            )
            for i, v in enumerate(verdicts)
        ]
        return ComplianceReport(
            tool_name="test_tool",
            protocol=ComplianceProtocol.onvif,
            device_under_test="192.168.1.10",
            results=results,
        )

    def test_all_pass(self):
        rpt = self._make_report([TestVerdict.pass_, TestVerdict.pass_])
        assert rpt.overall_pass is True
        assert rpt.total == 2
        assert rpt.passed_count == 2
        assert rpt.failed_count == 0

    def test_one_fail(self):
        rpt = self._make_report([TestVerdict.pass_, TestVerdict.fail])
        assert rpt.overall_pass is False
        assert rpt.passed_count == 1
        assert rpt.failed_count == 1

    def test_skipped_counts_as_pass(self):
        rpt = self._make_report([TestVerdict.pass_, TestVerdict.skipped])
        assert rpt.overall_pass is True
        assert rpt.skipped_count == 1

    def test_empty_report_not_passing(self):
        rpt = self._make_report([])
        assert rpt.overall_pass is False
        assert rpt.total == 0

    def test_error_count(self):
        rpt = self._make_report([TestVerdict.error, TestVerdict.pass_])
        assert rpt.error_count == 1
        assert rpt.overall_pass is False

    def test_summary_dict(self):
        rpt = self._make_report([TestVerdict.pass_])
        d = rpt.summary_dict()
        assert d["tool_name"] == "test_tool"
        assert d["protocol"] == "onvif"
        assert d["overall_pass"] is True
        assert d["total"] == 1
        assert "timestamp" in d

    def test_to_dict_includes_results(self):
        rpt = self._make_report([TestVerdict.pass_, TestVerdict.fail])
        d = rpt.to_dict()
        assert len(d["results"]) == 2
        assert d["results"][0]["verdict"] == "pass"
        assert d["results"][1]["verdict"] == "fail"

    def test_metadata(self):
        rpt = ComplianceReport(
            tool_name="t", protocol=ComplianceProtocol.usb,
            device_under_test="dev1",
            metadata={"vid": "1234", "pid": "5678"},
        )
        assert rpt.metadata["vid"] == "1234"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. ODTT wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SAMPLE_ODTT_OUTPUT = """\
ONVIF Device Test Tool v22.12 — headless mode
Device: 192.168.1.100
Profile: S

TEST-ONVIF-001  GetCapabilities                 PASS 0.12s
TEST-ONVIF-002  GetProfiles                     PASS 0.08s
TEST-ONVIF-003  GetStreamUri                    PASS 0.15s
TEST-ONVIF-004  RTSP stream validation          FAIL 2.30s stream timeout
TEST-ONVIF-005  PTZ continuous move             SKIP
TEST-ONVIF-006  GetDeviceInformation            PASS 0.05s

Summary: 4 pass, 1 fail, 1 skip
"""


class TestODTTWrapper:
    def test_tool_info(self):
        w = ODTTWrapper()
        assert w.tool_info.name == "odtt"
        assert w.tool_info.protocol == ComplianceProtocol.onvif
        assert "S" in w.tool_info.supported_profiles

    def test_parse_output(self):
        w = ODTTWrapper()
        results = w.parse_output(SAMPLE_ODTT_OUTPUT)
        assert len(results) == 6
        assert results[0].test_id == "TEST-ONVIF-001"
        assert results[0].verdict == TestVerdict.pass_
        assert results[0].duration_s == 0.12
        assert results[3].verdict == TestVerdict.fail
        assert results[3].message == "stream timeout"
        assert results[4].verdict == TestVerdict.skipped

    def test_parse_empty_output(self):
        w = ODTTWrapper()
        results = w.parse_output("")
        assert results == []

    def test_invalid_profile(self):
        w = ODTTWrapper()
        with pytest.raises(ValueError, match="Unsupported ONVIF profile"):
            w.run("192.168.1.1", profile="Z")

    @patch.object(ODTTWrapper, "_exec")
    def test_run_success(self, mock_exec):
        mock_exec.return_value = (0, SAMPLE_ODTT_OUTPUT, "")
        w = ODTTWrapper()
        report = w.run("192.168.1.100", profile="S")
        assert report.tool_name == "odtt"
        assert report.protocol == ComplianceProtocol.onvif
        assert report.device_under_test == "192.168.1.100"
        assert report.total == 6
        assert report.passed_count == 4
        assert report.failed_count == 1
        assert report.skipped_count == 1
        assert report.metadata["profile"] == "S"

    @patch.object(ODTTWrapper, "_exec")
    def test_run_binary_not_found(self, mock_exec):
        mock_exec.return_value = (-2, "", "binary not found: onvif_test_tool")
        w = ODTTWrapper()
        report = w.run("192.168.1.1", profile="S")
        assert len(report.results) == 1
        assert report.results[0].verdict == TestVerdict.error
        assert "binary not found" in report.results[0].message

    @patch.object(ODTTWrapper, "_exec")
    def test_run_with_credentials(self, mock_exec):
        mock_exec.return_value = (0, SAMPLE_ODTT_OUTPUT, "")
        w = ODTTWrapper()
        report = w.run(
            "192.168.1.100", profile="S",
            username="admin", password="secret",
        )
        call_args = mock_exec.call_args[0][0]
        assert "--user" in call_args
        assert "--pass" in call_args
        assert report.metadata["username"] == "admin"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. USBCV wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SAMPLE_USBCV_OUTPUT = """\
USB Command Verifier v4.1.2 — CLI mode
Device: /dev/bus/usb/001/003
Class: device

USB-DEV-001  Device Descriptor           PASS 0.01s
USB-DEV-002  Configuration Descriptor    PASS 0.01s
USB-DEV-003  String Descriptors          PASS 0.02s
USB-DEV-004  SetConfiguration            PASS 0.01s
USB-DEV-005  Suspend/Resume              FAIL 1.50s resume timeout
USB-DEV-006  Remote Wakeup              ERROR 0.00s not supported

Summary: 4 pass, 1 fail, 1 error
"""


class TestUSBCVWrapper:
    def test_tool_info(self):
        w = USBCVWrapper()
        assert w.tool_info.name == "usbcv"
        assert w.tool_info.protocol == ComplianceProtocol.usb
        assert "device" in w.tool_info.supported_profiles

    def test_parse_output(self):
        w = USBCVWrapper()
        results = w.parse_output(SAMPLE_USBCV_OUTPUT)
        assert len(results) == 6
        assert results[0].test_id == "USB-DEV-001"
        assert results[0].verdict == TestVerdict.pass_
        assert results[4].verdict == TestVerdict.fail
        assert results[4].message == "resume timeout"
        assert results[5].verdict == TestVerdict.error

    def test_parse_empty_output(self):
        w = USBCVWrapper()
        results = w.parse_output("")
        assert results == []

    def test_invalid_profile(self):
        w = USBCVWrapper()
        with pytest.raises(ValueError, match="Unsupported USB test class"):
            w.run("/dev/usb/0", profile="nonexistent")

    @patch.object(USBCVWrapper, "_exec")
    def test_run_success(self, mock_exec):
        mock_exec.return_value = (0, SAMPLE_USBCV_OUTPUT, "")
        w = USBCVWrapper()
        report = w.run("/dev/bus/usb/001/003", profile="device")
        assert report.tool_name == "usbcv"
        assert report.total == 6
        assert report.passed_count == 4
        assert report.failed_count == 1
        assert report.error_count == 1
        assert report.metadata["test_class"] == "device"

    @patch.object(USBCVWrapper, "_exec")
    def test_run_with_vid_pid(self, mock_exec):
        mock_exec.return_value = (0, SAMPLE_USBCV_OUTPUT, "")
        w = USBCVWrapper()
        report = w.run("/dev/usb/0", profile="device", vid="1d6b", pid="0002")
        call_args = mock_exec.call_args[0][0]
        assert "--vid" in call_args
        assert "--pid" in call_args
        assert report.metadata["vid"] == "1d6b"

    @patch.object(USBCVWrapper, "_exec")
    def test_run_binary_not_found(self, mock_exec):
        mock_exec.return_value = (-2, "", "binary not found: usbcv")
        w = USBCVWrapper()
        report = w.run("/dev/usb/0", profile="device")
        assert report.results[0].verdict == TestVerdict.error


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. UAC test wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SAMPLE_UAC_OUTPUT = """\
UAC Compliance Test Suite v1.0.0
Device: /dev/snd/pcmC1D0c
Version: UAC 2.0

UAC-DESC-001  Audio Control Interface Descriptor    PASS 0.01s
UAC-DESC-002  Clock Source Descriptor              PASS 0.01s
UAC-DESC-003  Input Terminal Descriptor            PASS 0.01s
UAC-DESC-004  Output Terminal Descriptor           PASS 0.01s
UAC-FUNC-001  Sample Rate Switch 48000Hz           PASS 0.25s
UAC-FUNC-002  Sample Rate Switch 96000Hz           PASS 0.30s
UAC-FUNC-003  Volume Control Get/Set               FAIL 0.10s range mismatch
UAC-FUNC-004  Mute Control                         PASS 0.05s
UAC-STRM-001  Streaming Endpoint Open              PASS 0.15s
UAC-STRM-002  Isochronous Transfer                 PASS 1.00s
UAC-STRM-003  Feedback Endpoint                    SKIP

Summary: 9 pass, 1 fail, 1 skip
"""


class TestUACTestWrapper:
    def test_tool_info(self):
        w = UACTestWrapper()
        assert w.tool_info.name == "uac_test"
        assert w.tool_info.protocol == ComplianceProtocol.uac
        assert "uac2" in w.tool_info.supported_profiles

    def test_parse_output(self):
        w = UACTestWrapper()
        results = w.parse_output(SAMPLE_UAC_OUTPUT)
        assert len(results) == 11
        assert results[0].test_id == "UAC-DESC-001"
        assert results[0].verdict == TestVerdict.pass_
        assert results[6].verdict == TestVerdict.fail
        assert results[6].message == "range mismatch"
        assert results[10].verdict == TestVerdict.skipped

    def test_parse_empty_output(self):
        w = UACTestWrapper()
        results = w.parse_output("")
        assert results == []

    def test_invalid_profile(self):
        w = UACTestWrapper()
        with pytest.raises(ValueError, match="Unsupported UAC version"):
            w.run("/dev/snd/0", profile="uac3")

    @patch.object(UACTestWrapper, "_exec")
    def test_run_success(self, mock_exec):
        mock_exec.return_value = (0, SAMPLE_UAC_OUTPUT, "")
        w = UACTestWrapper()
        report = w.run("/dev/snd/pcmC1D0c", profile="uac2")
        assert report.tool_name == "uac_test"
        assert report.total == 11
        assert report.passed_count == 9
        assert report.failed_count == 1
        assert report.skipped_count == 1
        assert report.metadata["uac_version"] == "uac2"

    @patch.object(UACTestWrapper, "_exec")
    def test_run_with_sample_rate(self, mock_exec):
        mock_exec.return_value = (0, SAMPLE_UAC_OUTPUT, "")
        w = UACTestWrapper()
        w.run("/dev/snd/0", profile="uac2", sample_rate=48000, channels=2)
        call_args = mock_exec.call_args[0][0]
        assert "--sample-rate" in call_args
        assert "--channels" in call_args

    @patch.object(UACTestWrapper, "_exec")
    def test_run_binary_not_found(self, mock_exec):
        mock_exec.return_value = (-2, "", "binary not found: uac_test")
        w = UACTestWrapper()
        report = w.run("/dev/snd/0", profile="uac2")
        assert report.results[0].verdict == TestVerdict.error


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRegistry:
    def test_list_tools(self):
        tools = list_tools()
        names = {t.name for t in tools}
        assert "odtt" in names
        assert "usbcv" in names
        assert "uac_test" in names

    def test_get_tool_known(self):
        tool = get_tool("odtt")
        assert isinstance(tool, ODTTWrapper)

    def test_get_tool_unknown(self):
        with pytest.raises(KeyError, match="Unknown compliance tool"):
            get_tool("nonexistent_tool")

    def test_register_custom_tool(self):
        class StubTool(ComplianceTool):
            def __init__(self):
                self.tool_info = ComplianceToolInfo(
                    name="stub",
                    protocol=ComplianceProtocol.usb,
                    version="0.1",
                    binary="stub_bin",
                )

            def run(self, device_target, profile="", **kw):
                return ComplianceReport(
                    tool_name="stub",
                    protocol=ComplianceProtocol.usb,
                    device_under_test=device_target,
                )

            def parse_output(self, raw_output):
                return []

        register_tool("stub", StubTool)
        tool = get_tool("stub")
        assert isinstance(tool, StubTool)
        report = tool.run("dev0")
        assert report.tool_name == "stub"

    @patch.object(ODTTWrapper, "_exec")
    def test_run_tool(self, mock_exec):
        mock_exec.return_value = (0, SAMPLE_ODTT_OUTPUT, "")
        report = run_tool("odtt", "192.168.1.1", profile="S")
        assert report.tool_name == "odtt"
        assert report.total == 6


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. Audit log integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAuditIntegration:
    @pytest.mark.asyncio
    async def test_log_compliance_report(self):
        report = ComplianceReport(
            tool_name="odtt",
            protocol=ComplianceProtocol.onvif,
            device_under_test="192.168.1.10",
            results=[
                TestCaseResult(
                    test_id="T-001", test_name="Test",
                    verdict=TestVerdict.pass_,
                ),
            ],
        )
        with patch("backend.audit.log", new_callable=AsyncMock, return_value=42):
            result = await log_compliance_report(report)
            assert result == 42

    @pytest.mark.asyncio
    async def test_log_compliance_report_failure_no_raise(self):
        report = ComplianceReport(
            tool_name="odtt",
            protocol=ComplianceProtocol.onvif,
            device_under_test="192.168.1.10",
        )
        with patch("backend.audit.log", new_callable=AsyncMock, side_effect=Exception("db error")):
            result = await log_compliance_report(report)
            assert result is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. Edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEdgeCases:
    def test_check_available_missing_binary(self):
        w = ODTTWrapper()
        assert w.check_available() is False

    @patch.object(ODTTWrapper, "_exec")
    def test_timeout_handling(self, mock_exec):
        mock_exec.return_value = (-1, "", "timeout after 10s")
        w = ODTTWrapper()
        report = w.run("192.168.1.1", profile="S", timeout_s=10)
        assert report.metadata["return_code"] == -1

    def test_verdict_enum_values(self):
        assert TestVerdict.pass_.value == "pass"
        assert TestVerdict.fail.value == "fail"
        assert TestVerdict.error.value == "error"
        assert TestVerdict.skipped.value == "skipped"

    def test_protocol_enum_values(self):
        assert ComplianceProtocol.onvif.value == "onvif"
        assert ComplianceProtocol.usb.value == "usb"
        assert ComplianceProtocol.uac.value == "uac"

    def test_compliance_tool_info_defaults(self):
        info = ComplianceToolInfo(
            name="test", protocol=ComplianceProtocol.usb,
            version="1.0", binary="test_bin",
        )
        assert info.description == ""
        assert info.supported_profiles == []

    @patch.object(USBCVWrapper, "_exec")
    def test_run_with_output_file(self, mock_exec):
        mock_exec.return_value = (0, SAMPLE_USBCV_OUTPUT, "")
        w = USBCVWrapper()
        report = w.run(
            "/dev/usb/0", profile="device",
            output_file="/tmp/report.xml",
        )
        call_args = mock_exec.call_args[0][0]
        assert "--report" in call_args
        assert report.raw_log_path == "/tmp/report.xml"

    def test_report_with_mixed_verdicts(self):
        results = [
            TestCaseResult(test_id=f"T-{i}", test_name=f"T{i}", verdict=v)
            for i, v in enumerate([
                TestVerdict.pass_, TestVerdict.fail,
                TestVerdict.error, TestVerdict.skipped,
            ])
        ]
        rpt = ComplianceReport(
            tool_name="test", protocol=ComplianceProtocol.usb,
            device_under_test="dev", results=results,
        )
        assert rpt.total == 4
        assert rpt.passed_count == 1
        assert rpt.failed_count == 1
        assert rpt.error_count == 1
        assert rpt.skipped_count == 1
        assert rpt.overall_pass is False

    @patch.object(ODTTWrapper, "_exec")
    def test_odtt_default_profile(self, mock_exec):
        mock_exec.return_value = (0, "", "")
        w = ODTTWrapper()
        report = w.run("192.168.1.1")
        assert report.metadata["profile"] == "S"

    @patch.object(UACTestWrapper, "_exec")
    def test_uac_default_profile(self, mock_exec):
        mock_exec.return_value = (0, "", "")
        w = UACTestWrapper()
        report = w.run("/dev/snd/0")
        assert report.metadata["uac_version"] == "uac2"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. Smoke test per wrapper (integration-style)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSmokeODTT:
    @patch.object(ODTTWrapper, "_exec")
    def test_full_lifecycle(self, mock_exec):
        mock_exec.return_value = (0, SAMPLE_ODTT_OUTPUT, "")
        report = run_tool("odtt", "192.168.1.100", profile="S")
        assert report.overall_pass is False  # has 1 fail
        assert report.passed_count == 4
        assert report.failed_count == 1
        assert report.skipped_count == 1
        d = report.to_dict()
        assert d["protocol"] == "onvif"
        assert len(d["results"]) == 6


class TestSmokeUSBCV:
    @patch.object(USBCVWrapper, "_exec")
    def test_full_lifecycle(self, mock_exec):
        mock_exec.return_value = (0, SAMPLE_USBCV_OUTPUT, "")
        report = run_tool("usbcv", "/dev/bus/usb/001/003", profile="device")
        assert report.overall_pass is False  # has 1 fail + 1 error
        assert report.passed_count == 4
        assert report.failed_count == 1
        assert report.error_count == 1
        d = report.to_dict()
        assert d["protocol"] == "usb"
        assert len(d["results"]) == 6


class TestSmokeUAC:
    @patch.object(UACTestWrapper, "_exec")
    def test_full_lifecycle(self, mock_exec):
        mock_exec.return_value = (0, SAMPLE_UAC_OUTPUT, "")
        report = run_tool("uac_test", "/dev/snd/pcmC1D0c", profile="uac2")
        assert report.overall_pass is False  # has 1 fail
        assert report.passed_count == 9
        assert report.failed_count == 1
        assert report.skipped_count == 1
        d = report.to_dict()
        assert d["protocol"] == "uac"
        assert len(d["results"]) == 11


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. All-pass scenario
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ALL_PASS_OUTPUT = """\
TEST-ONVIF-001  GetCapabilities  PASS 0.1s
TEST-ONVIF-002  GetProfiles      PASS 0.1s
TEST-ONVIF-003  GetStreamUri     PASS 0.1s
"""


class TestAllPassScenario:
    @patch.object(ODTTWrapper, "_exec")
    def test_overall_pass_true(self, mock_exec):
        mock_exec.return_value = (0, ALL_PASS_OUTPUT, "")
        report = run_tool("odtt", "192.168.1.100", profile="S")
        assert report.overall_pass is True
        assert report.passed_count == 3
        assert report.failed_count == 0
