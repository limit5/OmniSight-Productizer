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
import concurrent.futures
import fcntl
import json
import os
import re
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.agents.auto_resolve_config import (
    AutoResolveRule,
    load_auto_resolve_config,
)

OP_KEY_RE = re.compile(r"\b(OP-\d+)\b")

GERRIT_REST_BASE_URL = "https://sora.services:29420"
DEFAULT_SWEEP_DEBOUNCE_SECONDS = 30.0
DEFAULT_REBASE_CONCURRENCY = 2
REBASE_CONCURRENCY_ENV = "OMNISIGHT_REBASE_CONCURRENCY"
SWEEP_MAX_WORKERS = 4

SWEEP_DISABLE_ENV = "OMNISIGHT_AUTO_REBASE_DISABLED"
REPO_ROOT = Path(__file__).resolve().parents[2]
AUTO_RESOLVE_PATH = Path(".gerrit/auto-resolve.yaml")
AUTO_RESOLVE_LOCK_DIR = Path("~/.cache/omnisight/auto-resolve-locks").expanduser()
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


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
    auto_resolved: tuple[str, ...] = ()


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
        audit_recorder: Callable[[dict[str, Any]], None] | None = None,
        repo_root: Path = REPO_ROOT,
        auto_resolve_config_path: Path = AUTO_RESOLVE_PATH,
        local_rebase_runner: Callable[..., RebaseResult] | None = None,
        log: Callable[..., None] | None = None,
        rebase_concurrency: int | None = None,
    ) -> None:
        self._ssh_cmd_builder = ssh_cmd_builder
        self._ssh_env_builder = ssh_env_builder
        self._run_command = run_command
        self._rest_base_url = rest_base_url.rstrip("/")
        self._urlopen = urlopen
        self._load_password = load_password
        self._notify_jira = notify_jira
        self._audit_recorder = audit_recorder
        self._repo_root = repo_root
        self._auto_resolve_config_path = auto_resolve_config_path
        self._local_rebase_runner = local_rebase_runner
        self._log: Callable[..., None] = log or (lambda *args, **kwargs: None)
        self._rebased_onto: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._rebase_concurrency = self._resolve_rebase_concurrency(
            rebase_concurrency,
        )
        self._rebase_tokens = threading.Semaphore(
            value=self._rebase_concurrency,
        )

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
        worker_count = min(SWEEP_MAX_WORKERS, max(1, len(open_changes)))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_count,
        ) as executor:
            futures = [
                executor.submit(
                    self._attempt_rebase_with_token, change, merged_sha,
                )
                for change in open_changes
            ]
            for future in futures:
                result = future.result()
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

    def _attempt_rebase_with_token(
        self, change: dict[str, Any], target_sha: str,
    ) -> RebaseResult:
        with self._rebase_tokens:
            return self.attempt_rebase(change, target_sha=target_sha)

    def _resolve_rebase_concurrency(self, value: int | None) -> int:
        if value is not None:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                parsed = DEFAULT_REBASE_CONCURRENCY
            return max(1, parsed)
        raw = os.environ.get(REBASE_CONCURRENCY_ENV, "").strip()
        if not raw:
            return DEFAULT_REBASE_CONCURRENCY
        try:
            parsed = int(raw)
        except ValueError:
            self._log(
                "WARN", "auto_rebase_concurrency_invalid_env",
                env=REBASE_CONCURRENCY_ENV, value=raw,
                default=DEFAULT_REBASE_CONCURRENCY,
            )
            return DEFAULT_REBASE_CONCURRENCY
        if parsed < 1:
            self._log(
                "WARN", "auto_rebase_concurrency_invalid_env",
                env=REBASE_CONCURRENCY_ENV, value=raw,
                default=DEFAULT_REBASE_CONCURRENCY,
            )
            return DEFAULT_REBASE_CONCURRENCY
        return parsed

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
            auto_resolvers = self._load_auto_resolvers()
            auto_handled = [
                f for f in files if f in auto_resolvers
            ]
            if files and set(files) == set(auto_handled):
                result = self._run_local_auto_resolve(
                    change,
                    target_sha,
                    tuple(auto_handled),
                    auto_resolvers,
                )
                if result.success:
                    with self._lock:
                        self._rebased_onto.add(session_key)
                    self._post_auto_resolve_jira_notice(
                        change,
                        target_sha,
                        result.auto_resolved,
                        auto_resolvers,
                    )
                    self._record_auto_resolve_audit(change, result, auto_resolvers)
                    return result
                self._post_auto_resolve_failed_jira_notice(change, result)
                self._log(
                    "WARN",
                    "auto_rebase_auto_resolve_failed",
                    change_id=change_number,
                    files=files,
                    err=result.error,
                )
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

    def _load_auto_resolvers(self) -> dict[str, AutoResolveRule]:
        try:
            return load_auto_resolve_config(
                self._repo_root / self._auto_resolve_config_path,
                log=self._log,
            )
        except Exception as exc:
            self._log(
                "WARN", "auto_resolve_config_load_failed",
                err=f"{type(exc).__name__}: {exc}",
            )
            return {}

    def _run_local_auto_resolve(
        self,
        change: dict[str, Any],
        target_sha: str,
        files: tuple[str, ...],
        resolvers: dict[str, AutoResolveRule],
    ) -> RebaseResult:
        runner = self._local_rebase_runner or local_rebase_with_resolvers
        try:
            return runner(
                change=change,
                target_sha=target_sha,
                files=files,
                resolvers=resolvers,
                repo_root=self._repo_root,
                run_command=self._run_command,
                log=self._log,
            )
        except Exception as exc:  # noqa: BLE001
            return RebaseResult(
                change_number=str(change.get("number") or ""),
                conflict=True,
                files=files,
                error=f"{type(exc).__name__}: {exc}",
            )

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

    def _post_auto_resolve_jira_notice(
        self,
        change: dict[str, Any],
        target_sha: str,
        files: tuple[str, ...],
        resolvers: dict[str, AutoResolveRule],
    ) -> None:
        if self._notify_jira is None:
            return
        ticket_key = self._extract_ticket_key(change)
        if not ticket_key:
            return
        details = ", ".join(
            f"{path} via {Path(resolvers[path].resolver).name}"
            for path in files
            if path in resolvers
        )
        message = (
            f"[auto-resolve] R3 regenerated {details} to handle merge "
            f"conflict against develop tip {target_sha}"
        )
        try:
            self._notify_jira(ticket_key, message)
        except Exception as exc:
            self._log("WARN", "auto_rebase_jira_notify_failed",
                      ticket_key=ticket_key,
                      err=f"{type(exc).__name__}: {exc}")

    def _post_auto_resolve_failed_jira_notice(
        self, change: dict[str, Any], result: RebaseResult,
    ) -> None:
        if self._notify_jira is None:
            return
        ticket_key = self._extract_ticket_key(change)
        if not ticket_key:
            return
        files = ", ".join(result.files) if result.files else "registered files"
        message = (
            f"[auto-resolve] R3 could not regenerate {files}; falling back "
            f"to manual conflict handling. Reason: {result.error or 'unknown'}"
        )
        try:
            self._notify_jira(ticket_key, message)
        except Exception as exc:
            self._log("WARN", "auto_rebase_jira_notify_failed",
                      ticket_key=ticket_key,
                      err=f"{type(exc).__name__}: {exc}")

    def _extract_ticket_key(self, change: dict[str, Any]) -> str | None:
        subject = str(change.get("subject") or "")
        match = OP_KEY_RE.search(subject)
        return match.group(1) if match else None

    def _record_auto_resolve_audit(
        self,
        change: dict[str, Any],
        result: RebaseResult,
        resolvers: dict[str, AutoResolveRule],
    ) -> None:
        change_id = str(change.get("id") or change.get("change_id") or "")
        ps = str((change.get("currentPatchSet") or {}).get("number") or "")
        for file_path in result.auto_resolved:
            rule = resolvers.get(file_path)
            record = {
                "event": "auto_resolve.generated_file",
                "change_id": change_id,
                "change_number": result.change_number,
                "ps": ps,
                "file": file_path,
                "resolver": rule.resolver if rule else "",
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }
            self._log("INFO", "auto_resolve_audit", **record)
            if self._audit_recorder is not None:
                try:
                    self._audit_recorder(record)
                except Exception as exc:  # noqa: BLE001
                    self._log(
                        "WARN", "auto_resolve_audit_record_failed",
                        change_id=result.change_number,
                        file=file_path,
                        err=f"{type(exc).__name__}: {exc}",
                    )

    def _emit_result_log(self, result: RebaseResult) -> None:
        if result.success:
            self._log("INFO", "auto_rebase_success",
                      change_id=result.change_number,
                      new_revision=result.new_revision,
                      auto_resolved=list(result.auto_resolved))
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


