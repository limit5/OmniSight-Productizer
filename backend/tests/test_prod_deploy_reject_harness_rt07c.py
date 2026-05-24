"""RT-07c — prod-deploy rejection test harness (=activation).

What this is
------------
The RT-07 trilogy locked the single-trunk release-train production-deploy
contract (ADR-0040). **RT-20 (ADR-0040 §"Decisions LOCKED") then re-scoped
the final release identity to IMAGE-TAG-ONLY**: a release is a promoted
GitLab CR image tag ``vX.Y.Z`` resolving to a validated image DIGEST
(plus the ``release_train`` / ``release_audit`` row). **No ``v*`` git tag
is ever created** — one would trip the existing ``^v`` CI build rule and
rebuild a different digest, breaking "validated digest == shipped digest".

Consequence for ``scripts/check_deploy_ref.sh`` (OP-1704, finding #25):
the v*-git-tag deploy identity — and the git-side **allowlist** (Layer 1)
+ **GPG-signature** (Layer 2) gating, plus the Layer-0 final-tag-shape
gate that pinned it — are **dead** and have been removed. The verifier
now gates on exactly one production deploy identity: a well-formed image
**digest** (cosign owns its content-trust, RT-06). A git **tag** and a
**branch** are never deploy identities and are rejected outright.

* **RT-07b** (``OP-1580``) — the prod compose (``docker-compose.prod.yml``)
  fails closed when required vars are unset.

This harness is the **activation** evidence: it exercises every reject
case *and* the digest pass against the **real committed artifacts** an
operator actually runs — the committed compose — and the operator
entrypoint ``deploy-prod.sh`` end-to-end in ``--dry-run``.

Test harness only — nothing here performs (or can perform) a prod
deploy: the verifier runs in shape-gate mode, the deploy script runs
``--dry-run``, and the compose is only ``config``-parsed (no daemon, no
``up``).

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
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "scripts" / "check_deploy_ref.sh"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy-prod.sh"
# The REAL committed compose — not a synthetic fixture. That is what
# makes this an activation harness rather than a unit drift guard.
COMPOSE = REPO_ROOT / "docker-compose.prod.yml"

GOOD_DIGEST = "sha256:" + "a" * 64

# Compose required-var contract (RT-07b + OP-1515 + OP-1699). To isolate
# the fail-closed assertion on ONE var, every OTHER required var must be
# set so interpolation reaches the var under test.
CLOUDFLARE_TOKEN_VAR = "OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN"
REGISTRY_VAR = "OMNISIGHT_REGISTRY"
IMAGE_TAG_VAR = "OMNISIGHT_IMAGE_TAG"
DATABASE_URL_VAR = "OMNISIGHT_DATABASE_URL"  # OP-1699: prod DSN, no SQLite fallback
ALL_COMPOSE_REQUIRED = {
    REGISTRY_VAR: "reg.example/ns",
    IMAGE_TAG_VAR: "v1.2.3",
    CLOUDFLARE_TOKEN_VAR: "test-tunnel-token",
    DATABASE_URL_VAR: "postgresql://u:p@db:5432/omnisight",
}


# ─── subprocess helpers ─────────────────────────────────────────────


def _run_verifier(
    *,
    kind: str,
    ref: str,
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the verifier against the REAL committed artifacts.

    The image-tag-only verifier consults no allowlist/signers file — the
    digest gate is self-contained — so there is nothing to override.
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
    for p in (VERIFIER, DEPLOY_SH, COMPOSE):
        assert p.exists(), f"RT-07c activation: missing committed artifact {p}"
    for sh in (VERIFIER, DEPLOY_SH):
        assert sh.stat().st_mode & stat.S_IXUSR, f"{sh} must be executable"
        rc = subprocess.run(["bash", "-n", str(sh)], capture_output=True, text=True)
        assert rc.returncode == 0, f"{sh} has a bash syntax error:\n{rc.stderr}"
    # Compose must be parseable YAML before any interpolation test means
    # anything.
    yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# ═════════════════════════════════════════════════════════════════════
# 1. check_deploy_ref.sh REJECT matrix — image-tag-only (RT-20)
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
        # Final semver tags — once a valid identity, now REJECTED (RT-20).
        "v1.2.3",
        "v9.9.9",
        # Non-final / malformed tags — still rejected, now for the same
        # image-tag-only reason rather than a shape mismatch.
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
def test_git_tags_are_rejected_outright(ref: str) -> None:
    """RT-20 (image-tag-only): a git tag is NEVER a production deploy
    identity — no ``v*`` git tag is created — so the verifier rejects the
    ``tag`` kind outright. The old final-tag-shape gate (+ allowlist + GPG
    layers) is gone; there is no shape that accepts."""
    proc = _run_verifier(kind="tag", ref=ref, extra=["--allowlist-only"])
    assert proc.returncode != 0, f"expected reject for {ref!r}: {proc.stderr}"
    assert "git-tag deploys are not permitted" in proc.stderr
    assert "image-tag-only" in proc.stderr


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


def test_required_args_enforced() -> None:
    no_kind = subprocess.run(
        ["bash", str(VERIFIER), "--ref", "v1.2.3"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert no_kind.returncode != 0 and "kind" in no_kind.stderr.lower()

    no_ref = subprocess.run(
        ["bash", str(VERIFIER), "--kind", "digest"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert no_ref.returncode != 0 and "ref" in no_ref.stderr.lower()


def test_unknown_kind_is_rejected() -> None:
    proc = _run_verifier(kind="junk", ref="v1.2.3")
    assert proc.returncode != 0
    assert "digest" in proc.stderr


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
# 3. PASS path — image digest accept (the AC's positive case)
# ═════════════════════════════════════════════════════════════════════


def test_well_formed_digest_accepts_full_verification() -> None:
    """A well-formed digest passes the REAL committed contract end-to-end
    (cosign owns digest content-trust — no allowlist is consulted)."""
    proc = _run_verifier(kind="digest", ref=GOOD_DIGEST)
    assert proc.returncode == 0, proc.stderr
    assert "well-formed image digest" in proc.stderr


def test_deploy_digest_dry_run_passes_end_to_end() -> None:
    """Operator entrypoint, digest identity, full --dry-run: the gate
    accepts and the script walks every step without touching prod."""
    proc = _run_deploy(f"--digest={GOOD_DIGEST}", "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "well-formed image digest" in proc.stdout + proc.stderr


# ═════════════════════════════════════════════════════════════════════
# 4. RT-07b — prod compose fails closed when required vars are unset
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(
    not _docker_compose_available(),
    reason="docker compose v2 not available to interpolate the compose file",
)
def test_compose_fails_closed_when_registry_unset() -> None:
    proc = _compose_config(
        compose_path=COMPOSE,
        env_overrides={
            k: v for k, v in ALL_COMPOSE_REQUIRED.items() if k != REGISTRY_VAR
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
            k: v for k, v in ALL_COMPOSE_REQUIRED.items() if k != IMAGE_TAG_VAR
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
