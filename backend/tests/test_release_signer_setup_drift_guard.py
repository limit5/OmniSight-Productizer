"""FX.9.8 — drift guard for the operator GPG release-signer setup.

Background
----------
FX.7.9 layered the deploy-ref allowlist + GPG signer gate but shipped
``deploy/prod-deploy-signers.txt`` empty, so every prod deploy required
the ``--insecure-skip-verify`` escape hatch. FX.9.8 provisioned the
first real release-signer and pinned the artefacts that make a real
verify possible:

* ``deploy/prod-deploy-signers.txt``      — committed fingerprint(s)
* ``deploy/release-signers.asc``          — committed public-key bundle
* ``scripts/setup_release_signer.sh``     — operator key-gen helper
* ``docs/runbook/gpg-release-signer-setup.md`` — operator runbook

What this test enforces
-----------------------
* The signers file holds at least one real fingerprint (i.e. has not
  silently regressed to the empty FX.7.9 placeholder).
* The bundled public-key file exists and parses as a PGP armor block.
* Every fingerprint listed in the signers file has a matching public
  key inside the bundle — the bundle import on a cold-spare host must
  cover every trusted signer (drift between the two files = silent
  fail at deploy time on the spare).
* The setup helper script exists, is executable, and is syntactically
  valid bash.
* The runbook exists and documents the four canonical operator flows
  (first-time setup / rotation / revocation / cold-spare).

Why subprocess instead of Python helpers
----------------------------------------
The helper is bash, the verifier is bash, the consumers are operators
running bash. The contract worth pinning is the bash-level invariant,
not Python.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SIGNERS = REPO_ROOT / "deploy" / "prod-deploy-signers.txt"
KEYBUNDLE = REPO_ROOT / "deploy" / "release-signers.asc"
SETUP_HELPER = REPO_ROOT / "scripts" / "setup_release_signer.sh"
RUNBOOK = REPO_ROOT / "docs" / "runbook" / "gpg-release-signer-setup.md"

FPR_RE = re.compile(r"^[0-9A-Fa-f]{40}$")


def _parse_signers(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        token = "".join(line.split())
        if FPR_RE.match(token):
            out.append(token.upper())
    return out


def test_signers_file_has_at_least_one_real_fingerprint() -> None:
    """FX.9.8 closure: a real release-signer must be provisioned.

    The empty signers file was the FX.7.9 placeholder state — if this
    test goes red because the file went empty again, FX.9.8 has
    regressed and every deploy is back to needing --insecure-skip-verify.
    """
    assert SIGNERS.exists(), f"FX.9.8 missing: {SIGNERS}"
    fprs = _parse_signers(SIGNERS)
    assert len(fprs) >= 1, (
        "FX.9.8 regression: deploy/prod-deploy-signers.txt has zero "
        "fingerprints. Without a real signer, every prod deploy must "
        "use --insecure-skip-verify, which defeats the whole gate. "
        "Re-run scripts/setup_release_signer.sh and re-add the line."
    )


def test_release_signers_bundle_exists_and_is_pgp_armor() -> None:
    """The committed pubkey bundle must parse as a PGP block."""
    assert KEYBUNDLE.exists(), (
        f"FX.9.8 missing: {KEYBUNDLE}. Without the bundled public key, "
        "a cold-spare prod host cannot import the trusted signer's "
        "public half and verify will fail closed."
    )
    body = KEYBUNDLE.read_text(encoding="utf-8")
    assert "-----BEGIN PGP PUBLIC KEY BLOCK-----" in body, (
        f"FX.9.8: {KEYBUNDLE} is not a PGP armor block. Re-export with: "
        "gpg --armor --export $(awk '!/^#/ && NF {print $1}' "
        "deploy/prod-deploy-signers.txt) > deploy/release-signers.asc"
    )
    assert "-----END PGP PUBLIC KEY BLOCK-----" in body, (
        f"FX.9.8: {KEYBUNDLE} PGP block is unterminated."
    )


@pytest.mark.skipif(
    shutil.which("gpg") is None, reason="gpg binary not available in this environment"
)
def test_signers_and_keybundle_fingerprints_match(tmp_path: Path) -> None:
    """Every fingerprint in the signers file must appear in the asc bundle.

    Drift between the two = a cold-spare host imports the bundle and
    finds it does NOT cover all trusted signers, so deploys signed by
    the missing fingerprint fail closed on the spare with no clue why.
    """
    signers = set(_parse_signers(SIGNERS))
    if not signers:
        pytest.skip("no fingerprints in signers file (covered by other test)")

    # Import the bundle into an isolated GNUPGHOME so we don't pollute
    # the host keyring, then list its fingerprints.
    gnupg_home = tmp_path / "gnupg"
    gnupg_home.mkdir(mode=0o700)
    env = {"GNUPGHOME": str(gnupg_home), "PATH": "/usr/bin:/bin"}

    subprocess.run(
        ["gpg", "--batch", "--quiet", "--import", str(KEYBUNDLE)],
        check=True, capture_output=True, env=env,
    )
    out = subprocess.run(
        ["gpg", "--list-keys", "--with-colons", "--fingerprint"],
        check=True, capture_output=True, text=True, env=env,
    )
    bundle_fprs: set[str] = set()
    for line in out.stdout.splitlines():
        if line.startswith("fpr:"):
            bundle_fprs.add(line.split(":")[9].upper())

    missing = signers - bundle_fprs
    assert not missing, (
        f"FX.9.8 drift: {sorted(missing)} listed in {SIGNERS.name} but "
        f"not present in {KEYBUNDLE.name}. Re-export the bundle with: "
        "gpg --armor --export $(awk '!/^#/ && NF {print $1}' "
        "deploy/prod-deploy-signers.txt) > deploy/release-signers.asc"
    )


def test_setup_helper_script_exists_and_executable() -> None:
    assert SETUP_HELPER.exists(), f"FX.9.8 missing: {SETUP_HELPER}"
    mode = SETUP_HELPER.stat().st_mode
    assert mode & stat.S_IXUSR, (
        f"FX.9.8: {SETUP_HELPER} must be executable (chmod +x); the "
        "operator runbook documents `./scripts/setup_release_signer.sh`."
    )


def test_setup_helper_script_is_valid_bash() -> None:
    rc = subprocess.run(
        ["bash", "-n", str(SETUP_HELPER)], capture_output=True, text=True
    )
    assert rc.returncode == 0, (
        f"FX.9.8: scripts/setup_release_signer.sh has bash syntax error:\n"
        f"{rc.stderr}"
    )


def test_setup_helper_help_lists_required_flags() -> None:
    """`--help` should print the contract operators rely on.

    Drift guard: if someone refactors the arg parser and forgets the
    --name / --email / --allow-existing flags, the runbook (which
    documents these exact flags) silently lies. Pin the contract.
    """
    rc = subprocess.run(
        ["bash", str(SETUP_HELPER), "--help"], capture_output=True, text=True
    )
    assert rc.returncode == 0
    text = rc.stdout + rc.stderr
    for flag in ("--name", "--email", "--allow-existing"):
        assert flag in text, (
            f"FX.9.8: --help output missing flag {flag!r}. The runbook "
            "documents this flag — keep them in sync."
        )


def test_runbook_exists_and_covers_canonical_flows() -> None:
    assert RUNBOOK.exists(), f"FX.9.8 missing: {RUNBOOK}"
    body = RUNBOOK.read_text(encoding="utf-8").lower()
    # Section headings — keep flexible to allow minor wording edits.
    required_topics = [
        "first-time setup",
        "rotation",
        "revocation",
        "cold-spare",
    ]
    missing = [t for t in required_topics if t not in body]
    assert not missing, (
        f"FX.9.8: {RUNBOOK.name} missing required topic section(s): "
        f"{missing}. The four canonical operator flows must stay "
        "documented so operators don't improvise during an incident."
    )
