"""Auto-rebase open Gerrit PSes onto develop after a change merges.

OP-733 — extends the OP-714/OP-715 stream-events daemon. When a
``change-merged`` event arrives on ``develop``, sweep all open bot-owned
PSes and try to rebase each one onto the new tip. PSes that rebase
cleanly get a fresh PS uploaded; PSes that conflict are left alone for
OP-720 Phase 4's stale-PS scanner to handle after 4h.

Why REST (not SSH): Gerrit's rebase endpoint is HTTP-only —
``POST /a/changes/{id}/revisions/current/rebase``. SSH does not expose
rebase. The REST call needs HTTP Basic auth as the change's owner so
Gerrit's per-change write-permission check passes; we read the owner's
HTTP password from a per-bot key-bag (OP-720 Phase 3 Option A pattern).

Why debounce: a batch +2 from the operator can fire 5+ change-merged
events in <1 s. Without a debounce we'd run the full sweep N times,
hammering Gerrit. The :class:`DebouncedSweepScheduler` aggregates
events for ``DEFAULT_SWEEP_DEBOUNCE_SECONDS`` and fires the runner once
on the latest merged SHA.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

OP_KEY_RE = re.compile(r"\b(OP-\d+)\b")

GERRIT_REST_BASE_URL = "https://sora.services:29420"
DEFAULT_SWEEP_DEBOUNCE_SECONDS = 30.0

SWEEP_DISABLE_ENV = "OMNISIGHT_AUTO_REBASE_DISABLED"


# ── Owner-key dispatch (OP-720 Phase 3 Option A pattern) ──────────────

OWNER_HTTP_PASSWORD_PATHS: dict[str, Path] = {
    "claude-bot": Path(
        "~/.config/omnisight/gerrit-claude-bot-http-password"
    ).expanduser(),
    "codex-bot": Path(
        "~/.config/omnisight/gerrit-codex-bot-http-password"
    ).expanduser(),
}

# Fallback: same secret may live inside the per-bot env file (legacy
# layout). The dedicated password file in the dict above is preferred.
OWNER_ENV_FALLBACK: dict[str, tuple[Path, str]] = {
    "claude-bot": (
        Path("~/.config/omnisight/gerrit-claude.env").expanduser(),
        "OMNISIGHT_GERRIT_CLAUDE_HTTP_PASSWORD",
    ),
    "codex-bot": (
        Path("~/.config/omnisight/gerrit-codex.env").expanduser(),
        "OMNISIGHT_GERRIT_CODEX_HTTP_PASSWORD",
    ),
}


def load_owner_http_password(username: str) -> str | None:
    """Resolve the HTTP Basic password for ``username`` or ``None``.

    Lookup order:

    1. ``~/.config/omnisight/gerrit-<bot>-bot-http-password`` (preferred).
    2. ``OMNISIGHT_GERRIT_<BOT>_HTTP_PASSWORD`` parsed from the per-bot
       env file (fallback for dev hosts that haven't been provisioned
       with the dedicated password file yet).

    Returns ``None`` when no source has the password — caller must skip
    the rebase for that owner (no graceful fallback: rebase requires
    write permission and we can only impersonate the change's owner).
    """
    path = OWNER_HTTP_PASSWORD_PATHS.get(username)
    if path is not None and path.exists():
        try:
            value = path.read_text().strip()
        except OSError:
            value = ""
        if value:
            return value
    fallback = OWNER_ENV_FALLBACK.get(username)
    if fallback is None:
        return None
    env_path, env_key = fallback
    if not env_path.exists():
        return None
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == env_key:
                value = value.strip()
                return value or None
    except OSError:
        return None
    return None


# ── RebaseResult + Sweeper ────────────────────────────────────────────


@dataclass(frozen=True)
class RebaseResult:
    """Outcome of a single rebase attempt."""

    change_number: str
    success: bool = False
    conflict: bool = False
    skipped: bool = False
    skip_reason: str = ""
    files: tuple[str, ...] = ()
    error: str = ""
    new_revision: str = ""


class AutoRebaseSweeper:
    """Sweep open bot-owned PSes onto a new develop tip.

    Stateless across sweeps except for an in-memory ``rebased_onto`` set
    used as a within-session belt-and-suspenders idempotency check. The
    primary idempotency mechanism is the patchset-parent compare:
    after a successful rebase the change's ``currentPatchSet.parents[0]``
    becomes the new base, so a second sweep on the same merged_sha
    naturally short-circuits via ``already_on_target_base``.
    """

    def __init__(
        self,
        *,
        ssh_cmd_builder: Callable[..., list[str]],
        ssh_env_builder: Callable[[], dict[str, str]],
        run_command: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        rest_base_url: str = GERRIT_REST_BASE_URL,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        load_password: Callable[[str], str | None] = load_owner_http_password,
        notify_jira: Callable[[str, str], None] | None = None,
        log: Callable[..., None] | None = None,
    ) -> None:
        self._ssh_cmd_builder = ssh_cmd_builder
        self._ssh_env_builder = ssh_env_builder
        self._run_command = run_command
        self._rest_base_url = rest_base_url.rstrip("/")
        self._urlopen = urlopen
        self._load_password = load_password
        self._notify_jira = notify_jira
        self._log: Callable[..., None] = log or (lambda *args, **kwargs: None)
        self._rebased_onto: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    # ── public API ──

    def sweep(self, *, project: str, merged_sha: str) -> list[RebaseResult]:
        """Query open PSes; attempt rebase on each; return per-PS results.

        Honours the ``OMNISIGHT_AUTO_REBASE_DISABLED`` env kill switch
        (operators can flip it to ``1`` to silence the daemon when
        Gerrit is having a bad day).
        """
        if os.environ.get(SWEEP_DISABLE_ENV, "").strip().lower() in {"1", "true", "yes"}:
            self._log("INFO", "auto_rebase_sweep_disabled_env",
                      project=project, merged_sha=merged_sha)
            return []
        if not project or not merged_sha:
            self._log("WARN", "auto_rebase_sweep_invalid_args",
                      project=project, merged_sha=merged_sha)
            return []

        self._log("INFO", "auto_rebase_sweep_start",
                  project=project, merged_sha=merged_sha)
        open_changes = self._query_open_bot_changes(project)
        results: list[RebaseResult] = []
        for change in open_changes:
            result = self.attempt_rebase(change, target_sha=merged_sha)
            results.append(result)
            self._emit_result_log(result)
        self._log("INFO", "auto_rebase_sweep_done",
                  project=project, merged_sha=merged_sha,
                  total=len(results),
                  succeeded=sum(1 for r in results if r.success),
                  conflicts=sum(1 for r in results if r.conflict),
                  skipped=sum(1 for r in results if r.skipped),
                  errors=sum(1 for r in results if r.error))
        return results

    def attempt_rebase(
        self, change: dict[str, Any], target_sha: str,
    ) -> RebaseResult:
        """Try to rebase ``change`` onto ``target_sha``.

        Skips and reasons:

        * ``code_review_plus_2`` — change is about to merge; leave alone.
        * ``no_password_for_owner:<u>`` — owner not in the HTTP key-bag.
        * ``already_on_target_base`` — current PS parent is already
          ``target_sha`` (catches OP-733 AC #7 idempotency naturally).
        * ``already_rebased_in_session`` — within-session dedupe.

        Outcomes:

        * ``success=True`` — REST 200/201; ``new_revision`` populated.
        * ``conflict=True`` — REST 409; ``files`` carries the parsed
          conflict list (heuristic — Gerrit returns plain text).
        * ``error`` — any other HTTP / I/O failure.
        """
        change_number = str(change.get("number") or "")
        change_id = str(change.get("id") or change.get("change_id") or "")
        owner_obj = change.get("owner") or {}
        owner = str(
            owner_obj.get("username")
            or owner_obj.get("name")
            or ""
        )

        if self._has_code_review_plus_2(change):
            return RebaseResult(
                change_number=change_number,
                skipped=True, skip_reason="code_review_plus_2",
            )

        if not owner:
            return RebaseResult(
                change_number=change_number,
                skipped=True, skip_reason="no_owner_username",
            )
        password = self._load_password(owner)
        if not password:
            return RebaseResult(
                change_number=change_number,
                skipped=True,
                skip_reason=f"no_password_for_owner:{owner}",
            )

        cps = change.get("currentPatchSet") or {}
        parents = cps.get("parents") or []
        parent_revs = [
            str(p.get("revision") or "")
            for p in parents
            if isinstance(p, dict)
        ]
        if target_sha and target_sha in parent_revs:
            return RebaseResult(
                change_number=change_number,
                skipped=True, skip_reason="already_on_target_base",
            )

        with self._lock:
            session_key = (change_number, target_sha)
            if session_key in self._rebased_onto:
                return RebaseResult(
                    change_number=change_number,
                    skipped=True,
                    skip_reason="already_rebased_in_session",
                )

        if not change_id:
            return RebaseResult(
                change_number=change_number,
                error="missing change_id",
            )

        rest_path = (
            f"/a/changes/{urllib.parse.quote(change_id, safe='')}"
            f"/revisions/current/rebase"
        )
        body = {"base": target_sha}
        try:
            status, payload = self._rest_post(
                rest_path, owner, password, body,
            )
        except Exception as exc:
            return RebaseResult(
                change_number=change_number,
                error=f"{type(exc).__name__}: {exc}",
            )

        if status in (200, 201):
            new_rev = self._parse_new_revision(payload)
            with self._lock:
                self._rebased_onto.add(session_key)
            self._post_rebase_jira_notice(change, target_sha)
            return RebaseResult(
                change_number=change_number,
                success=True, new_revision=new_rev,
            )

        if status == 409:
            files = self._parse_conflict_files(payload)
            return RebaseResult(
                change_number=change_number,
                conflict=True, files=tuple(files),
            )

        return RebaseResult(
            change_number=change_number,
            error=f"HTTP {status}: {self._summarise(payload)}",
        )

    # ── internals ──

    def _has_code_review_plus_2(self, change: dict[str, Any]) -> bool:
        cps = change.get("currentPatchSet") or {}
        for approval in cps.get("approvals") or []:
            if approval.get("type") != "Code-Review":
                continue
            try:
                if int(approval.get("value", 0)) >= 2:
                    return True
            except (TypeError, ValueError):
                continue
        # Top-level fallback for query payloads that surface labels there
        for record in change.get("submitRecords") or []:
            for label in record.get("labels") or []:
                if (
                    label.get("label") == "Code-Review"
                    and label.get("status") == "OK"
                ):
                    return True
        return False

    def _query_open_bot_changes(self, project: str) -> list[dict[str, Any]]:
        query = (
            f"status:open project:{project} "
            f"(owner:claude-bot OR owner:codex-bot)"
        )
        cmd = self._ssh_cmd_builder(
            "gerrit", "query", "--format=JSON",
            "--current-patch-set", "--all-approvals",
            query,
        )
        try:
            result = self._run_command(
                cmd,
                capture_output=True, text=True, timeout=30,
                env=self._ssh_env_builder(),
            )
        except Exception as exc:
            self._log("ERROR", "auto_rebase_query_failed",
                      err=f"{type(exc).__name__}: {exc}")
            return []
        if result.returncode != 0:
            self._log("ERROR", "auto_rebase_query_failed",
                      err=(result.stderr or "")[-500:])
            return []
        out: list[dict[str, Any]] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "stats":
                continue
            if not (obj.get("id") or obj.get("change_id")):
                continue
            out.append(obj)
        return out

    def _rest_post(
        self,
        path: str,
        owner_username: str,
        owner_password: str,
        body: dict[str, Any],
    ) -> tuple[int, str]:
        url = self._rest_base_url + path
        data = json.dumps(body).encode()
        token = base64.b64encode(
            f"{owner_username}:{owner_password}".encode(),
        ).decode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._urlopen(req, timeout=30) as resp:
                payload = resp.read().decode("utf-8", "replace")
                # Some HTTPResponse objects expose ``status``; older
                # ones expose ``code``. Honour both.
                code = getattr(resp, "status", None) or getattr(resp, "code", 200)
                return int(code), payload
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read().decode("utf-8", "replace") if exc.fp else ""
            except Exception:
                payload = ""
            return int(exc.code), payload

    def _parse_new_revision(self, payload: str) -> str:
        body = payload.lstrip(")]}'\n").strip()
        if not body:
            return ""
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            return ""
        return str(obj.get("current_revision") or "")

    def _parse_conflict_files(self, payload: str) -> list[str]:
        text = payload.strip()
        if not text:
            return []
        match = re.search(r"in:\s*(.+)", text)
        if match:
            return [
                s.strip() for s in match.group(1).split(",") if s.strip()
            ]
        return [text[:200]]

    def _summarise(self, payload: str) -> str:
        return payload.strip()[:200]

    def _post_rebase_jira_notice(
        self, change: dict[str, Any], target_sha: str,
    ) -> None:
        if self._notify_jira is None:
            return
        subject = str(change.get("subject") or "")
        match = OP_KEY_RE.search(subject)
        if not match:
            return
        ticket_key = match.group(1)
        message = (
            f"Auto-rebased onto {target_sha[:8]}; please re-review the "
            f"diff against the new base."
        )
        try:
            self._notify_jira(ticket_key, message)
        except Exception as exc:
            self._log("WARN", "auto_rebase_jira_notify_failed",
                      ticket_key=ticket_key,
                      err=f"{type(exc).__name__}: {exc}")

    def _emit_result_log(self, result: RebaseResult) -> None:
        if result.success:
            self._log("INFO", "auto_rebase_success",
                      change_id=result.change_number,
                      new_revision=result.new_revision)
        elif result.conflict:
            self._log("WARN", "auto_rebase_conflict",
                      change_id=result.change_number,
                      files=list(result.files))
            # OP-746 — record one conflict observation per failed rebase
            # so the daily report + dashboard tile can attribute the
            # event to "sibling merged on develop". Errors are swallowed
            # so telemetry doesn't break the sweeper.
            self._record_sibling_merged_observation(result)
        elif result.skipped:
            self._log("INFO", "auto_rebase_skipped",
                      change_id=result.change_number,
                      reason=result.skip_reason)
        elif result.error:
            self._log("ERROR", "auto_rebase_failed",
                      change_id=result.change_number,
                      err=result.error)

    def _record_sibling_merged_observation(self, result: RebaseResult) -> None:
        try:
            from datetime import datetime, timezone
            from backend.agents.conflict_observations import (
                ConflictObservation,
                record_observation_sync,
            )
            change_number: int | None
            try:
                change_number = int(result.change_number) if result.change_number else None
            except (TypeError, ValueError):
                change_number = None
            obs = ConflictObservation(
                ts=datetime.now(timezone.utc),
                cause_category="sibling_merged",
                files_in_conflict=tuple(result.files),
                ps_change_id=None,
                ps_change_number=change_number,
                ticket=None,
                pre_existing_open_count=0,
            )
            record_observation_sync(obs, log=self._log)
        except Exception as exc:  # noqa: BLE001
            self._log(
                "WARN", "sibling_merged_observation_record_failed",
                change_id=result.change_number,
                err=f"{type(exc).__name__}: {exc}",
            )


# ── DebouncedSweepScheduler ───────────────────────────────────────────


class DebouncedSweepScheduler:
    """Aggregate change-merged events; fire the runner once after a delay.

    Each :meth:`schedule` call (re)starts a Timer for ``delay_seconds``.
    If a second event arrives before the timer fires, the existing
    timer is cancelled and a new one is started — so a burst of merges
    produces exactly ONE sweep on the most recent merged SHA.

    The scheduler is thread-safe: multiple stream-events daemon threads
    can call :meth:`schedule` concurrently.
    """

    def __init__(
        self,
        *,
        runner: Callable[[str, str], Any],
        delay_seconds: float = DEFAULT_SWEEP_DEBOUNCE_SECONDS,
        timer_factory: Callable[..., threading.Timer] = threading.Timer,
        log: Callable[..., None] | None = None,
    ) -> None:
        self._runner = runner
        self._delay = float(delay_seconds)
        self._timer_factory = timer_factory
        self._log: Callable[..., None] = log or (lambda *args, **kwargs: None)
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._pending: tuple[str, str] | None = None

    def schedule(self, *, project: str, merged_sha: str) -> None:
        with self._lock:
            self._pending = (project, merged_sha)
            existing = self._timer
            if existing is not None:
                existing.cancel()
            timer = self._timer_factory(self._delay, self._fire)
            timer.daemon = True
            self._timer = timer
            timer.start()
        self._log(
            "INFO", "auto_rebase_sweep_scheduled",
            project=project, merged_sha=merged_sha,
            delay_s=self._delay,
        )

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = None
            self._pending = None

    def _fire(self) -> None:
        with self._lock:
            pending = self._pending
            self._pending = None
            self._timer = None
        if pending is None:
            return
        project, merged_sha = pending
        try:
            self._runner(project, merged_sha)
        except Exception as exc:
            self._log(
                "ERROR", "auto_rebase_sweep_runner_error",
                err=f"{type(exc).__name__}: {exc}",
            )
