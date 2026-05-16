#!/usr/bin/env python3
"""Inventory all labels in JIRA OP project.

For each label, reports:
  - total ticket count
  - open count (To Do / 進行中 / Under Review / 承認済み)
  - closed count (公開済み / Archived / Won't Do / Done)
  - latest ticket created with this label (key + date)

Groups output by prefix (area:, tier:, class:, scope:, etc.).

Usage:
  scripts/audit_jira_labels.py [--all] [--min-count N]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

CRED_DIR = Path("~/.config/omnisight").expanduser()
USER_AGENT = "OmniSight-audit-jira-labels/1.0"

OPEN_STATUSES = {"To Do", "In Progress", "進行中", "Under Review", "承認済み", "Open", "Reopened"}
CLOSED_STATUSES = {"公開済み", "Archived", "Won't Do", "Done", "Closed", "Resolved"}


def _load_env(env_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _jira_config() -> tuple[str, str, str]:
    env = _load_env(CRED_DIR / "jira-claude.env")
    token = (CRED_DIR / "jira-claude-token").read_text().strip()
    auth = "Basic " + b64encode(f"{env['OMNISIGHT_JIRA_CLAUDE_EMAIL']}:{token}".encode()).decode()
    return env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/"), env.get("OMNISIGHT_JIRA_PROJECT_KEY", "OP"), auth


def _get(url: str, auth: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": auth, "Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        text = exc.read().decode() if exc.fp else ""
        raise RuntimeError(f"GET {url} -> {exc.code}: {text}") from exc


def fetch_all_issues(site: str, project: str, auth: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    next_token: str | None = None
    page = 0
    while True:
        page += 1
        params = {
            "jql": f"project = {project} ORDER BY created DESC",
            "fields": "labels,status,created",
            "maxResults": "100",
        }
        if next_token:
            params["nextPageToken"] = next_token
        url = site + "/rest/api/3/search/jql?" + urllib.parse.urlencode(params)
        try:
            resp = _get(url, auth)
        except RuntimeError as exc:
            if "410" in str(exc) or "404" in str(exc):
                url = site + "/rest/api/3/search?" + urllib.parse.urlencode({
                    "jql": f"project = {project} ORDER BY created DESC",
                    "fields": "labels,status,created",
                    "maxResults": "100",
                    "startAt": str(len(issues)),
                })
                resp = _get(url, auth)
            else:
                raise
        batch = resp.get("issues", [])
        issues.extend(batch)
        print(f"  page {page}: fetched {len(batch)} (cumulative {len(issues)})", file=sys.stderr)
        if "nextPageToken" in resp and resp["nextPageToken"]:
            next_token = resp["nextPageToken"]
        elif "isLast" in resp:
            if resp["isLast"]:
                break
        elif len(batch) < 100:
            break
        else:
            if "nextPageToken" not in resp:
                if resp.get("startAt", 0) + len(batch) >= resp.get("total", 999999):
                    break
                next_token = None
        if page > 200:
            print("  WARN: stopping at 200 pages safety cap", file=sys.stderr)
            break
    return issues


def categorize(label: str) -> str:
    if ":" in label:
        return label.split(":", 1)[0] + ":*"
    return "(no-prefix)"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="print every label (default: top 100)")
    parser.add_argument("--min-count", type=int, default=1, help="hide labels with fewer than N total tickets")
    args = parser.parse_args(argv)

    site, project, auth = _jira_config()
    print(f"Fetching all issues from {site} project={project} ...", file=sys.stderr)
    issues = fetch_all_issues(site, project, auth)
    print(f"Total issues: {len(issues)}\n", file=sys.stderr)

    label_total: dict[str, int] = defaultdict(int)
    label_open: dict[str, int] = defaultdict(int)
    label_closed: dict[str, int] = defaultdict(int)
    label_latest_key: dict[str, str] = {}
    label_latest_date: dict[str, str] = {}

    for issue in issues:
        fields = issue.get("fields", {})
        labels = fields.get("labels", []) or []
        status_name = (fields.get("status", {}) or {}).get("name", "")
        created = fields.get("created", "")[:10]
        key = issue.get("key", "")
        for lab in labels:
            label_total[lab] += 1
            if status_name in OPEN_STATUSES:
                label_open[lab] += 1
            elif status_name in CLOSED_STATUSES:
                label_closed[lab] += 1
            if lab not in label_latest_date or created > label_latest_date[lab]:
                label_latest_date[lab] = created
                label_latest_key[lab] = key

    sorted_labels = sorted(label_total.items(), key=lambda x: (-x[1], x[0]))
    if not args.all:
        sorted_labels = sorted_labels[:200]

    by_prefix: dict[str, list[tuple[str, int, int, int, str, str]]] = defaultdict(list)
    for lab, total in sorted_labels:
        if total < args.min_count:
            continue
        prefix = categorize(lab)
        by_prefix[prefix].append((
            lab, total, label_open[lab], label_closed[lab],
            label_latest_key.get(lab, ""), label_latest_date.get(lab, ""),
        ))

    print(f"=== JIRA OP label inventory ({len(label_total)} unique labels) ===\n")
    print(f"{'LABEL':<45s} {'TOT':>5s} {'OPEN':>5s} {'CLSD':>5s} {'LATEST_KEY':>10s} {'LATEST_DATE':>12s}")
    print("-" * 90)
    for prefix in sorted(by_prefix.keys()):
        entries = by_prefix[prefix]
        print(f"\n--- {prefix} ({len(entries)} labels, totaling {sum(e[1] for e in entries)} ticket-uses) ---")
        for lab, total, op, cl, key, date in entries:
            print(f"{lab:<45s} {total:>5d} {op:>5d} {cl:>5d} {key:>10s} {date:>12s}")

    print(f"\n=== Summary ===")
    print(f"Total unique labels: {len(label_total)}")
    print(f"Labels with >50 tickets: {sum(1 for v in label_total.values() if v > 50)}")
    print(f"Labels with 1-5 tickets (likely one-off): {sum(1 for v in label_total.values() if v <= 5)}")
    print(f"Labels with 0 open tickets (likely stale): {sum(1 for lab in label_total if label_open[lab] == 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
