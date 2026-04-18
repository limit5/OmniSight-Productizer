"""V6 #2 (issue #322) — ``mobile_screenshot`` contract tests.

Pins ``backend/mobile_screenshot.py`` against:

* structural invariants (``__all__`` membership, schema version,
  defaults, supported platforms/formats, status enum);
* :class:`ScreenshotRequest` validation (session-id charset, platform
  enum, absolute paths, positive timeouts, UDID shape, env types,
  format enum, `is_remote_ios` property, JSON-safe ``to_dict``);
* :class:`ScreenshotResult` guards (status enum, non-negative
  dimensions + bytes + duration, ``ok`` / ``has_real_capture``
  properties, ``to_dict`` drops binary bytes);
* deterministic argv builders (same input → byte-identical list);
* security — ``ssh`` remote capture argv shell-quotes every token;
* :func:`parse_png_dimensions` — happy-path, bad magic, truncated
  bytes, wrong IHDR, None, empty, non-bytes;
* :func:`capture_android` happy path, missing adb → ``mock``, capture
  rc!=0 → ``fail``, pull rc!=0 → ``fail``, timeouts → ``fail``,
  arbitrary executor exceptions → ``fail``;
* :func:`capture_ios` local happy path, remote-ssh happy path,
  missing xcrun/ssh/scp → ``mock``, rc!=0 → ``fail``, timeouts →
  ``fail``;
* :func:`capture` dispatch to the right platform + TypeError on
  non-``ScreenshotRequest``;
* ``attach_bytes=False`` suppresses byte payload while keeping status;
* byte-sniffing fallback — empty file → ``fail`` "empty file";
  non-PNG bytes → ``fail`` "bad IHDR";
* :func:`render_screenshot_result_markdown` renders expected lines.

No real adb / xcrun / ssh / scp is touched — we inject a ``FakeRunner``
that records every argv + serves canned :class:`CompletedProcess`.
"""

from __future__ import annotations

import json
import os
import shlex
import struct
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from backend import mobile_screenshot as mss
from backend.mobile_screenshot import (
    DEFAULT_ANDROID_REMOTE_PATH,
    DEFAULT_IOS_UDID,
    DEFAULT_SCREENSHOT_TIMEOUT_S,
    MOBILE_SCREENSHOT_SCHEMA_VERSION,
    PNG_MAGIC,
    SUPPORTED_FORMATS,
    SUPPORTED_PLATFORMS,
    MobileScreenshotCaptureError,
    MobileScreenshotConfigError,
    MobileScreenshotError,
    MobileScreenshotTimeout,
    MobileScreenshotToolMissing,
    ScreenshotRequest,
    ScreenshotResult,
    ScreenshotStatus,
    build_android_capture_argv,
    build_android_pull_argv,
    build_ios_capture_argv,
    build_ios_remote_capture_argv,
    build_ios_scp_argv,
    capture,
    capture_android,
    capture_ios,
    parse_png_dimensions,
    render_screenshot_result_markdown,
)


# ── Module invariants ─────────────────────────────────────────────


EXPECTED_ALL = {
    "MOBILE_SCREENSHOT_SCHEMA_VERSION",
    "DEFAULT_SCREENSHOT_TIMEOUT_S",
    "DEFAULT_ANDROID_REMOTE_PATH",
    "DEFAULT_IOS_UDID",
    "SUPPORTED_PLATFORMS",
    "SUPPORTED_FORMATS",
    "PNG_MAGIC",
    "ScreenshotStatus",
    "ScreenshotRequest",
    "ScreenshotResult",
    "MobileScreenshotError",
    "MobileScreenshotConfigError",
    "MobileScreenshotToolMissing",
    "MobileScreenshotTimeout",
    "MobileScreenshotCaptureError",
    "build_android_capture_argv",
    "build_android_pull_argv",
    "build_ios_capture_argv",
    "build_ios_remote_capture_argv",
    "build_ios_scp_argv",
    "parse_png_dimensions",
    "capture_android",
    "capture_ios",
    "capture",
    "render_screenshot_result_markdown",
}


def test_all_matches_expected():
    assert set(mss.__all__) == EXPECTED_ALL


def test_schema_version_is_semver():
    parts = MOBILE_SCREENSHOT_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_supported_platforms_stable():
    assert SUPPORTED_PLATFORMS == ("android", "ios")


def test_supported_formats_stable():
    assert SUPPORTED_FORMATS == ("png",)


def test_defaults_sane():
    assert DEFAULT_SCREENSHOT_TIMEOUT_S > 0
    assert DEFAULT_ANDROID_REMOTE_PATH.startswith("/")
    assert DEFAULT_IOS_UDID == "booted"


