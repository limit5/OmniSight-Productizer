#!/usr/bin/env python3
"""OP-1735 -- build a promote-ready bundle FROM image refs (no hand-reconstruction).

Promote (``scripts/promote_image_bundle.py``) needs a bundle manifest
naming the validated backend+frontend digests (the RT-21 pair). The two
"normal" sources of that manifest are both unavailable at promote time:

* the GitLab CI ``audit-emit`` seal artifact is unfetchable -- ``claude-bot``
  404s on the GitLab pipeline-read API and OP-1735 explicitly does NOT
  widen that permission (security tradeoff, out of scope); and
* the ``/app/bundle.json`` baked into the image is **zero-digest by
  design** (the digests are not known until after the image is sealed),
  so it cannot name the real content.

Before this helper the operator had to do it by hand during a cut:
``docker buildx imagetools inspect`` each image, copy the digests into a
JSON skeleton, delete the ``bridge`` entry, fix up ``bundle_id`` -- a
fragile, undocumented, fat-finger-prone dance (hit live during v0.6.2).

This helper does exactly that, deterministically and LOCALLY:

* resolve each image ref's real manifest digest with ``docker buildx
  imagetools inspect`` (a pure registry read -- never a build);
* read ``bundle_id`` / ``git_sha`` / ``git_ref`` from the image's OCI
  labels (``org.opencontainers.image.bundle.id`` etc.); and
* emit a promote-ready bundle that is **exactly the RT-21
  ``{backend, frontend}`` pair** -- ``bridge`` is a host control-plane
  daemon, not a prod container, so it is auto-omitted (a bundle that
  carries it is rejected by promote per RT-21).

The output is consumed unchanged by ``promote_image_bundle.py``; this
helper does NOT re-implement or change promote's validation contract.

MUST NOT (per OP-1735): this is **LOCAL inspect only**. It never fetches
a CI artifact and never touches ``claude-bot``'s GitLab permissions.

Usage (operator, during a cut):

    python3 scripts/build_promote_bundle.py \\
        --candidate-tag v0.6.2-rc1 \\
        --out artifacts/bundle-v0.6.2-rc1.json

    # or with explicit refs (e.g. pinning by digest):
    python3 scripts/build_promote_bundle.py \\
        --backend-ref  sora.services:49160/omnisight/omnisight-productizer/backend:v0.6.2-rc1 \\
        --frontend-ref sora.services:49160/omnisight/omnisight-productizer/frontend:v0.6.2-rc1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parent.parent

# Mirror promote_image_bundle.py's default so the repo we inspect is the
# one prod actually pulls (GitLab CR, ADR-0038/0040).
DEFAULT_REGISTRY = "sora.services:49160/omnisight/omnisight-productizer"

# RT-21: the release train tracks exactly this pair. bridge is never in a
# promote bundle, so it is never inspected or emitted.
REQUIRED_IMAGES: tuple[str, ...] = ("backend", "frontend")

# OCI labels the CI build stamps onto each runtime image (Dockerfile.*:
# org.opencontainers.image.bundle.id). git_sha/git_ref use the standard
# OCI revision/ref labels when present; they are informational in the
# bundle (promote does not validate them) so they are best-effort.
LABEL_BUNDLE_ID = "org.opencontainers.image.bundle.id"
LABEL_GIT_SHA = "org.opencontainers.image.revision"
LABEL_GIT_REF = "org.opencontainers.image.ref.name"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class BuildBundleError(RuntimeError):
    """An image ref could not be inspected or carried unusable identity."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repository_of(ref: str) -> str:
    """Return the repository part of an image ref (strip ``:tag`` / ``@digest``).

    Careful with the registry port: ``host:5000/path/backend:tag`` has a
    ``:`` for the port too, so a tag only exists if the text after the
    last ``:`` has no ``/`` in it.
    """

    if "@" in ref:
        return ref.split("@", 1)[0]
    head, sep, tail = ref.rpartition(":")
    if sep and "/" not in tail:
        return head
    return ref


