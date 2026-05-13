"""Tests for governance_engine.security.roster_verifier (OP-1095)."""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from governance_engine.security.roster_verifier import (  # noqa: E402
    VerifyResult,
    load_l1_fingerprint,
    verify_roster_signature,
)

TRUSTED_FP = "AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555"
WRONG_FP = "DEADBEEFCAFE000011112222333344445555AAAA"


def _fake_gpg(tmp_path: Path, name: str, fingerprint: str | None, *, rc: int = 0) -> Path:
    """Shell stub emitting a `gpg --status-fd=1 --verify`-shaped status line."""
    body = (
        f"[GNUPG:] VALIDSIG {fingerprint} 2026-05-14 0 4 0 1 8 00 {fingerprint}"
        if fingerprint else ""
    )
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{body}'\nexit {rc}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_roster(tmp_path: Path, *, with_sig: bool = True) -> Path:
    roster = tmp_path / "operator-deputy-roster.yaml"
    roster.write_text("deputies: []\n", encoding="utf-8")
    if with_sig:
        sig = roster.with_suffix(roster.suffix + ".asc")
        sig.write_text("-----BEGIN PGP SIGNATURE-----\n(stub)\n-----END PGP SIGNATURE-----\n",
                       encoding="utf-8")
    return roster


def test_load_l1_fingerprint_present(tmp_path: Path) -> None:
    fp = tmp_path / "fp"
    fp.write_text(TRUSTED_FP + "\n", encoding="utf-8")
    assert load_l1_fingerprint(fp) == TRUSTED_FP


def test_load_l1_fingerprint_missing(tmp_path: Path) -> None:
    assert load_l1_fingerprint(tmp_path / "absent") is None


def test_load_l1_fingerprint_empty_normalises_to_none(tmp_path: Path) -> None:
    fp = tmp_path / "fp"
    fp.write_text("   \n", encoding="utf-8")
    assert load_l1_fingerprint(fp) is None


def test_load_l1_fingerprint_normalises_spaces_and_case(tmp_path: Path) -> None:
    fp = tmp_path / "fp"
    spaced = " ".join(TRUSTED_FP[i : i + 4] for i in range(0, len(TRUSTED_FP), 4)).lower()
    fp.write_text(spaced + "\n", encoding="utf-8")
    assert load_l1_fingerprint(fp) == TRUSTED_FP


def test_verify_happy_path_returns_valid_result(tmp_path: Path) -> None:
    roster = _make_roster(tmp_path)
    gpg = _fake_gpg(tmp_path, "gpg-ok", TRUSTED_FP, rc=0)
    result = verify_roster_signature(roster, TRUSTED_FP, gpg_binary=str(gpg))
    assert isinstance(result, VerifyResult)
    assert (result.is_valid, result.signer_fingerprint, result.error) == (True, TRUSTED_FP, None)


def test_verify_accepts_spaced_lowercase_trusted_fingerprint(tmp_path: Path) -> None:
    roster = _make_roster(tmp_path)
    gpg = _fake_gpg(tmp_path, "gpg-ok", TRUSTED_FP, rc=0)
    spaced = " ".join(TRUSTED_FP[i : i + 4] for i in range(0, len(TRUSTED_FP), 4)).lower()
    assert verify_roster_signature(roster, spaced, gpg_binary=str(gpg)).is_valid


def test_verify_missing_roster(tmp_path: Path) -> None:
    r = verify_roster_signature(tmp_path / "no-such.yaml", TRUSTED_FP, gpg_binary="/bin/false")
    assert r.is_valid is False
    assert "roster" in (r.error or "").lower()


def test_verify_missing_signature(tmp_path: Path) -> None:
    roster = _make_roster(tmp_path, with_sig=False)
    r = verify_roster_signature(roster, TRUSTED_FP, gpg_binary="/bin/false")
    assert r.is_valid is False
    assert "signature" in (r.error or "").lower()


def test_verify_gpg_reports_invalid_signature(tmp_path: Path) -> None:
    # /bin/false simulates "gpg verify rejected the signature" (rc != 0).
    roster = _make_roster(tmp_path)
    r = verify_roster_signature(roster, TRUSTED_FP, gpg_binary="/bin/false")
    assert r.is_valid is False
    assert "gpg verify failed" in (r.error or "")


def test_verify_signer_does_not_match_trusted(tmp_path: Path) -> None:
    roster = _make_roster(tmp_path)
    gpg = _fake_gpg(tmp_path, "gpg-wrong", WRONG_FP, rc=0)
    r = verify_roster_signature(roster, TRUSTED_FP, gpg_binary=str(gpg))
    assert r.is_valid is False
    assert r.signer_fingerprint == WRONG_FP
    assert "does not match" in (r.error or "")


def test_verify_no_validsig_status_line(tmp_path: Path) -> None:
    roster = _make_roster(tmp_path)
    gpg = _fake_gpg(tmp_path, "gpg-empty", None, rc=0)
    r = verify_roster_signature(roster, TRUSTED_FP, gpg_binary=str(gpg))
    assert r.is_valid is False
    assert "VALIDSIG" in (r.error or "")


def test_verify_empty_trusted_fingerprint(tmp_path: Path) -> None:
    roster = _make_roster(tmp_path)
    r = verify_roster_signature(roster, "  ", gpg_binary="/bin/false")
    assert r.is_valid is False
    assert "empty" in (r.error or "").lower()


def test_verify_gpg_binary_missing_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    roster = _make_roster(tmp_path)
    missing = tmp_path / "no-such-gpg"
    with caplog.at_level("WARNING", logger="governance_engine.security.roster_verifier"):
        r = verify_roster_signature(roster, TRUSTED_FP, gpg_binary=str(missing))
    assert r.is_valid is False
    assert r.error == "gpg not installed"
    assert any("gpg" in rec.message.lower() for rec in caplog.records)
