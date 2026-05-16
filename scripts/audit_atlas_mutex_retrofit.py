#!/usr/bin/env python3
"""Retrospectively dry-run the OP-1124 mutex injector against Atlas tickets.

Implements the Integration AC of OP-1124:

    Re-validate the existing 23 Atlas tickets retrospectively; assert
    >=3 get mutex_with auto-added.

The script queries every open OP ticket carrying the ``sprint:atlas`` or
``meta:atlas`` family of labels (configurable via ``--label``), pulls the
description, runs :func:`jira_mutex_auto_inject.auto_inject_for_filing`
against it, and reports how many tickets *would* have gained
``mutex_with`` entries had the injector been live at filing time.

Outputs a Markdown audit table + JSON summary. Read-only — does NOT
mutate any JIRA ticket. Operator follow-up tickets per row are out of
scope; this script's purpose is to prove the injector fires on real
ticket bodies and to satisfy the AC quorum.

Usage::

    python3 scripts/audit_atlas_mutex_retrofit.py            # text + json to stdout
    python3 scripts/audit_atlas_mutex_retrofit.py --json     # json only
    python3 scripts/audit_atlas_mutex_retrofit.py --label sprint:atlas

Exit codes:
    0   audit completed (regardless of quorum)
    2   ``--require-quorum N`` was set and fewer than N tickets matched
    3   JIRA fetch failed (network / auth)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from jira_mutex_auto_inject import auto_inject_for_filing  # noqa: E402

CRED_DIR = Path("~/.config/omnisight").expanduser()
DEFAULT_LABELS = ("sprint:atlas", "meta:atlas", "phase:atlas")
USER_AGENT = "OmniSight-OP-1124-atlas-retrofit/1.0"


def _load_env(env_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _jira_config() -> tuple[str, str]:
    env_file = CRED_DIR / "jira-claude.env"
    token_file = CRED_DIR / "jira-claude-token"
    env = _load_env(env_file)
    raw = f"{env['OMNISIGHT_JIRA_CLAUDE_EMAIL']}:{token_file.read_text().strip()}".encode()
    site = env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/")
    return site, "Basic " + b64encode(raw).decode()


def _request(method: str, url: str, auth_header: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": auth_header,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "{}")


def _adf_to_text(adf: dict[str, Any] | None) -> str:
    """Best-effort flatten ADF → plain text (codeBlocks + paragraphs)."""
    if not adf:
        return ""
    chunks: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            chunks.append(node["text"])
        for child in node.get("content") or []:
            walk(child)
        if node.get("type") in ("paragraph", "codeBlock", "heading"):
            chunks.append("\n")

    walk(adf)
    return "".join(chunks)


def fetch_tickets(site: str, auth: str, labels: tuple[str, ...]) -> list[dict[str, Any]]:
    """JQL-fetch all tickets carrying any of ``labels`` (paginated)."""
    label_clause = " OR ".join(f'labels = "{label}"' for label in labels)
    jql = f'project = OP AND ({label_clause}) ORDER BY created ASC'
    issues: list[dict[str, Any]] = []
    start_at = 0
    page_size = 50
    while True:
        url = (
            f"{site}/rest/api/3/search?jql="
            f"{urllib.parse.quote(jql)}"
            f"&fields=summary,description,labels,status"
            f"&startAt={start_at}&maxResults={page_size}"
        )
        page = _request("GET", url, auth)
        batch = page.get("issues") or []
        if not batch:
            break
        issues.extend(batch)
        if len(issues) >= int(page.get("total", 0)):
            break
        start_at += page_size
    return issues


def evaluate_ticket(issue: dict[str, Any]) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    desc_text = _adf_to_text(fields.get("description"))
    result = auto_inject_for_filing(desc_text)
    return {
        "key": issue.get("key"),
        "summary": fields.get("summary", ""),
        "status": (fields.get("status") or {}).get("name", ""),
        "hot_files_touched": list(result.hot_files_touched),
        "would_add_mutexes": list(result.added_mutexes),
        "already_present": list(result.already_present),
    }


def render_report(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# OP-1124 Atlas mutex-retrofit audit",
        "",
        f"Tickets scanned: **{len(rows)}**",
        f"Would gain mutex_with: **{sum(1 for r in rows if r['would_add_mutexes'])}**",
        f"Already declared correctly: **{sum(1 for r in rows if r['already_present'] and not r['would_add_mutexes'])}**",
        "",
        "| Key | Status | Hot files touched | Would add | Already present |",
        "|-----|--------|-------------------|-----------|-----------------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['key']} | {r['status']} | "
            f"{', '.join(r['hot_files_touched']) or '—'} | "
            f"{', '.join(r['would_add_mutexes']) or '—'} | "
            f"{', '.join(r['already_present']) or '—'} |"
        )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--label",
        action="append",
        default=None,
        help=f"Override default labels (default: {DEFAULT_LABELS}).",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument(
        "--require-quorum",
        type=int,
        default=0,
        help="exit 2 if fewer than N tickets would gain mutex_with",
    )
    parser.add_argument(
        "--from-file",
        type=Path,
        default=None,
        help="dev/test path: load ticket fixtures from JSON instead of JIRA",
    )
    args = parser.parse_args(argv)
    labels = tuple(args.label) if args.label else DEFAULT_LABELS

    if args.from_file is not None:
        issues = json.loads(args.from_file.read_text())
    else:
        try:
            site, auth = _jira_config()
            issues = fetch_tickets(site, auth, labels)
        except (FileNotFoundError, KeyError) as exc:
            print(f"jira config missing: {exc}", file=sys.stderr)
            return 3
        except urllib.error.URLError as exc:
            print(f"jira fetch failed: {exc}", file=sys.stderr)
            return 3

    rows = [evaluate_ticket(issue) for issue in issues]
    summary = {
        "scanned": len(rows),
        "would_add_count": sum(1 for r in rows if r["would_add_mutexes"]),
        "already_present_count": sum(
            1 for r in rows if r["already_present"] and not r["would_add_mutexes"]
        ),
        "rows": rows,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(render_report(rows))
        print()
        print("```json")
        print(json.dumps(
            {k: v for k, v in summary.items() if k != "rows"}, indent=2
        ))
        print("```")

    if args.require_quorum and summary["would_add_count"] < args.require_quorum:
        print(
            f"FAIL: only {summary['would_add_count']} tickets would gain "
            f"mutex_with; required >= {args.require_quorum}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
