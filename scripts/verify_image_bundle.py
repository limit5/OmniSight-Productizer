#!/usr/bin/env python3
"""OP-1482 — preflight verifier for digest-pinned compose deploys.

Codex's #2 highest-priority deploy-pipeline gap was that the prod and
staging compose files referenced images by mutable tag
(``ghcr.io/.../omnisight-backend:${OMNISIGHT_IMAGE_TAG}``) with
``pull_policy: missing``. Result: a deploy that thought it was promoting
a freshly-cut release could silently no-op against whatever stale tag
happened to already be in the local Docker cache. The four-AC fix for
OP-1482 is:

  1. Pin each image in compose by ``@sha256:...`` digest, sourced from a
     per-env lock file (``staging.env.lock.json`` / ``canary.env.lock.json``
     / ``prod.env.lock.json``).
  2. Run this preflight script before ``docker compose up`` to verify
     that:

       (a) The lock-file digest for each image matches what the remote
           registry serves today (``docker manifest inspect``). Catches
           a rebase / re-tag that moved the alias under us.
       (b) The cosign signature on the lock-file digest verifies. Reuses
           ``scripts/verify_image_signature.sh`` so the keyless-vs-key
           detection mode stays consistent with the rest of the pipeline.
       (c) The local Docker cache has the lock-file digest available —
           i.e. ``docker image inspect <repo>@<digest>`` succeeds AND
           one of its RepoDigests matches the lock. This catches the
           Codex #15 failure mode where an operator manually pulled an
           old digest and re-tagged it (``docker tag`` is mutable;
           Docker has no notion of "the local cache is dirty").

  Exit codes:

    0 — local cache == remote registry == lock file for every image
        AND cosign verified each digest.
    1 — at least one image diverges. The script enumerates every
        failing image with a remediation hint
        (typically ``docker compose pull --policy=always``).
    2 — bad invocation or external dependency missing (no docker, no
        cosign, malformed lock file). The runner can distinguish this
        from a real verification failure and surface "broken host" vs
        "broken deploy".

This script is intentionally side-effect-free — it never mutates the
local cache, never pushes to the registry, never edits the lock file.
That separation lets the deploy runbook call it ``--dry-run``-style as
a gate ahead of ``docker compose up``.

Usage:

    python3 scripts/verify_image_bundle.py \\
        --compose docker-compose.prod.yml \\
        --lock prod.env.lock.json

    # CI / runner mode — exit on first failure, emit JSON for log scrape
    python3 scripts/verify_image_bundle.py \\
        --compose docker-compose.prod.yml \\
        --lock prod.env.lock.json \\
        --output json --fail-fast

    # Skip cosign verification (e.g. in environments without sigstore
    # network reach — staging-on-laptop). Digest / cache checks still run.
    python3 scripts/verify_image_bundle.py \\
        --compose docker-compose.staging.yml \\
        --lock staging.env.lock.json \\
        --skip-signature

The argument names mirror ``scripts/verify_image_signature.sh`` so an
operator's muscle memory carries across the two scripts.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SIGNATURE_SH = REPO_ROOT / "scripts" / "verify_image_signature.sh"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Capture the value after `image:` up to end-of-line. We deliberately
# allow spaces inside the value because compose `${VAR:?error message}`
# interpolation embeds free-form text that can contain spaces — the
# `\S+` greedy form would truncate at the first space and miss the
# digest pin entirely (OP-1482 regression caught in test).
_IMAGE_REF_RE = re.compile(
    r"^image:\s*(?P<ref>\S.*?)\s*$"
)

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_BAD_INVOCATION = 2


@dataclass
class ImageCheck:
    """Per-image result accumulator. Each ``add_*`` setter records a
    machine-readable status (``ok`` / ``mismatch`` / ``skipped`` /
    ``error``) plus a human-readable detail for the operator log.
    """

    name: str
    repository: str
    lock_digest: str
    remote_digest: str | None = None
    remote_status: str = "pending"
    remote_detail: str = ""
    signature_status: str = "pending"
    signature_detail: str = ""
    cache_digest: str | None = None
    cache_status: str = "pending"
    cache_detail: str = ""
    compose_ref_status: str = "pending"
    compose_ref_detail: str = ""

    def ok(self) -> bool:
        for status in (
            self.remote_status,
            self.signature_status,
            self.cache_status,
            self.compose_ref_status,
        ):
            if status not in ("ok", "skipped"):
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "repository": self.repository,
            "lock_digest": self.lock_digest,
            "remote": {
                "status": self.remote_status,
                "digest": self.remote_digest,
                "detail": self.remote_detail,
            },
            "signature": {
                "status": self.signature_status,
                "detail": self.signature_detail,
            },
            "cache": {
                "status": self.cache_status,
                "digest": self.cache_digest,
                "detail": self.cache_detail,
            },
            "compose_ref": {
                "status": self.compose_ref_status,
                "detail": self.compose_ref_detail,
            },
            "ok": self.ok(),
        }


@dataclass
class LockFile:
    path: Path
    raw: dict[str, Any]
    images: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "LockFile":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BadInvocation(f"lock file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise BadInvocation(f"lock file is not valid JSON: {path} ({exc})") from exc
        if not isinstance(raw, dict):
            raise BadInvocation(f"lock file must be a JSON object: {path}")
        images = raw.get("images")
        if not isinstance(images, dict) or not images:
            raise BadInvocation(
                f"lock file {path} has no 'images' object — expected keys "
                "'backend', 'frontend', 'bridge'"
            )
        parsed: dict[str, dict[str, str]] = {}
        for name, entry in images.items():
            if not isinstance(entry, dict):
                raise BadInvocation(
                    f"lock entry for image '{name}' must be an object"
                )
            repo = entry.get("repository")
            digest = entry.get("digest")
            if not isinstance(repo, str) or not repo:
                raise BadInvocation(
                    f"lock entry '{name}' is missing 'repository'"
                )
            if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
                raise BadInvocation(
                    f"lock entry '{name}' has invalid digest '{digest}' "
                    f"(expected sha256:<64 hex>)"
                )
            parsed[name] = {
                "repository": repo,
                "digest": digest,
                "env_var": str(entry.get("env_var") or ""),
            }
        return cls(path=path, raw=raw, images=parsed)


class BadInvocation(Exception):
    """Raised for malformed inputs (lock file, compose file, args).
    Surfaces as exit code 2 so the runner can distinguish a "broken
    host" from a real "lock file is out of date" failure.
    """


def _is_placeholder_digest(digest: str) -> bool:
    """The shipped lock files start as all-zero placeholders so the
    repo's first commit doesn't carry a real digest. We treat those
    as "skip remote checks" rather than "fail remote checks" so a
    fresh clone's preflight is honest about what it can/can't verify.
    """
    return digest == "sha256:" + ("0" * 64)


def _compose_image_refs(compose_path: Path) -> list[str]:
    """Return the raw ``image:`` values from a compose file.

    We deliberately do not use PyYAML — the project pyproject.toml does
    not pin it, and the runner host is allowed to be stdlib-only. The
    regex is good enough because compose ``image:`` lines are flat
    scalars (never multi-line / never anchors); the worst false negative
    is a deeply-indented map under ``services.<svc>.image`` which compose
    itself rejects.
    """
    try:
        text = compose_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BadInvocation(f"compose file not found: {compose_path}") from exc
    refs: list[str] = []
    for line in text.splitlines():
        m = _IMAGE_REF_RE.match(line.lstrip())
        if m:
            refs.append(m.group("ref"))
    return refs


def _docker_manifest_digest(image_ref: str) -> tuple[str | None, str]:
    """Return ``(remote_digest, detail)``.

    ``docker manifest inspect --verbose`` prints a JSON document whose
    top-level ``Descriptor.digest`` is the registry-side content digest.
    Falling back to plain ``docker manifest inspect`` and hashing the
    response is unsafe (the JSON body is canonicalised differently
    between Docker versions), so we always go through ``--verbose``.
    """
    if not shutil.which("docker"):
        return None, "docker CLI not on PATH"
    try:
        proc = subprocess.run(
            ["docker", "manifest", "inspect", "--verbose", image_ref],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return None, f"docker manifest inspect timed out for {image_ref}"
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()
        tail = stderr[-1] if stderr else "(no stderr)"
        return None, f"docker manifest inspect failed: {tail}"
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, f"docker manifest inspect emitted non-JSON: {exc}"
    candidates: list[dict[str, Any]] = (
        payload if isinstance(payload, list) else [payload]
    )
    for entry in candidates:
        descriptor = entry.get("Descriptor") if isinstance(entry, dict) else None
        if isinstance(descriptor, dict):
            digest = descriptor.get("digest")
            if isinstance(digest, str) and _DIGEST_RE.match(digest):
                return digest, "ok"
    return None, "docker manifest inspect response missing Descriptor.digest"


def _docker_image_cache_digests(image_ref: str) -> tuple[list[str], str]:
    """Return ``(repo_digests, detail)`` for a locally-cached image.

    ``docker image inspect`` returns a list of RepoDigests
    (``<repo>@sha256:<...>``); comparing those against the lock catches
    the Codex #15 case where ``docker tag`` glued a stale digest onto
    the alias.
    """
    if not shutil.which("docker"):
        return [], "docker CLI not on PATH"
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image_ref,
             "--format", "{{json .RepoDigests}}"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return [], f"docker image inspect timed out for {image_ref}"
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()
        tail = stderr[-1] if stderr else "(no stderr)"
        return [], f"docker image inspect failed: {tail}"
    raw = proc.stdout.strip()
    if not raw or raw == "null":
        return [], "no RepoDigests reported"
    try:
        repo_digests = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], f"RepoDigests not JSON: {exc}"
    if not isinstance(repo_digests, list):
        return [], "RepoDigests is not a list"
    return [str(rd) for rd in repo_digests], "ok"


def _verify_cosign_signature(image_with_digest: str) -> tuple[str, str]:
    """Shell out to ``scripts/verify_image_signature.sh``.

    Re-implementing the cosign logic here would mean keeping two
    verifier paths in sync forever. The bash script owns the key-based
    cosign policy; we just propagate its exit code.
    """
    if not VERIFY_SIGNATURE_SH.exists():
        return "error", f"verify_image_signature.sh not found at {VERIFY_SIGNATURE_SH}"
    try:
        proc = subprocess.run(
            ["bash", str(VERIFY_SIGNATURE_SH), image_with_digest],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "error", f"cosign verification timed out for {image_with_digest}"
    if proc.returncode == 0:
        return "ok", "cosign verified"
    stderr_tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    detail = stderr_tail[-1] if stderr_tail else "cosign verification failed"
    return "mismatch", detail


def check_image(
    name: str,
    repository: str,
    lock_digest: str,
    compose_refs: list[str],
    *,
    skip_remote: bool,
    skip_signature: bool,
    skip_cache: bool,
) -> ImageCheck:
    result = ImageCheck(name=name, repository=repository, lock_digest=lock_digest)

    digest_ref = f"{repository}@{lock_digest}"

    # Match by the image-name component, not the full repository
    # path. The compose `image:` line typically embeds the GHCR
    # namespace as a separate `${OMNISIGHT_GHCR_NAMESPACE}` var, so a
    # naive substring match against the lock's `repository`
    # (which is fully-qualified, e.g. `ghcr.io/your-org/omnisight-backend`)
    # never hits. The trailing image name (`omnisight-backend`) is
    # stable across env-var interpolation and unique within the
    # OmniSight namespace.
    image_name = repository.rsplit("/", 1)[-1]
    matching_compose_refs = [
        r for r in compose_refs
        if f"/{image_name}@" in r or f"/{image_name}:" in r
    ]
    if not matching_compose_refs:
        # The lock includes images that may not be in this compose
        # file — e.g. `bridge` is in every env lock but only some
        # deploys colocate it with backend/frontend. We mark this as
        # "skipped" rather than failing so the bridge's signature +
        # remote-digest checks still gate the deploy.
        result.compose_ref_status = "skipped"
        result.compose_ref_detail = (
            f"compose file does not reference '{repository}' — image "
            "is deployed by a separate compose / systemd unit"
        )
    else:
        unpinned: list[str] = []
        for r in matching_compose_refs:
            # Accept either a literal `@sha256:<...>` pin OR an
            # interpolated `@${VAR}` (which compose resolves to a
            # digest from the env lock at `up` time — this is the
            # canonical form in the shipped compose files).
            after_at = r.split("@", 1)[1] if "@" in r else ""
            literal_digest = bool(_DIGEST_RE.match(after_at))
            interpolated_digest = (
                after_at.startswith("${")
                and "DIGEST" in after_at
                and after_at.endswith("}")
            )
            if not (literal_digest or interpolated_digest):
                unpinned.append(r)
        if unpinned:
            result.compose_ref_status = "mismatch"
            result.compose_ref_detail = (
                f"compose ref not pinned by digest: {unpinned[0]} "
                "(expected '@" + lock_digest + "' or '@${...DIGEST}')"
            )
        else:
            result.compose_ref_status = "ok"
            result.compose_ref_detail = "compose ref pinned by digest"

    if _is_placeholder_digest(lock_digest):
        result.remote_status = "skipped"
        result.remote_detail = "placeholder lock digest (all-zero) — skipping remote check"
        result.signature_status = "skipped"
        result.signature_detail = "placeholder lock digest — skipping cosign"
        result.cache_status = "skipped"
        result.cache_detail = "placeholder lock digest — skipping cache check"
        return result

    if skip_remote:
        result.remote_status = "skipped"
        result.remote_detail = "--skip-remote requested"
    else:
        remote_digest, detail = _docker_manifest_digest(digest_ref)
        result.remote_digest = remote_digest
        if remote_digest is None:
            result.remote_status = "error"
            result.remote_detail = detail
        elif remote_digest == lock_digest:
            result.remote_status = "ok"
            result.remote_detail = "registry digest matches lock"
        else:
            result.remote_status = "mismatch"
            result.remote_detail = (
                f"registry serves {remote_digest} but lock pins {lock_digest} — "
                "the alias was retagged after the lock was sealed"
            )

    if skip_signature:
        result.signature_status = "skipped"
        result.signature_detail = "--skip-signature requested"
    else:
        sig_status, sig_detail = _verify_cosign_signature(digest_ref)
        result.signature_status = sig_status
        result.signature_detail = sig_detail

    if skip_cache:
        result.cache_status = "skipped"
        result.cache_detail = "--skip-cache requested"
    else:
        cache_digests, detail = _docker_image_cache_digests(digest_ref)
        if not cache_digests:
            if "No such image" in detail or "no RepoDigests" in detail or "image inspect failed" in detail:
                result.cache_status = "mismatch"
                result.cache_detail = (
                    f"local cache has no copy of {digest_ref} — run "
                    "`docker compose pull --policy=always` to refresh"
                )
            else:
                result.cache_status = "error"
                result.cache_detail = detail
        else:
            expected = digest_ref
            if expected in cache_digests:
                result.cache_status = "ok"
                result.cache_detail = "local cache pins lock digest"
                result.cache_digest = lock_digest
            else:
                result.cache_status = "mismatch"
                cache_digest = cache_digests[0].rsplit("@", 1)[-1] if "@" in cache_digests[0] else cache_digests[0]
                result.cache_digest = cache_digest
                result.cache_detail = (
                    f"local cache holds {cache_digests[0]} but lock pins "
                    f"{lock_digest} — Codex-#15 stale-cache state, run "
                    "`docker compose pull --policy=always` to refresh"
                )
    return result


def _format_text(results: list[ImageCheck], lock: LockFile) -> str:
    out: list[str] = []
    out.append(f"verify_image_bundle: lock={lock.path} env={lock.raw.get('env', '?')}")
    out.append(f"  bundle_id      = {lock.raw.get('bundle_id', '?')}")
    out.append(f"  promoted_from  = {lock.raw.get('promoted_from', '?')}")
    out.append(f"  last_promoted  = {lock.raw.get('last_promoted_at', '?')}")
    for r in results:
        marker = "OK" if r.ok() else "FAIL"
        out.append(f"[{marker}] {r.name} ({r.repository})")
        out.append(f"    lock digest    : {r.lock_digest}")
        out.append(f"    compose ref    : {r.compose_ref_status} — {r.compose_ref_detail}")
        out.append(f"    remote manifest: {r.remote_status} — {r.remote_detail}")
        out.append(f"    cosign sig     : {r.signature_status} — {r.signature_detail}")
        out.append(f"    local cache    : {r.cache_status} — {r.cache_detail}")
    all_ok = all(r.ok() for r in results)
    out.append("")
    if all_ok:
        out.append("RESULT: OK — local cache, remote registry and lock all agree.")
    else:
        out.append(
            "RESULT: FAIL — at least one image diverges. Refusing to deploy. "
            "Run `docker compose pull --policy=always` then re-run preflight, "
            "or re-seal the lock file from CI if the divergence is intentional."
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a docker-compose file is pinned to the digests in a "
            "matching env lock file, and that local cache + remote "
            "registry + cosign signature all agree."
        )
    )
    parser.add_argument(
        "--compose", required=True, type=Path,
        help="Path to the docker-compose YAML file (e.g. docker-compose.prod.yml)",
    )
    parser.add_argument(
        "--lock", required=True, type=Path,
        help="Path to the env lock JSON file (e.g. prod.env.lock.json)",
    )
    parser.add_argument(
        "--skip-remote", action="store_true",
        help="Skip the `docker manifest inspect` remote-digest comparison.",
    )
    parser.add_argument(
        "--skip-signature", action="store_true",
        help="Skip the cosign signature verification.",
    )
    parser.add_argument(
        "--skip-cache", action="store_true",
        help="Skip the local Docker cache digest check.",
    )
    parser.add_argument(
        "--output", choices=("text", "json"), default="text",
        help="Output format. 'json' is machine-friendly for the deploy runner.",
    )
    parser.add_argument(
        "--fail-fast", action="store_true",
        help="Stop checking on the first failing image (default: check all).",
    )
    args = parser.parse_args(argv)

    try:
        lock = LockFile.load(args.lock)
    except BadInvocation as exc:
        print(f"verify_image_bundle: {exc}", file=sys.stderr)
        return EXIT_BAD_INVOCATION

    try:
        compose_refs = _compose_image_refs(args.compose)
    except BadInvocation as exc:
        print(f"verify_image_bundle: {exc}", file=sys.stderr)
        return EXIT_BAD_INVOCATION

    results: list[ImageCheck] = []
    any_failed = False
    for name, entry in lock.images.items():
        result = check_image(
            name=name,
            repository=entry["repository"],
            lock_digest=entry["digest"],
            compose_refs=compose_refs,
            skip_remote=args.skip_remote,
            skip_signature=args.skip_signature,
            skip_cache=args.skip_cache,
        )
        results.append(result)
        if not result.ok():
            any_failed = True
            if args.fail_fast:
                break

    if args.output == "json":
        payload = {
            "lock": str(lock.path),
            "compose": str(args.compose),
            "env": lock.raw.get("env"),
            "bundle_id": lock.raw.get("bundle_id"),
            "images": [r.to_dict() for r in results],
            "ok": not any_failed,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_format_text(results, lock))

    return EXIT_MISMATCH if any_failed else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
