"""OP-1739 — release_preflight.sh contract.

A read-only preflight that asserts the implicit prod-deploy prerequisites,
prints a PASS/FAIL checklist, and prints the exact next command only when
everything passes. These tests drive it over a fully-stubbed fixture
environment so no network / docker / cosign is required: one all-green run
and one deliberately-broken run (per AC "Exercised").
"""
from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release_preflight.sh"

CANDIDATE = "v9.9.9-rc1"
BACKEND_DIGEST = "sha256:" + "a" * 64
FRONTEND_DIGEST = "sha256:" + "b" * 64


def _stub(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)
    return path


def _fixture(tmp_path: Path, *, with_passphrase: bool = True, tag: str = CANDIDATE):
    """Build a self-contained fixture environment + the script's env/argv.

    Returns (env, args). Every external dependency (docker, cosign, the
    GitLab CR check, the staging gate) is stubbed to succeed, so the only
    thing under test is the preflight's own assertion + reporting logic.
    """
    bind = tmp_path / "bin"
    bind.mkdir()
    # docker stub: `docker compose --env-file F -f C config` resolves clean.
    _stub(bind / "docker", "#!/usr/bin/env bash\nexit 0\n")
    # cosign stub pointed at via COSIGN_BIN (OP-1736 contract).
    cosign = _stub(bind / "cosign", "#!/usr/bin/env bash\nexit 0\n")

    prod_env = tmp_path / "prod.env"
    prod_env.write_text(
        f"OMNISIGHT_REGISTRY=reg.example/omnisight\nOMNISIGHT_IMAGE_TAG={tag}\n",
        encoding="utf-8",
    )

    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps(
            {
                "bundle_id": "bundle-test",
                "images": {
                    "backend": {"repository": "reg.example/omnisight/backend",
                                "digest": BACKEND_DIGEST, "tag": CANDIDATE},
                    "frontend": {"repository": "reg.example/omnisight/frontend",
                                 "digest": FRONTEND_DIGEST, "tag": CANDIDATE},
                },
                "signatures": [],
            }
        ),
        encoding="utf-8",
    )

    backup_dr = tmp_path / "backup-dr.env"
    backup_dr.write_text(
        "OMNISIGHT_BACKUP_PASSPHRASE=hunter2\n" if with_passphrase else "# no passphrase here\n",
        encoding="utf-8",
    )

    # Start from a clean env: drop any ambient OMNISIGHT_* (the runner may
    # export OMNISIGHT_BACKUP_PASSPHRASE etc.) so the fixture is authoritative.
    env = {k: v for k, v in os.environ.items() if not k.startswith("OMNISIGHT_")}
    env.pop("COSIGN_BIN", None)
    env["PATH"] = f"{bind}:{env.get('PATH', '')}"
    env["COSIGN_BIN"] = str(cosign)
    env["OMNISIGHT_BACKUP_DR_ENV"] = str(backup_dr)
    env["OMNISIGHT_PREFLIGHT_DOCKER"] = str(bind / "docker")
    env["OMNISIGHT_PREFLIGHT_GITLAB_CR_CMD"] = "true"
    env["OMNISIGHT_PREFLIGHT_STAGING_GATE_CMD"] = "true"

    args = [
        "--candidate", CANDIDATE,
        "--bundle", str(bundle),
        "--env-file", str(prod_env),
        "--compose", str(REPO_ROOT / "docker-compose.prod.yml"),
    ]
    return env, args


def _run(env, args) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_release_preflight_is_valid_bash() -> None:
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr


def test_all_green_run_passes_and_prints_next_command(tmp_path: Path) -> None:
    env, args = _fixture(tmp_path, with_passphrase=True)
    proc = _run(env, args)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0 failed" in proc.stdout
    assert "All prerequisites PASSED" in proc.stdout
    # The exact next command carries the bundle's validated digests.
    assert f"--backend-digest={BACKEND_DIGEST}" in proc.stdout
    assert f"--frontend-digest={FRONTEND_DIGEST}" in proc.stdout
    assert "scripts/deploy-prod.sh" in proc.stdout
    # It is a guide, not an orchestrator.
    assert "executed NOTHING" in proc.stdout


def test_unset_passphrase_is_reported_fail_not_skipped(tmp_path: Path) -> None:
    # The AC's named broken prereq: unset backup passphrase.
    env, args = _fixture(tmp_path, with_passphrase=False)
    proc = _run(env, args)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    # Reported FAIL explicitly...
    assert "❌ FAIL" in proc.stdout
    assert "OMNISIGHT_BACKUP_PASSPHRASE" in proc.stdout
    # ...and NOT silently skipped: the other prereqs still ran + passed.
    assert "✅ PASS" in proc.stdout
    assert "cosign resolvable" in proc.stdout
    # The next command is withheld until every prereq passes.
    assert f"--backend-digest={BACKEND_DIGEST}" not in proc.stdout
    assert "prerequisite(s) FAILED" in proc.stdout


def test_digest_shaped_image_tag_is_reported_fail(tmp_path: Path) -> None:
    # OMNISIGHT_IMAGE_TAG must be tag-shaped; a digest there is the wrong
    # path and must be caught (not fed to the tag-path compose interpolation).
    env, args = _fixture(tmp_path, tag="sha256:" + "c" * 64)
    proc = _run(env, args)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "digest-shaped" in proc.stdout
