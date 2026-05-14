"""Integration tests for roster verifier GPG fixtures (OP-1096)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.security.roster_verifier import verify_roster_signature  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROSTER = FIXTURES / "sample-roster.yaml"
PUBLIC_KEY = FIXTURES / "test-l1-public-key.asc"
TRUSTED_FP = "DB05223CD1CECB4DA69C7C02011FEE752BFEDBB3"


@pytest.fixture
def hermetic_gnupg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    gpg = shutil.which("gpg")
    if gpg is None:
        pytest.skip("gpg not installed")

    gnupg_home = tmp_path / "gnupg"
    gnupg_home.mkdir(mode=0o700)
    subprocess.run(
        [gpg, "--batch", "--homedir", str(gnupg_home), "--import", str(PUBLIC_KEY)],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("GNUPGHOME", str(gnupg_home))
    return gnupg_home


def test_valid_signature_passes(hermetic_gnupg_home: Path) -> None:
    result = verify_roster_signature(ROSTER, TRUSTED_FP)
    assert result.is_valid is True
    assert result.signer_fingerprint == TRUSTED_FP
    assert result.error is None


def test_modified_roster_fails(tmp_path: Path, hermetic_gnupg_home: Path) -> None:
    modified = tmp_path / ROSTER.name
    modified.write_text(
        ROSTER.read_text(encoding="utf-8").replace("Grace Hopper", "Grace Murray Hopper"),
        encoding="utf-8",
    )
    shutil.copyfile(ROSTER.with_suffix(ROSTER.suffix + ".asc"), modified.with_suffix(".yaml.asc"))

    result = verify_roster_signature(modified, TRUSTED_FP)
    assert result.is_valid is False
    assert "gpg verify failed" in (result.error or "")


def test_wrong_signer_fingerprint_fails(hermetic_gnupg_home: Path) -> None:
    result = verify_roster_signature(ROSTER, "WRONG")
    assert result.is_valid is False
    assert result.signer_fingerprint == TRUSTED_FP
    assert "does not match" in (result.error or "")


def test_missing_signature_fails(tmp_path: Path) -> None:
    roster = tmp_path / "sample-roster.yaml"
    roster.write_text(ROSTER.read_text(encoding="utf-8"), encoding="utf-8")

    result = verify_roster_signature(roster, TRUSTED_FP, gpg_binary="/bin/false")
    assert result.is_valid is False
    assert "signature file not found" in (result.error or "")


def test_gpg_missing_returns_graceful() -> None:
    result = verify_roster_signature(ROSTER, TRUSTED_FP, gpg_binary="/bin/false")
    assert result.is_valid is False
    assert "gpg verify failed" in (result.error or "")
