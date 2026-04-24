"""Gerrit Code Review client — uses SSH CLI for all operations.

Authentication reuses the same SSH key configured for Git operations.
No HTTP API token required.

Phase 5-7 (#multi-account-forge) refactor (2026-04-24)
──────────────────────────────────────────────────────
Each public op now resolves a per-project account from the credential
registry (``backend.git_credentials.get_credential_registry_async`` /
:func:`pick_default`) instead of reading ``settings.gerrit_*`` scalars.
Strategy:

1. If ``project`` is supplied AND a ``git_accounts(platform='gerrit')``
   row has the same ``project`` value (case-insensitive exact match),
   use that account's SSH host/port/key/project.
2. Otherwise fall back to :func:`pick_default("gerrit")`, which honours
   the ``is_default`` flag in ``git_accounts`` and (via the legacy
   shim's synthesised ``default-gerrit`` virtual row) the legacy
   ``settings.gerrit_*`` scalars.
3. Returns an ``{"error": "Gerrit not configured"}`` payload if no
   gerrit account exists in the registry AND the legacy scalar
   fallback is empty.

This means a single deployment can run multiple Gerrit instances —
each project routed to the right SSH host with the right key — while
still working transparently for the single-instance / scalar-only
deployment that ``5-5`` auto-migration converted into a single
``git_accounts`` row.

Module-global audit (SOP Step 1, qualified answer #1)
─────────────────────────────────────────────────────
The module-level singleton ``gerrit_client`` is a stateless
``GerritClient`` instance — no per-call cache, no shared mutable
state. Each call resolves the account fresh via the async pool
(or the per-process shim cache, which is identical across workers
by construction). Cross-worker coherence is therefore preserved.

Read-after-write audit (SOP Step 1)
───────────────────────────────────
No new write paths added. The resolver's optional ``last_used_at``
touch (Phase 5-3) is best-effort and does not change Gerrit op
behaviour.

Usage::

    from backend.gerrit import gerrit_client
    change = await gerrit_client.query_change("I1234abcd")
    await gerrit_client.post_review("I1234abcd", 1, "LGTM", {"Code-Review": 1})
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from backend.config import settings

logger = logging.getLogger(__name__)

_SSH_TIMEOUT = 30  # seconds


class GerritClient:
    """Gerrit operations via SSH CLI (port 29418).

    Phase 5-7: every op resolves a per-project account before building
    the SSH argv. See module docstring for the resolution strategy.
    """

    async def _resolve_account(self, project: str = "") -> dict | None:
        """Pick the right ``git_accounts(platform='gerrit')`` row.

        Resolution order:

        1. If *project* is non-empty, scan the tenant-scoped registry
           for a gerrit row whose ``project`` field matches
           case-insensitive — direct hit for multi-project tenants.
        2. Fall back to :func:`backend.git_credentials.pick_default`
           (``platform='gerrit'``) which honours ``is_default=TRUE``
           and the legacy shim's synthesised ``default-gerrit`` row.

        Returns ``None`` when no gerrit account is configured in
        either source. Callers should surface ``"Gerrit not
        configured"`` to the user in that case.
        """
        from backend.git_credentials import (
            get_credential_registry_async, pick_default,
        )

        if project:
            needle = project.strip().lower()
            try:
                registry = await get_credential_registry_async()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "GerritClient._resolve_account: registry read failed (%s)",
                    type(exc).__name__,
                )
                registry = []
            for entry in registry:
                if entry.get("platform") != "gerrit":
                    continue
                if (entry.get("project") or "").strip().lower() == needle:
                    return entry

        return await pick_default("gerrit", touch=False)

    def _ssh_args_for(self, account: dict) -> list[str]:
        """Build the base SSH argument list from a resolved account row.

        Prefers per-account ``ssh_key`` (from ``git_accounts``) and
        falls back to the legacy global ``settings.git_ssh_key_path``
        when the account does not carry its own key (the common case
        for the auto-migrated ``ga-legacy-gerrit-*`` row, which the
        Phase-5-5 migration leaves blank by design — operator must
        paste keys via the Phase-5-9 UI).
        """
        args = ["ssh"]
        key_path = account.get("ssh_key") or settings.git_ssh_key_path
        if key_path:
            args.extend(["-i", str(Path(key_path).expanduser())])
        port = int(account.get("ssh_port") or 29418)
        host = account.get("ssh_host") or ""
        args.extend([
            "-p", str(port),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
            host,
        ])
        return args

    async def _ssh_with(
        self, account: dict, cmd: str,
    ) -> tuple[int, str, str]:
        """Execute a Gerrit SSH command with a resolved account (shell-free)."""
        import shlex
        args = self._ssh_args_for(account) + shlex.split(cmd)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_SSH_TIMEOUT,
        )
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )

    def _project_for(self, account: dict, project: str) -> str:
        """Resolve the project to use: explicit → account row → empty."""
        return (project or account.get("project") or "").strip()

    # ─── Query ───

    async def query_change(self, change_id: str, project: str = "") -> dict | None:
        """Query a Gerrit change by Change-Id or change number.

        Returns parsed JSON dict or None if not found.
        """
        account = await self._resolve_account(project)
        if account is None:
            logger.warning("Gerrit query: no account configured")
            return None
        rc, out, err = await self._ssh_with(
            account,
            f'gerrit query --format=JSON --current-patch-set "change:{change_id}"',
        )
        if rc != 0:
            logger.warning("Gerrit query failed: %s", err)
            return None
        # Gerrit returns one JSON object per line; last line is stats
        lines = [l for l in out.splitlines() if l.strip()]
        if not lines:
            return None
        try:
            return json.loads(lines[0])
        except json.JSONDecodeError:
            return None

    async def query_open_changes(self, project: str = "") -> list[dict]:
        """List open changes for the project."""
        account = await self._resolve_account(project)
        if account is None:
            return []
        proj = self._project_for(account, project)
        if not proj:
            return []
        rc, out, _ = await self._ssh_with(
            account,
            f'gerrit query --format=JSON "project:{proj} status:open"',
        )
        if rc != 0:
            return []
        results = []
        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if "id" in obj:  # Skip stats line
                    results.append(obj)
            except json.JSONDecodeError:
                continue
        return results

    # ─── Review ───

    async def post_review(
        self,
        commit: str,
        message: str = "",
        labels: dict[str, int] | None = None,
        project: str = "",
    ) -> dict:
        """Post a review (score + message) on a commit.

        Args:
            commit: Git commit SHA of the patchset.
            message: Review comment message.
            labels: Label scores, e.g. ``{"Code-Review": 1}``.
            project: Gerrit project (defaults to the resolved account's
                ``project`` field).
        """
        account = await self._resolve_account(project)
        if account is None:
            return {"error": "Gerrit not configured"}
        proj = self._project_for(account, project)
        parts = [f'gerrit review --project "{proj}"']

        if message:
            safe_msg = message.replace('"', '\\"').replace("\n", "\\n")
            parts.append(f'--message "{safe_msg}"')

        for label, score in (labels or {}).items():
            sign = f"+{score}" if score > 0 else str(score)
            parts.append(f"--label {label}={sign}")

        parts.append(commit)

        rc, out, err = await self._ssh_with(account, " ".join(parts))
        if rc != 0:
            return {"error": err or out}
        return {"status": "ok", "commit": commit}

    async def post_inline_comments(
        self,
        commit: str,
        comments: dict[str, list[dict]],
        project: str = "",
    ) -> dict:
        """Post inline comments via Gerrit SSH review command.

        Passes the JSON payload via stdin to avoid temp file issues
        (SSH runs remotely, can't see local files).

        Args:
            commit: Git commit SHA.
            comments: ``{filepath: [{line: int, message: str}, ...]}``.
            project: Gerrit project.
        """
        account = await self._resolve_account(project)
        if account is None:
            return {"error": "Gerrit not configured"}
        proj = self._project_for(account, project)
        payload = json.dumps({
            "labels": {},
            "comments": {
                filepath: [
                    {"line": c["line"], "message": c["message"]}
                    for c in file_comments
                ]
                for filepath, file_comments in comments.items()
            },
        })

        # Pass JSON via stdin (shell-free)
        args = self._ssh_args_for(account) + [
            "gerrit", "review", "--project", proj, "--json", "-", commit,
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=payload.encode()), timeout=_SSH_TIMEOUT,
        )
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()

        if proc.returncode != 0:
            return {"error": err or out}
        return {"status": "ok", "commit": commit, "comment_count": sum(len(v) for v in comments.values())}

    # ─── Submit ───

    async def submit_change(self, commit: str, project: str = "") -> dict:
        """Submit (merge) a change. Requires appropriate permissions."""
        account = await self._resolve_account(project)
        if account is None:
            return {"error": "Gerrit not configured"}
        proj = self._project_for(account, project)
        rc, out, err = await self._ssh_with(
            account,
            f'gerrit review --project "{proj}" --submit {commit}',
        )
        if rc != 0:
            return {"error": err or out}
        return {"status": "submitted", "commit": commit}

    # ─── Set reviewer ───

    async def set_reviewer(
        self,
        change: str,
        reviewer: str,
        project: str = "",
    ) -> dict:
        """Add (or remove) a reviewer on a change via ``gerrit set-reviewers``.

        Args:
            change: Change-Id, change number, or commit SHA.
            reviewer: Reviewer username or email. Prefix with ``-`` to
                remove instead of add (matches Gerrit CLI semantics).
            project: Gerrit project (defaults to resolved account's
                ``project``).
        """
        account = await self._resolve_account(project)
        if account is None:
            return {"error": "Gerrit not configured"}
        proj = self._project_for(account, project)
        action = "--remove" if reviewer.startswith("-") else "--add"
        target = reviewer.lstrip("-")
        cmd_parts = ["gerrit", "set-reviewers", action, target]
        if proj:
            cmd_parts.extend(["--project", proj])
        cmd_parts.append(change)
        rc, out, err = await self._ssh_with(
            account, " ".join(f'"{p}"' if " " in p else p for p in cmd_parts),
        )
        if rc != 0:
            return {"error": err or out}
        return {"status": "ok", "change": change, "reviewer": target}

    # ─── Connectivity test ───

    async def test_connection(self, project: str = "") -> dict:
        """Test SSH connectivity to Gerrit."""
        account = await self._resolve_account(project)
        if account is None or not (account.get("ssh_host") or "").strip():
            return {"status": "not_configured"}
        rc, out, err = await self._ssh_with(account, "gerrit version")
        if rc != 0:
            return {"status": "error", "message": err}
        return {"status": "ok", "version": out}


# Singleton
gerrit_client = GerritClient()
