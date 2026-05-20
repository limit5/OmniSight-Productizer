#!/usr/bin/env python3
"""OP-1008 / AUDIT-29f-10 — schema validator for the coordinator decision log.

The coordinator and its sibling agents (gerrit_jira_bridge, learning_loop,
sprint_replan, ...) all append to one append-only JSONL audit trail
(ADR-0021 §9 L4). That log is the *source of truth* for cold-start L6
crash-replay, the learning loop's write-back/outcome-check, and every
post-incident audit. A single malformed line (truncated write, renamed
field, wrong type) silently breaks replay — so this script is the hard
CI gate that rejects a log the replayers could choke on.

It is deliberately **stdlib-only** (no jsonschema/yaml) so it runs in
<1s in any slot and can be invoked straight from the chaos test
(``backend/tests/test_pipeline_coordinator_chaos.py``) as well as a CI
step, with no extra dependency to install.

What it enforces, per JSONL line:

  1. The line is a single, well-formed JSON object.
  2. The common envelope holds: ``ts`` parses as ISO-8601, ``event`` is
     one of the closed :data:`KNOWN_EVENTS` set, ``pid`` (when present)
     is an int.
  3. The event's own required fields are present with the right types
     (e.g. a ``decision_tick`` carries ``decision_id`` + an ``actions``
     array; each action's ``kind`` is in the §6.1 closed set).

And, in ``--strict`` mode, two cross-line integrity invariants:

  4. ``decision_id`` of every ``decision_tick`` is unique (a duplicate id
     means a tick was logged twice — the decision-log half of the
     "zero double-relabels" guarantee the chaos test asserts on state).
  5. No real (non-dry-run) ``relabel`` is applied to the same
     ``(target, label)`` twice (the log-trail half of "zero
     double-relabels"). Vacuously true while the action layer is in
     shadow mode (every action ``dry_run=True``), kept for when §6 goes
     live.

Note (intentionally NOT an error): a ``shutdown_began`` with no matching
``shutdown_complete`` is the *signature of a crash*, not a malformed log
— it is exactly what L6 replay keys on. ``--report-drains`` surfaces
unmatched drains as informational lines without failing the gate.

Exit codes (mirroring scripts/check_catalog_schema.py):
    0 — every line valid (and, in --strict, every cross-line invariant holds)
    1 — one or more validation failures (CI-red)
    2 — script-environment error (paths unreadable, no input found)

Usage:
    python3 scripts/validate-decision-log.py path/to/2026-05-20.jsonl
    python3 scripts/validate-decision-log.py --dir ~/.config/omnisight/coordinator/decision-log
    python3 scripts/validate-decision-log.py --strict --report-drains LOG...
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


# ── The closed action-kind set (ADR-0021 §6.1) ───────────────────────
# Mirrors ``ALLOWED_ACTION_KINDS`` in
# ``backend/agents/pipeline_coordinator_rules.py``. Hardcoded (not
# imported) so the gate stays stdlib-only and import-cheap; the set is
# closed by design (§6.1 "deliberately keeps this list closed for
# safety"), so drift here is a deliberate, reviewable edit.
ALLOWED_ACTION_KINDS: frozenset[str] = frozenset(
    {
        "noop",
        "file_ticket",
        "relabel",
        "transition",
        "mention_operator",
        "mark_for_followup",
        "escalate",
    }
)


# ── Type-check helpers ────────────────────────────────────────────────

def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _is_int(v: Any) -> bool:
    # bool is an int subclass; a pid/phase must be a real int.
    return isinstance(v, int) and not isinstance(v, bool)


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_list(v: Any) -> bool:
    return isinstance(v, list)


def _is_obj(v: Any) -> bool:
    return isinstance(v, dict)


# A field spec is (field_name, predicate, human-type). Used per event for
# its *required* fields beyond the common envelope.
FieldSpec = tuple[str, Callable[[Any], bool], str]


# ── Per-event required-field specs ────────────────────────────────────
# Common envelope (ts:str, event:str, pid:int-if-present) is checked
# separately for every event; these are the event-specific additions.
EVENT_SPECS: dict[str, list[FieldSpec]] = {
    "decision_tick": [
        ("engine_version", _is_str, "string"),
        ("mode", _is_str, "string"),
        ("decision_id", _is_str, "string"),
        ("actions", _is_list, "array"),
        ("reason", _is_str, "string"),
        ("dry_run", _is_bool, "boolean"),
    ],
    "shutdown_began": [
        ("engine_version", _is_str, "string"),
        ("decision_id", _is_str, "string"),
    ],
    "shutdown_complete": [
        ("engine_version", _is_str, "string"),
        ("decision_id", _is_str, "string"),
    ],
    "cold_start_began": [
        ("engine_version", _is_str, "string"),
    ],
    "cold_start_complete": [
        ("engine_version", _is_str, "string"),
    ],
    "startup_phase": [
        ("phase", _is_int, "integer"),
    ],
    "crash_recovery_applied": [
        ("crashed_drain_id", _is_str, "string"),
        ("resolutions", _is_list, "array"),
    ],
    "coordinator_entered_steady_state": [
        ("ts_entered", _is_str, "string"),
    ],
    "coordinator_source_event": [
        ("trigger", _is_str, "string"),
        ("dry_run", _is_bool, "boolean"),
    ],
    # learning_loop (OP-1009) events — share the common envelope; the
    # write-back / outcome-check lines must carry the decision_id they key on.
    "learning_writeback": [
        ("decision_id", _is_str, "string"),
        ("written", _is_bool, "boolean"),
    ],
    "decision_outcome_check": [
        ("decision_id", _is_str, "string"),
    ],
    "learning_daily_ran": [],
    "learning_weekly_ran": [],
    "learning_rule_graduation": [],
}

KNOWN_EVENTS: frozenset[str] = frozenset(EVENT_SPECS)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def validate_action(action: Any, idx: int) -> list[str]:
    """Validate one element of a decision_tick ``actions`` array."""
    errors: list[str] = []
    if not _is_obj(action):
        return [f"actions[{idx}]: expected object, got {type(action).__name__}"]
    kind = action.get("kind")
    if not _is_str(kind):
        errors.append(f"actions[{idx}].kind: missing or not a string")
    elif kind not in ALLOWED_ACTION_KINDS:
        errors.append(
            f"actions[{idx}].kind {kind!r} not in §6.1 closed set "
            f"{sorted(ALLOWED_ACTION_KINDS)}"
        )
    if "target" in action and not _is_str(action["target"]):
        errors.append(f"actions[{idx}].target: must be a string")
    if "params" in action and not _is_obj(action["params"]):
        errors.append(f"actions[{idx}].params: must be an object")
    if "dry_run" in action and not _is_bool(action["dry_run"]):
        errors.append(f"actions[{idx}].dry_run: must be a boolean")
    return errors


def validate_record(record: Any) -> list[str]:
    """Validate one decoded JSONL record. Returns a list of error strings."""
    if not _is_obj(record):
        return [f"line is not a JSON object (got {type(record).__name__})"]

    errors: list[str] = []

    # ── common envelope ──
    if _parse_iso(record.get("ts")) is None:
        errors.append("ts: missing or not an ISO-8601 timestamp")
    if "pid" in record and not _is_int(record["pid"]):
        errors.append("pid: present but not an integer")

    event = record.get("event")
    if not _is_str(event):
        errors.append("event: missing or not a string")
        return errors  # can't dispatch event-specific checks without it
    if event not in KNOWN_EVENTS:
        errors.append(
            f"event {event!r} is not a known decision-log event "
            f"(known: {sorted(KNOWN_EVENTS)})"
        )
        return errors

    # ── event-specific required fields ──
    for name, predicate, human in EVENT_SPECS[event]:
        if name not in record:
            errors.append(f"{event}: missing required field {name!r}")
        elif not predicate(record[name]):
            errors.append(f"{event}.{name}: expected {human}")

    # ── nested action objects ──
    if event == "decision_tick" and _is_list(record.get("actions")):
        for idx, action in enumerate(record["actions"]):
            errors.extend(validate_action(action, idx))

    return errors


class ValidationReport:
    """Accumulates per-line errors and cross-line integrity findings."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.info: list[str] = []
        self.lines_checked = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, source: str, lineno: int, message: str) -> None:
        # GitHub-Actions-friendly annotation, like check_catalog_schema.py.
        self.errors.append(f"::error file={source},line={lineno}::{message}")


