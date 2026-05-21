"""[OP-1574] RT-05b — deploy candidate BY DIGEST + post-pull digest equality.

Two scripts cooperate to make staging serve *exactly* the certified candidate:

  * ``scripts/staging_deploy.sh`` — after ``docker compose pull`` it inspects
    the digest actually pulled for backend + frontend and asserts it equals
    the candidate bundle's (OP-1513 ``bundle.json`` shape). A mismatch — the
    tag alias was retagged under us — is REJECTED before the standby stack
    starts (no ``compose up``, no ingress switch).
  * ``scripts/sync_staging_to_develop.sh`` — resolves the develop tip's
    candidate bundle (``$SYNC_CANDIDATE_BUNDLE``), refuses if the bundle is
    for a different git_sha, and passes it through as ``--bundle`` so the
    deployer runs the digest-equality gate.

The tests are hermetic: a fake ``docker`` emulates ``image inspect`` /
``compose`` so no daemon, registry, or network is needed; the deployer is
sourced to exercise the digest helpers directly, and driven end-to-end for
the rejection path. ``sync`` is driven with a fake ``staging_deploy.sh`` that
records its argv. Stdlib + pytest only.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGING_DEPLOY = REPO_ROOT / "scripts" / "staging_deploy.sh"
SYNC_STAGING = REPO_ROOT / "scripts" / "sync_staging_to_develop.sh"

REGISTRY = "reg.example/ns"
BACKEND_DIGEST = "sha256:" + "1" * 64
FRONTEND_DIGEST = "sha256:" + "2" * 64
WRONG_DIGEST = "sha256:" + "9" * 64
TIP_SHA = "a" * 40


def _bundle(path: Path, *, git_sha: str = TIP_SHA,
            backend: str | None = BACKEND_DIGEST,
            frontend: str | None = FRONTEND_DIGEST) -> Path:
    """Write an OP-1513-shaped candidate bundle.json."""
    images: dict[str, dict[str, str]] = {}
    if backend is not None:
        images["backend"] = {"digest": backend}
    if frontend is not None:
        images["frontend"] = {"digest": frontend}
    path.write_text(json.dumps({"git_sha": git_sha, "images": images}))
    return path


def _fake_docker(tmp_path: Path, *, backend: str, frontend: str) -> Path:
    """A docker stub: logs argv, answers `image inspect` with RepoDigests, and
    treats every `compose ...` (pull/up/...) as a success."""
    docker = tmp_path / "docker"
    docker.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> "${{FAKE_DOCKER_LOG:-/dev/null}}"
            if [[ "$1" == "image" && "$2" == "inspect" ]]; then
              ref="$3"
              if [[ "$ref" == *"/backend:"* ]]; then
                printf '["repo@{backend}"]\\n'
              else
                printf '["repo@{frontend}"]\\n'
              fi
              exit 0
            fi
            exit 0
            """
        )
    )
    docker.chmod(0o755)
    return docker


