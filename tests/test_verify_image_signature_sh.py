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
            case "${4:-}" in
              *unsigned*|*tampered*) exit 1 ;;
              *) exit 0 ;;
            esac
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
