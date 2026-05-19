"""OP-1513 / OP-1478-act-T1.3 — .gitlab-ci.yml wiring contract.

Locks the four CI-side guarantees that AC #1, #3, #4 depend on:
  * ``prepare-bundle`` runs in stage ``build`` and invokes
    ``scripts/emit_bundle_json.sh``.
  * Its dotenv artifact exports ``BUNDLE_ID`` / ``BUNDLE_SHA`` and the
    plain artifact carries ``bundle.json``.
  * ``build-image`` ``needs:`` ``prepare-bundle`` (so build receives both
    the file and the dotenv vars) and passes them via ``--build-arg``.
  * ``bundle-assert`` runs in stage ``audit-emit`` and rejects the three
    stub shapes the ticket calls out (``local-dev``, 40-zero SHA, epoch
    build_time).
"""
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


def test_audit_emit_stage_exists_before_other_stages_assumed() -> None:
    ci = _load()
    assert "audit-emit" in ci["stages"]
    assert ci["stages"][0] == "build", "prepare-bundle must live in the build stage"


def test_prepare_bundle_runs_emit_script_in_build_stage() -> None:
    ci = _load()
    job = ci["prepare-bundle"]
    assert job["stage"] == "build"
    flat = _flatten_script(job)
    assert "scripts/emit_bundle_json.sh" in flat
    # Identity inputs that AC #1 makes mandatory.
    for required in (
        "GIT_SHA=",
        "GIT_REF=",
        "OPENAPI_HASH=",
        "IMAGE_DIGEST_BACKEND=",
        "IMAGE_DIGEST_FRONTEND=",
        "IMAGE_DIGEST_BRIDGE=",
    ):
        assert required in flat, f"prepare-bundle must export {required}"


def test_prepare_bundle_exports_dotenv_with_bundle_id_and_sha() -> None:
    ci = _load()
    job = ci["prepare-bundle"]
    artifacts = job["artifacts"]
    assert "bundle.json" in artifacts["paths"]
    assert artifacts["reports"]["dotenv"] == "bundle.env"
    flat = _flatten_script(job)
    assert "BUNDLE_ID=" in flat and "BUNDLE_SHA=" in flat
    assert "bundle.env" in flat


def test_build_image_needs_prepare_bundle_and_uses_build_args() -> None:
    ci = _load()
    job = ci["build-image"]
    # `needs:` may be list of strings or list of {job: ...} dicts.
    needs = job.get("needs") or []
    needed_jobs = {
        n if isinstance(n, str) else n["job"] for n in needs
    }
    assert "prepare-bundle" in needed_jobs

    flat = _flatten_script(job)
    assert "--build-arg" in flat and "BUNDLE_ID=${BUNDLE_ID}" in flat
    assert "--build-arg" in flat and "BUNDLE_SHA=${BUNDLE_SHA}" in flat


def test_bundle_assert_runs_in_audit_emit_and_rejects_stub_shapes() -> None:
    ci = _load()
    job = ci["bundle-assert"]
    assert job["stage"] == "audit-emit"
    needs = job.get("needs") or []
    needed_jobs = {n if isinstance(n, str) else n["job"] for n in needs}
    assert "prepare-bundle" in needed_jobs

    flat = _flatten_script(job)
    # Each stub shape from the ticket description.
    assert "local-dev" in flat
    assert "0000000000000000000000000000000000000000" in flat  # 40-zero SHA
    assert "1970-01-01T00:00:00Z" in flat                       # epoch build_time
