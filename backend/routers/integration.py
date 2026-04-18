"""System integration settings — view, update, and test external connections."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend import auth as _au
from backend.config import settings
from backend.db_context import set_tenant_id

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


async def _get_tenant_secrets_summary(user) -> dict:
    """Fetch tenant-scoped secrets grouped by type for the settings view."""
    try:
        tid = getattr(user, "tenant_id", "t-default")
        set_tenant_id(tid)
        from backend import tenant_secrets as sec
        items = await sec.list_secrets()
        grouped: dict[str, list] = {}
        for s in items:
            grouped.setdefault(s["secret_type"], []).append({
                "id": s["id"],
                "key_name": s["key_name"],
                "fingerprint": s["fingerprint"],
                "metadata": s["metadata"],
                "updated_at": s["updated_at"],
            })
        return {"tenant_id": tid, "secrets": grouped}
    except Exception:
        return {"tenant_id": getattr(user, "tenant_id", "t-default"), "secrets": {}}


@router.get("/settings")
async def get_settings(_user=Depends(_au.require_operator)):
    """Return all integration settings grouped by category. Tokens are masked."""
    tenant_secrets = await _get_tenant_secrets_summary(_user)
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
        "tenant_secrets": tenant_secrets,
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
async def update_settings(body: SettingsUpdate, _user=Depends(_au.require_admin)):
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
async def test_integration(integration: str, _user=Depends(_au.require_admin)):
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


# ─── B14 Part A row 3: Git-forge token probe (Bootstrap Step 3.5) ──────
#
# Validates a *candidate* Git forge token supplied in the request body —
# does NOT mutate ``settings.github_token`` / ``settings.gitlab_token``.
# The Bootstrap wizard needs this because the operator is entering a
# brand-new token they haven't saved yet: reusing ``/system/test/github``
# would force a save-before-validate round-trip and leave a bad token
# persisted if validation fails.
#
# The existing ``/system/test/{integration}`` endpoint still exercises
# the currently-configured credential and is what Settings → Integration
# uses after the token has been written.

class GitForgeTokenTest(BaseModel):
    provider: str  # "github" | "gitlab" | "gerrit"
    token: str = ""
    url: str = ""  # optional — for GitLab self-hosted instances / Gerrit REST URL
    ssh_host: str = ""  # Gerrit only — `[user@]host` for the SSH probe
    ssh_port: int = 29418  # Gerrit only — SSH port (Gerrit default 29418)


# ─── B14 Part B row 217: masked read / PUT of the multi-instance token map ──
#
# Row 216 already lets the SAVE & APPLY flow serialise the instance list into
# ``settings.github_token_map`` / ``settings.gitlab_token_map`` via the generic
# ``PUT /system/settings`` endpoint — but the matching readback round-trips the
# raw JSON (token-bearing), which is unsafe to surface to the UI. This endpoint
# is the dedicated masked view: GET returns host-keyed entries with tokens
# reduced to the same ``_mask()`` shape used elsewhere; PUT accepts a full host
# → token list per-platform and writes the JSON form back to settings plus
# invalidates the credential cache so subsequent operations see the new map.


def _parse_token_map(raw: str) -> dict[str, str]:
    """Tolerant parse of a settings JSON map → {host: token}. Non-dict and
    invalid JSON both collapse to an empty map so callers never need to
    distinguish "unset" from "malformed"."""
    if not raw:
        return {}
    try:
        import json
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str) and k and v:
            out[k] = v
    return out


def _masked_instance_list(raw: str, platform: str) -> list[dict]:
    """Build the UI-friendly masked view of a {host: token} map. Stable
    ordering makes the endpoint round-trip predictable in tests."""
    entries = _parse_token_map(raw)
    return [
        {"platform": platform, "host": host, "token_masked": _mask(token)}
        for host, token in sorted(entries.items())
    ]


class TokenMapInstance(BaseModel):
    host: str
    token: str = ""  # blank on a PUT means "keep existing token for this host"


class TokenMapUpdate(BaseModel):
    github: list[TokenMapInstance] = []
    gitlab: list[TokenMapInstance] = []


@router.get("/settings/git/token-map")
async def get_git_token_map(_user=Depends(_au.require_operator)):
    """Return the configured per-host token maps with tokens masked.

    Shape::

        {
          "github": [{"platform": "github", "host": "...", "token_masked": "..."}],
          "gitlab": [...],
        }

    Empty platforms surface as empty lists — never ``null`` — so the UI
    can render "no additional instances configured" without branching on
    presence.
    """
    return {
        "github": _masked_instance_list(settings.github_token_map, "github"),
        "gitlab": _masked_instance_list(settings.gitlab_token_map, "gitlab"),
    }


@router.put("/settings/git/token-map")
async def update_git_token_map(
    body: TokenMapUpdate, _user=Depends(_au.require_admin),
):
    """Replace the per-host token maps.

    A blank ``token`` for a given host preserves the existing secret so the
    UI can round-trip the masked list without re-prompting every token.
    Removing a host just means omitting it from the PUT body — this
    endpoint is a replace, not a patch.

    Duplicate hosts in the payload are merged last-write-wins (the final
    entry in the list). Empty host strings are ignored.
    """
    import json

    def _merge(
        new: list[TokenMapInstance], existing_raw: str,
    ) -> tuple[str, int, int]:
        existing = _parse_token_map(existing_raw)
        merged: dict[str, str] = {}
        preserved = 0
        for inst in new:
            host = (inst.host or "").strip()
            if not host:
                continue
            token = inst.token
            if not token:
                # Blank token → keep whatever was already stored. If the
                # caller never supplied a token for a brand-new host the
                # entry is dropped rather than written as an empty string
                # (an empty token would silently break every credential
                # lookup for that host).
                prior = existing.get(host, "")
                if not prior:
                    continue
                token = prior
                preserved += 1
            merged[host] = token
        serialised = json.dumps(merged) if merged else ""
        return serialised, len(merged), preserved

    gh_json, gh_count, gh_preserved = _merge(body.github, settings.github_token_map)
    gl_json, gl_count, gl_preserved = _merge(body.gitlab, settings.gitlab_token_map)

    settings.github_token_map = gh_json
    settings.gitlab_token_map = gl_json

    # Bust the credential registry cache so the new map is observed by
    # `find_credential_for_url()` and friends without a process restart.
    try:
        from backend.git_credentials import clear_credential_cache
        clear_credential_cache()
    except Exception:  # pragma: no cover — defensive
        pass

    logger.info(
        "Token map updated: github=%d (kept %d) gitlab=%d (kept %d)",
        gh_count, gh_preserved, gl_count, gl_preserved,
    )
    return {
        "status": "updated",
        "github": _masked_instance_list(gh_json, "github"),
        "gitlab": _masked_instance_list(gl_json, "gitlab"),
        "note": "Changes are runtime-only and will reset on restart.",
    }


async def _probe_gerrit_ssh(ssh_host: str, ssh_port: int, url: str = "") -> dict:
    """Run ``ssh -p {port} {host} gerrit version`` against a *candidate*
    Gerrit SSH endpoint and return the parsed version. Never reads from
    or mutates ``settings``.

    B14 Part A row 5 — Bootstrap Step 3.5 Gerrit tab. Mirrors the
    GitHub / GitLab probes in spirit (non-mutating, timeout-bounded,
    structured ``{status, version|message}`` result) but uses SSH
    because Gerrit's canonical API over SSH (``gerrit version``) is the
    only probe that exercises the same transport the merger agent and
    the replication path will later use — a token-only HTTP probe would
    not catch SSH key / host-key mismatches.

    The host field may contain ``user@host`` (standard ssh syntax); the
    SSH key is pulled from the operator's running environment via the
    ssh client's default search path. ``StrictHostKeyChecking=accept-new``
    lets first-time probes succeed on a fresh host without a manual
    ``ssh-keyscan`` dance while still protecting against later host-key
    swaps (once the key is recorded).
    """
    host = (ssh_host or "").strip()
    if not host:
        return {"status": "error", "message": "SSH host is required"}
    try:
        port = int(ssh_port) if ssh_port is not None else 29418
    except (TypeError, ValueError):
        return {"status": "error", "message": "SSH port must be an integer"}
    if port < 1 or port > 65535:
        return {"status": "error", "message": "SSH port must be between 1 and 65535"}
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-p", str(port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        "-o", "BatchMode=yes",
        host,
        "gerrit", "version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        # Gerrit prints `gerrit version 3.9.2` on stdout.
        raw = stdout.decode(errors="replace").strip()
        m = re.search(r"gerrit version\s+(\S+)", raw, re.IGNORECASE)
        version = m.group(1) if m else raw or "unknown"
        result = {
            "status": "ok",
            "version": version,
            "ssh_host": host,
            "ssh_port": port,
        }
        if url:
            result["url"] = url.strip().rstrip("/")
        return result
    err = (stderr or stdout).decode(errors="replace").strip()
    return {"status": "error", "message": err[:300] or "SSH probe failed"}


async def _probe_gitlab_token(token: str, url: str) -> dict:
    """Call GitLab's ``GET /api/v4/version`` with the supplied token and
    return the instance ``version`` + ``revision``. Never reads from
    ``settings``. ``url`` is optional — falls back to ``gitlab.com``.

    B14 Part A row 4 — Bootstrap Step 3.5 GitLab tab. The probe is
    intentionally distinct from ``_test_gitlab`` (which exercises
    ``settings.gitlab_token`` + ``settings.gitlab_url``) so a candidate
    token can be validated before being written."""
    if not token:
        return {"status": "error", "message": "Token is required"}
    base = (url or "").strip().rstrip("/") or "https://gitlab.com"
    if not (base.startswith("http://") or base.startswith("https://")):
        return {
            "status": "error",
            "message": "URL must start with http:// or https://",
        }
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s",
        "-H", f"PRIVATE-TOKEN: {token}",
        "-H", "User-Agent: OmniSight-Bootstrap",
        f"{base}/api/v4/version",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    raw = stdout.decode(errors="replace")
    try:
        import json
        data = json.loads(raw)
    except Exception:
        return {
            "status": "error",
            "message": "Invalid response from GitLab API",
        }
    if isinstance(data, dict) and "version" in data:
        result = {
            "status": "ok",
            "version": data["version"],
            "url": base,
        }
        if data.get("revision"):
            result["revision"] = data["revision"]
        return result
    message = "GitLab returned an unexpected response"
    if isinstance(data, dict):
        message = data.get("message") or data.get("error") or message
    return {"status": "error", "message": message}


async def _probe_github_token(token: str) -> dict:
    """Call GitHub's ``GET /user`` with the supplied token and return
    the resolved login + display name. Never reads from ``settings``."""
    if not token:
        return {"status": "error", "message": "Token is required"}
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-D", "-",
        "-H", f"Authorization: token {token}",
        "-H", "User-Agent: OmniSight-Bootstrap",
        "https://api.github.com/user",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    raw = stdout.decode(errors="replace")
    # Split headers from body on the blank line (curl -D - prepends them).
    scopes = ""
    body_start = 0
    if "\r\n\r\n" in raw:
        head, _, rest = raw.partition("\r\n\r\n")
        # Follow any 100-continue / 3xx continuations if curl left extra
        # header blocks — take the last one as the response headers.
        while "\r\n\r\n" in rest and rest.lstrip().startswith("HTTP/"):
            head, _, rest = rest.partition("\r\n\r\n")
        for line in head.splitlines():
            if line.lower().startswith("x-oauth-scopes:"):
                scopes = line.split(":", 1)[1].strip()
                break
        body = rest
        body_start = raw.find(body)
    else:
        body = raw
    try:
        import json
        data = json.loads(body)
    except Exception:
        return {
            "status": "error",
            "message": "Invalid response from GitHub API",
        }
    if "login" in data:
        return {
            "status": "ok",
            "user": data["login"],
            "name": data.get("name") or data["login"],
            "scopes": scopes,
            "_body_offset": body_start,  # unused; retained for debugging
        }
    return {
        "status": "error",
        "message": data.get("message", "GitHub returned an unexpected response"),
    }


@router.post("/git-forge/test-token")
async def test_git_forge_token(
    body: GitForgeTokenTest, _user=Depends(_au.require_admin)
):
    """Validate a candidate Git forge credential WITHOUT persisting it.

    Used by the Bootstrap Step 3.5 Git Forge setup to let the operator
    sanity-check their credential before they commit it to settings.
    ``github`` / ``gitlab`` run a token probe against the respective
    REST APIs; ``gerrit`` runs an SSH probe (``gerrit version``) since
    Gerrit's first-class transport is SSH, not HTTP.
    """
    provider = (body.provider or "").strip().lower()
    if provider not in {"github", "gitlab", "gerrit"}:
        raise HTTPException(400, f"Unknown provider: {body.provider}")
    try:
        if provider == "gitlab":
            result = await asyncio.wait_for(
                _probe_gitlab_token(body.token, body.url), timeout=15,
            )
        elif provider == "gerrit":
            result = await asyncio.wait_for(
                _probe_gerrit_ssh(body.ssh_host, body.ssh_port, body.url),
                timeout=15,
            )
        else:
            result = await asyncio.wait_for(
                _probe_github_token(body.token), timeout=15,
            )
    except asyncio.TimeoutError:
        return {"status": "error", "message": "Connection timed out (15s)"}
    except Exception as exc:  # pragma: no cover — network-level failure
        return {"status": "error", "message": str(exc)}
    # Strip internal debug key before returning.
    result.pop("_body_offset", None)
    return result


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
    # SDK source for auto-provisioning (Phase 45)
    sdk_git_url: str = ""          # Git URL to clone SDK from
    sdk_git_branch: str = "main"   # Branch to clone
    sdk_install_script: str = ""   # Post-clone setup script
    npu_enabled: bool = False
    deploy_method: str = "ssh"
    deploy_target_ip: str = ""


@router.post("/vendor/sdks")
async def create_vendor_sdk(body: VendorSDKCreate, _user=Depends(_au.require_admin)):
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
        "sdk_git_url": body.sdk_git_url,
        "sdk_git_branch": body.sdk_git_branch,
        "sdk_install_script": body.sdk_install_script,
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
async def delete_vendor_sdk(platform: str, _user=Depends(_au.require_admin)):
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


@router.post("/vendor/sdks/{platform}/install")
async def install_vendor_sdk(platform: str, _user=Depends(_au.require_admin)):
    """Clone and provision the vendor SDK for a platform.

    Reads sdk_git_url from the platform YAML, clones the repo,
    scans for toolchain/sysroot, and updates the platform profile.
    """
    if not re.match(r'^[a-zA-Z0-9_-]+$', platform):
        raise HTTPException(400, "Invalid platform name")
    from backend.sdk_provisioner import provision_sdk
    result = await provision_sdk(platform)
    if result["status"] == "error":
        raise HTTPException(400, result["details"])
    return result


@router.get("/vendor/sdks/{platform}/validate")
async def validate_vendor_sdk(platform: str):
    """Validate that SDK paths in a platform profile exist on disk."""
    if not re.match(r'^[a-zA-Z0-9_-]+$', platform):
        raise HTTPException(400, "Invalid platform name")
    from backend.sdk_provisioner import validate_sdk_paths
    return validate_sdk_paths(platform)
