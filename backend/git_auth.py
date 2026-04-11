"""Git authentication abstraction layer.

Supports SSH key and HTTPS token authentication for GitHub, GitLab
(both gitlab.com and self-hosted), and generic git hosts.

Authentication is injected via environment variables (``GIT_ASKPASS``)
so it never touches the global git config.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from backend.config import settings

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Platform detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def detect_platform(url: str) -> str:
    """Detect the git hosting platform from a remote URL.

    Returns ``"github"``, ``"gitlab"``, or ``"unknown"``.

    Handles HTTPS and SSH URLs::

        https://github.com/org/repo.git        → github
        git@github.com:org/repo.git             → github
        https://gitlab.com/org/repo.git         → gitlab
        git@gitlab.company.com:org/repo.git     → gitlab  (if matches GITLAB_URL)
        https://gitlab.company.com/org/repo.git → gitlab
    """
    url_lower = url.lower()

    # SSH-style: git@host:org/repo.git
    if url_lower.startswith("git@"):
        host = url_lower.split("@", 1)[1].split(":")[0]
    else:
        parsed = urlparse(url_lower)
        host = parsed.hostname or ""

    if "github.com" in host or "github" in host:
        return "github"

    if "gitlab.com" in host or "gitlab" in host:
        return "gitlab"

    # Check against configured Gerrit host
    if settings.gerrit_ssh_host:
        gerrit_host = settings.gerrit_ssh_host.lower()
        if gerrit_host in host or host in gerrit_host:
            return "gerrit"

    # Check against configured self-hosted GitLab URL
    if settings.gitlab_url:
        gitlab_host = urlparse(settings.gitlab_url.lower()).hostname or settings.gitlab_url.lower()
        if gitlab_host in host or host in gitlab_host:
            return "gitlab"

    return "unknown"


def parse_repo_path(url: str) -> str:
    """Extract ``owner/repo`` from a git URL.

    Works with both HTTPS and SSH formats::

        https://github.com/org/repo.git  → org/repo
        git@gitlab.com:org/repo.git      → org/repo
    """
    if url.startswith("git@"):
        # git@host:org/repo.git
        path = url.split(":", 1)[1]
    else:
        parsed = urlparse(url)
        path = parsed.path.lstrip("/")

    # Remove trailing .git
    if path.endswith(".git"):
        path = path[:-4]

    return path


def get_gitlab_api_url(remote_url: str) -> str:
    """Derive the GitLab API base URL from a remote URL.

    - ``git@gitlab.com:...``         → ``https://gitlab.com``
    - ``https://gitlab.company.com/...`` → ``https://gitlab.company.com``
    - Falls back to configured ``OMNISIGHT_GITLAB_URL`` or ``https://gitlab.com``.
    """
    if remote_url.startswith("git@"):
        host = remote_url.split("@", 1)[1].split(":")[0]
        return f"https://{host}"

    parsed = urlparse(remote_url)
    if parsed.scheme and parsed.hostname:
        return f"{parsed.scheme}://{parsed.hostname}"

    return settings.gitlab_url or "https://gitlab.com"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTPS token authentication
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _get_token_for_url(url: str) -> str:
    """Return the appropriate token for a given remote URL."""
    platform = detect_platform(url)
    if platform == "github" and settings.github_token:
        return settings.github_token
    if platform == "gitlab" and settings.gitlab_token:
        return settings.gitlab_token
    return ""


# Cached path to the askpass helper script
_askpass_script: str | None = None


def _ensure_askpass_script() -> str:
    """Create a small helper script that echoes a token.

    ``GIT_ASKPASS`` is called by git when it needs credentials for HTTPS.
    The script receives the prompt as ``$1`` and should print the password.
    We pass the actual token via ``GIT_OMNISIGHT_TOKEN`` env var so the
    script itself contains no secrets.
    """
    global _askpass_script
    if _askpass_script and Path(_askpass_script).exists():
        return _askpass_script

    script = tempfile.NamedTemporaryFile(
        prefix="omnisight_askpass_",
        suffix=".sh",
        delete=False,
        mode="w",
    )
    script.write("#!/bin/sh\necho \"$GIT_OMNISIGHT_TOKEN\"\n")
    script.close()
    os.chmod(script.name, stat.S_IRWXU)
    _askpass_script = script.name
    logger.debug("Created GIT_ASKPASS helper: %s", _askpass_script)
    # Register cleanup on interpreter exit
    import atexit
    atexit.register(lambda: Path(_askpass_script).unlink(missing_ok=True))
    return _askpass_script


def get_auth_env(url: str) -> dict[str, str]:
    """Return environment variables that inject authentication for *url*.

    For HTTPS URLs with a configured token, this sets up ``GIT_ASKPASS``
    so git receives the token without touching global config.

    For SSH URLs, this sets ``GIT_SSH_COMMAND`` to use the configured key
    and skip host-key prompts.

    Returns a dict that should be merged into the subprocess environment.
    """
    env: dict[str, str] = {}

    is_ssh = url.startswith("git@") or url.startswith("ssh://")

    if is_ssh:
        # SSH authentication
        key_path = settings.git_ssh_key_path
        if key_path and Path(key_path).expanduser().exists():
            resolved = str(Path(key_path).expanduser())
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {resolved} -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
            )
    else:
        # HTTPS token authentication
        token = _get_token_for_url(url)
        if token:
            askpass = _ensure_askpass_script()
            env["GIT_ASKPASS"] = askpass
            env["GIT_OMNISIGHT_TOKEN"] = token
            # Prevent git from prompting interactively
            env["GIT_TERMINAL_PROMPT"] = "0"

    return env


async def get_auth_env_for_remote(repo_path: Path, remote: str = "origin") -> dict[str, str]:
    """Convenience: read the remote URL from *repo_path* and return auth env."""
    import asyncio

    proc = await asyncio.create_subprocess_shell(
        f'git remote get-url "{remote}"',
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    url = stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""
    return get_auth_env(url) if url else {}
