"""RT-07c — prod-deploy rejection test harness (=activation).

What this is
------------
The RT-07 trilogy locks the single-trunk release-train production-deploy
contract (ADR-0040):

* **RT-07a** (``OP-1579``) — ``scripts/check_deploy_ref.sh`` +
  ``scripts/deploy-prod.sh`` accept ONLY a final semver tag ``vX.Y.Z``
  or an image digest ``sha256:<64 lowercase hex>``; branch deploys, the
  ``main`` default, and the ``--insecure-skip-verify`` bypass are gone.
* **RT-07b** (``OP-1580``) — the prod compose
  (``docker-compose.prod.yml``) fails closed when ``OMNISIGHT_IMAGE_TAG``
  / ``OMNISIGHT_REGISTRY`` are unset, and the prod allowlist
  (``deploy/prod-deploy-allowlist.txt``) carries final-tag rules only —
  every branch rule was removed.

RT-07a shipped a *drift guard* (``test_deploy_prod_ref_verifier_drift_guard``)
that pins the verifier's shape gate using **synthetic ``tmp_path``
allowlists**. RT-07b shipped its allowlist + compose edits with **no
tests at all**.

This harness is the **activation** evidence the RT-07 META asks for: it
exercises every reject case *and* the final-tag/digest pass against the
**real committed artifacts** an operator actually runs — the committed
allowlist, the committed signers file, the committed compose — and the
operator entrypoint ``deploy-prod.sh`` end-to-end in ``--dry-run``. If
any RT-07a/b guarantee silently regresses in the files that ship, a case
here goes red.

Test harness only — nothing here performs (or can perform) a prod
deploy: the verifier runs in ``--allowlist-only`` shape-gate mode, the
deploy script runs ``--dry-run``, and the compose is only ``config``-
parsed (no daemon, no ``up``).

Why subprocess (not imported helpers)
-------------------------------------
The contract is bash exit codes + operator-facing stderr and a Compose
interpolation error — that is exactly what an operator sees. Pinning the
real process boundary is the point.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "scripts" / "check_deploy_ref.sh"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy-prod.sh"
# The REAL committed policy artifacts — not synthetic fixtures. That is
# what makes this an activation harness rather than a unit drift guard.
ALLOWLIST = REPO_ROOT / "deploy" / "prod-deploy-allowlist.txt"
SIGNERS = REPO_ROOT / "deploy" / "prod-deploy-signers.txt"
COMPOSE = REPO_ROOT / "docker-compose.prod.yml"

GOOD_DIGEST = "sha256:" + "a" * 64

# Compose required-var contract (RT-07b + OP-1515). To isolate the
# fail-closed assertion on ONE var, every OTHER required var must be set
# so interpolation reaches the var under test.
CLOUDFLARE_TOKEN_VAR = "OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN"
REGISTRY_VAR = "OMNISIGHT_REGISTRY"
IMAGE_TAG_VAR = "OMNISIGHT_IMAGE_TAG"
ALL_COMPOSE_REQUIRED = {
    REGISTRY_VAR: "reg.example/ns",
    IMAGE_TAG_VAR: "v1.2.3",
    CLOUDFLARE_TOKEN_VAR: "test-tunnel-token",
}


# ─── subprocess helpers ─────────────────────────────────────────────


def _run_verifier(
    *,
    kind: str,
    ref: str,
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the verifier against the REAL committed allowlist/signers.

    No ``--allowlist`` / ``--signers`` override: the script defaults to
    the committed files, which is the whole point of the activation
    harness.
    """
    cmd = ["bash", str(VERIFIER), "--kind", kind, "--ref", ref]
    if extra:
        cmd += extra
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), env=env
    )


def _run_deploy(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(DEPLOY_SH), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True
    )
    return probe.returncode == 0


def _gpg_available() -> bool:
    return shutil.which("gpg") is not None


def _gerrit_remote_detectable() -> bool:
    """Mirror deploy-prod.sh::_detect_gerrit_source — the tag dry-run
    path fetches from a Gerrit remote, so the operator-entrypoint pass
    test only makes sense where one is configured."""
    out = subprocess.run(
        ["git", "remote", "-v"], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    if out.returncode != 0:
        return False
    text = out.stdout
    return any(
        marker in text for marker in ("gerrit", "sora.services", "29418")
    )


def _compose_config(
    *, compose_path: Path, env_overrides: dict[str, str], drop: set[str]
) -> subprocess.CompletedProcess:
    """``docker compose -f <compose_path> config`` with a controlled env.

    ``drop`` keys are removed from the inherited environment so an
    ambient export can't mask a fail-closed assertion.
    """
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(env_overrides)
    return subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "config"],
        capture_output=True,
        text=True,
        cwd=str(compose_path.parent),
        env=env,
    )


