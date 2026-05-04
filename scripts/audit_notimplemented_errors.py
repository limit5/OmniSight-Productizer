#!/usr/bin/env python3
"""FX.8.2 - audit NotImplementedError sites as reasonable contracts or stubs.

This script scans Python-like project sources for ``NotImplementedError``
occurrences and emits a reviewable JSONL manifest plus optional Markdown
summary.  The classifier is intentionally conservative:

  * abstract methods, Protocol/ABC contracts, explicit unsupported code
    paths, and exception handlers are marked ``reasonable``.
  * bare raises or placeholder messages in concrete code are marked ``stub``.

Why a standalone script:
  * dry-run first - operators can inspect every finding before deciding
    which stubs deserve follow-up issues.
  * stdlib-only - this repo hygiene audit must run even when dependency
    installs are broken.
  * deterministic output - stable finding IDs let follow-up batches diff
    cleanly after individual stubs are fixed.

Usage:
    python3 scripts/audit_notimplemented_errors.py audit \\
        --out out/notimplemented-audit.jsonl \\
        --markdown out/notimplemented-audit.md

    python3 scripts/audit_notimplemented_errors.py audit \\
        --out out/notimplemented-audit.jsonl \\
        --fail-on-stub
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_EXCLUDE_DIRS = frozenset({
    ".git",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "backend/.venv",
    "backend/tests",
    "build",
    "dist",
    "node_modules",
    "out",
    "test_assets",
    "venv",
})
DEFAULT_EXTENSIONS = frozenset({".py", ".pyi"})
NOT_IMPLEMENTED_RE = re.compile(r"\bNotImplementedError\b")
PLACEHOLDER_RE = re.compile(
    r"\b(todo|fixme|stub|placeholder|not implemented yet|implement later)\b",
    re.IGNORECASE,
)
UNSUPPORTED_RE = re.compile(
    r"\b(unsupported|not supported|unavailable|disabled|background|backend|"
    r"provider|platform|dialect|driver|optional)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AuditFinding:
    """One NotImplementedError occurrence classified for follow-up triage."""

    finding_id: str
    verdict: str
    category: str
    path: str
    line: int
    column: int
    symbol: str
    message: str
    excerpt: str
    rationale: str


@dataclass(frozen=True)
class _Context:
    symbol: str
    class_name: str
    abstract_class: bool
    abstract_function: bool


@dataclass(frozen=True)
class _AstOccurrence:
    line: int
    column: int
    role: str
    context: _Context
    message: str


def _utc_timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_excluded(path: Path, root: Path, exclude_dirs: set[str]) -> bool:
    rel = path.relative_to(root)
    for index, part in enumerate(rel.parts):
        if part in exclude_dirs:
            return True
        joined = "/".join(rel.parts[: index + 1])
        if joined in exclude_dirs:
            return True
    return False


def _looks_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return True
    return b"\0" in chunk


def iter_candidate_files(
    root: Path,
    *,
    extensions: set[str] | None = None,
    exclude_dirs: set[str] | None = None,
) -> Iterable[Path]:
    """Yield Python-like source files under ``root`` in deterministic order."""
    root = root.resolve()
    extensions = extensions or set(DEFAULT_EXTENSIONS)
    exclude_dirs = exclude_dirs or set(DEFAULT_EXCLUDE_DIRS)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if _is_excluded(path, root, exclude_dirs):
            continue
        if path.suffix.lower() not in extensions:
            continue
        if _looks_binary(path):
            continue
        yield path


def _name_of(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Subscript):
        return _name_of(node.value)
    return ""


def _is_notimplemented_node(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Call):
        return _name_of(node.func) == "NotImplementedError"
    return _name_of(node) == "NotImplementedError"


def _notimplemented_nodes(node: ast.AST | None) -> list[ast.AST]:
    if node is None:
        return []
    if isinstance(node, ast.Tuple):
        return [item for item in node.elts if _is_notimplemented_node(item)]
    if _is_notimplemented_node(node):
        return [node]
    return []


def _extract_message(node: ast.AST | None) -> str:
    if isinstance(node, ast.Call) and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return " ".join(first.value.split())
        if isinstance(first, ast.JoinedStr):
            parts = [
                part.value
                for part in first.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ]
            return " ".join("".join(parts).split())
    return ""


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _name_of(node.func)
    return _name_of(node)


def _is_abstract_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_decorator_name(dec).endswith("abstractmethod") for dec in node.decorator_list)


def _is_abstract_class(node: ast.ClassDef) -> bool:
    names = {_name_of(base) for base in node.bases}
    if {"ABC", "abc.ABC", "Protocol", "typing.Protocol"} & names:
        return True
    return node.name.endswith(("ABC", "Base", "Protocol", "Interface"))


def _context(stack: list[ast.AST]) -> _Context:
    class_names: list[str] = []
    func_names: list[str] = []
    abstract_class = False
    abstract_function = False
    for node in stack:
        if isinstance(node, ast.ClassDef):
            class_names.append(node.name)
            abstract_class = abstract_class or _is_abstract_class(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_names.append(node.name)
            abstract_function = abstract_function or _is_abstract_function(node)
    symbol = ".".join(class_names + func_names) or "<module>"
    class_name = class_names[-1] if class_names else ""
    return _Context(
        symbol=symbol,
        class_name=class_name,
        abstract_class=abstract_class,
        abstract_function=abstract_function,
    )


class _OccurrenceVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[ast.AST] = []
        self.occurrences: list[_AstOccurrence] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Raise(self, node: ast.Raise) -> None:
        if _is_notimplemented_node(node.exc):
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            self.occurrences.append(
                _AstOccurrence(
                    line=getattr(target, "lineno", node.lineno),
                    column=getattr(target, "col_offset", node.col_offset) + 1,
                    role="raise",
                    context=_context(self.stack),
                    message=_extract_message(node.exc),
                )
            )
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        for target in _notimplemented_nodes(node.type):
            self.occurrences.append(
                _AstOccurrence(
                    line=getattr(target, "lineno", node.lineno),
                    column=getattr(target, "col_offset", node.col_offset) + 1,
                    role="except-handler",
                    context=_context(self.stack),
                    message="",
                )
            )
        self.generic_visit(node)


def _parse_ast_occurrences(path: Path) -> dict[int, list[_AstOccurrence]]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return {}
    visitor = _OccurrenceVisitor()
    visitor.visit(tree)
    by_line: dict[int, list[_AstOccurrence]] = {}
    for occurrence in visitor.occurrences:
        by_line.setdefault(occurrence.line, []).append(occurrence)
    return by_line


def _finding_id(path: str, line: int, column: int, verdict: str, category: str) -> str:
    raw = f"{path}:{line}:{column}:{verdict}:{category}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _classify(occurrence: _AstOccurrence | None) -> tuple[str, str, str, str, str]:
    if occurrence is None:
        return (
            "reasonable",
            "text-reference",
            "<unknown>",
            "",
            "Text reference only; no executable raise was detected by AST.",
        )

    ctx = occurrence.context
    if occurrence.role == "except-handler":
        return (
            "reasonable",
            "caught-optional",
            ctx.symbol,
            occurrence.message,
            "Exception handler documents fallback behavior rather than a stub.",
        )

    message = occurrence.message
    if ctx.abstract_function or ctx.abstract_class:
        return (
            "reasonable",
            "abstract-contract",
            ctx.symbol,
            message,
            "Raise is inside an abstract method or abstract/protocol-style class.",
        )
    if message and UNSUPPORTED_RE.search(message) and not PLACEHOLDER_RE.search(message):
        return (
            "reasonable",
            "explicit-unsupported",
            ctx.symbol,
            message,
            "Message describes an intentionally unsupported provider/platform path.",
        )
    if not message:
        return (
            "stub",
            "concrete-bare-raise",
            ctx.symbol,
            message,
            "Concrete code raises bare NotImplementedError with no operator rationale.",
        )
    if PLACEHOLDER_RE.search(message):
        return (
            "stub",
            "concrete-placeholder",
            ctx.symbol,
            message,
            "Message reads as a placeholder rather than an intentional unsupported path.",
        )
    return (
        "stub",
        "concrete-notimplemented",
        ctx.symbol,
        message,
        "Concrete code raises NotImplementedError outside known reasonable patterns.",
    )


def scan_notimplemented(
    root: Path,
    *,
    extensions: set[str] | None = None,
    exclude_dirs: set[str] | None = None,
) -> list[AuditFinding]:
    """Scan ``root`` for NotImplementedError occurrences and classify them."""
    root = root.resolve()
    findings: list[AuditFinding] = []
    for path in iter_candidate_files(root, extensions=extensions, exclude_dirs=exclude_dirs):
        rel_path = path.relative_to(root).as_posix()
        ast_by_line = _parse_ast_occurrences(path)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            matches = list(NOT_IMPLEMENTED_RE.finditer(line))
            if not matches:
                continue
            ast_hits = ast_by_line.get(line_no, [])
            for index, match in enumerate(matches):
                occurrence = ast_hits[index] if index < len(ast_hits) else None
                if occurrence is None:
                    continue
                verdict, category, symbol, message, rationale = _classify(occurrence)
                column = match.start() + 1
                findings.append(
                    AuditFinding(
                        finding_id=_finding_id(rel_path, line_no, column, verdict, category),
                        verdict=verdict,
                        category=category,
                        path=rel_path,
                        line=line_no,
                        column=column,
                        symbol=symbol,
                        message=message,
                        excerpt=line.strip(),
                        rationale=rationale,
                    )
                )
    return findings


def write_jsonl(path: Path, findings: list[AuditFinding]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for finding in findings:
            fp.write(json.dumps(asdict(finding), sort_keys=True) + "\n")


def load_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as fp:
        for line_no, line in enumerate(fp, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: malformed JSONL: {exc}") from exc
    return records


def render_markdown(findings: list[AuditFinding]) -> str:
    by_verdict: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_path: dict[str, int] = {}
    for finding in findings:
        by_verdict[finding.verdict] = by_verdict.get(finding.verdict, 0) + 1
        by_category[finding.category] = by_category.get(finding.category, 0) + 1
        by_path[finding.path] = by_path.get(finding.path, 0) + 1

    lines = [
        "# NotImplementedError Audit",
        "",
        f"Generated at `{_utc_timestamp()}` by `scripts/audit_notimplemented_errors.py`.",
        "",
        f"Total occurrences: **{len(findings)}**",
        "",
        "## By verdict",
        "",
    ]
    for verdict, count in sorted(by_verdict.items()):
        lines.append(f"- `{verdict}`: {count}")
    lines.extend(["", "## By category", ""])
    for category, count in sorted(by_category.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{category}`: {count}")
    lines.extend(["", "## Top files", ""])
    for path, count in sorted(by_path.items(), key=lambda item: (-item[1], item[0]))[:25]:
        lines.append(f"- `{path}`: {count}")
    lines.extend(["", "## Findings", ""])
    lines.append("| ID | Verdict | Category | Source | Symbol | Rationale |")
    lines.append("|---|---|---|---|---|---|")
    for finding in findings:
        rationale = finding.rationale.replace("|", "\\|")
        symbol = finding.symbol.replace("|", "\\|")
        lines.append(
            f"| `{finding.finding_id}` | `{finding.verdict}` | "
            f"`{finding.category}` | `{finding.path}:{finding.line}` | "
            f"`{symbol}` | {rationale} |"
        )
    return "\n".join(lines) + "\n"


def _parse_csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_audit = sub.add_parser("audit", help="Scan repo and classify NotImplementedError sites")
    p_audit.add_argument("--root", type=Path, default=REPO_ROOT)
    p_audit.add_argument("--out", required=True, type=Path)
    p_audit.add_argument("--markdown", type=Path)
    p_audit.add_argument("--fail-on-stub", action="store_true")
    p_audit.add_argument(
        "--extensions",
        default=",".join(sorted(DEFAULT_EXTENSIONS)),
        help="Comma-separated file extensions to scan",
    )
    p_audit.add_argument(
        "--exclude-dir",
        action="append",
        default=sorted(DEFAULT_EXCLUDE_DIRS),
        help="Directory name or relative directory path to skip",
    )

    args = parser.parse_args(argv)
    extensions = _parse_csv(args.extensions)
    exclude_dirs = set(args.exclude_dir)
    findings = scan_notimplemented(
        args.root,
        extensions=extensions,
        exclude_dirs=exclude_dirs,
    )
    write_jsonl(args.out, findings)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(findings), encoding="utf-8")

    stub_count = sum(1 for finding in findings if finding.verdict == "stub")
    reasonable_count = len(findings) - stub_count
    print(
        "audited "
        f"{len(findings)} NotImplementedError occurrences "
        f"({reasonable_count} reasonable, {stub_count} stub) to {args.out}"
    )
    if args.fail_on_stub and stub_count:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
