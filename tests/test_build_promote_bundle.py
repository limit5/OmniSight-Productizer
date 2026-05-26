"""OP-1735 -- build_promote_bundle.py contract.

Covers the OP-1735 acceptance criteria:

* Code: the helper emits the RT-21 backend+frontend pair from image
  refs/labels (resolved digests + bundle_id/git_sha/git_ref from the OCI
  labels), auto-omitting bridge.
* Integration: the emitted bundle is accepted unchanged by
  ``promote_image_bundle.py`` -- digests match, no bridge, bundle_id from
  the image label.
* Exercised: a unit test over a fixture ``imagetools inspect`` output
  produces the correct bundle JSON.

The ``docker buildx imagetools inspect`` calls are stubbed by a fake
runner driven from committed fixtures, so the test never touches a real
registry (the helper is LOCAL-inspect-only per OP-1735).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_promote_bundle.py"
PROMOTE_SCRIPT = REPO_ROOT / "scripts" / "promote_image_bundle.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "build_promote_bundle"

REGISTRY = "sora.services:49160/omnisight/omnisight-productizer"
CANDIDATE_TAG = "v0.6.2-rc1"
BACKEND_REF = f"{REGISTRY}/backend:{CANDIDATE_TAG}"
FRONTEND_REF = f"{REGISTRY}/frontend:{CANDIDATE_TAG}"

DIGEST_BE = "sha256:" + "a" * 64
DIGEST_FE = "sha256:" + "b" * 64

EXPECTED_BUNDLE_ID = "v0.6.2-rc1+20260520.sha7a3b4c5"
EXPECTED_GIT_SHA = "7a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b"
EXPECTED_GIT_REF = "refs/tags/v0.6.2-rc1"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bpb():
    return _load_module("build_promote_bundle", BUILD_SCRIPT)


@pytest.fixture(scope="module")
def promote():
    return _load_module("promote_image_bundle", PROMOTE_SCRIPT)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_runner(
    *,
    digests: dict[str, str] | None = None,
    images: dict[str, str] | None = None,
    fail: set[str] | None = None,
):
    """Build a fake ``subprocess.run`` driven by the ref being inspected.

    ``digests`` / ``images`` map an image ref to the stdout the real
    ``docker buildx imagetools inspect`` would print for the
    ``{{.Manifest.Digest}}`` and ``{{json .Image}}`` formats.
    """

    digests = digests or {
        BACKEND_REF: DIGEST_BE,
        FRONTEND_REF: DIGEST_FE,
    }
    images = images or {
        BACKEND_REF: _fixture("backend_image_multiarch.json"),
        FRONTEND_REF: _fixture("frontend_image_single.json"),
    }
    fail = fail or set()
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        ref = cmd[4]
        fmt = cmd[6]
        if ref in fail:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"manifest unknown: {ref}")
        if fmt == "{{.Manifest.Digest}}":
            return SimpleNamespace(returncode=0, stdout=digests[ref] + "\n", stderr="")
        if fmt == "{{json .Image}}":
            return SimpleNamespace(returncode=0, stdout=images[ref], stderr="")
        raise AssertionError(f"unexpected format {fmt!r}")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# ───────────────────────── Code AC: emit the RT-21 pair ───────────────────


def test_emits_rt21_pair_with_resolved_digests_and_label_identity(bpb):
    bundle = bpb.build_promote_bundle(
        candidate_tag=CANDIDATE_TAG,
        registry=REGISTRY,
        runner=make_runner(),
        build_time="2026-05-26T00:00:00Z",
    )

    # Exactly the RT-21 pair, bridge auto-omitted.
    assert set(bundle["images"]) == {"backend", "frontend"}
    assert "bridge" not in bundle["images"]

    # Digests are the resolved manifest digests, not placeholders.
    assert bundle["images"]["backend"]["digest"] == DIGEST_BE
    assert bundle["images"]["frontend"]["digest"] == DIGEST_FE

    # Repository + tag are carried so promote retags the exact repo.
    assert bundle["images"]["backend"]["repository"] == f"{REGISTRY}/backend"
    assert bundle["images"]["backend"]["tag"] == CANDIDATE_TAG

    # Identity is read from the OCI labels (not reconstructed by hand).
    assert bundle["bundle_id"] == EXPECTED_BUNDLE_ID
    assert bundle["git_sha"] == EXPECTED_GIT_SHA
    assert bundle["git_ref"] == EXPECTED_GIT_REF


def test_inspect_reads_labels_from_single_and_multiarch_images(bpb):
    runner = make_runner()
    be_digest, be_labels = bpb.inspect_image(BACKEND_REF, runner=runner)
    fe_digest, fe_labels = bpb.inspect_image(FRONTEND_REF, runner=runner)

    assert be_digest == DIGEST_BE
    assert fe_digest == DIGEST_FE
    # Multi-arch (.Image is a platform map) and single-arch both yield labels.
    assert be_labels[bpb.LABEL_BUNDLE_ID] == EXPECTED_BUNDLE_ID
    assert fe_labels[bpb.LABEL_BUNDLE_ID] == EXPECTED_BUNDLE_ID


def test_explicit_refs_override_candidate_tag(bpb):
    runner = make_runner()
    bundle = bpb.build_promote_bundle(
        backend_ref=BACKEND_REF,
        frontend_ref=FRONTEND_REF,
        runner=runner,
    )
    assert set(bundle["images"]) == {"backend", "frontend"}
    # Only the two pair refs were inspected -- bridge is never touched.
    inspected = {c[4] for c in runner.calls}
    assert inspected == {BACKEND_REF, FRONTEND_REF}


def test_digest_by_ref_is_carried_as_repository(bpb):
    """A ref pinned by @digest emits the repository (no tag) and digest."""
    pinned = f"{REGISTRY}/backend@{DIGEST_BE}"
    runner = make_runner(
        digests={pinned: DIGEST_BE, FRONTEND_REF: DIGEST_FE},
        images={
            pinned: _fixture("backend_image_multiarch.json"),
            FRONTEND_REF: _fixture("frontend_image_single.json"),
        },
    )
    bundle = bpb.build_promote_bundle(
        backend_ref=pinned, frontend_ref=FRONTEND_REF, runner=runner,
    )
    assert bundle["images"]["backend"]["repository"] == f"{REGISTRY}/backend"
    assert "tag" not in bundle["images"]["backend"]


def test_repository_of_handles_port_tag_and_digest(bpb):
    assert bpb.repository_of(f"{REGISTRY}/backend:v1.2.3") == f"{REGISTRY}/backend"
    assert bpb.repository_of(f"{REGISTRY}/backend@{DIGEST_BE}") == f"{REGISTRY}/backend"
    # No tag, registry port preserved.
    assert bpb.repository_of(f"{REGISTRY}/backend") == f"{REGISTRY}/backend"


# ───────────────────────── error paths ────────────────────────────────────


def test_missing_ref_without_candidate_tag_errors(bpb):
    with pytest.raises(bpb.BuildBundleError, match="--candidate-tag or --backend-ref"):
        bpb.build_promote_bundle(runner=make_runner())


def test_inspect_failure_is_reported(bpb):
    runner = make_runner(fail={BACKEND_REF})
    with pytest.raises(bpb.BuildBundleError, match="imagetools inspect"):
        bpb.build_promote_bundle(candidate_tag=CANDIDATE_TAG, registry=REGISTRY, runner=runner)


def test_missing_bundle_id_label_errors(bpb):
    no_label = json.dumps({"config": {"Labels": {}}})
    runner = make_runner(
        images={BACKEND_REF: no_label, FRONTEND_REF: no_label},
    )
    with pytest.raises(bpb.BuildBundleError, match="bundle.id"):
        bpb.build_promote_bundle(candidate_tag=CANDIDATE_TAG, registry=REGISTRY, runner=runner)


def test_mismatched_bundle_ids_rejected(bpb):
    other = json.dumps(
        {"config": {"Labels": {bpb.LABEL_BUNDLE_ID: "v9.9.9+20260101.shadeadbee"}}}
    )
    runner = make_runner(
        images={
            BACKEND_REF: _fixture("backend_image_multiarch.json"),
            FRONTEND_REF: other,
        },
    )
    with pytest.raises(bpb.BuildBundleError, match="different .* labels"):
        bpb.build_promote_bundle(candidate_tag=CANDIDATE_TAG, registry=REGISTRY, runner=runner)


def test_bad_digest_from_inspect_errors(bpb):
    runner = make_runner(
        digests={BACKEND_REF: "not-a-digest", FRONTEND_REF: DIGEST_FE},
    )
    with pytest.raises(bpb.BuildBundleError, match="not a sha256"):
        bpb.build_promote_bundle(candidate_tag=CANDIDATE_TAG, registry=REGISTRY, runner=runner)


# ───────────────────────── Integration AC: promote accepts it ─────────────


def test_emitted_bundle_is_accepted_by_promote(bpb, promote):
    bundle = bpb.build_promote_bundle(
        candidate_tag=CANDIDATE_TAG, registry=REGISTRY, runner=make_runner(),
    )

    # promote's own planning path must accept the bundle unchanged: it
    # plans exactly the backend+frontend retags, with matching digests and
    # the repository from the bundle.
    promotions = promote.planned_promotions(bundle, version="v0.6.2", registry=REGISTRY)
    by_name = {p.name: p for p in promotions}
    assert set(by_name) == {"backend", "frontend"}
    assert by_name["backend"].digest == DIGEST_BE
    assert by_name["frontend"].digest == DIGEST_FE
    assert by_name["backend"].source_ref == f"{REGISTRY}/backend@{DIGEST_BE}"
    assert by_name["backend"].target_ref == f"{REGISTRY}/backend:v0.6.2"
    # bundle_id from the image label flows through to the audit identity.
    assert str(bundle.get("bundle_id")) == EXPECTED_BUNDLE_ID


def test_promote_rejects_bridge_but_helper_never_emits_it(bpb, promote):
    """Defensive: promote rejects a bridge entry (RT-21), and the helper's
    output never contains one, so the two contracts agree."""
    bundle = bpb.build_promote_bundle(
        candidate_tag=CANDIDATE_TAG, registry=REGISTRY, runner=make_runner(),
    )
    assert "bridge" not in bundle["images"]

    # If a bridge entry were present, promote must refuse it.
    poisoned = json.loads(json.dumps(bundle))
    poisoned["images"]["bridge"] = {"repository": f"{REGISTRY}/bridge", "digest": "sha256:" + "c" * 64}
    with pytest.raises(promote.PromoteError, match="(?i)bridge|extra image"):
        promote.planned_promotions(poisoned, version="v0.6.2", registry=REGISTRY)


# ───────────────────────── CLI smoke ──────────────────────────────────────


def test_cli_writes_bundle_to_out(bpb, tmp_path, monkeypatch):
    monkeypatch.setattr(bpb.subprocess, "run", make_runner())
    out = tmp_path / "bundle.json"
    rc = bpb.main(
        ["--candidate-tag", CANDIDATE_TAG, "--registry", REGISTRY, "--out", str(out)]
    )
    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["bundle_id"] == EXPECTED_BUNDLE_ID
    assert set(written["images"]) == {"backend", "frontend"}