def _run_deploy_func(tmp_path: Path, func_call: str, env_extra: dict[str, str]):
    """Source staging_deploy.sh (main is guarded out) and run a helper."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env_extra}
    script = f'source "{STAGING_DEPLOY}"; {func_call}'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, env=env, timeout=60,
    )


# ── verify_pulled_digests: the digest-equality helper ───────────────────────

def test_pulled_digests_match_candidate_bundle_passes(tmp_path: Path):
    bundle = _bundle(tmp_path / "bundle.json")
    docker = _fake_docker(tmp_path, backend=BACKEND_DIGEST, frontend=FRONTEND_DIGEST)
    proc = _run_deploy_func(
        tmp_path,
        'if verify_pulled_digests sha-cand; then echo MATCH; else echo "FAIL rc=$?"; fi',
        {
            "DOCKER_BIN": str(docker),
            "OMNISIGHT_REGISTRY": REGISTRY,
            "OMNISIGHT_CANDIDATE_BUNDLE": str(bundle),
        },
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "MATCH" in proc.stdout
    # both runtime images were inspected against the candidate bundle
    assert "pulled digest matches candidate bundle" in proc.stdout


def test_pulled_backend_digest_mismatch_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path / "bundle.json")
    # registry serves a *different* backend digest than the bundle pins.
    docker = _fake_docker(tmp_path, backend=WRONG_DIGEST, frontend=FRONTEND_DIGEST)
    proc = _run_deploy_func(
        tmp_path,
        'if verify_pulled_digests sha-cand; then echo MATCH; else echo "REJECTED rc=$?"; fi',
        {
            "DOCKER_BIN": str(docker),
            "OMNISIGHT_REGISTRY": REGISTRY,
            "OMNISIGHT_CANDIDATE_BUNDLE": str(bundle),
        },
    )
    assert "REJECTED" in proc.stdout, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "MATCH" not in proc.stdout
    assert "candidate bundle pins" in proc.stderr
    assert BACKEND_DIGEST in proc.stderr  # the expected digest is named


def test_no_bundle_skips_digest_verification(tmp_path: Path):
    docker = _fake_docker(tmp_path, backend=WRONG_DIGEST, frontend=WRONG_DIGEST)
    proc = _run_deploy_func(
        tmp_path,
        'if verify_pulled_digests sha-cand; then echo SKIPPED; else echo "FAIL rc=$?"; fi',
        {"DOCKER_BIN": str(docker), "OMNISIGHT_REGISTRY": REGISTRY},
    )
    assert proc.returncode == 0
    assert "SKIPPED" in proc.stdout
    # staging_deploy.sh's log()/alert() write to stdout.
    assert "skipping post-pull digest verification" in proc.stdout


def test_missing_bundle_file_is_rejected(tmp_path: Path):
    docker = _fake_docker(tmp_path, backend=BACKEND_DIGEST, frontend=FRONTEND_DIGEST)
    proc = _run_deploy_func(
        tmp_path,
        'if verify_pulled_digests sha-cand; then echo MATCH; else echo "REJECTED rc=$?"; fi',
        {
            "DOCKER_BIN": str(docker),
            "OMNISIGHT_REGISTRY": REGISTRY,
            "OMNISIGHT_CANDIDATE_BUNDLE": str(tmp_path / "does-not-exist.json"),
        },
    )
    assert "REJECTED" in proc.stdout
    assert "StagingDigestBundleMissing" in proc.stdout


def test_incomplete_bundle_missing_frontend_digest_is_rejected(tmp_path: Path):
    bundle = _bundle(tmp_path / "bundle.json", frontend=None)
    docker = _fake_docker(tmp_path, backend=BACKEND_DIGEST, frontend=FRONTEND_DIGEST)
    proc = _run_deploy_func(
        tmp_path,
        'if verify_pulled_digests sha-cand; then echo MATCH; else echo "REJECTED rc=$?"; fi',
        {
            "DOCKER_BIN": str(docker),
            "OMNISIGHT_REGISTRY": REGISTRY,
            "OMNISIGHT_CANDIDATE_BUNDLE": str(bundle),
        },
    )
    assert "REJECTED" in proc.stdout
    assert "StagingDigestBundleIncomplete" in proc.stdout


# ── full deployer entrypoint: rejection happens BEFORE `compose up` ─────────

def test_deploy_entrypoint_rejects_mismatch_before_compose_up(tmp_path: Path):
    bundle = _bundle(tmp_path / "bundle.json")
    docker = _fake_docker(tmp_path, backend=WRONG_DIGEST, frontend=FRONTEND_DIGEST)
    log = tmp_path / "docker.log"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DOCKER_BIN": str(docker),
        "OMNISIGHT_REGISTRY": REGISTRY,
        "OMNISIGHT_STAGING_STATE_DIR": str(tmp_path / "state"),
        "FAKE_DOCKER_LOG": str(log),
    }
    proc = subprocess.run(
        ["bash", str(STAGING_DEPLOY),
         "--image-tag", "sha-cand", "--bundle", str(bundle)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 1, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "StagingDigestMismatch" in proc.stdout
    calls = log.read_text()
    assert "pull" in calls, "the candidate image must be pulled before verification"
    assert "image inspect" in calls, "the pulled digest must be inspected"
    # the rejection must happen before the standby stack is started.
    assert " up " not in calls and not calls.rstrip().endswith(" up"), (
        f"compose up must NOT run on digest mismatch; docker calls:\n{calls}"
    )


# ── sync orchestrator: bundle resolution + pass-through ─────────────────────

def _fake_staging_deploy(tmp_path: Path) -> Path:
    sh = tmp_path / "fake_staging_deploy.sh"
    sh.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> "${FAKE_DEPLOY_LOG:?}"
            exit 0
            """
        )
    )
    sh.chmod(0o755)
    return sh


