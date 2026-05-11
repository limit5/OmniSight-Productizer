"""OP-902 (F4) — parse runner log archives into incident rows.

The runner logs are transcripts: most lines are model output, command
output, or copied ticket text. This parser only treats runner-owned
markers (``[runner] ...``) and explicit bracket markers as incidents so
source snippets and ticket descriptions do not become false history.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from backend.agents.failure_class import FailureClass, classify_from_traceback

log = logging.getLogger(__name__)


class LogParseFailure(ValueError):
    """Raised internally for one malformed log line; callers continue."""


class UnknownFailureClass(ValueError):
    """Raised internally when an explicit class is outside the enum."""


class DuplicateIncidentInsertion(RuntimeError):
    """Raised by strict inserters; default backfill skips duplicates."""


class RunnerLogsCorrupted(RuntimeError):
    """Raised by strict callers when a log file can only be partially read."""


@dataclass(frozen=True)
class ParsedRunnerIncident:
    """One row destined for ``runner_incidents``."""

    incident_id: str
    timestamp: datetime
    ticket_key: str
    failure_class: FailureClass
    summary: str
    mutex_label: str | None
    runner_class: str
    raw_traceback: str
    source_file: str
    source_line: int
    event_type: str


_TICKET_RE = re.compile(r"\bOP-\d+\b")
_FILENAME_TS_RE = re.compile(r"(?P<stamp>20\d{6}-\d{6})")
_RUNNER_LINE_RE = re.compile(r"^\[runner\]\s+(?P<body>.*)$")
_EXPLICIT_MARKER_RE = re.compile(
    r"^\[(?P<marker>pickup|revert|mutex-lost|area-validation-failure|push-rejected)\]\s*(?P<body>.*)$",
    re.I,
)
_AGENT_RE = re.compile(r"\bagent_class=(?P<class>[-\w]+)")
_PICKUP_RE = re.compile(r"\bselected:\s+(?P<ticket>OP-\d+)\s+\(component=(?P<component>[^)]+)\)", re.I)
_REVERT_RE = re.compile(r"\b(?P<ticket>OP-\d+)\s+CLI failed rc=(?P<rc>\d+);\s+reverting ticket", re.I)
_PUSH_FAIL_RE = re.compile(r"\bGerrit push(?: setup)? failed\b", re.I)
_MUTEX_RE = re.compile(r"\b(?:mutex[- ]lost|mutex contention|could not acquire mutex|file coordinator.*lock)\b", re.I)
_AREA_RE = re.compile(r"\b(?:area[- ]validation[- ]failure|unknown area label|scope[- ]to[- ]paths)\b", re.I)
_PATH_RE = re.compile(
    r"\b(?:backend|scripts|alembic|migrations)/[A-Za-z0-9_./-]+\.[A-Za-z0-9_+-]+\b"
)
_CLASS_RE = re.compile(r"\bfailure_class=(?P<class>[A-Z0-9_ -]+)\b")


def incident_id_for(timestamp: datetime, ticket_key: str, failure_class: FailureClass) -> str:
    """Stable idempotency key: sha256(timestamp + ticket + class)."""
    stamp = timestamp.astimezone(timezone.utc).isoformat()
    raw = f"{stamp}{ticket_key}{failure_class.value}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def iter_log_files(log_dir: Path) -> list[Path]:
    """Return ``*.log`` files oldest first using filename timestamp first."""
    return sorted(log_dir.glob("*.log"), key=lambda p: (_file_base_timestamp(p), p.name))


def parse_log_file(path: Path) -> list[ParsedRunnerIncident]:
    """Parse one log file, warning on bad lines and returning partial data."""
    base_ts = _file_base_timestamp(path)
    runner_class = _runner_class_from_name(path.name)
    incidents: list[ParsedRunnerIncident] = []
    current_ticket: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                if "\ufffd" in line:
                    log.warning(
                        "RunnerLogsCorrupted file=%s line=%d replacement_chars=true",
                        path,
                        line_no,
                    )
                try:
                    parsed = parse_log_line(
                        line.rstrip("\n"),
                        source_file=path,
                        source_line=line_no,
                        base_timestamp=base_ts,
                        runner_class=runner_class,
                        current_ticket=current_ticket,
                    )
                except LogParseFailure as exc:
                    log.warning("LogParseFailure file=%s line=%d %s", path, line_no, exc)
                    continue
                if parsed is not None:
                    current_ticket = parsed.ticket_key
                    incidents.append(parsed)
    except OSError as exc:
        raise RunnerLogsCorrupted(f"{path}: {exc}") from exc
    return incidents


def parse_log_files(paths: Iterable[Path]) -> list[ParsedRunnerIncident]:
    """Parse files in supplied order, preserving incident order."""
    out: list[ParsedRunnerIncident] = []
    for path in paths:
        try:
            out.extend(parse_log_file(path))
        except RunnerLogsCorrupted as exc:
            log.warning("RunnerLogsCorrupted %s", exc)
    return out


def parse_log_line(
    line: str,
    *,
    source_file: Path,
    source_line: int,
    base_timestamp: datetime,
    runner_class: str,
    current_ticket: str | None = None,
) -> ParsedRunnerIncident | None:
    """Parse one line into an incident, or ``None`` for non-incidents."""
    marker, body = _extract_marker(line)
    if marker is None:
        return None

    timestamp = base_timestamp + timedelta(seconds=source_line)
    event_type, ticket_key, failure_class, summary = _classify_marker(
        marker, body, current_ticket=current_ticket
    )
    mutex_label = _extract_mutex_label(body)
    if event_type == "pickup" and mutex_label is None:
        mutex_label = _component_mutex_label(body)
    incident_id = incident_id_for(timestamp, ticket_key, failure_class)
    return ParsedRunnerIncident(
        incident_id=incident_id,
        timestamp=timestamp,
        ticket_key=ticket_key,
        failure_class=failure_class,
        summary=summary,
        mutex_label=mutex_label,
        runner_class=runner_class,
        raw_traceback=body,
        source_file=str(source_file),
        source_line=source_line,
        event_type=event_type,
    )


def _extract_marker(line: str) -> tuple[str | None, str]:
    explicit = _EXPLICIT_MARKER_RE.match(line)
    if explicit:
        return explicit.group("marker").lower(), explicit.group("body").strip()
    runner = _RUNNER_LINE_RE.match(line)
    if not runner:
        return None, line
    body = runner.group("body").strip()
    if _PICKUP_RE.search(body):
        return "pickup", body
    if _REVERT_RE.search(body):
        return "revert", body
    if _PUSH_FAIL_RE.search(body):
        return "push-rejected", body
    if _MUTEX_RE.search(body):
        return "mutex-lost", body
    if _AREA_RE.search(body):
        return "area-validation-failure", body
    return None, body


def _classify_marker(
    marker: str, body: str, *, current_ticket: str | None
) -> tuple[str, str, FailureClass, str]:
    ticket_key = _extract_ticket(body, current_ticket=current_ticket)
    marker = marker.lower()
    if marker == "pickup":
        match = _PICKUP_RE.search(body)
        if match:
            ticket_key = match.group("ticket")
        return marker, ticket_key, FailureClass.OTHER, _summary("pickup", body)
    if marker == "revert":
        match = _REVERT_RE.search(body)
        if match:
            ticket_key = match.group("ticket")
        klass = FailureClass.RUNNER_TIMEOUT if "rc=143" in body else classify_from_traceback(body)
        if klass is FailureClass.OTHER:
            klass = FailureClass.LLM_LOOP_DETECTED
        return marker, ticket_key, klass, _summary("revert", body)
    if marker == "mutex-lost":
        return marker, ticket_key, FailureClass.MUTEX_CONTENTION, _summary("mutex lost", body)
    if marker == "area-validation-failure":
        return marker, ticket_key, FailureClass.UNKNOWN_AREA_LABEL, _summary("area validation failed", body)
    if marker == "push-rejected":
        return marker, ticket_key, _classify_push_rejection(body), _summary("push rejected", body)
    raise LogParseFailure(f"unknown marker {marker!r}")


def _classify_push_rejection(body: str) -> FailureClass:
    explicit = _CLASS_RE.search(body)
    if explicit:
        try:
            return FailureClass(explicit.group("class").strip().upper().replace("-", "_"))
        except ValueError:
            log.warning(
                "UnknownFailureClass raw=%r; coerced to OTHER; follow-up needed to extend enum",
                explicit.group("class"),
            )
            return FailureClass.OTHER
    klass = classify_from_traceback(body)
    if klass is not FailureClass.OTHER:
        return klass
    if re.search(r"\brebase\b|conflict|cannot merge", body, re.I):
        return FailureClass.MERGE_CONFLICT
    if re.search(r"\bdirty\b|untracked|uncommitted|local changes", body, re.I):
        return FailureClass.WORKTREE_DIRTY
    return FailureClass.BRIDGE_DESYNC


def _extract_ticket(body: str, *, current_ticket: str | None = None) -> str:
    match = _TICKET_RE.search(body)
    if match:
        return match.group(0)
    if current_ticket:
        return current_ticket
    raise LogParseFailure("missing JIRA ticket key")


def _extract_mutex_label(body: str) -> str | None:
    path = _PATH_RE.search(body)
    return path.group(0) if path else None


def _component_mutex_label(body: str) -> str | None:
    match = _PICKUP_RE.search(body)
    if not match:
        return None
    return f"component:{match.group('component').strip().lower()}"


def _summary(prefix: str, body: str) -> str:
    body = " ".join(body.split())
    text = f"{prefix}: {body}"
    return text[:240]


def _file_base_timestamp(path: Path) -> datetime:
    match = _FILENAME_TS_RE.search(path.name)
    if match:
        return datetime.strptime(match.group("stamp"), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _runner_class_from_name(name: str) -> str:
    if name.startswith("claude"):
        return "subscription-claude"
    if name.startswith("codex"):
        return "subscription-codex"
    match = _AGENT_RE.search(name)
    return match.group("class") if match else "unknown"


__all__ = [
    "DuplicateIncidentInsertion",
    "LogParseFailure",
    "ParsedRunnerIncident",
    "RunnerLogsCorrupted",
    "UnknownFailureClass",
    "incident_id_for",
    "iter_log_files",
    "parse_log_file",
    "parse_log_files",
    "parse_log_line",
]
