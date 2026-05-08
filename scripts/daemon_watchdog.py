"""OP-723 — Daemon liveness watchdog (META OP-721 T2).

Scans a list of daemons declared in ``configs/watchdog.yaml`` and
fires alerts when:

* the daemon's systemd unit is no longer enabled (P0,
  ``unit_not_loaded`` — the OP-689 case study, where the bridge daemon
  was never installed for two days and we only noticed by accident).
* the daemon's most-recent heartbeat structured-log line is older
  than the configured silence window (DEGRADED, ``daemon_silent``).

Healthy daemons emit a one-line INFO ``all_green`` record to stdout so
an operator tailing the journal can confirm the watchdog itself is
alive (AC #3 — "no false positive").

Wiring:

* ``deploy/systemd/gerrit-jira-bridge-watchdog.service`` runs this
  script with ``--config configs/watchdog.yaml``.
* ``deploy/systemd/gerrit-jira-bridge-watchdog.timer`` triggers the
  service every 5 minutes.

Alert fan-out:

1. Every alert is appended to ``alerts_path`` as one JSON record per
   line. T1 (OP-722) tails this file as the canonical sink.
2. If ``notifier_module`` (default ``backend.agents.operator_notifier``)
   is importable, ``notify(severity, code=..., **fields)`` is called for
   defence-in-depth; missing modules are silently tolerated so the
   watchdog stays useful before T1 is wired in production.
3. Every alert is also mirrored to stderr — systemd routes that to
   journald, where the T3 journal monitor catches it as the AC #4
   ERROR record path.

The script is stdlib + PyYAML only by design — same self-defence
rationale as ``check_alembic_downgrade.py``: the liveness checker has
to remain runnable when something heavier upstream has broken.
"""
from __future__ import annotations

import argparse
import importlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "watchdog.yaml"

SEVERITY_DEGRADED = "DEGRADED"
SEVERITY_P0 = "P0"
SEVERITY_INFO = "INFO"

CODE_UNIT_NOT_LOADED = "unit_not_loaded"
CODE_DAEMON_SILENT = "daemon_silent"
CODE_LOG_MISSING = "log_missing"
CODE_ALL_GREEN = "all_green"


@dataclass
class DaemonSpec:
    unit: str
    scope: str = "user"
    log_path: Path | None = None
    heartbeat_event: str = "heartbeat"
    max_silence_minutes: int = 10


@dataclass
class WatchdogConfig:
    daemons: list[DaemonSpec] = field(default_factory=list)
    log_tail_bytes: int = 262_144
    alerts_path: Path | None = None
    notifier_module: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _structured_log(level: str, event: str, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "timestamp": _utc_now().isoformat(),
        "level": level,
        "event": event,
    }
    record.update(fields)
    print(json.dumps(record, sort_keys=True), flush=True)
    return record


def load_config(path: Path) -> WatchdogConfig:
    import yaml  # local import so --help works without PyYAML

    raw = yaml.safe_load(path.read_text()) or {}
    daemons: list[DaemonSpec] = []
    for entry in raw.get("daemons") or []:
        daemons.append(
            DaemonSpec(
                unit=str(entry["unit"]),
                scope=str(entry.get("scope", "user")),
                log_path=Path(entry["log_path"]) if entry.get("log_path") else None,
                heartbeat_event=str(entry.get("heartbeat_event", "heartbeat")),
                max_silence_minutes=int(entry.get("max_silence_minutes", 10)),
            )
        )
    return WatchdogConfig(
        daemons=daemons,
        log_tail_bytes=int(raw.get("log_tail_bytes", 262_144)),
        alerts_path=Path(raw["alerts_path"]) if raw.get("alerts_path") else None,
        notifier_module=raw.get("notifier_module"),
    )


def _systemctl_argv(scope: str) -> list[str]:
    if scope not in ("user", "system"):
        raise ValueError(f"unknown systemctl scope: {scope!r}")
    binary = shutil.which("systemctl") or "/usr/bin/systemctl"
    return [binary, "--user"] if scope == "user" else [binary]


