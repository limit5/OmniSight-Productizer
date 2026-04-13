"""Git Credential Registry — multi-repo credential management.

Loads credentials from:
  1. configs/git_credentials.yaml (file-based registry)
  2. config.py JSON map fields (env var overrides)
  3. config.py scalar fields (legacy fallback)

Priority: YAML file > JSON maps > scalar fallback
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse

import yaml

from backend.config import settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CREDENTIALS_CACHE: list[dict] | None = None
import threading as _threading
_CACHE_LOCK = _threading.Lock()


def _allowed_credential_roots() -> list[Path]:
    """Directories from which the credentials file may be loaded."""
    roots = [(_PROJECT_ROOT / "configs").resolve()]
    home_ssh = (Path("~/.config/omnisight").expanduser()).resolve()
    roots.append(home_ssh)
    return roots


def _load_yaml_credentials() -> list[dict]:
    """Load credentials from git_credentials.yaml.

    The configured path must resolve under one of the allowed roots
    (configs/ or ~/.config/omnisight/) to prevent path-traversal abuse
    via OMNISIGHT_GIT_CREDENTIALS_FILE.
    """
    if settings.git_credentials_file:
        candidate = Path(settings.git_credentials_file).expanduser()
    else:
        candidate = _PROJECT_ROOT / "configs" / "git_credentials.yaml"

    try:
        resolved = candidate.resolve(strict=False)
    except Exception:
        return []

    allowed = _allowed_credential_roots()
    if not any(_is_within(resolved, root) for root in allowed):
        logger.warning(
            "Refusing to load credentials from %s (outside allowed roots)",
            resolved,
        )
        return []

    if not resolved.exists():
        return []

    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        repos = data.get("repositories", [])
        if isinstance(repos, list):
            logger.info("Loaded %d repo credentials from %s", len(repos), resolved)
            return repos
    except yaml.YAMLError as exc:
        # Avoid logging credential-bearing snippets — only the error type/line.
        logger.warning(
            "Failed to parse git_credentials.yaml (%s)", type(exc).__name__
        )
    except Exception as exc:  # pragma: no cover — unexpected I/O
        logger.warning("Credential load I/O error: %s", type(exc).__name__)

    return []


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _load_json_map(json_str: str) -> dict[str, str]:
    """Parse a JSON map string from config, return empty dict on failure."""
    if not json_str:
        return {}
    try:
        data = json.loads(json_str)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


def _load_gerrit_instances() -> list[dict]:
    """Parse gerrit_instances JSON from config."""
    if not settings.gerrit_instances:
        return []
    try:
        data = json.loads(settings.gerrit_instances)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def get_credential_registry() -> list[dict]:
    """Get the full credential registry (cached after first call).

    Each entry has: {id, url, platform, token, ssh_key, ssh_host, ssh_port, project, webhook_secret}
    Lock prevents two concurrent first-callers from each parsing config and
    racing on _CREDENTIALS_CACHE assignment (one half-built dict could win).
    """
    global _CREDENTIALS_CACHE
    # Fast-path read without lock — dict-pointer assignment is atomic in CPython.
    cached = _CREDENTIALS_CACHE
    if cached is not None:
        return list(cached)

    with _CACHE_LOCK:
        # Double-check inside lock
        if _CREDENTIALS_CACHE is not None:
            return list(_CREDENTIALS_CACHE)
        registry = _build_registry()
        _CREDENTIALS_CACHE = registry
        logger.info("Credential registry: %d entries", len(registry))
        return list(registry)


def _build_registry() -> list[dict]:
    """Build the full credential registry from all configured sources."""
    registry: list[dict] = []

    # 1. Load from YAML file
    yaml_creds = _load_yaml_credentials()
    for entry in yaml_creds:
        registry.append({
            "id": entry.get("id", ""),
            "url": entry.get("url", ""),
            "platform": entry.get("platform", "unknown"),
            "token": entry.get("token", ""),
            "ssh_key": entry.get("ssh_key", ""),
            "ssh_host": entry.get("ssh_host", ""),
            "ssh_port": entry.get("ssh_port", 22),
            "project": entry.get("project", ""),
            "webhook_secret": entry.get("webhook_secret", ""),
        })

    # 2. Build entries from JSON maps (env var overrides)
    ssh_map = _load_json_map(settings.git_ssh_key_map)
    gh_map = _load_json_map(settings.github_token_map)
    gl_map = _load_json_map(settings.gitlab_token_map)

    # GitHub token map entries
    for host, token in gh_map.items():
        if not any(host in (r.get("url", "") or "") for r in registry):
            registry.append({
                "id": f"github-{host.replace('.', '-')}",
                "url": f"https://{host}",
                "platform": "github",
                "token": token,
                "ssh_key": ssh_map.get(host, ""),
            })

    # GitLab token map entries
    for host, token in gl_map.items():
        if not any(host in (r.get("url", "") or "") for r in registry):
            registry.append({
                "id": f"gitlab-{host.replace('.', '-')}",
                "url": f"https://{host}",
                "platform": "gitlab",
                "token": token,
                "ssh_key": ssh_map.get(host, ""),
            })

    # Gerrit instances from JSON
    for inst in _load_gerrit_instances():
        inst_id = inst.get("id", f"gerrit-{inst.get('ssh_host', 'unknown')}")
        if not any(r.get("id") == inst_id for r in registry):
            registry.append({
                "id": inst_id,
                "url": inst.get("url", ""),
                "platform": "gerrit",
                "ssh_host": inst.get("ssh_host", ""),
                "ssh_port": inst.get("ssh_port", 29418),
                "project": inst.get("project", ""),
                "webhook_secret": inst.get("webhook_secret", ""),
                "ssh_key": ssh_map.get(inst.get("ssh_host", ""), ""),
            })

    # 3. Build fallback entries from scalar config (backward compat)
    if settings.github_token and not any(r["platform"] == "github" for r in registry):
        registry.append({
            "id": "default-github",
            "url": "https://github.com",
            "platform": "github",
            "token": settings.github_token,
            "ssh_key": settings.git_ssh_key_path,
        })

    if settings.gitlab_token and not any(r["platform"] == "gitlab" for r in registry):
        registry.append({
            "id": "default-gitlab",
            "url": settings.gitlab_url or "https://gitlab.com",
            "platform": "gitlab",
            "token": settings.gitlab_token,
            "ssh_key": settings.git_ssh_key_path,
        })

    if settings.gerrit_enabled and settings.gerrit_ssh_host and not any(r["platform"] == "gerrit" for r in registry):
        registry.append({
            "id": "default-gerrit",
            "url": settings.gerrit_url,
            "platform": "gerrit",
            "ssh_host": settings.gerrit_ssh_host,
            "ssh_port": settings.gerrit_ssh_port,
            "project": settings.gerrit_project,
            "webhook_secret": settings.gerrit_webhook_secret,
            "ssh_key": settings.git_ssh_key_path,
        })

    return registry


def clear_credential_cache() -> None:
    """Clear the cached registry (call after settings change)."""
    global _CREDENTIALS_CACHE
    with _CACHE_LOCK:
        _CREDENTIALS_CACHE = None


def find_credential_for_url(url: str) -> dict | None:
    """Find the best matching credential entry for a git URL.

    Matches by host extraction from the URL against registered entries.
    """
    if not url:
        return None

    # Extract host from URL
    url_lower = url.lower()
    if url_lower.startswith("git@"):
        host = url_lower.split("@", 1)[1].split(":")[0]
    elif url_lower.startswith("ssh://"):
        parsed = urlparse(url_lower)
        host = parsed.hostname or ""
    else:
        parsed = urlparse(url_lower)
        host = parsed.hostname or ""

    if not host:
        return None

    registry = get_credential_registry()

    # Exact host match
    for entry in registry:
        entry_url = entry.get("url", "")
        entry_host = ""
        if entry_url:
            if "://" in entry_url:
                entry_host = urlparse(entry_url.lower()).hostname or ""
            else:
                entry_host = entry_url.lower()
        # Also check ssh_host for Gerrit entries
        ssh_host = (entry.get("ssh_host") or "").lower()

        if host == entry_host or host == ssh_host:
            return entry

    # Partial match (e.g., "gitlab" in host matches a gitlab entry)
    for entry in registry:
        entry_url = (entry.get("url") or "").lower()
        if host in entry_url or entry_url.rstrip("/").endswith(host):
            return entry

    return None


def get_token_for_url(url: str) -> str:
    """Get the authentication token for a URL from the registry.

    Falls back to scalar config if no registry match.
    """
    cred = find_credential_for_url(url)
    if cred and cred.get("token"):
        return cred["token"]

    # Legacy fallback
    from backend.git_auth import detect_platform
    platform = detect_platform(url)
    if platform == "github" and settings.github_token:
        return settings.github_token
    if platform == "gitlab" and settings.gitlab_token:
        return settings.gitlab_token
    return ""


def get_ssh_key_for_url(url: str) -> str:
    """Get the SSH key path for a URL from the registry.

    Falls back to scalar config if no registry match.
    """
    cred = find_credential_for_url(url)
    if cred and cred.get("ssh_key"):
        return cred["ssh_key"]
    return settings.git_ssh_key_path


def get_webhook_secret_for_host(host: str, platform: str = "") -> str:
    """Get the webhook secret for a specific host.

    Falls back to scalar config secrets.
    """
    registry = get_credential_registry()
    for entry in registry:
        entry_host = (entry.get("ssh_host") or "").lower()
        entry_url_host = ""
        if entry.get("url"):
            entry_url_host = (urlparse(entry["url"]).hostname or "").lower()
        if host.lower() in (entry_host, entry_url_host):
            secret = entry.get("webhook_secret", "")
            if secret:
                return secret

    # Fallback to scalar secrets
    if platform == "gerrit":
        return settings.gerrit_webhook_secret
    if platform == "github":
        return settings.github_webhook_secret
    if platform == "gitlab":
        return settings.gitlab_webhook_secret
    return ""