def test_png_magic_shape():
    assert isinstance(PNG_MAGIC, bytes)
    assert len(PNG_MAGIC) == 8
    assert PNG_MAGIC == b"\x89PNG\r\n\x1a\n"


def test_status_enum_complete():
    values = {s.value for s in ScreenshotStatus}
    assert values == {"pass", "fail", "skip", "mock"}


def test_errors_hierarchy():
    assert issubclass(MobileScreenshotConfigError, MobileScreenshotError)
    assert issubclass(MobileScreenshotToolMissing, MobileScreenshotError)
    assert issubclass(MobileScreenshotTimeout, MobileScreenshotError)
    assert issubclass(MobileScreenshotCaptureError, MobileScreenshotError)
    assert issubclass(MobileScreenshotError, RuntimeError)


# ── Test fixtures / helpers ───────────────────────────────────────


def _make_png_bytes(width: int = 4, height: int = 2) -> bytes:
    """Build a minimal but IHDR-valid PNG byte stream for tests.

    We only populate the magic + IHDR chunk length/type/width/height +
    a single CRC filler — enough for :func:`parse_png_dimensions` to
    succeed. Full PNG decoding (IDAT / IEND) is out of scope here
    because the module never calls an image decoder.
    """
    ihdr_len = struct.pack(">I", 13)
    ihdr_type = b"IHDR"
    ihdr_body = struct.pack(
        ">IIBBBBB",
        width, height,
        8,     # bit depth
        6,     # color type (RGBA)
        0, 0, 0,
    )
    ihdr_crc = b"\x00\x00\x00\x00"
    # Minimum additional chunks so the file looks like a PNG to humans
    idat_len = struct.pack(">I", 0)
    idat_type = b"IDAT"
    idat_crc = b"\x00\x00\x00\x00"
    iend_len = struct.pack(">I", 0)
    iend_type = b"IEND"
    iend_crc = b"\xae\x42\x60\x82"
    return (
        PNG_MAGIC
        + ihdr_len + ihdr_type + ihdr_body + ihdr_crc
        + idat_len + idat_type + idat_crc
        + iend_len + iend_type + iend_crc
    )


@pytest.fixture
def fake_png() -> bytes:
    return _make_png_bytes(1080, 1920)


@pytest.fixture
def android_request(tmp_path: Path) -> ScreenshotRequest:
    return ScreenshotRequest(
        session_id="sess-1",
        platform="android",
        output_path=str(tmp_path / "out" / "sess-1.png"),
    )


@pytest.fixture
def ios_request(tmp_path: Path) -> ScreenshotRequest:
    return ScreenshotRequest(
        session_id="sess-ios",
        platform="ios",
        output_path=str(tmp_path / "out" / "sess-ios.png"),
    )


@pytest.fixture
def remote_ios_request(tmp_path: Path) -> ScreenshotRequest:
    return ScreenshotRequest(
        session_id="sess-remote",
        platform="ios",
        output_path=str(tmp_path / "out" / "sess-remote.png"),
        ios_remote_host="builder@mac-runner",
    )


class _FakeProc:
    """Stand-in for :class:`subprocess.CompletedProcess`."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Records every ``runner(argv, ...)`` call + serves canned
    returns or raises canned exceptions."""

    def __init__(
        self, *,
        returns: list[_FakeProc | BaseException] | None = None,
        side_effect_on_disk: list[tuple[str, bytes]] | None = None,
    ):
        self.calls: list[dict[str, Any]] = []
        self._returns = list(returns or [])
        self._side_effect_on_disk = list(side_effect_on_disk or [])

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": dict(kwargs)})
        # Side effect: when a call is meant to simulate "adb pull wrote
        # the file", we drop bytes at the declared path.
        if self._side_effect_on_disk:
            path, payload = self._side_effect_on_disk.pop(0)
            if path:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(payload)
        if not self._returns:
            return _FakeProc(returncode=0, stdout="", stderr="")
        nxt = self._returns.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


# ── ScreenshotRequest validation ──────────────────────────────────


def test_request_happy_android(tmp_path: Path):
    r = ScreenshotRequest(
        session_id="s1", platform="android",
        output_path=str(tmp_path / "x.png"),
    )
    assert r.platform == "android"
    assert r.output_path.endswith("x.png")
    assert r.is_remote_ios is False


def test_request_happy_ios(tmp_path: Path):
    r = ScreenshotRequest(
        session_id="s1", platform="ios",
        output_path=str(tmp_path / "x.png"),
        ios_udid="booted",
    )
    assert r.platform == "ios"
    assert r.ios_udid == "booted"
    assert r.is_remote_ios is False


