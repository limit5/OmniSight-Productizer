"""B2/INGEST-01 — Repository ingestion and introspection.

Clones a repo (public or private), reads manifest files
(package.json, README.md, next.config.mjs, requirements.txt,
Cargo.toml), and maps discovered fields to a ParsedSpec for the
intent/DAG pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from backend.git_credentials import find_credential_for_url, get_token_for_url
from backend.intent_parser import Field, ParsedSpec

logger = logging.getLogger(__name__)

CLONE_TIMEOUT = 60  # seconds
_INGEST_ROOT = Path(tempfile.gettempdir()) / "omnisight_ingest"


@dataclass
class IntrospectionResult:
    """Raw data extracted from repo manifest files."""
    package_json: dict | None = None
    readme_content: str = ""
    next_config: str = ""
    requirements_txt: list[str] = field(default_factory=list)
    cargo_toml: str = ""
    detected_files: list[str] = field(default_factory=list)


def _validate_url(url: str) -> str:
    """Validate and normalise a git URL. Raises ValueError on bad input."""
    url = url.strip()
    if not url:
        raise ValueError("Empty repository URL")
    if any(c in url for c in ('`', '$', ';', '|', '&', '\n', '\r')):
        raise ValueError(f"Invalid characters in repository URL: {url}")
    if url.startswith("git@") or url.startswith("ssh://"):
        return url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError(f"No hostname in URL: {url}")
    return url


def _build_auth_url(url: str, token: str) -> str:
    """Embed token into HTTPS URL for authenticated clone."""
    if not token or not url.startswith("http"):
        return url
    parsed = urlparse(url)
    netloc = f"x-access-token:{token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def _build_auth_env(url: str) -> dict[str, str]:
    """Build GIT_ASKPASS env for SSH-based URLs."""
    from backend.git_credentials import get_ssh_key_for_url
    env: dict[str, str] = {}
    ssh_key = get_ssh_key_for_url(url)
    if ssh_key:
        expanded = os.path.expanduser(ssh_key)
        if os.path.isfile(expanded):
            env["GIT_SSH_COMMAND"] = f'ssh -i "{expanded}" -o StrictHostKeyChecking=accept-new'
    return env


async def clone_repo(
    url: str,
    *,
    shallow: bool = True,
    dest: Path | None = None,
) -> Path:
    """Clone a repository with credential validation.

    Returns the path to the cloned repo. Uses shallow clone by default
    for speed. Credentials are resolved via the git_credentials registry.
    """
    url = _validate_url(url)

    _INGEST_ROOT.mkdir(parents=True, exist_ok=True)
    if dest is None:
        parsed = urlparse(url if url.startswith("http") else f"https://{url}")
        repo_name = Path(parsed.path or "repo").stem.rstrip(".git") or "repo"
        repo_name = re.sub(r'[^a-zA-Z0-9_-]', '_', repo_name)
        dest = _INGEST_ROOT / f"{repo_name}_{os.getpid()}"

    if dest.exists():
        shutil.rmtree(dest)

    token = get_token_for_url(url)
    clone_url = _build_auth_url(url, token) if url.startswith("http") else url
    extra_env = _build_auth_env(url) if not url.startswith("http") else {}

    depth_flag = "--depth 1" if shallow else ""
    cmd = f'git clone {depth_flag} "{clone_url}" "{dest}"'

    env = {**os.environ, **extra_env} if extra_env else None
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CLONE_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"Clone timed out after {CLONE_TIMEOUT}s: {url}")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip()
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        if "Authentication" in err or "could not read Username" in err:
            raise PermissionError(f"Authentication failed for {url}: {err}")
        raise RuntimeError(f"git clone failed (rc={proc.returncode}): {err}")

    logger.info("Cloned %s → %s (shallow=%s)", url, dest, shallow)
    return dest


def introspect(repo_path: Path) -> IntrospectionResult:
    """Read manifest files from a cloned repository."""
    repo_path = Path(repo_path)
    if not repo_path.is_dir():
        raise FileNotFoundError(f"Repository path does not exist: {repo_path}")

    result = IntrospectionResult()
    manifest_names = [
        "package.json", "README.md", "readme.md", "README.rst",
        "next.config.mjs", "next.config.js", "next.config.ts",
        "requirements.txt", "Cargo.toml",
        "pyproject.toml", "setup.py", "setup.cfg",
    ]
    for name in manifest_names:
        if (repo_path / name).is_file():
            result.detected_files.append(name)

    pkg = repo_path / "package.json"
    if pkg.is_file():
        try:
            result.package_json = json.loads(pkg.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Failed to parse package.json: %s", exc)

    for readme_name in ("README.md", "readme.md", "README.rst"):
        readme = repo_path / readme_name
        if readme.is_file():
            try:
                content = readme.read_text(encoding="utf-8", errors="replace")
                result.readme_content = content[:8192]
            except OSError:
                pass
            break

    for next_cfg_name in ("next.config.mjs", "next.config.js", "next.config.ts"):
        ncfg = repo_path / next_cfg_name
        if ncfg.is_file():
            try:
                result.next_config = ncfg.read_text(encoding="utf-8", errors="replace")[:4096]
            except OSError:
                pass
            break

    reqs = repo_path / "requirements.txt"
    if reqs.is_file():
        try:
            lines = reqs.read_text(encoding="utf-8", errors="replace").splitlines()
            result.requirements_txt = [
                ln.strip() for ln in lines
                if ln.strip() and not ln.strip().startswith("#")
            ]
        except OSError:
            pass

    cargo = repo_path / "Cargo.toml"
    if cargo.is_file():
        try:
            result.cargo_toml = cargo.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            pass

    return result


def _detect_framework_from_package_json(pkg: dict) -> tuple[str, float]:
    """Detect framework from package.json dependencies."""
    all_deps: dict[str, str] = {}
    all_deps.update(pkg.get("dependencies") or {})
    all_deps.update(pkg.get("devDependencies") or {})

    framework_map = {
        "next": ("nextjs", 0.95),
        "nuxt": ("nuxt", 0.95),
        "@angular/core": ("angular", 0.95),
        "svelte": ("svelte", 0.9),
        "@sveltejs/kit": ("sveltekit", 0.95),
        "react": ("react", 0.85),
        "vue": ("vue", 0.85),
        "gatsby": ("gatsby", 0.95),
        "astro": ("astro", 0.95),
        "remix": ("remix", 0.95),
        "express": ("express", 0.85),
        "fastify": ("fastify", 0.85),
    }

    for dep, (fw, conf) in framework_map.items():
        if dep in all_deps:
            return fw, conf

    return "unknown", 0.0


def _detect_runtime_model(
    pkg: dict | None, next_config: str, framework: str,
) -> tuple[str, float]:
    """Infer runtime model from manifest clues."""
    if next_config:
        if "output" in next_config:
            if re.search(r"""['"]export['"]""", next_config):
                return "ssg", 0.9
            if re.search(r"""['"]standalone['"]""", next_config):
                return "ssr", 0.9
        if "getServerSideProps" in next_config or "app/" in next_config:
            return "ssr", 0.7

    if pkg:
        scripts = pkg.get("scripts") or {}
        if "export" in scripts.get("build", ""):
            return "ssg", 0.8
        if "start" in scripts and framework in ("nextjs", "nuxt", "remix"):
            return "ssr", 0.7

    if framework in ("react", "vue", "angular", "svelte"):
        return "spa", 0.6

    return "unknown", 0.0


