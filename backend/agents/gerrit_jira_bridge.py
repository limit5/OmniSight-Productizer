"""Gerrit stream-events → JIRA Approved/Published bridge.

OP-689 closes OP-247 Phase 3: after Gerrit merges a develop change, this
daemon transitions the matching JIRA ticket from Approved to Published.
It deliberately uses only transition id=7 (Deploy) and refuses to advance
any issue that is not already Approved.
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

from backend.agents import jira_dispatch

APPROVED_STATUS_NAMES = {"Approved", "承認済み"}
PUBLISHED_STATUS_NAMES = {"Published", "公開済み"}
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

    def search_approved_tickets(self) -> list[dict[str, Any]]:
        jql = (
            f'project = "{self.client.project_key}" '
            'AND status = "Approved" '
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

    def fetch_issue_status(self, ticket_key: str) -> str:
        issue = self.jira_request("GET", f"/issue/{ticket_key}?fields=status")
        status = ((issue.get("fields") or {}).get("status") or {}).get("name", "")
        return str(status)

    def fetch_issue_comments(self, ticket_key: str) -> list[dict[str, Any]]:
        resp = self.jira_request("GET", f"/issue/{ticket_key}/comment?maxResults=100")
        return list(resp.get("comments", []))

    def transition_to_published(self, ticket_key: str) -> None:
        self.jira_request(
            "POST",
            f"/issue/{ticket_key}/transitions",
            {"transition": {"id": jira_dispatch.TRANSITION_IDS["to_published"]}},
        )

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
        for issue in self.search_approved_tickets():
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
        if event_type != "change-merged":
            return
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

    def process_ticket_for_change(self, ticket_key: str, change_id: str) -> bool:
        lock = self._lock_for(ticket_key)
        with lock:
            status = self.fetch_issue_status(ticket_key)
            if status in PUBLISHED_STATUS_NAMES:
                return False
            if status not in APPROVED_STATUS_NAMES:
                self.log(
                    "WARN",
                    "ticket_unexpected_status_skip",
                    ticket_key=ticket_key,
                    change_id=change_id,
                    err=status,
                )
                return False
            self.transition_to_published(ticket_key)
            self.counters.transitions_made += 1
            self.log("INFO", "ticket_published", ticket_key=ticket_key, change_id=change_id)
            return True

    def _lock_for(self, ticket_key: str) -> Lock:
        with self._locks_guard:
            if ticket_key not in self._ticket_locks:
                self._ticket_locks[ticket_key] = Lock()
            return self._ticket_locks[ticket_key]

    def stream_forever(self) -> None:
        self.startup_catchup()
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

    def run_once_from_lines(self, lines: Iterable[str]) -> None:
        self.startup_catchup()
        for line in lines:
            payload = parse_stream_line(line)
            if payload is None:
                self.counters.parse_errors += 1
                self.log("WARN", "malformed_json_line", err=line.strip()[:500])
                continue
            self.process_stream_event(payload)
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


def run(agent_class: str = "subscription-claude") -> int:
    try:
        build_bridge(agent_class).stream_forever()
        return 0
    except BridgeFatalError as exc:
        structured_log("ALERT", "bridge_fatal", err=str(exc))
        return 2
