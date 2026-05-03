#!/usr/bin/env python3
"""FX.8.1 - export TODO/FIXME markers and batch-create GitHub issues.

This script turns source-code ``TODO`` / ``FIXME`` comments into a
reviewable JSONL manifest plus optional GitHub issue creation calls.

Why a standalone script:
  * dry-run first - operators can inspect the exact issue payloads
    before spending API quota or creating 1000+ issues.
  * stdlib-only - this is repo hygiene tooling and must keep working
    even when dependency installs are broken.
  * resumable batch creation - ``--created-log`` records marker IDs
    that already landed in GitHub, so interrupted runs can continue
    without re-creating the same local batch.

Usage:
    python3 scripts/export_todo_fixme_issues.py export \\
        --out out/todo-fixme-issues.jsonl \\
        --markdown out/todo-fixme-issues.md

    python3 scripts/export_todo_fixme_issues.py batch-create \\
        --input out/todo-fixme-issues.jsonl \\
        --repo owner/repo \\
        --dry-run --limit 25

    GITHUB_TOKEN=... python3 scripts/export_todo_fixme_issues.py batch-create \\
        --input out/todo-fixme-issues.jsonl \\
        --repo owner/repo \\
        --created-log out/todo-fixme-created.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
GITHUB_API = "https://api.github.com"
USER_AGENT = "OmniSight-FX8-TodoFixmeIssueExporter/1.0"

DEFAULT_EXCLUDE_DIRS = frozenset({
    ".git",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "backend/.venv",
    "build",
    "dist",
    "node_modules",
    "out",
    "test_assets",
    "venv",
})
DEFAULT_LABELS = ("todo-fixme", "technical-debt")
DEFAULT_EXTENSIONS = frozenset({
    ".c",
    ".cc",
    ".conf",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".md",
    ".mjs",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
})
MARKER_RE = re.compile(r"\b(?P<kind>TODO|FIXME)\b(?P<tail>[^A-Za-z0-9_]*)", re.I)
ISSUE_BODY_MAX = 60_000


@dataclass(frozen=True)
class MarkerIssue:
    """One source TODO/FIXME marker projected into a GitHub issue payload."""

    marker_id: str
    kind: str
    path: str
    line: int
    column: int
    text: str
    excerpt: str
    title: str
    body: str
    labels: list[str]


def _utc_timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_excluded(path: Path, root: Path, exclude_dirs: set[str]) -> bool:
    rel = path.relative_to(root)
    parts = rel.parts
    for index, part in enumerate(parts):
        if part in exclude_dirs:
            return True
        joined = "/".join(parts[: index + 1])
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
    """Yield source-like files under ``root`` in deterministic order."""
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


def _clean_marker_text(line: str, marker_end: int, next_marker_start: int | None = None) -> str:
    after = line[marker_end:next_marker_start]
    text = after.strip()
    text = re.sub(r"^[\s:()#/*\-]+", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text or "(no marker text)"


def _marker_id(path: str, line: int, column: int, kind: str, text: str) -> str:
    raw = f"{path}:{line}:{column}:{kind}:{text}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def build_issue(
    *,
    path: str,
    line: int,
    column: int,
    kind: str,
    text: str,
    excerpt: str,
    labels: list[str] | None = None,
) -> MarkerIssue:
    """Build the GitHub issue payload for one marker."""
    marker_id = _marker_id(path, line, column, kind, text)
    title = _truncate(f"[{kind}] {path}:{line} - {text}", 120)
    issue_labels = list(labels or DEFAULT_LABELS)
    body_lines = [
        f"Automated tracking issue for `{kind}` marker `{marker_id}`.",
        "",
        f"- **Source**: `{path}:{line}`",
        f"- **Column**: {column}",
        f"- **Marker**: `{kind}`",
        f"- **Exported at**: `{_utc_timestamp()}`",
        "",
        "## Source excerpt",
        "",
        "```text",
        excerpt.rstrip(),
        "```",
        "",
        "## Acceptance",
        "",
        "- Resolve the underlying TODO/FIXME or convert it into a documented decision.",
        "- Remove or update the source marker after the fix lands.",
        "",
        "---",
        "_Generated by `scripts/export_todo_fixme_issues.py` for FX.8.1._",
    ]
    body = "\n".join(body_lines)
    if len(body.encode("utf-8")) > ISSUE_BODY_MAX:
        body = body.encode("utf-8")[:ISSUE_BODY_MAX].decode("utf-8", "ignore")
    return MarkerIssue(
        marker_id=marker_id,
        kind=kind,
        path=path,
        line=line,
        column=column,
        text=text,
        excerpt=excerpt.rstrip(),
        title=title,
        body=body,
        labels=issue_labels,
    )


def scan_markers(
    root: Path,
    *,
    labels: list[str] | None = None,
    extensions: set[str] | None = None,
    exclude_dirs: set[str] | None = None,
) -> list[MarkerIssue]:
    """Scan ``root`` for TODO/FIXME markers and return issue payloads."""
    root = root.resolve()
    issues: list[MarkerIssue] = []
    for path in iter_candidate_files(
        root, extensions=extensions, exclude_dirs=exclude_dirs
    ):
        rel_path = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            matches = list(MARKER_RE.finditer(line))
            for index, match in enumerate(matches):
                kind = match.group("kind").upper()
                column = match.start("kind") + 1
                next_start = (
                    matches[index + 1].start("kind")
                    if index + 1 < len(matches) else None
                )
                text = _clean_marker_text(line, match.end("kind"), next_start)
                issues.append(
                    build_issue(
                        path=rel_path,
                        line=line_no,
                        column=column,
                        kind=kind,
                        text=text,
                        excerpt=line.strip(),
                        labels=labels,
                    )
                )
    return issues


def write_jsonl(path: Path, issues: list[MarkerIssue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for issue in issues:
            fp.write(json.dumps(asdict(issue), sort_keys=True) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
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


def render_markdown(issues: list[MarkerIssue]) -> str:
    by_kind: dict[str, int] = {}
    by_path: dict[str, int] = {}
    for issue in issues:
        by_kind[issue.kind] = by_kind.get(issue.kind, 0) + 1
        by_path[issue.path] = by_path.get(issue.path, 0) + 1

    lines = [
        "# TODO/FIXME GitHub Issue Export",
        "",
        f"Generated at `{_utc_timestamp()}` by `scripts/export_todo_fixme_issues.py`.",
        "",
        f"Total markers: **{len(issues)}**",
        "",
        "## By marker",
        "",
    ]
    for kind, count in sorted(by_kind.items()):
        lines.append(f"- `{kind}`: {count}")
    lines.extend(["", "## Top files", ""])
    for path, count in sorted(by_path.items(), key=lambda item: (-item[1], item[0]))[:25]:
        lines.append(f"- `{path}`: {count}")
    lines.extend(["", "## Issue payloads", ""])
    lines.append("| ID | Marker | Source | Title |")
    lines.append("|---|---|---|---|")
    for issue in issues:
        title = issue.title.replace("|", "\\|")
        lines.append(
            f"| `{issue.marker_id}` | `{issue.kind}` | "
            f"`{issue.path}:{issue.line}` | {title} |"
        )
    return "\n".join(lines) + "\n"


def _load_created_ids(path: Path | None) -> set[str]:
    if not path or not path.is_file():
        return set()
    ids: set[str] = set()
    for record in load_jsonl(path):
        marker_id = record.get("marker_id")
        if isinstance(marker_id, str):
            ids.add(marker_id)
    return ids


def _append_created_log(path: Path | None, record: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, sort_keys=True) + "\n")


def _github_create_issue(repo: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{GITHUB_API}/repos/{repo}/issues"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"GitHub API HTTP {exc.code}: {detail}") from exc


def batch_create_issues(
    records: list[dict[str, Any]],
    *,
    repo: str,
    token: str | None,
    dry_run: bool,
    limit: int | None = None,
    created_log: Path | None = None,
    sleep_seconds: float = 0.0,
    create_issue: Callable[[str, str, dict[str, Any]], dict[str, Any]] = _github_create_issue,
) -> dict[str, int]:
    """Create GitHub issues from exported records.

    The function has no shared module state: resumability comes only
    from the explicit ``created_log`` file, so parallel workers do not
    coordinate through Python globals.
    """
    if not dry_run and not token:
        raise ValueError("GITHUB token required unless --dry-run is set")

    created_ids = _load_created_ids(created_log)
    counts = {"created": 0, "dry_run": 0, "skipped": 0}
    for record in records:
        marker_id = str(record.get("marker_id", ""))
        if marker_id in created_ids:
            counts["skipped"] += 1
            continue
        if limit is not None and counts["created"] + counts["dry_run"] >= limit:
            break

        payload = {
            "title": record["title"],
            "body": record["body"],
            "labels": record.get("labels", list(DEFAULT_LABELS)),
        }
        if dry_run:
            print(json.dumps({"marker_id": marker_id, "payload": payload}, sort_keys=True))
            counts["dry_run"] += 1
            continue

        response = create_issue(repo, token or "", payload)
        log_record = {
            "marker_id": marker_id,
            "html_url": response.get("html_url"),
            "number": response.get("number"),
            "created_at": _utc_timestamp(),
        }
        _append_created_log(created_log, log_record)
        created_ids.add(marker_id)
        counts["created"] += 1
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return counts


def _parse_csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_export = sub.add_parser("export", help="Scan repo and export issue payloads")
    p_export.add_argument("--root", type=Path, default=REPO_ROOT)
    p_export.add_argument("--out", required=True, type=Path)
    p_export.add_argument("--markdown", type=Path)
    p_export.add_argument("--label", action="append", default=list(DEFAULT_LABELS))
    p_export.add_argument(
        "--extensions",
        default=",".join(sorted(DEFAULT_EXTENSIONS)),
        help="Comma-separated file extensions to scan",
    )
    p_export.add_argument(
        "--exclude-dir",
        action="append",
        default=sorted(DEFAULT_EXCLUDE_DIRS),
        help="Directory name or relative directory path to skip",
    )

    p_create = sub.add_parser("batch-create", help="Create GitHub issues from JSONL")
    p_create.add_argument("--input", required=True, type=Path)
    p_create.add_argument("--repo", required=True, help="GitHub repo `owner/name`")
    p_create.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    p_create.add_argument("--dry-run", action="store_true")
    p_create.add_argument("--limit", type=int)
    p_create.add_argument("--created-log", type=Path)
    p_create.add_argument("--sleep", type=float, default=0.0)

    args = parser.parse_args(argv)
    if args.cmd == "export":
        extensions = _parse_csv(args.extensions)
        exclude_dirs = set(args.exclude_dir)
        issues = scan_markers(
            args.root,
            labels=args.label,
            extensions=extensions,
            exclude_dirs=exclude_dirs,
        )
        write_jsonl(args.out, issues)
        if args.markdown:
            args.markdown.parent.mkdir(parents=True, exist_ok=True)
            args.markdown.write_text(render_markdown(issues), encoding="utf-8")
        print(f"exported {len(issues)} markers to {args.out}")
        return 0

    records = load_jsonl(args.input)
    counts = batch_create_issues(
        records,
        repo=args.repo,
        token=args.token,
        dry_run=args.dry_run,
        limit=args.limit,
        created_log=args.created_log,
        sleep_seconds=args.sleep,
    )
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
