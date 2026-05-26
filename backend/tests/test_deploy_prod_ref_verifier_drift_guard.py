"""RT-20 — drift guard for the production-deploy ref verifier.

Background
----------
The 2026-05-03 deep-audit row FX.7.9 first hardened
``scripts/deploy-prod.sh`` so it could no longer ship an arbitrary git
ref to prod, layering a ref **allowlist** + **GPG signature** check
(``scripts/check_deploy_ref.sh``). RT-07a (single-trunk release train,
ADR-0040) tightened that to **final-tag-or-digest only**.

RT-20 (ADR-0040 §"Decisions LOCKED") then re-scoped the final release
identity to **IMAGE-TAG-ONLY**: a release is a promoted GitLab CR image
tag ``vX.Y.Z`` resolving to a validated image **DIGEST** (plus the
``release_train`` / ``release_audit`` row). **No ``v*`` git tag is ever
created** — one would trip the existing ``^v`` CI build rule and rebuild
a different digest, breaking "validated digest == shipped digest".

OP-1704 (finding #25) removed the now-dead v*-git-tag gating from
``scripts/check_deploy_ref.sh``: the Layer-0 final-tag-shape gate plus
the git-side **allowlist** (Layer 1) and **GPG-signature** (Layer 2)
layers that only a git tag ever reached. The verifier now gates on
exactly one production deploy identity — a well-formed image **digest**
(cosign owns its content-trust, RT-06) — and rejects both a git **tag**
and a **branch** outright.

What this test enforces
-----------------------
* ``scripts/check_deploy_ref.sh`` exists, is executable, valid bash.
* The deploy-identity gate: ``branch`` → reject; ``tag`` → reject
  (image-tag-only, no v* git tag); malformed digest → reject; missing
  ref/kind → reject; well-formed digest → accept.
* ``deploy-prod.sh`` invokes the verifier on the digest path BEFORE it
  uses the digest; it no longer defaults to ``main``, no longer accepts
  ``--branch``, and no longer accepts ``--insecure-skip-verify``.

Why subprocess instead of unit-tested Python helpers
----------------------------------------------------
The verifier is bash, called by another bash deploy script. The
contract worth pinning is the *bash exit code + stderr text* an
operator sees, not an internal function call.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "scripts" / "check_deploy_ref.sh"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy-prod.sh"


# ─── Static structure ───────────────────────────────────────────────


def test_verifier_script_exists_and_executable() -> None:
    assert VERIFIER.exists(), f"RT-20 missing: {VERIFIER}"
    mode = VERIFIER.stat().st_mode
    assert mode & stat.S_IXUSR, (
        f"RT-20: {VERIFIER} must be executable (chmod +x); the deploy "
        "script invokes it directly without `bash` prefix on hosts that "
        "respect the bit."
    )


def test_verifier_script_is_valid_bash() -> None:
    """Catches a syntax error before any operator hits it."""
    rc = subprocess.run(
        ["bash", "-n", str(VERIFIER)], capture_output=True, text=True
    )
    assert rc.returncode == 0, (
        f"RT-20: scripts/check_deploy_ref.sh has bash syntax error:\n"
        f"{rc.stderr}"
    )


# ─── deploy-prod.sh wiring (release-train contract) ─────────────────


def test_deploy_sh_invokes_the_verifier_on_the_digest_path() -> None:
    """Without this wiring, the gate exists but never runs. Under RT-20
    the live production deploy identity is the image digest, so the
    verifier must gate it before the deploy proceeds."""
    body = DEPLOY_SH.read_text(encoding="utf-8")
    assert "scripts/check_deploy_ref.sh --kind digest" in body, (
        "RT-20 regression: scripts/deploy-prod.sh no longer invokes "
        "check_deploy_ref.sh on the digest path."
    )
    # The verifier must run before the rolling restart actually swaps a
    # replica — a rejected ref never reaches a running container.
    idx_verify = body.find("check_deploy_ref.sh --kind digest")
    idx_restart = body.find('up -d --no-deps backend-a')
    assert idx_verify >= 0 and idx_restart >= 0, (
        "RT-20: expected the digest verifier call and the backend-a "
        "rolling-restart in deploy-prod.sh."
    )
    assert idx_verify < idx_restart, (
        "RT-20: verifier must run BEFORE the rolling restart; otherwise a "
        "rejected ref could already be swapped into a replica."
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
    extra: list[str] | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = ["bash", str(VERIFIER), "--kind", kind, "--ref", ref]
    if extra:
        cmd += extra
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else str(REPO_ROOT),
        env=env,
    )


# Deploy-identity gate — branch is rejected outright.


@pytest.mark.parametrize("ref", ["main", "develop", "release/2.5", "hotfix/v0.3.1"])
def test_verifier_rejects_every_branch(ref: str) -> None:
    proc = _run_verifier(kind="branch", ref=ref, extra=["--allowlist-only"])
    assert proc.returncode != 0, proc.stderr
    assert "branch deploys are not permitted" in proc.stderr


# Deploy-identity gate — a git tag is rejected outright (RT-20).


@pytest.mark.parametrize(
    "ref",
    [
        # Final semver tags — once accepted, now REJECTED (image-tag-only).
        "v1.2.3",
        "v9.9.9",
        # Non-final / malformed — still rejected, now for the image-tag-
        # only reason rather than a shape mismatch.
        "v1.2.3-rc.1",
        "v1.2.3-hotfix.2",
        "v1.2.3-alpha",
        "v1.2",
        "v1",
        "release-x",
        "1.2.3",
    ],
)
def test_verifier_rejects_every_git_tag(ref: str) -> None:
    """RT-20 retired the v*-git-tag identity: no ``v*`` git tag is ever
    created, so the verifier rejects the ``tag`` kind for every ref — the
    final-tag-shape gate (+ allowlist + GPG layers) is gone."""
    proc = _run_verifier(kind="tag", ref=ref, extra=["--allowlist-only"])
    assert proc.returncode != 0, f"expected reject for {ref!r}"
    assert "git-tag deploys are not permitted" in proc.stderr
    assert "image-tag-only" in proc.stderr


# Deploy-identity gate — digest shape gate (the only accept path).


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


# The insecure escape hatch is gone (flag + env both inert / rejected).


def test_verifier_rejects_removed_insecure_flag() -> None:
    proc = _run_verifier(
        kind="branch", ref="anything", extra=["--insecure-skip-verify"]
    )
    assert proc.returncode != 0
    # Unknown arg now — there is no bypass.
    assert "unknown arg" in proc.stderr.lower()


def test_verifier_env_var_skip_no_longer_bypasses() -> None:
    env = {**os.environ, "OMNISIGHT_DEPLOY_INSECURE_SKIP_VERIFY": "1"}
    # A branch must still be rejected even with the old env var set.
    proc = _run_verifier(
        kind="branch", ref="main", extra=["--allowlist-only"], env=env
    )
    assert proc.returncode != 0
    assert "branch deploys are not permitted" in proc.stderr


# Arg validation.


def test_verifier_required_args_are_enforced() -> None:
    proc = subprocess.run(
        ["bash", str(VERIFIER), "--ref", "sha256:" + "a" * 64],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0
    assert "kind" in proc.stderr.lower()

    proc = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "digest"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0
    assert "ref" in proc.stderr.lower()


def test_verifier_kind_value_is_validated() -> None:
    proc = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "junk", "--ref", "sha256:" + "a" * 64],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0
    assert "digest" in proc.stderr


# ─── OP-1734 — the dead --tag path is reconciled to the digest form ──
#
# Before OP-1734, `deploy-prod.sh` advertised `--tag=vX.Y.Z` as the
# primary deploy/rollback path in its usage header, examples, rollback
# hint, and the runbook — yet `check_deploy_ref.sh` rejects `--kind tag`
# unconditionally (RT-20, image-tag-only), so `--tag` ALWAYS errored. Worse,
# the tag path ran gerrit-source detection + `git fetch --tags` BEFORE
# reaching that always-failing gate (an operator hit this live during
# v0.6.2). These guards pin the reconciliation: `--tag` now fails fast with
# an ACTIONABLE pointer at the digest deploy command, the usage/rollback
# text shows the digest form, and the dead git-fetch machinery is gone.


def _deploy_sh_text() -> str:
    return DEPLOY_SH.read_text(encoding="utf-8")


def _deploy_sh_code() -> str:
    """deploy-prod.sh with full-line `#` comments stripped.

    The reconciliation comments deliberately NAME the removed `git fetch
    --tags` / `git checkout` machinery to explain why it is gone, so the
    no-fetch assertion must look at executable lines only (same pattern as
    test_deploy_sh_rollback.py)."""
    return "\n".join(
        ln for ln in _deploy_sh_text().splitlines() if not ln.lstrip().startswith("#")
    )


def test_deploy_tag_path_errors_with_digest_pointer() -> None:
    """`deploy-prod.sh --tag=vX` must hard-error with a message that NAMES
    the digest deploy flags — not an opaque arg error or a stale-mirror
    fetch failure. This is the operator-facing fix for OP-1734."""
    proc = _run_deploy("--tag=v0.6.2")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    # The RT-20 gate still owns the rejection (MUST NOT weaken it)…
    assert "git-tag deploys are not permitted" in out
    assert "image-tag-only" in out
    # …and it is now actionable: it names BOTH per-image digest flags.
    assert "--backend-digest=sha256:" in out
    assert "--frontend-digest=sha256:" in out


def test_deploy_tag_path_does_not_fetch_before_rejecting() -> None:
    """F7: the dead tag path's gerrit-source detection + `git fetch --tags`
    + `git checkout` only ever ran to feed a deploy identity the gate then
    rejected. They must be gone so `--tag` cannot fetch from a stale mirror
    before erroring, and a digest deploy never touches the git tree."""
    code = _deploy_sh_code()
    assert "git fetch" not in code, (
        "OP-1734/F7 regression: deploy-prod.sh still runs `git fetch` — the "
        "dead tag path's fetch must be removed (digest deploys pull a "
        "pre-built image and do no git checkout)."
    )
    assert "git checkout" not in code, (
        "OP-1734/F7 regression: deploy-prod.sh still runs `git checkout`."
    )
    assert "_detect_gerrit_source" not in code and "--gerrit-source" not in code, (
        "OP-1734/F7 regression: deploy-prod.sh still carries the gerrit-source "
        "detection/flag that only the removed tag path used."
    )


def test_deploy_usage_and_help_show_digest_form() -> None:
    """The advertised primary path must be the digest form. `--help` prints
    the digest invocation; the usage header no longer presents `--tag` as a
    deploy example."""
    proc = _run_deploy("--help")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "--backend-digest=sha256:<64hex>" in out
    assert "--frontend-digest=sha256:<64hex>" in out
    # The usage header (script source) must not advertise a `--tag=` deploy
    # example any more — that was the trap.
    text = _deploy_sh_text()
    assert "deploy-prod.sh --tag=v" not in text, (
        "OP-1734 regression: deploy-prod.sh usage still shows a "
        "`deploy-prod.sh --tag=v...` example as a deploy path."
    )


def test_rollback_hint_is_digest_only() -> None:
    """The end-of-run rollback hint must point at the digest form, not the
    retired `--tag=<previous>` redeploy (OP-1734)."""
    text = _deploy_sh_text()
    # The rollback section must offer the per-image digest redeploy…
    assert "--backend-digest=sha256:<prev-64hex>" in text
    # …and must NOT instruct a --tag rollback redeploy.
    assert "--tag=<previous" not in text, (
        "OP-1734 regression: rollback hint still suggests `--tag=<previous>`."
    )


def test_verifier_tag_rejection_is_actionable() -> None:
    """`check_deploy_ref.sh --kind tag` must reject (RT-20 gate intact) AND
    point at the digest deploy command (OP-1734)."""
    proc = _run_verifier(kind="tag", ref="v0.6.2", extra=["--allowlist-only"])
    assert proc.returncode != 0
    assert "git-tag deploys are not permitted" in proc.stderr
    assert "image-tag-only" in proc.stderr
    assert "--backend-digest=sha256:" in proc.stderr
    assert "--frontend-digest=sha256:" in proc.stderr
