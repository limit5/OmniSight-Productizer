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
import os
from pathlib import Path

from backend.config import settings

logger = logging.getLogger(__name__)

_SSH_TIMEOUT = 30  # seconds


class GerritClient:
    """Gerrit operations via SSH CLI (port 29418).

    Phase 5-7: every op resolves a per-project account before building
    the SSH argv. See module docstring for the resolution strategy.
    """

    # OP-1190 prewarm cache. Populated by ``prewarm_for_daemon`` from
    # the gerrit-jira-bridge daemon's main event loop AFTER
    # ``db_pool.init_pool`` has succeeded. Once set, ``_resolve_account``
    # serves from this cache instead of touching the asyncpg pool — which
    # is required because the daemon's per-event handler threads call
    # ``asyncio.run(...)`` and therefore run in a different event loop
    # than the one the pool was created in.
    #
    # Class-level state (not instance) so the module-global
    # ``gerrit_client`` singleton and any test instances share the same
    # warm cache. None means "no cache; fall back to async DB read".
    _cached_registry: list[dict] | None = None
    _cached_default: dict | None = None

    @classmethod
    async def prewarm_for_daemon(cls) -> None:
        """OP-1190 + OP-1193 — populate the daemon's gerrit-account cache.

        Background — the layered fix
        ----------------------------
        OP-1190 introduced this method to sidestep the asyncpg
        "different loop" RuntimeError raised when a per-event worker
        thread (spawned by ``_handle_patchset_created``) calls
        ``asyncio.run(_proactive_merger_check(event))`` and the inner
        ``_resolve_account`` tries to acquire from the pool bound to
        the daemon's main loop. The fix populates a class-level cache
        in the main loop, then ``_resolve_account`` serves the cache
        synchronously without ever calling ``pool.acquire()``.

        OP-1193 rewrites the cache SOURCE (this method) — the
        OP-1190-era implementation read from the DB via
        ``pick_default("gerrit")`` and stashed whatever first-default
        row it found. But the only gerrit row in ``git_accounts`` is
        ``merger-agent-bot``'s (ga-23a020393575, from OP-693), and
        loading it into the daemon's account slot conflated identities:
        the daemon (claude-bot, with its own SSH key + ``user@host``
        in env) ended up authenticating reads against Gerrit AS the
        merger-bot — and crashed in practice because the row's
        ``ssh_host`` lacks the ``user@`` prefix and ``ssh_key`` is the
        decrypted PEM content rather than a filesystem path. The
        2026-05-17 OP-1185 re-run (Gerrit Change #716, abandoned)
        observed ``Gerrit query failed: user@sora.services: Permission
        denied (publickey)`` — local-OS-user fallback because no
        ``user@`` prefix was present.

        New design — env-sourced identity
        ---------------------------------
        Build the cached default account from THE DAEMON'S OWN env
        (``OMNISIGHT_GERRIT_SSH_HOST`` carries ``claude-bot@host``,
        ``OMNISIGHT_GIT_SSH_KEY_PATH`` is a real filesystem path,
        ``OMNISIGHT_GERRIT_PROJECT`` is the project root). This mirrors
        the legacy ``default-gerrit (legacy scalar)`` row that
        ``backend.git_credentials`` synthesises when no real DB row
        exists — see ``git_credentials.py`` around the
        ``entry_id="default-gerrit"`` shim block. The DB rows remain
        reachable for OTHER consumers (e.g., merger-bot's own resolution
        push) via explicit ``pick_account_for_url`` / ``pick_by_id``
        calls; only this daemon-identity cache changes.

        Pool init is no longer a prerequisite — kept the call site
        after ``db_pool.init_pool`` only because other daemon
        subsystems (audit log, JIRA write) still need the pool.

        Idempotent: safe to call multiple times. The cache is reset
        only by an explicit ``invalidate_prewarm_cache`` call — daemon
        restart re-runs this method.
        """
        ssh_host = (
            (settings.gerrit_ssh_host or "").strip()
            or os.environ.get("OMNISIGHT_GERRIT_SSH_HOST", "").strip()
        )
        ssh_port_str = (
            str(settings.gerrit_ssh_port).strip()
            if settings.gerrit_ssh_port
            else os.environ.get("OMNISIGHT_GERRIT_SSH_PORT", "29418").strip()
        )
        try:
            ssh_port = int(ssh_port_str or "29418")
        except ValueError:
            ssh_port = 29418
        ssh_key = (
            (settings.git_ssh_key_path or "").strip()
            or os.environ.get("OMNISIGHT_GIT_SSH_KEY_PATH", "").strip()
        )
        project = (
            (settings.gerrit_project or "").strip()
            or os.environ.get("OMNISIGHT_GERRIT_PROJECT", "").strip()
        )

        if not ssh_host:
            logger.warning(
                "GerritClient.prewarm_for_daemon: no SSH host configured "
                "(settings.gerrit_ssh_host empty AND OMNISIGHT_GERRIT_SSH_HOST "
                "env unset) — cache stays empty; daemon worker threads will "
                "fall back to the async DB path and likely hit OP-1190 or "
                "the merger-bot-identity mismatch."
            )
            return

        account: dict = {
            "id": "daemon-env",
            "platform": "gerrit",
            "ssh_host": ssh_host,        # contract: 'user@host' or bare 'host'
            "ssh_port": ssh_port,
            "ssh_key": ssh_key,          # contract: filesystem PATH (not PEM)
            "project": project,
            "is_default": True,
        }
        cls._cached_default = account
        cls._cached_registry = [account]
        logger.info(
            "GerritClient.prewarm_for_daemon: cache populated from env "
            "(ssh_host=%s ssh_port=%d project=%s ssh_key=%s)",
            ssh_host, ssh_port, project,
            "set" if ssh_key else "MISSING",
        )

    @classmethod
    def invalidate_prewarm_cache(cls) -> None:
        """OP-1190 — clear the prewarm cache.

        Tests use this between cases to force a fresh resolution. Prod
        code shouldn't need it: the daemon re-prewarms on each restart,
        and credential rotation requires a daemon restart anyway.
        """
        cls._cached_registry = None
        cls._cached_default = None

    def _resolve_account_from_cache(self, project: str = "") -> dict | None:
        """OP-1190 — synchronous account resolution using the prewarm cache.

        Returns ``None`` if the cache is not populated (caller must fall
        back to the async DB-read path). Returns the resolved account
        dict otherwise, applying the same project-aware → default
        precedence as :meth:`_resolve_account`.
        """
        registry = type(self)._cached_registry
        if registry is None:
            return None
        if project:
            needle = project.strip().lower()
            for entry in registry:
                if entry.get("platform") != "gerrit":
                    continue
                if (entry.get("project") or "").strip().lower() == needle:
                    return entry
        return type(self)._cached_default

    async def _resolve_account(self, project: str = "") -> dict | None:
        """Pick the right ``git_accounts(platform='gerrit')`` row.

        Resolution order:

        0. **OP-1190** — if :meth:`prewarm_for_daemon` has populated the
           class-level cache, serve from it synchronously. This is the
           daemon-worker-thread path: the asyncpg pool is bound to the
           daemon's main loop and cannot be acquired from per-event
           ``asyncio.run`` worker loops.
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
        cached = self._resolve_account_from_cache(project)
        if cached is not None or type(self)._cached_registry is not None:
            # Cache populated → trust it. cached==None means "registry was
            # cached but no gerrit row matches", which is the same answer
            # the async path would give without burning a pool acquire.
            return cached

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

    # ─── Hashtag set (OP-714 / OP-694) ───

    async def add_hashtag(
        self,
        *,
        change_id: str,
        project: str = "",
        hashtag: str,
    ) -> dict:
        """Add a single hashtag to ``change_id``.

        Uses Gerrit's SSH ``set-hashtags`` command (idempotent: adding
        an existing hashtag is a no-op). Returns ``{"status": "ok"}``
        on success, ``{"error": ...}`` otherwise.

        Used by:
          * OP-694 — merger sets ``Merge-Conflict-Resolved`` after a
            successful conflict-resolution patchset.
          * OP-714 — proactive merger trigger sets
            ``Merger-Proactive-PS<n>`` to throttle re-fires on the
            same patchset.
        """
        account = await self._resolve_account(project)
        if account is None:
            return {"error": "Gerrit not configured"}
        # Gerrit SSH set-hashtags expects --add <tag> [--add <tag> ...].
        # Hashtag values may not contain whitespace per Gerrit's rules,
        # so simple shlex-style quoting is enough.
        from shlex import quote as _q
        cmd = (
            f"gerrit set-hashtags {_q(change_id)} "
            f"--add {_q(hashtag)}"
        )
        rc, out, err = await self._ssh_with(account, cmd)
        if rc != 0:
            return {"error": err or out or f"set-hashtags rc={rc}"}
        return {"status": "ok", "change": change_id, "hashtag": hashtag}

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
