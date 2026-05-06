#!/usr/bin/env python3
"""MP.W0.2 - report TODO.md items missing ``[class:X]`` labels.

MP.W0.1 defines the canonical ``agent_class`` values in
``config/agent_class_schema.yaml``. This helper scans every open or
completed TODO checkbox item (``- [ ]`` / ``- [x]``, including optional
agent markers like ``[G]``) and reports rows that still need an inline
``[class:<agent_class>]`` label.

The script is intentionally read-only: Tier B Codex worktrees must not
edit TODO.md, and operators should review any inferred labels before
applying them.

Usage:
    python3 scripts/reslice_todo_agent_class.py
    python3 scripts/reslice_todo_agent_class.py --check
    python3 scripts/reslice_todo_agent_class.py --format json

Exit codes:
0   no missing or invalid labels
1   at least one item needs operator action
2   schema / input parse error
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TODO_PATH = REPO_ROOT / "TODO.md"
SCHEMA_PATH = REPO_ROOT / "config" / "agent_class_schema.yaml"
ASSIGNMENT_PATH = REPO_ROOT / "docs" / "adr" / "0008-agent-rpg-class-skill-leveling.md"

CHECKBOX_RE = re.compile(r"^\s*-\s+\[(?P<state>[ xX])\](?P<agent>\[[A-Z]\])?\s+(?P<body>.*)$")
CLASS_LABEL_RE = re.compile(r"\[class:(?P<value>[A-Za-z0-9_.-]+)\]")
TASK_ID_RE = re.compile(r"\b(?P<task_id>[A-Z]{1,4}\d*(?:\.[A-Z0-9]+)+)\b")
WAVE_ID_RE = re.compile(r"\bW(?P<start>\d+)(?:\s*-\s*W?(?P<end>\d+))?\b")
ASSIGNMENT_ROW_RE = re.compile(
    r"^\|\s*(?P<wave>[^|]+?)\s*\|\s*(?P<agent_class>`?[A-Za-z0-9_.-]+`?)\s*\|\s*(?P<why>[^|]+?)\s*\|$"
)


@dataclass(frozen=True)
class AgentClassSchema:
    """Canonical TODO ``[class:X]`` schema loaded from MP.W0.1 YAML."""

    inline_label_prefix: str
    unknown_value: str
    allowed_values: tuple[str, ...]


@dataclass(frozen=True)
class AssignmentRule:
    """One wave-to-agent-class row parsed from ADR 0008."""

    wave: str
    agent_class: str
    why: str
    wave_ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class TodoItem:
    """One TODO.md checkbox item relevant to MP.W0 label slicing."""

    line: int
    state: str
    agent_marker: str | None
    body: str
    task_id: str | None
    class_label: str | None


@dataclass(frozen=True)
class Finding:
    """One operator action needed for a TODO row."""

    line: int
    kind: str
    state: str
    task_id: str | None
    current_class: str | None
    suggested_class: str
    suggestion_source: str
    item: str


def load_schema(path: Path = SCHEMA_PATH) -> AgentClassSchema:
    """Load the MP.W0.1 agent_class schema."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read schema {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"schema {path} did not parse as a mapping")
    allowed = data.get("allowed_values")
    if not isinstance(allowed, list) or not all(isinstance(v, str) for v in allowed):
        raise ValueError("schema allowed_values must be a list of strings")
    prefix = data.get("inline_label_prefix")
    unknown = data.get("unknown_value")
    if not isinstance(prefix, str) or not prefix:
        raise ValueError("schema inline_label_prefix must be a non-empty string")
    if not isinstance(unknown, str) or unknown not in allowed:
        raise ValueError("schema unknown_value must be present in allowed_values")
    return AgentClassSchema(
        inline_label_prefix=prefix,
        unknown_value=unknown,
        allowed_values=tuple(allowed),
    )


def parse_assignment_rules(text: str, schema: AgentClassSchema) -> tuple[AssignmentRule, ...]:
    """Parse ADR 0008's Capability assignment table into routing hints."""
    rules: list[AssignmentRule] = []
    allowed = set(schema.allowed_values)
    for raw_line in text.splitlines():
        m = ASSIGNMENT_ROW_RE.match(raw_line)
        if not m:
            continue
        wave = m.group("wave").strip()
        agent_class = m.group("agent_class").strip().strip("`")
        if wave == "---" or agent_class == "agent_class":
            continue
        if agent_class not in allowed:
            continue
        ranges = tuple(
            (int(w.group("start")), int(w.group("end") or w.group("start")))
            for w in WAVE_ID_RE.finditer(wave)
        )
        if not ranges:
            continue
        rules.append(
            AssignmentRule(
                wave=wave,
                agent_class=agent_class,
                why=m.group("why").strip(),
                wave_ranges=ranges,
            )
        )
    return tuple(rules)