def _detect_persistence_from_requirements(deps: list[str]) -> tuple[str, float]:
    """Detect persistence from Python requirements."""
    dep_lower = [d.lower().split("==")[0].split(">=")[0].split("[")[0].strip() for d in deps]

    if "psycopg2" in dep_lower or "psycopg2-binary" in dep_lower or "asyncpg" in dep_lower:
        return "postgres", 0.9
    if "mysqlclient" in dep_lower or "pymysql" in dep_lower:
        return "mysql", 0.9
    if "redis" in dep_lower or "aioredis" in dep_lower:
        return "redis", 0.8
    if any("sqlite" in d for d in dep_lower):
        return "sqlite", 0.8
    if "sqlalchemy" in dep_lower:
        return "postgres", 0.5

    return "unknown", 0.0


def _detect_framework_from_requirements(deps: list[str]) -> tuple[str, float]:
    """Detect framework from Python requirements."""
    dep_lower = [d.lower().split("==")[0].split(">=")[0].split("[")[0].strip() for d in deps]

    framework_map = {
        "fastapi": ("fastapi", 0.95),
        "django": ("django", 0.95),
        "flask": ("flask", 0.95),
        "tornado": ("tornado", 0.9),
        "sanic": ("sanic", 0.9),
        "starlette": ("starlette", 0.85),
    }
    for dep, (fw, conf) in framework_map.items():
        if dep in dep_lower:
            return fw, conf

    return "unknown", 0.0


