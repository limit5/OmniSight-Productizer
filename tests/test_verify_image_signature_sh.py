"""OP-1578 — key-based cosign verifier contract."""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_image_signature.sh"


def _pem_key(path: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                "-----BEGIN PUBLIC KEY-----",
                "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEfakefakefakefakefakefakefakefakefake",
                "-----END PUBLIC KEY-----",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _fake_cosign(bin_dir: Path) -> None:
    cosign = bin_dir / "cosign"
    cosign.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [ "${1:-}" != "verify" ] || [ "${2:-}" != "--key" ]; then
              exit 99
            fi
            # Scan every arg for the image ref so we are robust to verify
            # flags (e.g. --insecure-ignore-tlog=true) shifting its position.
            for arg in "$@"; do
              case "$arg" in
                *unsigned*|*tampered*) exit 1 ;;
              esac
            done
            exit 0
            """
        ),
        encoding="utf-8",
    )
    cosign.chmod(0o755)


def _run_verify(tmp_path: Path, image_ref: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_cosign(bin_dir)
    key = _pem_key(tmp_path / "cosign.pub")
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT), image_ref, "--key", str(key)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_verify_image_signature_is_valid_bash() -> None:
    proc = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr


def test_key_based_verifier_accepts_signed_digest(tmp_path: Path) -> None:
    proc = _run_verify(tmp_path, "registry.example/omnisight-backend@sha256:signed")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "OK"
    assert "via key" in proc.stderr


def test_unsigned_image_fails_key_based_verifier(tmp_path: Path) -> None:
    proc = _run_verify(tmp_path, "registry.example/omnisight-backend@sha256:unsigned")
    assert proc.returncode == 1
    assert "cosign key-based verification failed" in proc.stderr


def test_tampered_image_fails_key_based_verifier(tmp_path: Path) -> None:
    proc = _run_verify(tmp_path, "registry.example/omnisight-backend@sha256:tampered")
    assert proc.returncode == 1
    assert "cosign key-based verification failed" in proc.stderr


def _minimal_env(home: Path) -> dict[str, str]:
    """A clean-ish env: minimal system PATH + a HOME with no cosign under it.

    Mirrors the OP-1736 clean-env scenario (``env -i PATH=/usr/bin:/bin:
    /usr/local/bin``) that used to drop a cosign installed in ~/bin.
    """
    return {"PATH": "/usr/bin:/bin", "HOME": str(home)}


def test_cosign_self_located_via_cosign_bin(tmp_path: Path) -> None:
    # cosign lives in a dir that is NOT on PATH and NOT a probed default;
    # only $COSIGN_BIN points at it. OP-1736: verification still succeeds.
    bin_dir = tmp_path / "opt" / "sig"
    bin_dir.mkdir(parents=True)
    _fake_cosign(bin_dir)
    key = _pem_key(tmp_path / "cosign.pub")
    home = tmp_path / "home"
    home.mkdir()
    env = _minimal_env(home)
    env["COSIGN_BIN"] = str(bin_dir / "cosign")
    proc = subprocess.run(
        ["bash", str(SCRIPT),
         "registry.example/omnisight-backend@sha256:signed", "--key", str(key)],
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


def test_cosign_self_located_via_home_bin(tmp_path: Path) -> None:
    # No $COSIGN_BIN, cosign not on PATH, but present in ~/bin — the script
    # probes the common install dirs and finds it (OP-1736).
    home = tmp_path / "home"
    home_bin = home / "bin"
    home_bin.mkdir(parents=True)
    _fake_cosign(home_bin)
    key = _pem_key(tmp_path / "cosign.pub")
    env = _minimal_env(home)
    proc = subprocess.run(
        ["bash", str(SCRIPT),
         "registry.example/omnisight-backend@sha256:signed", "--key", str(key)],
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


def test_hard_fails_when_cosign_truly_absent(tmp_path: Path) -> None:
    # cosign nowhere on PATH, no COSIGN_BIN, none in the probed dirs.
    # MUST fail-closed (exit 2) — never skip verification (OP-1736).
    if Path("/usr/local/bin/cosign").exists():
        import pytest
        pytest.skip("a real cosign in /usr/local/bin defeats the absence probe")
    key = _pem_key(tmp_path / "cosign.pub")
    home = tmp_path / "home"
    home.mkdir()
    env = _minimal_env(home)
    proc = subprocess.run(
        ["bash", str(SCRIPT),
         "registry.example/omnisight-backend@sha256:signed", "--key", str(key)],
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert proc.returncode == 2
    assert "cosign not found" in proc.stderr
    assert "set COSIGN_BIN" in proc.stderr


def test_cosign_bin_set_but_not_executable_hard_fails(tmp_path: Path) -> None:
    # A misconfigured $COSIGN_BIN must fail-closed, not silently fall through.
    key = _pem_key(tmp_path / "cosign.pub")
    home = tmp_path / "home"
    home.mkdir()
    bogus = tmp_path / "nope" / "cosign"
    env = _minimal_env(home)
    env["COSIGN_BIN"] = str(bogus)
    proc = subprocess.run(
        ["bash", str(SCRIPT),
         "registry.example/omnisight-backend@sha256:signed", "--key", str(key)],
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert proc.returncode == 2
    assert "not an executable file" in proc.stderr
