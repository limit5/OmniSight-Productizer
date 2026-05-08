"""Submit queue worker — atomic rebase + CI + merge for ready PSes.

OP-751 H1 — replaces the manual "rebase, re-+2, hope-no-sibling-merged"
loop with a serialized submit queue (à la GitHub Merge Queue / Google
Rosie). The operator marks a Gerrit change ``Submit-Ready=+1``; this
daemon picks it up, rebases onto the current ``develop`` tip, runs CI
when OP-739 ships, and submits the change atomically. On any failure
in the pipeline the worker votes ``Submit-Ready=-1`` and posts a
comment; the operator fixes manually and re-marks ``+1`` to retry.

Why a queue: with N parallel PSes touching the same file, manual rebase
is O(N²). Serializing the merge step makes each PS rebase exactly once
at land time and removes the "sibling merged before my +2" race class
entirely.

Why a separate ``Submit-Ready`` label: review approval (``Code-Review
=+2``) and "ready to land" are decoupled. A reviewer may approve a
change that should wait for a sibling to merge first; the operator
gates landing with ``Submit-Ready``.

Why per-change lock files: the worker is single-process today but the
contract is "no double-process per change", and a future operator may
run two daemons (e.g. blue/green rollover). ``fcntl.flock`` on a file
under ``~/.cache/omnisight/submit-queue-locks/<change-number>.lock``
gives us OS-level mutual exclusion at near-zero cost; the lock is held
for the duration of one process_change call, never across polls.

Why rate-limit submits: even with serialized merges, a burst of 5+
ready PSes on the same hot file would re-trigger the auto-rebase
sweeper N times in <1s and drown the operator's notification
channels. A configurable inter-merge sleep (default 30s) gives the
sweeper, CI, and notifier headroom to drain.
"""
from __future__ import annotations

import base64
import errno
import fcntl
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from backend.agents import auto_rebase
from backend.agents.auto_resolve_config import AutoResolveRule, load_auto_resolve_config

OP_KEY_RE = re.compile(r"\b(OP-\d+)\b")

GERRIT_REST_BASE_URL = "https://sora.services:29420"
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
DEFAULT_RATE_LIMIT_SECONDS = 30.0

WORKER_DISABLE_ENV = "OMNISIGHT_SUBMIT_QUEUE_DISABLED"
DEFAULT_LOCK_DIR = Path("~/.cache/omnisight/submit-queue-locks").expanduser()


# ── Per-change lock (AC#3) ───────────────────────────────────────────


class ChangeLockBusy(RuntimeError):
    """Raised when another process already holds the change lock."""


class ChangeLock:
    """Filesystem mutex keyed on Gerrit change number.

    Acquired with ``fcntl.LOCK_EX | LOCK_NB`` so a second worker (or a
    re-entrant call from the same daemon) sees ``ChangeLockBusy`` and
    skips the change rather than corrupting state mid-flight.
    """

    def __init__(self, lock_dir: Path, change_number: str) -> None:
        self._dir = lock_dir
        self._change_number = change_number
        self._fd: int | None = None
        self._path: Path | None = None

    def __enter__(self) -> "ChangeLock":
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"{self._change_number}.lock"
        fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise ChangeLockBusy(
                    f"lock held for change {self._change_number}",
                ) from exc
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
            self._fd = None


# ── Result types ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class QueueResult:
    """Outcome of one process_change call.

    Exactly one of ``merged`` / ``rejected`` / ``skipped`` / ``error``
    is true per result.
    """

    change_number: str
    change_id: str = ""
    merged: bool = False
    rejected: bool = False
    skipped: bool = False
    error_only: bool = False
    skip_reason: str = ""
    reject_reason: str = ""
    rebase_files: tuple[str, ...] = ()
    error: str = ""
    auto_resolved: tuple[str, ...] = ()


# ── Worker ───────────────────────────────────────────────────────────


