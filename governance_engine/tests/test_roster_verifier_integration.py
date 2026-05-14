"""Integration tests for committed roster signature fixtures (OP-1096)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.security.roster_verifier import verify_roster_signature  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
ROSTER_PATH = FIXTURE_DIR / "sample-roster.yaml"
PUBLIC_KEY_PATH = FIXTURE_DIR / "test-l1-public-key.asc"
TEST_L1_FINGERPRINT = "F995558736203E70E98F29175464E20846F314B2"


@pytest.fixture()
def hermetic_gnupg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    gpg = shutil.which("gpg")
    if gpg is None:
        pytest.skip("gpg not installed")

    gnupg_home = tmp_path / "gnupg"
    gnupg_home.mkdir(mode=0o700)
    completed = subprocess.run(
        [
            gpg, "--batch", "--no-tty", "--homedir", str(gnupg_home),
            "--import", str(PUBLIC_KEY_PATH),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.fail(f"failed to import test L1 public key: {completed.stderr}")

    monkeypatch.setenv("GNUPGHOME", str(gnupg_home))
    return gnupg_home


def test_valid_signature_passes(hermetic_gnupg_home: Path) -> None:
    result = verify_roster_signature(ROSTER_PATH, TEST_L1_FINGERPRINT)

    assert result.is_valid is True
    assert result.signer_fingerprint == TEST_L1_FINGERPRINT
    assert result.error is None


def test_modified_roster_fails(tmp_path: Path, hermetic_gnupg_home: Path) -> None:
    roster = tmp_path / "sample-roster.yaml"
    signature = roster.with_suffix(roster.suffix + ".asc")
    roster.write_text(ROSTER_PATH.read_text(encoding="utf-8") + "notes: modified\n", encoding="utf-8")
    fixture_signature = ROSTER_PATH.with_suffix(ROSTER_PATH.suffix + ".asc")
    signature.write_text(fixture_signature.read_text(encoding="utf-8"), encoding="utf-8")

    result = verify_roster_signature(roster, TEST_L1_FINGERPRINT)

    assert result.is_valid is False
    assert "gpg verify failed" in (result.error or "")


def test_wrong_signer_fingerprint_fails(hermetic_gnupg_home: Path) -> None:
    result = verify_roster_signature(ROSTER_PATH, "WRONG")

    assert result.is_valid is False
    assert result.signer_fingerprint == TEST_L1_FINGERPRINT
    assert "does not match" in (result.error or "")


def test_missing_signature_fails(tmp_path: Path) -> None:
    roster = tmp_path / "sample-roster.yaml"
    roster.write_text(ROSTER_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    result = verify_roster_signature(roster, TEST_L1_FINGERPRINT, gpg_binary="/bin/false")

    assert result.is_valid is False
    assert "signature file not found" in (result.error or "")


def test_gpg_missing_returns_graceful() -> None:
    result = verify_roster_signature(ROSTER_PATH, TEST_L1_FINGERPRINT, gpg_binary="/bin/false")

    assert result.is_valid is False
    assert "gpg verify failed" in (result.error or "")
