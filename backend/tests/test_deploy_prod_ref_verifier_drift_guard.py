"""FX.7.9 — drift guard for the production-deploy ref verifier.

Background
----------
The 2026-05-03 deep-audit row FX.7.9 flagged that
``scripts/deploy-prod.sh`` accepted *any* git ref produced by
``--branch=`` / ``--tag=`` and never verified its GPG signature, so
a misconfigured operator (or a compromised CI runner with shell
access) could ship an arbitrary feature branch / personal fork to
production. FX.7.9 layered two defences:

1. **Ref allowlist** — ``deploy/prod-deploy-allowlist.txt`` lists
   the only branches and tag patterns that may reach prod. Every
   addition is reviewed via PR — the audit trail for "who said this
   ref is OK to deploy".
2. **GPG signature** — ``deploy/prod-deploy-signers.txt`` lists the
   fingerprints whose keys may sign a deploy-eligible tag (annotated)
   or branch tip. The verifier parses ``git verify-tag/commit --raw``
   ``[GNUPG:] VALIDSIG`` lines and matches against the list.

What this test enforces
-----------------------
* The standalone verifier ``scripts/check_deploy_ref.sh`` exists and
  is syntactically valid bash.
* The allowlist file exists with at least one ``branch`` rule and at
  least one ``tag-regex`` rule (otherwise no prod deploy is possible
  and someone has accidentally emptied the policy).
* Every line in the allowlist is one of the three canonical rule
  kinds; unknown kinds make the verifier abort, so they would break
  every deploy on first use — pin the format here too.
* The signers file exists. Empty is permitted (initial state) — the
  verifier fails closed at runtime, which is what we want.
* Each signer fingerprint that *is* present must be a 40-char hex
  token (the parser rejects anything else).
* ``deploy-prod.sh`` actually invokes the verifier between the
  ``git fetch`` step and the checkout/merge step (the original
  smoking-gun callsite). Without this wiring, the policy files
  exist but never run.
* End-to-end: subprocess-running the verifier with controlled
  allowlist + signer fixtures matches the documented contract
  (accept allowed branch, reject feature branch, reject unknown tag,
  reject empty signers file, accept ``--insecure-skip-verify``
  escape hatch with a loud stderr warning).

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


# ─── Static structure ───────────────────────────────────────────────


def test_verifier_script_exists_and_executable() -> None:
    assert VERIFIER.exists(), f"FX.7.9 missing: {VERIFIER}"
    mode = VERIFIER.stat().st_mode
    assert mode & stat.S_IXUSR, (
        f"FX.7.9: {VERIFIER} must be executable (chmod +x); the deploy "
        "script invokes it directly without `bash` prefix on hosts that "
        "respect the bit."
    )


def test_verifier_script_is_valid_bash() -> None:
    """Catches a syntax error before any operator hits it."""
    rc = subprocess.run(
        ["bash", "-n", str(VERIFIER)], capture_output=True, text=True
    )
    assert rc.returncode == 0, (
        f"FX.7.9: scripts/check_deploy_ref.sh has bash syntax error:\n"
        f"{rc.stderr}"
    )


def test_allowlist_file_exists_with_required_rule_kinds() -> None:
    assert ALLOWLIST.exists(), f"FX.7.9 missing: {ALLOWLIST}"
    rules = _parse_allowlist(ALLOWLIST)
    assert any(k == "branch" for k, _ in rules), (
        "FX.7.9: allowlist has zero `branch` rules — every prod deploy "
        "would abort. master/main lines must remain listed."
    )
    assert any(k == "tag-regex" for k, _ in rules), (
        "FX.7.9: allowlist has zero `tag-regex` rules — semver-tag "
        "deploys would all abort."
    )


def test_allowlist_uses_only_canonical_rule_kinds() -> None:
    """Reject any rule kind the verifier doesn't recognise.

    The verifier aborts on unknown rule kinds, so an unrecognised
    line would brick every deploy on first use. Catch that here.
    """
    rules = _parse_allowlist(ALLOWLIST)
    bad = [k for k, _ in rules if k not in CANONICAL_RULE_KINDS]
    assert not bad, (
        f"FX.7.9: allowlist has unknown rule kind(s) {bad}. "
        f"Only {sorted(CANONICAL_RULE_KINDS)} are accepted."
    )


def test_signers_file_exists_and_contains_only_valid_fingerprints() -> None:
    """Empty is allowed (initial state); any non-empty entry must be 40-hex."""
    assert SIGNERS.exists(), f"FX.7.9 missing: {SIGNERS}"
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
        f"FX.7.9: signers file has non-fingerprint line(s): {bad!r}. "
        "Each non-comment line must be exactly 40 hex characters."
    )


def test_deploy_sh_invokes_the_verifier() -> None:
    """Without this wiring, the policy files exist but never run."""
    body = DEPLOY_SH.read_text(encoding="utf-8")
    # Both code paths (tag + branch) must invoke the verifier.
    assert "scripts/check_deploy_ref.sh --kind tag" in body, (
        "FX.7.9 regression: scripts/deploy-prod.sh no longer invokes "
        "check_deploy_ref.sh on the --tag path."
    )
    assert "scripts/check_deploy_ref.sh --kind branch" in body, (
        "FX.7.9 regression: scripts/deploy-prod.sh no longer invokes "
        "check_deploy_ref.sh on the branch path."
    )
    # The verifier must run BEFORE the checkout / merge — not after.
    # We assert ordering by string-index in the file.
    idx_verify = body.find("check_deploy_ref.sh")
    idx_checkout = body.find("git checkout '$TAG'")
    idx_merge = body.find("git merge origin/$BRANCH")
    assert idx_verify >= 0 and idx_verify < idx_checkout, (
        "FX.7.9: verifier must run BEFORE 'git checkout $TAG'; otherwise "
        "the working tree is already updated when the policy fires."
    )
    assert idx_verify < idx_merge, (
        "FX.7.9: verifier must run BEFORE 'git merge origin/$BRANCH'; "
        "otherwise the rejected branch's commits are already merged."
    )
    # The escape hatch flag must be exposed at the deploy-script CLI too.
    assert "--insecure-skip-verify" in body, (
        "FX.7.9 regression: scripts/deploy-prod.sh no longer exposes "
        "--insecure-skip-verify; operators have no audited bypass."
    )


# ─── Subprocess behaviour ───────────────────────────────────────────


def _run_verifier(
    *,
    kind: str,
    ref: str,
    allowlist: Path,
    signers: Path | None = None,
    extra: list[str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    cmd = [
        "bash",
        str(VERIFIER),
        "--kind",
        kind,
        "--ref",
        ref,
        "--allowlist",
        str(allowlist),
    ]
    if signers is not None:
        cmd += ["--signers", str(signers)]
    if extra:
        cmd += extra
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else str(REPO_ROOT),
    )


def _write_allowlist(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_verifier_accepts_allowlisted_branch(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "branch master\n")
    proc = _run_verifier(kind="branch", ref="master", allowlist=al, extra=["--allowlist-only"])
    assert proc.returncode == 0, proc.stderr
    assert "matched allowlist" in proc.stderr


def test_verifier_rejects_non_allowlisted_branch(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "branch master\n")
    proc = _run_verifier(
        kind="branch", ref="feature/random", allowlist=al, extra=["--allowlist-only"]
    )
    assert proc.returncode != 0
    assert "NOT permitted" in proc.stderr
    assert "feature/random" in proc.stderr


def test_verifier_accepts_semver_tag(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, r"tag-regex ^v[0-9]+\.[0-9]+\.[0-9]+$" + "\n")
    proc = _run_verifier(kind="tag", ref="v1.2.3", allowlist=al, extra=["--allowlist-only"])
    assert proc.returncode == 0, proc.stderr


def test_verifier_rejects_non_semver_tag(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, r"tag-regex ^v[0-9]+\.[0-9]+\.[0-9]+$" + "\n")
    proc = _run_verifier(kind="tag", ref="prerelease-x", allowlist=al, extra=["--allowlist-only"])
    assert proc.returncode != 0
    assert "NOT permitted" in proc.stderr


def test_verifier_branch_regex_matches_release_branches(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, r"branch-regex ^release/[0-9]+\.[0-9]+$" + "\n")
    accepted = _run_verifier(
        kind="branch", ref="release/2.5", allowlist=al, extra=["--allowlist-only"]
    )
    rejected = _run_verifier(
        kind="branch", ref="release/foo", allowlist=al, extra=["--allowlist-only"]
    )
    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0


def test_verifier_branch_rules_do_not_match_tags(tmp_path: Path) -> None:
    """Cross-kind rules must not bleed.

    A `branch master` rule accepting a deploy that asked for `--kind
    tag --ref master` would let an attacker push a tag named ``master``
    to an unprotected mirror and ride the rule. Verify isolation.
    """
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "branch master\n")
    proc = _run_verifier(
        kind="tag", ref="master", allowlist=al, extra=["--allowlist-only"]
    )
    assert proc.returncode != 0, proc.stderr
    assert "NOT permitted" in proc.stderr


def test_verifier_rejects_empty_signers_file(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "branch master\n")
    sn = tmp_path / "sign.txt"
    sn.write_text("# no fingerprints yet\n", encoding="utf-8")
    proc = _run_verifier(kind="branch", ref="master", allowlist=al, signers=sn)
    # Allowlist passes, then signers-empty fails — that's the safe-by-
    # default posture: deploys without a real signer require explicit
    # --insecure-skip-verify.
    assert proc.returncode != 0
    assert "zero trusted fingerprints" in proc.stderr


def test_verifier_rejects_malformed_signer_line(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "branch master\n")
    sn = tmp_path / "sign.txt"
    sn.write_text("not-a-fingerprint\n", encoding="utf-8")
    proc = _run_verifier(kind="branch", ref="master", allowlist=al, signers=sn)
    assert proc.returncode != 0
    assert "syntax error" in proc.stderr


def test_verifier_unknown_rule_kind_is_loud(tmp_path: Path) -> None:
    """An accidental typo in the allowlist must not silently pass-through."""
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "brunch master\n")  # typo: brunch
    proc = _run_verifier(
        kind="branch", ref="master", allowlist=al, extra=["--allowlist-only"]
    )
    assert proc.returncode != 0
    assert "syntax error" in proc.stderr or "unknown rule kind" in proc.stderr


def test_verifier_insecure_skip_verify_prints_loud_warning(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "branch master\n")  # would even pass anyway
    # No signers file — would normally fail Layer 2; --insecure should bypass.
    proc = _run_verifier(
        kind="branch",
        ref="totally-unauthorised",
        allowlist=al,
        extra=["--insecure-skip-verify"],
    )
    assert proc.returncode == 0, proc.stderr
    # Loud warning required so audit log captures the bypass.
    assert "INSECURE_SKIP_VERIFY" in proc.stderr
    assert "emergency escape hatch" in proc.stderr.lower() or "escape hatch" in proc.stderr


def test_verifier_env_var_skip_is_equivalent_to_flag(tmp_path: Path) -> None:
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "branch master\n")
    proc = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "branch", "--ref", "anything",
         "--allowlist", str(al)],
        capture_output=True, text=True,
        env={**os.environ, "OMNISIGHT_DEPLOY_INSECURE_SKIP_VERIFY": "1"},
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "INSECURE_SKIP_VERIFY" in proc.stderr


def test_verifier_required_args_are_enforced() -> None:
    """Missing --kind / --ref must be a hard error, not a default."""
    proc = subprocess.run(
        ["bash", str(VERIFIER), "--ref", "master"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0
    assert "kind" in proc.stderr.lower()

    proc = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "branch"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0
    assert "ref" in proc.stderr.lower()


def test_verifier_kind_value_is_validated() -> None:
    proc = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "junk", "--ref", "master"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0
    assert "branch" in proc.stderr and "tag" in proc.stderr


# ─── Optional GPG end-to-end (skipped when gpg unavailable) ─────────


def _gpg_available() -> bool:
    return shutil.which("gpg") is not None


@pytest.mark.skipif(not _gpg_available(), reason="gpg binary not available in this environment")
def test_verifier_e2e_accepts_signed_branch_tip(tmp_path: Path) -> None:
    """Real GPG round-trip: signed commit by a trusted fingerprint accepts."""
    fpr, gnupg_home = _gen_test_key(tmp_path)
    repo = _init_signed_repo(tmp_path, fpr, gnupg_home)
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "branch master\n")
    sn = tmp_path / "sign.txt"
    sn.write_text(fpr + "\n", encoding="utf-8")

    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    proc = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "branch", "--ref", "master",
         "--allowlist", str(al), "--signers", str(sn)],
        capture_output=True, text=True, cwd=str(repo), env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "signed by trusted fingerprint" in proc.stderr


@pytest.mark.skipif(not _gpg_available(), reason="gpg binary not available in this environment")
def test_verifier_e2e_rejects_signed_branch_with_untrusted_fpr(tmp_path: Path) -> None:
    fpr, gnupg_home = _gen_test_key(tmp_path)
    repo = _init_signed_repo(tmp_path, fpr, gnupg_home)
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "branch master\n")
    sn = tmp_path / "sign.txt"
    sn.write_text("F" * 40 + "\n", encoding="utf-8")

    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    proc = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "branch", "--ref", "master",
         "--allowlist", str(al), "--signers", str(sn)],
        capture_output=True, text=True, cwd=str(repo), env=env,
    )
    assert proc.returncode != 0
    assert "NOT in" in proc.stderr


@pytest.mark.skipif(not _gpg_available(), reason="gpg binary not available in this environment")
def test_verifier_e2e_rejects_unsigned_branch_tip(tmp_path: Path) -> None:
    fpr, gnupg_home = _gen_test_key(tmp_path)
    # Init repo with an UNSIGNED commit (override the helper).
    repo = _init_signed_repo(tmp_path, fpr, gnupg_home, sign_commit=False)
    al = tmp_path / "allow.txt"
    _write_allowlist(al, "branch master\n")
    sn = tmp_path / "sign.txt"
    sn.write_text(fpr + "\n", encoding="utf-8")

    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    proc = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "branch", "--ref", "master",
         "--allowlist", str(al), "--signers", str(sn)],
        capture_output=True, text=True, cwd=str(repo), env=env,
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
            Name-Real: OmniSight FX.7.9 Drift Test
            Name-Email: fx79-driftguard@omnisight.local
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
         "fx79-driftguard@omnisight.local"],
        check=True, capture_output=True, text=True, env=env,
    )
    fpr = ""
    for line in out.stdout.splitlines():
        if line.startswith("fpr:"):
            fpr = line.split(":")[9]
            break
    assert fpr and len(fpr) == 40, f"unexpected gpg fpr listing: {out.stdout!r}"
    return fpr, gnupg_home


def _init_signed_repo(
    tmp_path: Path,
    fpr: str,
    gnupg_home: Path,
    *,
    sign_commit: bool = True,
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], check=True, capture_output=True, cwd=str(repo), env=env
        )

    git("init", "-q", "-b", "master")
    git("config", "user.email", "fx79-driftguard@omnisight.local")
    git("config", "user.name", "FX.7.9 Drift Test")
    git("config", "user.signingkey", fpr)
    git("config", "gpg.program", "gpg")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    git("add", "a.txt")
    if sign_commit:
        git("commit", "-q", "-S" + fpr, "-m", "fx79: signed test commit")
    else:
        git("commit", "--no-gpg-sign", "-q", "-m", "fx79: UNSIGNED commit")
    git("update-ref", "refs/remotes/origin/master", "HEAD")
    return repo