def validate_lines(
    lines: list[str],
    source: str,
    report: ValidationReport,
    *,
    strict: bool,
    seen_decision_ids: dict[str, str],
    real_relabels: dict[tuple[str, str], str],
    drain_began: dict[str, str],
    drain_completed: set[str],
) -> None:
    """Validate every non-blank line of one log file into ``report``.

    The cross-line accumulators (decision-ids, relabels, drains) are passed
    in so a multi-file run validates invariants across the *whole* trail,
    not per-file (a day boundary must not hide a duplicate id).
    """
    for offset, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        report.lines_checked += 1
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            report.error(source, offset, f"invalid JSON: {exc}")
            continue

        for message in validate_record(record):
            report.error(source, offset, message)

        # ── cross-line bookkeeping (only meaningful for valid-ish records) ──
        if not isinstance(record, dict):
            continue
        event = record.get("event")
        where = f"{source}:{offset}"

        if event == "decision_tick":
            did = record.get("decision_id")
            if isinstance(did, str) and did:
                if strict and did in seen_decision_ids:
                    report.error(
                        source, offset,
                        f"duplicate decision_id {did!r} (first seen at "
                        f"{seen_decision_ids[did]}) — a tick was logged twice",
                    )
                seen_decision_ids.setdefault(did, where)
            if strict:
                for action in record.get("actions", []) or []:
                    if not isinstance(action, dict):
                        continue
                    if action.get("kind") == "relabel" and not action.get("dry_run", True):
                        label = str(action.get("params", {}).get("label", ""))
                        key = (str(action.get("target", "")), label)
                        if key in real_relabels:
                            report.error(
                                source, offset,
                                f"double-relabel: real relabel {key} already "
                                f"applied at {real_relabels[key]}",
                            )
                        real_relabels.setdefault(key, where)
        elif event == "shutdown_began":
            did = record.get("decision_id")
            if isinstance(did, str):
                drain_began[did] = where
        elif event == "shutdown_complete":
            did = record.get("decision_id")
            if isinstance(did, str):
                drain_completed.add(did)


