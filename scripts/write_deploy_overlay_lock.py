#!/usr/bin/env python3
"""OP-1606 (RT-08) — deploy-overlay lock WRITER.

The reader half of RT-08 already shipped (OP-1582): a deployed backend reads
``/etc/omnisight/deploy-overlay.lock`` ONCE AT STARTUP, serves the six identity
fields on ``/api/version`` and gates ``/readyz`` on them
(``backend.api_versioning.load_deploy_overlay`` /
``backend.routers.health._check_deploy_overlay``). Nothing wrote that lock, so
the RT-08 deployment identity was always null and the gate was silently inert
everywhere. This script is the missing writer: the deploy/promote step runs it
to emit the env-file lock from the deployed *candidate* (its bundle), so the
overlay populates and ``OMNISIGHT_REQUIRE_DEPLOY_OVERLAY=1`` can be safely
enabled on staging.

The six fields (env key → ``/api/version`` field) are exactly the ones the
reader requires — see ``backend.api_versioning._OVERLAY_LOCK_FIELDS``; the
``test_write_deploy_overlay_lock`` suite pins this writer's key set against
that reader dict so the two halves can never drift:

    OMNISIGHT_BUILD_GIT_SHA            <- bundle.git_sha
    OMNISIGHT_BUILD_GIT_REF            <- bundle.git_ref
    OMNISIGHT_DEPLOYED_TAG            <- --tag (the image tag being deployed)
    OMNISIGHT_DEPLOYED_DIGEST_BACKEND  <- bundle.images.backend.digest
    OMNISIGHT_DEPLOYED_DIGEST_FRONTEND <- bundle.images.frontend.digest
    OMNISIGHT_PROMOTION_AUDIT_ID       <- --promotion-audit-id (default: bundle_id)

Each field resolves explicit-flag > env-var > bundle-derived. Every field MUST
end up non-empty and the two digests MUST look like ``sha256:<64 hex>`` — a
partial or malformed lock is REFUSED (no file written) and the process exits
non-zero, mirroring the reader's fail-closed contract: a deployed runtime must
never advertise a half-known identity.

The lock is the same env-file (``KEY=value``) shape the compose ``env_file:``
understands, and is written atomically (temp file + ``os.replace``) so a reader
mid-deploy never sees a torn file. The final file is mode ``0644`` because the
backend runs as uid 65532 against a read-only mount and must be able to read it.

Usage (the staging deploy path, scripts/staging_deploy.sh, invokes this):

    scripts/write_deploy_overlay_lock.py \\
        --bundle artifacts/bundle-v0.5.0-rc4-4218e1c7.json \\
        --tag v0.5.0-rc4 \\
        --out /var/lib/omnisight/staging/overlay-blue/deploy-overlay.lock
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Mapping

#: Default lock path — kept in lock-step with
#: ``backend.api_versioning.DEPLOY_OVERLAY_LOCK_PATH``'s default.
DEFAULT_LOCK_PATH = "/etc/omnisight/deploy-overlay.lock"

#: A registry image digest, as the reader's digest fields expect.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Env-lock key → bundle accessor. ORDER and KEYS mirror
#: ``backend.api_versioning._OVERLAY_LOCK_FIELDS`` exactly (pinned by a test).
#: The digest keys are flagged so :func:`validate_fields` knows to shape-check
#: them; everything else need only be non-empty.
LOCK_FIELDS: tuple[str, ...] = (
    "OMNISIGHT_BUILD_GIT_SHA",
    "OMNISIGHT_BUILD_GIT_REF",
    "OMNISIGHT_DEPLOYED_TAG",
    "OMNISIGHT_DEPLOYED_DIGEST_BACKEND",
    "OMNISIGHT_DEPLOYED_DIGEST_FRONTEND",
    "OMNISIGHT_PROMOTION_AUDIT_ID",
)
_DIGEST_KEYS = frozenset(
    {"OMNISIGHT_DEPLOYED_DIGEST_BACKEND", "OMNISIGHT_DEPLOYED_DIGEST_FRONTEND"}
)


class OverlayLockError(RuntimeError):
    """A required field is missing/empty or a digest is malformed."""


def _bundle_digest(bundle: Mapping, image: str) -> str:
    images = bundle.get("images")
    if not isinstance(images, dict):
        return ""
    entry = images.get(image)
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("digest") or "").strip()


def _first(*values: str | None) -> str:
    """First non-empty, stripped value (explicit > env > bundle)."""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def resolve_fields(
    bundle: Mapping,
    *,
    tag: str | None = None,
    promotion_audit_id: str | None = None,
    overrides: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve the six lock fields from a candidate bundle + deploy inputs.

    Precedence per field: explicit ``overrides[KEY]`` > ``env[KEY]`` >
    bundle-derived. ``tag`` is sugar for ``overrides['OMNISIGHT_DEPLOYED_TAG']``
    and ``promotion_audit_id`` for ``overrides['OMNISIGHT_PROMOTION_AUDIT_ID']``
    (default: the bundle id). Returned values are stripped but NOT yet
    validated — call :func:`validate_fields` before writing.
    """
    overrides = dict(overrides or {})
    env = env if env is not None else os.environ
    if tag is not None:
        overrides.setdefault("OMNISIGHT_DEPLOYED_TAG", tag)
    if promotion_audit_id is not None:
        overrides.setdefault("OMNISIGHT_PROMOTION_AUDIT_ID", promotion_audit_id)

    bundle_id = str(bundle.get("bundle_id") or "").strip()
    derived = {
        "OMNISIGHT_BUILD_GIT_SHA": str(bundle.get("git_sha") or "").strip(),
        "OMNISIGHT_BUILD_GIT_REF": str(bundle.get("git_ref") or "").strip(),
        "OMNISIGHT_DEPLOYED_TAG": "",
        "OMNISIGHT_DEPLOYED_DIGEST_BACKEND": _bundle_digest(bundle, "backend"),
        "OMNISIGHT_DEPLOYED_DIGEST_FRONTEND": _bundle_digest(bundle, "frontend"),
        # The promotion audit id falls back to the candidate bundle id when the
        # caller does not pass the real promotion-audit row id.
        "OMNISIGHT_PROMOTION_AUDIT_ID": bundle_id,
    }
    return {
        key: _first(overrides.get(key), env.get(key), derived[key])
        for key in LOCK_FIELDS
    }


