"""OP-1859 (P3.3) — host-side Android HIL launch verification."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agents import hil_verify as hv
from backend.agents import product_source as ps


PACKAGE_ID = "com.operator.camviewpro"


def _source(tier: str = "consumer") -> ps.ProductSource:
    return ps.ProductSource(
        repo_url="https://github.com/operator/camviewpro-android.git",
        tier=tier,
        branch="main",
        pinned_ref="c360a9d0",
        git_account_ref="camviewpro-ro",
    )


def _build_result(apk_path: Path):
    return SimpleNamespace(apk_path=apk_path)


class RecordingRun:
    def __init__(
        self,
        *,
        devices: str | None = None,
        logcat: str = "",
        dumpsys: str | None = None,
    ):
        self.calls: list[list[str]] = []
        self.devices = devices or (
            "List of devices attached\n"
            "R5CW12345 device product:a36 model:Galaxy_A36 transport_id:1\n"
        )
        self.logcat = logcat
        self.dumpsys = dumpsys or (
            f"  topResumedActivity=ActivityRecord{{abc u0 {PACKAGE_ID}/.MainActivity t1}}\n"
        )

    def __call__(self, cmd, *args, **kwargs):
        argv = list(cmd)
        self.calls.append(argv)
        text = kwargs.get("text", True)
        if argv[:3] == ["aapt", "dump", "badging"]:
            return SimpleNamespace(
                returncode=0,
                stdout=f"package: name='{PACKAGE_ID}' versionCode='1'\n",
                stderr="",
            )
        if argv == ["adb", "devices", "-l"]:
            return SimpleNamespace(returncode=0, stdout=self.devices, stderr="")
        if argv[-2:] == ["logcat", "-d"]:
            return SimpleNamespace(returncode=0, stdout=self.logcat, stderr="")
        if argv[-3:] == ["dumpsys", "activity", "activities"]:
            return SimpleNamespace(returncode=0, stdout=self.dumpsys, stderr="")
        if argv[-3:] == ["shell", "screencap", "-p"]:
            stdout = b"png" if not text else "png"
            stderr = b"" if not text else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)
        return SimpleNamespace(returncode=0, stdout="" if text else b"", stderr="")


def _prepare(
    tmp_path,
    monkeypatch,
    *,
    tier: str = "consumer",
    run: RecordingRun | None = None,
):
    apk = tmp_path / "consumer-debug.apk"
    apk.write_text("apk", encoding="utf-8")
    monkeypatch.setattr(hv, "resolve_product_source", lambda key: _source(tier))
    monkeypatch.setattr(
        hv,
        "build_product_source",
        lambda key, *, workspace_root: _build_result(apk),
    )
    recorder = run or RecordingRun()
    monkeypatch.setattr(hv.subprocess, "run", recorder)
    monkeypatch.setattr(hv.time, "sleep", lambda seconds: None)
    return apk, recorder


def test_medical_guardrail_rejects_before_adb(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(hv, "resolve_product_source", lambda key: _source("medical"))
    monkeypatch.setattr(
        hv.subprocess,
        "run",
        lambda cmd, *args, **kwargs: calls.append(list(cmd)),
    )

    with pytest.raises(hv.HilVerifyError, match="medical-tier HIL"):
        hv.run_hil_verify("DEMO", workspace_root=tmp_path, capture_seconds=0)

    assert calls == []


def test_happy_path_creates_artifacts(tmp_path, monkeypatch):
    _, recorder = _prepare(tmp_path, monkeypatch)

    result = hv.run_hil_verify("DEMO", workspace_root=tmp_path, capture_seconds=0)

    assert result.pass_ is True
    assert result.package_id == PACKAGE_ID
    assert result.device == "R5CW12345"
    assert result.artifacts_dir.exists()
    assert (result.artifacts_dir / "logcat.txt").exists()
    payload = json.loads((result.artifacts_dir / "result.json").read_text())
    assert payload["pass_"] is True
    assert [
        "adb",
        "-s",
        "R5CW12345",
        "install",
        "-r",
        str(result.apk_path),
    ] in recorder.calls


def test_crash_detected_sets_failed_result(tmp_path, monkeypatch):
    run = RecordingRun(
        logcat=(
            "FATAL EXCEPTION: main\n"
            f"AndroidRuntime: Process: {PACKAGE_ID}, PID: 1234\n"
        )
    )
    _prepare(tmp_path, monkeypatch, run=run)

    result = hv.run_hil_verify("DEMO", workspace_root=tmp_path, capture_seconds=0)

    assert result.pass_ is False
    assert "FATAL EXCEPTION" in result.fail_reason


def test_not_foregrounded_sets_failed_result(tmp_path, monkeypatch):
    run = RecordingRun(
        dumpsys="  topResumedActivity=ActivityRecord{abc u0 com.other/.MainActivity t1}\n"
    )
    _prepare(tmp_path, monkeypatch, run=run)

    result = hv.run_hil_verify("DEMO", workspace_root=tmp_path, capture_seconds=0)

    assert result.pass_ is False
    assert "not foregrounded" in result.fail_reason


def test_no_device_raises(tmp_path, monkeypatch):
    run = RecordingRun(devices="List of devices attached\n\n")
    _prepare(tmp_path, monkeypatch, run=run)

    with pytest.raises(hv.HilVerifyError, match="expected exactly one"):
        hv.run_hil_verify("DEMO", workspace_root=tmp_path, capture_seconds=0)


def test_multiple_devices_without_serial_raises(tmp_path, monkeypatch):
    run = RecordingRun(
        devices=(
            "List of devices attached\n"
            "one device product:a\n"
            "two device product:b\n"
        )
    )
    _prepare(tmp_path, monkeypatch, run=run)

    with pytest.raises(hv.HilVerifyError, match="expected exactly one"):
        hv.run_hil_verify("DEMO", workspace_root=tmp_path, capture_seconds=0)


def test_missing_apk_raises(tmp_path, monkeypatch):
    missing = tmp_path / "missing.apk"
    monkeypatch.setattr(hv, "resolve_product_source", lambda key: _source("consumer"))
    monkeypatch.setattr(
        hv,
        "build_product_source",
        lambda key, *, workspace_root: _build_result(missing),
    )
    monkeypatch.setattr(hv.subprocess, "run", RecordingRun())

    with pytest.raises(hv.HilVerifyError, match="built APK does not exist"):
        hv.run_hil_verify("DEMO", workspace_root=tmp_path, capture_seconds=0)


def test_cli_smoke_dispatches(monkeypatch, tmp_path, capsys):
    script = Path(__file__).resolve().parents[2] / "scripts" / "hil_verify.py"
    spec = importlib.util.spec_from_file_location("hil_verify_cli", script)
    cli = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(cli)

    result = hv.HilVerifyResult(
        apk_path=tmp_path / "demo.apk",
        package_id=PACKAGE_ID,
        device="SERIAL",
        pass_=True,
        fail_reason=None,
        artifacts_dir=tmp_path / "artifacts",
    )
    seen = {}

    def fake_run(
        project_key,
        *,
        device_serial=None,
        workspace_root=None,
        capture_seconds=30,
    ):
        seen.update(
            {
                "project_key": project_key,
                "device_serial": device_serial,
                "workspace_root": workspace_root,
                "capture_seconds": capture_seconds,
            }
        )
        return result

    monkeypatch.setattr(cli, "run_hil_verify", fake_run)

    rc = cli.main(
        [
            "DEMO",
            "--device",
            "SERIAL",
            "--workspace",
            str(tmp_path),
            "--seconds",
            "1",
        ]
    )

    assert rc == 0
    assert seen == {
        "project_key": "DEMO",
        "device_serial": "SERIAL",
        "workspace_root": tmp_path,
        "capture_seconds": 1,
    }
    assert "artifacts:" in capsys.readouterr().out
