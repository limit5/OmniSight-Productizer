"""Permission & Environment Error Classifier — auto-fix for recoverable issues.

Parses tool output for permission/environment errors and provides
auto-fix actions or user-actionable suggestions.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

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
        path = _extract_path_from_error(error_output)
        target = _resolve_under_workspace(path, workspace_path)
        if not target:
            return {"fixed": False, "action": "path outside workspace", "detail": path}
        if target.exists() and not target.is_symlink():
            try:
                import stat
                current = target.stat().st_mode
                target.chmod(current | stat.S_IWUSR)
                return {"fixed": True, "action": "chmod u+w", "detail": str(target)}
            except Exception as exc:
                return {"fixed": False, "action": "chmod failed", "detail": str(exc)}
        return {"fixed": False, "action": "target missing or symlink", "detail": str(target)}

    if category == PermissionErrorCategory.DIR_NOT_WRITABLE:
        path = _extract_path_from_error(error_output)
        target = _resolve_under_workspace(path, workspace_path)
        if not target:
            return {"fixed": False, "action": "path outside workspace", "detail": path}
        parent = target.parent if not target.is_dir() else target
        # Re-validate parent stays in workspace (parent of workspace root would escape)
        if not _resolve_under_workspace(str(parent), workspace_path, _accept_existing=True):
            return {"fixed": False, "action": "parent outside workspace", "detail": str(parent)}
        if parent.exists() and not parent.is_symlink():
            try:
                import stat
                parent.chmod(parent.stat().st_mode | stat.S_IWUSR)
                return {"fixed": True, "action": "chmod u+w dir", "detail": str(parent)}
            except Exception as exc:
                return {"fixed": False, "action": "chmod dir failed", "detail": str(exc)}
        return {"fixed": False, "action": "no path found", "detail": ""}

    if category == PermissionErrorCategory.SSH_KEY_PERMISSION:
        m = re.search(r"for '([^']+)'|for \"([^\"]+)\"|permissions.*?(/[^\s'\"]+)", error_output, re.I)
        if not m:
            return {"fixed": False, "action": "no key path found", "detail": ""}
        raw = Path(m.group(1) or m.group(2) or m.group(3)).expanduser()
        # Reject symlinks BEFORE resolve to prevent attacker pivoting via /tmp/key→/etc/shadow
        if raw.is_symlink():
            return {"fixed": False, "action": "refused: symlink", "detail": str(raw)}
        if not _is_allowed_ssh_key_path(raw):
            return {"fixed": False, "action": "refused: outside allowed ssh key dirs", "detail": str(raw)}
        if raw.exists():
            try:
                import stat
                raw.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
                return {"fixed": True, "action": "chmod 600", "detail": str(raw)}
            except Exception as exc:
                return {"fixed": False, "action": "chmod 600 failed", "detail": str(exc)}
        return {"fixed": False, "action": "key not found", "detail": str(raw)}

    if category == PermissionErrorCategory.DISK_FULL:
        try:
            from backend.routers.artifacts import get_artifacts_root
            artifacts_root = get_artifacts_root().resolve()
            import time as _time
            now = _time.time()
            # Whitelist: only delete known artifact extensions/dirs
            allowed_ext = {".tar.gz", ".tgz", ".zip", ".log", ".tmp", ".bin"}
            candidates: list[Path] = []
            for f in artifacts_root.rglob("*"):
                if not f.is_file() or f.is_symlink():
                    continue
                # Ensure inside artifacts_root after resolve (no symlink escape)
                try:
                    f_resolved = f.resolve()
                    f_resolved.relative_to(artifacts_root)
                except (ValueError, OSError):
                    continue
                # Skip in-flight writes (modified within last 1h)
                try:
                    if now - f.stat().st_mtime < 3600:
                        continue
                except OSError:
                    continue
                # Must match whitelist or be under releases/
                suffix = "".join(f.suffixes[-2:]) if len(f.suffixes) >= 2 else f.suffix
                if suffix not in allowed_ext and "releases" not in f.parts:
                    continue
                candidates.append(f)
            candidates.sort(key=lambda p: p.stat().st_mtime)
            removed = 0
            freed = 0
            for f in candidates[:-20]:
                try:
                    size = f.stat().st_size
                    # TOCTOU guard: re-check symlink right before unlink
                    if f.is_symlink():
                        continue
                    f.unlink(missing_ok=True)
                    removed += 1
                    freed += size
                except OSError:
                    continue
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
            try:
                ws_resolved = Path(workspace_path).resolve(strict=False)
                lock = (ws_resolved / ".git" / "index.lock")
                if lock.exists() and not lock.is_symlink():
                    # Stale-lock guard: only remove if older than 60s
                    import time as _time
                    if _time.time() - lock.stat().st_mtime < 60:
                        return {"fixed": False, "action": "lock too fresh — likely held", "detail": str(lock)}
                    lock.unlink()
                    return {"fixed": True, "action": "removed stale index.lock", "detail": str(lock)}
            except Exception as exc:
                return {"fixed": False, "action": "lock removal failed", "detail": str(exc)}
        return {"fixed": False, "action": "no lock file found", "detail": ""}

    if category == PermissionErrorCategory.NPM_EACCES:
        return {"fixed": True, "action": "suggest --user flag", "detail": "npm install --user or use venv"}

    # Non-fixable categories
    return {"fixed": False, "action": "requires manual intervention", "detail": ""}


def _resolve_under_workspace(
    rel_or_abs: str, workspace_path: str, _accept_existing: bool = False
) -> Path | None:
    """Resolve a path and verify it stays inside *workspace_path*.

    Returns None if workspace is empty, the path escapes the workspace,
    or the path is itself a symlink (which could be redirected).
    """
    if not rel_or_abs or not workspace_path:
        return None
    try:
        ws = Path(workspace_path).resolve(strict=False)
        cand = Path(rel_or_abs)
        # If absolute, accept only when already inside workspace.
        # If relative, resolve under workspace.
        if cand.is_absolute():
            target = cand.resolve(strict=False)
        else:
            target = (ws / cand).resolve(strict=False)
        target.relative_to(ws)
        if not _accept_existing and target.is_symlink():
            return None
        return target
    except (ValueError, OSError):
        return None


def _is_allowed_ssh_key_path(p: Path) -> bool:
    """Allow only paths under ~/.ssh or the configured git_ssh_key_path dir."""
    try:
        resolved = p.resolve(strict=False)
        roots: list[Path] = [Path("~/.ssh").expanduser().resolve()]
        try:
            from backend.config import settings as _s
            if _s.git_ssh_key_path:
                roots.append(Path(_s.git_ssh_key_path).expanduser().resolve().parent)
        except Exception:
            pass
        for r in roots:
            try:
                resolved.relative_to(r)
                return True
            except ValueError:
                continue
        return False
    except OSError:
        return False


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

    # 2. Docker availability — guarantee subprocess is reaped on timeout
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info", "--format", "{{.ServerVersion}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
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
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, AttributeError):
                pass

    # 3. Git available
    git_proc = None
    try:
        git_proc = await asyncio.create_subprocess_exec(
            "git", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(git_proc.communicate(), timeout=3)
        except asyncio.TimeoutError:
            git_proc.kill()
            await git_proc.wait()
            raise
        if git_proc.returncode != 0:
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
    finally:
        if git_proc is not None and git_proc.returncode is None:
            try:
                git_proc.kill()
                await git_proc.wait()
            except (ProcessLookupError, AttributeError):
                pass

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
