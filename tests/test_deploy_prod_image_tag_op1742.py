"""OP-1742 — a digest deploy must NOT write a digest-as-tag into .env.

Background
----------
``scripts/deploy-prod.sh`` records ``OMNISIGHT_IMAGE_TAG`` in ``.env`` for the
OP-772 SLO monitor. On a digest deploy (``--backend-digest`` /
``--frontend-digest``) the pre-OP-1742 derivation

    CURRENT_IMAGE_TAG="${OMNISIGHT_IMAGE_TAG:-${TAG:-${BACKEND_DIGEST:-$FRONTEND_DIGEST}}}"

fell through to the digest, so it wrote ``OMNISIGHT_IMAGE_TAG=sha256:<digest>``
— a *digest-as-tag*. The OP-1717 ``ExecStartPre`` boot-guard on
``omnisight-compose-prod.service`` rejects ``^OMNISIGHT_IMAGE_TAG=(sha256:|@)``
(added to catch the v0.6.2 landmine), so the next reboot fail-closed → prod
would not auto-start. The real deploy identity lives in
``OMNISIGHT_{BACKEND,FRONTEND}_IMAGE_REF`` (``<registry>/<image>@sha256:...``),
which the compose consumes; ``OMNISIGHT_IMAGE_TAG`` is only the mutable :tag
fallback and must stay TAG-shaped.

OP-1742 keeps ``OMNISIGHT_IMAGE_TAG`` tag-shaped on a digest deploy (it keeps
the prior .env value, never the digest) while the digest still pins both
images via ``IMAGE_REF``.

Why subprocess + a sandbox copy
-------------------------------
The contract is what ``deploy-prod.sh`` actually writes into ``.env`` at the
real process boundary. We run it from a sandbox repo (real ``deploy-prod.sh``
+ ``check_deploy_ref.sh`` so the digest gate passes) with NO backup helper, so
a real (non --dry-run) deploy writes the IMAGE_REF + IMAGE_TAG keys and then
fails CLOSED at the Step 1b pre-deploy backup (OP-1740) — *after* the .env
writes under test but *before* any docker step. That keeps the test hermetic
on a host with no docker daemon.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy-prod.sh"
VERIFIER = REPO_ROOT / "scripts" / "check_deploy_ref.sh"

# Distinct per-image digests so we can assert each ref carries its OWN digest.
BACKEND_DIGEST = "sha256:" + "a" * 64
FRONTEND_DIGEST = "sha256:" + "b" * 64
REGISTRY = "reg.example/ns"

# The OP-1717 ExecStartPre boot-guard rejects a digest-as-tag with exactly
# this shape (see omnisight-compose-prod.service / the deploy-fragility audit).
BOOT_GUARD_RE = re.compile(r"^OMNISIGHT_IMAGE_TAG=(sha256:|@)", re.MULTILINE)


def _make_sandbox(tmp_path: Path, *, env_seed: str) -> Path:
    """A minimal repo sandbox deploy-prod.sh can run a digest deploy in.

    Contains the real ``deploy-prod.sh`` + ``check_deploy_ref.sh`` and a
    ``.env`` seeded with ``env_seed``. No ``backup_prod_db.sh`` is present, so
    a real deploy aborts (fail-closed) at Step 1b — after the .env writes.
    """
    sandbox = tmp_path / "repo"
    scripts = sandbox / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(DEPLOY_SH, scripts / "deploy-prod.sh")
    shutil.copy2(VERIFIER, scripts / "check_deploy_ref.sh")
    (sandbox / ".env").write_text(env_seed, encoding="utf-8")
    return sandbox


def _run_deploy(sandbox: Path, *args: str, dry_run: bool = False) -> subprocess.CompletedProcess:
    """Run a digest deploy (both images) in the sandbox.

    Real (non --dry-run) runs abort at Step 1b backup (no helper present); the
    .env writes under test have already happened by then.
    """
    env = {k: v for k, v in os.environ.items() if k != "OMNISIGHT_BACKUP_PASSPHRASE"}
    env["OMNISIGHT_REGISTRY"] = REGISTRY
    extra = ["--dry-run"] if dry_run else []
    return subprocess.run(
        [
            "bash", str(sandbox / "scripts" / "deploy-prod.sh"),
            f"--backend-digest={BACKEND_DIGEST}",
            f"--frontend-digest={FRONTEND_DIGEST}",
            *extra, *args,
        ],
        capture_output=True, text=True, cwd=str(sandbox), env=env,
    )


def _env_value(env_text: str, key: str) -> str | None:
    """Last value for ``key`` in a .env body (deploy-prod upserts in place)."""
    val = None
    for line in env_text.splitlines():
        if line.startswith(f"{key}="):
            val = line.split("=", 1)[1]
    return val


# ═════════════════════════════════════════════════════════════════════
# AC: a digest deploy keeps OMNISIGHT_IMAGE_TAG tag-shaped; digest → IMAGE_REF
# ═════════════════════════════════════════════════════════════════════


def test_digest_deploy_keeps_image_tag_tag_shaped(tmp_path: Path) -> None:
    """Prior tag-shaped OMNISIGHT_IMAGE_TAG survives a digest deploy; the
    digest lands in OMNISIGHT_{BACKEND,FRONTEND}_IMAGE_REF, never in the tag."""
    sandbox = _make_sandbox(tmp_path, env_seed="OMNISIGHT_IMAGE_TAG=v0.6.2\n")
    _run_deploy(sandbox)  # aborts at Step 1b backup; .env already written
    env_text = (sandbox / ".env").read_text(encoding="utf-8")

    # The tag stayed exactly its prior, tag-shaped value — NOT the digest.
    assert _env_value(env_text, "OMNISIGHT_IMAGE_TAG") == "v0.6.2", env_text
    assert "OMNISIGHT_IMAGE_TAG=sha256:" not in env_text, env_text

    # The digest is where it belongs: each image's own content-addressed ref.
    assert _env_value(env_text, "OMNISIGHT_BACKEND_IMAGE_REF") == \
        f"{REGISTRY}/backend@{BACKEND_DIGEST}", env_text
    assert _env_value(env_text, "OMNISIGHT_FRONTEND_IMAGE_REF") == \
        f"{REGISTRY}/frontend@{FRONTEND_DIGEST}", env_text


def test_resulting_env_passes_op1717_boot_guard(tmp_path: Path) -> None:
    """Integration AC: the .env left by a digest deploy is NOT matched by the
    OP-1717 boot-guard regex → the next reboot would auto-start prod."""
    sandbox = _make_sandbox(tmp_path, env_seed="OMNISIGHT_IMAGE_TAG=v0.6.2\n")
    _run_deploy(sandbox)
    env_text = (sandbox / ".env").read_text(encoding="utf-8")
    assert BOOT_GUARD_RE.search(env_text) is None, \
        f"boot-guard would fail-close on:\n{env_text}"


def test_dry_run_never_prints_digest_as_tag(tmp_path: Path) -> None:
    """The ticket's reproduction: ``--dry-run`` used to print
    'set OMNISIGHT_IMAGE_TAG=sha256:...'. It must now print the prior tag and
    route the digest to IMAGE_REF instead."""
    sandbox = _make_sandbox(tmp_path, env_seed="OMNISIGHT_IMAGE_TAG=v0.6.2\n")
    proc = _run_deploy(sandbox, dry_run=True)
    out = proc.stdout + proc.stderr
    assert "set OMNISIGHT_IMAGE_TAG=sha256:" not in out, out
    assert "set OMNISIGHT_IMAGE_TAG=v0.6.2 in .env" in out, out
    # The digest is still shown going into the per-image refs.
    assert f"set OMNISIGHT_BACKEND_IMAGE_REF={REGISTRY}/backend@{BACKEND_DIGEST}" in out, out
    assert f"set OMNISIGHT_FRONTEND_IMAGE_REF={REGISTRY}/frontend@{FRONTEND_DIGEST}" in out, out


def test_prior_digest_as_tag_is_not_propagated(tmp_path: Path) -> None:
    """Defensive: if .env is ALREADY in the bad digest-as-tag state (a pre-fix
    deploy), deploy-prod refuses to re-write a digest into the tag and warns —
    it never appends a fresh OMNISIGHT_IMAGE_TAG=sha256: line of its own."""
    bad = f"OMNISIGHT_IMAGE_TAG={BACKEND_DIGEST}\n"
    sandbox = _make_sandbox(tmp_path, env_seed=bad)
    proc = _run_deploy(sandbox)
    out = proc.stdout + proc.stderr
    env_text = (sandbox / ".env").read_text(encoding="utf-8")

    assert "refusing to re-write a digest-as-tag" in out, out
    # Exactly the one seeded line — deploy added no new digest-as-tag line, and
    # crucially did NOT record the digest as the PREVIOUS tag either.
    assert env_text.count("OMNISIGHT_IMAGE_TAG=") == 1, env_text
    assert "OMNISIGHT_PREVIOUS_IMAGE_TAG=sha256:" not in env_text, env_text
    # The deploy identity is still pinned by digest via the ref.
    assert _env_value(env_text, "OMNISIGHT_BACKEND_IMAGE_REF") == \
        f"{REGISTRY}/backend@{BACKEND_DIGEST}", env_text


# ═════════════════════════════════════════════════════════════════════
# Static guards — the source carries the OP-1742 contract
# ═════════════════════════════════════════════════════════════════════


def test_deploy_script_is_valid_bash() -> None:
    rc = subprocess.run(["bash", "-n", str(DEPLOY_SH)], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stderr


def test_source_no_longer_derives_tag_from_digest() -> None:
    """The pre-OP-1742 derivation that fell through to the digest is gone, and
    the OP-1742 rationale is recorded in the source."""
    body = DEPLOY_SH.read_text(encoding="utf-8")
    # The old assignment let CURRENT_IMAGE_TAG fall through to the digest. The
    # mention in a comment (explaining what was removed) is fine; the live
    # assignment must be gone.
    assert 'CURRENT_IMAGE_TAG="${OMNISIGHT_IMAGE_TAG:-${TAG:-${BACKEND_DIGEST:-$FRONTEND_DIGEST}}}"' not in body
    assert "OP-1742" in body
