"""Gerrit stream-events → JIRA Published bridge.

OP-689 closes OP-247 Phase 3: after Gerrit merges a develop change, this
daemon transitions the matching JIRA ticket from Approved to Published.
OP-743 makes the terminal merge event authoritative: the bridge force-walks
In Progress / Under Review / Approved tickets to Published, while leaving
Published and Archived tickets idempotent.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable, Iterable
from uuid import uuid4

from backend import db_pool
from backend.agents import jira_dispatch, reviewer_safety
from backend.db import _resolve_pg_dsn
from scripts import medical_readiness_check

# OP-831 (2026-05-11 post-mortem): the cursor file path was hard-coded to a
# root-owned path under ``/var/lib/``. The bridge runs as user-level systemd
# and cannot ``mkdir`` there, so every restart entered a 7-second crash
# loop (catchup → stream → first event → save_cursor → PermissionError →
# systemd restart). Live change-merged events streamed past the bridge
# unprocessed for ~16 minutes (01:08–01:24). The env override below lets
# the systemd unit point at an XDG-compliant user-writable default
# (``~/.local/state/omnisight-bridge/``); the legacy path remains the
# fallback so existing root-installed deployments keep working without
# config drift. Cross-reference: OP-827 (companion runner-side typed
# precondition exceptions for ``ensure_change_ids``).
CURSOR_FILE = Path(
    os.environ.get(
        "OMNISIGHT_BRIDGE_CURSOR_FILE",
        "/var/lib/omnisight-bridge/event-cursor.json",
    )
)

# SP-B-X-009 (OP-1067) — C9 bridge-health heartbeat. The bridge daemon
# touches this file on every maintenance tick (default 30s cadence) so
# pickup-side runners can fail fast when the bridge has stopped
# producing change-merged transitions. ``OMNISIGHT_BRIDGE_HEARTBEAT_PATH``
# overrides for user-level systemd installs where ``/var/run`` is not
# writable; ``OMNISIGHT_BRIDGE_STALE_AFTER_SEC`` tunes the staleness
# threshold consumed by ``check_bridge_heartbeat`` and the runner gate.
DEFAULT_HEARTBEAT_FILE = "/var/run/omnisight-bridge/heartbeat"
DEFAULT_HEARTBEAT_FILE_SECONDS = 30.0
DEFAULT_BRIDGE_STALE_AFTER_SEC = 300
DEFAULT_COORDINATOR_BRIDGE_EVENTS_FILE = (
    "~/.config/omnisight/coordinator/bridge-events.jsonl"
)
MERGER_VERIFY_SCRATCH_ROOT = Path("/tmp")
MERGER_VERIFY_SCRATCH_GLOB = "merger-verify-*"
MERGER_VERIFY_REAP_AGE_SECONDS = 30 * 60

APPROVED_STATUS_NAMES = {"Approved", "承認済み"}
ARCHIVED_STATUS_NAMES = {"Archived"}
IN_PROGRESS_STATUS_NAMES = {"In Progress", "進行中"}
PUBLISHED_STATUS_NAMES = {"Published", "公開済み"}
UNDER_REVIEW_STATUS_NAMES = {"Under Review"}
KEEP_OPEN_LABEL = "coord-keep-open"
DEFAULT_ARCHIVE_AGE_DAYS = 30
AUTH_FAILURE_MARKERS = (
    "Permission denied",
    "Authentication failed",
    "publickey",
    "No supported authentication methods",
)

OP_KEY_RE = re.compile(r"\bOP-\d+\b")
OP_BRACKET_RE = re.compile(r"\[(OP-\d+)(?:/[^\]]*)?\]")
GERRIT_CHANGE_URL_RE = re.compile(
    r"https://\S+/c/[^/\s]+/[^/\s]+/\+/(\d+)"
)


class BridgeFatalError(RuntimeError):
    """Fatal daemon error; launcher maps this to exit 2."""


class JiraAuthError(BridgeFatalError):
    """JIRA token/account cannot perform required bridge operations."""


class GerritAuthError(BridgeFatalError):
    """Gerrit SSH auth failed; systemd restart/operator attention needed."""


@dataclass
class BridgeCounters:
    events_received: int = 0
    transitions_made: int = 0
    parse_errors: int = 0
    jira_errors: int = 0
    gerrit_reconnects: int = 0
    last_event_at_ts: str | None = None


@dataclass(frozen=True)
class GerritChange:
    change_id: str
    number: str | None = None
    subject: str = ""
    status: str = ""
    branch: str = ""


def load_cursor(path: Path = CURSOR_FILE) -> tuple[str, datetime] | None:
    """Return the last successfully processed stream event cursor."""
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return str(data["event_id"]), datetime.fromisoformat(str(data["timestamp"]))


def save_cursor(event_id: str, ts: datetime, path: Path = CURSOR_FILE) -> None:
    """Atomically persist the stream event cursor with daemon-private mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"event_id": event_id, "timestamp": ts.isoformat()}))
    tmp.chmod(0o600)
    tmp.replace(path)


def load_drift_cooldown_state(path: Path) -> tuple[dict[str, bool], dict[str, float]]:
    """Return persisted develop-drift cooldown state."""
    if not path.exists():
        return {}, {}
    data = json.loads(path.read_text())
    changes = data.get("changes", {})
    if not isinstance(changes, dict):
        return {}, {}

    last_seen_mergeable: dict[str, bool] = {}
    last_re_eval: dict[str, float] = {}
    for raw_key, raw_state in changes.items():
        if not isinstance(raw_state, dict):
            continue
        key = str(raw_key)
        mergeable = raw_state.get("last_seen_mergeable")
        if isinstance(mergeable, bool):
            last_seen_mergeable[key] = mergeable
        re_eval = raw_state.get("last_re_eval_ts")
        if isinstance(re_eval, (int, float)):
            last_re_eval[key] = float(re_eval)
    return last_seen_mergeable, last_re_eval


def save_drift_cooldown_state(
    last_seen_mergeable: dict[str, bool],
    last_re_eval: dict[str, float],
    path: Path,
) -> None:
    """Atomically persist develop-drift cooldown state with private mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    changes = {
        change_key: {
            "last_seen_mergeable": mergeable,
            "last_re_eval_ts": last_re_eval.get(change_key),
        }
        for change_key, mergeable in sorted(last_seen_mergeable.items())
    }
    for change_key, re_eval in sorted(last_re_eval.items()):
        changes.setdefault(change_key, {
            "last_seen_mergeable": None,
            "last_re_eval_ts": re_eval,
        })
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"changes": changes}, sort_keys=True))
    tmp.chmod(0o600)
    tmp.replace(path)


def heartbeat_path_from_env(env: dict[str, str] | None = None) -> Path:
    """Resolve the bridge heartbeat file path. SP-B-X-009 (OP-1067)."""

    e = env if env is not None else os.environ
    return Path(e.get("OMNISIGHT_BRIDGE_HEARTBEAT_PATH", DEFAULT_HEARTBEAT_FILE))


def heartbeat_stale_after_seconds(env: dict[str, str] | None = None) -> int:
    """Resolve ``OMNISIGHT_BRIDGE_STALE_AFTER_SEC``; defaults to 300."""

    e = env if env is not None else os.environ
    raw = e.get("OMNISIGHT_BRIDGE_STALE_AFTER_SEC", "").strip()
    if not raw:
        return DEFAULT_BRIDGE_STALE_AFTER_SEC
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BRIDGE_STALE_AFTER_SEC
    return max(1, value)


def touch_heartbeat_file(path: Path | None = None, now: float | None = None) -> Path:
    """Touch the heartbeat file to ``now`` (or wall-clock ``time.time()``).

    Creates the parent directory if missing — the systemd unit may run as
    a user-level service against ``~/.local/state/omnisight-bridge/`` and
    we cannot rely on the deployer pre-creating the directory.
    """

    target = path if path is not None else heartbeat_path_from_env()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=True)
    ts = now if now is not None else time.time()
    os.utime(target, (ts, ts))
    return target


def coordinator_bridge_events_path_from_env(
    env: dict[str, str] | None = None,
) -> Path:
    """Resolve the JSONL event tap consumed by ``pipeline_coordinator``."""

    e = env if env is not None else os.environ
    return Path(
        e.get(
            "OMNISIGHT_COORDINATOR_BRIDGE_EVENTS_FILE",
            DEFAULT_COORDINATOR_BRIDGE_EVENTS_FILE,
        )
    ).expanduser()


def append_coordinator_bridge_event(
    event: dict[str, Any],
    *,
    path: Path | None = None,
) -> Path:
    """Append one Gerrit stream event for the coordinator's bridge tap."""

    target = path if path is not None else coordinator_bridge_events_path_from_env()
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    record = {
        "ts": utc_now_iso(),
        "source": "gerrit-jira-bridge",
        "event": event,
    }
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    if not existed:
        target.chmod(0o600)
    return target


def check_bridge_heartbeat(
    path: Path | None = None,
    stale_after_seconds: int | None = None,
    now: float | None = None,
) -> tuple[bool, float, Path]:
    """Return ``(is_fresh, age_seconds, resolved_path)``.

    A missing heartbeat file is treated as stale (``is_fresh=False``,
    ``age_seconds=inf``) — the bridge has either never started or its
    install drifted from the configured path; pickup-side callers must
    fail closed in either case.
    """

    target = path if path is not None else heartbeat_path_from_env()
    stale = (
        stale_after_seconds
        if stale_after_seconds is not None
        else heartbeat_stale_after_seconds()
    )
    current = now if now is not None else time.time()
    try:
        mtime = target.stat().st_mtime
    except FileNotFoundError:
        return False, float("inf"), target
    age = max(0.0, current - mtime)
    return age <= stale, age, target


def reap_stale_merger_verify_worktrees(
    *,
    root: Path | None = None,
    stale_after_seconds: float = MERGER_VERIFY_REAP_AGE_SECONDS,
    now: float | None = None,
    logger: Callable[..., None] | None = None,
) -> int:
    """Remove stale ``/tmp/merger-verify-*`` scratch worktrees."""

    scratch_root = root if root is not None else MERGER_VERIFY_SCRATCH_ROOT
    log = logger if logger is not None else structured_log
    current = now if now is not None else time.time()
    reaped = 0
    for path in scratch_root.glob(MERGER_VERIFY_SCRATCH_GLOB):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if not path.is_dir():
            continue
        age_seconds = max(0.0, current - stat.st_mtime)
        if age_seconds < stale_after_seconds:
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            log(
                "ERROR",
                "merger_verify_scratch_reap_failed",
                err=f"{type(exc).__name__}: {exc}",
                path=str(path),
                age_seconds=round(age_seconds, 3),
            )
            continue
        reaped += 1
        log(
            "WARN",
            "merger_verify_scratch_reaped",
            path=str(path),
            age_seconds=round(age_seconds, 3),
        )
    return reaped