def check_unit_loaded(
    unit: str,
    scope: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> bool:
    """Return True iff ``systemctl [--user] is-enabled <unit>`` exits 0.

    ``is-enabled`` returns 0 for ``enabled``, ``static`` and ``alias``
    states, which are exactly the states under which a healthy daemon
    *can* run. ``disabled`` / ``masked`` / missing-unit all return
    non-zero, which is the AC #2 P0 condition.
    """
    cmd = _systemctl_argv(scope) + ["is-enabled", unit]
    invoker = runner or subprocess.run
    try:
        result = invoker(cmd, capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        return False
    return result.returncode == 0


def _tail_bytes(log_path: Path, max_bytes: int) -> str:
    size = log_path.stat().st_size
    with log_path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()  # drop the partial first line
        data = fh.read()
    return data.decode("utf-8", errors="replace")


def find_latest_heartbeat(
    log_path: Path,
    *,
    event_name: str,
    max_bytes: int,
) -> datetime | None:
    """Return the timestamp of the most-recent heartbeat, or None."""
    if not log_path.exists():
        return None
    text = _tail_bytes(log_path, max_bytes)
    latest: datetime | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("event") != event_name:
            continue
        ts = payload.get("timestamp")
        if not isinstance(ts, str):
            continue
        try:
            parsed = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def _load_notifier(module_name: str | None) -> Any | None:
    if not module_name:
        return None
    try:
        return importlib.import_module(module_name)
    except Exception:
        return None


class AlertSink:
    """Writes structured alert records to file + best-effort to T1."""

    def __init__(self, alerts_path: Path | None, notifier: Any | None) -> None:
        self.alerts_path = alerts_path
        self.notifier = notifier

    def emit(self, severity: str, code: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": _utc_now().isoformat(),
            "severity": severity,
            "code": code,
        }
        record.update(fields)
        if self.alerts_path is not None:
            self.alerts_path.parent.mkdir(parents=True, exist_ok=True)
            with self.alerts_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        notify = getattr(self.notifier, "notify", None) if self.notifier else None
        if callable(notify):
            try:
                notify(severity, code=code, **fields)
            except Exception as exc:
                # Notifier bug must never crash the watchdog itself —
                # log + keep going so the file sink still wins.
                _structured_log(
                    "ERROR", "notifier_dispatch_failed", code=code, err=str(exc)
                )
        # Mirror to stderr so journald (and therefore T3) sees it.
        print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)
        return record


def evaluate_daemon(
    spec: DaemonSpec,
    sink: AlertSink,
    *,
    log_tail_bytes: int,
    now: datetime | None = None,
    is_enabled: Callable[[str, str], bool] | None = None,
) -> dict[str, Any]:
    """Apply the AC-defined liveness rules to one daemon spec.

    Returns the record that was emitted (alert *or* INFO heartbeat) so
    callers can derive an exit code without re-deriving severity.
    """
    now = now or _utc_now()
    is_enabled_fn = is_enabled or check_unit_loaded
    if not is_enabled_fn(spec.unit, spec.scope):
        return sink.emit(
            SEVERITY_P0,
            CODE_UNIT_NOT_LOADED,
            unit=spec.unit,
            scope=spec.scope,
            message=(
                f"systemctl is-enabled {spec.unit} failed — "
                "unit missing, masked, or disabled"
            ),
        )

    if spec.log_path is None or not spec.log_path.exists():
        return sink.emit(
            SEVERITY_DEGRADED,
            CODE_LOG_MISSING,
            unit=spec.unit,
            log_path=str(spec.log_path) if spec.log_path else None,
            message="daemon log file not found — cannot evaluate heartbeat",
        )

    latest = find_latest_heartbeat(
        spec.log_path,
        event_name=spec.heartbeat_event,
        max_bytes=log_tail_bytes,
    )
    if latest is None:
        return sink.emit(
            SEVERITY_DEGRADED,
            CODE_DAEMON_SILENT,
            unit=spec.unit,
            log_path=str(spec.log_path),
            message=(
                f"no '{spec.heartbeat_event}' events found in last "
                f"{log_tail_bytes} bytes of daemon log"
            ),
        )

    age_seconds = (now - latest).total_seconds()
    threshold_seconds = spec.max_silence_minutes * 60
    if age_seconds > threshold_seconds:
        return sink.emit(
            SEVERITY_DEGRADED,
            CODE_DAEMON_SILENT,
            unit=spec.unit,
            log_path=str(spec.log_path),
            silence_minutes=round(age_seconds / 60.0, 2),
            threshold_minutes=spec.max_silence_minutes,
            last_heartbeat=latest.isoformat(),
        )

    return _structured_log(
        SEVERITY_INFO,
        CODE_ALL_GREEN,
        unit=spec.unit,
        silence_seconds=round(age_seconds, 1),
        threshold_minutes=spec.max_silence_minutes,
    )


def run(config_path: Path) -> int:
    """Run one watchdog pass. Returns 0 if everything green, 1 otherwise."""
    cfg = load_config(config_path)
    notifier = _load_notifier(cfg.notifier_module)
    sink = AlertSink(cfg.alerts_path, notifier)
    if not cfg.daemons:
        _structured_log("WARN", "no_daemons_configured", config=str(config_path))
        return 0
    exit_code = 0
    for spec in cfg.daemons:
        try:
            result = evaluate_daemon(spec, sink, log_tail_bytes=cfg.log_tail_bytes)
        except Exception as exc:
            _structured_log(
                "ERROR",
                "evaluate_failed",
                unit=spec.unit,
                err=str(exc),
            )
            exit_code = 1
            continue
        if result.get("severity") in (SEVERITY_P0, SEVERITY_DEGRADED):
            exit_code = 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Daemon liveness watchdog (OP-723 / META OP-721 T2)."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to watchdog YAML config (default: %(default)s).",
    )
    args = parser.parse_args(argv)
    return run(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
