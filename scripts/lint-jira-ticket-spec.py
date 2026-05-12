#!/usr/bin/env python3
"""Reject area:devops ticket specs without an activation AC bullet.

The hook accepts either:

* Python files containing ``TicketSpec(...)`` calls like
  ``scripts/file_audit_29_tickets.py``.
* Markdown/plain-text ticket descriptions that include ``area:devops``.

For Python specs, AC list entries are treated as the future Markdown
bullets because ``build_description()`` renders each item as ``- [ ]``.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path


DEVOPS_LABEL = "area:devops"
ACTIVATION_RE = re.compile(r"\bactivation\b", re.IGNORECASE)
MARKDOWN_ACTIVATION_BULLET_RE = re.compile(
    r"^\s*(?:[-*]|\d+[.)])\s*(?:\[[ xX]\]\s*)?.*\bactivation\b",
    re.IGNORECASE | re.MULTILINE,
)
AC_KEYWORDS = {"code_ac", "deploy_ac", "integration_ac", "exercised_ac"}


@dataclass(frozen=True)
class LintFinding:
    path: Path
    line: int
    message: str

    def format(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _literal_string_list(node: ast.AST | None) -> list[str]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    items: list[str] = []
    for item in node.elts:
        value = ast.literal_eval(item)
        if isinstance(value, str):
            items.append(value)
    return items


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _keyword_map(call: ast.Call) -> dict[str, ast.AST]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}


def _lint_python_ticket_specs(path: Path, text: str) -> list[LintFinding]:
    findings: list[LintFinding] = []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return [LintFinding(path, exc.lineno or 1, f"cannot parse Python file: {exc.msg}")]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "TicketSpec":
            continue
        keywords = _keyword_map(node)
        labels = _literal_string_list(keywords.get("labels"))
        if DEVOPS_LABEL not in labels:
            continue

        ac_items: list[str] = []
        for key in AC_KEYWORDS:
            ac_items.extend(_literal_string_list(keywords.get(key)))
        if any(ACTIVATION_RE.search(item) for item in ac_items):
            continue

        summary = ""
        summary_node = keywords.get("summary")
        if summary_node is not None:
            value = ast.literal_eval(summary_node)
            if isinstance(value, str):
                summary = f" ({value})"
        findings.append(
            LintFinding(
                path,
                node.lineno,
                f"{DEVOPS_LABEL} TicketSpec lacks an activation AC bullet{summary}",
            )
        )
    return findings


def _lint_text_ticket_description(path: Path, text: str) -> list[LintFinding]:
    if DEVOPS_LABEL not in text:
        return []
    if MARKDOWN_ACTIVATION_BULLET_RE.search(text):
        return []
    return [
        LintFinding(
            path,
            1,
            f"{DEVOPS_LABEL} ticket description lacks an activation bullet",
        )
    ]


def lint_path(path: Path) -> list[LintFinding]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    if path.suffix == ".py":
        return _lint_python_ticket_specs(path, text)
    return _lint_text_ticket_description(path, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject area:devops JIRA ticket specs without an activation bullet."
    )
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)

    findings: list[LintFinding] = []
    for path in args.paths:
        if path.is_file():
            findings.extend(lint_path(path))

    if not findings:
        return 0

    for finding in findings:
        print(finding.format(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
