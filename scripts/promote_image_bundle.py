#!/usr/bin/env python3
"""OP-1481 — promote an immutable image bundle by digest retag.

Promotion never rebuilds. Each image from the bundle manifest is
retagged with ``docker buildx imagetools create`` from the source
digest to the target environment alias, then a promotion attestation
and local JSONL audit row are written.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIT_LOG = REPO_ROOT / "audit" / "image_promotion_audit.jsonl"
DEFAULT_REGISTRY_PREFIX = "ghcr.io/omnisight"
SIGN_SCRIPT = REPO_ROOT / "scripts" / "sign_promotion_attestation.py"

Runner = Callable[..., subprocess.CompletedProcess[str]]

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class ImagePromotion:
    name: str
    repository: str
    digest: str
    source_ref: str
    target_ref: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"bundle not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"bundle is not valid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"bundle root must be an object: {path}")
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
    raise SystemExit(
        f"could not resolve bundle {bundle!r}; pass a bundle JSON path or create artifacts/bundle-{bundle}.json",
    )


def _repository_for_image(name: str, entry: dict[str, Any], registry_prefix: str) -> str:
    repo = str(entry.get("repository") or "").strip()
    if repo:
        return repo
    return f"{registry_prefix.rstrip('/')}/omnisight-{name}"


def planned_promotions(
    bundle: dict[str, Any],
    *,
    to_env: str,
    registry_prefix: str = DEFAULT_REGISTRY_PREFIX,
) -> list[ImagePromotion]:
    images = bundle.get("images")
    if not isinstance(images, dict) or not images:
        raise SystemExit("bundle images must be a non-empty object")

    planned: list[ImagePromotion] = []
    for name in sorted(images):
        entry = images[name]
        if not isinstance(entry, dict):
            raise SystemExit(f"bundle image {name!r} must be an object")
        digest = str(entry.get("digest") or "").strip()
        if not _DIGEST_RE.match(digest):
            raise SystemExit(f"bundle image {name!r} digest must look like sha256:<64 hex chars>")
        repository = _repository_for_image(name, entry, registry_prefix)
        planned.append(
            ImagePromotion(
                name=name,
                repository=repository,
                digest=digest,
                source_ref=f"{repository}@{digest}",
                target_ref=f"{repository}:{to_env}",
            ),
        )
    return planned


def _run(cmd: list[str], *, dry_run: bool, runner: Runner) -> None:
    if dry_run:
        print("DRY RUN: would exec " + " ".join(cmd))
        return
    runner(cmd, cwd=REPO_ROOT, check=True, text=True)


def retag_image(promotion: ImagePromotion, *, dry_run: bool, runner: Runner) -> None:
    _run(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "-t",
            promotion.target_ref,
            promotion.source_ref,
        ],
        dry_run=dry_run,
        runner=runner,
    )


def verify_signature(promotion: ImagePromotion, *, dry_run: bool, runner: Runner) -> None:
    _run(
        ["cosign", "verify", promotion.source_ref],
        dry_run=dry_run,
        runner=runner,
    )


def sign_attestation(
    subject: ImagePromotion,
    *,
    bundle_id: str,
    from_env: str,
    to_env: str,
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
        to_env,
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
    _run(cmd, dry_run=dry_run, runner=runner)


def append_audit_row(
    audit_log: Path,
    *,
    bundle_id: str,
    from_env: str,
    to_env: str,
    actor: str,
    approval_refs: list[str],
    promotions: Iterable[ImagePromotion],
    parent_attestation: str | None,
    time: str | None = None,
) -> dict[str, Any]:
    row = {
        "event": "image_bundle_promoted",
        "bundle_id": bundle_id,
        "from_env": from_env,
        "to_env": to_env,
        "actor": actor,
        "time": time or utc_now_iso(),
        "approval_refs": approval_refs,
        "parent_attestation": parent_attestation,
        "images": [
            {
                "name": p.name,
                "repository": p.repository,
                "digest": p.digest,
                "source_ref": p.source_ref,
                "target_ref": p.target_ref,
            }
            for p in promotions
        ],
    }
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    with audit_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def update_env_lock(
    lock_path: Path,
    *,
    bundle: dict[str, Any],
    to_env: str,
    promotions: Iterable[ImagePromotion],
    time: str | None = None,
) -> dict[str, Any]:
    existing: dict[str, Any] = {}
    if lock_path.exists():
        existing = _load_json(lock_path)
    lock = {
        **existing,
        "environment": to_env,
        "bundle_id": bundle.get("bundle_id"),
        "git_ref": bundle.get("git_ref"),
        "git_sha": bundle.get("git_sha"),
        "promoted_at": time or utc_now_iso(),
        "images": {
            p.name: {
                "repository": p.repository,
                "digest": p.digest,
                "tag": to_env,
                "ref": p.target_ref,
            }
            for p in promotions
        },
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return lock


def promote_bundle(
    *,
    bundle_path: Path,
    from_env: str,
    to_env: str,
    actor: str,
    approval_refs: list[str],
    audit_log: Path = DEFAULT_AUDIT_LOG,
    lock_path: Path | None = None,
    registry_prefix: str = DEFAULT_REGISTRY_PREFIX,
    parent_attestation: str | None = None,
    predicate_out_dir: Path | None = None,
    dry_run: bool = True,
    verify_cosign: bool = True,
    runner: Runner = subprocess.run,
) -> list[ImagePromotion]:
    bundle = _load_json(bundle_path)
    bundle_id = str(bundle.get("bundle_id") or bundle_path.stem)
    promotions = planned_promotions(bundle, to_env=to_env, registry_prefix=registry_prefix)

    print(f"Promoting bundle {bundle_id}: {from_env} -> {to_env}")
    for promotion in promotions:
        print(f"  {promotion.source_ref} -> {promotion.target_ref}")
        retag_image(promotion, dry_run=dry_run, runner=runner)
        if verify_cosign:
            verify_signature(promotion, dry_run=dry_run, runner=runner)
        else:
            print(f"SKIP: cosign verify {promotion.source_ref}")

    sign_attestation(
        promotions[0],
        bundle_id=bundle_id,
        from_env=from_env,
        to_env=to_env,
        actor=actor,
        approval_refs=approval_refs,
        parent_attestation=parent_attestation,
        predicate_out_dir=predicate_out_dir,
        dry_run=dry_run,
        runner=runner,
    )

    target_lock = lock_path or (REPO_ROOT / f"{to_env}.env.lock.json")
    if dry_run:
        print(f"DRY RUN: would append audit row to {audit_log}")
        print(f"DRY RUN: would update lock file {target_lock}")
    else:
        append_audit_row(
            audit_log,
            bundle_id=bundle_id,
            from_env=from_env,
            to_env=to_env,
            actor=actor,
            approval_refs=approval_refs,
            promotions=promotions,
            parent_attestation=parent_attestation,
        )
        update_env_lock(target_lock, bundle=bundle, to_env=to_env, promotions=promotions)
    return promotions


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="promote_image_bundle",
        description="Promote an immutable OmniSight image bundle by digest retag.",
    )
    parser.add_argument("--bundle", required=True, help="Bundle id or path to bundle JSON.")
    parser.add_argument("--from", dest="from_env", required=True, help="Source environment alias.")
    parser.add_argument("--to", dest="to_env", required=True, help="Target environment alias.")
    parser.add_argument("--actor", required=True, help="Human or bot approving the promotion.")
    parser.add_argument("--approval-refs", required=True, help="Comma-separated JIRA/change refs.")
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG)
    parser.add_argument("--lock-file", type=Path, default=None, help="Override target {env}.env.lock.json path.")
    parser.add_argument(
        "--registry-prefix",
        default=os.environ.get("OMNISIGHT_IMAGE_REGISTRY_PREFIX", DEFAULT_REGISTRY_PREFIX),
        help="Default registry prefix when bundle image entries omit repository.",
    )
    parser.add_argument("--parent-attestation", default=None)
    parser.add_argument("--predicate-out-dir", type=Path, default=None)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    parser.add_argument(
        "--skip-cosign-verify",
        action="store_true",
        help="Skip digest signature verification; intended only for isolated local registry tests.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    approval_refs = [v.strip() for v in args.approval_refs.split(",") if v.strip()]
    if not approval_refs:
        raise SystemExit("--approval-refs must contain at least one reference")
    bundle_path = resolve_bundle_path(args.bundle)
    promote_bundle(
        bundle_path=bundle_path,
        from_env=args.from_env,
        to_env=args.to_env,
        actor=args.actor,
        approval_refs=approval_refs,
        audit_log=args.audit_log,
        lock_path=args.lock_file,
        registry_prefix=args.registry_prefix,
        parent_attestation=args.parent_attestation,
        predicate_out_dir=args.predicate_out_dir,
        dry_run=args.dry_run,
        verify_cosign=not args.skip_cosign_verify,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