def validate_fields(fields: Mapping[str, str]) -> dict[str, str]:
    """Return the fields if complete + well-shaped, else raise.

    Fail-closed: every one of the six fields must be present and non-empty,
    and the two image digests must match ``sha256:<64 hex>`` — the same shape
    the reader's overlay treats as a valid identity.
    """
    missing = [key for key in LOCK_FIELDS if not str(fields.get(key, "")).strip()]
    if missing:
        raise OverlayLockError(
            "deploy-overlay lock incomplete; missing/empty: " + ", ".join(missing)
        )
    bad = [
        key
        for key in _DIGEST_KEYS
        if not _DIGEST_RE.match(str(fields.get(key, "")).strip())
    ]
    if bad:
        raise OverlayLockError(
            "deploy-overlay lock has malformed digest(s): "
            + ", ".join(f"{key}={fields.get(key)!r}" for key in bad)
        )
    return {key: str(fields[key]).strip() for key in LOCK_FIELDS}


def render_lock(fields: Mapping[str, str]) -> str:
    """Render the env-file lock body (``KEY=value`` lines, in reader order)."""
    lines = [
        "# OmniSight RT-08 deploy-overlay lock — written by "
        "scripts/write_deploy_overlay_lock.py (OP-1606).",
        "# The deployed backend reads this ONCE at startup; serves it on "
        "/api/version and gates /readyz.",
        "# Do NOT edit by hand — it is regenerated from the deployed candidate "
        "on every deploy/promote.",
    ]
    lines += [f"{key}={fields[key]}" for key in LOCK_FIELDS]
    return "\n".join(lines) + "\n"


def write_lock(path: Path, fields: Mapping[str, str]) -> Path:
    """Validate ``fields`` and atomically write the lock to ``path``.

    Writes to a temp file in the destination directory and ``os.replace``s it
    into place so a reader never sees a torn or half-written lock.
    """
    valid = validate_fields(fields)
    body = render_lock(valid)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".deploy-overlay.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            os.fchmod(handle.fileno(), 0o644)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return path


def _load_bundle(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OverlayLockError(f"candidate bundle not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OverlayLockError(f"candidate bundle is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OverlayLockError(f"candidate bundle root must be an object: {path}")
    return data


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="write_deploy_overlay_lock",
        description="Write the RT-08 deploy-overlay env lock from a candidate bundle.",
    )
    parser.add_argument(
        "--bundle",
        required=True,
        type=Path,
        help="Candidate bundle.json (git_sha, git_ref, images.{backend,frontend}.digest).",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Deployed image tag -> OMNISIGHT_DEPLOYED_TAG (required unless set in env).",
    )
    parser.add_argument(
        "--promotion-audit-id",
        default=None,
        help="Promotion audit id -> OMNISIGHT_PROMOTION_AUDIT_ID (default: bundle_id).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(os.environ.get("OMNISIGHT_DEPLOY_OVERLAY_LOCK", DEFAULT_LOCK_PATH)),
        help="Lock output path (default: $OMNISIGHT_DEPLOY_OVERLAY_LOCK or "
        f"{DEFAULT_LOCK_PATH}).",
    )
    # Per-field explicit overrides (highest precedence) — for promote paths that
    # carry an identity field outside the bundle.
    parser.add_argument("--build-git-sha", default=None)
    parser.add_argument("--build-git-ref", default=None)
    parser.add_argument("--digest-backend", default=None)
    parser.add_argument("--digest-frontend", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    overrides = {
        "OMNISIGHT_BUILD_GIT_SHA": args.build_git_sha,
        "OMNISIGHT_BUILD_GIT_REF": args.build_git_ref,
        "OMNISIGHT_DEPLOYED_DIGEST_BACKEND": args.digest_backend,
        "OMNISIGHT_DEPLOYED_DIGEST_FRONTEND": args.digest_frontend,
    }
    overrides = {key: val for key, val in overrides.items() if val}
    try:
        bundle = _load_bundle(args.bundle)
        fields = resolve_fields(
            bundle,
            tag=args.tag,
            promotion_audit_id=args.promotion_audit_id,
            overrides=overrides,
        )
        out = write_lock(args.out, fields)
    except OverlayLockError as exc:
        print(f"write_deploy_overlay_lock: {exc}", file=sys.stderr)
        return 1
    print(
        f"wrote deploy-overlay lock {out} "
        f"(tag={fields['OMNISIGHT_DEPLOYED_TAG']} "
        f"audit={fields['OMNISIGHT_PROMOTION_AUDIT_ID']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