def _detect_from_cargo_toml(cargo_content: str) -> tuple[str, str, float]:
    """Detect framework and project type from Cargo.toml. Returns (framework, project_type, confidence)."""
    if not cargo_content:
        return "unknown", "unknown", 0.0

    deps_section = cargo_content.lower()
    if "actix-web" in deps_section or "actix_web" in deps_section:
        return "actix-web", "web_app", 0.9
    if "axum" in deps_section:
        return "axum", "web_app", 0.9
    if "rocket" in deps_section:
        return "rocket", "web_app", 0.9
    if "clap" in deps_section or "structopt" in deps_section:
        return "rust", "cli_tool", 0.85
    if "embedded-hal" in deps_section or "cortex-m" in deps_section:
        return "embedded-rust", "embedded_firmware", 0.9
    if "tokio" in deps_section:
        return "rust", "web_app", 0.6

    return "rust", "cli_tool", 0.6


def map_to_parsed_spec(result: IntrospectionResult) -> ParsedSpec:
    """Map introspection result to a ParsedSpec."""
    framework_val, framework_conf = "unknown", 0.0
    project_type_val, project_type_conf = "unknown", 0.0
    runtime_val, runtime_conf = "unknown", 0.0
    persistence_val, persistence_conf = "unknown", 0.0
    target_arch_val, target_arch_conf = "unknown", 0.0

    if result.package_json:
        framework_val, framework_conf = _detect_framework_from_package_json(result.package_json)
        project_type_val, project_type_conf = "web_app", 0.85
        runtime_val, runtime_conf = _detect_runtime_model(
            result.package_json, result.next_config, framework_val,
        )

        all_deps: dict[str, str] = {}
        all_deps.update(result.package_json.get("dependencies") or {})
        if "prisma" in all_deps or "@prisma/client" in all_deps:
            persistence_val, persistence_conf = "postgres", 0.7
        elif "better-sqlite3" in all_deps or "sql.js" in all_deps:
            persistence_val, persistence_conf = "sqlite", 0.8
        elif "mongoose" in all_deps or "mongodb" in all_deps:
            persistence_val, persistence_conf = "postgres", 0.5
        elif "redis" in all_deps or "ioredis" in all_deps:
            persistence_val, persistence_conf = "redis", 0.8

    if result.requirements_txt:
        if framework_conf < 0.5:
            fw, fc = _detect_framework_from_requirements(result.requirements_txt)
            if fc > framework_conf:
                framework_val, framework_conf = fw, fc
        if project_type_conf < 0.5:
            project_type_val, project_type_conf = "web_app", 0.7
        if persistence_conf < 0.5:
            pv, pc = _detect_persistence_from_requirements(result.requirements_txt)
            if pc > persistence_conf:
                persistence_val, persistence_conf = pv, pc
        runtime_val, runtime_conf = "ssr", 0.6

    if result.cargo_toml:
        cargo_fw, cargo_pt, cargo_conf = _detect_from_cargo_toml(result.cargo_toml)
        if cargo_conf > framework_conf:
            framework_val, framework_conf = cargo_fw, cargo_conf
        if cargo_conf > project_type_conf:
            project_type_val, project_type_conf = cargo_pt, cargo_conf
        if project_type_val == "cli_tool":
            runtime_val, runtime_conf = "cli", 0.8

    hw = "yes" if project_type_val == "embedded_firmware" else "no"
    hw_conf = 0.8 if project_type_val == "embedded_firmware" else 0.5

    return ParsedSpec(
        project_type=Field(project_type_val, project_type_conf),
        runtime_model=Field(runtime_val, runtime_conf),
        target_arch=Field(target_arch_val, target_arch_conf),
        target_os=Field("linux", 0.3),
        framework=Field(framework_val, framework_conf),
        persistence=Field(persistence_val, persistence_conf),
        deploy_target=Field("unknown", 0.0),
        hardware_required=Field(hw, hw_conf),
        raw_text=f"[ingested from repo: {', '.join(result.detected_files)}]",
    )


async def ingest_repo(url: str, *, shallow: bool = True) -> tuple[ParsedSpec, IntrospectionResult]:
    """Full pipeline: clone → introspect → map to ParsedSpec.

    Returns (ParsedSpec, IntrospectionResult) so callers can access
    both the structured spec and the raw manifest data.
    """
    repo_path = await clone_repo(url, shallow=shallow)
    try:
        result = introspect(repo_path)
        spec = map_to_parsed_spec(result)
        return spec, result
    finally:
        shutil.rmtree(repo_path, ignore_errors=True)


def cleanup_ingest_cache() -> None:
    """Remove all cached clones from the ingest temp directory."""
    if _INGEST_ROOT.exists():
        shutil.rmtree(_INGEST_ROOT, ignore_errors=True)
        logger.info("Cleaned ingest cache: %s", _INGEST_ROOT)
