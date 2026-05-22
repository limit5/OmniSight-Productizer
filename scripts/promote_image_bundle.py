#!/usr/bin/env python3
"""OP-1590 -- RT-12 promote: retag validated GitLab CR digests to vX.Y.Z.

Rewritten for the single-trunk release train (ADR-0040). A *promote*
takes the backend+frontend image digests that were validated on staging
(RT-05e) and migration-checked (RT-11) and retags them -- in GitLab CR,
by digest, **never rebuilding** -- to the reserved release version
``vX.Y.Z``. It then verifies each final tag resolves to the exact
validated digest, writes a per-image promotion attestation, and appends
exactly one bundle audit row (a hard gate: a failed audit write aborts
the promote).

Consumes the locked decisions (this ticket does NOT re-decide them):

* **RT-20 -- final release identity is image-tag-only.** The release is
  the GitLab CR image tag ``vX.Y.Z`` -> validated digest plus the
  ``release_train`` / ``release_audit`` row. **No ``v*`` git tag is ever
  created** -- a ``v*`` git tag would trip the existing ``^v`` CI build
  rule and rebuild a *different* digest, breaking "validated digest ==
  shipped digest". This script therefore only ever touches the registry
  (``docker buildx imagetools``) and never invokes ``git`` or any image
  build.
* **RT-21 -- the train tracks the backend+frontend PAIR only.** ``bridge``
  is a decoupled host control-plane daemon, not a prod container, so a
  bundle that carries it (or omits either of the pair) is rejected.

The retag is idempotent: a target tag that already resolves to the
validated digest is left untouched, so re-running after a partial or
failed promote is safe.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIT_LOG = REPO_ROOT / "audit" / "image_promotion_audit.jsonl"

# RT-12: promotion targets GitLab CR (ADR-0038/0040), not GHCR. Mirrors
# the ``OMNISIGHT_REGISTRY`` default the deploy compose files use so the
# repository the promote retags is the one prod actually pulls.
DEFAULT_REGISTRY = "sora.services:49154/omnisight/OmniSight-Productizer"

SIGN_SCRIPT = REPO_ROOT / "scripts" / "sign_promotion_attestation.py"
VERIFY_SIGNATURE_SH = REPO_ROOT / "scripts" / "verify_image_signature.sh"

# RT-21: the release train is exactly this pair. Order is the per-image
# attestation / audit order.
REQUIRED_IMAGES: tuple[str, ...] = ("backend", "frontend")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# RT-20: the final tag is a release version, never an env alias.
_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# Staging-gate (RT-05c/RT-05e) green signal as written by
# ``backend.agents.staging_gate``.
_STAGING_GREEN = "green"


class PromoteError(RuntimeError):
    """A promote precondition failed or a retag could not be verified."""


class StagingGateNotPassed(PromoteError):
    """No green staging-gate evidence matching the validated digests."""


@dataclass(frozen=True)
class ImagePromotion:
    name: str
    repository: str
    digest: str
    source_ref: str
    target_ref: str


@dataclass(frozen=True)
class PromoteOutcome:
    """Result of a promote: the version, the per-image plan, and whether
    every final tag resolved to its exact validated digest."""

    version: str
    promotions: list[ImagePromotion]
    final_digest_equality: bool
    per_image_equality: dict[str, bool] = field(default_factory=dict)
    audit_row: dict[str, Any] | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_version(value: str) -> str:
    clean = str(value or "").strip()
    if not _VERSION_RE.fullmatch(clean):
        raise PromoteError(f"--to must be a release version vX.Y.Z, got {value!r}")
    return clean


def _clean_digest(field_name: str, value: Any) -> str:
    clean = str(value or "").strip()
    if not _DIGEST_RE.match(clean):
        raise PromoteError(f"{field_name} must look like sha256:<64 hex chars>, got {value!r}")
    return clean


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PromoteError(f"bundle not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PromoteError(f"bundle is not valid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PromoteError(f"bundle root must be an object: {path}")
    return value


def resolve_bundle_path(bundle: str, *, repo_root: Path = REPO_ROOT) -> Path:
    candidate = Path(bundle)
    if candidate.exists():
        return candidate
    search = [
        repo_root / "artifacts" / f"bundle-{bundle}.json",
        repo_root / "bundles" / bundle / "bundle.json",
        repo_root / "bundle.json",
    ]
    for path in search:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if data.get("bundle_id") == bundle:
            return path
    raise PromoteError(
        f"could not resolve bundle {bundle!r}; pass a bundle JSON path or create "
        f"artifacts/bundle-{bundle}.json",
    )


def _repository_for_image(name: str, entry: dict[str, Any], registry: str) -> str:
    repo = str(entry.get("repository") or "").strip()
    if repo:
        return repo
    # GitLab CR layout: <registry>/<image> (e.g. .../OmniSight-Productizer/backend).
    return f"{registry.rstrip('/')}/{name}"


def build_promotions(
    *,
    version: str,
    digests: dict[str, str],
    registry: str = DEFAULT_REGISTRY,
    repositories: dict[str, str] | None = None,
) -> list[ImagePromotion]:
    """Plan the backend+frontend retags to ``version`` from validated digests.

    ``digests`` must contain exactly the RT-21 pair; ``repositories`` may
    override the default GitLab CR repository per image.
    """

    version = _clean_version(version)
    repositories = repositories or {}
    extras = sorted(set(digests) - set(REQUIRED_IMAGES))
    if extras:
        raise PromoteError(
            f"release train tracks the backend+frontend pair only (RT-21); "
            f"refusing extra image(s): {extras}"
        )
    planned: list[ImagePromotion] = []
    for name in REQUIRED_IMAGES:
        if name not in digests:
            raise PromoteError(f"bundle is missing the required {name!r} image (RT-21 pair)")
        digest = _clean_digest(f"{name} digest", digests[name])
        repo = repositories.get(name) or f"{registry.rstrip('/')}/{name}"
        planned.append(
            ImagePromotion(
                name=name,
                repository=repo,
                digest=digest,
                source_ref=f"{repo}@{digest}",
                target_ref=f"{repo}:{version}",
            )
        )
    return planned


def planned_promotions(
    bundle: dict[str, Any],
    *,
    version: str,
    registry: str = DEFAULT_REGISTRY,
) -> list[ImagePromotion]:
    """Build the retag plan from a bundle manifest (CLI path)."""

    images = bundle.get("images")
    if not isinstance(images, dict) or not images:
        raise PromoteError("bundle images must be a non-empty object")

    digests: dict[str, str] = {}
    repositories: dict[str, str] = {}
    for name, entry in images.items():
        if not isinstance(entry, dict):
            raise PromoteError(f"bundle image {name!r} must be an object")
        digests[name] = str(entry.get("digest") or "").strip()
        repositories[name] = _repository_for_image(name, entry, registry)
    return build_promotions(
        version=version,
        digests=digests,
        registry=registry,
        repositories=repositories,
    )


# ───────────────────────── staging-gate precondition ─────────────────────


def load_staging_evidence(path: Path, *, bundle_id: str | None = None) -> dict[str, Any]:
    """Return the newest staging-gate JSONL record (optionally for a bundle).

    The staging gate (``backend.agents.staging_gate``, RT-05d) appends one
    JSON line per run carrying ``status`` + the observed backend/frontend
    digests. We take the most recent matching record so a re-run reads the
    latest verdict.
    """

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise StagingGateNotPassed(f"staging-gate evidence not found: {path}") from exc
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if bundle_id is not None and obj.get("bundle_id") not in (None, bundle_id):
            continue
        records.append(obj)
    if not records:
        raise StagingGateNotPassed(f"no staging-gate records in {path}")
    return records[-1]


def assert_staging_gate_passed(evidence: dict[str, Any], promotions: Sequence[ImagePromotion]) -> None:
    """Refuse the promote unless ``evidence`` is a green gate over *these* digests.

    Binds the gate to the digests being promoted: a green run for a
    different image pair must not authorise this promote.
    """

    status = str(evidence.get("status") or "").strip().lower()
    if status != _STAGING_GREEN:
        raise StagingGateNotPassed(
            f"staging gate is not green (status={status!r}); refusing to promote"
        )
    by_name = {p.name: p.digest for p in promotions}
    expected = {
        "backend": evidence.get("backend_digest"),
        "frontend": evidence.get("frontend_digest"),
    }
    for name, validated in by_name.items():
        observed = str(expected.get(name) or "").strip()
        if observed != validated:
            raise StagingGateNotPassed(
                f"staging-gate {name} digest {observed!r} does not match the "
                f"validated digest {validated!r}; refusing to promote"
            )


# ───────────────────────── registry retag + verify ───────────────────────


def _exec(cmd: list[str], *, runner: Runner) -> "subprocess.CompletedProcess[str]":
    return runner(cmd, cwd=REPO_ROOT, check=True, text=True)


def _imagetools_create(promotion: ImagePromotion, *, runner: Runner) -> None:
    """Retag by digest -> version. Pure registry op; never a build."""

    _exec(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "-t",
            promotion.target_ref,
            promotion.source_ref,
        ],
        runner=runner,
    )


def resolve_tag_digest(ref: str, *, runner: Runner) -> str | None:
    """Return the manifest digest the registry resolves ``ref`` to, or
    ``None`` if the tag does not exist."""

    proc = runner(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            ref,
            "--format",
            "{{.Manifest.Digest}}",
        ],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    digest = (proc.stdout or "").strip()
    return digest or None


def verify_signature(promotion: ImagePromotion, *, runner: Runner) -> None:
    _exec(["bash", str(VERIFY_SIGNATURE_SH), promotion.source_ref], runner=runner)


def promote_one_image(promotion: ImagePromotion, *, runner: Runner) -> bool:
    """Retag (idempotently) and verify one image; return digest equality.

    * If the target tag already resolves to the validated digest, the
      retag is skipped (idempotent re-run).
    * If it resolves to a *different* digest, the version is already
      claimed by other content -> hard error (never overwrite).
    * Otherwise retag, then re-inspect and require the final tag resolves
      to the exact validated digest.
    """

    existing = resolve_tag_digest(promotion.target_ref, runner=runner)
    if existing == promotion.digest:
        return True
    if existing is not None:
        raise PromoteError(
            f"{promotion.target_ref} already resolves to {existing}; refusing to "
            f"retag over it with {promotion.digest}"
        )
    _imagetools_create(promotion, runner=runner)
    final = resolve_tag_digest(promotion.target_ref, runner=runner)
    if final != promotion.digest:
        raise PromoteError(
            f"final tag {promotion.target_ref} resolved to {final!r}, expected the "
            f"validated digest {promotion.digest!r}"
        )
    return True


# ───────────────────────── attestation + audit ───────────────────────────


def sign_attestation(
    subject: ImagePromotion,
    *,
    bundle_id: str,
    from_env: str,
    version: str,
    actor: str,
    approval_refs: list[str],
    parent_attestation: str | None,
    predicate_out_dir: Path | None,
    dry_run: bool,
    runner: Runner,
) -> None:
    cmd = [
        sys.executable,
        str(SIGN_SCRIPT),
        "--bundle",
        bundle_id,
        "--from",
        from_env,
        "--to",
        version,
        "--actor",
        actor,
        "--approval-refs",
        ",".join(approval_refs),
        "--image-ref",
        subject.source_ref,
    ]
    if parent_attestation:
        cmd += ["--parent-attestation", parent_attestation]
    if predicate_out_dir is not None:
        cmd += ["--predicate-out-dir", str(predicate_out_dir)]
    if dry_run:
        cmd.append("--dry-run")
        print("DRY RUN: would exec " + " ".join(cmd))
        return
    _exec(cmd, runner=runner)


def append_audit_row(
    audit_log: Path,
    *,
    bundle_id: str,
    from_env: str,
    version: str,
    actor: str,
    approval_refs: list[str],
    promotions: Iterable[ImagePromotion],
    per_image_equality: dict[str, bool],
    parent_attestation: str | None,
    time: str | None = None,
) -> dict[str, Any]:
    """Append exactly one bundle audit row. Raising here aborts the promote
    (the row is a hard gate)."""

    promotions = list(promotions)
    row = {
        "event": "image_bundle_promoted",
        "bundle_id": bundle_id,
        "from_env": from_env,
        "version": version,
        "actor": actor,
        "time": time or utc_now_iso(),
        "approval_refs": approval_refs,
        "parent_attestation": parent_attestation,
        "final_digest_equality": all(per_image_equality.get(p.name, False) for p in promotions),
        "images": [
            {
                "name": p.name,
                "repository": p.repository,
                "digest": p.digest,
                "source_ref": p.source_ref,
                "target_ref": p.target_ref,
                "final_digest_equality": per_image_equality.get(p.name, False),
            }
            for p in promotions
        ],
    }
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    with audit_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row


# ───────────────────────── orchestration ─────────────────────────────────


def promote_images(
    promotions: Sequence[ImagePromotion],
    *,
    version: str,
    bundle_id: str,
    from_env: str,
    actor: str,
    approval_refs: list[str],
    audit_log: Path = DEFAULT_AUDIT_LOG,
    parent_attestation: str | None = None,
    predicate_out_dir: Path | None = None,
    staging_evidence: dict[str, Any] | None = None,
    require_staging_gate: bool = True,
    dry_run: bool = True,
    verify_cosign: bool = True,
    runner: Runner = subprocess.run,
) -> PromoteOutcome:
    """Retag the validated digests to ``version``, verify, attest, audit.

    Ordering: staging-gate precondition -> per-image retag+verify ->
    per-image attestation -> one bundle audit row. The retag NEVER builds
    and NEVER creates a git tag (RT-20).
    """

    version = _clean_version(version)
    promotions = list(promotions)

    # (0) staging-gate precondition (RT-05e). The migration gate (RT-11)
    # is enforced upstream by backend.agents.release_train.promote, which
    # runs the migration_gate before invoking this retag as its tag_writer.
    if require_staging_gate:
        if staging_evidence is None:
            raise StagingGateNotPassed(
                "no staging-gate evidence supplied; promote requires a green "
                "staging gate (RT-05e). Pass --staging-evidence or, for an "
                "isolated local-registry test, --skip-staging-gate."
            )
        assert_staging_gate_passed(staging_evidence, promotions)

    print(f"Promoting bundle {bundle_id} -> {version} (from {from_env})")

    per_image: dict[str, bool] = {}
    if dry_run:
        for promotion in promotions:
            print(f"  {promotion.source_ref} -> {promotion.target_ref}")
            print(
                "DRY RUN: would exec docker buildx imagetools create -t "
                f"{promotion.target_ref} {promotion.source_ref}"
            )
            print(f"DRY RUN: would verify final tag {promotion.target_ref} -> {promotion.digest}")
            if verify_cosign:
                print(
                    "DRY RUN: would exec bash "
                    f"{VERIFY_SIGNATURE_SH} {promotion.source_ref}"
                )
            per_image[promotion.name] = True
    else:
        for promotion in promotions:
            print(f"  {promotion.source_ref} -> {promotion.target_ref}")
            if verify_cosign:
                verify_signature(promotion, runner=runner)
            else:
                print(f"SKIP: cosign verify {promotion.source_ref}")
            per_image[promotion.name] = promote_one_image(promotion, runner=runner)

    # Per-image attestation (RT-12: one attestation per image, not just the
    # first of the pair).
    for promotion in promotions:
        sign_attestation(
            promotion,
            bundle_id=bundle_id,
            from_env=from_env,
            version=version,
            actor=actor,
            approval_refs=approval_refs,
            parent_attestation=parent_attestation,
            predicate_out_dir=predicate_out_dir,
            dry_run=dry_run,
            runner=runner,
        )

    overall = all(per_image.get(p.name, False) for p in promotions)
    audit_row: dict[str, Any] | None = None
    if dry_run:
        print(f"DRY RUN: would append bundle audit row to {audit_log}")
    else:
        # Hard gate: one bundle audit row; a failed write raises and aborts.
        audit_row = append_audit_row(
            audit_log,
            bundle_id=bundle_id,
            from_env=from_env,
            version=version,
            actor=actor,
            approval_refs=approval_refs,
            promotions=promotions,
            per_image_equality=per_image,
            parent_attestation=parent_attestation,
        )

    return PromoteOutcome(
        version=version,
        promotions=promotions,
        final_digest_equality=overall,
        per_image_equality=per_image,
        audit_row=audit_row,
    )


def promote_bundle(
    *,
    bundle_path: Path,
    version: str,
    from_env: str,
    actor: str,
    approval_refs: list[str],
    audit_log: Path = DEFAULT_AUDIT_LOG,
    registry: str = DEFAULT_REGISTRY,
    parent_attestation: str | None = None,
    predicate_out_dir: Path | None = None,
    staging_evidence_path: Path | None = None,
    require_staging_gate: bool = True,
    dry_run: bool = True,
    verify_cosign: bool = True,
    runner: Runner = subprocess.run,
) -> PromoteOutcome:
    """CLI entrypoint: read the bundle, plan the pair, and promote."""

    bundle = _load_json(bundle_path)
    bundle_id = str(bundle.get("bundle_id") or bundle_path.stem)
    promotions = planned_promotions(bundle, version=version, registry=registry)

    staging_evidence: dict[str, Any] | None = None
    if require_staging_gate and staging_evidence_path is not None:
        staging_evidence = load_staging_evidence(staging_evidence_path, bundle_id=bundle_id)

    return promote_images(
        promotions,
        version=version,
        bundle_id=bundle_id,
        from_env=from_env,
        actor=actor,
        approval_refs=approval_refs,
        audit_log=audit_log,
        parent_attestation=parent_attestation,
        predicate_out_dir=predicate_out_dir,
        staging_evidence=staging_evidence,
        require_staging_gate=require_staging_gate,
        dry_run=dry_run,
        verify_cosign=verify_cosign,
        runner=runner,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="promote_image_bundle",
        description="RT-12 promote: retag validated GitLab CR digests to vX.Y.Z.",
    )
    parser.add_argument("--bundle", required=True, help="Bundle id or path to bundle JSON.")
    parser.add_argument("--from", dest="from_env", required=True, help="Source environment (e.g. staging).")
    parser.add_argument("--to", dest="version", required=True, help="Reserved release version vX.Y.Z.")
    parser.add_argument("--actor", required=True, help="Human or bot approving the promotion.")
    parser.add_argument("--approval-refs", required=True, help="Comma-separated JIRA/change refs.")
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG)
    parser.add_argument(
        "--registry",
        default=os.environ.get(
            "OMNISIGHT_REGISTRY",
            os.environ.get("OMNISIGHT_IMAGE_REGISTRY_PREFIX", DEFAULT_REGISTRY),
        ),
        help="GitLab CR registry prefix when bundle image entries omit repository.",
    )
    parser.add_argument("--parent-attestation", default=None)
    parser.add_argument("--predicate-out-dir", type=Path, default=None)
    parser.add_argument(
        "--staging-evidence",
        type=Path,
        default=None,
        help="Staging-gate JSONL evidence (RT-05e) proving the pair is green.",
    )
    parser.add_argument(
        "--skip-staging-gate",
        action="store_true",
        help="Skip the staging-gate precondition; isolated local-registry tests only.",
    )
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    parser.add_argument(
        "--skip-cosign-verify",
        action="store_true",
        help="Skip digest signature verification; isolated local-registry tests only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    approval_refs = [v.strip() for v in args.approval_refs.split(",") if v.strip()]
    if not approval_refs:
        raise SystemExit("--approval-refs must contain at least one reference")
    try:
        bundle_path = resolve_bundle_path(args.bundle)
        promote_bundle(
            bundle_path=bundle_path,
            version=args.version,
            from_env=args.from_env,
            actor=args.actor,
            approval_refs=approval_refs,
            audit_log=args.audit_log,
            registry=args.registry,
            parent_attestation=args.parent_attestation,
            predicate_out_dir=args.predicate_out_dir,
            staging_evidence_path=args.staging_evidence,
            require_staging_gate=not args.skip_staging_gate,
            dry_run=args.dry_run,
            verify_cosign=not args.skip_cosign_verify,
        )
    except PromoteError as exc:
        print(f"promote failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
