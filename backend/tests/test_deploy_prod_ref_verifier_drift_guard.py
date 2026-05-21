"""RT-07a — drift guard for the production-deploy ref verifier.

Background
----------
The 2026-05-03 deep-audit row FX.7.9 first hardened
``scripts/deploy-prod.sh`` so it could no longer ship an arbitrary git
ref to prod, layering a ref **allowlist** + **GPG signature** check
(``scripts/check_deploy_ref.sh``).

RT-07a (single-trunk release train, ADR-0040) tightens that contract to
**final-tag-or-digest only**:

* A production deploy identity is ONLY a FINAL semver tag ``vX.Y.Z``
  (no ``-rc`` / ``-hotfix`` / pre-release suffix) OR an image digest
  ``sha256:<64 lowercase hex>``.
* **Branch deploys are rejected outright** — ``main`` / ``develop`` /
  ``release/*`` / ``hotfix/*`` / any. There is no long-lived release
  branch under the release train, and ``deploy-prod.sh`` no longer
  defaults to ``main`` nor accepts ``--branch``.
* The ``--insecure-skip-verify`` escape hatch (and its
  ``OMNISIGHT_DEPLOY_INSECURE_SKIP_VERIFY`` env equivalent) is
  **removed** — it bypassed both layers with no durable audit. The
  audited path to authorise a new ref is a reviewed PR to the allowlist
  + a signed final tag, or a cosign-verified image digest.

What this test enforces
-----------------------
* ``scripts/check_deploy_ref.sh`` exists, is executable, valid bash.
* The Layer-0 shape gate: ``branch`` → reject; non-final tag (rc/hotfix/
  partial) → reject; malformed digest → reject; missing ref → reject;
  final tag → accept; well-formed digest → accept.
* The allowlist + signers policy files still exist and remain
  well-formed (Layer 1/2 still run for final tags; the allowlist file
  *content* is tightened separately in RT-07b).
* ``deploy-prod.sh`` invokes the verifier on BOTH the tag and digest
  paths, BEFORE the checkout; it no longer defaults to ``main``, no
  longer accepts ``--branch``, and no longer accepts
  ``--insecure-skip-verify``.

Why subprocess instead of unit-tested Python helpers
----------------------------------------------------
The verifier is bash, called by another bash deploy script. The
contract worth pinning is the *bash exit code + stderr text* an
operator sees, not an internal function call.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "scripts" / "check_deploy_ref.sh"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy-prod.sh"
ALLOWLIST = REPO_ROOT / "deploy" / "prod-deploy-allowlist.txt"
SIGNERS = REPO_ROOT / "deploy" / "prod-deploy-signers.txt"

CANONICAL_RULE_KINDS = {"branch", "branch-regex", "tag-regex"}
SEMVER_TAG_REGEX = r"tag-regex ^v[0-9]+\.[0-9]+\.[0-9]+$" + "\n"


# ─── Static structure ───────────────────────────────────────────────


def test_verifier_script_exists_and_executable() -> None:
    assert VERIFIER.exists(), f"RT-07a missing: {VERIFIER}"
    mode = VERIFIER.stat().st_mode
    assert mode & stat.S_IXUSR, (
        f"RT-07a: {VERIFIER} must be executable (chmod +x); the deploy "
        "script invokes it directly without `bash` prefix on hosts that "
        "respect the bit."
    )


def test_verifier_script_is_valid_bash() -> None:
    """Catches a syntax error before any operator hits it."""
    rc = subprocess.run(
        ["bash", "-n", str(VERIFIER)], capture_output=True, text=True
    )
    assert rc.returncode == 0, (
        f"RT-07a: scripts/check_deploy_ref.sh has bash syntax error:\n"
        f"{rc.stderr}"
    )


def test_allowlist_file_exists_with_required_rule_kinds() -> None:
    assert ALLOWLIST.exists(), f"RT-07a missing: {ALLOWLIST}"
    rules = _parse_allowlist(ALLOWLIST)
    assert any(k == "tag-regex" for k, _ in rules), (
        "RT-07a: allowlist has zero `tag-regex` rules — final-tag deploys "
        "would all abort at Layer 1."
    )


def test_allowlist_uses_only_canonical_rule_kinds() -> None:
    """Reject any rule kind the verifier doesn't recognise.

    The verifier aborts on unknown rule kinds, so an unrecognised
    line would brick every deploy on first use. Catch that here.
    """
    rules = _parse_allowlist(ALLOWLIST)
    bad = [k for k, _ in rules if k not in CANONICAL_RULE_KINDS]
    assert not bad, (
        f"RT-07a: allowlist has unknown rule kind(s) {bad}. "
        f"Only {sorted(CANONICAL_RULE_KINDS)} are accepted."
    )


def test_signers_file_exists_and_contains_only_valid_fingerprints() -> None:
    """Empty is allowed (initial state); any non-empty entry must be 40-hex."""
    assert SIGNERS.exists(), f"RT-07a missing: {SIGNERS}"
    bad: list[str] = []
    for raw in SIGNERS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # The verifier strips whitespace before checking; mirror that.
        token = "".join(line.split())
        if len(token) != 40 or not all(c in "0123456789abcdefABCDEF" for c in token):
            bad.append(raw)
    assert not bad, (
        f"RT-07a: signers file has non-fingerprint line(s): {bad!r}. "
        "Each non-comment line must be exactly 40 hex characters."
    )


# ─── deploy-prod.sh wiring (release-train contract) ─────────────────


def test_deploy_sh_invokes_the_verifier_on_both_final_paths() -> None:
    """Without this wiring, the policy files exist but never run."""
    body = DEPLOY_SH.read_text(encoding="utf-8")
    assert "scripts/check_deploy_ref.sh --kind tag" in body, (
        "RT-07a regression: scripts/deploy-prod.sh no longer invokes "
        "check_deploy_ref.sh on the --tag path."
    )
    assert "scripts/check_deploy_ref.sh --kind digest" in body, (
        "RT-07a regression: scripts/deploy-prod.sh no longer invokes "
        "check_deploy_ref.sh on the --digest path."
    )
    # The verifier must run BEFORE the checkout — not after — so a
    # rejected ref never reaches the working tree.
    idx_verify = body.find("check_deploy_ref.sh --kind tag")
    idx_checkout = body.find('git checkout "$TAG"')
    assert idx_verify >= 0 and idx_checkout >= 0, (
        "RT-07a: expected both the tag verifier call and "
        '`git checkout "$TAG"` in deploy-prod.sh.'
    )
    assert idx_verify < idx_checkout, (
        'RT-07a: verifier must run BEFORE `git checkout "$TAG"`; otherwise '
        "the working tree is already updated when the policy fires."
    )


def test_deploy_sh_dropped_branch_deploys() -> None:
    """No more `BRANCH=main` default and no more `--branch` flag."""
    body = DEPLOY_SH.read_text(encoding="utf-8")
    assert "OMNISIGHT_DEPLOY_BRANCH" not in body, (
        "RT-07a regression: deploy-prod.sh still references the retired "
        "OMNISIGHT_DEPLOY_BRANCH (the implicit main default)."
    )
    assert "--branch=*)" not in body and "--branch)" not in body, (
        "RT-07a regression: deploy-prod.sh still parses a --branch flag; "
        "branch deploys were removed under the single-trunk release train."
    )
    # The deploy must never invoke the verifier with a branch kind.
    assert "--kind branch" not in body, (
        "RT-07a regression: deploy-prod.sh still calls the verifier with "
        "--kind branch."
    )


def test_deploy_sh_dropped_insecure_skip_verify_flag() -> None:
    """The unaudited bypass must not be a parseable option any more."""
    body = DEPLOY_SH.read_text(encoding="utf-8")
    # It is fine (and desirable) for a comment to explain the removal;
    # what must be gone is the *option handler* and the variable.
    assert "--insecure-skip-verify)" not in body, (
        "RT-07a regression: deploy-prod.sh still has a "
        "--insecure-skip-verify case arm."
    )
    assert "INSECURE_SKIP_VERIFY=" not in body, (
        "RT-07a regression: deploy-prod.sh still defines an "
        "INSECURE_SKIP_VERIFY variable."
    )


def _run_deploy(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(DEPLOY_SH), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_deploy_sh_requires_a_final_identity() -> None:
    proc = _run_deploy()  # no --tag / --digest
    assert proc.returncode != 0
    assert "final deploy identity is required" in proc.stdout + proc.stderr


def test_deploy_sh_rejects_removed_branch_flag() -> None:
    proc = _run_deploy("--branch=main")
    assert proc.returncode != 0
    assert "Unknown argument" in proc.stdout + proc.stderr


def test_deploy_sh_rejects_removed_insecure_flag() -> None:
    proc = _run_deploy("--insecure-skip-verify", "--tag=v1.2.3")
    assert proc.returncode != 0
    assert "Unknown argument" in proc.stdout + proc.stderr


def test_deploy_sh_tag_and_digest_are_mutually_exclusive() -> None:
    proc = _run_deploy("--tag=v1.2.3", "--digest=sha256:" + "a" * 64)
    assert proc.returncode != 0
    assert "mutually exclusive" in proc.stdout + proc.stderr


# ─── Verifier subprocess behaviour ──────────────────────────────────


def _run_verifier(
    *,
    kind: str,
    ref: str,
    allowlist: Path | None = None,
    signers: Path | None = None,
    extra: list[str] | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = ["bash", str(VERIFIER), "--kind", kind, "--ref", ref]
    if allowlist is not None:
        cmd += ["--allowlist", str(allowlist)]
    if signers is not None:
        cmd += ["--signers", str(signers)]
    if extra:
        cmd += extra
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else str(REPO_ROOT),
        env=env,
    )


def _write_allowlist(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body), encoding="utf-8")


# Layer 0 — branch is rejected outright.


@pytest.mark.parametrize("ref", ["main", "develop", "release/2.5", "hotfix/v0.3.1"])
def test_verifier_rejects_every_branch(tmp_path: Path, ref: str) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "branch main\n")  # even an allowlisted name must fail
    proc = _run_verifier(kind="branch", ref=ref, allowlist=al, extra=["--allowlist-only"])
    assert proc.returncode != 0, proc.stderr
    assert "branch deploys are not permitted" in proc.stderr


# Layer 0 — final tag accepts; non-final tags reject.


def test_verifier_accepts_final_semver_tag(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, SEMVER_TAG_REGEX)
    proc = _run_verifier(kind="tag", ref="v1.2.3", allowlist=al, extra=["--allowlist-only"])
    assert proc.returncode == 0, proc.stderr
    assert "final release tag" in proc.stderr


@pytest.mark.parametrize(
    "ref", ["v1.2.3-rc.1", "v1.2.3-hotfix.2", "v1.2.3-alpha", "v1.2", "v1", "release-x", "1.2.3"]
)
def test_verifier_rejects_non_final_tags(tmp_path: Path, ref: str) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, SEMVER_TAG_REGEX)
    proc = _run_verifier(kind="tag", ref=ref, allowlist=al, extra=["--allowlist-only"])
    assert proc.returncode != 0, f"expected reject for {ref!r}"
    assert "not a FINAL release tag" in proc.stderr


# Layer 0 — digest shape gate.


def test_verifier_accepts_well_formed_digest() -> None:
    proc = _run_verifier(kind="digest", ref="sha256:" + "a" * 64)
    assert proc.returncode == 0, proc.stderr
    assert "well-formed image digest" in proc.stderr


@pytest.mark.parametrize(
    "ref",
    [
        "sha256:deadbeef",            # too short
        "sha256:" + "a" * 63,         # 63 hex
        "sha256:" + "a" * 65,         # 65 hex
        "sha256:" + "A" * 64,         # uppercase rejected
        "sha512:" + "a" * 64,         # wrong algo
        "a" * 64,                     # no sha256: prefix
    ],
)
def test_verifier_rejects_malformed_digest(ref: str) -> None:
    proc = _run_verifier(kind="digest", ref=ref)
    assert proc.returncode != 0, f"expected reject for {ref!r}"
    assert "malformed" in proc.stderr


# Layer 1 — allowlist still gates final tags.


def test_verifier_rejects_final_tag_not_in_allowlist(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    # An allowlist that only matches a narrow range; v9.9.9 final tag is
    # well-formed (passes Layer 0) but not allowlisted (fails Layer 1).
    _write_allowlist(al, r"tag-regex ^v1\.[0-9]+\.[0-9]+$" + "\n")
    proc = _run_verifier(kind="tag", ref="v9.9.9", allowlist=al, extra=["--allowlist-only"])
    assert proc.returncode != 0
    assert "NOT permitted" in proc.stderr


def test_verifier_unknown_rule_kind_is_loud(tmp_path: Path) -> None:
    """An accidental typo in the allowlist must not silently pass-through."""
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "brunch master\n")  # typo: brunch
    proc = _run_verifier(kind="tag", ref="v1.2.3", allowlist=al, extra=["--allowlist-only"])
    assert proc.returncode != 0
    assert "syntax error" in proc.stderr or "unknown rule kind" in proc.stderr


# Layer 2 — signers gate (final tags only).


def test_verifier_rejects_empty_signers_file(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, SEMVER_TAG_REGEX)
    sn = tmp_path / "sign.txt"
    sn.write_text("# no fingerprints yet\n", encoding="utf-8")
    proc = _run_verifier(kind="tag", ref="v1.2.3", allowlist=al, signers=sn)
    # Layer 0 + Layer 1 pass, then signers-empty fails — the safe-by-
    # default posture: a final tag still needs a real release signer.
    assert proc.returncode != 0
    assert "zero trusted fingerprints" in proc.stderr


def test_verifier_rejects_malformed_signer_line(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, SEMVER_TAG_REGEX)
    sn = tmp_path / "sign.txt"
    sn.write_text("not-a-fingerprint\n", encoding="utf-8")
    proc = _run_verifier(kind="tag", ref="v1.2.3", allowlist=al, signers=sn)
    assert proc.returncode != 0
    assert "syntax error" in proc.stderr


# The insecure escape hatch is gone (flag + env both inert / rejected).


def test_verifier_rejects_removed_insecure_flag(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, SEMVER_TAG_REGEX)
    proc = _run_verifier(
        kind="branch", ref="anything", allowlist=al, extra=["--insecure-skip-verify"]
    )
    assert proc.returncode != 0
    # Unknown arg now — there is no bypass.
    assert "unknown arg" in proc.stderr.lower()


def test_verifier_env_var_skip_no_longer_bypasses(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, SEMVER_TAG_REGEX)
    env = {**os.environ, "OMNISIGHT_DEPLOY_INSECURE_SKIP_VERIFY": "1"}
    # A branch must still be rejected even with the old env var set.
    proc = _run_verifier(
        kind="branch", ref="main", allowlist=al, extra=["--allowlist-only"], env=env
    )
    assert proc.returncode != 0
    assert "branch deploys are not permitted" in proc.stderr


# Arg validation.


def test_verifier_required_args_are_enforced() -> None:
    proc = subprocess.run(
        ["bash", str(VERIFIER), "--ref", "v1.2.3"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0
    assert "kind" in proc.stderr.lower()

    proc = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "tag"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0
    assert "ref" in proc.stderr.lower()


def test_verifier_kind_value_is_validated() -> None:
    proc = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "junk", "--ref", "v1.2.3"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0
    assert "tag" in proc.stderr and "digest" in proc.stderr


# ─── Optional GPG end-to-end (skipped when gpg unavailable) ─────────


def _gpg_available() -> bool:
    return shutil.which("gpg") is not None


@pytest.mark.skipif(not _gpg_available(), reason="gpg binary not available in this environment")
def test_verifier_e2e_accepts_signed_final_tag(tmp_path: Path) -> None:
    """Real GPG round-trip: a signed final tag by a trusted fingerprint accepts."""
    fpr, gnupg_home = _gen_test_key(tmp_path)
    repo = _init_signed_tag_repo(tmp_path, fpr, gnupg_home, tag="v1.2.3")
    al = tmp_path / "allow.txt"
    _write_allowlist(al, SEMVER_TAG_REGEX)
    sn = tmp_path / "sign.txt"
    sn.write_text(fpr + "\n", encoding="utf-8")

    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    proc = _run_verifier(
        kind="tag", ref="v1.2.3", allowlist=al, signers=sn, cwd=repo, env=env
    )
    assert proc.returncode == 0, proc.stderr
    assert "signed by trusted fingerprint" in proc.stderr


@pytest.mark.skipif(not _gpg_available(), reason="gpg binary not available in this environment")
def test_verifier_e2e_rejects_final_tag_with_untrusted_fpr(tmp_path: Path) -> None:
    fpr, gnupg_home = _gen_test_key(tmp_path)
    repo = _init_signed_tag_repo(tmp_path, fpr, gnupg_home, tag="v1.2.3")
    al = tmp_path / "allow.txt"
    _write_allowlist(al, SEMVER_TAG_REGEX)
    sn = tmp_path / "sign.txt"
    sn.write_text("F" * 40 + "\n", encoding="utf-8")

    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    proc = _run_verifier(
        kind="tag", ref="v1.2.3", allowlist=al, signers=sn, cwd=repo, env=env
    )
    assert proc.returncode != 0
    assert "NOT in" in proc.stderr


@pytest.mark.skipif(not _gpg_available(), reason="gpg binary not available in this environment")
def test_verifier_e2e_rejects_unsigned_final_tag(tmp_path: Path) -> None:
    fpr, gnupg_home = _gen_test_key(tmp_path)
    repo = _init_signed_tag_repo(tmp_path, fpr, gnupg_home, tag="v1.2.3", sign_tag=False)
    al = tmp_path / "allow.txt"
    _write_allowlist(al, SEMVER_TAG_REGEX)
    sn = tmp_path / "sign.txt"
    sn.write_text(fpr + "\n", encoding="utf-8")

    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    proc = _run_verifier(
        kind="tag", ref="v1.2.3", allowlist=al, signers=sn, cwd=repo, env=env
    )
    assert proc.returncode != 0
    assert "not GPG-signed" in proc.stderr


# ─── Helpers ────────────────────────────────────────────────────────


def _parse_allowlist(path: Path) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if " " not in line:
            rules.append((line, ""))
            continue
        kind, _, value = line.partition(" ")
        rules.append((kind, value.strip()))
    return rules


def _gen_test_key(tmp_path: Path) -> tuple[str, Path]:
    """Generate an isolated GPG key and return (fingerprint, GNUPGHOME)."""
    gnupg_home = tmp_path / "gnupg"
    gnupg_home.mkdir(mode=0o700)
    batch = tmp_path / "keygen.batch"
    batch.write_text(
        textwrap.dedent(
            """
            %no-protection
            Key-Type: RSA
            Key-Length: 2048
            Key-Usage: sign
            Name-Real: OmniSight RT-07a Drift Test
            Name-Email: rt07a-driftguard@omnisight.local
            Expire-Date: 0
            %commit
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    subprocess.run(
        ["gpg", "--batch", "--quiet", "--gen-key", str(batch)],
        check=True, capture_output=True, env=env,
    )
    out = subprocess.run(
        ["gpg", "--list-keys", "--with-colons", "--fingerprint",
         "rt07a-driftguard@omnisight.local"],
        check=True, capture_output=True, text=True, env=env,
    )
    fpr = ""
    for line in out.stdout.splitlines():
        if line.startswith("fpr:"):
            fpr = line.split(":")[9]
            break
    assert fpr and len(fpr) == 40, f"unexpected gpg fpr listing: {out.stdout!r}"
    return fpr, gnupg_home


def _init_signed_tag_repo(
    tmp_path: Path,
    fpr: str,
    gnupg_home: Path,
    *,
    tag: str,
    sign_tag: bool = True,
) -> Path:
    """Init a repo with one commit and an annotated (optionally signed) tag.

    The verifier checks the tag object's signature via ``git verify-tag``,
    so the deploy identity under test is the tag, not a branch tip.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], check=True, capture_output=True, cwd=str(repo), env=env
        )

    git("init", "-q", "-b", "develop")
    git("config", "user.email", "rt07a-driftguard@omnisight.local")
    git("config", "user.name", "RT-07a Drift Test")
    git("config", "user.signingkey", fpr)
    git("config", "gpg.program", "gpg")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "--no-gpg-sign", "-q", "-m", "rt07a: test commit")
    if sign_tag:
        git("tag", "-s", "-u", fpr, "-m", f"release {tag}", tag)
    else:
        # Lightweight tag — no tag object, so no signature for
        # git verify-tag to validate (avoids any global tag.gpgSign that
        # would auto-sign an annotated `-a` tag).
        git("-c", "tag.gpgSign=false", "tag", tag)
    return repo
