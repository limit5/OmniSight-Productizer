"""External product contribution orchestration (OP-1846 / P2.4.3a).

Composes the B2 source resolver, clone-at-base mechanics, and GitHub PR target
into a library-only flow. The feature authoring step is an injected callable so
the autonomous runner and real agent invocation remain out of this module.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from backend.agents.github_pr_target import PrResult, open_contribution_pr
from backend.agents.product_build import _repo_dir_name, _url_with_token
from backend.agents.product_source import resolve_product_source


class ContributionError(RuntimeError):
    """A product contribution could not be prepared safely."""


@dataclass(frozen=True)
class ContributionResult:
    branch: str
    pr: PrResult | None
    no_changes: bool


def _sanitize_slug(slug: str) -> str:
    value = re.sub(r"[\s_]+", "-", slug.strip().lower())
    value = re.sub(r"[^a-z0-9-]+", "", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "change"


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    redact: tuple[str, ...] = (),
):
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        for secret in redact:
            if secret:
                detail = detail.replace(secret, "<redacted>")
        raise ContributionError(f"{cmd[:2]} failed: {detail}")
    return proc


async def _resolve_git_account(
    git_account_ref: str,
    *,
    tenant_id: str | None,
) -> dict:
    if not git_account_ref:
        raise ContributionError("git_account_ref is required")

    from backend import git_credentials

    row = await git_credentials.pick_by_id(git_account_ref, tenant_id=tenant_id)
    if row is None:
        raise ContributionError(f"git_accounts row {git_account_ref!r} not found")
    return row


def _commit_message(ticket_key: str) -> str:
    env_name = _git_config(["git", "config", "--get", "user.name"])
    env_email = _git_config(["git", "config", "--get", "user.email"])
    global_name = _git_config(["git", "config", "--global", "--get", "user.name"])
    global_email = _git_config(["git", "config", "--global", "--get", "user.email"])

    return "\n".join([
        f"{ticket_key} product contribution",
        "",
        f"Automated contribution for {ticket_key}.",
        "",
        "Co-Authored-By: GPT-5.5 (codex-cli) <noreply@openai.com>",
        f"Co-Authored-By: {env_name} <{env_email}>",
        f"Co-Authored-By: {global_name} <{global_email}>",
    ])


def _git_config(cmd: list[str]) -> str:
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
    )
    value = (proc.stdout or "").strip()
    return value or "unconfigured"


async def contribute_to_product(
    project_key: str,
    *,
    ticket_key: str,
    base: str,
    slug: str,
    implement: Callable[[Path], Awaitable[None] | None],
    git_account_ref: str,
    workspace_root: Path,
    tenant_id: str | None = None,
) -> ContributionResult:
    source = resolve_product_source(project_key)
    if source is None:
        raise ContributionError(
            "project not configured for external-source contribution"
        )

    cred = await _resolve_git_account(git_account_ref, tenant_id=tenant_id)
    token_url = _url_with_token(source.repo_url, cred)
    token = str(cred.get("token") or "").strip()
    clean_url = source.repo_url

    workspace_root.mkdir(parents=True, exist_ok=True)
    worktree = workspace_root / _repo_dir_name(source.repo_url)
    branch = f"feature/{ticket_key}-{_sanitize_slug(slug)}"

    _run(["git", "clone", token_url, str(worktree)], redact=(token,))
    # Fetch/checkout the base under the AUTHENTICATED origin first; only then
    # scrub the token. A private base cannot be fetched once origin is
    # de-tokenised, so the scrub must follow every auth'd fetch (OP-1846 fix —
    # the token window stays bounded to clone+fetch+checkout, before implement()).
    _run(["git", "fetch", "origin", base], cwd=worktree)
    _run(["git", "checkout", "-B", base, f"origin/{base}"], cwd=worktree)
    _run(["git", "checkout", "-b", branch], cwd=worktree)
    _run(["git", "remote", "set-url", "origin", clean_url], cwd=worktree)

    result = implement(worktree)
    if inspect.isawaitable(result):
        await result

    status = _run(["git", "status", "--porcelain"], cwd=worktree)
    if not (status.stdout or "").strip():
        return ContributionResult(branch=branch, pr=None, no_changes=True)

    _run(["git", "add", "-A"], cwd=worktree)
    _run(["git", "commit", "-m", _commit_message(ticket_key)], cwd=worktree)

    # open_contribution_pr is synchronous and resolves its credential via an
    # internal asyncio.run(), so it must NOT be invoked from this coroutine's
    # running loop (it raises "must be called from synchronous runner code").
    # Bridge through a worker thread (OP-1846 fix) — idiomatic for calling sync
    # blocking I/O from async code, and it leaves merged OP-1845 untouched.
    pr = await asyncio.to_thread(
        open_contribution_pr,
        worktree=worktree,
        branch=branch,
        base=base,
        title=f"{ticket_key}: {slug}",
        body=(
            f"Automated camviewpro contribution for {ticket_key}.\n\n"
            "Generated through the OP-1846 contribution orchestrator."
        ),
        git_account_ref=git_account_ref,
        tenant_id=tenant_id,
    )
    return ContributionResult(branch=branch, pr=pr, no_changes=False)