class SubmitQueueWorker:
    """Polls for Submit-Ready=+1 changes; rebases + submits one at a time.

    Construction takes mockable seams for every external interaction so
    tests can drive the full pipeline (gerrit query → REST rebase →
    REST submit → REST review for the -1 vote) without a real daemon.

    Threading: ``poll_once`` is intentionally synchronous and processes
    candidates one at a time. The submit queue is an *order* primitive,
    not a parallelism primitive — the whole point is to serialise the
    merge step.
    """

    def __init__(
        self,
        *,
        project: str,
        ssh_cmd_builder: Callable[..., list[str]],
        ssh_env_builder: Callable[[], dict[str, str]],
        worker_username: str,
        worker_password_loader: Callable[[], str | None],
        run_command: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        rest_base_url: str = GERRIT_REST_BASE_URL,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        owner_password_loader: Callable[[str], str | None] = (
            auto_rebase.load_owner_http_password
        ),
        notify_jira: Callable[[str, str], None] | None = None,
        audit_recorder: Callable[[dict[str, Any]], None] | None = None,
        repo_root: Path = auto_rebase.REPO_ROOT,
        auto_resolve_config_path: Path = auto_rebase.AUTO_RESOLVE_PATH,
        local_rebase_runner: Callable[..., auto_rebase.RebaseResult] | None = None,
        ci_check: Callable[[dict[str, Any]], bool] | None = None,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        lock_dir: Path = DEFAULT_LOCK_DIR,
        log: Callable[..., None] | None = None,
    ) -> None:
        self._project = project
        self._ssh_cmd_builder = ssh_cmd_builder
        self._ssh_env_builder = ssh_env_builder
        self._worker_username = worker_username
        self._worker_password_loader = worker_password_loader
        self._run_command = run_command
        self._rest_base_url = rest_base_url.rstrip("/")
        self._urlopen = urlopen
        self._owner_password_loader = owner_password_loader
        self._notify_jira = notify_jira
        self._audit_recorder = audit_recorder
        self._repo_root = repo_root
        self._auto_resolve_config_path = auto_resolve_config_path
        self._local_rebase_runner = local_rebase_runner
        self._ci_check = ci_check
        self._rate_limit_s = float(rate_limit_seconds)
        self._poll_interval_s = float(poll_interval_seconds)
        self._sleep = sleep
        self._clock = clock
        self._lock_dir = lock_dir
        self._log: Callable[..., None] = log or (lambda *args, **kwargs: None)
        self._stop = threading.Event()
        self._last_merge_at: float | None = None

    # ── public API ──

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        """Poll → process → sleep loop. Honours stop() and the kill-switch env."""
        self._log("INFO", "submit_queue_worker_started",
                  project=self._project,
                  rate_limit_s=self._rate_limit_s,
                  poll_interval_s=self._poll_interval_s)
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001
                self._log("ERROR", "submit_queue_poll_error",
                          err=f"{type(exc).__name__}: {exc}")
            if self._stop.is_set():
                break
            self._sleep(self._poll_interval_s)

    def poll_once(self) -> list[QueueResult]:
        """Single poll: query candidates, process each in order."""
        if os.environ.get(WORKER_DISABLE_ENV, "").strip().lower() in {
            "1", "true", "yes",
        }:
            self._log("INFO", "submit_queue_disabled_env",
                      project=self._project)
            return []
        candidates = self._query_candidates()
        results: list[QueueResult] = []
        for change in candidates:
            if self._stop.is_set():
                break
            self._wait_for_rate_limit()
            result = self.process_change(change)
            results.append(result)
            if result.merged:
                self._last_merge_at = self._clock()
            self._emit_result_log(result)
        return results

    def process_change(self, change: dict[str, Any]) -> QueueResult:
        """Lock → rebase-if-needed → CI → submit. Vote -1 on any failure.

        Skip reasons: ``locked`` (other worker), ``not_submittable``
        (Gerrit's own submit-rule failed — the queue does not override
        Human-Plus-2 / No-Veto / etc), ``no_owner_password`` (rebase
        path needs owner creds), ``ci_pending`` (OP-739 returned not-
        ready — leave Submit-Ready=+1 in place for next poll).
        """
        change_number = str(change.get("number") or "")
        change_id = str(change.get("id") or change.get("change_id") or "")
        if not change_number or not change_id:
            return QueueResult(
                change_number=change_number, change_id=change_id,
                error_only=True, error="missing_change_id_or_number",
            )

        try:
            with ChangeLock(self._lock_dir, change_number):
                return self._process_locked(change)
        except ChangeLockBusy:
            return QueueResult(
                change_number=change_number, change_id=change_id,
                skipped=True, skip_reason="locked",
            )

    # ── internals ──

    def _process_locked(self, change: dict[str, Any]) -> QueueResult:
        change_number = str(change.get("number") or "")
        change_id = str(change.get("id") or change.get("change_id") or "")

        if not self._is_submittable(change):
            return QueueResult(
                change_number=change_number, change_id=change_id,
                skipped=True, skip_reason="not_submittable",
            )

        # CI gate (OP-739 hook). When ci_check is None the queue is
        # default-OFF for CI, which matches the project.config Verified
        # default-OFF state; the operator flips both gates together.
        if self._ci_check is not None:
            try:
                ready = bool(self._ci_check(change))
            except Exception as exc:  # noqa: BLE001
                self._reject(change, reason=f"ci_check_error: {exc}")
                return QueueResult(
                    change_number=change_number, change_id=change_id,
                    rejected=True,
                    reject_reason=f"ci_check_error: {exc}",
                )
            if not ready:
                return QueueResult(
                    change_number=change_number, change_id=change_id,
                    skipped=True, skip_reason="ci_pending",
                )

        # Rebase if the current PS isn't already on the develop tip.
        develop_tip = self._fetch_develop_tip()
        rebase_outcome = self._maybe_rebase(change, develop_tip)
        if rebase_outcome.conflict:
            files = rebase_outcome.files
            self._reject(
                change,
                reason=(
                    "Rebase conflict against develop tip "
                    f"{develop_tip[:8] if develop_tip else 'HEAD'}: "
                    f"{', '.join(files) if files else 'see Gerrit log'}. "
                    "Resolve manually and re-mark Submit-Ready=+1."
                ),
            )
            return QueueResult(
                change_number=change_number, change_id=change_id,
                rejected=True, reject_reason="rebase_conflict",
                rebase_files=files,
            )
        if rebase_outcome.error:
            self._reject(
                change,
                reason=f"Rebase failed: {rebase_outcome.error}",
            )
            return QueueResult(
                change_number=change_number, change_id=change_id,
                rejected=True, reject_reason="rebase_error",
                error=rebase_outcome.error,
            )

        # Submit (atomic merge).
        try:
            status, payload = self._rest_post(
                f"/a/changes/{urllib.parse.quote(change_id, safe='')}/submit",
                self._worker_username,
                self._worker_password_loader() or "",
                {},
            )
        except Exception as exc:  # noqa: BLE001
            self._reject(change, reason=f"Submit failed: {exc}")
            return QueueResult(
                change_number=change_number, change_id=change_id,
                rejected=True, reject_reason="submit_error",
                error=f"{type(exc).__name__}: {exc}",
            )
        if status not in (200, 201):
            self._reject(
                change,
                reason=(
                    f"Submit rejected by Gerrit (HTTP {status}): "
                    f"{payload.strip()[:200]}"
                ),
            )
            return QueueResult(
                change_number=change_number, change_id=change_id,
                rejected=True, reject_reason="submit_http_error",
                error=f"HTTP {status}: {payload.strip()[:200]}",
            )

        self._post_jira_merge_notice(change)
        return QueueResult(
            change_number=change_number, change_id=change_id, merged=True,
        )

    def _query_candidates(self) -> list[dict[str, Any]]:
        # ``--all-approvals`` surfaces label scores in submitRecords;
        # ``--current-patch-set`` exposes parents[0] for the rebase
        # short-circuit; ``--submit-records`` gives Gerrit's own
        # is:submittable verdict.
        query = (
            f"status:open project:{self._project} "
            "label:Submit-Ready=+1"
        )
        cmd = self._ssh_cmd_builder(
            "gerrit", "query", "--format=JSON",
            "--current-patch-set", "--all-approvals",
            "--submit-records",
            query,
        )
        try:
            result = self._run_command(
                cmd, capture_output=True, text=True, timeout=30,
                env=self._ssh_env_builder(),
            )
        except Exception as exc:  # noqa: BLE001
            self._log(
                "ERROR", "submit_queue_query_failed",
                err=f"{type(exc).__name__}: {exc}",
            )
            return []
        if result.returncode != 0:
            self._log(
                "ERROR", "submit_queue_query_failed",
                err=(result.stderr or "")[-500:],
            )
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
        # Stable order: by lastUpdated then by number, so an operator
        # who marks 5 PSes Submit-Ready in a known order sees them
        # land in roughly that order.
        out.sort(
            key=lambda c: (
                int(c.get("lastUpdated") or 0),
                int(c.get("number") or 0),
            ),
        )
        return out

    def _is_submittable(self, change: dict[str, Any]) -> bool:
        """Honour Gerrit's submit-records verdict.

        Gerrit emits ``submitRecords[*].status == "OK"`` when every
        submit-requirement is satisfied. The queue does NOT override
        Human-Plus-2 / No-Veto / Verified — it only picks up changes
        that already pass them.
        """
        records = change.get("submitRecords") or []
        if not records:
            # Fall back to checking labels directly when Gerrit didn't
            # populate submitRecords (older query payload shapes).
            cps = change.get("currentPatchSet") or {}
            for approval in cps.get("approvals") or []:
                if approval.get("type") == "Code-Review":
                    try:
                        if int(approval.get("value", 0)) <= -1:
                            return False
                    except (TypeError, ValueError):
                        pass
            return True
        for record in records:
            if record.get("status") == "OK":
                return True
            if record.get("status") == "CLOSED":
                return False
        return False

    def _fetch_develop_tip(self) -> str:
        """Return the current ``refs/heads/develop`` SHA, or ''.

        Used as the rebase ``base`` and as a short-circuit so a PS
        whose parent is already the tip is not re-rebased.
        """
        cmd = self._ssh_cmd_builder(
            "gerrit", "query", "--format=JSON",
            "--current-patch-set",
            f"project:{self._project} branch:develop status:merged limit:1",
        )
        try:
            result = self._run_command(
                cmd, capture_output=True, text=True, timeout=30,
                env=self._ssh_env_builder(),
            )
        except Exception:  # noqa: BLE001
            return ""
        if result.returncode != 0:
            return ""
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
            cps = obj.get("currentPatchSet") or {}
            rev = str(cps.get("revision") or "")
            if rev:
                return rev
        return ""

    @dataclass(frozen=True)
    class _RebaseOutcome:
        ok: bool = False
        skipped: bool = False
        conflict: bool = False
        error: str = ""
        files: tuple[str, ...] = ()

    def _maybe_rebase(
        self, change: dict[str, Any], develop_tip: str,
    ) -> "_RebaseOutcome":
        cps = change.get("currentPatchSet") or {}
        parents = cps.get("parents") or []
        parent_revs = [
            str(p.get("revision") or "")
            for p in parents
            if isinstance(p, dict)
        ]
        if develop_tip and develop_tip in parent_revs:
            return SubmitQueueWorker._RebaseOutcome(skipped=True)

        owner_obj = change.get("owner") or {}
        owner = str(
            owner_obj.get("username")
            or owner_obj.get("name")
            or ""
        )
        if not owner:
            return SubmitQueueWorker._RebaseOutcome(error="no_owner_username")
        password = self._owner_password_loader(owner)
        if not password:
            return SubmitQueueWorker._RebaseOutcome(
                error=f"no_password_for_owner:{owner}",
            )

        change_id = str(change.get("id") or change.get("change_id") or "")
        rest_path = (
            f"/a/changes/{urllib.parse.quote(change_id, safe='')}"
            f"/revisions/current/rebase"
        )
        body: dict[str, Any] = {}
        if develop_tip:
            body["base"] = develop_tip
        try:
            status, payload = self._rest_post(
                rest_path, owner, password, body,
            )
        except Exception as exc:  # noqa: BLE001
            return SubmitQueueWorker._RebaseOutcome(
                error=f"{type(exc).__name__}: {exc}",
            )
        if status in (200, 201):
            return SubmitQueueWorker._RebaseOutcome(ok=True)
        if status == 409:
            files = tuple(self._parse_conflict_files(payload))
            auto_resolvers = self._load_auto_resolvers()
            auto_handled = [f for f in files if f in auto_resolvers]
            if files and set(files) == set(auto_handled):
                result = self._run_local_auto_resolve(
                    change,
                    develop_tip,
                    tuple(auto_handled),
                    auto_resolvers,
                )
                if result.success:
                    self._post_auto_resolve_jira_notice(
                        change,
                        develop_tip,
                        result.auto_resolved,
                        auto_resolvers,
                    )
                    self._record_auto_resolve_audit(
                        change,
                        result,
                        auto_resolvers,
                    )
                    return SubmitQueueWorker._RebaseOutcome(ok=True)
                self._post_auto_resolve_failed_jira_notice(change, result)
                self._log(
                    "WARN",
                    "submit_queue_auto_resolve_failed",
                    change_id=change_id,
                    files=list(files),
                    err=result.error,
                )
            return SubmitQueueWorker._RebaseOutcome(
                conflict=True, files=files,
            )
        return SubmitQueueWorker._RebaseOutcome(
            error=f"HTTP {status}: {payload.strip()[:200]}",
        )

    def _load_auto_resolvers(self) -> dict[str, AutoResolveRule]:
        try:
            return load_auto_resolve_config(
                self._repo_root / self._auto_resolve_config_path,
                log=self._log,
            )
        except Exception as exc:  # noqa: BLE001
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
    ) -> auto_rebase.RebaseResult:
        runner = self._local_rebase_runner or auto_rebase.local_rebase_with_resolvers
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
            return auto_rebase.RebaseResult(
                change_number=str(change.get("number") or ""),
                conflict=True,
                files=files,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _reject(self, change: dict[str, Any], reason: str) -> None:
        """Vote Submit-Ready=-1 with ``reason`` as the review comment.

        Failures here are logged but never re-raised: a transient
        Gerrit blip should not crash the daemon. The operator can
        always re-mark Submit-Ready=+1 once the underlying issue is
        cleared.
        """
        change_id = str(change.get("id") or change.get("change_id") or "")
        if not change_id:
            return
        password = self._worker_password_loader() or ""
        body = {
            "labels": {"Submit-Ready": -1},
            "message": reason,
            "notify": "OWNER",
        }
        rest_path = (
            f"/a/changes/{urllib.parse.quote(change_id, safe='')}"
            f"/revisions/current/review"
        )
        try:
            status, payload = self._rest_post(
                rest_path, self._worker_username, password, body,
            )
            if status not in (200, 201):
                self._log(
                    "WARN", "submit_queue_reject_vote_failed",
                    change_id=change_id,
                    err=f"HTTP {status}: {payload.strip()[:200]}",
                )
        except Exception as exc:  # noqa: BLE001
            self._log(
                "WARN", "submit_queue_reject_vote_failed",
                change_id=change_id,
                err=f"{type(exc).__name__}: {exc}",
            )

        # Also notify JIRA so the ticket carries the failure reason.
        ticket = self._extract_ticket_key(change)
        if ticket and self._notify_jira is not None:
            try:
                self._notify_jira(
                    ticket,
                    f"Submit queue rejected the change: {reason}",
                )
            except Exception as exc:  # noqa: BLE001
                self._log(
                    "WARN", "submit_queue_reject_jira_notify_failed",
                    ticket=ticket,
                    err=f"{type(exc).__name__}: {exc}",
                )

    def _post_auto_resolve_jira_notice(
        self,
        change: dict[str, Any],
        target_sha: str,
        files: tuple[str, ...],
        resolvers: dict[str, AutoResolveRule],
    ) -> None:
        if self._notify_jira is None:
            return
        ticket = self._extract_ticket_key(change)
        if not ticket:
            return
        details = ", ".join(
            f"{path} via {Path(resolvers[path].resolver).name}"
            for path in files
            if path in resolvers
        )
        try:
            self._notify_jira(
                ticket,
                f"[auto-resolve] Submit queue regenerated {details} to "
                f"handle merge conflict against develop tip {target_sha}",
            )
        except Exception as exc:  # noqa: BLE001
            self._log(
                "WARN", "submit_queue_auto_resolve_jira_notify_failed",
                ticket=ticket,
                err=f"{type(exc).__name__}: {exc}",
            )

    def _post_auto_resolve_failed_jira_notice(
        self, change: dict[str, Any], result: auto_rebase.RebaseResult,
    ) -> None:
        if self._notify_jira is None:
            return
        ticket = self._extract_ticket_key(change)
        if not ticket:
            return
        files = ", ".join(result.files) if result.files else "registered files"
        try:
            self._notify_jira(
                ticket,
                f"[auto-resolve] Submit queue could not regenerate {files}; "
                "falling back to manual conflict handling. "
                f"Reason: {result.error or 'unknown'}",
            )
        except Exception as exc:  # noqa: BLE001
            self._log(
                "WARN", "submit_queue_auto_resolve_jira_notify_failed",
                ticket=ticket,
                err=f"{type(exc).__name__}: {exc}",
            )

    def _record_auto_resolve_audit(
        self,
        change: dict[str, Any],
        result: auto_rebase.RebaseResult,
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

    def _post_jira_merge_notice(self, change: dict[str, Any]) -> None:
        if self._notify_jira is None:
            return
        ticket = self._extract_ticket_key(change)
        if not ticket:
            return
        try:
            self._notify_jira(
                ticket,
                "Submit queue merged the change atomically.",
            )
        except Exception as exc:  # noqa: BLE001
            self._log(
                "WARN", "submit_queue_merge_jira_notify_failed",
                ticket=ticket,
                err=f"{type(exc).__name__}: {exc}",
            )

    def _extract_ticket_key(self, change: dict[str, Any]) -> str | None:
        subject = str(change.get("subject") or "")
        match = OP_KEY_RE.search(subject)
        return match.group(1) if match else None

    def _wait_for_rate_limit(self) -> None:
        if self._last_merge_at is None or self._rate_limit_s <= 0:
            return
        elapsed = self._clock() - self._last_merge_at
        remaining = self._rate_limit_s - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def _rest_post(
        self,
        path: str,
        username: str,
        password: str,
        body: dict[str, Any],
    ) -> tuple[int, str]:
        url = self._rest_base_url + path
        data = json.dumps(body).encode()
        token = base64.b64encode(
            f"{username}:{password}".encode(),
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
                code = (
                    getattr(resp, "status", None)
                    or getattr(resp, "code", 200)
                )
                return int(code), payload
        except urllib.error.HTTPError as exc:
            try:
                payload = (
                    exc.read().decode("utf-8", "replace")
                    if exc.fp else ""
                )
            except Exception:  # noqa: BLE001
                payload = ""
            return int(exc.code), payload

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

    def _emit_result_log(self, result: QueueResult) -> None:
        if result.merged:
            self._log(
                "INFO", "submit_queue_merged",
                change_id=result.change_id,
                change_number=result.change_number,
            )
        elif result.rejected:
            self._log(
                "WARN", "submit_queue_rejected",
                change_id=result.change_id,
                change_number=result.change_number,
                reason=result.reject_reason,
                files=list(result.rebase_files),
            )
        elif result.skipped:
            self._log(
                "INFO", "submit_queue_skipped",
                change_id=result.change_id,
                change_number=result.change_number,
                reason=result.skip_reason,
            )
        elif result.error_only:
            self._log(
                "ERROR", "submit_queue_error",
                change_id=result.change_id,
                change_number=result.change_number,
                err=result.error,
            )
