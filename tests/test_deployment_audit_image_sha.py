"""OP-1753 — deployment-audit image-SHA state machine fixtures.

Exercises the Family 5 T1/T2 image join added to
``scripts/deployment-audit.sh`` without touching live deployment state. Most
cases use the script's explicit test override environment; the final case uses
stub ``curl`` and ``docker buildx imagetools inspect`` binaries to cover the
real reader path against local fixtures.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SCRIPT = REPO_ROOT / "scripts" / "deployment-audit.sh"

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
NOW = "2026-05-26T12:00:00Z"


@pytest.fixture()
def audit_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(REAL_SCRIPT, repo / "scripts" / "deployment-audit.sh")
    os.chmod(repo / "scripts" / "deployment-audit.sh", 0o755)
    return repo


def _version_json(image_sha: str) -> str:
    return json.dumps(
        {
            "image_sha": image_sha,
            "build_time": "2026-05-26T00:00:00Z",
            "git_ref": "1" * 40,
            "alembic_head_in_image": "0204",
            "manifest_path": "/app/MANIFEST.json",
        }
    )


def _run_image_sha(repo: Path, env_extra: dict[str, str] | None = None):
    manifest = repo / "manifest.tsv"
    manifest.write_text("image-sha\tbackend\tyes\tOP-1753\tfixture\n", encoding="utf-8")
    env = {
        **os.environ,
        "USER": os.environ.get("USER", "tester"),
        "DEPLOYMENT_AUDIT_USER_SYSTEMD": "0",
        "OMNISIGHT_AUDIT_NOW": NOW,
        "OMNISIGHT_AUDIT_T1_VERSION_JSON": _version_json(SHA_A),
        "OMNISIGHT_AUDIT_T2_DIGEST": SHA_A,
        "OMNISIGHT_AUDIT_T2_PUSHED_AT": "2026-05-26T10:00:00Z",
        "OMNISIGHT_AUDIT_T4_IMAGE_SHA": SHA_A,
    }
    env.update(env_extra or {})
    return subprocess.run(
        [str(repo / "scripts" / "deployment-audit.sh"), str(manifest)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_image_sha_ok_when_t1_t2_t4_match(audit_repo: Path) -> None:
    res = _run_image_sha(audit_repo)
    assert res.returncode == 0, res.stdout
    assert "result_state=OK" in res.stdout
    assert "T1.image_sha == T2.digest == T4.manifest.image_sha" in res.stdout


@pytest.mark.parametrize(
    ("pushed_at", "state", "returncode"),
    [
        ("2026-05-26T11:00:00Z", "WARN_STALE_IMAGE", 0),
        ("2026-05-24T11:00:00Z", "PAGE_STALE_IMAGE", 1),
    ],
)
def test_image_sha_stale_state_uses_24h_threshold(
    audit_repo: Path,
    pushed_at: str,
    state: str,
    returncode: int,
) -> None:
    res = _run_image_sha(
        audit_repo,
        {
            "OMNISIGHT_AUDIT_T2_DIGEST": SHA_B,
            "OMNISIGHT_AUDIT_T2_PUSHED_AT": pushed_at,
        },
    )
    assert res.returncode == returncode, res.stdout
    assert f"result_state={state}" in res.stdout
    assert "alert=OmniSightStaleImage" in res.stdout


def test_image_sha_integrity_finding_is_independent_from_stale(
    audit_repo: Path,
) -> None:
    res = _run_image_sha(
        audit_repo,
        {
            "OMNISIGHT_AUDIT_T2_DIGEST": SHA_B,
            "OMNISIGHT_AUDIT_T2_PUSHED_AT": "2026-05-24T11:00:00Z",
            "OMNISIGHT_AUDIT_T4_IMAGE_SHA": SHA_C,
        },
    )
    assert res.returncode == 1, res.stdout
    assert "result_state=PAGE_INTEGRITY" in res.stdout
    assert "alert=OmniSightImageIntegrity" in res.stdout
    assert "result_state=PAGE_STALE_IMAGE" in res.stdout
    assert res.stdout.count("✗ RED") == 2


def test_image_sha_incomplete_emits_incomplete_metric_without_stale_alert(
    audit_repo: Path,
) -> None:
    res = _run_image_sha(
        audit_repo,
        {
            "OMNISIGHT_AUDIT_T1_VERSION_JSON": "{}",
            "OMNISIGHT_AUDIT_T2_DIGEST": SHA_B,
        },
    )
    assert res.returncode == 0, res.stdout
    assert "result_state=INCOMPLETE" in res.stdout
    assert "metric=omnisight_deployment_audit_incomplete value=1" in res.stdout
    assert "OmniSightStaleImage" not in res.stdout


def test_image_sha_integration_uses_stub_version_and_registry(
    audit_repo: Path,
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' '"
        + _version_json(SHA_A).replace("'", "'\\''")
        + "'\n",
        encoding="utf-8",
    )
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1 $2 $3\" = \"buildx imagetools inspect\" ]; then\n"
        f"  printf '%s\\n' 'Name: example/backend:latest' 'Digest: {SHA_B}' 'Created: 2026-05-26T11:00:00Z'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" = \"exec\" ]; then\n"
        f"  printf '%s\\n' '{{\"image_sha\":\"{SHA_A}\"}}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    os.chmod(curl, 0o755)
    os.chmod(docker, 0o755)

    res = _run_image_sha(
        audit_repo,
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "OMNISIGHT_AUDIT_VERSION_URL": "http://stub/version",
            "OMNISIGHT_AUDIT_IMAGE_REF": "example/backend:latest",
            "OMNISIGHT_AUDIT_T1_VERSION_JSON": "",
            "OMNISIGHT_AUDIT_T2_DIGEST": "",
            "OMNISIGHT_AUDIT_T2_PUSHED_AT": "",
            "OMNISIGHT_AUDIT_T4_IMAGE_SHA": "",
        },
    )
    assert res.returncode == 0, res.stdout
    assert "result_state=WARN_STALE_IMAGE" in res.stdout
    assert "ref=example/backend:latest" in res.stdout