def test_request_is_remote_ios_true_when_host_set(tmp_path: Path):
    r = ScreenshotRequest(
        session_id="s1", platform="ios",
        output_path=str(tmp_path / "x.png"),
        ios_remote_host="mac@host",
    )
    assert r.is_remote_ios is True


def test_request_is_frozen(tmp_path: Path):
    r = ScreenshotRequest(
        session_id="s1", platform="android",
        output_path=str(tmp_path / "x.png"),
    )
    with pytest.raises(FrozenInstanceError):
        r.platform = "ios"  # type: ignore[misc]


def test_request_rejects_empty_session_id(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="", platform="android",
            output_path=str(tmp_path / "x.png"),
        )


def test_request_rejects_non_string_session_id(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id=123,  # type: ignore[arg-type]
            platform="android",
            output_path=str(tmp_path / "x.png"),
        )


def test_request_rejects_bad_session_chars(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="sess space",
            platform="android",
            output_path=str(tmp_path / "x.png"),
        )


def test_request_rejects_unknown_platform(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="s1", platform="symbian",
            output_path=str(tmp_path / "x.png"),
        )


def test_request_normalises_platform_case(tmp_path: Path):
    r = ScreenshotRequest(
        session_id="s1", platform="ANDROID",
        output_path=str(tmp_path / "x.png"),
    )
    assert r.platform == "android"


def test_request_rejects_empty_output_path(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="s1", platform="android", output_path="",
        )


def test_request_rejects_relative_output_path(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="s1", platform="android",
            output_path="relative/x.png",
        )


@pytest.mark.parametrize("bad", [0, -1, "oops"])
def test_request_rejects_non_positive_timeout(tmp_path: Path, bad: Any):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="s1", platform="android",
            output_path=str(tmp_path / "x.png"),
            timeout_s=bad,
        )


def test_request_rejects_relative_android_remote_path(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="s1", platform="android",
            output_path=str(tmp_path / "x.png"),
            android_remote_path="tmp/out.png",
        )


def test_request_rejects_bad_android_serial(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="s1", platform="android",
            output_path=str(tmp_path / "x.png"),
            android_serial="bad serial with spaces",
        )


def test_request_rejects_empty_ios_udid(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="s1", platform="ios",
            output_path=str(tmp_path / "x.png"),
            ios_udid="",
        )


def test_request_rejects_bad_ios_udid_chars(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="s1", platform="ios",
            output_path=str(tmp_path / "x.png"),
            ios_udid="BAD UDID",
        )


def test_request_accepts_uuid_udid(tmp_path: Path):
    r = ScreenshotRequest(
        session_id="s1", platform="ios",
        output_path=str(tmp_path / "x.png"),
        ios_udid="ABCDEF12-3456-7890-ABCD-EF1234567890",
    )
    assert r.ios_udid.startswith("ABCDEF")


def test_request_rejects_relative_ios_remote_tmp(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="s1", platform="ios",
            output_path=str(tmp_path / "x.png"),
            ios_remote_tmp_dir="tmp",
        )


def test_request_rejects_unknown_format(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="s1", platform="android",
            output_path=str(tmp_path / "x.png"),
            format="jpg",
        )


def test_request_rejects_non_mapping_env(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="s1", platform="android",
            output_path=str(tmp_path / "x.png"),
            env=[("KEY", "VALUE")],  # type: ignore[arg-type]
        )


def test_request_rejects_non_string_env_values(tmp_path: Path):
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotRequest(
            session_id="s1", platform="android",
            output_path=str(tmp_path / "x.png"),
            env={"K": 1},  # type: ignore[dict-item]
        )


def test_request_env_is_readonly(tmp_path: Path):
    orig = {"PATH": "/usr/local/bin"}
    r = ScreenshotRequest(
        session_id="s1", platform="android",
        output_path=str(tmp_path / "x.png"),
        env=orig,
    )
    with pytest.raises(TypeError):
        r.env["MUTATE"] = "x"  # type: ignore[index]


def test_request_to_dict_json_safe(tmp_path: Path):
    r = ScreenshotRequest(
        session_id="s1", platform="android",
        output_path=str(tmp_path / "x.png"),
        env={"K": "V"},
    )
    payload = r.to_dict()
    json.dumps(payload)
    assert payload["schema_version"] == MOBILE_SCREENSHOT_SCHEMA_VERSION
    assert payload["platform"] == "android"
    assert payload["env"] == {"K": "V"}


# ── ScreenshotResult validation ───────────────────────────────────


def test_result_default_is_skip():
    r = ScreenshotResult(session_id="s1", platform="android")
    assert r.status is ScreenshotStatus.skip
    assert r.ok is False
    assert r.has_real_capture is False
    assert r.format == "png"


