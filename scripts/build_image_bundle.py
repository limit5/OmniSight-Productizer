#!/usr/bin/env python3
"""OP-1479 — assemble a release bundle manifest after the three runtime images build.

CI invokes this after ``Build Images`` finishes for backend, frontend,
and bridge. It collects the three image digests, the git ref/sha that
triggered the build, the committed OpenAPI hash, and the alembic head
the backend image was baked against, then writes a single
``artifacts/bundle-<id>.json`` matching ``omnisight-bundle.schema.json``.

The same file is also baked into the runtime images at
``/app/bundle.json`` (via the Dockerfiles) so ``/api/version`` can echo
the ``bundle_id`` an operator just deployed without a separate registry
roundtrip.

Usage (CI):

    python3 scripts/build_image_bundle.py \\
        --git-ref refs/tags/v0.5.0-rc3 \\
        --git-sha 3f1c0a4e... \\
        --backend-digest sha256:aa... \\
        --frontend-digest sha256:bb... \\
        --bridge-digest sha256:cc... \\
        --out artifacts/bundle-<id>.json

Usage (operator, local verification):

    python3 scripts/build_image_bundle.py \\
        --git-ref refs/tags/v0.5.0-rc3 \\
        --backend-digest sha256:aa --frontend-digest sha256:bb \\
        --bridge-digest sha256:cc --out /tmp/bundle.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OPENAPI_PATH = REPO_ROOT / "openapi.json"
DEFAULT_SCHEMA_PATH = REPO_ROOT / "omnisight-bundle.schema.json"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def _resolve_git_sha(arg_value: str | None) -> str:
    if arg_value:
        return arg_value
    out = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True,
    ).strip()
    return out


def _short_sha(sha: str) -> str:
    return sha[:8] if len(sha) >= 8 else sha


def _bundle_id_from_ref(git_ref: str, git_sha: str) -> str:
    """Derive a stable bundle id from the ref tail + short sha.

    Examples:
        refs/tags/v0.5.0-rc3 + 3f1c0a4e... → 'v0.5.0-rc3-3f1c0a4e'
        refs/heads/develop   + 3f1c0a4e... → 'develop-3f1c0a4e'
    """
    tail = git_ref.rsplit("/", 1)[-1] if "/" in git_ref else git_ref
    return f"{tail}-{_short_sha(git_sha)}"


def _hash_openapi(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _alembic_head(arg_value: str | None) -> str:
    """Return the single alembic head the backend image will carry.

    When the caller passes ``--db-migration-head`` we trust it (CI will).
    Otherwise we try to read it from the most recent baked
    ``MANIFEST.json`` next to the script's caller — same heads the
    backend image already exposes via ``/runtime/release/version``.
    """
    if arg_value:
        return arg_value
    manifest_candidates = [
        REPO_ROOT / "MANIFEST.json",
        Path("/app/MANIFEST.json"),
    ]
    for cand in manifest_candidates:
        try:
            raw = json.loads(cand.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        head = raw.get("alembic_head_in_image")
        if isinstance(head, str) and head:
            return head
    return "unknown"


def _validate_digest(name: str, value: str) -> None:
    if not _DIGEST_RE.match(value):
        raise SystemExit(
            f"--{name}-digest must look like 'sha256:<64 hex chars>' (got: {value!r})",
        )


def _validate_sha(value: str) -> None:
    if not _SHA40_RE.match(value):
        raise SystemExit(
            f"--git-sha must be a 40-char lowercase hex SHA (got: {value!r})",
        )


def build_bundle(
    *,
    git_ref: str,
    git_sha: str,
    backend_digest: str,
    frontend_digest: str,
    bridge_digest: str,
    bundle_id: str | None = None,
    build_time: datetime | None = None,
    openapi_path: Path = DEFAULT_OPENAPI_PATH,
    db_migration_head: str | None = None,
    api_required: str = "v1",
    api_supported: tuple[str, ...] = ("v1", "v2"),
    frontend_built_against_api: str = "v1",
    backend_repository: str | None = None,
    frontend_repository: str | None = None,
    bridge_repository: str | None = None,
    backend_tag: str | None = None,
    frontend_tag: str | None = None,
    bridge_tag: str | None = None,
    signatures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pure function (no I/O beyond reading openapi.json) that builds the bundle dict.

    Split out from ``main`` so the unit tests can exercise it without
    spawning the CLI.
    """
    _validate_digest("backend", backend_digest)
    _validate_digest("frontend", frontend_digest)
    _validate_digest("bridge", bridge_digest)
    _validate_sha(git_sha)

    bundle_id = bundle_id or _bundle_id_from_ref(git_ref, git_sha)
    build_time = build_time or datetime.now(timezone.utc)
    build_time_iso = build_time.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )

    def _image(digest: str, repo: str | None, tag: str | None) -> dict[str, str]:
        entry: dict[str, str] = {"digest": digest}
        if repo:
            entry["repository"] = repo
        if tag:
            entry["tag"] = tag
        return entry

    return {
        "bundle_id": bundle_id,
        "git_ref": git_ref,
        "git_sha": git_sha,
        "build_time": build_time_iso,
        "images": {
            "backend": _image(backend_digest, backend_repository, backend_tag),
            "frontend": _image(frontend_digest, frontend_repository, frontend_tag),
            "bridge": _image(bridge_digest, bridge_repository, bridge_tag),
        },
        "contracts": {
            "api_required": api_required,
            "api_supported": list(api_supported),
            "openapi_hash": _hash_openapi(openapi_path),
            "db_migration_head": _alembic_head(db_migration_head),
            "frontend_built_against_api": frontend_built_against_api,
        },
        "signatures": list(signatures) if signatures else [],
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_image_bundle",
        description="Build the OmniSight release bundle manifest.",
    )
    parser.add_argument("--git-ref", required=True, help="Full git ref (e.g. refs/tags/v0.5.0-rc3).")
    parser.add_argument("--git-sha", default=None, help="40-char commit SHA. Default: git rev-parse HEAD.")
    parser.add_argument("--backend-digest", required=True, help="Backend image digest (sha256:...).")
    parser.add_argument("--frontend-digest", required=True, help="Frontend image digest (sha256:...).")
    parser.add_argument("--bridge-digest", required=True, help="Bridge image digest (sha256:...).")
    parser.add_argument("--bundle-id", default=None, help="Override bundle id (default derived from ref + sha).")
    parser.add_argument("--openapi", default=str(DEFAULT_OPENAPI_PATH), help="Path to openapi.json for hashing.")
    parser.add_argument("--db-migration-head", default=None, help="Alembic head baked into the backend image.")
    parser.add_argument("--api-required", default="v1", help="Minimum API version the backend accepts.")
    parser.add_argument(
        "--api-supported",
        default="v1,v2",
        help="Comma-separated list of API versions the backend mounts.",
    )
    parser.add_argument(
        "--frontend-built-against-api",
        default="v1",
        help="API version the bundled frontend was built against.",
    )
    parser.add_argument("--backend-repository", default=None)
    parser.add_argument("--frontend-repository", default=None)
    parser.add_argument("--bridge-repository", default=None)
    parser.add_argument("--backend-tag", default=None)
    parser.add_argument("--frontend-tag", default=None)
    parser.add_argument("--bridge-tag", default=None)
    parser.add_argument("--out", required=True, help="Output path for the bundle JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    git_sha = _resolve_git_sha(args.git_sha)
    api_supported = tuple(v.strip() for v in args.api_supported.split(",") if v.strip())

    bundle = build_bundle(
        git_ref=args.git_ref,
        git_sha=git_sha,
        backend_digest=args.backend_digest,
        frontend_digest=args.frontend_digest,
        bridge_digest=args.bridge_digest,
        bundle_id=args.bundle_id,
        openapi_path=Path(args.openapi),
        db_migration_head=args.db_migration_head,
        api_required=args.api_required,
        api_supported=api_supported,
        frontend_built_against_api=args.frontend_built_against_api,
        backend_repository=args.backend_repository,
        frontend_repository=args.frontend_repository,
        bridge_repository=args.bridge_repository,
        backend_tag=args.backend_tag,
        frontend_tag=args.frontend_tag,
        bridge_tag=args.bridge_tag,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(bundle, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