# ── Local generated-file resolver ────────────────────────────────────


def local_rebase_with_resolvers(
    *,
    change: dict[str, Any],
    target_sha: str,
    files: tuple[str, ...],
    resolvers: dict[str, AutoResolveRule],
    repo_root: Path = REPO_ROOT,
    run_command: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    log: Callable[..., None] | None = None,
) -> RebaseResult:
    """Fetch a PS into a temp clone, rebase, regenerate files, push PS+1."""
    logger = log or (lambda *args, **kwargs: None)
    change_number = str(change.get("number") or "")
    change_id = str(change.get("id") or change.get("change_id") or "")
    patchset = change.get("currentPatchSet") or {}
    ps_number = str(patchset.get("number") or "")
    ps_ref = str(patchset.get("ref") or "")
    if not ps_ref and change_number and ps_number:
        ps_ref = f"refs/changes/{change_number[-2:]}/{change_number}/{ps_number}"
    if not ps_ref:
        return RebaseResult(
            change_number=change_number,
            conflict=True,
            files=files,
            error="missing_patchset_ref",
        )

    lock_names = [path.replace("/", "_") for path in sorted(files)]
    with _auto_resolve_file_locks(lock_names):
        with tempfile.TemporaryDirectory(prefix="omnisight-auto-resolve-") as tmp:
            worktree = Path(tmp) / "repo"
            try:
                clone_url = _origin_url(run_command, repo_root) or str(repo_root)
                _run_git(
                    run_command,
                    ["git", "clone", "--quiet", clone_url, str(worktree)],
                    cwd=repo_root,
                )
                _run_git(
                    run_command,
                    ["git", "fetch", "--quiet", "origin", ps_ref],
                    cwd=worktree,
                )
                _run_git(
                    run_command,
                    ["git", "checkout", "--quiet", "FETCH_HEAD"],
                    cwd=worktree,
                )
                rebase = run_command(
                    ["git", "rebase", target_sha],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if rebase.returncode == 0:
                    return RebaseResult(
                        change_number=change_number,
                        success=True,
                        auto_resolved=(),
                    )

                for file_path in files:
                    _run_git(
                        run_command,
                        ["git", "checkout", "--ours", "--", file_path],
                        cwd=worktree,
                    )
                    rule = resolvers[file_path]
                    before = _status_paths(run_command, worktree)
                    _run_resolver(run_command, worktree, rule.resolver)
                    after = _status_paths(run_command, worktree)
                    touched = after - before
                    unexpected = touched - {file_path}
                    if unexpected:
                        raise RuntimeError(
                            "resolver touched unregistered files: "
                            + ", ".join(sorted(unexpected)),
                        )
                    _reject_conflict_markers(worktree / file_path)

                _run_git(run_command, ["git", "add", "--", *files], cwd=worktree)
                env = os.environ.copy()
                env["GIT_EDITOR"] = "true"
                _run_git(
                    run_command,
                    ["git", "rebase", "--continue"],
                    cwd=worktree,
                    env=env,
                    timeout=120,
                )
                _run_git(
                    run_command,
                    ["git", "push", "origin", "HEAD:refs/for/develop"],
                    cwd=worktree,
                    timeout=120,
                )
            except Exception as exc:  # noqa: BLE001
                logger(
                    "WARN",
                    "auto_resolve_local_rebase_failed",
                    change_id=change_id,
                    change_number=change_number,
                    err=f"{type(exc).__name__}: {exc}",
                )
                return RebaseResult(
                    change_number=change_number,
                    conflict=True,
                    files=files,
                    error=f"{type(exc).__name__}: {exc}",
                )

    return RebaseResult(
        change_number=change_number,
        success=True,
        auto_resolved=files,
    )


class _auto_resolve_file_locks:
    def __init__(self, names: list[str]) -> None:
        self._names = names
        self._fds: list[int] = []

    def __enter__(self) -> "_auto_resolve_file_locks":
        AUTO_RESOLVE_LOCK_DIR.mkdir(parents=True, exist_ok=True)
        for name in self._names:
            path = AUTO_RESOLVE_LOCK_DIR / f"{name}.lock"
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except Exception:
                os.close(fd)
                raise
            self._fds.append(fd)
        return self

    def __exit__(self, *exc: Any) -> None:
        for fd in reversed(self._fds):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._fds.clear()


def _run_git(
    run_command: Callable[..., subprocess.CompletedProcess],
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int = 60,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    result = run_command(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise RuntimeError(f"{' '.join(cmd)} failed: {detail}")
    return result


def _run_resolver(
    run_command: Callable[..., subprocess.CompletedProcess],
    worktree: Path,
    resolver: str,
) -> None:
    cmd = ["python3", resolver] if resolver.endswith(".py") else [resolver]
    _run_git(run_command, cmd, cwd=worktree, timeout=120)


def _origin_url(
    run_command: Callable[..., subprocess.CompletedProcess],
    repo_root: Path,
) -> str:
    result = run_command(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _status_paths(
    run_command: Callable[..., subprocess.CompletedProcess],
    worktree: Path,
) -> set[str]:
    result = _run_git(
        run_command,
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=worktree,
    )
    paths: set[str] = set()
    for line in (result.stdout or "").splitlines():
        if not line:
            continue
        paths.add(line[3:].strip())
    return paths


def _reject_conflict_markers(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for marker in CONFLICT_MARKERS:
        if marker in text:
            raise RuntimeError(f"{path}: unresolved conflict marker {marker}")


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