def discover_files(args_paths: list[str], directory: str | None) -> list[Path]:
    """Resolve the set of JSONL files to validate from CLI args."""
    files: list[Path] = []
    for p in args_paths:
        files.append(Path(p))
    if directory:
        files.extend(sorted(Path(directory).glob("*.jsonl")))
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate coordinator decision-log JSONL against the "
                    "ADR-0021 §9 L4 schema.",
    )
    parser.add_argument("paths", nargs="*", help="JSONL files to validate")
    parser.add_argument(
        "--dir",
        help="validate every *.jsonl in this decision-log directory",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="also enforce cross-line invariants (unique decision_id, "
             "no double real-relabel)",
    )
    parser.add_argument(
        "--report-drains", action="store_true",
        help="print unmatched shutdown_began (crash signatures) as info",
    )
    args = parser.parse_args(argv)

    files = discover_files(args.paths, args.dir)
    if not files:
        print("ERROR: no JSONL files given (pass paths or --dir)",
              file=sys.stderr)
        return 2

    report = ValidationReport()
    seen_decision_ids: dict[str, str] = {}
    real_relabels: dict[tuple[str, str], str] = {}
    drain_began: dict[str, str] = {}
    drain_completed: set[str] = set()

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
            return 2
        validate_lines(
            text.splitlines(), str(path), report,
            strict=args.strict,
            seen_decision_ids=seen_decision_ids,
            real_relabels=real_relabels,
            drain_began=drain_began,
            drain_completed=drain_completed,
        )

    if args.report_drains:
        for did, where in sorted(drain_began.items()):
            if did not in drain_completed:
                print(
                    f"::notice::unmatched shutdown_began {did} at {where} "
                    "(crash signature — recovered by L6 replay, not an error)"
                )

    for line in report.errors:
        print(line, file=sys.stderr)

    if report.ok:
        print(
            f"decision-log OK: {report.lines_checked} line(s) across "
            f"{len(files)} file(s) valid"
            + (" (strict)" if args.strict else "")
        )
        return 0
    print(
        f"decision-log INVALID: {len(report.errors)} error(s) in "
        f"{report.lines_checked} line(s)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