# ═════════════════════════════════════════════════════════════════════
# 0. Activation preconditions — the real artifacts exist and are sane
# ═════════════════════════════════════════════════════════════════════


def test_real_artifacts_present_and_runnable() -> None:
    for p in (VERIFIER, DEPLOY_SH, ALLOWLIST, SIGNERS, COMPOSE):
        assert p.exists(), f"RT-07c activation: missing committed artifact {p}"
    for sh in (VERIFIER, DEPLOY_SH):
        assert sh.stat().st_mode & stat.S_IXUSR, f"{sh} must be executable"
        rc = subprocess.run(["bash", "-n", str(sh)], capture_output=True, text=True)
        assert rc.returncode == 0, f"{sh} has a bash syntax error:\n{rc.stderr}"
    # Compose must be parseable YAML before any interpolation test means
    # anything.
    yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# ═════════════════════════════════════════════════════════════════════
# 1. check_deploy_ref.sh REJECT matrix — against the REAL allowlist
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "ref",
    ["main", "develop", "release/2.5", "hotfix/v0.3.1", "feature/anything"],
)
def test_branch_kind_is_rejected_outright(ref: str) -> None:
    proc = _run_verifier(kind="branch", ref=ref, extra=["--allowlist-only"])
    assert proc.returncode != 0, proc.stderr
    assert "branch deploys are not permitted" in proc.stderr


@pytest.mark.parametrize(
    "ref",
    [
        "v1.2.3-rc.1",
        "v1.2.3-hotfix.2",
        "v1.2.3-alpha",
        "v1.2",
        "v1",
        "1.2.3",
        "vlatest",
        "release-x",
    ],
)
def test_non_final_tags_are_rejected(ref: str) -> None:
    proc = _run_verifier(kind="tag", ref=ref, extra=["--allowlist-only"])
    assert proc.returncode != 0, f"expected reject for {ref!r}: {proc.stderr}"
    assert "not a FINAL release tag" in proc.stderr


@pytest.mark.parametrize(
    "ref",
    [
        "sha256:deadbeef",         # too short
        "sha256:" + "a" * 63,      # 63 hex
        "sha256:" + "a" * 65,      # 65 hex
        "sha256:" + "A" * 64,      # uppercase rejected
        "sha512:" + "a" * 64,      # wrong algorithm
        "a" * 64,                  # missing sha256: prefix
        "sha256:" + "g" * 64,      # non-hex char
    ],
)
def test_malformed_digests_are_rejected(ref: str) -> None:
    proc = _run_verifier(kind="digest", ref=ref)
    assert proc.returncode != 0, f"expected reject for {ref!r}"
    assert "malformed" in proc.stderr


def test_well_formed_final_tag_fails_closed_at_signers_gate() -> None:
    """A well-formed, allowlisted final tag still REJECTS under the REAL
    signers file because no matching public key is imported (and no such
    tag is signed locally). Proves Layer 2 is active and fail-closed on
    the committed artifacts — not bypassed for shape-valid tags."""
    proc = _run_verifier(kind="tag", ref="v1.2.3")  # full verification
    assert proc.returncode != 0
    # It must have passed Layer 0 + Layer 1 (shape + allowlist) first…
    assert "Layer 1: ref 'tag:v1.2.3' matched allowlist" in proc.stderr
    # …then failed closed at the GPG signature gate.
    assert "not GPG-signed" in proc.stderr or "VALIDSIG" in proc.stderr