def _imagetools_inspect(ref: str, fmt: str, *, runner: Runner) -> str:
    proc = runner(
        ["docker", "buildx", "imagetools", "inspect", ref, "--format", fmt],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise BuildBundleError(
            f"docker buildx imagetools inspect {ref!r} failed: {stderr or 'unknown error'}"
        )
    return (proc.stdout or "").strip()


def _extract_labels(image_json: str, *, ref: str) -> dict[str, str]:
    """Pull the OCI config labels out of ``imagetools inspect {{json .Image}}``.

    ``.Image`` is a single image config for a single-platform image, or a
    ``platform -> image config`` map for a multi-arch image. The bundle
    identity labels are stamped identically across platforms, so we merge
    every platform's labels.
    """

    try:
        parsed = json.loads(image_json) if image_json else {}
    except json.JSONDecodeError as exc:
        raise BuildBundleError(f"could not parse image config for {ref!r}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise BuildBundleError(f"unexpected image config shape for {ref!r}")

    def _labels_of(image_cfg: dict[str, Any]) -> dict[str, str]:
        config = image_cfg.get("config")
        if not isinstance(config, dict):
            return {}
        labels = config.get("Labels") or config.get("labels") or {}
        return {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {}

    if "config" in parsed:
        return _labels_of(parsed)
    merged: dict[str, str] = {}
    for value in parsed.values():
        if isinstance(value, dict):
            merged.update(_labels_of(value))
    return merged


def inspect_image(ref: str, *, runner: Runner) -> tuple[str, dict[str, str]]:
    """Return ``(manifest_digest, labels)`` for an image ref via local inspect."""

    digest = _imagetools_inspect(ref, "{{.Manifest.Digest}}", runner=runner)
    if not _DIGEST_RE.match(digest):
        raise BuildBundleError(
            f"inspect of {ref!r} returned {digest!r}, not a sha256:<64 hex> digest"
        )
    labels = _extract_labels(
        _imagetools_inspect(ref, "{{json .Image}}", runner=runner), ref=ref
    )
    return digest, labels


def _resolve_refs(
    *,
    candidate_tag: str | None,
    backend_ref: str | None,
    frontend_ref: str | None,
    registry: str,
) -> dict[str, str]:
    """Pick the backend/frontend refs from explicit flags or a candidate tag."""

    refs: dict[str, str] = {}
    explicit = {"backend": backend_ref, "frontend": frontend_ref}
    for name in REQUIRED_IMAGES:
        if explicit[name]:
            refs[name] = explicit[name]  # type: ignore[assignment]
        elif candidate_tag:
            refs[name] = f"{registry.rstrip('/')}/{name}:{candidate_tag}"
        else:
            raise BuildBundleError(
                f"no ref for {name!r}: pass --candidate-tag or --{name}-ref"
            )
    return refs


def build_promote_bundle(
    *,
    candidate_tag: str | None = None,
    backend_ref: str | None = None,
    frontend_ref: str | None = None,
    registry: str = DEFAULT_REGISTRY,
    runner: Runner | None = None,
    build_time: str | None = None,
) -> dict[str, Any]:
    """Inspect the backend+frontend refs and assemble a promote-ready bundle.

    The result is exactly the RT-21 pair (bridge auto-omitted), with the
    real resolved digests and ``bundle_id`` read from the image label, in
    the shape ``promote_image_bundle.py`` consumes.
    """

    runner = runner or subprocess.run
    refs = _resolve_refs(
        candidate_tag=candidate_tag,
        backend_ref=backend_ref,
        frontend_ref=frontend_ref,
        registry=registry,
    )

    images: dict[str, dict[str, str]] = {}
    bundle_ids: dict[str, str] = {}
    git_shas: dict[str, str] = {}
    git_refs: dict[str, str] = {}
    for name in REQUIRED_IMAGES:
        ref = refs[name]
        digest, labels = inspect_image(ref, runner=runner)
        repo = repository_of(ref)
        entry = {"repository": repo, "digest": digest}
        tag = ref[len(repo) + 1:] if ref.startswith(repo + ":") else None
        if tag:
            entry["tag"] = tag
        images[name] = entry
        bundle_id = (labels.get(LABEL_BUNDLE_ID) or "").strip()
        if bundle_id:
            bundle_ids[name] = bundle_id
        git_sha = (labels.get(LABEL_GIT_SHA) or "").strip()
        if git_sha:
            git_shas[name] = git_sha
        git_ref = (labels.get(LABEL_GIT_REF) or "").strip()
        if git_ref:
            git_refs[name] = git_ref

    # bundle_id is the identity promote audits against. Both images must
    # carry it and agree -- a mismatch means the two refs come from
    # different builds and must not be promoted as one bundle.
    if not bundle_ids:
        raise BuildBundleError(
            f"no {LABEL_BUNDLE_ID!r} label on the images; cannot derive bundle_id "
            f"(refs: {', '.join(refs.values())})"
        )
    distinct_ids = set(bundle_ids.values())
    if len(distinct_ids) > 1:
        raise BuildBundleError(
            f"backend/frontend carry different {LABEL_BUNDLE_ID} labels {sorted(distinct_ids)}; "
            f"refusing to build a single bundle from mismatched builds"
        )
    bundle_id = next(iter(distinct_ids))

    def _agree(values: dict[str, str], field_name: str) -> str | None:
        distinct = set(values.values())
        if len(distinct) > 1:
            raise BuildBundleError(
                f"backend/frontend carry different {field_name} labels {sorted(distinct)}"
            )
        return next(iter(distinct)) if distinct else None

    bundle: dict[str, Any] = {
        "bundle_id": bundle_id,
        "build_time": build_time or utc_now_iso(),
        "images": images,
        "signatures": [],
    }
    git_sha = _agree(git_shas, LABEL_GIT_SHA)
    if git_sha:
        bundle["git_sha"] = git_sha
    git_ref = _agree(git_refs, LABEL_GIT_REF)
    if git_ref:
        bundle["git_ref"] = git_ref
    return bundle


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_promote_bundle",
        description="Build a promote-ready bundle from image refs (LOCAL inspect only).",
    )
    parser.add_argument(
        "--candidate-tag",
        default=None,
        help="Candidate tag (e.g. v0.6.2-rc1); combined with --registry to derive refs.",
    )
    parser.add_argument("--backend-ref", default=None, help="Explicit backend image ref (overrides --candidate-tag).")
    parser.add_argument("--frontend-ref", default=None, help="Explicit frontend image ref (overrides --candidate-tag).")
    parser.add_argument(
        "--registry",
        default=os.environ.get(
            "OMNISIGHT_REGISTRY",
            os.environ.get("OMNISIGHT_IMAGE_REGISTRY_PREFIX", DEFAULT_REGISTRY),
        ),
        help="GitLab CR registry prefix used to derive refs from --candidate-tag.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Output path (default: stdout).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        bundle = build_promote_bundle(
            candidate_tag=args.candidate_tag,
            backend_ref=args.backend_ref,
            frontend_ref=args.frontend_ref,
            registry=args.registry,
        )
    except BuildBundleError as exc:
        print(f"build_promote_bundle: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(bundle, sort_keys=True, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
        print(args.out)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
