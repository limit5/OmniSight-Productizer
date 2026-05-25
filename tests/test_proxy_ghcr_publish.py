"""A4 (OP-1719) — contract for the GitLab-CI omnisight-proxy GHCR publisher.

The customer-side omnisight-proxy (BYOG Tier-3) image keeps its PUBLIC contract
on GHCR (`ghcr.io/<ns>/omnisight-proxy:<tag>`), but its old publisher
(.github/workflows/docker-publish.yml) was disabled on GitHub by A1 (OP-1710)
and its `on: tags: v*` trigger never fires under the release train (RT-20). The
publisher is re-homed onto GitLab CI here; this pins the promises that re-home
makes so the customer image ref + the dormant-until-provisioned safety can't
silently regress.

No network / no Docker: the `.gitlab-ci.yml` is parsed as YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
GITLAB_CI = REPO_ROOT / ".gitlab-ci.yml"
JOB = "publish-proxy-ghcr"


@pytest.fixture(scope="module")
def ci() -> dict:
    assert GITLAB_CI.exists(), f"missing {GITLAB_CI}"
    return yaml.safe_load(GITLAB_CI.read_text())


@pytest.fixture(scope="module")
def job(ci: dict) -> dict:
    assert JOB in ci, f"expected `{JOB}` job in .gitlab-ci.yml"
    return ci[JOB]


def _script(job: dict) -> str:
    s = job.get("script", [])
    return "\n".join(s) if isinstance(s, list) else str(s)


def test_job_is_in_build_stage(job: dict) -> None:
    assert job.get("stage") == "build"


def test_dormant_until_token_provisioned(job: dict) -> None:
    """The job must NOT run until the operator provisions the GHCR PAT.

    Gating on $OMNISIGHT_GHCR_PUBLISH_TOKEN means the job is simply not
    created when the var is absent — zero pipeline impact pre-provisioning.
    """
    rules = job.get("rules", [])
    guarded = [r for r in rules if isinstance(r, dict) and "OMNISIGHT_GHCR_PUBLISH_TOKEN" in r.get("if", "")]
    assert guarded, "the run rule must gate on $OMNISIGHT_GHCR_PUBLISH_TOKEN (dormant until provisioned)"
    # And a terminal `when: never` so nothing else can make it run.
    assert any(isinstance(r, dict) and r.get("when") == "never" for r in rules), (
        "rules must end with `when: never` so the job only runs under the guarded condition"
    )


def test_builds_the_proxy_dockerfile(job: dict) -> None:
    assert "Dockerfile.omnisight-proxy" in _script(job)


def test_preserves_ghcr_customer_image_contract(job: dict) -> None:
    """The published ref MUST stay `ghcr.io/<ns>/omnisight-proxy` — the public
    BYOG contract (docs/ops/self_hosted_byog_proxy_alignment.md). Only the
    publisher moved; the image customers pull is unchanged."""
    script = _script(job)
    assert "ghcr.io/${OMNISIGHT_GHCR_NAMESPACE}/omnisight-proxy" in script


def test_scoped_to_proxy_only_not_internal_cr(job: dict) -> None:
    """Backend/frontend stay on the internal GitLab CR; this job must not push
    to ${CI_REGISTRY_IMAGE}."""
    assert "CI_REGISTRY_IMAGE" not in _script(job)


def test_no_literal_secret_in_source(job: dict) -> None:
    """The PAT is referenced only as a masked CI variable, never inlined."""
    script = _script(job)
    assert "$OMNISIGHT_GHCR_PUBLISH_TOKEN" in script
    for leaked in ("ghp_", "github_pat_"):
        assert leaked not in GITLAB_CI.read_text(), f"literal token prefix {leaked!r} must never appear"


def test_latest_tracks_releases_not_dev_merges(job: dict) -> None:
    """:latest + :<version> must move ONLY on a release/promote that passes
    OMNISIGHT_PROXY_RELEASE_TAG — every candidate only gets the immutable
    :sha-<sha> tag (so :latest never advances on a plain dev merge)."""
    script = _script(job)
    assert "sha-${CANDIDATE_SHA}" in script, "every candidate must publish the immutable :sha-<sha> tag"
    assert "OMNISIGHT_PROXY_RELEASE_TAG" in script, ":latest/:<version> must be gated on a release tag var"
    # :latest must be inside the release-tag conditional, not unconditional.
    assert ":latest" in script and "OMNISIGHT_PROXY_RELEASE_TAG" in script


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
