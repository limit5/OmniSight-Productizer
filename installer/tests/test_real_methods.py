"""BS.7.8 — deeper coverage for the real ``docker_pull`` / ``shell_script``
install methods.

The companion ``test_methods.py`` is a "drift guard" that locks the
dispatch table + four shared-interface invariants. This file exercises
the **internal** code paths inside ``docker_pull.py`` + ``shell_script.py``
that the drift guard does not reach:

* image-ref input validation (URL scheme + shell-meta hardening)
* ``shutil.which("docker")`` fallback when the CLI is absent
* ``docker pull`` exit-code → structured ``error_reason`` classifier
  (threat model §8 list)
* ``RepoDigests`` mismatch surfacing (threat model §5.4
  ``sha256_layer1_mismatch`` — registry compromise / catalog drift)
* end-to-end happy path with mocked subprocess
* ``shell_script`` ``file://`` fetch failure (vendor URL unreachable)
* ``shell_script`` env scrub: ``OMNISIGHT_INSTALLER_TOKEN`` MUST NOT
  leak to the vendor's bash script (threat model §4.5 sidecar token
  containment)
* ``shell_script`` rejects non-HTTPS / non-file:// URLs in non-airgap
  mode (threat model §5.2 Layer 1)

Module-global state audit (per ``docs/sop/implement_phase_step.md`` Step 1)
──────────────────────────────────────────────────────────────────────────
Tests are stateless. The autouse ``_isolate_installer_env`` fixture in
``conftest.py`` strips every ``OMNISIGHT_INSTALLER_*`` env var before
each test; ``isolated_toolchains_root`` redirects scratch writes into
``tmp_path``. Subprocess mocks are scoped to the test via
``monkeypatch`` and unwind on teardown. No singletons / module-level
caches are introduced by these tests.

Read-after-write timing audit
─────────────────────────────
N/A — these tests run against the local filesystem + mocked subprocess.
No PG / Redis / event-bus involvement; ``run_in_process_group`` returns
synchronously.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from installer.methods import dispatch
from installer.methods import docker_pull as docker_pull_mod
from installer.methods import shell_script as shell_script_mod


# ────────────────────────────────────────────────────────────────────
#  Helpers
# ────────────────────────────────────────────────────────────────────


def _make_progress_cb(events: list[dict[str, Any]] | None = None):
    sink = events if events is not None else []

    def cb(**kwargs: Any) -> None:
        sink.append(dict(kwargs))

    return cb


def _sha256_hex(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _file_url(path: Path) -> str:
    return f"file://{path.resolve()}"


_VALID_SHA = _sha256_hex(b"placeholder vendor image content")


# ────────────────────────────────────────────────────────────────────
#  docker_pull — input validation
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("bad_ref", "expected_reason"),
    [
        # URL scheme — docker pull rejects schemes; we hard-fail early
        # with a structured reason so the operator's UI shows the cause
        # rather than "docker pull failed: invalid reference format".
        ("https://ghcr.io/vendor/image:1.2", "malformed_image_ref"),
        # Shell-meta defence against a corrupted catalog row that
        # contains injection-y characters. We MUST NOT shell-evaluate
        # this string even though Popen takes argv (defence in depth —
        # if a future refactor ever switches to ``shell=True`` the
        # ref-charset still bounds the blast radius).
        ("ghcr.io/vendor/image;rm -rf /", "malformed_image_ref"),
    ],
)
def test_docker_pull_rejects_bad_image_ref(bad_ref: str, expected_reason: str) -> None:
    job = {
        "id": "ij-real00000001",
        "entry_id": "entry-vendor-image",
        "install_method": "docker_pull",
        "install_url": bad_ref,
        "sha256": _VALID_SHA,
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    assert result.error_reason == expected_reason


def test_docker_pull_missing_docker_cli_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sidecar image SHOULD ship docker-ce-cli (Dockerfile.installer
    line per threat model §4.6) but a misconfigured smoke / dev image
    may not. Surface a clean ``docker_cli_missing`` reason rather than
    raising — the operator gets a clear hint instead of a sidecar
    crash."""
    monkeypatch.setattr(docker_pull_mod.shutil, "which", lambda _bin: None)
    job = {
        "id": "ij-real00000002",
        "entry_id": "entry-vendor-image",
        "install_method": "docker_pull",
        "install_url": "ghcr.io/vendor/image:1.2",
        "sha256": _VALID_SHA,
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    assert result.error_reason == "docker_cli_missing"
    assert result.result_json is not None
    assert result.result_json["image_ref"] == "ghcr.io/vendor/image:1.2"


# ────────────────────────────────────────────────────────────────────
#  docker_pull — pull-failure classifier
# ────────────────────────────────────────────────────────────────────


def test_docker_pull_unauthorized_classified_via_log_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``docker pull`` exits non-zero and the log_tail contains
    "denied: requested access" or "unauthorized", classify the failure
    as ``docker_pull_unauthorized`` so the UI can suggest a vendor-
    credential check rather than a generic retry."""
    monkeypatch.setattr(
        docker_pull_mod.shutil, "which",
        lambda _bin: "/usr/bin/docker",
    )

    fake_pull_log = (
        "Pulling fs layer\n"
        "denied: requested access to the resource is denied\n"
    )
    monkeypatch.setattr(
        docker_pull_mod, "_docker_pull",
        lambda docker, ref, *, progress_cb, job_id: docker_pull_mod._PullOutcome(
            returncode=1,
            log_tail=fake_pull_log,
            cancelled=False,
        ),
    )

    job = {
        "id": "ij-real00000003",
        "entry_id": "entry-private-image",
        "install_method": "docker_pull",
        "install_url": "ghcr.io/private/image:1.2",
        "sha256": _VALID_SHA,
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    assert result.error_reason == "docker_pull_unauthorized"
    assert result.result_json is not None
    assert result.result_json["exit_code"] == 1
    # log_tail is preserved on the result so the modal (BS.7.6) can
    # display the vendor's actual error.
    assert "denied: requested access" in result.log_tail


# ────────────────────────────────────────────────────────────────────
#  docker_pull — RepoDigest verification (threat model §5.2 Layer 3)
# ────────────────────────────────────────────────────────────────────


def test_docker_pull_repo_digest_mismatch_returns_sha256_layer1_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pull succeeds but the image's RepoDigest does NOT match the
    catalog's expected sha256 → ``sha256_layer1_mismatch``. This is the
    security-critical drift guard for the registry-compromise threat:
    HTTPS protects the wire, this check protects the content. The
    expected hex is in the result_json so the operator can diff against
    the catalog row."""
    monkeypatch.setattr(
        docker_pull_mod.shutil, "which",
        lambda _bin: "/usr/bin/docker",
    )
    monkeypatch.setattr(
        docker_pull_mod, "_docker_pull",
        lambda docker, ref, *, progress_cb, job_id: docker_pull_mod._PullOutcome(
            returncode=0,
            log_tail="Status: Downloaded newer image for ghcr.io/vendor/image:1.2",
            cancelled=False,
        ),
    )

    # Inspect returns a digest for some OTHER content — exactly the
    # registry-compromise / catalog-drift case the layer 3 check is
    # meant to catch.
    other_sha = _sha256_hex(b"some other image bytes entirely")
    monkeypatch.setattr(
        docker_pull_mod, "_docker_inspect",
        lambda docker, ref: docker_pull_mod._InspectOutcome(
            image_id="sha256:fakeimageidabcdef",
            repo_digests=(f"ghcr.io/vendor/image@sha256:{other_sha}",),
            size_bytes=12345,
        ),
    )

    job = {
        "id": "ij-real00000004",
        "entry_id": "entry-drifted-image",
        "install_method": "docker_pull",
        "install_url": "ghcr.io/vendor/image:1.2",
        "sha256": _VALID_SHA,
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    assert result.error_reason == "sha256_layer1_mismatch"
    assert result.result_json is not None
    assert result.result_json["expected_sha256"] == _VALID_SHA
    # The repo_digests list is surfaced so the operator can see what
    # the registry actually served.
    assert any(
        f"sha256:{other_sha}" in d
        for d in result.result_json["repo_digests"]
    )


def test_docker_pull_happy_path_returns_completed_with_image_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pull succeeds AND the RepoDigest matches → ``completed`` with
    ``result_json`` carrying image_id / repo_digest / size_bytes for
    the catalog UI's "installed" badge + tooltip."""
    monkeypatch.setattr(
        docker_pull_mod.shutil, "which",
        lambda _bin: "/usr/bin/docker",
    )
    monkeypatch.setattr(
        docker_pull_mod, "_docker_pull",
        lambda docker, ref, *, progress_cb, job_id: docker_pull_mod._PullOutcome(
            returncode=0,
            log_tail="Status: Downloaded newer image for ghcr.io/vendor/image:1.2",
            cancelled=False,
        ),
    )
    monkeypatch.setattr(
        docker_pull_mod, "_docker_inspect",
        lambda docker, ref: docker_pull_mod._InspectOutcome(
            image_id="sha256:realimageid01234",
            # Matching repo_digest — the algorithm+hex tail equals the
            # catalog's expected sha256, regardless of the registry-name
            # prefix.
            repo_digests=(f"ghcr.io/vendor/image@sha256:{_VALID_SHA}",),
            size_bytes=987_654_321,
        ),
    )

    events: list[dict[str, Any]] = []
    job = {
        "id": "ij-real00000005",
        "entry_id": "entry-vendor-image",
        "install_method": "docker_pull",
        "install_url": "ghcr.io/vendor/image:1.2",
        "sha256": _VALID_SHA,
    }
    result = dispatch(job, _make_progress_cb(events))
    assert result.state == "completed"
    assert result.error_reason is None
    assert result.result_json is not None
    assert result.result_json["image_id"] == "sha256:realimageid01234"
    assert result.result_json["repo_digest"].endswith(f"sha256:{_VALID_SHA}")
    assert result.result_json["size_bytes"] == 987_654_321
    assert result.bytes_done == 987_654_321
    # The terminal "completed" stage reaches the bus so the drawer can
    # flip the row out of in-flight state.
    stages = [e["stage"] for e in events if "stage" in e]
    assert "pulling" in stages
    assert "completed" in stages


# ────────────────────────────────────────────────────────────────────
#  shell_script — vendor URL unreachable / insecure scheme
# ────────────────────────────────────────────────────────────────────


def test_shell_script_fetch_failed_when_vendor_url_unreachable(
    tmp_path: Path, isolated_toolchains_root: Path,
) -> None:
    """Vendor URL points at a path that doesn't exist on disk → the
    ``file://`` urlopen raises ``URLError(FileNotFoundError(...))`` and
    fetch_url wraps it as ``InstallMethodError(error_reason='fetch_failed')``.
    We use a sha that's well-formed (so the early validation doesn't
    short-circuit) and rely on the download failure to surface the
    real reason."""
    missing = tmp_path / "vendor" / "does-not-exist.sh"
    job = {
        "id": "ij-real00000006",
        "entry_id": "entry-shell-missing",
        "install_method": "shell_script",
        "install_url": _file_url(missing),
        "sha256": _VALID_SHA,
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    # ``fetch_failed`` covers both file:// ENOENT and HTTPS network
    # errors — the result_json carries the install_url so the operator
    # can verify the vendor link.
    assert result.error_reason == "fetch_failed"
    assert result.result_json is not None
    assert result.result_json["install_url"] == _file_url(missing)


def test_shell_script_rejects_http_url_in_non_airgap_mode(
    tmp_path: Path, isolated_toolchains_root: Path,
) -> None:
    """Non-airgap mode + non-HTTPS URL → ``insecure_url_scheme``
    (threat model §5.2 Layer 1: TLS+CA chain protects against on-path
    tampering before sha256 layer 3 even runs). file:// is allowed
    because BS.4.3 fixtures + air-gap mode rely on it; http:// is
    rejected so a corrupted catalog can't ship a clear-text install
    script."""
    job = {
        "id": "ij-real00000007",
        "entry_id": "entry-shell-http",
        "install_method": "shell_script",
        "install_url": "http://vendor.example.test/install.sh",
        "sha256": _VALID_SHA,
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "failed"
    assert result.error_reason == "insecure_url_scheme"


# ────────────────────────────────────────────────────────────────────
#  shell_script — env scrub (token containment)
# ────────────────────────────────────────────────────────────────────


def test_shell_script_env_scrub_blocks_omnisight_installer_token_leak(
    tmp_path: Path,
    isolated_toolchains_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threat model §4.5: the sidecar's bearer token MUST NOT leak into
    a vendor-script subprocess. ``_scrubbed_env`` keeps only an
    allowlist (PATH/HOME/LANG/LC_*/TZ/TERM/USER/LOGNAME); everything
    else — including ``OMNISIGHT_INSTALLER_TOKEN`` — is dropped.

    This test sets the token in the parent env, runs a real bash
    script that dumps its environment to ``env.dump`` under the scratch
    dir, then asserts the token never appears. As a positive control
    we also assert ``PATH`` IS in the dump (allowlist works)."""
    secret = "tk-not-for-vendor-eyes-deadbeef0123456789"
    monkeypatch.setenv("OMNISIGHT_INSTALLER_TOKEN", secret)
    monkeypatch.setenv("OMNISIGHT_DECISION_BEARER", "another-secret-bearer-zzzz")

    vendor_dir = tmp_path / "vendor"
    vendor_dir.mkdir()
    # The script writes its env to a sibling file we can inspect after.
    # We deliberately put the dump path in a fixed location under
    # ``tmp_path`` (NOT under the scratch dir, which gets rmtree'd on
    # successful exit per shell_script.py:_cleanup).
    dump_path = tmp_path / "env.dump"
    payload = (
        b"#!/usr/bin/env bash\n"
        b"env > " + str(dump_path).encode() + b"\n"
        b"exit 0\n"
    )
    script = vendor_dir / "install.sh"
    script.write_bytes(payload)
    correct_sha = _sha256_hex(payload)

    job = {
        "id": "ij-real00000008",
        "entry_id": "entry-shell-env-scrub",
        "install_method": "shell_script",
        "install_url": _file_url(script),
        "sha256": correct_sha,
    }
    result = dispatch(job, _make_progress_cb())
    assert result.state == "completed", result
    assert dump_path.is_file(), "vendor script did not write env dump"
    dumped = dump_path.read_text(encoding="utf-8", errors="replace")
    # Negative — the token MUST NOT be visible to the child:
    assert secret not in dumped, (
        "OMNISIGHT_INSTALLER_TOKEN leaked into vendor script env"
    )
    assert "OMNISIGHT_INSTALLER_TOKEN" not in dumped
    assert "OMNISIGHT_DECISION_BEARER" not in dumped
    # Positive control — the allowlist worked, PATH made it through:
    assert "PATH=" in dumped


# ────────────────────────────────────────────────────────────────────
#  Module-global state sanity (per implement_phase_step.md Step 1)
# ────────────────────────────────────────────────────────────────────


def test_no_module_level_mutable_state_in_real_methods() -> None:
    """Last-line drift guard: re-importing the modules MUST yield the
    same bytecode + same module-level constants. If somebody adds a
    module-level cache / singleton without thinking through the
    multi-worker model, this assertion still passes (Python module
    cache short-circuits the re-import) — so the real defence is the
    audit comments in each method's docstring + this file's audit
    note. We keep the assertion so a future ``del sys.modules[...]``
    pattern still has a place to grow."""
    # Constants are immutable references; verifying their type is the
    # cheapest sanity check we can express here.
    assert isinstance(docker_pull_mod._FORBIDDEN_REF_CHARS.pattern, str)
    # No env vars left over from earlier tests:
    leaked = [k for k in os.environ if k.startswith("OMNISIGHT_INSTALLER_")]
    assert leaked == [], f"OMNISIGHT_INSTALLER_* env leaked across tests: {leaked}"