def test_result_is_frozen():
    r = ScreenshotResult(session_id="s1", platform="android")
    with pytest.raises(FrozenInstanceError):
        r.status = ScreenshotStatus.passed  # type: ignore[misc]


def test_result_rejects_non_enum_status():
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotResult(session_id="s1", platform="android", status="pass")  # type: ignore[arg-type]


def test_result_rejects_bad_platform():
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotResult(session_id="s1", platform="windows")


@pytest.mark.parametrize("field,value", [
    ("width", -1),
    ("height", -1),
    ("size_bytes", -1),
    ("duration_ms", -1),
    ("captured_at", -1.0),
])
def test_result_rejects_negative_numbers(field: str, value: Any):
    kwargs = {"session_id": "s1", "platform": "android", field: value}
    with pytest.raises(MobileScreenshotConfigError):
        ScreenshotResult(**kwargs)


def test_result_ok_only_when_status_pass():
    r = ScreenshotResult(
        session_id="s1", platform="android",
        status=ScreenshotStatus.passed,
    )
    assert r.ok is True
    assert r.has_real_capture is True


def test_result_has_real_capture_excludes_skip_mock():
    r_mock = ScreenshotResult(
        session_id="s1", platform="android", status=ScreenshotStatus.mock,
    )
    r_skip = ScreenshotResult(
        session_id="s1", platform="android", status=ScreenshotStatus.skip,
    )
    r_fail = ScreenshotResult(
        session_id="s1", platform="android", status=ScreenshotStatus.fail,
    )
    assert r_mock.has_real_capture is False
    assert r_skip.has_real_capture is False
    assert r_fail.has_real_capture is True


def test_result_to_dict_json_safe_and_drops_bytes():
    r = ScreenshotResult(
        session_id="s1", platform="android",
        status=ScreenshotStatus.passed,
        path="/tmp/x.png", width=10, height=20, size_bytes=123,
        duration_ms=45, captured_at=1700.0, detail="ok",
        png_bytes=b"\x00\x01\x02",
    )
    payload = r.to_dict()
    blob = json.dumps(payload)
    assert "png_bytes" not in payload  # binary not in dict
    assert payload["has_bytes"] is True
    assert payload["status"] == "pass"
    assert payload["ok"] is True
    assert payload["schema_version"] == MOBILE_SCREENSHOT_SCHEMA_VERSION
    assert "\x00" not in blob


# ── Pure argv builders ────────────────────────────────────────────


def test_build_android_capture_argv_default():
    argv = build_android_capture_argv()
    assert argv == ["adb", "shell", "screencap", "-p", DEFAULT_ANDROID_REMOTE_PATH]


def test_build_android_capture_argv_with_serial():
    argv = build_android_capture_argv(serial="emulator-5554")
    assert argv[:3] == ["adb", "-s", "emulator-5554"]
    assert "screencap" in argv


def test_build_android_capture_argv_deterministic():
    a1 = build_android_capture_argv("/sdcard/foo.png", serial="abc")
    a2 = build_android_capture_argv("/sdcard/foo.png", serial="abc")
    assert a1 == a2


def test_build_android_capture_argv_rejects_relative():
    with pytest.raises(MobileScreenshotConfigError):
        build_android_capture_argv("tmp/out.png")


def test_build_android_capture_argv_rejects_bad_serial():
    with pytest.raises(MobileScreenshotConfigError):
        build_android_capture_argv(serial="bad; rm -rf /")


def test_build_android_pull_argv_default():
    argv = build_android_pull_argv("/sdcard/out.png", "/tmp/local.png")
    assert argv == ["adb", "pull", "/sdcard/out.png", "/tmp/local.png"]


def test_build_android_pull_argv_with_serial():
    argv = build_android_pull_argv(
        "/sdcard/out.png", "/tmp/x.png", serial="emulator-5556",
    )
    assert argv[:3] == ["adb", "-s", "emulator-5556"]


def test_build_android_pull_argv_rejects_empty_local():
    with pytest.raises(MobileScreenshotConfigError):
        build_android_pull_argv("/sdcard/a.png", "")


def test_build_android_pull_argv_rejects_relative_remote():
    with pytest.raises(MobileScreenshotConfigError):
        build_android_pull_argv("relative.png", "/tmp/x.png")


def test_build_ios_capture_argv_default():
    argv = build_ios_capture_argv("/tmp/out.png")
    assert argv == ["xcrun", "simctl", "io", "booted", "screenshot", "/tmp/out.png"]


