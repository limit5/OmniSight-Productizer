"""OP-1584 / RT-09 - candidate SHA image pipeline contract."""
from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
GITLAB_CI = REPO_ROOT / ".gitlab-ci.yml"


def _load() -> dict:
    return yaml.safe_load(GITLAB_CI.read_text())


def _flatten_script(job: dict) -> str:
    parts: list[str] = []
    for key in ("before_script", "script"):
        block = job.get(key) or []
        if isinstance(block, str):
            parts.append(block)
        else:
            parts.extend(block)
    return "\n".join(parts)


def _needed_jobs(job: dict) -> set[str]:
    needs = job.get("needs") or []
    return {n if isinstance(n, str) else n["job"] for n in needs}


def test_candidate_workflow_is_api_or_manual_with_full_sha() -> None:
    ci = _load()
    candidate_rule = {
        "if": (
            "$CANDIDATE_SHA =~ /^[0-9a-f]{40}$/ && "
            '($CI_PIPELINE_SOURCE == "api" || $CI_PIPELINE_SOURCE == "web")'
        )
    }

    assert candidate_rule in ci["workflow"]["rules"]
    assert ci[".candidate_rules"]["rules"] == [candidate_rule]


def test_candidate_matrix_builds_backend_and_frontend_only() -> None:
    ci = _load()
    matrix = ci[".candidate_image_matrix"]["parallel"]["matrix"]

    assert matrix == [
        {"IMAGE_NAME": "backend", "DOCKERFILE": "Dockerfile.backend"},
        {"IMAGE_NAME": "frontend", "DOCKERFILE": "Dockerfile.frontend"},
    ]


def test_candidate_prepare_fetches_develop_and_checks_out_detached_sha() -> None:
    ci = _load()
    job = ci["candidate-prepare-bundle"]
    flat = _flatten_script(job)

    assert job["stage"] == "build"
    assert job["resource_group"] == "candidate-image-pipeline"
    assert 'test "${#CANDIDATE_SHA}" = "40"' in flat
    assert "git fetch --no-tags --unshallow origin develop" in flat
    assert 'git merge-base --is-ancestor "$CANDIDATE_SHA" origin/develop' in flat
    assert 'git checkout --detach "$CANDIDATE_SHA"' in flat
    assert 'export GIT_SHA="${CANDIDATE_SHA}"' in flat
    assert 'export GIT_REF="refs/heads/develop"' in flat
    assert "CANDIDATE_IMAGE_TAG=sha-%s" in flat
    assert job["artifacts"]["reports"]["dotenv"] == "candidate-bundle.env"


def test_candidate_build_pushes_only_full_sha_tag_without_latest_or_release_tag() -> None:
    ci = _load()
    job = ci["candidate-build-image"]
    flat = _flatten_script(job)

    assert job["stage"] == "build"
    assert job["resource_group"] == "candidate-image-pipeline"
    assert "candidate-prepare-bundle" in _needed_jobs(job)
    assert 'test "${CANDIDATE_IMAGE_TAG}" = "sha-${CANDIDATE_SHA}"' in flat
    assert "cp bundle.json /tmp/candidate-bundle.json" in flat
    assert 'git checkout --detach "$CANDIDATE_SHA"' in flat
    assert "cp /tmp/candidate-bundle.json bundle.json" in flat
    assert '--label "org.opencontainers.image.revision=${CANDIDATE_SHA}"' in flat
    assert '--tag "${IMAGE_BASE}:${CANDIDATE_IMAGE_TAG}"' in flat
    assert "sha-${CI_COMMIT_SHORT_SHA}" not in flat
    assert "${CI_COMMIT_TAG}" not in flat
    assert ":latest" not in flat
    assert "git tag" not in flat


def test_candidate_seal_bundle_rewrites_artifact_with_resolved_pair_digests() -> None:
    ci = _load()
    job = ci["candidate-seal-bundle"]
    flat = _flatten_script(job)

    assert job["stage"] == "sign"
    assert job["resource_group"] == "candidate-image-pipeline"
    assert "candidate-prepare-bundle" in _needed_jobs(job)
    assert "candidate-build-image" in _needed_jobs(job)
    assert job["artifacts"]["reports"]["dotenv"] == "candidate-bundle.env"
    assert "bundle.json" in job["artifacts"]["paths"]

    assert "${CI_REGISTRY_IMAGE}/backend:${CANDIDATE_IMAGE_TAG}" in flat
    assert "${CI_REGISTRY_IMAGE}/frontend:${CANDIDATE_IMAGE_TAG}" in flat
    assert "IMAGE_DIGEST_BACKEND=" in flat
    assert "IMAGE_DIGEST_FRONTEND=" in flat
    assert 'test "$IMAGE_DIGEST_BACKEND" != "$placeholder_digest"' in flat
    assert 'test "$IMAGE_DIGEST_FRONTEND" != "$placeholder_digest"' in flat
    assert "scripts/emit_bundle_json.sh > bundle.json.sealed" in flat
    assert "jq 'del(.images.bridge)' bundle.json.sealed > bundle.json" in flat
    assert "BUNDLE_SHA=" in flat


def test_candidate_sign_sbom_and_attest_resolve_digest_by_full_sha_tag() -> None:
    ci = _load()

    sign_flat = _flatten_script(ci["candidate-sign-image"])
    sbom_flat = _flatten_script(ci["candidate-sbom-image"])
    attest_flat = _flatten_script(ci["candidate-attest-image"])

    for job_name in (
        "candidate-sign-image",
        "candidate-sbom-image",
        "candidate-attest-image",
    ):
        assert ci[job_name]["resource_group"] == "candidate-image-pipeline"

    assert "candidate-build-image" in _needed_jobs(ci["candidate-sign-image"])
    assert "candidate-sign-image" in _needed_jobs(ci["candidate-sbom-image"])
    assert "candidate-sbom-image" in _needed_jobs(ci["candidate-attest-image"])

    for flat in (sign_flat, sbom_flat, attest_flat):
        assert 'CANDIDATE_IMAGE_TAG="sha-${CANDIDATE_SHA}"' in flat
        assert '"${IMAGE_BASE}:${CANDIDATE_IMAGE_TAG}"' in flat
        assert 'IMAGE_REF="${IMAGE_BASE}@${DIGEST}"' in flat or "syft \"${IMAGE_BASE}@${DIGEST}\"" in flat
        assert ":latest" not in flat
        assert "${CI_COMMIT_TAG}" not in flat
        assert "git tag" not in flat

    assert 'cosign sign --yes --key "$COSIGN_KEY" "$IMAGE_REF"' in sign_flat
    assert 'cosign attest --yes --key "$COSIGN_KEY"' in attest_flat
    assert '--arg git_sha "$CANDIDATE_SHA"' in attest_flat
    assert '--arg git_ref "refs/heads/develop"' in attest_flat
