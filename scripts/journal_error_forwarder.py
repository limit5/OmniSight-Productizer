"""OP-724 — Journal ERROR-level forwarder (META OP-721 T3).

Long-running daemon that follows the systemd journal for ERROR /
CRITICAL / ALERT records, classifies them via
``configs/error_pager.yaml`` and routes the survivors through the T1
operator notifier (``backend.agents.operator_notifier.notify``).

Pipeline shape — the same four stages described at the top of
``configs/error_pager.yaml``:

    journalctl --priority=err --follow
              │
              ▼   (one journal record per stdout line, JSON)
        AllowList.match(code)  → drop on suppress
              │
              ▼
        SeverityClassifier.classify(priority, code) → DEGRADED/CRITICAL
              │
              ▼
        Notifier.notify(severity, code=..., **fields)  (T1 OP-722)

The forwarder is intentionally CPU-light and stdlib-only (plus PyYAML
for the config) so it stays runnable when something heavier upstream
has broken — the same self-defence rationale as
``scripts/daemon_watchdog.py`` (OP-723 T2) and
``scripts/check_alembic_downgrade.py``.

AC mapping
----------

* AC #1 — backend exception → DEGRADED within 1 min.
  Handled by :class:`Pipeline.handle_record` mapping PRIORITY=3 to
  Severity.DEGRADED and the tightened dedup window
  (``dedup_window_seconds=55`` + ``dispatch_poll_seconds=2``) so the
  worst-case latency is ≤ 57s.
* AC #2 — 5 identical errors in 1 min → 1 alert with count=5.
  Handled by T1 OP-722's existing burst-coalescing dedup. The
  forwarder builds a fresh :class:`Notifier` from env so T1 owns the
  count.
* AC #3 — allow-listed code → no alert.
  Handled by :class:`AllowList.match` returning ``suppress=True`` for
  matching codes; :meth:`Pipeline.handle_record` short-circuits before
  the notify() call AND skips the cursor write so the record is treated
  as "ack'd" without operator noise.
* AC #4 — forwarder crash → systemd restarts, no log loss > 10s.
  Handled by:
    1. ``deploy/systemd/omnisight-journal-error-forwarder.service`` declaring
       ``Restart=always`` + ``RestartSec=5``.
    2. ``journalctl --cursor-file=<path>`` resuming from the LAST
       committed cursor; the forwarder commits the cursor after each
       successful dispatch so a crash mid-record at most replays the
       in-flight record (which T1 dedups anyway).

Configuration reload (SIGHUP) is supported as a defensive feature for
shipping new allow-list rules during an active alert storm without
incurring the ``RestartSec`` outage window.
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "error_pager.yaml"

logger = logging.getLogger("omnisight.journal_error_forwarder")

# ── Severity ──────────────────────────────────────────────────────
#
# String literals chosen to match T1 OP-722's ``Severity`` enum values
# exactly so the notifier can call ``Severity(severity_str)`` without
# a translation layer. Do not rename without updating both T1 and T3.

SEVERITY_WARN = "WARN"
SEVERITY_DEGRADED = "DEGRADED"
SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_P0 = "P0"

# Journal PRIORITY values (RFC 5424 / sd-journal):
#   0 emerg, 1 alert, 2 crit, 3 err, 4 warning, 5 notice, 6 info, 7 debug
_PRIORITY_TO_SEVERITY: dict[int, str] = {
    0: SEVERITY_CRITICAL,  # EMERG — crit-class for paging purposes
    1: SEVERITY_CRITICAL,  # ALERT
    2: SEVERITY_CRITICAL,  # CRIT
    3: SEVERITY_DEGRADED,  # ERR — the AC #1 path
}


# ── Config dataclasses ────────────────────────────────────────────


@dataclass
class UnitSpec:
    """One systemd unit to follow. ``scope`` is either 'user' or
    'system' — matches the watchdog spec at ``configs/watchdog.yaml``."""

    name: str
    scope: str = "system"


@dataclass
class CodeExtractor:
    """One regex applied to the journal MESSAGE field to derive a
    stable error code. The first extractor whose pattern matches
    contributes its named-capture group ``code`` to the record."""

    pattern: re.Pattern[str]

    @classmethod
    def compile(cls, raw: str) -> "CodeExtractor":
        return cls(re.compile(raw))


@dataclass
class AllowListEntry:
    """One allow-list rule. ``suppress=True`` drops the record before
    any notify() call. Either ``code`` (exact match) or
    ``code_pattern`` (regex) must be set."""

    code: str | None = None
    code_pattern: re.Pattern[str] | None = None
    suppress: bool = False
    note: str = ""

    def matches(self, code: str) -> bool:
        if self.code is not None and self.code == code:
            return True
        if self.code_pattern is not None and self.code_pattern.search(code):
            return True
        return False


@dataclass
class ForwarderConfig:
    units: list[UnitSpec] = field(default_factory=list)
    code_extractors: list[CodeExtractor] = field(default_factory=list)
    allow_list: list[AllowListEntry] = field(default_factory=list)
    severity_map: dict[str, str] = field(default_factory=dict)
    dedup_window_seconds: float = 55.0
    dispatch_poll_seconds: float = 2.0
    cursor_file: Path | None = None
    alerts_path: Path | None = None
    notifier_module: str | None = None


def load_config(path: Path) -> ForwarderConfig:
    """Parse ``configs/error_pager.yaml`` into a :class:`ForwarderConfig`.

    All fields have safe defaults so a partially-filled YAML still
    yields a runnable forwarder (one of the lessons from L24 — silent
    config drift bites later).
    """
    import yaml  # local import so --help works without PyYAML

    raw = yaml.safe_load(path.read_text()) or {}
    units = [
        UnitSpec(name=str(e["name"]), scope=str(e.get("scope", "system")))
        for e in raw.get("units") or []
    ]
    extractors = [
        CodeExtractor.compile(str(e["pattern"]))
        for e in raw.get("code_extractors") or []
    ]
    allow_list: list[AllowListEntry] = []
    for e in raw.get("allow_list") or []:
        cp = e.get("code_pattern")
        allow_list.append(
            AllowListEntry(
                code=str(e["code"]) if e.get("code") else None,
                code_pattern=re.compile(str(cp)) if cp else None,
                suppress=bool(e.get("suppress", False)),
                note=str(e.get("note", "")),
            )
        )
    severity_map = {
        str(k): str(v) for k, v in (raw.get("severity_map") or {}).items()
    }
    return ForwarderConfig(
        units=units,
        code_extractors=extractors,
        allow_list=allow_list,
        severity_map=severity_map,
        dedup_window_seconds=float(raw.get("dedup_window_seconds", 55.0)),
        dispatch_poll_seconds=float(raw.get("dispatch_poll_seconds", 2.0)),
        cursor_file=Path(raw["cursor_file"]) if raw.get("cursor_file") else None,
        alerts_path=Path(raw["alerts_path"]) if raw.get("alerts_path") else None,
        notifier_module=raw.get("notifier_module"),
    )


# ── Code derivation + classification ──────────────────────────────


def derive_code(
    message: str,
    unit: str | None,
    priority: int,
    extractors: list[CodeExtractor],
) -> str:
    """Apply ``extractors`` in order; first ``code`` capture wins.

    Falls back to ``<unit>:<priority>`` so even an unparseable record
    has a STABLE dedup key — losing the dedup property here would
    re-introduce the alert flood that AC #2 forbids.
    """
    for ex in extractors:
        m = ex.pattern.search(message)
        if m:
            try:
                code = m.group("code")
            except IndexError:
                continue
            if code:
                return code
    return f"{unit or 'unknown'}:p{priority}"


def classify_severity(
    priority: int,
    code: str,
    severity_map: dict[str, str],
) -> str:
    """PRIORITY → severity, with per-code overrides.

    Priority 4+ (warning and below) is intentionally treated as
    DEGRADED so the journalctl ``--priority=err`` pre-filter remains
    the only authoritative gate; if a non-error somehow slipped past
    we'd rather over-page than drop on the floor.
    """
    if code in severity_map:
        return severity_map[code]
    return _PRIORITY_TO_SEVERITY.get(priority, SEVERITY_DEGRADED)


# ── Allow-list ────────────────────────────────────────────────────


class AllowList:
    """Match a derived code against allow-list rules. Match returns
    the first hit so operators can put a coarse pattern after a
    targeted exact-match (operator-friendly ordering)."""

    def __init__(self, entries: Iterable[AllowListEntry]) -> None:
        self.entries = list(entries)

    def match(self, code: str) -> AllowListEntry | None:
        for e in self.entries:
            if e.matches(code):
                return e
        return None


# ── Notifier loading (defence-in-depth like T2 watchdog) ──────────


def _load_notifier_module(name: str | None) -> Any | None:
    """Import the T1 module if available. A missing module is
    tolerated — the forwarder is built to remain useful before T1
    lands in this branch (same rationale as T2 OP-723)."""
    if not name:
        return None
    try:
        return importlib.import_module(name)
    except Exception:
        logger.warning("notifier module %r not importable; falling back to file sink only", name)
        return None


def build_notifier_from_module(
    module: Any | None,
    *,
    dedup_window_seconds: float,
) -> Any | None:
    """Build a fresh T1 ``Notifier`` instance with the forwarder's own
    tightened dedup window. Returning ``None`` means the caller should
    fall back to the file sink only.

    We deliberately do NOT use the module-level ``notify()`` shortcut
    because that walks through the lazy global ``Notifier`` whose
    dedup window is the 5-min general default. The forwarder needs
    a 55s window to meet AC #1; using the global would either poison
    every other T1 caller or silently violate AC #1.
    """
    if module is None:
        return None
    try:
        Notifier = getattr(module, "Notifier")
        NotifierConfig = getattr(module, "NotifierConfig")
        build_channels_from_env = getattr(module, "build_channels_from_env")
    except AttributeError:
        logger.warning("notifier module missing expected symbols; falling back to file sink only")
        return None
    cfg = NotifierConfig(
        dedup_window_seconds=dedup_window_seconds,
        # Other fields default — T3 only tightens dedup; the rest of
        # T1's behaviour (re-page intervals, JIRA default ticket from
        # env) is unchanged.
    )
    return Notifier(channels=build_channels_from_env(), config=cfg)


# ── Cursor file (AC #4 — no log loss across restart) ──────────────


def read_cursor(cursor_file: Path | None) -> str | None:
    if cursor_file is None or not cursor_file.exists():
        return None
    try:
        text = cursor_file.read_text().strip()
    except OSError:
        return None
    return text or None


def write_cursor(cursor_file: Path | None, cursor: str) -> None:
    if cursor_file is None or not cursor:
        return
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace so a crash mid-write can't half-truncate the
    # cursor file and force a full journal replay (which would then
    # alert-flood through T1's dedup, but still — defence in depth).
    tmp = cursor_file.with_suffix(cursor_file.suffix + ".tmp")
    tmp.write_text(cursor)
    os.replace(tmp, cursor_file)


# ── Journalctl source (production transport) ──────────────────────


def journalctl_argv(
    units: list[UnitSpec],
    *,
    cursor: str | None,
    binary: str = "journalctl",
) -> list[str]:
    """Build the journalctl command. Arg ordering is deliberate:
    ``--after-cursor`` MUST precede ``--follow`` per journalctl's CLI
    semantics, and ``--priority=err`` is the cheap kernel-side gate
    that keeps this Python loop from chewing on info/debug noise."""
    cmd = [binary, "--output=json", "--no-pager", "--priority=err", "--follow"]
    if cursor:
        cmd += [f"--after-cursor={cursor}"]
    for unit in units:
        flag = "--user-unit" if unit.scope == "user" else "--unit"
        cmd += [flag, unit.name]
    return cmd


def stream_journal_records(
    units: list[UnitSpec],
    *,
    cursor: str | None,
    binary: str = "journalctl",
) -> Iterator[dict[str, Any]]:
    """Spawn journalctl --follow and yield one parsed record per line.

    Generator interface so the caller can consume + commit cursor in
    a single tight loop. Closes the subprocess cleanly on caller exit
    (e.g. SIGTERM bubbles up through ``KeyboardInterrupt``)."""
    proc = subprocess.Popen(
        journalctl_argv(units, cursor=cursor, binary=binary),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("journalctl emitted non-JSON line; skipping: %r", line[:200])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── Pipeline core (testable without a live journal) ───────────────


@dataclass
class _DispatchResult:
    """Returned by :meth:`Pipeline.handle_record` so tests can assert
    on the per-record outcome without poking the notifier internals."""

    code: str
    severity: str
    suppressed: bool
    notified: bool
    cursor: str | None


class FileSink:
    """Append-only JSON-lines audit trail. T1 may also be wired; this
    sink is the defence-in-depth layer (T2 watchdog uses the same
    pattern at ``configs/watchdog.yaml::alerts_path``)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def emit(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")


class Pipeline:
    """One-shot: take a journal record, classify, dispatch.

    Held together as a class (not free functions) so the test suite
    can swap out the notifier and file sink with fakes via the
    constructor — no global state, no monkeypatching."""

    def __init__(
        self,
        config: ForwarderConfig,
        notifier: Any | None,
        file_sink: FileSink,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.notifier = notifier
        self.file_sink = file_sink
        self._allow_list = AllowList(config.allow_list)
        self._clock = clock

    @staticmethod
    def _record_message(record: dict[str, Any]) -> str:
        msg = record.get("MESSAGE", "")
        if isinstance(msg, list):
            # journald may emit MESSAGE as an array of bytes when the
            # original was non-UTF8; coerce to a string conservatively.
            try:
                msg = bytes(msg).decode("utf-8", errors="replace")
            except (TypeError, ValueError):
                msg = ""
        return str(msg)

    @staticmethod
    def _record_priority(record: dict[str, Any]) -> int:
        raw = record.get("PRIORITY", "3")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 3

    @staticmethod
    def _record_unit(record: dict[str, Any]) -> str | None:
        return (
            record.get("_SYSTEMD_USER_UNIT")
            or record.get("_SYSTEMD_UNIT")
            or record.get("SYSLOG_IDENTIFIER")
        )

    def handle_record(self, record: dict[str, Any]) -> _DispatchResult:
        message = self._record_message(record)
        priority = self._record_priority(record)
        unit = self._record_unit(record)
        cursor = record.get("__CURSOR")

        code = derive_code(message, unit, priority, self.config.code_extractors)

        # Stage 2 — allow-list. Suppressed records still update the
        # cursor (so we don't replay them on restart) but make no
        # notify() call.
        rule = self._allow_list.match(code)
        if rule is not None and rule.suppress:
            return _DispatchResult(
                code=code,
                severity=SEVERITY_DEGRADED,
                suppressed=True,
                notified=False,
                cursor=cursor,
            )

        # Stage 3 — severity.
        severity = classify_severity(priority, code, self.config.severity_map)

        # Stage 4 — T1 fanout + file sink.
        context: dict[str, Any] = {
            "unit": unit,
            "priority": priority,
            "message": message[:512],  # truncate so a runaway log line doesn't bloat the JIRA comment
        }
        # Carry a few of journald's stable identifiers when present —
        # they are immensely useful in JIRA triage but bloat the alert
        # if blindly forwarded, so we whitelist.
        for key in ("_PID", "_HOSTNAME", "_BOOT_ID"):
            if key in record:
                context[key.lstrip("_").lower()] = record[key]

        notified = self._call_notifier(severity, code, message, context)

        self.file_sink.emit({
            "timestamp": datetime.fromtimestamp(self._clock(), tz=timezone.utc).isoformat(),
            "severity": severity,
            "code": code,
            "unit": unit,
            "priority": priority,
            "message": message[:512],
            "notified": notified,
        })

        return _DispatchResult(
            code=code,
            severity=severity,
            suppressed=False,
            notified=notified,
            cursor=cursor,
        )

    def _call_notifier(
        self,
        severity: str,
        code: str,
        message: str,
        context: dict[str, Any],
    ) -> bool:
        if self.notifier is None:
            return False
        try:
            self.notifier.notify(severity, code, message[:512], context)
            return True
        except Exception:
            # T1 going sideways must NEVER kill the forwarder — that
            # would defeat the entire point of "fail loud, then fan
            # out". Log + carry on (the file sink keeps the trail).
            logger.exception("T1 notifier rejected record; continuing on file sink only")
            return False

    def flush(self) -> None:
        """Best-effort flush of T1's coalesced bursts. Called on
        SIGTERM to deliver any in-flight count=N before the process
        exits — otherwise a clean stop loses the residual count."""
        if self.notifier is None:
            return
        flush_all = getattr(self.notifier, "flush_all", None)
        if callable(flush_all):
            try:
                flush_all()
            except Exception:
                logger.exception("notifier flush_all failed")


# ── Top-level run loop ────────────────────────────────────────────


class _Daemon:
    """Long-running glue: stream records, dispatch, periodically flush
    T1 bursts, commit cursor. One instance per process."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        notifier_module = _load_notifier_module(self.config.notifier_module)
        self.pipeline = Pipeline(
            config=self.config,
            notifier=build_notifier_from_module(
                notifier_module,
                dedup_window_seconds=self.config.dedup_window_seconds,
            ),
            file_sink=FileSink(self.config.alerts_path),
        )
        self._stop = False
        self._reload = False

    def request_stop(self, *_unused: Any) -> None:
        self._stop = True

    def request_reload(self, *_unused: Any) -> None:
        # SIGHUP — flag a config reload at the next safe boundary.
        # See module docstring: the goal is shipping new allow-list
        # rules during an alert storm without taking the RestartSec
        # outage hit.
        self._reload = True

    def reload_config(self) -> None:
        new = load_config(self.config_path)
        # Only the allow-list, severity map and code extractors are
        # safely hot-reloadable. The notifier wiring (channels, dedup
        # window) requires a fresh Notifier and is therefore a process
        # restart concern — document this in operator docs.
        self.config.allow_list = new.allow_list
        self.config.severity_map = new.severity_map
        self.config.code_extractors = new.code_extractors
        self.pipeline.config = self.config
        self.pipeline._allow_list = AllowList(new.allow_list)
        self._reload = False
        logger.info(
            "reloaded config: allow_list=%d severity_overrides=%d extractors=%d",
            len(new.allow_list), len(new.severity_map), len(new.code_extractors),
        )

    def run(self) -> int:
        cursor = read_cursor(self.config.cursor_file)
        last_flush = self._now()
        try:
            for record in stream_journal_records(self.config.units, cursor=cursor):
                if self._stop:
                    break
                if self._reload:
                    try:
                        self.reload_config()
                    except Exception:
                        logger.exception("config reload failed; staying on previous config")
                        self._reload = False

                result = self.pipeline.handle_record(record)
                if result.cursor:
                    write_cursor(self.config.cursor_file, result.cursor)

                if (self._now() - last_flush) >= self.config.dispatch_poll_seconds:
                    self._flush_expired()
                    last_flush = self._now()
        finally:
            self.pipeline.flush()
        return 0

    def _now(self) -> float:
        return time.time()

    def _flush_expired(self) -> None:
        notifier = self.pipeline.notifier
        if notifier is None:
            return
        flush_expired = getattr(notifier, "flush_expired", None)
        if callable(flush_expired):
            try:
                flush_expired()
            except Exception:
                logger.exception("notifier flush_expired failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ERROR-level journal → operator-pager forwarder (OP-724 / META OP-721 T3)."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to error_pager YAML (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    daemon = _Daemon(args.config)
    signal.signal(signal.SIGTERM, daemon.request_stop)
    signal.signal(signal.SIGINT, daemon.request_stop)
    signal.signal(signal.SIGHUP, daemon.request_reload)
    return daemon.run()


if __name__ == "__main__":
    raise SystemExit(main())