def test_build_ios_capture_argv_with_udid():
    argv = build_ios_capture_argv("/tmp/out.png", udid="ABC-DEF-123")
    assert "ABC-DEF-123" in argv


def test_build_ios_capture_argv_deterministic():
    a1 = build_ios_capture_argv("/tmp/a.png", udid="booted")
    a2 = build_ios_capture_argv("/tmp/a.png", udid="booted")
    assert a1 == a2


def test_build_ios_capture_argv_rejects_empty_output():
    with pytest.raises(MobileScreenshotConfigError):
        build_ios_capture_argv("")


def test_build_ios_capture_argv_rejects_bad_udid():
    with pytest.raises(MobileScreenshotConfigError):
        build_ios_capture_argv("/tmp/x.png", udid="bad udid!")


def test_build_ios_remote_capture_argv_deterministic():
    a1 = build_ios_remote_capture_argv("mac@runner", "/tmp/out.png")
    a2 = build_ios_remote_capture_argv("mac@runner", "/tmp/out.png")
    assert a1 == a2


def test_build_ios_remote_capture_argv_shape():
    argv = build_ios_remote_capture_argv("mac@runner", "/tmp/out.png")
    assert argv[0] == "ssh"
    assert "mac@runner" in argv
    assert "--" in argv
    # The inner xcrun command is a single shell-quoted string, not
    # individual argv tokens.
    assert any("xcrun" in t for t in argv)


def test_build_ios_remote_capture_argv_includes_batch_flags():
    argv = build_ios_remote_capture_argv("mac@runner", "/tmp/out.png")
    joined = " ".join(argv)
    assert "BatchMode=yes" in joined
    assert "StrictHostKeyChecking=accept-new" in joined


def test_build_ios_remote_capture_argv_shell_quotes_tokens():
    # If a path contained spaces/quotes the ssh argv should wrap the
    # inner command in shlex.quote so the remote shell doesn't split
    # them apart.
    argv = build_ios_remote_capture_argv(
        "mac@runner", "/tmp/has spaces/out.png",
    )
    # The last token is the shell-quoted remote command. Confirm
    # shlex.split round-trips back to something containing our path.
    cmd = argv[-1]
    tokens = shlex.split(cmd)
    assert "/tmp/has spaces/out.png" in tokens


def test_build_ios_remote_capture_argv_rejects_empty_host():
    with pytest.raises(MobileScreenshotConfigError):
        build_ios_remote_capture_argv("", "/tmp/a.png")


def test_build_ios_remote_capture_argv_rejects_relative_remote():
    with pytest.raises(MobileScreenshotConfigError):
        build_ios_remote_capture_argv("mac@runner", "relative.png")


def test_build_ios_scp_argv_shape():
    argv = build_ios_scp_argv(
        "mac@runner", "/tmp/remote.png", "/host/local.png",
    )
    assert argv == ["scp", "mac@runner:/tmp/remote.png", "/host/local.png"]


def test_build_ios_scp_argv_rejects_empty_host():
    with pytest.raises(MobileScreenshotConfigError):
        build_ios_scp_argv("", "/tmp/r.png", "/host/local.png")


def test_build_ios_scp_argv_rejects_relative_remote():
    with pytest.raises(MobileScreenshotConfigError):
        build_ios_scp_argv("mac", "r.png", "/host/l.png")


# ── parse_png_dimensions ──────────────────────────────────────────


def test_parse_png_dimensions_happy(fake_png: bytes):
    w, h = parse_png_dimensions(fake_png)
    assert (w, h) == (1080, 1920)


def test_parse_png_dimensions_small():
    tiny = _make_png_bytes(4, 7)
    assert parse_png_dimensions(tiny) == (4, 7)


def test_parse_png_dimensions_none_safe():
    assert parse_png_dimensions(None) == (0, 0)


def test_parse_png_dimensions_empty_safe():
    assert parse_png_dimensions(b"") == (0, 0)


def test_parse_png_dimensions_short_safe():
    assert parse_png_dimensions(b"short") == (0, 0)


def test_parse_png_dimensions_bad_magic():
    payload = b"NOPE" * 10
    assert parse_png_dimensions(payload) == (0, 0)


def test_parse_png_dimensions_bad_ihdr():
    # Magic correct, but chunk type is not IHDR.
    data = PNG_MAGIC + struct.pack(">I", 13) + b"OOPS" + b"\x00" * 20
    assert parse_png_dimensions(data) == (0, 0)


def test_parse_png_dimensions_non_bytes_safe():
    assert parse_png_dimensions("not bytes") == (0, 0)  # type: ignore[arg-type]


def test_parse_png_dimensions_bytearray():
    ba = bytearray(_make_png_bytes(10, 20))
    assert parse_png_dimensions(ba) == (10, 20)