@dataclass
class BridgeConfig:
    agent_class: str = "subscription-claude"
    gerrit_user: str = "claude-bot"
    gerrit_host: str = jira_dispatch.GERRIT_SSH_HOST
    gerrit_port: int = jira_dispatch.GERRIT_SSH_PORT
    gerrit_key_path: Path = Path(
        "~/.config/omnisight/gerrit-claude-bot-ed25519"
    ).expanduser()
    heartbeat_seconds: float = 60.0
    # SP-B-X-009 — separate cadence for the on-disk heartbeat file; the
    # pickup gate reads its mtime, so the file must tick more often than
    # the log heartbeat to keep the runner-side stale-threshold tight.
    heartbeat_file_seconds: float = DEFAULT_HEARTBEAT_FILE_SECONDS
    heartbeat_file_path: Path | None = None
    coordinator_bridge_events_path: Path | None = None
    silent_warn_seconds: float = 600.0
    periodic_catchup_seconds: float = 900.0
    max_backoff_seconds: float = 60.0
    alert_after_failures: int = 10
    cursor_file: Path | None = CURSOR_FILE
    drift_cooldown_file: Path | None = None
    # OP-733 — debounce window for auto-rebase sweeps. A batch +2 of N
    # changes can fire N change-merged events within seconds; the
    # debounce coalesces them into a single sweep on the most recent
    # merged SHA.
    auto_rebase_debounce_seconds: float = 30.0
    # OP-1409 — debounce window for merger drift re-evaluation sweeps.
    # Develop can advance several times in a burst; re-check all open
    # develop PSes once after the burst settles.
    merger_drift_debounce_seconds: float = 60.0
    merger_drift_cooldown_seconds: float = 30.0 * 60.0
    gerrit_rest_base_url: str | None = None
    archive_age_days: int | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def archive_age_days_from_env(env: dict[str, str] | None = None) -> int:
    raw = (env or os.environ).get("OMNISIGHT_ARCHIVE_AGE_DAYS", "")
    if not raw:
        return DEFAULT_ARCHIVE_AGE_DAYS
    try:
        value = int(raw)
    except ValueError:
        structured_log(
            "WARN",
            "archive_age_days_invalid_defaulted",
            err=raw,
            default=DEFAULT_ARCHIVE_AGE_DAYS,
        )
        return DEFAULT_ARCHIVE_AGE_DAYS
    if value < 0:
        structured_log(
            "WARN",
            "archive_age_days_negative_defaulted",
            err=raw,
            default=DEFAULT_ARCHIVE_AGE_DAYS,
        )
        return DEFAULT_ARCHIVE_AGE_DAYS
    return value


