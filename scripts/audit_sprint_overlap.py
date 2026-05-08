#!/usr/bin/env python3
"""Audit child ticket ``Files / Paths`` sections for Sprint META overlap.

OP-799 implements Pattern 12 cure (3) at the planning layer: before a
Sprint META is filed or picked up, compare the declared file/path scopes of
its children and fail if two children claim the same file, a matching glob,
or the same added directory.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd
from backend.agents.scope_to_paths import FILES_SECTION_RE


PATH_CHARS_RE = re.compile(r"^[A-Za-z0-9_./*?\[\]-]+$")
GLOB_CHARS = "*?["
ADD_WORD_RE = re.compile(r"\b(?:ADD|ADDED|NEW|CREATE|CREATED)\b", re.I)


@dataclass(frozen=True)
class PathClaim:
    """One declared path from a child ticket."""

    child_key: str
    path: str
    add: bool = False
    directory: bool = False


@dataclass(frozen=True)
class ChildIssue:
    """Minimal child ticket view needed by the audit."""

    key: str
    summary: str
    description: str


@dataclass(frozen=True)
class Overlap:
    """One conflicting declaration between two child tickets."""

    path: str
    child_a: str
    child_b: str


def adf_to_text(node: Any) -> str:
    """Flatten JIRA ADF into markdown-ish text."""
    chunks: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            node_type = value.get("type")
            if node_type == "text":
                chunks.append(value.get("text", ""))
            elif node_type == "hardBreak":
                chunks.append("\n")
            else:
                for child in value.get("content", []):
                    walk(child)
                if node_type in {"paragraph", "heading", "codeBlock"}:
                    chunks.append("\n")
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return "".join(chunks)


def parse_files_paths(description: str, *, child_key: str) -> tuple[PathClaim, ...]:
    """Parse ``Files / Paths`` declarations, including globs and add-dirs."""
    match = FILES_SECTION_RE.search(description or "")
    if not match:
        return ()

    claims: list[PathClaim] = []
    seen: set[str] = set()
    for line in match.group("body").splitlines():
        candidate = _line_path_candidate(line)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        claims.append(
            PathClaim(
                child_key=child_key,
                path=candidate,
                add=bool(ADD_WORD_RE.search(line)),
                directory=_is_directory_claim(candidate),
            )
        )
    return tuple(claims)


def _line_path_candidate(line: str) -> str | None:
    text = line.strip()
    if not text or text.startswith("#") or text.startswith("_("):
        return None
    text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", text)
    if not text:
        return None

    ticked = re.search(r"`([^`]+)`", text)
    token = ticked.group(1) if ticked else text.split(None, 1)[0]
    token = token.strip("`'\".,;:()[]{}<>")
    token = token.removeprefix("./")
    if not token or "://" in token or "/" not in token:
        return None
    if not PATH_CHARS_RE.match(token):
        return None
    return token


def _is_directory_claim(path: str) -> bool:
    if path.endswith("/"):
        return True
    if any(ch in path for ch in GLOB_CHARS):
        return False
    return PurePosixPath(path).suffix == ""


def find_overlaps(children: Iterable[ChildIssue]) -> tuple[Overlap, ...]:
    """Return all pairwise scope overlaps among child issue declarations."""
    by_child = {
        child.key: parse_files_paths(child.description, child_key=child.key)
        for child in children
    }
    keys = sorted(by_child)
    overlaps: list[Overlap] = []
    seen: set[tuple[str, str, str]] = set()

    for idx, child_a in enumerate(keys):
        for child_b in keys[idx + 1:]:
            for claim_a in by_child[child_a]:
                for claim_b in by_child[child_b]:
                    path = overlapping_path(claim_a, claim_b)
                    if not path:
                        continue
                    marker = (path, child_a, child_b)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    overlaps.append(Overlap(path=path, child_a=child_a, child_b=child_b))
    return tuple(overlaps)


def overlapping_path(a: PathClaim, b: PathClaim) -> str | None:
    """Return the conflicting path expression, or ``None`` when disjoint."""
    if a.path == b.path:
        return a.path
    if _glob_overlaps(a.path, b.path):
        return f"{a.path} <-> {b.path}"
    if a.add and b.add and a.directory and b.directory and _directories_overlap(a.path, b.path):
        return f"{a.path.rstrip('/')}/"
    return None


def _glob_overlaps(left: str, right: str) -> bool:
    left_glob = any(ch in left for ch in GLOB_CHARS)
    right_glob = any(ch in right for ch in GLOB_CHARS)
    if not left_glob and not right_glob:
        return False
    if left_glob and not right_glob:
        return fnmatch.fnmatchcase(right, left)
    if right_glob and not left_glob:
        return fnmatch.fnmatchcase(left, right)

    left_prefix = _glob_literal_prefix(left)
    right_prefix = _glob_literal_prefix(right)
    if not left_prefix or not right_prefix:
        return True
    return left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)


def _glob_literal_prefix(pattern: str) -> str:
    first_glob = min((pattern.find(ch) for ch in GLOB_CHARS if ch in pattern), default=len(pattern))
    return pattern[:first_glob].rsplit("/", 1)[0].rstrip("/") + "/"


def _directories_overlap(left: str, right: str) -> bool:
    left_dir = left.rstrip("/")
    right_dir = right.rstrip("/")
    return (
        left_dir == right_dir
        or left_dir.startswith(right_dir + "/")
        or right_dir.startswith(left_dir + "/")
    )


def fetch_child_issues(client: jd.DispatchClient, meta_key: str) -> tuple[ChildIssue, ...]:
    """Fetch children linked to a Sprint META ticket."""
    meta = jd._request(
        client,
        "GET",
        f"/issue/{meta_key}?fields=summary",
    )
    sprint_letter = _sprint_letter(((meta.get("fields") or {}).get("summary") or ""))
    jql = (
        f'project = "{client.project_key}" '
        f'AND key != "{meta_key}" '
        f'AND (parent = "{meta_key}" OR issue in linkedIssues("{meta_key}")) '
        f'ORDER BY key ASC'
    )
    resp = jd._request(
        client,
        "POST",
        "/search/jql",
        {
            "jql": jql,
            "fields": ["summary", "description"],
            "maxResults": 100,
        },
    )
    children: list[ChildIssue] = []
    for issue in resp.get("issues", []) or []:
        fields = issue.get("fields") or {}
        children.append(
            ChildIssue(
                key=issue.get("key", ""),
                summary=fields.get("summary") or "",
                description=adf_to_text(fields.get("description")),
            )
        )
    if sprint_letter:
        prefixed = tuple(
            child for child in children
            if re.match(rf"^{re.escape(sprint_letter)}\d+\b", child.summary)
        )
        if prefixed:
            return prefixed
    return tuple(children)


def _sprint_letter(summary: str) -> str | None:
    match = re.search(r"\bSprint\s+([A-Z])\b", summary or "", re.I)
    if not match:
        return None
    return match.group(1).upper()


def render_report(meta_key: str, children: tuple[ChildIssue, ...], overlaps: tuple[Overlap, ...]) -> str:
    lines = [f"Sprint scope-overlap audit: {meta_key}", f"children scanned: {len(children)}"]
    if not overlaps:
        lines.append("result: OK - no overlapping Files / Paths declarations")
        return "\n".join(lines)

    lines.append("result: FAIL - overlapping Files / Paths declarations")
    for overlap in overlaps:
        lines.append(f"- {overlap.path}: {overlap.child_a} <-> {overlap.child_b}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("meta_key", help="Sprint META ticket key, e.g. OP-784")
    parser.add_argument("--agent-class", default="subscription-codex")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    args = parser.parse_args(argv)

    client = jd.make_client(args.agent_class)
    children = fetch_child_issues(client, args.meta_key)
    overlaps = find_overlaps(children)
    if args.json:
        print(
            json.dumps(
                {
                    "meta_key": args.meta_key,
                    "children_scanned": len(children),
                    "overlaps": [asdict(overlap) for overlap in overlaps],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(render_report(args.meta_key, children, overlaps))
    return 1 if overlaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