def load_assignment_rules(
    schema: AgentClassSchema,
    path: Path = ASSIGNMENT_PATH,
) -> tuple[AssignmentRule, ...]:
    """Load semi-automatic assignment hints from ADR 0008."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    return parse_assignment_rules(text, schema)


def iter_todo_items(text: str) -> Iterable[TodoItem]:
    """Yield TODO checkbox items whose first checkbox is open or done."""
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        m = CHECKBOX_RE.match(raw_line)
        if not m:
            continue
        body = m.group("body").rstrip()
        class_match = CLASS_LABEL_RE.search(body)
        task_match = TASK_ID_RE.search(body)
        state = m.group("state").lower()
        yield TodoItem(
            line=line_no,
            state="x" if state == "x" else " ",
            agent_marker=m.group("agent"),
            body=body,
            task_id=task_match.group("task_id") if task_match else None,
            class_label=class_match.group("value") if class_match else None,
        )


def infer_agent_class(
    item: TodoItem,
    rules: tuple[AssignmentRule, ...],
    schema: AgentClassSchema,
) -> tuple[str, str]:
    """Return ``(suggested_class, source)`` for a TODO item."""
    if not item.task_id or not item.task_id.startswith("W"):
        return (schema.unknown_value, "fallback:unassigned")
    m = re.match(r"W(?P<num>\d+)(?:\.|$)", item.task_id)
    if not m:
        return (schema.unknown_value, "fallback:unassigned")
    wave_num = int(m.group("num"))
    for rule in rules:
        for start, end in rule.wave_ranges:
            if start <= wave_num <= end:
                return (rule.agent_class, f"ADR0008:{rule.wave}")
    return (schema.unknown_value, "fallback:unassigned")


def find_label_gaps(
    items: Iterable[TodoItem],
    schema: AgentClassSchema,
    rules: tuple[AssignmentRule, ...],
) -> list[Finding]:
    """Return missing or invalid ``[class:X]`` labels."""
    allowed = set(schema.allowed_values)
    findings: list[Finding] = []
    for item in items:
        suggested, source = infer_agent_class(item, rules, schema)
        if item.class_label is None:
            findings.append(
                Finding(
                    line=item.line,
                    kind="missing",
                    state=item.state,
                    task_id=item.task_id,
                    current_class=None,
                    suggested_class=suggested,
                    suggestion_source=source,
                    item=item.body,
                )
            )
        elif item.class_label not in allowed:
            findings.append(
                Finding(
                    line=item.line,
                    kind="invalid",
                    state=item.state,
                    task_id=item.task_id,
                    current_class=item.class_label,
                    suggested_class=suggested,
                    suggestion_source=source,
                    item=item.body,
                )
            )
    return findings


def scan_todo(
    *,
    todo_path: Path = TODO_PATH,
    schema_path: Path = SCHEMA_PATH,
    assignment_path: Path = ASSIGNMENT_PATH,
) -> tuple[list[TodoItem], list[Finding], AgentClassSchema, tuple[AssignmentRule, ...]]:
    """Scan TODO.md and return items plus operator-action findings."""
    schema = load_schema(schema_path)
    rules = load_assignment_rules(schema, assignment_path)
    try:
        todo_text = todo_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read TODO file {todo_path}: {exc}") from exc
    items = list(iter_todo_items(todo_text))
    findings = find_label_gaps(items, schema, rules)
    return (items, findings, schema, rules)


def render_text(findings: list[Finding], *, total_items: int, rules_count: int) -> str:
    """Render a human-readable operator prompt."""
    lines = [
        "# TODO agent_class re-slice report",
        "",
        f"Scanned checkbox items: {total_items}",
        f"Assignment rules loaded: {rules_count}",
        f"Operator actions needed: {len(findings)}",
    ]
    if not findings:
        lines.append("")
        lines.append("All scanned TODO checkbox items have valid [class:X] labels.")
        return "\n".join(lines)

    lines.extend([
        "",
        "Add an inline [class:X] label to each missing row, or replace invalid labels.",
        "Suggested class is a hint; operator review remains required.",
        "",
    ])
    for finding in findings:
        current = f" current={finding.current_class}" if finding.current_class else ""
        task = finding.task_id or "<no-task-id>"
        lines.append(
            f"TODO.md:{finding.line}: {finding.kind} {task}{current} "
            f"-> [class:{finding.suggested_class}] ({finding.suggestion_source})"
        )
        lines.append(f"  {finding.item}")
    return "\n".join(lines)


def render_json(
    findings: list[Finding],
    *,
    total_items: int,
    schema: AgentClassSchema,
    rules: tuple[AssignmentRule, ...],
) -> str:
    """Render a machine-readable report."""
    payload = {
        "schema_version": 1,
        "todo_path": str(TODO_PATH.relative_to(REPO_ROOT)),
        "schema_path": str(SCHEMA_PATH.relative_to(REPO_ROOT)),
        "assignment_path": str(ASSIGNMENT_PATH.relative_to(REPO_ROOT)),
        "inline_label": f"[{schema.inline_label_prefix}:<agent_class>]",
        "allowed_values": list(schema.allowed_values),
        "total_items": total_items,
        "assignment_rules": [asdict(rule) for rule in rules],
        "finding_count": len(findings),
        "findings": [asdict(finding) for finding in findings],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--todo", type=Path, default=TODO_PATH, help="TODO.md path to scan.")
    parser.add_argument(
        "--schema",
        type=Path,
        default=SCHEMA_PATH,
        help="agent_class schema YAML path.",
    )
    parser.add_argument(
        "--assignment",
        type=Path,
        default=ASSIGNMENT_PATH,
        help="ADR markdown path containing the Capability assignment table.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 when any missing or invalid class labels are found.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        items, findings, schema, rules = scan_todo(
            todo_path=args.todo,
            schema_path=args.schema,
            assignment_path=args.assignment,
        )
    except ValueError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(render_json(findings, total_items=len(items), schema=schema, rules=rules))
    else:
        print(render_text(findings, total_items=len(items), rules_count=len(rules)))

    if args.check and findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
