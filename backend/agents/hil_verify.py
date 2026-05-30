"""Host-side Android HIL launch verification (OP-1859 / P3.3).

This module runs outside the runner bwrap jail because it talks to a real
Android device over ``adb``. The host must provide ``adb`` and either ``aapt``
or ``apkanalyzer`` so the built APK package id can be read before install.
Medical-tier deliveries are intentionally rejected here: that lane belongs to
the operator's regulated bench, not this scripted consumer/automotive flow.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.agents.product_build import (
    build_product_source_sync as build_product_source,
)
from backend.agents.product_source import resolve_product_source


PACKAGE_RE = re.compile(r"package:\s+name='(?P<package>[^']+)'")


class HilVerifyError(RuntimeError):
    """The on-device HIL verify flow could not be completed."""


@dataclass(frozen=True)
class HilVerifyResult:
    apk_path: Path
    package_id: str
    device: str
    pass_: bool
    fail_reason: str | None
    artifacts_dir: Path


def _run(cmd: list[str], *, text: bool = True):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=text,
        check=False,
    )


def _assert_ok(proc, *, action: str) -> None:
    if proc.returncode == 0:
        return
    detail = "\n".join(
        str(part or "").strip()
        for part in (getattr(proc, "stdout", ""), getattr(proc, "stderr", ""))
        if str(part or "").strip()
    )
    raise HilVerifyError(f"{action} failed with exit {proc.returncode}: {detail}")


def _extract_package_id(apk_path: Path) -> str:
    try:
        proc = _run(["aapt", "dump", "badging", str(apk_path)])
    except FileNotFoundError:
        proc = None
    if proc is not None and proc.returncode == 0:
        match = PACKAGE_RE.search(proc.stdout or "")
        if match:
            return match.group("package")

    try:
        proc = _run(["apkanalyzer", "manifest", "application-id", str(apk_path)])
    except FileNotFoundError as exc:
        raise HilVerifyError(
            "cannot extract APK package id; install aapt or apkanalyzer on the host"
        ) from exc
    _assert_ok(proc, action="apkanalyzer manifest application-id")
    package_id = (proc.stdout or "").strip()
    if not package_id:
        raise HilVerifyError("apkanalyzer returned an empty package id")
    return package_id


def _parse_adb_devices(output: str) -> list[str]:
    devices: list[str] = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def _select_device(device_serial: str | None) -> str:
    if device_serial:
        return device_serial

    proc = _run(["adb", "devices", "-l"])
    _assert_ok(proc, action="adb devices -l")
    devices = _parse_adb_devices(proc.stdout or "")
    if len(devices) != 1:
        raise HilVerifyError(
            "expected exactly one attached adb device; "
            f"found {len(devices)}\n{proc.stdout or ''}"
        )
    return devices[0]


def _adb(device: str, args: list[str], *, text: bool = True):
    return _run(["adb", "-s", device, *args], text=text)


def _crash_line(logcat: str, package_id: str) -> str | None:
    for line in (logcat or "").splitlines():
        if "FATAL EXCEPTION" in line:
            return line.strip()
        if f"AndroidRuntime: Process: {package_id}" in line:
            return line.strip()
    return None


def _is_foregrounded(dumpsys: str, package_id: str) -> bool:
    for line in (dumpsys or "").splitlines():
        lower = line.lower()
        if (
            package_id in line
            and f"{package_id}/" in line
            and (
                "resumed" in lower
                or "foreground" in lower
                or "topresumedactivity" in lower
                or "mfocusedapp" in lower
            )
        ):
            return True
    return False


def _jsonable_result(result: HilVerifyResult) -> dict[str, Any]:
    payload = asdict(result)
    payload["apk_path"] = str(result.apk_path)
    payload["artifacts_dir"] = str(result.artifacts_dir)
    return payload


def _safe_project_key(project_key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", project_key).strip(".-")
    return safe or "project"


def run_hil_verify(
    project_key: str,
    *,
    device_serial: str | None = None,
    workspace_root: Path | None = None,
    capture_seconds: int = 30,
) -> HilVerifyResult:
    source = resolve_product_source(project_key)
    if source is not None and source.tier == "medical":
        raise HilVerifyError(
            "medical-tier HIL is the operator's regulated bench, not this scripted flow"
        )

    root = (
        Path(workspace_root)
        if workspace_root is not None
        else Path(".artifacts/hil-verify")
    )
    root.mkdir(parents=True, exist_ok=True)

    build = build_product_source(project_key, workspace_root=root)
    apk_path = Path(build.apk_path)
    if not apk_path.exists():
        raise HilVerifyError(f"built APK does not exist: {apk_path}")

    package_id = _extract_package_id(apk_path)
    device = _select_device(device_serial)

    proc = _adb(device, ["install", "-r", str(apk_path)])
    _assert_ok(proc, action="adb install")

    proc = _adb(
        device,
        [
            "shell",
            "monkey",
            "-p",
            package_id,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ],
    )
    _assert_ok(proc, action="adb monkey launch")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifacts_dir = root / f"hil-{_safe_project_key(project_key)}-{timestamp}"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    proc = _adb(device, ["logcat", "-c"])
    _assert_ok(proc, action="adb logcat -c")
    if capture_seconds > 0:
        time.sleep(capture_seconds)
    proc = _adb(device, ["logcat", "-d"])
    _assert_ok(proc, action="adb logcat -d")
    logcat = proc.stdout or ""
    (artifacts_dir / "logcat.txt").write_text(logcat, encoding="utf-8")

    proc = _adb(device, ["shell", "screencap", "-p"], text=False)
    if proc.returncode == 0:
        (artifacts_dir / "screen.png").write_bytes(proc.stdout or b"")

    proc = _adb(device, ["shell", "dumpsys", "activity", "activities"])
    _assert_ok(proc, action="adb dumpsys activity activities")
    dumpsys = proc.stdout or ""

    fail_reason = None
    crash = _crash_line(logcat, package_id)
    if crash:
        fail_reason = crash
    elif not _is_foregrounded(dumpsys, package_id):
        fail_reason = f"package {package_id} is not foregrounded"

    result = HilVerifyResult(
        apk_path=apk_path,
        package_id=package_id,
        device=device,
        pass_=fail_reason is None,
        fail_reason=fail_reason,
        artifacts_dir=artifacts_dir,
    )
    (artifacts_dir / "result.json").write_text(
        json.dumps(_jsonable_result(result), indent=2),
        encoding="utf-8",
    )
    return result
