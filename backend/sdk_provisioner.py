"""SDK Provisioner — clone vendor SDK repos and configure toolchain paths.

Provides:
  - provision_sdk(): clone SDK git repo → extract sysroot/toolchain → update platform YAML
  - scan_sdk_repo(): scan a cloned repo for CMakeLists.txt, toolchain.cmake, sysroot dirs
  - validate_sdk_paths(): check if platform YAML paths actually exist on disk
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml

from backend.events import emit_pipeline_phase

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PLATFORMS_DIR = _PROJECT_ROOT / "configs" / "platforms"
_SDK_ROOT = _PROJECT_ROOT / ".sdks"  # Local SDK cache directory


async def provision_sdk(platform: str) -> dict:
    """Clone and provision the vendor SDK for a platform profile.

    Reads sdk_git_url from the platform YAML, clones the repo,
    scans for toolchain files, and updates sysroot/cmake paths.

    Returns: {status, sdk_path, sysroot_found, cmake_found, details}
    """
    profile = _PLATFORMS_DIR / f"{platform}.yaml"
    if not profile.exists():
        return {"status": "error", "details": f"Platform profile not found: {platform}"}

    data = yaml.safe_load(profile.read_text(encoding="utf-8")) or {}
    sdk_url = data.get("sdk_git_url", "")
    if not sdk_url:
        return {"status": "skipped", "details": "No sdk_git_url configured in platform YAML"}

    branch = data.get("sdk_git_branch", "main")
    sdk_path = _SDK_ROOT / platform

    emit_pipeline_phase("sdk_provision", f"Cloning SDK for {platform} from {sdk_url}")

    # Clone or update SDK repo
    try:
        _SDK_ROOT.mkdir(parents=True, exist_ok=True)
        if sdk_path.exists() and (sdk_path / ".git").exists():
            # Update existing clone
            emit_pipeline_phase("sdk_provision", f"Updating existing SDK: {platform}")
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(sdk_path), "pull", "--ff-only",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                logger.warning("SDK pull failed, re-cloning: %s", stderr.decode()[:100])
                import shutil
                shutil.rmtree(sdk_path, ignore_errors=True)

        if not sdk_path.exists():
            # Fresh clone with auth
            from backend.git_auth import get_auth_env
            auth_env = get_auth_env(sdk_url)
            import os
            env = {**os.environ, **auth_env}
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth", "1", "-b", branch, sdk_url, str(sdk_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            if proc.returncode != 0:
                return {"status": "error", "details": f"Clone failed: {stderr.decode()[:200]}"}

        emit_pipeline_phase("sdk_provision", f"SDK cloned: {sdk_path}")
    except asyncio.TimeoutError:
        return {"status": "error", "details": "SDK clone timed out (300s)"}
    except Exception as exc:
        return {"status": "error", "details": str(exc)[:200]}

    # Run install script if configured
    install_script = data.get("sdk_install_script", "")
    if install_script:
        script_path = sdk_path / install_script
        if script_path.exists():
            emit_pipeline_phase("sdk_provision", f"Running install script: {install_script}")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bash", str(script_path),
                    cwd=sdk_path,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=120)
            except Exception as exc:
                logger.warning("SDK install script failed: %s", exc)

    # Scan for toolchain files
    scan = scan_sdk_repo(sdk_path)

    # Auto-update platform YAML with discovered paths
    updated = False
    if scan["sysroot_path"] and not data.get("sysroot_path"):
        data["sysroot_path"] = scan["sysroot_path"]
        updated = True
    if scan["cmake_toolchain_file"] and not data.get("cmake_toolchain_file"):
        data["cmake_toolchain_file"] = scan["cmake_toolchain_file"]
        updated = True

    if updated:
        profile.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))
        emit_pipeline_phase("sdk_provision", f"Updated {platform}.yaml with discovered SDK paths")
        logger.info("SDK auto-discovery: updated %s with sysroot=%s cmake=%s",
                     platform, scan["sysroot_path"], scan["cmake_toolchain_file"])

    return {
        "status": "provisioned",
        "sdk_path": str(sdk_path),
        "sysroot_found": scan["sysroot_path"],
        "cmake_found": scan["cmake_toolchain_file"],
        "toolchain_files": scan["toolchain_files"],
        "details": f"SDK ready at {sdk_path}",
    }


def scan_sdk_repo(sdk_path: Path) -> dict:
    """Scan a cloned SDK repo for sysroot directories and toolchain files.

    Looks for common patterns:
    - sysroot: directories named 'sysroot', 'staging', 'target', or containing 'usr/lib'
    - cmake: files named 'toolchain.cmake', '*-toolchain.cmake', 'CMakeToolchain.cmake'
    - Makefile / build system indicators

    Returns: {sysroot_path, cmake_toolchain_file, toolchain_files}
    """
    sysroot_path = ""
    cmake_file = ""
    toolchain_files: list[str] = []

    if not sdk_path.is_dir():
        return {"sysroot_path": "", "cmake_toolchain_file": "", "toolchain_files": []}

    # Scan for sysroot directories (max depth 3)
    sysroot_candidates = ["sysroot", "staging", "target", "rootfs", "sdk/sysroot"]
    for candidate in sysroot_candidates:
        p = sdk_path / candidate
        if p.is_dir():
            # Verify it looks like a sysroot (has usr/lib or usr/include)
            if (p / "usr" / "lib").is_dir() or (p / "usr" / "include").is_dir() or (p / "lib").is_dir():
                sysroot_path = str(p)
                break
    # Deeper scan if no obvious candidate
    if not sysroot_path:
        for p in sdk_path.rglob("usr/lib"):
            if p.is_dir():
                sysroot_path = str(p.parent.parent)  # usr/lib → parent of usr
                break

    # Scan for CMake toolchain files
    cmake_patterns = ["toolchain.cmake", "*-toolchain.cmake", "CMakeToolchain.cmake",
                       "cmake/toolchain.cmake", "cmake/*.toolchain.cmake"]
    for pattern in cmake_patterns:
        matches = list(sdk_path.glob(pattern))
        if not matches:
            matches = list(sdk_path.glob(f"**/{pattern}"))
        for m in matches[:3]:  # Limit to prevent huge scans
            toolchain_files.append(str(m))
            if not cmake_file:
                cmake_file = str(m)

    # Also look for Makefile-based toolchain indicators
    for indicator in ["Makefile", "build.sh", "setup_env.sh", "environment-setup-*"]:
        matches = list(sdk_path.glob(indicator))
        for m in matches[:2]:
            toolchain_files.append(str(m))

    return {
        "sysroot_path": sysroot_path,
        "cmake_toolchain_file": cmake_file,
        "toolchain_files": toolchain_files[:10],
    }


def validate_sdk_paths(platform: str) -> dict:
    """Validate that the SDK paths in a platform YAML actually exist.

    Returns: {valid, missing_paths, warnings}
    """
    profile = _PLATFORMS_DIR / f"{platform}.yaml"
    if not profile.exists():
        return {"valid": False, "missing_paths": [], "warnings": ["Profile not found"]}

    data = yaml.safe_load(profile.read_text(encoding="utf-8")) or {}
    missing: list[str] = []
    warnings: list[str] = []

    sysroot = data.get("sysroot_path", "")
    if sysroot and not Path(sysroot).is_dir():
        missing.append(f"sysroot_path: {sysroot}")
        sdk_url = data.get("sdk_git_url", "")
        if sdk_url:
            warnings.append(f"sysroot missing but sdk_git_url is set — run /sdks install {platform}")
        else:
            warnings.append("sysroot missing and no sdk_git_url — set sdk_git_url or install SDK manually")

    cmake_tc = data.get("cmake_toolchain_file", "")
    if cmake_tc and not Path(cmake_tc).is_file():
        missing.append(f"cmake_toolchain_file: {cmake_tc}")

    toolchain = data.get("toolchain", "")
    if toolchain:
        import shutil
        if not shutil.which(toolchain):
            missing.append(f"toolchain binary: {toolchain}")
            warnings.append(f"Cross-compiler '{toolchain}' not found in PATH")

    return {
        "valid": len(missing) == 0,
        "missing_paths": missing,
        "warnings": warnings,
    }