def parse_jira_datetime(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    # Atlassian commonly returns +0000; fromisoformat wants +00:00.
    if re.search(r"[+-]\d{4}$", normalized):
        normalized = normalized[:-2] + ":" + normalized[-2:]
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def structured_log(
    level: str,
    event: str,
    *,
    ticket_key: str | None = None,
    change_id: str | None = None,
    err: str | None = None,
    **extra: Any,
) -> None:
    record = {
        "timestamp": utc_now_iso(),
        "level": level,
        "event": event,
        "ticket_key": ticket_key,
        "change_id": change_id,
        "err": err,
    }
    record.update(extra)
    print(json.dumps(record, sort_keys=True), flush=True)


def parse_stream_line(line: str) -> dict[str, Any] | None:
    """Return a Gerrit event dict, or None for malformed JSON."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def extract_gerrit_change(event: dict[str, Any]) -> GerritChange:
    change = event.get("change") or {}
    patch_set = event.get("patchSet") or {}
    change_id = (
        change.get("id")
        or change.get("change_id")
        or patch_set.get("changeId")
        or ""
    )
    return GerritChange(
        change_id=str(change_id),
        number=str(change.get("number") or event.get("changeNumber") or "") or None,
        subject=str(change.get("subject") or ""),
        status=str(change.get("status") or ""),
        branch=str(change.get("branch") or ""),
    )


def extract_ticket_keys_from_subject(subject: str) -> list[str]:
    """Extract OP keys from Gerrit commit subjects.

    The primary convention is a leading ``[OP-123]`` or ``[OP-123/foo]``
    prefix. If absent, fall back to any OP key in the subject.
    """
    hits = [m.group(1) for m in OP_BRACKET_RE.finditer(subject)]
    if not hits:
        hits = OP_KEY_RE.findall(subject)
    return list(dict.fromkeys(hits))


def flatten_adf_text(node: Any) -> str:
    chunks: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            if item.get("type") == "text":
                chunks.append(str(item.get("text", "")))
            elif item.get("type") == "hardBreak":
                chunks.append("\n")
            for child in item.get("content", []) or []:
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(node)
    return "".join(chunks)


def extract_change_numbers_from_comments(comments: Iterable[dict[str, Any]]) -> list[str]:
    numbers: list[str] = []
    for comment in comments:
        text = flatten_adf_text(comment.get("body"))
        if "[runner-pushed-to-gerrit]" not in text:
            continue
        for match in GERRIT_CHANGE_URL_RE.finditer(text):
            numbers.append(match.group(1))
    return list(dict.fromkeys(numbers))


# OP-746 — per-merged-PS observability ────────────────────────────────


def compute_ps_merged_metrics(event: dict[str, Any]) -> dict[str, Any]:
    """Extract per-PS metrics from a Gerrit ``change-merged`` event.

    Best-effort: the stream event payload exposes the *current* patchset
    only, so per-PS-history fields (``rebase_count`` / ``rework_count``
    by ``--patch-sets`` ``kind`` breakdown) are populated by the daemon
    via a follow-up Gerrit query rather than this pure helper.

    Returns a flat dict suitable for the structured logger ``**extra``
    bag. Missing fields are emitted as ``None`` / ``0`` so the daily
    report (``scripts/conflict_report.py``) can rely on the schema even
    when Gerrit truncates the stream-event payload.
    """
    change = event.get("change") or {}
    patch_set = event.get("patchSet") or {}
    current_ps = change.get("currentPatchSet") or patch_set
    subject = str(change.get("subject") or "")
    ticket_keys = extract_ticket_keys_from_subject(subject)

    created_on = _coerce_int(change.get("createdOn"))
    last_updated = _coerce_int(change.get("lastUpdated"))
    lifetime_min: float | None = None
    if created_on is not None and last_updated is not None:
        lifetime_min = round(max(0, last_updated - created_on) / 60.0, 2)

    insertions = _coerce_int(current_ps.get("sizeInsertions")) or 0
    deletions = _coerce_int(current_ps.get("sizeDeletions")) or 0
    final_diff_size = abs(insertions) + abs(deletions)

    patchset_count = _coerce_int(current_ps.get("number")) or 0

    verified_minus_one = 0
    for approval in current_ps.get("approvals") or []:
        if approval.get("type") != "Verified":
            continue
        try:
            if int(approval.get("value", 0)) <= -1:
                verified_minus_one += 1
        except (TypeError, ValueError):
            continue

    return {
        "change_id": str(change.get("id") or ""),
        "change_number": str(change.get("number") or ""),
        "ticket": ticket_keys[0] if ticket_keys else None,
        "lifetime_min": lifetime_min,
        "patchset_count": patchset_count,
        "rebase_count": 0,
        "rework_count": 0,
        "changed_files": [],
        "final_diff_size": final_diff_size,
        "verified_minus_one_count": verified_minus_one,
    }


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def retry_after_seconds(headers: Any) -> float | None:
    raw = None
    if headers is not None:
        raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class GerritJiraBridge:
    """Stateless Gerrit/JIRA archiver daemon."""

    def __init__(
        self,
        client: jira_dispatch.DispatchClient,
        config: BridgeConfig | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        run_command: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        logger: Callable[..., None] = structured_log,
    ) -> None:
        self.client = client
        self.config = config or BridgeConfig(agent_class=client.agent_class)
        self.sleep = sleep
        self.popen_factory = popen_factory
        self.run_command = run_command
        self.urlopen = urlopen
        self.log = logger
        self.counters = BridgeCounters()
        self._ticket_locks: dict[str, Lock] = {}
        self._locks_guard = Lock()
        self._stop = False
        self._started_at = time.monotonic()
        self._last_heartbeat = time.monotonic()
        # SP-B-X-009 — primed to "long ago" so the first maintenance tick
        # writes the heartbeat file immediately rather than waiting one
        # full ``heartbeat_file_seconds`` cycle.
        self._last_heartbeat_file = time.monotonic() - self.config.heartbeat_file_seconds
        self._last_periodic_catchup = time.monotonic()
        self._cursor_missing_warned = False
        # OP-733 — lazy-initialised on the first change-merged event so
        # bridge construction stays cheap + tests that never exercise
        # the rebase path don't import the module at all.
        self._auto_rebase_sweeper: Any = None
        self._auto_rebase_scheduler: Any = None
        self._develop_drift_scheduler: Any = None
        self._develop_drift_lock = Lock()
        self._develop_drift_last_seen_mergeable: dict[str, bool] = {}
        self._develop_drift_last_re_eval: dict[str, float] = {}
        self._load_develop_drift_cooldown_state()
        # SP-B-X-019 / OP-1077 — independent heartbeat-thread state.
        # The maintenance-tick loop runs *inside* ``stream_forever``'s
        # blocking SSH read, so it stalls during quiet Gerrit periods.
        # The heartbeat thread (started in ``stream_forever``) writes
        # the on-disk heartbeat on a wall-clock cadence regardless of
        # event traffic; this Event lets ``stop()`` unblock the thread
        # so the daemon can exit cleanly without a 30s wait.
        self._heartbeat_thread: Thread | None = None
        self._heartbeat_thread_stop = Event()

    def stop(self) -> None:
        self._stop = True
        self._heartbeat_thread_stop.set()

    def jira_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        url = self.client.base_url + path
        attempt = 0
        backoff = 1.0
        while True:
            attempt += 1
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(
                url,
                data=data,
                method=method,
                headers={
                    "Authorization": self.client.auth_header,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = resp.read().decode()
                    return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode() if exc.fp else ""
                if exc.code in (401, 403):
                    self.log("ALERT", "jira_auth_failed", err=f"{exc.code}: {detail}")
                    raise JiraAuthError(f"JIRA HTTP {exc.code}") from exc
                if exc.code == 429:
                    delay = retry_after_seconds(exc.headers) or backoff
                    self.log("WARN", "jira_rate_limited", err=f"429: {detail}", delay=delay)
                    self.sleep(delay)
                    backoff = min(backoff * 2, self.config.max_backoff_seconds)
                    continue
                if 500 <= exc.code <= 599 and attempt < max_attempts:
                    self.log("WARN", "jira_5xx_retry", err=f"{exc.code}: {detail}", attempt=attempt)
                    self.sleep(backoff)
                    backoff = min(backoff * 2, self.config.max_backoff_seconds)
                    continue
                self.counters.jira_errors += 1
                raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc

    def search_catchup_candidate_tickets(self) -> list[dict[str, Any]]:
        jql = (
            f'project = "{self.client.project_key}" '
            'AND status in ("In Progress", "Under Review", "Approved") '
            'AND assignee in (codex-bot, claude-bot) '
            "ORDER BY updated ASC"
        )
        resp = self.jira_request(
            "POST",
            "/search/jql",
            {
                "jql": jql,
                "fields": ["summary", "status", "assignee"],
                "maxResults": 100,
            },
        )
        return list(resp.get("issues", []))

    def search_archive_candidate_tickets(self) -> list[dict[str, Any]]:
        archive_age_days = self.archive_age_days()
        jql = (
            f'project = "{self.client.project_key}" '
            'AND status in ("Published", "公開済み") '
            f"AND statusCategoryChangedDate <= -{archive_age_days}d "
            "ORDER BY statusCategoryChangedDate ASC"
        )
        resp = self.jira_request(
            "POST",
            "/search/jql",
            {
                "jql": jql,
                "fields": [
                    "summary",
                    "status",
                    "labels",
                    "statuscategorychangedate",
                    "updated",
                ],
                "maxResults": 100,
            },
        )
        return list(resp.get("issues", []))

    def search_approved_tickets(self) -> list[dict[str, Any]]:
        """Backward-compatible alias for tests/scripts from OP-689."""
        return self.search_catchup_candidate_tickets()

    def fetch_issue_status(self, ticket_key: str) -> str:
        issue = self.jira_request("GET", f"/issue/{ticket_key}?fields=status")
        status = ((issue.get("fields") or {}).get("status") or {}).get("name", "")
        return str(status)

    def fetch_issue_archive_fields(self, ticket_key: str) -> dict[str, Any]:
        return self.jira_request(
            "GET",
            f"/issue/{ticket_key}?fields=status,labels,statuscategorychangedate,updated",
        )

    def fetch_issue_comments(self, ticket_key: str) -> list[dict[str, Any]]:
        resp = self.jira_request("GET", f"/issue/{ticket_key}/comment?maxResults=100")
        return list(resp.get("comments", []))

    def transition_ticket(self, ticket_key: str, transition_name: str) -> None:
        self.jira_request(
            "POST",
            f"/issue/{ticket_key}/transitions",
            {"transition": {"id": jira_dispatch.TRANSITION_IDS[transition_name]}},
        )

    def transition_to_published(self, ticket_key: str) -> None:
        self.transition_ticket(ticket_key, "to_published")

    def transition_to_archived(self, ticket_key: str) -> None:
        self.transition_ticket(ticket_key, "to_archived")

    def add_jira_comment(self, ticket_key: str, message: str) -> None:
        # OP-844 — defense-in-depth egress filter. The bridge daemon
        # itself never invokes Claude (the AI Reviewer pipeline it
        # spawns lives in ``ai_reviewer.review_patchset`` where the
        # primary defense fires), but anything ``add_jira_comment``
        # forwards may have been composed from upstream LLM output;
        # this second pass guarantees no api-key-shape token ever
        # leaves the bridge process even if a future caller forgets
        # to sanitize.
        sanitized, matches = reviewer_safety.egress_filter(message)
        if matches:
            self.log(
                "ALERT", "bridge_jira_comment_redacted",
                ticket_key=ticket_key,
                redaction_count=len(matches),
            )
        body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{
                    "type": "paragraph",
                    "content": [{"type": "text", "text": sanitized}],
                }],
            },
        }
        self.jira_request("POST", f"/issue/{ticket_key}/comment", body)

    def query_gerrit_change(self, query: str) -> GerritChange | None:
        cmd = self._ssh_cmd("gerrit", "query", "--format=JSON", query)
        backoff = 1.0
        for attempt in range(1, 4):
            result = self.run_command(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=self._ssh_env(),
            )
            blob = (result.stderr or "") + "\n" + (result.stdout or "")
            if result.returncode == 0:
                return self._parse_gerrit_query_output(result.stdout)
            if self._is_auth_failure(blob):
                self.log("ALERT", "gerrit_auth_failed", err=blob[-500:])
                raise GerritAuthError("Gerrit SSH auth failed")
            if attempt < 3:
                self.log("WARN", "gerrit_query_retry", err=blob[-500:], attempt=attempt)
                self.sleep(backoff)
                backoff = min(backoff * 2, self.config.max_backoff_seconds)
                continue
            self.log("ERROR", "gerrit_query_failed", err=blob[-500:])
            return None

    def replay_from_cursor(self) -> None:
        if self.config.cursor_file is None:
            return
        cursor = load_cursor(self.config.cursor_file)
        if cursor is None:
            if not self._cursor_missing_warned:
                self.log(
                    "WARN",
                    "event_cursor_missing_first_run",
                    cursor_file=str(self.config.cursor_file),
                )
                self._cursor_missing_warned = True
            return
        event_id, last_ts = cursor
        self.log(
            "INFO",
            "replaying_missed_events",
            last_event=event_id,
            since=last_ts.isoformat(),
        )
        self.replay_missed_events(last_ts)

    def replay_missed_events(self, last_ts: datetime) -> int:
        since_str = last_ts.strftime("%Y-%m-%d %H:%M:%S")
        query = (
            f"project:{jira_dispatch.GERRIT_PROJECT_PATH} "
            f'status:merged after:"{since_str}"'
        )
        cmd = self._ssh_cmd(
            "gerrit",
            "query",
            "--format=JSON",
            query,
            "--current-patch-set",
        )
        result = self.run_command(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env=self._ssh_env(),
        )
        blob = (result.stderr or "") + "\n" + (result.stdout or "")
        if result.returncode != 0:
            if self._is_auth_failure(blob):
                self.log("ALERT", "gerrit_auth_failed", err=blob[-500:])
                raise GerritAuthError("Gerrit SSH auth failed")
            self.log("ERROR", "replay_query_failed", err=blob[-500:], since=since_str)
            return 0

        backfill_count = 0
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                change = json.loads(line)
            except json.JSONDecodeError:
                continue
            if change.get("type") == "stats":
                continue
            synthetic_event = {
                "type": "change-merged",
                "change": change,
                "_backfilled": True,
            }
            self.process_stream_event(synthetic_event)
            backfill_count += 1
        self.log("INFO", "replay_complete", backfill_count=backfill_count, since=since_str)
        return backfill_count

    def save_event_cursor(self, event: dict[str, Any]) -> None:
        if self.config.cursor_file is None:
            return
        event_id = str(event.get("id") or uuid4())
        save_cursor(event_id, datetime.now(timezone.utc), self.config.cursor_file)

    def _parse_gerrit_query_output(self, stdout: str) -> GerritChange | None:
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("type") == "stats":
                continue
            change_id = str(item.get("id") or item.get("change_id") or "")
            if not change_id:
                continue
            return GerritChange(
                change_id=change_id,
                number=str(item.get("number") or "") or None,
                subject=str(item.get("subject") or ""),
                status=str(item.get("status") or ""),
                branch=str(item.get("branch") or ""),
            )
        return None

    def startup_catchup(self) -> None:
        self.log("INFO", "catchup_start")
        for issue in self.search_catchup_candidate_tickets():
            ticket_key = issue["key"]
            try:
                self.process_catchup_ticket(ticket_key)
            except BridgeFatalError:
                raise
            except Exception as exc:
                self.log("ERROR", "catchup_ticket_failed", ticket_key=ticket_key, err=str(exc))
        self.log("INFO", "catchup_done")

    def process_catchup_ticket(self, ticket_key: str) -> None:
        comments = self.fetch_issue_comments(ticket_key)
        change_numbers = extract_change_numbers_from_comments(comments)
        if not change_numbers:
            self.log("INFO", "catchup_no_change_mapping", ticket_key=ticket_key)
            return
        if len(change_numbers) > 1:
            self.log(
                "ERROR",
                "multiple_changes_for_ticket",
                ticket_key=ticket_key,
                err=",".join(change_numbers),
            )
            return
        change = self.query_gerrit_change(change_numbers[0])
        if change is None:
            return
        if change.status.upper() != "MERGED":
            self.log("INFO", "catchup_change_not_merged", ticket_key=ticket_key, change_id=change.change_id)
            return
        self.process_ticket_for_change(
            ticket_key,
            change.change_id,
            allow_auto_archive=True,
        )

    def process_stream_event(self, event: dict[str, Any]) -> None:
        self.counters.events_received += 1
        self.counters.last_event_at_ts = utc_now_iso()
        self._append_coordinator_bridge_event(event)
        event_type = event.get("type")
        if event_type == "change-merged":
            change = extract_gerrit_change(event)
            self.log(
                "INFO",
                "change_merged_event",
                change_id=change.change_id,
                source="replay" if event.get("_backfilled") else "live",
            )
            self._emit_ps_merged_metrics(event)
            self._handle_change_merged(event)
            self._schedule_auto_rebase_sweep(event)
            self._on_develop_merge_advance(event)
            return
        if event_type == "patchset-created":
            self._handle_patchset_created(event)
            return
        if event_type == "comment-added":
            # OP-746 — record Verified -1 votes as conflict observations
            # so the daily report can surface CI-failure clusters as
            # conflict-equivalent friction signals.
            self._record_verified_minus_one_if_applicable(event)
            return
        # Other event types are ignored — extend here if/when the daemon
        # gains additional duties (e.g. comment-added → coder-fix flow).

    def _append_coordinator_bridge_event(self, event: dict[str, Any]) -> None:
        """Best-effort event tap for the release-pipeline coordinator."""

        try:
            append_coordinator_bridge_event(
                event,
                path=self.config.coordinator_bridge_events_path,
            )
        except OSError as exc:
            self.log(
                "WARN",
                "coordinator_bridge_event_write_failed",
                err=f"{type(exc).__name__}: {exc}",
            )

    # ─── change-merged → ps_merged_metrics (OP-746) ─────────────────

    def _emit_ps_merged_metrics(self, event: dict[str, Any]) -> None:
        """Emit one ``ps_merged_metrics`` log line per merged change.

        Errors are swallowed — telemetry must never break the OP-689
        ticket-transition path. The daily report (OP-746
        ``scripts/conflict_report.py``) parses these lines from the
        bridge's structured-log stream.
        """
        try:
            metrics = compute_ps_merged_metrics(event)
        except Exception as exc:  # pragma: no cover — defensive
            self.log(
                "WARN", "ps_merged_metrics_compute_failed",
                err=f"{type(exc).__name__}: {exc}",
            )
            return
        self.log("INFO", "ps_merged_metrics", **metrics)

    # ─── comment-added → conflict observations (OP-746) ─────────────

    def _record_verified_minus_one_if_applicable(
        self, event: dict[str, Any],
    ) -> None:
        """Detect a Verified -1 vote and write one conflict-observation row.

        Gerrit emits ``comment-added`` for every label vote, including
        ``Verified -1``. The vote payload is in the ``approvals`` array
        on the event — each approval carries ``type``, ``value``, and
        (for ``comment-added``) an ``oldValue`` so we know if THIS event
        is the one that flipped the label to -1.
        """
        approvals = event.get("approvals") or []
        triggered = False
        for approval in approvals:
            if approval.get("type") != "Verified":
                continue
            try:
                value = int(approval.get("value", 0))
            except (TypeError, ValueError):
                continue
            if value > -1:
                continue
            old_raw = approval.get("oldValue")
            try:
                old_value = int(old_raw) if old_raw is not None else None
            except (TypeError, ValueError):
                old_value = None
            # Only record on the transition INTO -1 to avoid double-
            # counting re-fires of the same vote (Gerrit replays an
            # approvals snapshot on every comment-added).
            if old_value is None or old_value > -1:
                triggered = True
                break
        if not triggered:
            return

        change = extract_gerrit_change(event)
        ticket_keys = extract_ticket_keys_from_subject(change.subject)
        ticket = ticket_keys[0] if ticket_keys else None
        change_number = _coerce_int(change.number)

        try:
            from datetime import datetime, timezone
            from backend.agents.conflict_observations import (
                ConflictObservation,
                record_observation_sync,
            )
            obs = ConflictObservation(
                ts=datetime.now(timezone.utc),
                cause_category="verified_minus_one",
                files_in_conflict=(),
                ps_change_id=change.change_id or None,
                ps_change_number=change_number,
                ticket=ticket,
                pre_existing_open_count=0,
            )
            record_observation_sync(obs, log=self.log)
        except Exception as exc:  # noqa: BLE001
            self.log(
                "WARN", "verified_minus_one_record_failed",
                change_id=change.change_id,
                err=f"{type(exc).__name__}: {exc}",
            )

    # ─── change-merged → JIRA Published (OP-689, OP-743) ────────────

    def _handle_change_merged(self, event: dict[str, Any]) -> None:
        change = extract_gerrit_change(event)
        if change.branch and change.branch != "develop":
            self.log("INFO", "change_merged_non_develop_skip", change_id=change.change_id, branch=change.branch)
            return
        ticket_keys = extract_ticket_keys_from_subject(change.subject)
        if not ticket_keys and change.change_id:
            queried = self.query_gerrit_change(f"change:{change.change_id}")
            if queried is not None:
                change = queried
                ticket_keys = extract_ticket_keys_from_subject(change.subject)
        if not ticket_keys:
            self.log("INFO", "change_no_matching_ticket", change_id=change.change_id)
            return
        if len(ticket_keys) > 1:
            self.log(
                "ERROR",
                "multiple_tickets_for_change",
                change_id=change.change_id,
                err=",".join(ticket_keys),
            )
            return
        self.process_ticket_for_change(
            ticket_keys[0],
            change.change_id,
            allow_auto_archive=True,
        )

    # ─── patchset-created → proactive merger + AI Reviewer (OP-715/801) ──

    def _handle_patchset_created(self, event: dict[str, Any]) -> None:
        """Spawn proactive merger + AI Reviewer as fire-and-forget threads.

        OP-715 / OP-801 — Gerrit's webhooks plugin v3.13.5 has no auth
        surface, so the proactive merger trigger that OP-714 attempted
        to wire through ``/webhooks/gerrit`` runs from this
        stream-events daemon instead, and OP-801 mirrors that for the
        AI Reviewer (OP-713). SSH transport is auth'd at the protocol
        layer (``claude-bot`` SSH key), so we don't need any per-event
        signature.

        The shared decision logic lives in
        :func:`backend.routers.webhooks._proactive_merger_check` and
        :func:`backend.routers.webhooks._ai_reviewer_check` — both
        keep ONE implementation so a future webhook-auth fix and the
        daemon stay in sync without drift. Each spawns its own
        per-event daemon thread because the two pipelines are
        independent (a merger crash must not strand the AI review,
        and vice versa).

        Bridging async pipelines with a per-event daemon thread that
        calls ``asyncio.run`` is acceptable here because:

          1. Skip conditions early-exit fast (~10 ms each on the
             cached path), so most events do NOT actually start an
             event loop.
          2. The merger invocation is rate-limited by the
             ``Merger-Proactive-PS<n>`` hashtag throttle, and the AI
             Reviewer is rate-limited by the
             ``ai_reviewer._THROTTLE`` ``(change_id, revision)`` map,
             so we cap at one slow path per (change, patchset) per
             pipeline.
          3. Daemon threads are auto-reaped on process shutdown, so
             the systemd ``KillSignal=SIGTERM`` + ``TimeoutStopSec``
             still cleans up.

        Errors inside the spawned threads MUST NOT propagate up to
        the stream loop — losing one merger check or one AI review
        is recoverable (next patchset re-runs the pipeline), but
        losing the daemon means OP-689's change-merged → Published
        transitions stop too.
        """
        self._spawn_proactive_merger_thread(event)
        self._spawn_ai_reviewer_thread(event)

    # ─── OP-1196 phase 3b — startup conflict backfill ────────────────
    #
    # The daemon's `_handle_patchset_created` only fires on LIVE Gerrit
    # stream events. Conflicts that already exist when the daemon starts
    # (or are created during a daemon downtime window) never reach
    # `_proactive_merger_check` — they sit on Gerrit with no
    # Merger-Proactive-PS<n> hashtag, neither attempted nor resolved.
    #
    # `backfill_existing_conflicts` closes that gap at startup: it queries
    # Gerrit for open changes, filters down to "this is a real conflict
    # the merger pipeline hasn't seen yet", and synthesizes a
    # patchset-created event for each — feeding them into the same
    # ``_spawn_proactive_merger_thread`` path that live events use. From
    # the merger's perspective the backfilled event is indistinguishable
    # from a live one (same threading model, same skip-checks, same
    # POST-to-backend flow, same daemon-side push if backend returns
    # ``merger_resolved_pending_caller_push``).
    #
    # Filter rules — a change is a backfill candidate iff:
    #
    #   * `mergeable == False` in the gerrit query response (the merger
    #     wouldn't have anything to do if mergeable=True);
    #   * the change is `status:open` and `-is:wip`;
    #   * NO hashtag matching `Merger-Proactive-PS*` is set (would mean
    #     the merger already attempted this PS — daemon's throttle would
    #     skip it anyway, but we save the round-trip);
    #   * NO `Merge-Conflict-Resolved` hashtag (merger already succeeded;
    #     change is awaiting human +2);
    #   * the current patchset uploader is NOT merger-agent-bot itself
    #     (loop prevention — same check `_proactive_merger_check` does
    #     for live events, applied earlier here to avoid a wasted
    #     thread spawn);
    #
    # Skipped changes are counted + logged but otherwise silent —
    # operators grep the `backfill_complete` line to see how the scan
    # decomposed.

    @staticmethod
    def _select_backfill_candidates(
        gerrit_query_output: str,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Pure function — parse the ``gerrit query --current-patch-set
        --format=JSON`` output, apply the backfill filter rules, and
        return ``(candidate_change_dicts, counters_by_skip_reason)``.

        Split out from the orchestrator below so it can be unit-tested
        without mocking subprocess or threading. Each element of the
        returned candidates list is the FULL change dict from gerrit
        — the caller synthesizes the event envelope from it.
        """
        import json as _json

        counters = {
            "scanned": 0,
            "skipped_mergeable": 0,
            "skipped_already_attempted": 0,
            "skipped_resolved_awaiting_human": 0,
            "skipped_uploader_is_merger": 0,
            "skipped_no_current_patchset": 0,
            "skipped_unparseable": 0,
            "candidates": 0,
        }
        candidates: list[dict[str, Any]] = []

        for raw in gerrit_query_output.splitlines():
            if not raw.strip():
                continue
            try:
                obj = _json.loads(raw)
            except Exception:
                counters["skipped_unparseable"] += 1
                continue

            # gerrit query emits a stats line at the end like
            # {"type":"stats","rowCount":N,...}. Skip non-change rows.
            if obj.get("type") == "stats" or "rowCount" in obj:
                continue
            if not obj.get("id") or not obj.get("number"):
                continue

            counters["scanned"] += 1

            # Mergeable → nothing to do.
            if obj.get("mergeable") is True:
                counters["skipped_mergeable"] += 1
                continue

            hashtags = obj.get("hashtags") or []
            if any(
                h.startswith("Merger-Proactive-PS") for h in hashtags
            ):
                counters["skipped_already_attempted"] += 1
                continue
            if "Merge-Conflict-Resolved" in hashtags:
                counters["skipped_resolved_awaiting_human"] += 1
                continue

            cps = obj.get("currentPatchSet") or {}
            if not cps.get("revision") or not cps.get("number"):
                counters["skipped_no_current_patchset"] += 1
                continue

            uploader = cps.get("uploader") or {}
            uploader_username = (uploader.get("username") or "").lower()
            uploader_name = (uploader.get("name") or "").lower()
            uploader_email = (uploader.get("email") or "").lower()
            if (
                "merger-agent-bot" in uploader_username
                or "merger-agent-bot" in uploader_name
                or "merger-bot" in uploader_email
            ):
                counters["skipped_uploader_is_merger"] += 1
                continue

            counters["candidates"] += 1
            candidates.append(obj)

        return candidates, counters

    @staticmethod
    def _synthesize_patchset_created_event(
        change_obj: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert a `gerrit query --current-patch-set` change dict into
        the same event envelope shape `_handle_patchset_created` expects
        from a live stream event. The merger code paths read change.*
        and patchSet.* keys; we populate the ones they touch + a few
        extras (branch / subject / owner) for log-line clarity."""
        cps = change_obj.get("currentPatchSet") or {}
        return {
            "type": "patchset-created",
            "change": {
                "id": change_obj.get("id"),
                "number": change_obj.get("number"),
                "project": change_obj.get("project"),
                "branch": change_obj.get("branch"),
                "subject": change_obj.get("subject"),
                "owner": change_obj.get("owner") or {},
                "url": change_obj.get("url"),
                "hashtags": change_obj.get("hashtags") or [],
            },
            "patchSet": {
                "number": cps.get("number"),
                "revision": cps.get("revision"),
                "uploader": cps.get("uploader") or {},
                "parents": cps.get("parents") or [],
                "ref": cps.get("ref"),
            },
        }

    def backfill_existing_conflicts(self) -> int:
        """OP-1196 phase 3b — synthesize patchset-created events for
        pre-existing conflicts the daemon hasn't seen yet.

        Returns the number of events synthesized (= threads spawned).
        Never raises — backfill failure must not block the main stream
        loop. Designed to be called ONCE at startup, after the asyncpg
        pool + GerritClient prewarm cache are initialised.

        Subprocess uses ``self.run_command`` (the same injection point
        the rest of the bridge uses for testability) — tests pass a
        fake ``run_command`` that returns a stub gerrit query output.
        """
        # Lazy import — settings has heavy module-load side effects that
        # tests + unit-mode invocations otherwise pay needlessly. The
        # backfill is one-shot at daemon startup, so the import cost
        # here is negligible.
        try:
            from backend.config import settings as _settings
        except Exception:                                 # pragma: no cover
            _settings = None  # type: ignore[assignment]

        project = (
            os.environ.get("OMNISIGHT_GERRIT_PROJECT", "").strip()
            or (getattr(_settings, "gerrit_project", "") if _settings else "")
        )
        if not project:
            self.log(
                "WARN", "backfill_skipped",
                reason="OMNISIGHT_GERRIT_PROJECT not configured",
            )
            return 0

        ssh_host = (
            os.environ.get("OMNISIGHT_GERRIT_SSH_HOST", "").strip()
            or (getattr(_settings, "gerrit_ssh_host", "") if _settings else "")
        )
        ssh_port_raw = (
            os.environ.get("OMNISIGHT_GERRIT_SSH_PORT", "").strip()
            or str(
                getattr(_settings, "gerrit_ssh_port", 29418) or 29418
                if _settings else 29418
            )
        )
        try:
            ssh_port = int(ssh_port_raw)
        except ValueError:
            ssh_port = 29418
        ssh_key = (
            os.environ.get("OMNISIGHT_GIT_SSH_KEY_PATH", "").strip()
            or (getattr(_settings, "git_ssh_key_path", "") if _settings else "")
        )

        if not ssh_host:
            self.log("WARN", "backfill_skipped",
                     reason="OMNISIGHT_GERRIT_SSH_HOST not configured")
            return 0

        # OP-1196 phase 3b hotfix (2026-05-17 ~03:07): the search
        # expression MUST be passed as a single SHELL-QUOTED token to
        # the remote `gerrit query` CLI — otherwise `-is:wip` (a
        # Gerrit search NEGATION, not a CLI option) is interpreted by
        # Gerrit's argparser as an unknown CLI option and aborts with
        # `fatal: "-is:wip" is not a valid option`. Verified live:
        #   $ ssh ... gerrit query project:X status:open -is:wip
        #   fatal: "-is:wip" is not a valid option
        #   $ ssh ... "gerrit query 'project:X status:open -is:wip'"
        #   {"number":685,...}                     ← works
        # SSH concatenates the argv after the host into a single
        # command string for the remote shell, which re-tokenises by
        # whitespace. shlex.quote() preserves the search expression
        # as one token across that round-trip.
        import shlex as _shlex
        search_expr = f"project:{project} status:open -is:wip"

        args: list[str] = ["ssh"]
        if ssh_key:
            args.extend(["-i", str(ssh_key)])
        args.extend([
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
            "-p", str(ssh_port),
            ssh_host,
            "gerrit", "query",
            "--current-patch-set",
            "--format=JSON",
            _shlex.quote(search_expr),
        ])

        try:
            proc = self.run_command(
                args, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            self.log("WARN", "backfill_gerrit_query_timeout")
            return 0
        except Exception as exc:                         # pragma: no cover
            self.log(
                "ERROR", "backfill_gerrit_query_raised",
                err=f"{type(exc).__name__}: {exc}",
            )
            return 0

        if proc.returncode != 0:
            self.log(
                "WARN", "backfill_gerrit_query_failed",
                rc=proc.returncode,
                err=(proc.stderr or "").strip()[:300],
            )
            return 0

        candidates, counters = self._select_backfill_candidates(
            proc.stdout or "",
        )

        for change_obj in candidates:
            event = self._synthesize_patchset_created_event(change_obj)
            self.log(
                "INFO", "backfill_synthesize_event",
                change_id=str(change_obj.get("number") or ""),
                ps=str(
                    (change_obj.get("currentPatchSet") or {}).get(
                        "number") or ""
                ),
                hashtags=change_obj.get("hashtags") or [],
            )
            self._spawn_proactive_merger_thread(event)

        self.log(
            "INFO", "backfill_complete",
            project=project,
            **counters,
        )
        return counters["candidates"]

    def _spawn_proactive_merger_thread(self, event: dict[str, Any]) -> None:
        """OP-715 — fire the proactive merger pipeline in a daemon thread."""
        try:
            from backend.routers.webhooks import _proactive_merger_check
        except Exception as exc:  # pragma: no cover — import fail = bug
            self.log(
                "ERROR", "proactive_merger_import_failed",
                err=f"{type(exc).__name__}: {exc}",
            )
            return

        change = event.get("change") or {}
        patchset = event.get("patchSet") or {}
        change_number = change.get("number")
        ps_number = patchset.get("number")

        def _runner() -> None:
            import asyncio
            try:
                asyncio.run(_proactive_merger_check(event))
            except Exception as exc:  # pragma: no cover — async runtime safety
                self.log(
                    "ERROR", "proactive_merger_thread_error",
                    change_id=str(change_number) if change_number else "",
                    ps=str(ps_number) if ps_number else "",
                    err=f"{type(exc).__name__}: {exc}",
                )

        import threading
        thread = threading.Thread(
            target=_runner,
            name=f"proactive-merger-{change_number}-{ps_number}",
            daemon=True,
        )
        thread.start()
        self.log(
            "INFO", "proactive_merger_thread_spawned",
            change_id=str(change_number) if change_number else "",
            ps=str(ps_number) if ps_number else "",
        )

    def _spawn_ai_reviewer_thread(self, event: dict[str, Any]) -> None:
        """OP-801 — fire the AI Reviewer pipeline in a daemon thread.

        Loop prevention (uploader=merger-agent-bot) and the 24 h
        ``(change_id, revision)`` throttle live inside
        :func:`backend.routers.webhooks._ai_reviewer_check`, so the
        spawn here is unconditional — the inner pipeline early-exits
        on skip conditions and emits its own ``ai_reviewer_skip`` log
        line.
        """
        try:
            from backend.routers.webhooks import _ai_reviewer_check
        except Exception as exc:  # pragma: no cover — import fail = bug
            self.log(
                "ERROR", "ai_reviewer_import_failed",
                err=f"{type(exc).__name__}: {exc}",
            )
            return

        change = event.get("change") or {}
        patchset = event.get("patchSet") or {}
        change_number = change.get("number")
        ps_number = patchset.get("number")

        def _runner() -> None:
            import asyncio
            try:
                asyncio.run(_ai_reviewer_check(event))
            except Exception as exc:  # pragma: no cover — async runtime safety
                self.log(
                    "ERROR", "ai_reviewer_thread_error",
                    change_id=str(change_number) if change_number else "",
                    ps=str(ps_number) if ps_number else "",
                    err=f"{type(exc).__name__}: {exc}",
                )

        import threading
        thread = threading.Thread(
            target=_runner,
            name=f"ai-reviewer-{change_number}-{ps_number}",
            daemon=True,
        )
        thread.start()
        self.log(
            "INFO", "ai_reviewer_thread_spawned",
            change_id=str(change_number) if change_number else "",
            ps=str(ps_number) if ps_number else "",
        )

    # ─── change-merged → merger drift re-evaluation (OP-1409) ───────

    def _on_develop_merge_advance(self, event: dict[str, Any]) -> None:
        """Schedule a debounced sweep when ``develop`` advances.

        Type β conflicts arrive after upload: a PS was mergeable when
        ``patchset-created`` fired, then a later develop merge makes it
        unmergeable. This schedules one sweep per develop-merge burst
        and feeds newly-unmergeable changes back into the proactive
        merger path using the existing synthetic patchset-created shape.
        """
        try:
            change = event.get("change") or {}
            branch = str(
                change.get("branch")
                or change.get("ref")
                or event.get("refName")
                or ""
            )
            if branch.startswith("refs/heads/"):
                branch = branch.removeprefix("refs/heads/")
            if branch and branch != "develop":
                return
            project = str(change.get("project") or "")
            merged_sha = (
                str(event.get("newRev") or "")
                or str((event.get("patchSet") or {}).get("revision") or "")
            )
            scheduler = self._ensure_develop_drift_scheduler()
            scheduler.schedule(project=project, merged_sha=merged_sha)
            self.log(
                "INFO", "merger_drift_sweep_scheduled",
                project=project, merged_sha=merged_sha,
                delay_s=self.config.merger_drift_debounce_seconds,
            )
        except Exception as exc:  # pragma: no cover — defensive
            self.log(
                "ERROR", "merger_drift_schedule_error",
                err=f"{type(exc).__name__}: {exc}",
            )

    def _ensure_develop_drift_scheduler(self) -> Any:
        if self._develop_drift_scheduler is not None:
            return self._develop_drift_scheduler
        from backend.agents.auto_rebase import DebouncedSweepScheduler

        def _runner(project: str, merged_sha: str) -> None:
            try:
                self._run_develop_drift_sweep(
                    project=project, merged_sha=merged_sha,
                )
            except Exception as exc:  # pragma: no cover — defensive
                self.log(
                    "ERROR", "merger_drift_sweep_runner_error",
                    err=f"{type(exc).__name__}: {exc}",
                )

        self._develop_drift_scheduler = DebouncedSweepScheduler(
            runner=_runner,
            delay_seconds=self.config.merger_drift_debounce_seconds,
            log=lambda *args, **kwargs: None,
        )
        return self._develop_drift_scheduler

    def _run_develop_drift_sweep(
        self, *, project: str = "", merged_sha: str = "",
    ) -> int:
        """Re-check open develop PSes and re-evaluate new conflicts."""
        self.log(
            "INFO", "merger_drift_sweep_start",
            project=project, merged_sha=merged_sha,
        )
        changes = self._query_open_develop_changes()
        triggered = 0
        for change_obj in changes:
            change_key = str(
                change_obj.get("number")
                or change_obj.get("_number")
                or change_obj.get("id")
                or ""
            )
            if not change_key:
                continue
            mergeable = self._fetch_current_mergeable(change_key)
            if mergeable is None:
                continue
            if not self._should_re_evaluate_drift(change_key, mergeable):
                continue
            staleness = jira_dispatch.assess_patchset_staleness(
                change_obj.get("currentPatchSet") or {},
                run_command=self.run_command,
            )
            if staleness is not None and staleness.should_abstain:
                self._post_merger_staleness_abstain(change_obj, staleness)
                self.log(
                    "INFO", "merger_drift_re_eval_skipped_stale_ps",
                    reason="staleness_exceeded",
                    change=str(change_obj.get("number") or ""),
                    age_days=round(staleness.age_days, 1),
                    commits_behind=staleness.commits_behind,
                )
                continue
            event = self._synthesize_patchset_created_event(change_obj)
            self.log(
                "INFO", "merger_drift_re_eval_triggered",
                reason="merger_drift_detected_re_evaluation",
                change=str(change_obj.get("number") or ""),
                ps=str(
                    (change_obj.get("currentPatchSet") or {}).get("number")
                    or ""
                ),
                project=str(change_obj.get("project") or ""),
            )
            self._spawn_proactive_merger_thread(event)
            triggered += 1
        self.log(
            "INFO", "merger_drift_sweep_complete",
            project=project, merged_sha=merged_sha,
            scanned=len(changes), triggered=triggered,
        )
        return triggered

    def _post_merger_staleness_abstain(
        self,
        change_obj: dict[str, Any],
        staleness: jira_dispatch.PatchSetStaleness,
    ) -> None:
        """Flag a stale PS in JIRA instead of invoking the merger."""
        ticket_keys = extract_ticket_keys_from_subject(
            str(change_obj.get("subject") or "")
        )
        if not ticket_keys:
            return
        comment = jira_dispatch._format_ps_staleness_comment(
            marker="merger-ps-staleness-abstain",
            assessment=staleness,
            change_number=change_obj.get("number") or change_obj.get("_number"),
        )
        for ticket_key in ticket_keys:
            try:
                jira_dispatch.add_comment(
                    self.client,
                    ticket_key,
                    comment,
                    idem_key=f"merger-ps-staleness-abstain-{ticket_key}",
                )
            except Exception as exc:
                self.log(
                    "WARN", "merger_staleness_jira_comment_failed",
                    ticket=ticket_key,
                    err=f"{type(exc).__name__}: {exc}",
                )

    def _query_open_develop_changes(self) -> list[dict[str, Any]]:
        cmd = self._ssh_cmd(
            "gerrit", "query", "--current-patch-set", "--format=JSON",
            "status:open", "branch:develop",
        )
        try:
            result = self.run_command(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=self._ssh_env(),
            )
        except subprocess.TimeoutExpired:
            self.log("WARN", "merger_drift_gerrit_query_timeout")
            return []
        except Exception as exc:  # pragma: no cover — defensive
            self.log(
                "ERROR", "merger_drift_gerrit_query_raised",
                err=f"{type(exc).__name__}: {exc}",
            )
            return []
        if result.returncode != 0:
            self.log(
                "WARN", "merger_drift_gerrit_query_failed",
                rc=result.returncode,
                err=((result.stderr or "") + "\n" + (result.stdout or ""))[-500:],
            )
            return []

        changes: list[dict[str, Any]] = []
        for raw in (result.stdout or "").splitlines():
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "stats" or "rowCount" in obj:
                continue
            if obj.get("id") or obj.get("number") or obj.get("_number"):
                changes.append(obj)
        return changes

    def _fetch_current_mergeable(self, change_key: str) -> bool | None:
        rest_base = self._gerrit_rest_base_url()
        path_key = urllib.parse.quote(change_key, safe="")
        url = f"{rest_base}/changes/{path_key}/revisions/current/mergeable"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            with self.urlopen(req, timeout=30) as resp:
                payload = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            self.log(
                "WARN", "merger_drift_mergeable_fetch_failed",
                change=change_key, status=exc.code, err=detail[:300],
            )
            return None
        except Exception as exc:
            self.log(
                "WARN", "merger_drift_mergeable_fetch_error",
                change=change_key, err=f"{type(exc).__name__}: {exc}",
            )
            return None
        body = payload.lstrip(")]}'\n").strip()
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.log("WARN", "merger_drift_mergeable_parse_error",
                     change=change_key)
            return None
        value = data.get("mergeable")
        return value if isinstance(value, bool) else None

    def _gerrit_rest_base_url(self) -> str:
        if self.config.gerrit_rest_base_url:
            return self.config.gerrit_rest_base_url.rstrip("/")
        host = self.config.gerrit_host
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        return f"https://{host}:29420"

    def _develop_drift_cooldown_file(self) -> Path | None:
        if self.config.drift_cooldown_file is not None:
            return self.config.drift_cooldown_file
        if self.config.cursor_file is None:
            return None
        return self.config.cursor_file.with_name("drift-cooldown.json")

    def _load_develop_drift_cooldown_state(self) -> None:
        path = self._develop_drift_cooldown_file()
        if path is None:
            return
        try:
            (
                self._develop_drift_last_seen_mergeable,
                self._develop_drift_last_re_eval,
            ) = load_drift_cooldown_state(path)
        except Exception as exc:
            self.log(
                "WARN", "merger_drift_cooldown_load_failed",
                path=str(path), err=f"{type(exc).__name__}: {exc}",
            )

    def _save_develop_drift_cooldown_state(self) -> None:
        path = self._develop_drift_cooldown_file()
        if path is None:
            return
        try:
            save_drift_cooldown_state(
                self._develop_drift_last_seen_mergeable,
                self._develop_drift_last_re_eval,
                path,
            )
        except Exception as exc:
            self.log(
                "WARN", "merger_drift_cooldown_save_failed",
                path=str(path), err=f"{type(exc).__name__}: {exc}",
            )

    def _should_re_evaluate_drift(
        self, change_key: str, mergeable: bool,
    ) -> bool:
        now = time.time()
        with self._develop_drift_lock:
            previous = self._develop_drift_last_seen_mergeable.get(change_key)
            if mergeable:
                self._develop_drift_last_seen_mergeable[change_key] = True
                self._save_develop_drift_cooldown_state()
                return False
            if previous is False:
                return False
            last_eval = self._develop_drift_last_re_eval.get(change_key)
            if (
                last_eval is not None
                and now - last_eval < self.config.merger_drift_cooldown_seconds
            ):
                self.log(
                    "INFO", "merger_drift_re_eval_skipped_cooldown",
                    change=change_key,
                    age_s=round(now - last_eval, 3),
                )
                return False
            self._develop_drift_last_seen_mergeable[change_key] = False
            self._develop_drift_last_re_eval[change_key] = now
            self._save_develop_drift_cooldown_state()
            return True

    # ─── change-merged → auto-rebase sweep (OP-733) ──────────────────

    def _schedule_auto_rebase_sweep(self, event: dict[str, Any]) -> None:
        """OP-733: schedule a debounced sweep of open bot-owned PSes.

        Fired alongside the OP-689 ticket-transition handler on every
        ``change-merged`` event for ``develop``. Uses a 30 s debounce
        (``BridgeConfig.auto_rebase_debounce_seconds``) so a batch +2
        produces ONE sweep on the latest merged SHA, not N sweeps.

        Errors are swallowed — a single bad sweep schedule must not
        crash the OP-689 transition pipeline.
        """
        try:
            change = event.get("change") or {}
            branch = str(change.get("branch") or "")
            if branch and branch != "develop":
                return
            project = str(change.get("project") or "")
            merged_sha = (
                str(event.get("newRev") or "")
                or str((event.get("patchSet") or {}).get("revision") or "")
            )
            if not (project and merged_sha):
                self.log(
                    "INFO", "auto_rebase_sweep_skip_missing_fields",
                    change_id=str(change.get("id") or ""),
                    project=project, merged_sha=merged_sha,
                )
                return
            scheduler = self._ensure_auto_rebase_scheduler()
            scheduler.schedule(project=project, merged_sha=merged_sha)
        except Exception as exc:  # pragma: no cover — defensive
            self.log(
                "ERROR", "auto_rebase_sweep_schedule_error",
                err=f"{type(exc).__name__}: {exc}",
            )

    def _ensure_auto_rebase_scheduler(self) -> Any:
        if self._auto_rebase_scheduler is not None:
            return self._auto_rebase_scheduler
        from backend.agents.auto_rebase import (
            AutoRebaseSweeper,
            DebouncedSweepScheduler,
        )
        self._auto_rebase_sweeper = AutoRebaseSweeper(
            ssh_cmd_builder=self._ssh_cmd,
            ssh_env_builder=self._ssh_env,
            run_command=self.run_command,
            notify_jira=self._post_auto_rebase_jira_comment,
            log=self.log,
        )
        sweeper = self._auto_rebase_sweeper

        def _runner(project: str, merged_sha: str) -> None:
            sweeper.sweep(project=project, merged_sha=merged_sha)

        self._auto_rebase_scheduler = DebouncedSweepScheduler(
            runner=_runner,
            delay_seconds=self.config.auto_rebase_debounce_seconds,
            log=self.log,
        )
        return self._auto_rebase_scheduler

    def _post_auto_rebase_jira_comment(
        self, ticket_key: str, message: str,
    ) -> None:
        """Post the auto-rebase notice as a JIRA comment.

        Routed through :meth:`jira_request` so the daemon's existing
        429 / 5xx retry logic + auth-error escalation applies.
        """
        body = {
            "body": {
                "type": "doc", "version": 1,
                "content": [{
                    "type": "paragraph",
                    "content": [{"type": "text", "text": message}],
                }],
            },
        }
        self.jira_request("POST", f"/issue/{ticket_key}/comment", body)

    def remove_jira_label(self, ticket_key: str, label: str) -> None:
        self.jira_request(
            "PUT",
            f"/issue/{ticket_key}",
            {"update": {"labels": [{"remove": label}]}},
        )

    def archive_age_days(self) -> int:
        if self.config.archive_age_days is not None:
            return self.config.archive_age_days
        return archive_age_days_from_env()

    def run_archive_sweep(self) -> int:
        archived = 0
        self.log(
            "INFO",
            "archive_sweep_start",
            archive_age_days=self.archive_age_days(),
        )
        for issue in self.search_archive_candidate_tickets():
            ticket_key = str(issue.get("key") or "")
            if not ticket_key:
                continue
            fields = issue.get("fields") or {}
            if self.maybe_archive_published_ticket(
                ticket_key,
                change_id="daily-archive-sweep",
                issue_fields=fields,
            ):
                archived += 1
        self.log("INFO", "archive_sweep_done", archived=archived)
        return archived

    def maybe_archive_published_ticket(
        self,
        ticket_key: str,
        change_id: str,
        *,
        issue_fields: dict[str, Any] | None = None,
    ) -> bool:
        if issue_fields is None:
            issue = self.fetch_issue_archive_fields(ticket_key)
            issue_fields = issue.get("fields") or {}

        status = str(((issue_fields.get("status") or {}).get("name")) or "")
        if status not in PUBLISHED_STATUS_NAMES:
            self.log(
                "INFO",
                "archive_skip_not_published",
                ticket_key=ticket_key,
                change_id=change_id,
                err=status,
            )
            return False

        labels = set(issue_fields.get("labels") or [])
        if KEEP_OPEN_LABEL in labels:
            self.log(
                "INFO",
                "archive_keep_open_skip",
                ticket_key=ticket_key,
                change_id=change_id,
                label=KEEP_OPEN_LABEL,
            )
            return False

        published_at_raw = (
            issue_fields.get("statuscategorychangedate")
            or issue_fields.get("updated")
            or ""
        )
        published_at = parse_jira_datetime(str(published_at_raw))
        if published_at is None:
            self.log(
                "WARN",
                "archive_skip_missing_published_at",
                ticket_key=ticket_key,
                change_id=change_id,
            )
            return False

        age_days = (
            datetime.now(timezone.utc) - published_at
        ).total_seconds() / 86400
        retention_days = self.archive_age_days()
        if age_days <= retention_days:
            self.log(
                "INFO",
                "archive_retention_not_met",
                ticket_key=ticket_key,
                change_id=change_id,
                age_days=round(age_days, 3),
                retention_days=retention_days,
            )
            return False

        self.transition_to_archived(ticket_key)
        self.counters.transitions_made += 1
        self.add_jira_comment(
            ticket_key,
            (
                "[bridge] Auto-archived after 公開済み retention window. "
                f"age_days={age_days:.1f}; retention_days={retention_days}; "
                f"source={change_id}."
            ),
        )
        self._record_archive_decision(
            ticket_key=ticket_key,
            change_id=change_id,
            age_days=age_days,
            retention_days=retention_days,
        )
        self.log(
            "INFO",
            "ticket_auto_archived",
            ticket_key=ticket_key,
            change_id=change_id,
            age_days=round(age_days, 3),
            retention_days=retention_days,
        )
        return True

    def _record_archive_decision(
        self,
        *,
        ticket_key: str,
        change_id: str,
        age_days: float,
        retention_days: int,
    ) -> None:
        event = {
            "ts": utc_now_iso(),
            "source": "gerrit-jira-bridge",
            "action_type": "auto_archive_published_ticket",
            "ticket_key": ticket_key,
            "change_id": change_id,
            "from_status": "公開済み",
            "to_status": "Archived",
            "age_days": round(age_days, 3),
            "retention_days": retention_days,
        }
        try:
            root = Path(
                os.environ.get(
                    "OMNISIGHT_COORDINATOR_DECISION_LOG_DIR",
                    "~/.config/omnisight/coordinator/decision-log",
                )
            ).expanduser()
            root.mkdir(parents=True, exist_ok=True)
            date_slug = datetime.now(timezone.utc).date().isoformat()
            log_path = root / f"{date_slug}.jsonl"
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, sort_keys=True) + "\n")
        except Exception as exc:  # noqa: BLE001
            self.log(
                "WARN",
                "archive_decision_log_write_failed",
                ticket_key=ticket_key,
                err=f"{type(exc).__name__}: {exc}",
            )

    def process_ticket_for_change(
        self,
        ticket_key: str,
        change_id: str,
        *,
        allow_auto_archive: bool = False,
    ) -> bool:
        lock = self._lock_for(ticket_key)
        with lock:
            status = self.fetch_issue_status(ticket_key)
            original_status = status
            if status in PUBLISHED_STATUS_NAMES:
                if allow_auto_archive:
                    try:
                        return self.maybe_archive_published_ticket(
                            ticket_key,
                            change_id,
                        )
                    except Exception as exc:
                        self.log(
                            "ERROR",
                            "ticket_auto_archive_failed",
                            ticket_key=ticket_key,
                            change_id=change_id,
                            err=f"{type(exc).__name__}: {exc}",
                        )
                        return False
                self.log(
                    "INFO",
                    "ticket_already_published",
                    ticket_key=ticket_key,
                    change_id=change_id,
                )
                return False
            if status in ARCHIVED_STATUS_NAMES:
                self.log(
                    "WARN",
                    "ticket_archived_skip",
                    ticket_key=ticket_key,
                    change_id=change_id,
                    err=status,
                )
                return False
            if not self.medical_readiness_ok_for_closure(ticket_key):
                return False
            try:
                if status in IN_PROGRESS_STATUS_NAMES:
                    self.transition_ticket(ticket_key, "to_under_review")
                    self.counters.transitions_made += 1
                    status = "Under Review"
                if status in UNDER_REVIEW_STATUS_NAMES:
                    self.transition_ticket(ticket_key, "to_approved")
                    self.counters.transitions_made += 1
                    status = "Approved"
                if status in APPROVED_STATUS_NAMES:
                    self.transition_ticket(ticket_key, "to_published")
                    self.counters.transitions_made += 1
                    self.remove_jira_label(ticket_key, jira_dispatch.MIGRATION_IN_FLIGHT_LABEL)
                else:
                    self.log(
                        "WARN",
                        "ticket_unexpected_status_skip",
                        ticket_key=ticket_key,
                        change_id=change_id,
                        err=status,
                    )
                    return False
            except Exception as exc:
                self.log(
                    "ERROR",
                    "ticket_force_publish_transition_failed",
                    ticket_key=ticket_key,
                    change_id=change_id,
                    err=f"{type(exc).__name__}: {exc}",
                    from_state=original_status,
                    at_state=status,
                )
                return False
            self.log(
                "INFO",
                "ticket_force_published",
                ticket_key=ticket_key,
                change_id=change_id,
                from_state=original_status,
            )
            self.add_jira_comment(
                ticket_key,
                (
                    "[bridge] Gerrit change merged; force-transitioned ticket "
                    f"from {original_status} to 公開済み. "
                    "(Previous state was unexpected; see OP-743 for the "
                    "force-walk fix that allows this.)"
                ),
            )
            return True

    def medical_readiness_labels_and_summary(
        self,
        ticket_key: str,
    ) -> tuple[tuple[str, ...], str]:
        issue = self.jira_request("GET", f"/issue/{ticket_key}?fields=summary,labels")
        fields = issue.get("fields") or {}
        return tuple(fields.get("labels") or ()), str(fields.get("summary") or "")

    def medical_readiness_ok_for_closure(self, ticket_key: str) -> bool:
        labels, summary = self.medical_readiness_labels_and_summary(ticket_key)
        result = medical_readiness_check.check_medical_readiness(
            labels=labels,
            summary=summary,
        )
        if result.passed:
            return True
        self.log(
            "WARN",
            "medical_readiness_blocked",
            ticket_key=ticket_key,
            err="; ".join(result.reasons),
        )
        reasons = "\n".join(f"- {reason}" for reason in result.reasons)
        command = " ".join(result.negative_leak_command) or "(not run)"
        self.add_jira_comment(
            ticket_key,
            (
                "[medical-readiness-blocked]\n\n"
                "Medical ticket closure refused before Published transition.\n\n"
                f"Reasons:\n{reasons}\n\n"
                f"Negative-leak command: `{command}`"
            ),
        )
        return False

    def _lock_for(self, ticket_key: str) -> Lock:
        with self._locks_guard:
            if ticket_key not in self._ticket_locks:
                self._ticket_locks[ticket_key] = Lock()
            return self._ticket_locks[ticket_key]

    def stream_forever(self) -> None:
        reap_stale_merger_verify_worktrees(logger=self.log)
        # SP-B-X-009 — emit one heartbeat before catchup so the file
        # appears on disk the moment the daemon is up. Without this the
        # gate could read a missing file during the (potentially minute-
        # long) startup catchup and trip a false "bridge down" alert.
        self._touch_heartbeat_file()
        self._last_heartbeat_file = time.monotonic()
        # SP-B-X-019 / OP-1077 — start the heartbeat thread BEFORE
        # catchup. ``startup_catchup`` and the SSH stream read can both
        # block for minutes during quiet Gerrit periods; without the
        # thread, ``_maintenance_ticks``'s heartbeat write only fires
        # when the next stream-event arrives, and the on-disk file
        # goes stale → runner pickup gate trips a false "bridge_down".
        # The thread runs ``_touch_heartbeat_file`` on a half-cadence
        # wall clock so the gate's 5-min stale threshold never trips
        # while the daemon is healthy. ``stop()`` sets the Event so
        # shutdown doesn't wait a full cycle.
        self._start_heartbeat_thread()
        self.startup_catchup()
        self.replay_from_cursor()
        consecutive_failures = 0
        backoff = 1.0
        while not self._stop:
            proc = self.popen_factory(
                self._ssh_cmd("gerrit", "stream-events"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._ssh_env(),
            )
            if proc.stdout is None:
                raise RuntimeError("stream-events stdout pipe unavailable")
            for raw_line in proc.stdout:
                self._maintenance_ticks()
                payload = parse_stream_line(raw_line)
                if payload is None:
                    self.counters.parse_errors += 1
                    self.log("WARN", "malformed_json_line", err=raw_line.strip()[:500])
                    continue
                self.process_stream_event(payload)
                self.save_event_cursor(payload)
                if self._stop:
                    break
            rc = proc.wait()
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            if self._stop:
                break
            if self._is_auth_failure(stderr):
                self.log("ALERT", "gerrit_auth_failed", err=stderr[-500:])
                raise GerritAuthError("Gerrit SSH auth failed")
            consecutive_failures += 1
            self.counters.gerrit_reconnects += 1
            if consecutive_failures >= self.config.alert_after_failures:
                self.log(
                    "ALERT",
                    "gerrit_stream_reconnects_high",
                    err=stderr[-500:],
                    failures=consecutive_failures,
                )
            else:
                self.log("WARN", "gerrit_stream_disconnected", err=stderr[-500:], returncode=rc)
            self.sleep(backoff)
            backoff = min(backoff * 2, self.config.max_backoff_seconds)
            self.replay_from_cursor()

    def run_once_from_lines(self, lines: Iterable[str]) -> None:
        self.startup_catchup()
        self.replay_from_cursor()
        self._touch_heartbeat_file()
        self._last_heartbeat_file = time.monotonic()
        for line in lines:
            payload = parse_stream_line(line)
            if payload is None:
                self.counters.parse_errors += 1
                self.log("WARN", "malformed_json_line", err=line.strip()[:500])
                continue
            self.process_stream_event(payload)
            self.save_event_cursor(payload)
        self._emit_heartbeat()
        self._touch_heartbeat_file()

    def _maintenance_ticks(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat >= self.config.heartbeat_seconds:
            self._emit_heartbeat()
            self._last_heartbeat = now
        if now - self._last_heartbeat_file >= self.config.heartbeat_file_seconds:
            self._touch_heartbeat_file()
            self._last_heartbeat_file = now
        if self.config.periodic_catchup_seconds > 0 and (
            now - self._last_periodic_catchup >= self.config.periodic_catchup_seconds
        ):
            self.startup_catchup()
            self._last_periodic_catchup = now

    def _touch_heartbeat_file(self) -> None:
        """SP-B-X-009 — write the on-disk heartbeat for the pickup gate.

        Errors are logged but never raised: a stale heartbeat is *exactly*
        the failure-mode the pickup gate is designed to detect, so a write
        failure must not also crash the daemon. The runner side will pick
        up the stale mtime and notify the operator on its next pickup."""

        path = self.config.heartbeat_file_path or heartbeat_path_from_env()
        try:
            touch_heartbeat_file(path)
        except OSError as exc:
            self.log(
                "WARN",
                "heartbeat_file_write_failed",
                err=f"{type(exc).__name__}: {exc}",
                heartbeat_path=str(path),
            )

    def _start_heartbeat_thread(self) -> None:
        """SP-B-X-019 / OP-1077 — start the wall-clock heartbeat thread.

        The thread is a ``daemon=True`` background worker that calls
        :meth:`_touch_heartbeat_file` every ``heartbeat_file_seconds / 2``
        (clamped to a 5s floor) regardless of Gerrit event traffic. This
        keeps the on-disk heartbeat fresh during quiet periods when the
        SSH stream-events read blocks for minutes — without the thread,
        the runner-side pickup gate would falsely trip ``bridge_down``.

        ``stop()`` sets ``self._heartbeat_thread_stop`` so the thread
        observes the Event during its sleep and exits within one
        check interval rather than waiting a full cadence.

        Half-cadence (``heartbeat_file_seconds / 2``) keeps the stale
        window safely below the runner gate's ``OMNISIGHT_BRIDGE_STALE_AFTER_SEC``
        threshold even if one tick fires late.

        Idempotent: if the thread is already alive (e.g. ``stream_forever``
        is restarted by an in-process test), this method is a no-op so
        we don't accumulate threads.
        """
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_thread_stop.clear()
        cadence = max(5.0, float(self.config.heartbeat_file_seconds) / 2.0)

        def _run() -> None:
            # First write happens at startup via stream_forever; loop
            # waits a full cadence before the first repeated write.
            while not self._heartbeat_thread_stop.wait(cadence):
                try:
                    self._touch_heartbeat_file()
                except Exception as exc:  # noqa: BLE001 — must never crash the daemon
                    # _touch_heartbeat_file already swallows OSError; this
                    # catches anything else (e.g. mocked-test sentinels).
                    self.log(
                        "WARN",
                        "heartbeat_thread_write_failed",
                        err=f"{type(exc).__name__}: {exc}",
                    )

        thread = Thread(
            target=_run,
            name="bridge-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread = thread
        thread.start()

    def _emit_heartbeat(self) -> None:
        payload = self.counters.__dict__.copy()
        self.log("INFO", "heartbeat", **payload)
        if self.counters.last_event_at_ts is None:
            age = time.monotonic() - self._started_at
            if age > self.config.silent_warn_seconds:
                self.log("WARN", "stream_silent", err="stream silent", silent_seconds=round(age, 3))
            return
        try:
            last = datetime.fromisoformat(self.counters.last_event_at_ts)
        except ValueError:
            return
        age = (datetime.now(timezone.utc) - last).total_seconds()
        if age > self.config.silent_warn_seconds:
            self.log("WARN", "stream_silent", err="stream silent", silent_seconds=round(age, 3))

    def _ssh_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_SSH_COMMAND"] = f"ssh -i {self.config.gerrit_key_path}"
        return env

    def _ssh_cmd(self, *remote_args: str) -> list[str]:
        return [
            "ssh",
            "-i",
            str(self.config.gerrit_key_path),
            "-p",
            str(self.config.gerrit_port),
            f"{self.config.gerrit_user}@{self.config.gerrit_host}",
            *remote_args,
        ]

    def _is_auth_failure(self, blob: str) -> bool:
        return any(marker.lower() in blob.lower() for marker in AUTH_FAILURE_MARKERS)


def build_bridge(agent_class: str = "subscription-claude") -> GerritJiraBridge:
    client = jira_dispatch.make_client(agent_class)
    auth = jira_dispatch._GERRIT_AUTH_BY_CLASS.get(
        agent_class,
        jira_dispatch._GERRIT_AUTH_BY_CLASS["subscription-claude"],
    )
    user, key_path = auth
    return GerritJiraBridge(
        client,
        BridgeConfig(
            agent_class=agent_class,
            gerrit_user=user,
            gerrit_key_path=key_path,
        ),
    )


def archive_sweep_once(agent_class: str = "subscription-claude") -> int:
    """Run the Published → Archived sweep once for systemd timer use."""
    return build_bridge(agent_class).run_archive_sweep()


async def _run_with_db_pool(agent_class: str = "subscription-claude") -> None:
    """Run the bridge with an explicit pool lifecycle.

    The stream-events daemon runs outside FastAPI's lifespan context,
    but proactive merger audit writes still use ``backend.audit.log``.
    Initialise the process-global pool here before any stream event can
    spawn merger work.

    OP-1190: also pre-warm ``GerritClient``'s account-resolution cache.
    Per-event handler threads call ``asyncio.run(...)`` and therefore
    run in a fresh event loop; the asyncpg pool created above is bound
    to THIS (main) loop and cannot be acquired from those worker loops.
    Pre-warming here lets ``_proactive_merger_check`` and
    ``_ai_reviewer_check`` resolve the Gerrit account synchronously from
    the cache without touching the pool from a different loop.
    """
    dsn = _resolve_pg_dsn()
    if not dsn:
        raise BridgeFatalError(
            "Gerrit/JIRA bridge requires a Postgres DSN for audit logging"
        )
    await db_pool.init_pool(dsn)
    from backend.gerrit import GerritClient
    await GerritClient.prewarm_for_daemon()
    bridge = build_bridge(agent_class)
    # OP-1196 phase 3b — one-shot scan of pre-existing conflicts that
    # never received a live stream event. After this returns, live
    # `_handle_patchset_created` events take over normally. Backfill
    # failure does NOT block the main stream loop — it's logged and
    # swallowed via the bridge.backfill_existing_conflicts internal
    # try/except, returning 0 on any error.
    try:
        bridge.backfill_existing_conflicts()
    except Exception as exc:                              # pragma: no cover
        # Defensive — the method already swallows its own errors, but if
        # something genuinely catastrophic escapes (e.g., missing
        # subprocess module) we still want stream_forever to run.
        structured_log(
            "ERROR", "backfill_unexpected_exception",
            err=f"{type(exc).__name__}: {exc}",
        )
    try:
        bridge.stream_forever()
    finally:
        await db_pool.close_pool()


def run(agent_class: str = "subscription-claude") -> int:
    import asyncio

    try:
        asyncio.run(_run_with_db_pool(agent_class))
        return 0
    except BridgeFatalError as exc:
        structured_log("ALERT", "bridge_fatal", err=str(exc))
        return 2


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-class",
        default="subscription-claude",
        help="JIRA/Gerrit bot credential class; default uses claude-bot.",
    )
    parser.add_argument(
        "--archive-sweep-once",
        action="store_true",
        help="Run one Published-to-Archived retention sweep and exit.",
    )
    args = parser.parse_args(argv)
    if args.archive_sweep_once:
        archive_sweep_once(args.agent_class)
        return 0
    return run(args.agent_class)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