# ── capture_android ───────────────────────────────────────────────


def test_capture_android_rejects_non_android_request(ios_request: ScreenshotRequest):
    with pytest.raises(MobileScreenshotConfigError):
        capture_android(ios_request)


def test_capture_android_missing_adb_returns_mock(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: name == "adb")
    result = capture_android(android_request, runner=lambda *a, **kw: _FakeProc())
    assert result.status is ScreenshotStatus.mock
    assert "adb" in result.detail


def test_capture_android_happy_path(
    android_request: ScreenshotRequest, fake_png: bytes, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[_FakeProc(returncode=0), _FakeProc(returncode=0)],
        side_effect_on_disk=[
            ("", b""),  # capture writes on device, nothing locally
            (android_request.output_path, fake_png),  # pull populates local
        ],
    )
    result = capture_android(android_request, runner=runner)
    assert result.status is ScreenshotStatus.passed
    assert result.width == 1080
    assert result.height == 1920
    assert result.size_bytes == len(fake_png)
    assert len(runner.calls) == 2
    # First call is screencap, second is pull
    assert "screencap" in runner.calls[0]["argv"]
    assert "pull" in runner.calls[1]["argv"]
    # PNG bytes attached
    assert result.png_bytes == fake_png


def test_capture_android_attach_bytes_false_suppresses_payload(
    tmp_path: Path, fake_png: bytes, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    req = ScreenshotRequest(
        session_id="sessno", platform="android",
        output_path=str(tmp_path / "out.png"),
        attach_bytes=False,
    )
    runner = FakeRunner(
        returns=[_FakeProc(), _FakeProc()],
        side_effect_on_disk=[("", b""), (req.output_path, fake_png)],
    )
    result = capture_android(req, runner=runner)
    assert result.status is ScreenshotStatus.passed
    assert result.size_bytes == len(fake_png)
    assert result.png_bytes == b""


def test_capture_android_capture_rc_fail(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[_FakeProc(returncode=1, stderr="device offline")],
    )
    result = capture_android(android_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "rc=1" in result.detail
    assert "device offline" in result.detail


def test_capture_android_pull_rc_fail(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[
            _FakeProc(returncode=0),
            _FakeProc(returncode=2, stderr="no such file"),
        ],
    )
    result = capture_android(android_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "pull rc=2" in result.detail


def test_capture_android_timeout_fail(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[subprocess.TimeoutExpired(cmd="adb", timeout=20)],
    )
    result = capture_android(android_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "timed out" in result.detail


def test_capture_android_pull_timeout_fail(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[
            _FakeProc(returncode=0),
            subprocess.TimeoutExpired(cmd="adb pull", timeout=20),
        ],
    )
    result = capture_android(android_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "pull" in result.detail


def test_capture_android_generic_exception_fail(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(returns=[RuntimeError("adb crashed")])
    result = capture_android(android_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "adb crashed" in result.detail


def test_capture_android_pull_generic_exception_fail(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[_FakeProc(returncode=0), RuntimeError("pull boom")],
    )
    result = capture_android(android_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "pull" in result.detail
    assert "boom" in result.detail


def test_capture_android_empty_file_falls_to_fail(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[_FakeProc(), _FakeProc()],
        side_effect_on_disk=[
            ("", b""),
            (android_request.output_path, b""),  # empty file
        ],
    )
    result = capture_android(android_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "empty" in result.detail.lower()


def test_capture_android_bad_png_falls_to_fail(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    bad_png = b"NOT A PNG" * 10
    runner = FakeRunner(
        returns=[_FakeProc(), _FakeProc()],
        side_effect_on_disk=[
            ("", b""),
            (android_request.output_path, bad_png),
        ],
    )
    result = capture_android(android_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "IHDR" in result.detail or "non-PNG" in result.detail


def test_capture_android_creates_parent_directory(
    tmp_path: Path, fake_png: bytes, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    nested = tmp_path / "deep" / "nested" / "folder" / "shot.png"
    req = ScreenshotRequest(
        session_id="sdeep", platform="android",
        output_path=str(nested),
    )
    runner = FakeRunner(
        returns=[_FakeProc(), _FakeProc()],
        side_effect_on_disk=[("", b""), (str(nested), fake_png)],
    )
    result = capture_android(req, runner=runner)
    assert result.status is ScreenshotStatus.passed
    assert nested.parent.is_dir()


# ── capture_ios (local) ───────────────────────────────────────────


def test_capture_ios_rejects_android_request(android_request: ScreenshotRequest):
    with pytest.raises(MobileScreenshotConfigError):
        capture_ios(android_request)


def test_capture_ios_missing_xcrun_returns_mock(
    ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(
        mss, "_tool_missing", lambda name: name == "xcrun",
    )
    result = capture_ios(
        ios_request, runner=lambda *a, **kw: _FakeProc(),
    )
    assert result.status is ScreenshotStatus.mock
    assert "xcrun" in result.detail


def test_capture_ios_local_happy(
    ios_request: ScreenshotRequest, fake_png: bytes, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[_FakeProc(returncode=0)],
        side_effect_on_disk=[(ios_request.output_path, fake_png)],
    )
    result = capture_ios(ios_request, runner=runner)
    assert result.status is ScreenshotStatus.passed
    assert result.width == 1080
    assert result.height == 1920
    call = runner.calls[0]
    assert "xcrun" in call["argv"][0] or "xcrun" in call["argv"]
    assert "simctl" in call["argv"]


def test_capture_ios_local_rc_fail(
    ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(returns=[_FakeProc(returncode=3, stderr="No device")])
    result = capture_ios(ios_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "rc=3" in result.detail


def test_capture_ios_local_timeout_fail(
    ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[subprocess.TimeoutExpired(cmd="xcrun", timeout=20)],
    )
    result = capture_ios(ios_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "timed out" in result.detail


def test_capture_ios_local_generic_exception(
    ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(returns=[OSError("no xcrun")])
    result = capture_ios(ios_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "no xcrun" in result.detail


# ── capture_ios (remote via ssh) ──────────────────────────────────


def test_capture_ios_remote_missing_ssh_returns_mock(
    remote_ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(
        mss, "_tool_missing", lambda name: name == "ssh",
    )
    result = capture_ios(
        remote_ios_request, runner=lambda *a, **kw: _FakeProc(),
    )
    assert result.status is ScreenshotStatus.mock
    assert "ssh" in result.detail


def test_capture_ios_remote_missing_scp_returns_mock(
    remote_ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(
        mss, "_tool_missing", lambda name: name == "scp",
    )
    result = capture_ios(
        remote_ios_request, runner=lambda *a, **kw: _FakeProc(),
    )
    assert result.status is ScreenshotStatus.mock
    assert "scp" in result.detail


def test_capture_ios_remote_happy(
    remote_ios_request: ScreenshotRequest, fake_png: bytes, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[_FakeProc(returncode=0), _FakeProc(returncode=0)],
        side_effect_on_disk=[
            ("", b""),  # remote ssh capture — nothing lands locally
            (remote_ios_request.output_path, fake_png),  # scp pulls back
        ],
    )
    result = capture_ios(remote_ios_request, runner=runner)
    assert result.status is ScreenshotStatus.passed
    assert result.width == 1080
    assert len(runner.calls) == 2
    # First call = ssh xcrun. Second = scp.
    first_argv = runner.calls[0]["argv"]
    assert any("ssh" in t for t in first_argv)
    second_argv = runner.calls[1]["argv"]
    assert any("scp" in t for t in second_argv)


def test_capture_ios_remote_ssh_rc_fail(
    remote_ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[_FakeProc(returncode=255, stderr="ssh refused")],
    )
    result = capture_ios(remote_ios_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "rc=255" in result.detail


def test_capture_ios_remote_scp_rc_fail(
    remote_ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[_FakeProc(returncode=0), _FakeProc(returncode=1, stderr="scp missing file")],
    )
    result = capture_ios(remote_ios_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "scp rc=1" in result.detail


def test_capture_ios_remote_ssh_timeout(
    remote_ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[subprocess.TimeoutExpired(cmd="ssh", timeout=20)],
    )
    result = capture_ios(remote_ios_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "timed out" in result.detail


def test_capture_ios_remote_scp_timeout(
    remote_ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[
            _FakeProc(returncode=0),
            subprocess.TimeoutExpired(cmd="scp", timeout=20),
        ],
    )
    result = capture_ios(remote_ios_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "scp" in result.detail


def test_capture_ios_remote_ssh_generic_exception(
    remote_ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(returns=[RuntimeError("ssh bug")])
    result = capture_ios(remote_ios_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "ssh" in result.detail
    assert "bug" in result.detail


def test_capture_ios_remote_scp_generic_exception(
    remote_ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[_FakeProc(returncode=0), RuntimeError("scp bug")],
    )
    result = capture_ios(remote_ios_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert "scp" in result.detail


def test_capture_ios_remote_uses_session_in_remote_path(
    remote_ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[_FakeProc(returncode=0), _FakeProc(returncode=255)],
    )
    capture_ios(remote_ios_request, runner=runner)
    first_argv = runner.calls[0]["argv"]
    joined = " ".join(first_argv)
    assert remote_ios_request.session_id in joined


# ── capture (dispatch) ────────────────────────────────────────────


def test_capture_dispatches_android(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: name == "adb")
    result = capture(android_request)
    assert result.platform == "android"
    assert result.status is ScreenshotStatus.mock


def test_capture_dispatches_ios(
    ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: name == "xcrun")
    result = capture(ios_request)
    assert result.platform == "ios"
    assert result.status is ScreenshotStatus.mock


def test_capture_rejects_non_request():
    with pytest.raises(TypeError):
        capture("not a request")  # type: ignore[arg-type]


def test_capture_passes_runner_through(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        returns=[_FakeProc(returncode=1, stderr="denied")],
    )
    result = capture(android_request, runner=runner)
    assert result.status is ScreenshotStatus.fail
    assert len(runner.calls) == 1


# ── Markdown renderer ─────────────────────────────────────────────


def test_render_markdown_pass():
    r = ScreenshotResult(
        session_id="s1", platform="android",
        status=ScreenshotStatus.passed,
        path="/tmp/x.png", width=1080, height=1920, size_bytes=123,
        duration_ms=45,
    )
    md = render_screenshot_result_markdown(r)
    assert "session: `s1`" in md
    assert "status: `pass`" in md
    assert "geometry: 1080x1920" in md
    assert "/tmp/x.png" in md
    assert "45 ms" in md


def test_render_markdown_fail_omits_geometry():
    r = ScreenshotResult(
        session_id="s1", platform="ios",
        status=ScreenshotStatus.fail,
        detail="adb offline",
    )
    md = render_screenshot_result_markdown(r)
    assert "geometry" not in md
    assert "detail: adb offline" in md


def test_render_markdown_mock():
    r = ScreenshotResult(
        session_id="s1", platform="android",
        status=ScreenshotStatus.mock,
        detail="adb not on PATH",
    )
    md = render_screenshot_result_markdown(r)
    assert "status: `mock`" in md
    assert "adb not on PATH" in md


# ── Integration smoke ─────────────────────────────────────────────


def test_request_to_dict_round_trip(tmp_path: Path):
    r = ScreenshotRequest(
        session_id="abc", platform="ios",
        output_path=str(tmp_path / "shot.png"),
        ios_udid="booted",
        ios_remote_host="mac@runner",
    )
    payload = r.to_dict()
    json.dumps(payload)
    assert payload["ios_remote_host"] == "mac@runner"
    assert payload["attach_bytes"] is True


def test_result_to_dict_includes_dimensions_on_pass():
    r = ScreenshotResult(
        session_id="s1", platform="ios",
        status=ScreenshotStatus.passed,
        width=390, height=844, size_bytes=50000,
        duration_ms=900,
    )
    payload = r.to_dict()
    assert payload["width"] == 390
    assert payload["height"] == 844
    assert payload["size_bytes"] == 50000
    assert payload["ok"] is True


def test_capture_android_argv_contains_default_remote_path(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(returns=[_FakeProc(returncode=1)])
    capture_android(android_request, runner=runner)
    argv = runner.calls[0]["argv"]
    assert DEFAULT_ANDROID_REMOTE_PATH in argv


def test_capture_ios_argv_contains_output_path(
    ios_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(returns=[_FakeProc(returncode=1)])
    capture_ios(ios_request, runner=runner)
    argv = runner.calls[0]["argv"]
    assert ios_request.output_path in argv


def test_capture_honors_injected_clock(
    android_request: ScreenshotRequest, monkeypatch,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: name == "adb")
    result = capture_android(
        android_request,
        runner=lambda *a, **kw: _FakeProc(),
        clock=lambda: 4242.0,
    )
    assert result.captured_at == 4242.0


def test_default_timeout_matches_constant():
    assert DEFAULT_SCREENSHOT_TIMEOUT_S == 20.0


def test_capture_android_sorted_env_passed_to_subprocess(
    android_request: ScreenshotRequest, monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(mss, "_tool_missing", lambda name: False)
    monkeypatch.setattr(mss.shutil, "which", lambda name: f"/usr/bin/{name}")
    req = ScreenshotRequest(
        session_id="senv", platform="android",
        output_path=str(tmp_path / "shot.png"),
        env={"ANDROID_HOME": "/opt/android"},
    )
    runner = FakeRunner(returns=[_FakeProc(returncode=1)])
    capture_android(req, runner=runner)
    # env is passed as a keyword to subprocess.run; inspect it.
    env_passed = runner.calls[0]["kwargs"].get("env")
    assert env_passed is not None
    assert env_passed["ANDROID_HOME"] == "/opt/android"
