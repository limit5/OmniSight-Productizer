"""C8 — L4-CORE-08 Protocol compliance harness (#217).

Wrappers for external protocol compliance tools (ONVIF ODTT, USB-IF USBCV,
UAC test suite).  Each wrapper shells out to the tool binary in headless /
subprocess mode, parses stdout/stderr into a normalised ``ComplianceReport``,
and logs the result to the audit_log hash-chain.

Public API:
    report = wrapper.run(device_target, profile, **opts)
    await log_compliance_report(report)  # → audit_log
    registry.list_tools() / registry.get(name) / registry.run(name, ...)
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Enums ──────────────────────────────────────────────────────────────

class ComplianceProtocol(str, Enum):
    onvif = "onvif"
    usb = "usb"
    uac = "uac"


class TestVerdict(str, Enum):
    pass_ = "pass"
    fail = "fail"
    error = "error"
    skipped = "skipped"


# ── Normalised report schema ──────────────────────────────────────────

@dataclass
class TestCaseResult:
    test_id: str
    test_name: str
    verdict: TestVerdict
    evidence: str = ""
    duration_s: float = 0.0
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict == TestVerdict.pass_


@dataclass
class ComplianceReport:
    tool_name: str
    protocol: ComplianceProtocol
    device_under_test: str
    timestamp: float = field(default_factory=time.time)
    results: list[TestCaseResult] = field(default_factory=list)
    raw_log_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def overall_pass(self) -> bool:
        return len(self.results) > 0 and all(
            r.verdict in (TestVerdict.pass_, TestVerdict.skipped)
            for r in self.results
        )

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.verdict == TestVerdict.pass_)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if r.verdict == TestVerdict.fail)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.verdict == TestVerdict.error)

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.results if r.verdict == TestVerdict.skipped)

    def summary_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "protocol": self.protocol.value,
            "device_under_test": self.device_under_test,
            "timestamp": self.timestamp,
            "overall_pass": self.overall_pass,
            "total": self.total,
            "passed": self.passed_count,
            "failed": self.failed_count,
            "errors": self.error_count,
            "skipped": self.skipped_count,
            "raw_log_path": self.raw_log_path,
            "metadata": self.metadata,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.summary_dict()
        d["results"] = [
            {
                "test_id": r.test_id,
                "test_name": r.test_name,
                "verdict": r.verdict.value,
                "evidence": r.evidence,
                "duration_s": r.duration_s,
                "message": r.message,
            }
            for r in self.results
        ]
        return d


# ── Tool info ─────────────────────────────────────────────────────────

@dataclass
class ComplianceToolInfo:
    name: str
    protocol: ComplianceProtocol
    version: str
    binary: str
    description: str = ""
    supported_profiles: list[str] = field(default_factory=list)


# ── Shared parser ─────────────────────────────────────────────────────

_VERDICT_MAP = {
    "PASS": TestVerdict.pass_,
    "FAIL": TestVerdict.fail,
    "ERROR": TestVerdict.error,
    "SKIP": TestVerdict.skipped,
}


def _parse_tool_output(raw_output: str, id_pattern: str) -> list[TestCaseResult]:
    results: list[TestCaseResult] = []
    line_re = re.compile(
        r"^[ \t]*(?P<id>" + id_pattern + r")[ \t]+"
        r"(?P<name>.+?)[ \t]+"
        r"(?P<verdict>PASS|FAIL|ERROR|SKIP)"
        r"(?:[ \t]+(?P<time>[\d.]+)s)?"
        r"(?:[ \t]+(?P<msg>.*))?$",
    )
    for line in raw_output.splitlines():
        m = line_re.match(line)
        if m:
            results.append(
                TestCaseResult(
                    test_id=m.group("id"),
                    test_name=m.group("name").strip(),
                    verdict=_VERDICT_MAP.get(m.group("verdict"), TestVerdict.error),
                    duration_s=float(m.group("time") or 0),
                    message=(m.group("msg") or "").strip(),
                )
            )
    return results


# ── Abstract base ─────────────────────────────────────────────────────

class ComplianceTool(ABC):
    tool_info: ComplianceToolInfo

    def check_available(self) -> bool:
        return shutil.which(self.tool_info.binary) is not None

    @abstractmethod
    def run(
        self,
        device_target: str,
        profile: str = "",
        *,
        timeout_s: int = 600,
        work_dir: Optional[str] = None,
        **kwargs: Any,
    ) -> ComplianceReport:
        """Execute the tool against *device_target* and return a normalised report."""

    @abstractmethod
    def parse_output(self, raw_output: str) -> list[TestCaseResult]:
        """Parse raw stdout/stderr into individual test case results."""

    def _exec(
        self,
        cmd: list[str],
        timeout_s: int = 600,
        cwd: Optional[str] = None,
    ) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=cwd,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"timeout after {timeout_s}s"
        except FileNotFoundError:
            return -2, "", f"binary not found: {cmd[0]}"


# ── ODTT wrapper (ONVIF Device Test Tool) ─────────────────────────────

class ODTTWrapper(ComplianceTool):
    """ONVIF Device Test Tool — headless mode subprocess wrapper.

    Expected binary: ``onvif_test_tool`` (or ``ODTT`` on Windows).
    Supports profiles: S (streaming), T (advanced streaming), G (recording),
    C (access control), A (credential), D (door control).
    """

    def __init__(self) -> None:
        self.tool_info = ComplianceToolInfo(
            name="odtt",
            protocol=ComplianceProtocol.onvif,
            version="22.12",
            binary="onvif_test_tool",
            description="ONVIF Device Test Tool — headless compliance testing",
            supported_profiles=["S", "T", "G", "C", "A", "D"],
        )

    def run(
        self,
        device_target: str,
        profile: str = "S",
        *,
        timeout_s: int = 600,
        work_dir: Optional[str] = None,
        **kwargs: Any,
    ) -> ComplianceReport:
        if profile and profile not in self.tool_info.supported_profiles:
            raise ValueError(
                f"Unsupported ONVIF profile '{profile}'. "
                f"Supported: {self.tool_info.supported_profiles}"
            )

        cmd = [
            self.tool_info.binary,
            "--headless",
            "--device", device_target,
        ]
        if profile:
            cmd += ["--profile", profile]
        username = kwargs.get("username", "")
        password = kwargs.get("password", "")
        if username:
            cmd += ["--user", username]
        if password:
            cmd += ["--pass", password]
        output_file = kwargs.get("output_file", "")
        if output_file:
            cmd += ["--output", output_file]

        rc, stdout, stderr = self._exec(cmd, timeout_s=timeout_s, cwd=work_dir)
        raw = stdout + stderr

        results = self.parse_output(raw)
        if rc == -2:
            results = [
                TestCaseResult(
                    test_id="ODTT-AVAIL",
                    test_name="Tool availability check",
                    verdict=TestVerdict.error,
                    message=stderr,
                )
            ]

        report = ComplianceReport(
            tool_name=self.tool_info.name,
            protocol=self.tool_info.protocol,
            device_under_test=device_target,
            results=results,
            raw_log_path=output_file or "",
            metadata={
                "profile": profile,
                "return_code": rc,
                "username": username,
            },
        )
        return report

    def parse_output(self, raw_output: str) -> list[TestCaseResult]:
        return _parse_tool_output(raw_output, r"TEST-\S+")


# ── USBCV wrapper (USB-IF USB Command Verifier) ──────────────────────

class USBCVWrapper(ComplianceTool):
    """USB-IF USBCV — USB Command Verifier for USB compliance testing.

    Expected binary: ``usbcv`` (CLI mode).
    Supports test classes: device, hub, hid, video, audio, mass_storage.
    """

    def __init__(self) -> None:
        self.tool_info = ComplianceToolInfo(
            name="usbcv",
            protocol=ComplianceProtocol.usb,
            version="4.1.2",
            binary="usbcv",
            description="USB-IF USB Command Verifier — USB compliance testing",
            supported_profiles=["device", "hub", "hid", "video", "audio", "mass_storage"],
        )

    def run(
        self,
        device_target: str,
        profile: str = "device",
        *,
        timeout_s: int = 600,
        work_dir: Optional[str] = None,
        **kwargs: Any,
    ) -> ComplianceReport:
        if profile and profile not in self.tool_info.supported_profiles:
            raise ValueError(
                f"Unsupported USB test class '{profile}'. "
                f"Supported: {self.tool_info.supported_profiles}"
            )

        cmd = [
            self.tool_info.binary,
            "--cli",
            "--device", device_target,
        ]
        if profile:
            cmd += ["--class", profile]
        vid = kwargs.get("vid", "")
        pid = kwargs.get("pid", "")
        if vid:
            cmd += ["--vid", vid]
        if pid:
            cmd += ["--pid", pid]
        output_file = kwargs.get("output_file", "")
        if output_file:
            cmd += ["--report", output_file]

        rc, stdout, stderr = self._exec(cmd, timeout_s=timeout_s, cwd=work_dir)
        raw = stdout + stderr

        results = self.parse_output(raw)
        if rc == -2:
            results = [
                TestCaseResult(
                    test_id="USBCV-AVAIL",
                    test_name="Tool availability check",
                    verdict=TestVerdict.error,
                    message=stderr,
                )
            ]

        report = ComplianceReport(
            tool_name=self.tool_info.name,
            protocol=self.tool_info.protocol,
            device_under_test=device_target,
            results=results,
            raw_log_path=output_file or "",
            metadata={
                "test_class": profile,
                "return_code": rc,
                "vid": vid,
                "pid": pid,
            },
        )
        return report

    def parse_output(self, raw_output: str) -> list[TestCaseResult]:
        return _parse_tool_output(raw_output, r"USB-\S+")


# ── UAC test wrapper (USB Audio Class) ────────────────────────────────

class UACTestWrapper(ComplianceTool):
    """UAC (USB Audio Class) test suite wrapper.

    Expected binary: ``uac_test`` (headless CLI).
    Supports UAC versions: uac1, uac2.
    Tests: descriptor validation, sample rate switching, volume control,
    streaming endpoint verification.
    """

    def __init__(self) -> None:
        self.tool_info = ComplianceToolInfo(
            name="uac_test",
            protocol=ComplianceProtocol.uac,
            version="1.0.0",
            binary="uac_test",
            description="USB Audio Class compliance test suite",
            supported_profiles=["uac1", "uac2"],
        )

    def run(
        self,
        device_target: str,
        profile: str = "uac2",
        *,
        timeout_s: int = 600,
        work_dir: Optional[str] = None,
        **kwargs: Any,
    ) -> ComplianceReport:
        if profile and profile not in self.tool_info.supported_profiles:
            raise ValueError(
                f"Unsupported UAC version '{profile}'. "
                f"Supported: {self.tool_info.supported_profiles}"
            )

        cmd = [
            self.tool_info.binary,
            "--headless",
            "--device", device_target,
        ]
        if profile:
            cmd += ["--version", profile]
        sample_rate = kwargs.get("sample_rate", "")
        channels = kwargs.get("channels", "")
        if sample_rate:
            cmd += ["--sample-rate", str(sample_rate)]
        if channels:
            cmd += ["--channels", str(channels)]
        output_file = kwargs.get("output_file", "")
        if output_file:
            cmd += ["--output", output_file]

        rc, stdout, stderr = self._exec(cmd, timeout_s=timeout_s, cwd=work_dir)
        raw = stdout + stderr

        results = self.parse_output(raw)
        if rc == -2:
            results = [
                TestCaseResult(
                    test_id="UAC-AVAIL",
                    test_name="Tool availability check",
                    verdict=TestVerdict.error,
                    message=stderr,
                )
            ]

        report = ComplianceReport(
            tool_name=self.tool_info.name,
            protocol=self.tool_info.protocol,
            device_under_test=device_target,
            results=results,
            raw_log_path=output_file or "",
            metadata={
                "uac_version": profile,
                "return_code": rc,
                "sample_rate": sample_rate,
                "channels": channels,
            },
        )
        return report

    def parse_output(self, raw_output: str) -> list[TestCaseResult]:
        return _parse_tool_output(raw_output, r"UAC-\S+")


# ── Registry ──────────────────────────────────────────────────────────

_BUILTIN_TOOLS: dict[str, type[ComplianceTool]] = {
    "odtt": ODTTWrapper,
    "usbcv": USBCVWrapper,
    "uac_test": UACTestWrapper,
}

_CUSTOM_TOOLS: dict[str, type[ComplianceTool]] = {}


def register_tool(name: str, cls: type[ComplianceTool]) -> None:
    _CUSTOM_TOOLS[name] = cls


def list_tools() -> list[ComplianceToolInfo]:
    infos = []
    for cls in {**_BUILTIN_TOOLS, **_CUSTOM_TOOLS}.values():
        inst = cls()
        infos.append(inst.tool_info)
    return infos


def get_tool(name: str) -> ComplianceTool:
    all_tools = {**_BUILTIN_TOOLS, **_CUSTOM_TOOLS}
    if name not in all_tools:
        raise KeyError(f"Unknown compliance tool: {name}. Available: {list(all_tools)}")
    return all_tools[name]()


def run_tool(
    name: str,
    device_target: str,
    profile: str = "",
    *,
    timeout_s: int = 600,
    work_dir: Optional[str] = None,
    **kwargs: Any,
) -> ComplianceReport:
    tool = get_tool(name)
    return tool.run(
        device_target, profile,
        timeout_s=timeout_s, work_dir=work_dir, **kwargs,
    )


# ── Audit log integration ────────────────────────────────────────────

async def log_compliance_report(report: ComplianceReport) -> Optional[int]:
    """Write a compliance report summary to the audit_log hash-chain."""
    try:
        from backend import audit
        return await audit.log(
            action="compliance_test",
            entity_kind="compliance_report",
            entity_id=f"{report.tool_name}:{report.device_under_test}",
            before=None,
            after=report.to_dict(),
            actor="compliance_harness",
        )
    except Exception as exc:
        logger.warning("Failed to log compliance report to audit: %s", exc)
        return None


def log_compliance_report_sync(report: ComplianceReport) -> None:
    """Fire-and-forget for sync callers."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("log_compliance_report_sync skipped (no running loop)")
        return
    loop.create_task(log_compliance_report(report))
