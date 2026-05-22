#!/usr/bin/env python3
"""OP-1591 / RT-13a -- release-train status + rollback digest resolver.

Read-only operator tool for the release-train identity question:

* status joins ``/api/version`` overlay, the env compose lock, registry
  resolution, and ``release_audit`` into
  ``vX.Y.Z == git_sha == {backend,frontend digests}``.
* rollback mode (``--to vX.Y.Z``) resolves the backend/frontend digest
  pair from immutable ``release_audit`` only. It does not deploy.

The live prod exercise is RT-13b; this script owns the resolver logic.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = "prod"
DEFAULT_TIMEOUT_SECONDS = 10.0

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_STATUS_MISMATCH = 3
EXIT_AUDIT_NOT_FOUND = 4


class ReleaseTrainStatusError(RuntimeError):
    """Base class for resolver failures."""


class AuditNotFound(ReleaseTrainStatusError):
    """No immutable audit row could resolve the requested release."""


class IdentityMismatch(ReleaseTrainStatusError):
    """One of the joined status sources disagreed."""


@dataclass(frozen=True)
class DigestPair:
    backend: str
    frontend: str

    def as_dict(self) -> dict[str, str]:
        return {"backend": self.backend, "frontend": self.frontend}


@dataclass(frozen=True)
class AuditRelease:
    version: str
    git_sha: str
    digests: DigestPair
    audit_id: int | None = None
    ts: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "git_sha": self.git_sha,
            "digests": self.digests.as_dict(),
            "audit_id": self.audit_id,
            "ts": self.ts,
        }


@dataclass(frozen=True)
class StatusReport:
    env: str
    version: str
    git_sha: str
    digests: DigestPair
    promotion_audit_id: str | None
    registry: dict[str, str]
    audit: AuditRelease

    def as_dict(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "version": self.version,
            "git_sha": self.git_sha,
            "digests": self.digests.as_dict(),
            "promotion_audit_id": self.promotion_audit_id,
            "registry": self.registry,
            "audit": self.audit.as_dict(),
        }


def _clean_version(value: Any, field: str = "version") -> str:
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value.strip()):
        raise ValueError(f"{field} must be a vX.Y.Z release tag")
    return value.strip()


def _clean_sha(value: Any, field: str = "git_sha") -> str:
    if not isinstance(value, str) or not _FULL_SHA_RE.fullmatch(value.strip().lower()):
        raise ValueError(f"{field} must be a full 40-char git SHA")
    return value.strip().lower()


def _clean_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.strip()):
        raise ValueError(f"{field} must be a sha256 digest")
    return value.strip()


def _json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _fetch_version_payload(
    *,
    version_json: Path | None,
    version_url: str | None,
) -> dict:
    if version_json is not None:
        raw = _json_file(version_json)
    elif version_url:
        with urllib.request.urlopen(
            version_url,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        ) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    else:
        raise ValueError("--version-json or --version-url is required for status")
    if not isinstance(raw, dict):
        raise ValueError("/api/version payload must be a JSON object")
    return raw


def _load_lock(path: Path) -> dict:
    raw = _json_file(path)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return raw


def _lock_image(lock: dict, name: str) -> tuple[str, str]:
    images = lock.get("images")
    if not isinstance(images, dict):
        raise ValueError("compose lock missing images object")
    entry = images.get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"compose lock missing images.{name}")
    repo = entry.get("repository")
    digest = entry.get("digest")
    if not isinstance(repo, str) or not repo:
        raise ValueError(f"compose lock images.{name}.repository must be set")
    return repo, _clean_digest(digest, f"compose lock images.{name}.digest")


def _overlay_digest_pair(payload: dict) -> DigestPair:
    return DigestPair(
        backend=_clean_digest(
            payload.get("deployed_digest_backend"), "deployed_digest_backend"
        ),
        frontend=_clean_digest(
            payload.get("deployed_digest_frontend"), "deployed_digest_frontend"
        ),
    )


def _registry_digest(image_ref: str) -> str:
    proc = subprocess.run(
        ["docker", "manifest", "inspect", image_ref, "--verbose"],
        check=False,
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise ReleaseTrainStatusError(
            f"registry inspect failed for {image_ref}: {proc.stderr.strip()}"
        )
    raw = json.loads(proc.stdout)
    digest = _extract_registry_digest(raw)
    if digest is None:
        raise ReleaseTrainStatusError(
            f"registry inspect for {image_ref} did not expose a sha256 digest"
        )
    return digest


def _extract_registry_digest(raw: Any) -> str | None:
    if isinstance(raw, dict):
        for path in (
            ("Descriptor", "digest"),
            ("descriptor", "digest"),
        ):
            node: Any = raw
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            if isinstance(node, str) and _DIGEST_RE.fullmatch(node):
                return node
        digest = raw.get("digest")
        if isinstance(digest, str) and _DIGEST_RE.fullmatch(digest):
            return digest
        manifests = raw.get("manifests")
        if isinstance(manifests, list) and manifests:
            return _extract_registry_digest(manifests[0])
    if isinstance(raw, list) and raw:
        return _extract_registry_digest(raw[0])
    return None


def _parse_detail(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            raw = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return raw if isinstance(raw, dict) else {}
    return {}


def _pick_first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _audit_release_from_row(row: dict[str, Any], version: str) -> AuditRelease | None:
    detail = _parse_detail(row.get("detail"))
    after = detail.get("after") if isinstance(detail.get("after"), dict) else {}
    digests = detail.get("digests") if isinstance(detail.get("digests"), dict) else {}
    images = detail.get("images") if isinstance(detail.get("images"), dict) else {}

    backend_image = (
        images.get("backend") if isinstance(images.get("backend"), dict) else {}
    )
    frontend_image = (
        images.get("frontend") if isinstance(images.get("frontend"), dict) else {}
    )

    row_version = _pick_first(
        row.get("fix_version"),
        detail.get("version"),
        detail.get("deployed_tag"),
        detail.get("reserved_version"),
        after.get("reserved_version"),
    )
    try:
        resolved_version = _clean_version(row_version, "audit version")
    except ValueError:
        return None
    if resolved_version != version:
        return None

    git_sha = _pick_first(
        row.get("develop_sha"),
        detail.get("git_sha"),
        detail.get("candidate_sha"),
        detail.get("build_git_sha"),
        after.get("candidate_sha"),
    )
    backend_digest = _pick_first(
        detail.get("source_digest_backend"),
        detail.get("deployed_digest_backend"),
        digests.get("backend"),
        backend_image.get("digest"),
        after.get("source_digest_backend"),
    )
    frontend_digest = _pick_first(
        detail.get("source_digest_frontend"),
        detail.get("deployed_digest_frontend"),
        digests.get("frontend"),
        frontend_image.get("digest"),
        after.get("source_digest_frontend"),
    )

    try:
        return AuditRelease(
            version=resolved_version,
            git_sha=_clean_sha(git_sha),
            digests=DigestPair(
                backend=_clean_digest(backend_digest, "audit backend digest"),
                frontend=_clean_digest(frontend_digest, "audit frontend digest"),
            ),
            audit_id=int(row["id"]) if row.get("id") is not None else None,
            ts=str(row["ts"]) if row.get("ts") is not None else None,
        )
    except (TypeError, ValueError):
        return None


def _sqlite_audit_rows(path: Path, version: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, ts, outcome, fix_version, develop_sha, main_sha, detail
            FROM release_audit
            WHERE fix_version = ? OR detail LIKE ?
            ORDER BY id DESC
            """,
            (version, f"%{version}%"),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


