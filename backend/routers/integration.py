"""System integration settings — view, update, and test external connections."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["integration"])


def _mask(value: str) -> str:
    """Mask sensitive values for API response."""
    if not value or len(value) < 8:
        return "***" if value else ""
    return value[:3] + "*" * min(len(value) - 6, 20) + value[-3:]


def _get_masked_credentials() -> list[dict]:
    """Get credential registry with tokens masked for API response."""
    try:
        from backend.git_credentials import get_credential_registry
        registry = get_credential_registry()
        return [
            {
                "id": r.get("id", ""),
                "url": r.get("url", ""),
                "platform": r.get("platform", "unknown"),
                "token": _mask(r.get("token", "")),
                "ssh_key": r.get("ssh_key", ""),
                "ssh_host": r.get("ssh_host", ""),
                "ssh_port": r.get("ssh_port", 0),
                "project": r.get("project", ""),
                "has_secret": bool(r.get("webhook_secret", "")),
            }
            for r in registry
        ]
    except Exception:
        return []


@router.get("/settings")
async def get_settings():
    """Return all integration settings grouped by category. Tokens are masked."""
    return {
        "llm": {
            "provider": settings.llm_provider,
            "model": settings.get_model_name(),
            "temperature": settings.llm_temperature,
            "fallback_chain": settings.llm_fallback_chain,
            "anthropic_api_key": _mask(settings.anthropic_api_key),
            "google_api_key": _mask(settings.google_api_key),
            "openai_api_key": _mask(settings.openai_api_key),
            "xai_api_key": _mask(settings.xai_api_key),
            "groq_api_key": _mask(settings.groq_api_key),
            "deepseek_api_key": _mask(settings.deepseek_api_key),
            "together_api_key": _mask(settings.together_api_key),
            "openrouter_api_key": _mask(settings.openrouter_api_key),
            "ollama_base_url": settings.ollama_base_url,
        },
        "git": {
            "ssh_key_path": settings.git_ssh_key_path,
            "github_token": _mask(settings.github_token),
            "gitlab_token": _mask(settings.gitlab_token),
            "gitlab_url": settings.gitlab_url,
            "credentials": _get_masked_credentials(),
        },
        "gerrit": {
            "enabled": settings.gerrit_enabled,
            "url": settings.gerrit_url,
            "ssh_host": settings.gerrit_ssh_host,
            "ssh_port": settings.gerrit_ssh_port,
            "project": settings.gerrit_project,
            "replication_targets": settings.gerrit_replication_targets,
        },
        "jira": {
            "url": settings.notification_jira_url,
            "token": _mask(settings.notification_jira_token),
            "project": settings.notification_jira_project,
        },
        "slack": {
            "webhook": _mask(settings.notification_slack_webhook),
            "mention": settings.notification_slack_mention,
        },
        "pagerduty": {
            "key": _mask(settings.notification_pagerduty_key),
        },
        "webhooks": {
            "github_secret": "configured" if settings.github_webhook_secret else "",
            "gitlab_secret": "configured" if settings.gitlab_webhook_secret else "",
            "jira_secret": "configured" if settings.jira_webhook_secret else "",
        },
        "ci": {
            "github_actions_enabled": settings.ci_github_actions_enabled,
            "jenkins_enabled": settings.ci_jenkins_enabled,
            "jenkins_url": settings.ci_jenkins_url,
            "gitlab_ci_enabled": settings.ci_gitlab_enabled,
        },
        "docker": {
            "enabled": settings.docker_enabled,
            "memory_limit": settings.docker_memory_limit,
            "cpu_limit": settings.docker_cpu_limit,
        },
    }


class SettingsUpdate(BaseModel):
    """Flat key-value update — keys match config.py field names."""
    updates: dict[str, str | int | float | bool]


# Whitelist of fields safe to update at runtime
_UPDATABLE_FIELDS = frozenset({
    "llm_provider", "llm_model", "llm_temperature", "llm_fallback_chain",
    "anthropic_api_key", "google_api_key", "openai_api_key", "xai_api_key",
    "groq_api_key", "deepseek_api_key", "together_api_key", "openrouter_api_key",
    "ollama_base_url",
    "github_token", "gitlab_token", "gitlab_url", "git_ssh_key_path",
    "gerrit_enabled", "gerrit_url", "gerrit_ssh_host", "gerrit_ssh_port",
    "gerrit_project", "gerrit_replication_targets",
    "notification_jira_url", "notification_jira_token", "notification_jira_project",
    "notification_slack_webhook", "notification_slack_mention",
    "notification_pagerduty_key",
    "github_webhook_secret", "gitlab_webhook_secret", "jira_webhook_secret",
    "ci_github_actions_enabled", "ci_jenkins_enabled", "ci_jenkins_url",
    "ci_jenkins_user", "ci_jenkins_api_token", "ci_gitlab_enabled",
    "docker_enabled", "docker_memory_limit", "docker_cpu_limit",
})


@router.put("/settings")
async def update_settings(body: SettingsUpdate):
    """Update integration settings at runtime (not persisted to .env)."""
    applied = {}
    rejected = {}
    for key, value in body.updates.items():
        if key not in _UPDATABLE_FIELDS:
            rejected[key] = "not updatable"
            continue
        if not hasattr(settings, key):
            rejected[key] = "unknown field"
            continue
        setattr(settings, key, value)
        applied[key] = True

    # Clear LLM cache if provider/model/key changed
    llm_related = {"llm_", "anthropic_", "google_", "openai_", "xai_", "groq_", "deepseek_", "together_", "openrouter_", "ollama_"}
    if any(any(k.startswith(p) for p in llm_related) for k in applied):
        try:
            from backend.agents.llm import _cache
            _cache.clear()
        except Exception:
            pass
        # Emit SSE event so Orchestrator panel can sync
        try:
            from backend.events import emit_invoke
            emit_invoke("provider_switch", f"{settings.llm_provider}/{settings.get_model_name()}")
        except Exception:
            pass

    logger.info("Settings updated: %s", list(applied.keys()))
    return {
        "status": "updated",
        "applied": list(applied.keys()),
        "rejected": rejected,
        "note": "Changes are runtime-only and will reset on restart.",
    }


@router.post("/test/{integration}")
async def test_integration(integration: str):
    """Test connectivity for an external integration."""
    tester = _TESTERS.get(integration)
    if not tester:
        raise HTTPException(400, f"Unknown integration: {integration}. Valid: {sorted(_TESTERS.keys())}")
    try:
        return await asyncio.wait_for(tester(), timeout=15)
    except asyncio.TimeoutError:
        return {"status": "error", "message": "Connection timed out (15s)"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ── Test functions ──

async def _test_ssh() -> dict:
    key_path = Path(settings.git_ssh_key_path).expanduser()
    if not key_path.exists():
        return {"status": "error", "message": f"SSH key not found: {key_path}"}
    if not os.access(str(key_path), os.R_OK):
        return {"status": "error", "message": f"SSH key not readable: {key_path}"}
    return {"status": "ok", "path": str(key_path)}


async def _test_gerrit() -> dict:
    if not settings.gerrit_enabled:
        return {"status": "not_configured", "message": "Gerrit is disabled"}
    if not settings.gerrit_ssh_host:
        return {"status": "not_configured", "message": "Gerrit SSH host not set"}
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-p", str(settings.gerrit_ssh_port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        f"{settings.gerrit_ssh_host}",
        "gerrit", "version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        return {"status": "ok", "version": stdout.decode().strip()}
    return {"status": "error", "message": (stderr or stdout).decode().strip()[:200]}


async def _test_github() -> dict:
    if not settings.github_token:
        return {"status": "not_configured", "message": "GitHub token not set"}
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-H", f"Authorization: token {settings.github_token}",
        "https://api.github.com/user",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        import json
        data = json.loads(stdout)
        if "login" in data:
            return {"status": "ok", "user": data["login"]}
        return {"status": "error", "message": data.get("message", "Unknown error")}
    except Exception:
        return {"status": "error", "message": "Invalid response from GitHub API"}


async def _test_gitlab() -> dict:
    if not settings.gitlab_token:
        return {"status": "not_configured", "message": "GitLab token not set"}
    base = settings.gitlab_url or "https://gitlab.com"
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-H", f"PRIVATE-TOKEN: {settings.gitlab_token}",
        f"{base}/api/v4/user",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        import json
        data = json.loads(stdout)
        if "username" in data:
            return {"status": "ok", "user": data["username"]}
        return {"status": "error", "message": data.get("message", "Unknown error")}
    except Exception:
        return {"status": "error", "message": "Invalid response from GitLab API"}


async def _test_jira() -> dict:
    if not settings.notification_jira_url or not settings.notification_jira_token:
        return {"status": "not_configured", "message": "Jira URL or token not set"}
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s",
        "-H", f"Authorization: Bearer {settings.notification_jira_token}",
        f"{settings.notification_jira_url}/rest/api/2/myself",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        import json
        data = json.loads(stdout)
        if "displayName" in data:
            return {"status": "ok", "user": data["displayName"]}
        return {"status": "error", "message": data.get("message", str(data)[:100])}
    except Exception:
        return {"status": "error", "message": "Invalid response from Jira"}


async def _test_slack() -> dict:
    if not settings.notification_slack_webhook:
        return {"status": "not_configured", "message": "Slack webhook not set"}
    import json
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST", settings.notification_slack_webhook,
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"text": "[TEST] OmniSight integration test — connection OK"}),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    response = stdout.decode().strip()
    if response == "ok":
        return {"status": "ok", "message": "Test message sent to Slack (a real message was posted to the channel)"}
    return {"status": "error", "message": f"Slack returned: {response[:100]}"}


_TESTERS = {
    "ssh": _test_ssh,
    "gerrit": _test_gerrit,
    "github": _test_github,
    "gitlab": _test_gitlab,
    "jira": _test_jira,
    "slack": _test_slack,
}


# ── Vendor SDK CRUD ──

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PLATFORMS_DIR = _PROJECT_ROOT / "configs" / "platforms"


class VendorSDKCreate(BaseModel):
    platform: str  # Profile name (filename without .yaml)
    label: str
    vendor_id: str
    soc_model: str = ""
    sdk_version: str = ""
    toolchain: str = "aarch64-linux-gnu-gcc"
    cross_prefix: str = "aarch64-linux-gnu-"
    kernel_arch: str = "arm64"
    arch_flags: str = "-march=armv8-a"
    qemu: str = "qemu-aarch64-static"
    sysroot_path: str = ""
    cmake_toolchain_file: str = ""
    npu_enabled: bool = False
    deploy_method: str = "ssh"
    deploy_target_ip: str = ""


@router.post("/vendor/sdks")
async def create_vendor_sdk(body: VendorSDKCreate):
    """Create a new vendor SDK platform profile."""
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', body.platform):
        raise HTTPException(400, "Platform name must be alphanumeric/dash/underscore")
    profile_path = _PLATFORMS_DIR / f"{body.platform}.yaml"
    if profile_path.exists():
        raise HTTPException(409, f"Platform profile already exists: {body.platform}")

    import yaml
    data = {
        "platform": body.platform,
        "label": body.label,
        "vendor_id": body.vendor_id,
        "soc_model": body.soc_model,
        "sdk_version": body.sdk_version,
        "toolchain": body.toolchain,
        "cross_prefix": body.cross_prefix,
        "kernel_arch": body.kernel_arch,
        "arch_flags": body.arch_flags,
        "qemu": body.qemu,
        "sysroot_path": body.sysroot_path,
        "cmake_toolchain_file": body.cmake_toolchain_file,
        "npu_enabled": body.npu_enabled,
        "deploy_method": body.deploy_method,
        "deploy_target_ip": body.deploy_target_ip,
        "docker_packages": [
            f"gcc-{body.cross_prefix.rstrip('-')}",
            f"g++-{body.cross_prefix.rstrip('-')}",
            f"binutils-{body.cross_prefix.rstrip('-')}",
        ],
    }
    profile_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))
    logger.info("Created vendor SDK profile: %s", body.platform)
    return {"status": "created", "platform": body.platform, "path": str(profile_path)}


@router.delete("/vendor/sdks/{platform}")
async def delete_vendor_sdk(platform: str):
    """Delete a vendor SDK platform profile."""
    if not re.match(r'^[a-zA-Z0-9_-]+$', platform):
        raise HTTPException(400, "Invalid platform name (alphanumeric, hyphens, underscores only)")
    profile_path = _PLATFORMS_DIR / f"{platform}.yaml"
    if not profile_path.exists():
        raise HTTPException(404, f"Platform profile not found: {platform}")
    # Prevent deleting built-in profiles
    builtin = {"aarch64", "armv7", "riscv64"}
    if platform in builtin:
        raise HTTPException(403, f"Cannot delete built-in platform: {platform}")
    profile_path.unlink()
    logger.info("Deleted vendor SDK profile: %s", platform)
    return {"status": "deleted", "platform": platform}
