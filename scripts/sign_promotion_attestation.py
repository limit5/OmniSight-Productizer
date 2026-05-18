#!/usr/bin/env python3
"""OP-1481 — sign an image-promotion attestation with cosign keyless.

The promotion CLI builds the predicate and delegates the actual
``cosign attest`` call here so operators can re-run or inspect the
attestation step independently.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PREDICATE_DIR = REPO_ROOT / "audit" / "promotion-predicates"
DEFAULT_ATTESTATION_TYPE = "omnisight.image.promotion.v1"

Runner = Callable[..., subprocess.CompletedProcess[str]]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_predicate(
    *,
    bundle_id: str,
    from_env: str,
    to_env: str,
    actor: str,
    approval_refs: list[str],
    parent_attestation: str | None,
    image_ref: str,
    time: str | None = None,
) -> dict[str, Any]:
    return {
        "bundle_id": bundle_id,
        "from_env": from_env,
        "to_env": to_env,
        "actor": actor,
        "time": time or utc_now_iso(),
        "approval_refs": approval_refs,
        "parent_attestation": parent_attestation,
        "image_ref": image_ref,
    }


def write_predicate(predicate: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_bundle = "".join(c if c.isalnum() or c in "._-" else "-" for c in predicate["bundle_id"])
    safe_image = "".join(c if c.isalnum() or c in "._-" else "-" for c in predicate["image_ref"].split("/")[-1])
    path = out_dir / f"{safe_bundle}-{predicate['to_env']}-{safe_image}.promotion.json"
    path.write_text(json.dumps(predicate, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def attest(
    *,
    image_ref: str,
    predicate_path: Path,
    attestation_type: str = DEFAULT_ATTESTATION_TYPE,
    dry_run: bool = False,
    runner: Runner = subprocess.run,
) -> list[str]:
    cmd = [
        "cosign",
        "attest",
        "--predicate",
        str(predicate_path),
        "--type",
        attestation_type,
        "--yes",
        image_ref,
    ]
    if dry_run:
        print("DRY RUN: would exec " + " ".join(cmd))
        return cmd
    runner(cmd, cwd=REPO_ROOT, check=True, text=True)
    return cmd


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sign_promotion_attestation",
        description="Write and cosign an OmniSight image-promotion attestation.",
    )
    parser.add_argument("--bundle", required=True, help="Bundle id promoted.")
    parser.add_argument("--from", dest="from_env", required=True, help="Source environment alias.")
    parser.add_argument("--to", dest="to_env", required=True, help="Target environment alias.")
    parser.add_argument("--actor", required=True, help="Human or bot approving the promotion.")
    parser.add_argument("--approval-refs", required=True, help="Comma-separated JIRA/change refs.")
    parser.add_argument("--image-ref", required=True, help="Digest image ref being attested.")
    parser.add_argument("--parent-attestation", default=None, help="Previous promotion attestation digest/ref.")
    parser.add_argument("--predicate-out-dir", type=Path, default=DEFAULT_PREDICATE_DIR)
    parser.add_argument("--type", default=DEFAULT_ATTESTATION_TYPE, help="Cosign attestation type.")
    parser.add_argument("--dry-run", action="store_true", help="Write predicate and print cosign command.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    approval_refs = [v.strip() for v in args.approval_refs.split(",") if v.strip()]
    predicate = build_predicate(
        bundle_id=args.bundle,
        from_env=args.from_env,
        to_env=args.to_env,
        actor=args.actor,
        approval_refs=approval_refs,
        parent_attestation=args.parent_attestation,
        image_ref=args.image_ref,
    )
    predicate_path = write_predicate(predicate, args.predicate_out_dir)
    print(predicate_path)
    attest(
        image_ref=args.image_ref,
        predicate_path=predicate_path,
        attestation_type=args.type,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