async def _postgres_audit_rows(dsn: str, version: str) -> list[dict[str, Any]]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT id, ts, outcome, fix_version, develop_sha, main_sha, detail
            FROM release_audit
            WHERE fix_version = $1 OR detail LIKE $2
            ORDER BY id DESC
            """,
            version,
            f"%{version}%",
        )
        return [dict(row) for row in rows]
    finally:
        await conn.close()


def load_audit_release(
    version: str,
    *,
    audit_db: Path | None = None,
    audit_dsn: str | None = None,
    audit_json: Path | None = None,
) -> AuditRelease:
    version = _clean_version(version)
    if audit_json is not None:
        raw = _json_file(audit_json)
        rows = raw if isinstance(raw, list) else [raw]
        candidate_rows = [r for r in rows if isinstance(r, dict)]
    elif audit_db is not None:
        candidate_rows = _sqlite_audit_rows(audit_db, version)
    else:
        dsn = audit_dsn or os.environ.get("OMNISIGHT_DATABASE_URL")
        if not dsn:
            raise ValueError(
                "--audit-db, --audit-dsn, or OMNISIGHT_DATABASE_URL is required"
            )
        candidate_rows = asyncio.run(_postgres_audit_rows(dsn, version))

    for row in candidate_rows:
        release = _audit_release_from_row(row, version)
        if release is not None:
            return release
    raise AuditNotFound(f"no release_audit row resolves {version}")


def build_status_report(
    *,
    env: str,
    version_payload: dict,
    compose_lock: dict,
    audit_release: AuditRelease,
    registry_resolver: Any = _registry_digest,
) -> StatusReport:
    overlay_version = _clean_version(
        version_payload.get("deployed_tag"),
        "deployed_tag",
    )
    overlay_sha = _clean_sha(version_payload.get("build_git_sha"), "build_git_sha")
    overlay_pair = _overlay_digest_pair(version_payload)

    lock_backend_repo, lock_backend_digest = _lock_image(compose_lock, "backend")
    lock_frontend_repo, lock_frontend_digest = _lock_image(compose_lock, "frontend")
    lock_pair = DigestPair(lock_backend_digest, lock_frontend_digest)

    registry = {
        "backend": _clean_digest(
            registry_resolver(f"{lock_backend_repo}:{overlay_version}"),
            "registry backend digest",
        ),
        "frontend": _clean_digest(
            registry_resolver(f"{lock_frontend_repo}:{overlay_version}"),
            "registry frontend digest",
        ),
    }

    if audit_release.version != overlay_version:
        raise IdentityMismatch(
            f"audit version {audit_release.version} != overlay {overlay_version}"
        )
    if audit_release.git_sha != overlay_sha:
        raise IdentityMismatch(
            f"audit git_sha {audit_release.git_sha} != overlay {overlay_sha}"
        )
    if audit_release.digests != overlay_pair:
        raise IdentityMismatch("audit digest pair != /api/version overlay pair")
    if lock_pair != overlay_pair:
        raise IdentityMismatch("compose lock digest pair != /api/version overlay pair")
    if registry != overlay_pair.as_dict():
        raise IdentityMismatch("registry digest pair != /api/version overlay pair")

    return StatusReport(
        env=env,
        version=overlay_version,
        git_sha=overlay_sha,
        digests=overlay_pair,
        promotion_audit_id=(
            str(version_payload.get("promotion_audit_id"))
            if version_payload.get("promotion_audit_id") is not None
            else None
        ),
        registry=registry,
        audit=audit_release,
    )


def render_status(report: StatusReport) -> str:
    return (
        f"{report.env} {report.version} == {report.git_sha} == "
        f"{{backend:{report.digests.backend}, frontend:{report.digests.frontend}}}"
    )


def render_rollback(release: AuditRelease) -> str:
    return (
        f"rollback {release.version} -> "
        f"backend={release.digests.backend} frontend={release.digests.frontend} "
        f"git_sha={release.git_sha}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", default=DEFAULT_ENV)
    parser.add_argument("--to", help="Resolve rollback digest pair for vX.Y.Z")
    parser.add_argument("--version-json", type=Path)
    parser.add_argument("--version-url")
    parser.add_argument(
        "--compose-lock",
        type=Path,
        help="Env compose lock JSON; defaults to <env>.env.lock.json",
    )
    parser.add_argument("--audit-db", type=Path, help="SQLite release_audit export")
    parser.add_argument("--audit-dsn", help="Postgres release_audit DSN")
    parser.add_argument(
        "--audit-json",
        type=Path,
        help="JSON fixture/export of release_audit rows",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.to:
            release = load_audit_release(
                args.to,
                audit_db=args.audit_db,
                audit_dsn=args.audit_dsn,
                audit_json=args.audit_json,
            )
            print(
                json.dumps(release.as_dict(), indent=2, sort_keys=True)
                if args.json
                else render_rollback(release)
            )
            return EXIT_OK

        payload = _fetch_version_payload(
            version_json=args.version_json,
            version_url=args.version_url,
        )
        version = _clean_version(payload.get("deployed_tag"), "deployed_tag")
        lock_path = args.compose_lock or REPO_ROOT / f"{args.env}.env.lock.json"
        audit_release = load_audit_release(
            version,
            audit_db=args.audit_db,
            audit_dsn=args.audit_dsn,
            audit_json=args.audit_json,
        )
        report = build_status_report(
            env=args.env,
            version_payload=payload,
            compose_lock=_load_lock(lock_path),
            audit_release=audit_release,
        )
        print(
            json.dumps(report.as_dict(), indent=2, sort_keys=True)
            if args.json
            else render_status(report)
        )
        return EXIT_OK
    except AuditNotFound as exc:
        print(f"AuditNotFound: {exc}", file=sys.stderr)
        return EXIT_AUDIT_NOT_FOUND
    except IdentityMismatch as exc:
        print(f"IdentityMismatch: {exc}", file=sys.stderr)
        return EXIT_STATUS_MISMATCH
    except (
        ReleaseTrainStatusError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