def _fake_curl(tmp_path: Path) -> Path:
    curl = tmp_path / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 0\n")
    curl.chmod(0o755)
    return curl


def _sync_env(tmp_path: Path, deploy_log: Path, **extra: str) -> dict[str, str]:
    docker = _fake_docker(tmp_path, backend=BACKEND_DIGEST, frontend=FRONTEND_DIGEST)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SYNC_DEVELOP_TIP": TIP_SHA,
        "STAGING_DEPLOY_SH": str(_fake_staging_deploy(tmp_path)),
        "OMNISIGHT_STAGING_STATE_DIR": str(tmp_path / "state"),
        "SYNC_SKIP_MIGRATE": "1",
        "DOCKER_BIN": str(docker),
        "CURL_BIN": str(_fake_curl(tmp_path)),
        "FAKE_DEPLOY_LOG": str(deploy_log),
    }
    env.update(extra)
    return env


def _run_sync(tmp_path: Path, env: dict[str, str]):
    return subprocess.run(
        ["bash", str(SYNC_STAGING)],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_sync_passes_candidate_bundle_through_to_deployer(tmp_path: Path):
    bundle = _bundle(tmp_path / "bundle.json", git_sha=TIP_SHA)
    deploy_log = tmp_path / "deploy.log"
    env = _sync_env(tmp_path, deploy_log, SYNC_CANDIDATE_BUNDLE=str(bundle))
    proc = _run_sync(tmp_path, env)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    calls = deploy_log.read_text()
    assert f"--image-tag {TIP_SHA}" in calls
    assert f"--bundle {bundle}" in calls


def test_sync_refuses_bundle_for_a_different_git_sha(tmp_path: Path):
    bundle = _bundle(tmp_path / "bundle.json", git_sha="b" * 40)
    deploy_log = tmp_path / "deploy.log"
    deploy_log.write_text("")
    env = _sync_env(tmp_path, deploy_log, SYNC_CANDIDATE_BUNDLE=str(bundle))
    proc = _run_sync(tmp_path, env)
    assert proc.returncode == 1, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "refusing deploy" in proc.stderr
    assert deploy_log.read_text() == "", "deployer must not run on a bundle/tip mismatch"


def test_sync_without_bundle_deploys_by_tag_only(tmp_path: Path):
    deploy_log = tmp_path / "deploy.log"
    env = _sync_env(tmp_path, deploy_log)  # no SYNC_CANDIDATE_BUNDLE
    proc = _run_sync(tmp_path, env)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    calls = deploy_log.read_text()
    assert f"--image-tag {TIP_SHA}" in calls
    assert "--bundle" not in calls


def test_sync_missing_bundle_file_is_rejected(tmp_path: Path):
    deploy_log = tmp_path / "deploy.log"
    deploy_log.write_text("")
    env = _sync_env(tmp_path, deploy_log,
                    SYNC_CANDIDATE_BUNDLE=str(tmp_path / "missing.json"))
    proc = _run_sync(tmp_path, env)
    assert proc.returncode == 1
    assert "candidate bundle not found" in proc.stderr
    assert deploy_log.read_text() == ""


# ── static contract guards ──────────────────────────────────────────────────

def test_staging_deploy_verifies_before_up():
    text = STAGING_DEPLOY.read_text()
    verify_idx = text.index("verify_pulled_digests \"$image_tag\"")
    up_idx = text.index('eval "$compose up -d"')
    assert verify_idx < up_idx, "digest verification must gate `compose up`"


def test_sync_does_not_digest_check_rollback():
    """The previous-tag rollbacks must NOT pass a bundle (they re-deploy an
    already-good tag, not the candidate)."""
    text = SYNC_STAGING.read_text()
    assert 'deploy_tag "$prev"' in text
    assert 'deploy_tag "$tip" "$bundle"' in text
