"""Release packaging — version resolution, manifest generation, and upload.

Provides:
  - resolve_version(): get version from git tags, VERSION file, or package.json
  - generate_release_manifest(): JSON manifest listing all artifacts for a release
  - create_release_bundle(): tar.gz bundle with manifest
  - upload_to_github(): create GitHub Release and upload assets
  - upload_to_gitlab(): create GitLab Release and upload assets
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tarfile
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def resolve_version() -> str:
    """Resolve the current project version from multiple sources.

    Priority: git describe --tags > VERSION file > package.json > fallback
    """
    # 1. git describe --tags
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "describe", "--tags", "--always",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=_PROJECT_ROOT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        tag = stdout.decode().strip()
        if tag:
            return tag
    except Exception:
        pass

    # 2. VERSION file
    version_file = _PROJECT_ROOT / "VERSION"
    if version_file.exists():
        v = version_file.read_text().strip()
        if v:
            return v

    # 3. package.json
    try:
        pkg = json.loads((_PROJECT_ROOT / "package.json").read_text())
        v = pkg.get("version", "")
        if v and v != "0.0.0":
            return v
    except Exception:
        pass

    # 4. Fallback
    return "0.1.0-dev"


async def generate_release_manifest(
    version: str = "",
    artifact_ids: list[str] | None = None,
) -> dict:
    """Generate a JSON manifest for a release.

    If artifact_ids is None, includes all artifacts. Otherwise filters.
    """
    from backend import db

    if not version:
        version = await resolve_version()

    all_artifacts = await db.list_artifacts(limit=200)
    if artifact_ids:
        artifacts = [a for a in all_artifacts if a["id"] in set(artifact_ids)]
    else:
        artifacts = all_artifacts

    manifest = {
        "name": "OmniSight Productizer",
        "version": version,
        "created_at": datetime.now().isoformat(),
        "artifact_count": len(artifacts),
        "artifacts": [
            {
                "id": a["id"],
                "name": a["name"],
                "type": a.get("type", "binary"),
                "size": a.get("size", 0),
                "checksum_sha256": a.get("checksum", ""),
                "version": a.get("version", ""),
                "download_url": f"/api/v1/artifacts/{a['id']}/download",
            }
            for a in artifacts
        ],
    }
    return manifest


async def create_release_bundle(
    version: str = "",
    artifact_ids: list[str] | None = None,
) -> dict:
    """Create a release tar.gz bundle with manifest.

    Returns artifact metadata dict for the bundle itself.
    """
    from backend import db
    from backend.routers.artifacts import get_artifacts_root

    if not version:
        version = await resolve_version()

    # Generate manifest
    manifest = await generate_release_manifest(version, artifact_ids)

    # Prepare bundle
    artifacts_root = get_artifacts_root()
    releases_dir = artifacts_root / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)

    safe_version = "".join(c if c.isalnum() or c in ".-_" else "_" for c in version)
    bundle_name = f"omnisight-release-{safe_version}.tar.gz"
    bundle_path = releases_dir / bundle_name
    manifest_path = releases_dir / f"manifest-{safe_version}.json"

    # Write manifest
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    # Create tar.gz with all artifact files + manifest
    with tarfile.open(bundle_path, "w:gz") as tar:
        # Add manifest
        tar.add(manifest_path, arcname="manifest.json")

        # Add artifact files
        for art_meta in manifest["artifacts"]:
            art = await db.get_artifact(art_meta["id"])
            if art and art.get("file_path"):
                fpath = Path(art["file_path"])
                if fpath.exists():
                    tar.add(fpath, arcname=art["name"])

    # Compute checksum
    sha = hashlib.sha256()
    with open(bundle_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)

    bundle_id = f"art-{uuid.uuid4().hex[:12]}"
    bundle_data = {
        "id": bundle_id,
        "task_id": "",
        "agent_id": "release-system",
        "name": bundle_name,
        "type": "archive",
        "file_path": str(bundle_path),
        "size": bundle_path.stat().st_size,
        "created_at": datetime.now().isoformat(),
        "version": version,
        "checksum": sha.hexdigest(),
    }

    await db.insert_artifact(bundle_data)

    # Emit SSE
    try:
        from backend.events import bus
        bus.publish("artifact_created", {
            "id": bundle_id, "name": bundle_name, "type": "archive",
            "task_id": "", "agent_id": "release-system",
            "size": bundle_data["size"],
        })
    except Exception:
        pass

    logger.info("Release bundle created: %s (%d bytes, %d artifacts)",
                bundle_name, bundle_data["size"], manifest["artifact_count"])

    # Clean up standalone manifest (it's inside the tar.gz now)
    manifest_path.unlink(missing_ok=True)

    return {
        **bundle_data,
        "manifest": manifest,
        "download_url": f"/api/v1/artifacts/{bundle_id}/download",
    }


async def upload_to_github(bundle_path: str, version: str, manifest: dict) -> dict:
    """Upload release bundle to GitHub Releases.

    Creates a release (draft by default) and uploads the bundle as an asset.
    """
    from backend.config import settings

    if not settings.github_token or not settings.github_repo:
        return {"status": "skipped", "reason": "github_token or github_repo not configured"}

    repo = settings.github_repo
    token = settings.github_token
    tag = f"v{version}" if not version.startswith("v") else version
    draft = settings.release_draft

    try:
        # Create release
        proc = await asyncio.create_subprocess_exec(
            "gh", "release", "create", tag,
            "--repo", repo,
            "--title", f"OmniSight {version}",
            "--notes", f"Release {version} — {manifest.get('artifact_count', 0)} artifact(s)",
            *(["--draft"] if draft else []),
            bundle_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**__import__("os").environ, "GH_TOKEN": token},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            url = stdout.decode().strip()
            logger.info("GitHub release created: %s", url)
            return {"status": "uploaded", "url": url, "tag": tag}
        else:
            error = stderr.decode()[:200]
            logger.warning("GitHub release failed: %s", error)
            return {"status": "error", "error": error}
    except asyncio.TimeoutError:
        return {"status": "error", "error": "GitHub release upload timed out"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


async def upload_to_gitlab(bundle_path: str, version: str, manifest: dict) -> dict:
    """Upload release bundle to GitLab Releases."""
    from backend.config import settings

    if not settings.gitlab_token or not settings.gitlab_project_id:
        return {"status": "skipped", "reason": "gitlab_token or gitlab_project_id not configured"}

    project = settings.gitlab_project_id
    token = settings.gitlab_token
    base_url = settings.gitlab_url or "https://gitlab.com"
    tag = f"v{version}" if not version.startswith("v") else version

    try:
        import urllib.parse
        encoded_project = urllib.parse.quote(project, safe="")

        # Create tag (may already exist)
        await asyncio.create_subprocess_exec(
            "curl", "-s", "-X", "POST",
            f"{base_url}/api/v4/projects/{encoded_project}/repository/tags",
            "-H", f"PRIVATE-TOKEN: {token}",
            "-d", f"tag_name={tag}&ref=main",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

        # Create release
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-X", "POST",
            f"{base_url}/api/v4/projects/{encoded_project}/releases",
            "-H", f"PRIVATE-TOKEN: {token}",
            "-H", "Content-Type: application/json",
            "-d", json.dumps({
                "tag_name": tag,
                "name": f"OmniSight {version}",
                "description": f"Release {version} — {manifest.get('artifact_count', 0)} artifact(s)",
            }),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        result = json.loads(stdout.decode())
        if "tag_name" in result:
            logger.info("GitLab release created: %s", result.get("_links", {}).get("self", ""))
            return {"status": "uploaded", "tag": tag, "url": result.get("_links", {}).get("self", "")}
        return {"status": "error", "error": result.get("message", str(result))[:200]}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}
