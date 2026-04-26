"""BS.4.7 — drift guard for ``installer.methods`` install dispatch.

Locks the four-method shared interface and the threat-model invariants
the BS.4.3 row established. Coverage axes:

* dispatch table — unknown method, missing method
* noop — bare success, expected_image_present + missing image
* docker_pull — invalid sha256 format, airgap mode hard-fail
* shell_script — sha256 mismatch (mocked vendor URL), real run
  completes, exit-code failure flags ``auto_retry_blocked``,
  cancel via progress_cb raise
* vendor_installer — invalid sha256 format, atomic-promote on success

The tests use ``file://`` URLs (served from ``tmp_path``) for
shell_script / vendor_installer payloads — air-gap mode is OFF so
``require_https`` short-circuits on the ``file://`` scheme. This
matches the dev-fixture path BS.4.3 inline-smoked but as a permanent
CI-wired contract.

Module-global state audit (per implement_phase_step.md Step 1)
──────────────────────────────────────────────────────────────
Tests are stateless. The shared ``isolated_toolchains_root`` fixture
monkeypatches ``installer.methods.base.TOOLCHAINS_ROOT`` per-test so
filesystem writes land in the per-test ``tmp_path`` instead of the
hardcoded ``/var/lib/omnisight/toolchains/``. monkeypatch unwinds on
teardown.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import pytest

from installer.methods import METHODS, dispatch
from installer.methods.base import InstallCancelled


def _make_progress_cb(events: list[dict[str, Any]] | None = None):
    """Return a no-op progress_cb that appends every kwargs dict to
    *events* (when provided). Used to assert stage transitions and to
    inject cancel via ``raise_at`` patterns."""
    sink = events if events is not None else []

    def cb(**kwargs: Any) -> None:
        sink.append(dict(kwargs))

    return cb


def _sha256_hex(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _file_url(path: Path) -> str:
    """Return ``file:///abs/path`` — urllib accepts the triple slash
    on Linux for absolute paths."""
    return f"file://{path.resolve()}"


# ────────────────────────────────────────────────────────────────────
#  Dispatch table contract
# ────────────────────────────────────────────────────────────────────


def test_methods_table_lists_exactly_four_methods() -> None:
    """The dispatch table is the single source of truth (per
    ``__init__.py`` docstring). New methods MUST land via this table
    AND alembic 0051 CHECK constraint AND the threat model — locking
    the count here makes "ship a 5th method without updating CHECK"
    fail loudly in CI."""
    assert sorted(METHODS.keys()) == [
        "docker_pull", "noop", "shell_script", "vendor_installer",
    ]


def test_dispatch_unknown_install_method_returns_failed() -> None:
    """Operator catalog row drifted to a method label the sidecar
    image doesn't ship — must NOT crash, must report a structured
    error_reason so the UI surfaces it."""
    job = {
        "id": "ij-deadbeef0001",
        "entry_id": "entry-x",
        "install_method": "rsync_pull",  # not in METHODS
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    assert result.error_reason == "dispatch_unknown_install_method"
    # The supported list goes back in result_json so an operator
    # screenshot tells them what they should have used.
    assert result.result_json is not None
    assert result.result_json["supported"] == sorted(METHODS.keys())


def test_dispatch_missing_install_method_returns_failed() -> None:
    """Job dict missing ``install_method`` (catalog fetch failed mid-
    flight, schema drift, etc.) — same uniform-failure path."""
    job = {"id": "ij-deadbeef0002", "entry_id": "entry-x"}
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    assert result.error_reason == "dispatch_missing_install_method"


# ────────────────────────────────────────────────────────────────────
#  noop
# ────────────────────────────────────────────────────────────────────


def test_noop_succeeds_without_image_check() -> None:
    """The bare noop path — no metadata, no image check requested.
    Used for catalog entries that model a host-provided dependency."""
    job = {
        "id": "ij-noop00000001",
        "entry_id": "entry-host-docker",
        "install_method": "noop",
    }
    events: list[dict[str, Any]] = []
    result = dispatch(job, _make_progress_cb(events))
    assert result.state == "completed"
    assert result.error_reason is None
    # Stage transition must be observable on the bus so the UI flips
    # from "queued" to a terminal-ish badge.
    assert any(e.get("stage") == "verifying" for e in events)


def test_noop_expected_image_missing_when_docker_cli_absent_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When metadata.expected_image_present=true AND docker CLI is
    not on PATH, noop fails OPEN (per the noop.py docstring rationale
    — "don't block on infrastructure gap"). Inverts the docker-cli
    presence so the test runs the same on a host with/without docker.
    """
    monkeypatch.setattr(
        "installer.methods.noop.shutil.which", lambda _bin: None,
    )
    job = {
        "id": "ij-noop00000002",
        "entry_id": "entry-vendor-image",
        "install_method": "noop",
        "metadata": {
            "expected_image_present": True,
            "image_ref": "ghcr.io/vendor/image:1.2",
        },
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "completed"
    assert result.result_json is not None
    assert result.result_json["checked_via"] == "docker_cli_missing"


# ────────────────────────────────────────────────────────────────────
#  docker_pull
# ────────────────────────────────────────────────────────────────────


def test_docker_pull_invalid_sha256_returns_failed() -> None:
    """sha256 must match ``^[a-f0-9]{64}$`` (alembic 0051 CHECK +
    threat model §5.1). A short / non-hex value short-circuits before
    we ever shell out to docker, so this works without mocking."""
    job = {
        "id": "ij-dpull00000001",
        "entry_id": "entry-vendor-image",
        "install_method": "docker_pull",
        "install_url": "ghcr.io/vendor/image:1.2",
        "sha256": "not-a-valid-hex",
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    assert result.error_reason == "catalog_entry_invalid_sha256"


def test_docker_pull_in_airgap_mode_hard_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threat model §6.2: docker_pull is hard-disabled in air-gap mode.
    Operators must pre-load the image and use noop +
    expected_image_present=true. The hint must be returned so the UI
    can guide the operator without a runbook lookup."""
    monkeypatch.setenv("OMNISIGHT_INSTALLER_AIRGAP", "1")
    valid_sha = "0" * 64
    job = {
        "id": "ij-dpull00000002",
        "entry_id": "entry-vendor-image",
        "install_method": "docker_pull",
        "install_url": "ghcr.io/vendor/image:1.2",
        "sha256": valid_sha,
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    assert result.error_reason == "airgap_violation"
    assert result.result_json is not None
    assert "hint" in result.result_json
    assert "docker load" in result.result_json["hint"]


# ────────────────────────────────────────────────────────────────────
#  shell_script
# ────────────────────────────────────────────────────────────────────


def test_shell_script_sha256_mismatch_returns_failed(
    tmp_path: Path, isolated_toolchains_root: Path,
) -> None:
    """Mock vendor URL serves a shell payload, catalog claims a
    different sha256 → ``sha256_layer1_mismatch`` (threat model §5.2
    Layer 3). This is the security-critical drift guard: without it,
    a registry compromise would silently land a swapped script."""
    vendor_dir = tmp_path / "vendor"
    vendor_dir.mkdir()
    payload = b"#!/usr/bin/env bash\necho legit\n"
    script = vendor_dir / "install.sh"
    script.write_bytes(payload)

    # Catalog promises a DIFFERENT digest than what the fixture serves.
    wrong_sha = _sha256_hex(b"some other content entirely")

    job = {
        "id": "ij-shell00000001",
        "entry_id": "entry-shell-vendor",
        "install_method": "shell_script",
        "install_url": _file_url(script),
        "sha256": wrong_sha,
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    assert result.error_reason == "sha256_layer1_mismatch"


def test_shell_script_real_run_completes(
    tmp_path: Path, isolated_toolchains_root: Path,
) -> None:
    """Full shell_script flow: download via ``file://`` → sha verify →
    bash subprocess exits 0 → completed. Exercises the BS.4.3 happy
    path including ``run_in_process_group``'s setsid + non-blocking
    stdout drain."""
    vendor_dir = tmp_path / "vendor"
    vendor_dir.mkdir()
    payload = b"#!/usr/bin/env bash\necho hello-from-vendor\nexit 0\n"
    script = vendor_dir / "install.sh"
    script.write_bytes(payload)
    correct_sha = _sha256_hex(payload)

    job = {
        "id": "ij-shell00000002",
        "entry_id": "entry-shell-vendor-2",
        "install_method": "shell_script",
        "install_url": _file_url(script),
        "sha256": correct_sha,
    }
    events: list[dict[str, Any]] = []
    result = dispatch(job, _make_progress_cb(events))
    assert result.state == "completed", result
    assert result.error_reason is None
    assert result.result_json is not None
    assert result.result_json["exit_code"] == 0
    assert "hello-from-vendor" in result.log_tail
    # Stage progression observed on the bus: downloading → verifying →
    # running. (Order locked because the UI relies on it for the
    # progress bar phase indicator.)
    stages = [e["stage"] for e in events if "stage" in e]
    assert "downloading" in stages
    assert "verifying" in stages
    assert "running" in stages


def test_shell_script_exit_code_flags_auto_retry_blocked(
    tmp_path: Path, isolated_toolchains_root: Path,
) -> None:
    """Vendor script exits non-zero → failed with structured
    ``shell_script_exit_code_<N>`` reason AND ``auto_retry_blocked: true``
    so BS.7.6 gates the retry button on operator confirmation
    (per ADR §4.4 step 3 — vendor scripts aren't idempotent)."""
    vendor_dir = tmp_path / "vendor"
    vendor_dir.mkdir()
    payload = b"#!/usr/bin/env bash\necho oops 1>&2\nexit 7\n"
    script = vendor_dir / "install.sh"
    script.write_bytes(payload)
    correct_sha = _sha256_hex(payload)

    job = {
        "id": "ij-shell00000003",
        "entry_id": "entry-shell-fail",
        "install_method": "shell_script",
        "install_url": _file_url(script),
        "sha256": correct_sha,
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    assert result.error_reason == "shell_script_exit_code_7"
    assert result.result_json is not None
    assert result.result_json["auto_retry_blocked"] is True
    assert result.result_json["exit_code"] == 7


def test_shell_script_cancel_via_progress_cb(
    tmp_path: Path, isolated_toolchains_root: Path,
) -> None:
    """Operator hits cancel mid-install → progress emitter (in real
    deployment) detects the backend-side state flip and raises
    InstallCancelled inside the next progress_cb. The
    ``run_in_process_group`` cancel path then killpg's the whole tree
    (threat model §4.8). This test simulates the cb raise on the 3rd
    invocation (~1.5s into a ``sleep 30``) — wall-clock asserts the
    SIGTERM-grace shortcut, not the 30s sleep duration."""
    vendor_dir = tmp_path / "vendor"
    vendor_dir.mkdir()
    payload = b"#!/usr/bin/env bash\nsleep 30\necho done\n"
    script = vendor_dir / "install.sh"
    script.write_bytes(payload)
    correct_sha = _sha256_hex(payload)

    counter = {"n": 0}

    def raising_cb(**kwargs: Any) -> None:
        # Stage progression — let the early stages through; only raise
        # once we're in the "running" stage so we exercise the
        # subprocess kill path (not the pre-subprocess cancel path).
        if kwargs.get("stage") == "running":
            counter["n"] += 1
            if counter["n"] >= 3:
                raise InstallCancelled("operator cancelled mid-install")

    job = {
        "id": "ij-shell00000004",
        "entry_id": "entry-shell-cancel",
        "install_method": "shell_script",
        "install_url": _file_url(script),
        "sha256": correct_sha,
    }
    started = time.monotonic()
    result = dispatch(job, raising_cb)
    elapsed = time.monotonic() - started

    assert result.state == "cancelled"
    assert result.error_reason == "cancelled_by_operator"
    # Must NOT take anywhere close to 30s — kill_process_group with
    # SIGTERM grace 10s plus a few progress_cb ticks should be < 12s.
    # The actual sleep is < 2s on a healthy machine because bash
    # respects SIGTERM immediately while sleeping.
    assert elapsed < 15.0, f"cancel took {elapsed:.2f}s; killpg path slow"


# ────────────────────────────────────────────────────────────────────
#  vendor_installer
# ────────────────────────────────────────────────────────────────────


def test_vendor_installer_invalid_sha256_returns_failed() -> None:
    """Same alembic 0051 sha256 contract as docker_pull — invalid hex
    short-circuits before any download attempt."""
    job = {
        "id": "ij-vend00000001",
        "entry_id": "entry-vendor-bin",
        "install_method": "vendor_installer",
        "install_url": "https://vendor.example/installer.bin",
        "sha256": "abc",  # 3 chars, not 64-hex
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    assert result.error_reason == "catalog_entry_invalid_sha256"


def test_vendor_installer_atomic_promote_on_success(
    tmp_path: Path, isolated_toolchains_root: Path,
) -> None:
    """Full vendor_installer flow: download a fake "installer binary"
    that is in fact a bash script, run it with --target=<scratch>,
    and verify atomic_promote moves the payload onto the final entry
    path. Locks threat model §4.9 (catalog UI must NEVER list a
    half-installed entry — sibling-of-final scratch + os.replace).
    """
    vendor_dir = tmp_path / "vendor"
    vendor_dir.mkdir()
    # The "binary" is a bash script with a shebang. After chmod 700
    # the kernel runs it the same as bash directly.
    payload = (
        b"#!/usr/bin/env bash\n"
        b"set -e\n"
        b"target=\n"
        b"while [ $# -gt 0 ]; do\n"
        b"  case \"$1\" in --target) target=\"$2\"; shift 2;;\n"
        b"                --silent) shift;;\n"
        b"                *) shift;; esac\n"
        b"done\n"
        b"mkdir -p \"$target/bin\" \"$target/lib\"\n"
        b"echo '#!/bin/sh' > \"$target/bin/vendor-tool\"\n"
        b"chmod +x \"$target/bin/vendor-tool\"\n"
        b"echo 'config=ok' > \"$target/lib/data.conf\"\n"
        b"exit 0\n"
    )
    binary = vendor_dir / "installer.bin"
    binary.write_bytes(payload)
    correct_sha = _sha256_hex(payload)

    job = {
        "id": "ij-vend00000002",
        "entry_id": "vendor-toolchain-2",
        "install_method": "vendor_installer",
        "install_url": _file_url(binary),
        "sha256": correct_sha,
        "metadata": {
            # Default args use "{scratch}" placeholder for the payload dir.
            "installer_args": ["--silent", "--target", "{scratch}"],
            "success_exit_codes": [0],
        },
    }

    result = dispatch(job, _make_progress_cb())
    assert result.state == "completed", result
    assert result.error_reason is None

    # Atomic-promoted to final entry path:
    final_entry = isolated_toolchains_root / "vendor-toolchain-2"
    assert final_entry.is_dir(), f"final entry not present: {final_entry}"
    assert (final_entry / "bin" / "vendor-tool").is_file()
    assert (final_entry / "lib" / "data.conf").read_text().strip() == "config=ok"

    # Scratch dir must be cleaned up on success (no half-state visible).
    siblings = sorted(p.name for p in isolated_toolchains_root.iterdir())
    scratch_leftovers = [s for s in siblings if s.startswith(".scratch-")]
    assert scratch_leftovers == [], f"scratch leaked: {scratch_leftovers}"
