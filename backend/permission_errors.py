"""Permission & Environment Error Classifier — auto-fix for recoverable issues.

Parses tool output for permission/environment errors and provides
auto-fix actions or user-actionable suggestions.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


class PermissionErrorCategory:
    FILE_READONLY = "file_readonly"
    DIR_NOT_WRITABLE = "dir_not_writable"
    SSH_KEY_PERMISSION = "ssh_key_permission"
    DISK_FULL = "disk_full"
    DOCKER_SOCKET = "docker_socket"
    PORT_IN_USE = "port_in_use"
    COMMAND_NOT_FOUND = "command_not_found"
    NPM_EACCES = "npm_eacces"
    GIT_LOCK = "git_lock"
    UNKNOWN = "unknown"


# Pattern → (category, auto_fixable, fix_description)
_PATTERNS: list[tuple[re.Pattern, str, bool, str]] = [
    # File/directory permission
    (re.compile(r"permission denied.*write|read-only file system|EACCES.*open", re.I),
     PermissionErrorCategory.FILE_READONLY, True, "chmod u+w on affected file"),
    (re.compile(r"permission denied.*mkdir|cannot create directory|EACCES.*mkdir", re.I),
     PermissionErrorCategory.DIR_NOT_WRITABLE, True, "chmod u+w on parent directory"),

    # SSH key permissions
    (re.compile(r"permissions .* for .* are too open|bad permissions|UNPROTECTED PRIVATE KEY", re.I),
     PermissionErrorCategory.SSH_KEY_PERMISSION, True, "chmod 600 on SSH key file"),

    # Disk space
    (re.compile(r"no space left on device|disk quota exceeded|ENOSPC", re.I),
     PermissionErrorCategory.DISK_FULL, True, "cleanup old artifacts and temp files"),

    # Docker
    (re.compile(r"permission denied.*docker\.sock|cannot connect to the docker daemon|dial unix.*docker", re.I),
     PermissionErrorCategory.DOCKER_SOCKET, False, "Run: sudo usermod -aG docker $USER && newgrp docker"),

    # Port in use
    (re.compile(r"address already in use|EADDRINUSE|bind.*port.*in use", re.I),
     PermissionErrorCategory.PORT_IN_USE, True, "use next available port"),

    # Command not found
    (re.compile(r"command not found|no such file or directory.*bin/|not recognized as.*command", re.I),
     PermissionErrorCategory.COMMAND_NOT_FOUND, False, "Install missing tool"),

    # npm/pip permission
    (re.compile(r"EACCES.*npm|npm.*EACCES|permission denied.*node_modules|pip.*permission", re.I),
     PermissionErrorCategory.NPM_EACCES, True, "use --user flag or virtual environment"),

    # Git lock
    (re.compile(r"index\.lock.*exists|unable to create.*lock|another git process", re.I),
     PermissionErrorCategory.GIT_LOCK, True, "remove stale git lock file"),
]


def classify_permission_error(output: str) -> dict | None:
    """Classify a tool error output for permission/environment issues.

    Returns None if not a permission error, or a dict with:
        category, auto_fixable, fix_description, matched_text
    """
    if not output:
        return None

    output_lower = output.lower()

    # Quick pre-check: skip if no permission-related keywords
    keywords = ("permission", "denied", "eacces", "readonly", "read-only",
                "no space", "disk quota", "docker.sock", "address already",
                "eaddrinuse", "command not found", "not found", "index.lock",
                "too open", "enospc", "npm", "pip")
    if not any(k in output_lower for k in keywords):
        return None

    for pattern, category, auto_fixable, fix_desc in _PATTERNS:
        m = pattern.search(output)
        if m:
            return {
                "category": category,
                "auto_fixable": auto_fixable,
                "fix_description": fix_desc,
                "matched_text": m.group(0)[:100],
            }

    return None


async def attempt_auto_fix(category: str, error_output: str, workspace_path: str = "") -> dict:
    """Attempt to automatically fix a classified permission error.

    Returns: {"fixed": bool, "action": str, "detail": str}
    """
    import asyncio
    from pathlib import Path

    if category == PermissionErrorCategory.FILE_READONLY:
        # Extract file path from error
        path = _extract_path_from_error(error_output)
        if path and workspace_path:
            target = Path(workspace_path) / path
            if target.exists():
                try:
                    import os, stat
                    current = target.stat().st_mode
                    target.chmod(current | stat.S_IWUSR)
                    return {"fixed": True, "action": "chmod u+w", "detail": str(target)}
                except Exception as exc:
                    return {"fixed": False, "action": "chmod failed", "detail": str(exc)}
        return {"fixed": False, "action": "no path found", "detail": ""}

    if category == PermissionErrorCategory.DIR_NOT_WRITABLE:
        path = _extract_path_from_error(error_output)
        if path and workspace_path:
            target = Path(workspace_path) / path
            parent = target.parent if not target.is_dir() else target
            if parent.exists():
                try:
                    import os, stat
                    parent.chmod(parent.stat().st_mode | stat.S_IWUSR)
                    return {"fixed": True, "action": "chmod u+w dir", "detail": str(parent)}
                except Exception as exc:
                    return {"fixed": False, "action": "chmod dir failed", "detail": str(exc)}
        return {"fixed": False, "action": "no path found", "detail": ""}

    if category == PermissionErrorCategory.SSH_KEY_PERMISSION:
        # Extract key path
        m = re.search(r"for '([^']+)'|for \"([^\"]+)\"|permissions.*?(/\S+)", error_output, re.I)
        if m:
            key_path = Path(m.group(1) or m.group(2) or m.group(3)).expanduser()
            if key_path.exists():
                try:
                    import os, stat
                    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
                    return {"fixed": True, "action": "chmod 600", "detail": str(key_path)}
                except Exception as exc:
                    return {"fixed": False, "action": "chmod 600 failed", "detail": str(exc)}
        return {"fixed": False, "action": "no key path found", "detail": ""}

    if category == PermissionErrorCategory.DISK_FULL:
        try:
            from backend.routers.artifacts import get_artifacts_root
            artifacts_root = get_artifacts_root()
            # Remove old artifact files (keep last 20)
            import shutil
            all_files = sorted(artifacts_root.rglob("*"), key=lambda f: f.stat().st_mtime if f.is_file() else 0)
            files = [f for f in all_files if f.is_file()]
            removed = 0
            freed = 0
            for f in files[:-20]:  # Keep last 20
                size = f.stat().st_size
                f.unlink(missing_ok=True)
                removed += 1
                freed += size
            if removed > 0:
                return {"fixed": True, "action": f"cleaned {removed} old artifacts", "detail": f"freed {freed // 1024}KB"}
        except Exception as exc:
            return {"fixed": False, "action": "cleanup failed", "detail": str(exc)}
        return {"fixed": False, "action": "no artifacts to clean", "detail": ""}

    if category == PermissionErrorCategory.PORT_IN_USE:
        # Extract port number
        m = re.search(r"port[:\s]*(\d+)|:(\d+)", error_output, re.I)
        if m:
            port = int(m.group(1) or m.group(2))
            next_port = port + 1
            return {"fixed": True, "action": f"suggest port {next_port}", "detail": f"original port {port} in use"}
        return {"fixed": False, "action": "no port found", "detail": ""}

    if category == PermissionErrorCategory.GIT_LOCK:
        if workspace_path:
            lock = Path(workspace_path) / ".git" / "index.lock"
            if lock.exists():
                try:
                    lock.unlink()
                    return {"fixed": True, "action": "removed index.lock", "detail": str(lock)}
                except Exception as exc:
                    return {"fixed": False, "action": "lock removal failed", "detail": str(exc)}
        return {"fixed": False, "action": "no lock file found", "detail": ""}

    if category == PermissionErrorCategory.NPM_EACCES:
        return {"fixed": True, "action": "suggest --user flag", "detail": "npm install --user or use venv"}

    # Non-fixable categories
    return {"fixed": False, "action": "requires manual intervention", "detail": ""}


def _extract_path_from_error(output: str) -> str:
    """Try to extract a file/directory path from an error message."""
    # Common patterns: "Permission denied: '/path/to/file'"
    m = re.search(r"['\"]([/\w._-]+)['\"]", output)
    if m:
        return m.group(1)
    # Bare path after "Permission denied"
    m = re.search(r"permission denied[:\s]+(/\S+)", output, re.I)
    if m:
        return m.group(1)
    return ""


async def check_environment(workspace_path: str = "") -> list[dict]:
    """Run preventive environment checks before workspace operations.

    Returns list of issues found: [{check, status, detail, suggestion}]
    """
    import asyncio
    import shutil
    from pathlib import Path

    issues = []

    # 1. Disk space
    try:
        usage = shutil.disk_usage(workspace_path or "/")
        free_mb = usage.free // (1024 * 1024)
        if free_mb < 100:
            issues.append({
                "check": "disk_space",
                "status": "critical",
                "detail": f"{free_mb}MB free",
                "suggestion": "Free up disk space or clean old artifacts",
            })
        elif free_mb < 500:
            issues.append({
                "check": "disk_space",
                "status": "warning",
                "detail": f"{free_mb}MB free",
                "suggestion": "Disk space is low",
            })
    except Exception:
        pass

    # 2. Docker availability
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info", "--format", "{{.ServerVersion}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            err = stderr.decode()[:100]
            if "permission denied" in err.lower():
                issues.append({
                    "check": "docker",
                    "status": "error",
                    "detail": "Docker socket permission denied",
                    "suggestion": "Run: sudo usermod -aG docker $USER && newgrp docker",
                })
            else:
                issues.append({
                    "check": "docker",
                    "status": "warning",
                    "detail": "Docker not available",
                    "suggestion": "Install Docker or set OMNISIGHT_DOCKER_ENABLED=false",
                })
    except Exception:
        issues.append({
            "check": "docker",
            "status": "warning",
            "detail": "Docker command not found",
            "suggestion": "Install Docker or disable container isolation",
        })

    # 3. Git available
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=3)
        if proc.returncode != 0:
            issues.append({
                "check": "git",
                "status": "error",
                "detail": "Git not available",
                "suggestion": "Install git",
            })
    except Exception:
        issues.append({
            "check": "git", "status": "error",
            "detail": "Git not found", "suggestion": "Install git",
        })

    # 4. SSH key exists
    from backend.config import settings
    key_path = Path(settings.git_ssh_key_path).expanduser()
    if settings.git_ssh_key_path and not key_path.exists():
        issues.append({
            "check": "ssh_key",
            "status": "warning",
            "detail": f"SSH key not found: {settings.git_ssh_key_path}",
            "suggestion": "Generate with: ssh-keygen -t ed25519",
        })
    elif key_path.exists():
        import stat
        mode = key_path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            issues.append({
                "check": "ssh_key_perms",
                "status": "warning",
                "detail": f"SSH key {key_path} has too open permissions",
                "suggestion": f"Run: chmod 600 {key_path}",
            })

    return issues
