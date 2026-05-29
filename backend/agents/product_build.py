"""Pinned external product-source build orchestration (OP-1843 / P2.2).

Builds on :mod:`backend.agents.product_source`: resolve the configured source,
clone it at the configured immutable ref, scrub the clone credential from git
metadata, run the tier-specific Android debug build, and return the APK path.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from backend.agents.product_source import (
    ProductSourceError,
    resolve_product_source,
    resolve_product_source_credential,
)


TIER_MODULES = {
    "consumer": ":apps:consumer",
    "medical": ":apps:medical",
    "automotive": ":apps:automotive",
}


@dataclass(frozen=True)
class ProductBuildResult:
    apk_path: Path
    tier: str
    pinned_ref: str
    repo_url: str
    module: str


def _repo_dir_name(repo_url: str) -> str:
    tail = repo_url.rstrip("/").rsplit("/", 1)[-1]
    if ":" in tail:
        tail = tail.rsplit(":", 1)[-1]
    name = tail.removesuffix(".git")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return safe or "product-source"


def _credential_token(row: dict) -> str:
    token = str(row.get("token") or "").strip()
    if not token:
        raise ProductSourceError("product source git_accounts row has no token")
    return token


def _url_with_token(repo_url: str, cred: dict) -> str:
    token = _credential_token(cred)
    parts = urlsplit(repo_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return repo_url

    username = str(cred.get("username") or "x-access-token").strip()
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    auth = f"{quote(username, safe='')}:{quote(token, safe='')}"
    return urlunsplit(
        (parts.scheme, f"{auth}@{host}{port}", parts.path, parts.query, parts.fragment)
    )


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None):
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _tail(text: str, *, lines: int = 40) -> str:
    return "\n".join((text or "").splitlines()[-lines:])


def _assert_ok(proc, *, action: str) -> None:
    if proc.returncode == 0:
        return
    detail = _tail("\n".join(part for part in [proc.stdout, proc.stderr] if part))
    raise ProductSourceError(f"{action} failed with exit {proc.returncode}: {detail}")


def _gradle_env(workspace_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    gradle_home = workspace_root / ".gradle-user-home"
    gradle_home.mkdir(parents=True, exist_ok=True)
    env["GRADLE_USER_HOME"] = str(gradle_home)
    for name in ("ANDROID_HOME", "JAVA_HOME", "GRADLE_RO_DEP_CACHE"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


async def build_product_source(
    project_key: str,
    *,
    workspace_root: Path,
    tenant_id: str | None = None,
) -> ProductBuildResult:
    source = resolve_product_source(project_key)
    if source is None:
        raise ProductSourceError("project not configured for external-source build")

    cred = await resolve_product_source_credential(source, tenant_id=tenant_id)
    token_url = _url_with_token(source.repo_url, cred)

    tier = source.tier
    module = TIER_MODULES.get(tier)
    if module is None:
        expected = sorted(TIER_MODULES)
        raise ProductSourceError(
            f"product source tier {tier!r} cannot be built; expected one of {expected}"
        )

    workspace_root.mkdir(parents=True, exist_ok=True)
    checkout_dir = workspace_root / _repo_dir_name(source.repo_url)

    proc = _run(["git", "clone", token_url, str(checkout_dir)])
    _assert_ok(proc, action="git clone")

    proc = _run(
        ["git", "remote", "set-url", "origin", source.repo_url],
        cwd=checkout_dir,
    )
    _assert_ok(proc, action="git remote scrub")

    proc = _run(["git", "checkout", "--detach", source.pinned_ref], cwd=checkout_dir)
    _assert_ok(proc, action="git checkout pinned_ref")

    proc = _run(["git", "rev-parse", "HEAD"], cwd=checkout_dir)
    _assert_ok(proc, action="git rev-parse HEAD")
    actual_head = (proc.stdout or "").strip()
    if actual_head != source.pinned_ref:
        raise ProductSourceError(
            "checked out HEAD "
            f"{actual_head!r} does not match pinned_ref {source.pinned_ref!r}"
        )

    gradle_task = f"{module}:assembleDebug"
    proc = _run(
        ["gradle", gradle_task, "--offline", "--console=plain"],
        cwd=checkout_dir,
        env=_gradle_env(workspace_root),
    )
    _assert_ok(proc, action="gradle assembleDebug")

    apk_path = (
        checkout_dir
        / "apps"
        / tier
        / "build"
        / "outputs"
        / "apk"
        / "debug"
        / f"{tier}-debug.apk"
    )
    if not apk_path.exists():
        raise ProductSourceError(f"expected APK was not produced: {apk_path}")

    return ProductBuildResult(
        apk_path=apk_path,
        tier=tier,
        pinned_ref=source.pinned_ref,
        repo_url=source.repo_url,
        module=module,
    )


def build_product_source_sync(
    project_key: str,
    *,
    workspace_root: Path,
    tenant_id: str | None = None,
) -> ProductBuildResult:
    return asyncio.run(
        build_product_source(
            project_key,
            workspace_root=workspace_root,
            tenant_id=tenant_id,
        )
    )