def test_required_args_enforced() -> None:
    no_kind = subprocess.run(
        ["bash", str(VERIFIER), "--ref", "v1.2.3"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert no_kind.returncode != 0 and "kind" in no_kind.stderr.lower()

    no_ref = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "tag"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert no_ref.returncode != 0 and "ref" in no_ref.stderr.lower()


def test_unknown_kind_is_rejected() -> None:
    proc = _run_verifier(kind="junk", ref="v1.2.3")
    assert proc.returncode != 0
    assert "tag" in proc.stderr and "digest" in proc.stderr


def test_removed_insecure_flag_is_unknown_arg() -> None:
    """The retired bypass is now just an unknown argument — no escape."""
    proc = _run_verifier(
        kind="branch", ref="main", extra=["--insecure-skip-verify"]
    )
    assert proc.returncode != 0
    assert "unknown arg" in proc.stderr.lower()


# ═════════════════════════════════════════════════════════════════════
# 2. deploy-prod.sh REJECT matrix — the operator entrypoint, REAL files
# ═════════════════════════════════════════════════════════════════════


def test_deploy_requires_a_final_identity() -> None:
    proc = _run_deploy()  # no --tag / --digest
    assert proc.returncode != 0
    assert "final deploy identity is required" in proc.stdout + proc.stderr


def test_deploy_rejects_branch_flag() -> None:
    proc = _run_deploy("--branch=main")
    assert proc.returncode != 0
    assert "Unknown argument" in proc.stdout + proc.stderr


def test_deploy_rejects_insecure_flag() -> None:
    proc = _run_deploy("--insecure-skip-verify", "--tag=v1.2.3")
    assert proc.returncode != 0
    assert "Unknown argument" in proc.stdout + proc.stderr


def test_deploy_tag_and_digest_mutually_exclusive() -> None:
    proc = _run_deploy("--tag=v1.2.3", f"--digest={GOOD_DIGEST}")
    assert proc.returncode != 0
    assert "mutually exclusive" in proc.stdout + proc.stderr


def test_deploy_rejects_bad_alembic_mode() -> None:
    proc = _run_deploy(f"--digest={GOOD_DIGEST}", "--alembic-mode=wipe", "--dry-run")
    assert proc.returncode != 0
    assert "alembic-mode" in proc.stdout + proc.stderr


# ═════════════════════════════════════════════════════════════════════
# 3. PASS path — final tag / digest accept (the AC's positive case)
# ═════════════════════════════════════════════════════════════════════


def test_well_formed_digest_accepts_full_verification() -> None:
    """A well-formed digest passes the REAL committed contract end-to-end
    (the allowlist is not consulted — cosign owns digest content-trust)."""
    proc = _run_verifier(kind="digest", ref=GOOD_DIGEST)
    assert proc.returncode == 0, proc.stderr
    assert "well-formed image digest" in proc.stderr


def test_final_tag_passes_shape_and_real_allowlist() -> None:
    """Layer 0 (shape) + Layer 1 (REAL committed allowlist) accept a
    final semver tag. Layer 2 (GPG) is exercised separately — it needs a
    signed tag + imported key."""
    proc = _run_verifier(kind="tag", ref="v9.9.9", extra=["--allowlist-only"])
    assert proc.returncode == 0, proc.stderr
    assert "is a final release tag" in proc.stderr
    assert "matched allowlist" in proc.stderr


def test_deploy_digest_dry_run_passes_end_to_end() -> None:
    """Operator entrypoint, digest identity, full --dry-run: the gate
    accepts and the script walks every step without touching prod."""
    proc = _run_deploy(f"--digest={GOOD_DIGEST}", "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "well-formed image digest" in proc.stdout + proc.stderr


@pytest.mark.skipif(
    not _gerrit_remote_detectable(),
    reason="no Gerrit-like git remote configured; deploy-prod tag path fetches one",
)
def test_deploy_tag_dry_run_passes_end_to_end() -> None:
    """Operator entrypoint, final-tag identity, full --dry-run."""
    proc = _run_deploy("--tag=v1.2.3", "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "final release tag" in proc.stdout + proc.stderr


# ═════════════════════════════════════════════════════════════════════
# 4. RT-07b — prod compose fails closed; allowlist carries tags only
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(
    not _docker_compose_available(),
    reason="docker compose v2 not available to interpolate the compose file",
)
def test_compose_fails_closed_when_registry_unset() -> None:
    proc = _compose_config(
        compose_path=COMPOSE,
        env_overrides={
            IMAGE_TAG_VAR: "v1.2.3",
            CLOUDFLARE_TOKEN_VAR: "test-tunnel-token",
        },
        drop={REGISTRY_VAR},
    )
    assert proc.returncode != 0, "compose must fail closed when registry unset"
    assert "OMNISIGHT_REGISTRY" in proc.stderr


@pytest.mark.skipif(
    not _docker_compose_available(),
    reason="docker compose v2 not available to interpolate the compose file",
)
def test_compose_fails_closed_when_image_tag_unset() -> None:
    """Every other required var is set so interpolation reaches — and
    fails on — the unset image tag (RT-07b: no implicit ``latest``)."""
    proc = _compose_config(
        compose_path=COMPOSE,
        env_overrides={
            REGISTRY_VAR: "reg.example/ns",
            CLOUDFLARE_TOKEN_VAR: "test-tunnel-token",
        },
        drop={IMAGE_TAG_VAR},
    )
    assert proc.returncode != 0, "compose must fail closed when image tag unset"
    assert "OMNISIGHT_IMAGE_TAG" in proc.stderr


@pytest.mark.skipif(
    not _docker_compose_available(),
    reason="docker compose v2 not available to interpolate the compose file",
)
def test_compose_config_succeeds_when_all_required_vars_set(tmp_path: Path) -> None:
    """The positive compose case: with every required var set, ``config``
    interpolates cleanly. Run from a tmp copy with an empty ``.env`` so
    the service-level ``env_file: .env`` (absent in the repo) resolves."""
    staged = tmp_path / "docker-compose.prod.yml"
    staged.write_text(COMPOSE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text("", encoding="utf-8")
    proc = _compose_config(
        compose_path=staged,
        env_overrides=dict(ALL_COMPOSE_REQUIRED),
        drop=set(),
    )
    assert proc.returncode == 0, proc.stderr


def _parse_allowlist_rules(path: Path) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        kind, _, value = line.partition(" ")
        rules.append((kind, value.strip()))
    return rules


def test_real_allowlist_has_no_branch_rules() -> None:
    """RT-07b removed every branch / branch-regex rule — a branch can no
    longer be allowlisted into prod even by editing this file."""
    rules = _parse_allowlist_rules(ALLOWLIST)
    branch_rules = [(k, v) for k, v in rules if k in {"branch", "branch-regex"}]
    assert not branch_rules, (
        f"RT-07b regression: prod allowlist still has branch rule(s) "
        f"{branch_rules}; only `tag-regex` rules are permitted."
    )
    assert any(k == "tag-regex" for k, _ in rules), (
        "prod allowlist must keep at least one tag-regex rule or every "
        "final-tag deploy aborts at Layer 1."
    )


# ═════════════════════════════════════════════════════════════════════
# 5. Optional GPG end-to-end PASS — the full Layer 0→1→2 green chain
# ═════════════════════════════════════════════════════════════════════


def _gen_test_key(tmp_path: Path) -> tuple[str, Path]:
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
            Name-Real: OmniSight RT-07c Activation Test
            Name-Email: rt07c-harness@omnisight.local
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
         "rt07c-harness@omnisight.local"],
        check=True, capture_output=True, text=True, env=env,
    )
    fpr = ""
    for line in out.stdout.splitlines():
        if line.startswith("fpr:"):
            fpr = line.split(":")[9]
            break
    assert fpr and len(fpr) == 40, f"unexpected gpg fpr listing: {out.stdout!r}"
    return fpr, gnupg_home


def _init_signed_tag_repo(tmp_path: Path, fpr: str, gnupg_home: Path, *, tag: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], check=True, capture_output=True, cwd=str(repo), env=env
        )

    git("init", "-q", "-b", "develop")
    git("config", "user.email", "rt07c-harness@omnisight.local")
    git("config", "user.name", "RT-07c Activation Test")
    git("config", "user.signingkey", fpr)
    git("config", "gpg.program", "gpg")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "--no-gpg-sign", "-q", "-m", "rt07c: test commit")
    git("tag", "-s", "-u", fpr, "-m", f"release {tag}", tag)
    return repo


@pytest.mark.skipif(not _gpg_available(), reason="gpg binary not available")
def test_signed_final_tag_passes_full_chain_when_trusted(tmp_path: Path) -> None:
    """End-to-end positive: a final tag that is shape-valid, allowlisted,
    AND signed by a trusted fingerprint passes Layer 0→1→2. Uses a tmp
    signers file (the real one lists a key whose private half we cannot
    hold) but the REAL committed allowlist regex."""
    fpr, gnupg_home = _gen_test_key(tmp_path)
    repo = _init_signed_tag_repo(tmp_path, fpr, gnupg_home, tag="v1.2.3")
    signers = tmp_path / "sign.txt"
    signers.write_text(fpr + "\n", encoding="utf-8")

    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    proc = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "tag", "--ref", "v1.2.3",
         "--allowlist", str(ALLOWLIST), "--signers", str(signers)],
        capture_output=True, text=True, cwd=str(repo), env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "signed by trusted fingerprint" in proc.stderr
