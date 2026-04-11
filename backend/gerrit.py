"""Gerrit Code Review client — uses SSH CLI for all operations.

Authentication reuses the same SSH key configured for Git operations.
No HTTP API token required.

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
    """Gerrit operations via SSH CLI (port 29418)."""

    @property
    def _ssh_base(self) -> str:
        key_path = settings.git_ssh_key_path
        key_flag = f"-i {Path(key_path).expanduser()}" if key_path else ""
        return (
            f"ssh {key_flag} -p {settings.gerrit_ssh_port}"
            f" -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
            f" {settings.gerrit_ssh_host}"
        )

    async def _ssh(self, cmd: str) -> tuple[int, str, str]:
        """Execute a Gerrit SSH command."""
        full_cmd = f"{self._ssh_base} {cmd}"
        proc = await asyncio.create_subprocess_shell(
            full_cmd,
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

    # ─── Query ───

    async def query_change(self, change_id: str) -> dict | None:
        """Query a Gerrit change by Change-Id or change number.

        Returns parsed JSON dict or None if not found.
        """
        rc, out, err = await self._ssh(
            f'gerrit query --format=JSON --current-patch-set "change:{change_id}"'
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
        proj = project or settings.gerrit_project
        rc, out, err = await self._ssh(
            f'gerrit query --format=JSON "project:{proj} status:open"'
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
            project: Gerrit project (defaults to config).
        """
        proj = project or settings.gerrit_project
        parts = [f'gerrit review --project "{proj}"']

        if message:
            safe_msg = message.replace('"', '\\"').replace("\n", "\\n")
            parts.append(f'--message "{safe_msg}"')

        for label, score in (labels or {}).items():
            sign = f"+{score}" if score > 0 else str(score)
            parts.append(f"--label {label}={sign}")

        parts.append(commit)

        rc, out, err = await self._ssh(" ".join(parts))
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
        proj = project or settings.gerrit_project
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

        # Pass JSON via stdin: echo '<json>' | ssh gerrit review --json -
        import shlex
        ssh_cmd = f"{self._ssh_base} gerrit review --project {shlex.quote(proj)} --json - {commit}"
        proc = await asyncio.create_subprocess_shell(
            ssh_cmd,
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
        proj = project or settings.gerrit_project
        rc, out, err = await self._ssh(
            f'gerrit review --project "{proj}" --submit {commit}'
        )
        if rc != 0:
            return {"error": err or out}
        return {"status": "submitted", "commit": commit}

    # ─── Connectivity test ───

    async def test_connection(self) -> dict:
        """Test SSH connectivity to Gerrit."""
        if not settings.gerrit_ssh_host:
            return {"status": "not_configured"}
        rc, out, err = await self._ssh("gerrit version")
        if rc != 0:
            return {"status": "error", "message": err}
        return {"status": "ok", "version": out}


# Singleton
gerrit_client = GerritClient()
