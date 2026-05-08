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
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable
from uuid import uuid4

from backend import db_pool
from backend.agents import jira_dispatch
from backend.db import _resolve_pg_dsn

CURSOR_FILE = Path("/var/lib/omnisight-bridge/event-cursor.json")

APPROVED_STATUS_NAMES = {"Approved", "承認済み"}
ARCHIVED_STATUS_NAMES = {"Archived"}
IN_PROGRESS_STATUS_NAMES = {"In Progress", "進行中"}
PUBLISHED_STATUS_NAMES = {"Published", "公開済み"}
UNDER_REVIEW_STATUS_NAMES = {"Under Review"}
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
    silent_warn_seconds: float = 600.0
    periodic_catchup_seconds: float = 900.0
    max_backoff_seconds: float = 60.0
    alert_after_failures: int = 10
    cursor_file: Path | None = CURSOR_FILE
    # OP-733 — debounce window for auto-rebase sweeps. A batch +2 of N
    # changes can fire N change-merged events within seconds; the
    # debounce coalesces them into a single sweep on the most recent
    # merged SHA.
    auto_rebase_debounce_seconds: float = 30.0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        logger: Callable[..., None] = structured_log,
    ) -> None:
        self.client = client
        self.config = config or BridgeConfig(agent_class=client.agent_class)
        self.sleep = sleep
        self.popen_factory = popen_factory
        self.run_command = run_command
        self.log = logger
        self.counters = BridgeCounters()
        self._ticket_locks: dict[str, Lock] = {}
        self._locks_guard = Lock()
        self._stop = False
        self._started_at = time.monotonic()
        self._last_heartbeat = time.monotonic()
        self._last_periodic_catchup = time.monotonic()
        self._cursor_missing_warned = False
        # OP-733 — lazy-initialised on the first change-merged event so
        # bridge construction stays cheap + tests that never exercise
        # the rebase path don't import the module at all.
        self._auto_rebase_sweeper: Any = None
        self._auto_rebase_scheduler: Any = None

    def stop(self) -> None:
        self._stop = True

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

    def search_approved_tickets(self) -> list[dict[str, Any]]:
        """Backward-compatible alias for tests/scripts from OP-689."""
        return self.search_catchup_candidate_tickets()

    def fetch_issue_status(self, ticket_key: str) -> str:
        issue = self.jira_request("GET", f"/issue/{ticket_key}?fields=status")
        status = ((issue.get("fields") or {}).get("status") or {}).get("name", "")
        return str(status)

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

    def add_jira_comment(self, ticket_key: str, message: str) -> None:
        body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{
                    "type": "paragraph",
                    "content": [{"type": "text", "text": message}],
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
        self.process_ticket_for_change(ticket_key, change.change_id)

    def process_stream_event(self, event: dict[str, Any]) -> None:
        self.counters.events_received += 1
        self.counters.last_event_at_ts = utc_now_iso()
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
        self.process_ticket_for_change(ticket_keys[0], change.change_id)

    # ─── patchset-created → proactive merger trigger (OP-715) ────────

    def _handle_patchset_created(self, event: dict[str, Any]) -> None:
        """Spawn the proactive merger check as a fire-and-forget thread.

        OP-715 — Gerrit's webhooks plugin v3.13.5 has no auth surface,
        so the proactive merger trigger that OP-714 attempted to wire
        through ``/webhooks/gerrit`` runs from this stream-events
        daemon instead. SSH transport is auth'd at the protocol layer
        (``claude-bot`` SSH key), so we don't need any per-event
        signature.

        The shared decision logic lives in
        :func:`backend.routers.webhooks._proactive_merger_check` —
        same skip conditions (uploader=merger-bot, hashtag check,
        WIP, mergeable check), same hashtag throttle
        (``Merger-Proactive-PS<n>``), same arbiter invocation. We
        keep ONE implementation so a future webhook-auth fix and the
        daemon stay in sync without drift.

        The function is async (uses aiohttp for the mergeable REST
        query and asyncio for the merger LLM call), but
        ``process_stream_event`` is synchronous because the daemon's
        outer ``stream_forever`` loop is sync (subprocess Popen +
        line iteration). Bridging the gap with a per-event daemon
        thread that calls ``asyncio.run`` is acceptable here because:

          1. Skip conditions early-exit fast (~10 ms each on the
             cached path), so most events do NOT actually start an
             event loop.
          2. The merger invocation itself is rate-limited by the
             ``Merger-Proactive-PS<n>`` hashtag throttle, so we cap
             at one slow path per (change, patchset).
          3. Daemon threads are auto-reaped on process shutdown, so
             the systemd ``KillSignal=SIGTERM`` + ``TimeoutStopSec``
             still cleans up.

        Errors inside the spawned thread MUST NOT propagate up to
        the stream loop — losing one merger check is recoverable
        (next patchset re-runs the check), but losing the daemon
        means OP-689's change-merged → Published transitions stop
        too.
        """
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

    def process_ticket_for_change(self, ticket_key: str, change_id: str) -> bool:
        lock = self._lock_for(ticket_key)
        with lock:
            status = self.fetch_issue_status(ticket_key)
            original_status = status
            if status in PUBLISHED_STATUS_NAMES:
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

    def _lock_for(self, ticket_key: str) -> Lock:
        with self._locks_guard:
            if ticket_key not in self._ticket_locks:
                self._ticket_locks[ticket_key] = Lock()
            return self._ticket_locks[ticket_key]

    def stream_forever(self) -> None:
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
        for line in lines:
            payload = parse_stream_line(line)
            if payload is None:
                self.counters.parse_errors += 1
                self.log("WARN", "malformed_json_line", err=line.strip()[:500])
                continue
            self.process_stream_event(payload)
            self.save_event_cursor(payload)
        self._emit_heartbeat()

    def _maintenance_ticks(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat >= self.config.heartbeat_seconds:
            self._emit_heartbeat()
            self._last_heartbeat = now
        if self.config.periodic_catchup_seconds > 0 and (
            now - self._last_periodic_catchup >= self.config.periodic_catchup_seconds
        ):
            self.startup_catchup()
            self._last_periodic_catchup = now

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
        BridgeConfig(agent_class=agent_class, gerrit_user=user, gerrit_key_path=key_path),
    )


async def _run_with_db_pool(agent_class: str = "subscription-claude") -> None:
    """Run the bridge with an explicit pool lifecycle.

    The stream-events daemon runs outside FastAPI's lifespan context,
    but proactive merger audit writes still use ``backend.audit.log``.
    Initialise the process-global pool here before any stream event can
    spawn merger work.
    """
    dsn = _resolve_pg_dsn()
    if not dsn:
        raise BridgeFatalError(
            "Gerrit/JIRA bridge requires a Postgres DSN for audit logging"
        )
    await db_pool.init_pool(dsn)
    try:
        build_bridge(agent_class).stream_forever()
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
