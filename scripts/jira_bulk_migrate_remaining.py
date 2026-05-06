"""Bulk-migrate remaining TODO sections (BP / WP / Z / CL / HD / L4 / L5 / HE / BS)
into JIRA tickets, assigned to corresponding sprints.

Per operator directive 2026-05-06: items in TODO should not stay there indefinitely;
each gets a target sprint (Q2 table mapping). Placeholder-quality AC — operators
refine per-item before pickup (same pattern as MP/RPG/FX2 migration earlier).

Sprint mapping (from /tmp/op_sprints.json):
  BP                → Sprint 4 (id=44)
  WP / Z            → Sprint 5 (id=45)
  CL                → Sprint 6 (id=46)
  HD                → Sprint 7 (id=47)
  L4                → Sprint 8 (id=48)
  L5                → Sprint 9 (id=49)
  HE                → Sprint 10 (id=50)
  BS / H / SALE     → Sprint 5 (id=45) catch-all for small priorities
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
import urllib.error
from base64 import b64encode
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TODO_PATH = REPO_ROOT / "TODO.md"
CRED_ENV = Path("~/.config/omnisight/jira-claude.env").expanduser()
CRED_TOKEN = Path("~/.config/omnisight/jira-claude-token").expanduser()


def _load_env():
    out = {}
    for line in CRED_ENV.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.strip().partition("=")
            out[k.strip()] = v.strip()
    return out


def _request(method, path, body=None):
    env = _load_env()
    auth = env["OMNISIGHT_JIRA_CLAUDE_EMAIL"] + ":" + CRED_TOKEN.read_text().strip()
    headers = {
        "Authorization": "Basic " + b64encode(auth.encode()).decode(),
        "Content-Type": "application/json", "Accept": "application/json",
    }
    url = env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read().decode()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        raise RuntimeError(f"{method} {path} → {e.code}: {body_text}") from e


def _adf_codeblock(md: str) -> dict:
    return {
        "type": "doc", "version": 1,
        "content": [{
            "type": "codeBlock", "attrs": {"language": "markdown"},
            "content": [{"type": "text", "text": md}],
        }],
    }


# Priority → (sprint_id, default tier, default class)
PRIORITY_SPRINT_MAP = {
    "BP":   (44, "tier:M", "class:subscription-codex"),
    "WP":   (45, "tier:M", "class:subscription-codex"),
    "Z":    (45, "tier:M", "class:subscription-codex"),
    "Q":    (45, "tier:M", "class:subscription-codex"),
    "KS":   (45, "tier:M", "class:subscription-codex"),
    "CL":   (46, "tier:M", "class:subscription-codex"),
    "HD":   (47, "tier:L", "class:api-anthropic"),  # hardware design = high blast
    "L4":   (48, "tier:M", "class:subscription-codex"),
    "L5":   (49, "tier:L", "class:api-anthropic"),  # R&D = needs deeper context
    "HE":   (50, "tier:L", "class:api-anthropic"),  # hardware = same as HD
    "BS":   (45, "tier:S", "class:subscription-codex"),
    "H":    (45, "tier:M", "class:subscription-codex"),
    "SALE": (45, "tier:S", "class:subscription-codex"),
}


# Section header regex: `## 🅡🅟🅖 Priority <PREFIX> — <title>`
PRIORITY_SECTION_RE = re.compile(
    r"^##\s+(?:🅐|🆐|🅑|🅒|🅓|🅔|🅕|🅖|🅗|🅘|🅙|🅚|🅛|🅜|🅝|🅞|🅟|🅠|🅡|🅢|🅣|🅤|🅥|🅦|🅧|🅨|🅩|🅡🅟🅖|🅛🅵🅸🅻|🅛🅵🅸🅵|🅒🅛|🅑🅢|🅐🅢|🅚🅢|🅦🅟|🅗🅓|🅷🅴|🆉🆉|🅵🆇|🅼🅿|📊|📅|🅑🅢|🅐🅢|🆉|☠️)\s+Priority\s+([A-Z][A-Za-z0-9-]*?)\s+—",
    re.MULTILINE,
)
CHECKBOX_RE = re.compile(r"^- \[ \]\s+(.+)$")
TASK_ID_RE = re.compile(r"^([A-Z]+\d*\.[A-Z0-9.\-]+)\s+")
CLASS_TAG_RE = re.compile(r"\[class:([a-z\-]+)\]")
FILE_PATH_RE = re.compile(r"`([^`]*\.(?:py|tsx?|md|sql|yaml|yml|json|toml|ini|sh))`")


def parse_priority_sections(text: str, target_priorities: set[str]) -> dict[str, list[dict]]:
    """Extract `[ ]` items per priority section.

    Returns: {priority_prefix: [{task_id, title, files, agent_class}, ...]}
    """
    lines = text.splitlines()
    result: dict[str, list[dict]] = {p: [] for p in target_priorities}
    current_prefix: str | None = None

    for line in lines:
        m_sec = PRIORITY_SECTION_RE.match(line)
        if m_sec:
            prefix = m_sec.group(1)
            # Map raw priority prefix to canonical (KS-early → KS, Y-prep → Y, etc.)
            canonical = prefix.split("-")[0]  # crude — handles "KS-early" → "KS"
            current_prefix = canonical if canonical in target_priorities else None
            continue
        if current_prefix is None:
            continue
        m_box = CHECKBOX_RE.match(line)
        if not m_box:
            continue
        body = m_box.group(1).strip()
        # Strip class tag if present
        cls_match = CLASS_TAG_RE.search(body)
        agent_class = cls_match.group(1) if cls_match else None
        body_clean = CLASS_TAG_RE.sub("", body).strip()
        # Extract task_id (must start with priority prefix to count)
        tid_match = TASK_ID_RE.match(body_clean)
        if not tid_match:
            continue
        task_id = tid_match.group(1)
        # Verify it really starts with the current_prefix
        if not (task_id.startswith(current_prefix + ".") or task_id == current_prefix):
            continue
        title = body_clean[tid_match.end():].strip()
        files = FILE_PATH_RE.findall(body_clean)
        result[current_prefix].append({
            "task_id": task_id,
            "title": title,
            "files": files,
            "agent_class": agent_class,
        })

    return result


def _existing_ticket(summary_prefix: str) -> str | None:
    jql = f'project = "OP" AND summary ~ "{summary_prefix[:50]}"'
    resp = _request("POST", "/rest/api/3/search/jql", {
        "jql": jql, "fields": ["summary"], "maxResults": 5,
    })
    for issue in resp.get("issues", []):
        if issue["fields"]["summary"].startswith(summary_prefix):
            return issue["key"]
    return None


def _create_story(item: dict, priority: str) -> str | None:
    """Create one story; return key (or None if exists/failed)."""
    sprint_id, default_tier, default_class = PRIORITY_SPRINT_MAP[priority]
    summary = f"{item['task_id']} — {item['title'][:200]}"
    existing = _existing_ticket(item['task_id'] + " —")
    if existing:
        return existing  # idempotent: skip create, return existing

    files_md = "\n".join(f"- `{fp}`" for fp in item["files"]) if item["files"] else "_(refine on pickup)_"
    description_md = f"""## Goal

{item['title']}

## Acceptance Criteria

- [ ] _(operator/agent: refine before pickup — TODO.md source line is the seed)_
- [ ] All CI green (drift guards, contract tests)

## Files / Paths

{files_md}

## Spec references

- TODO.md `Priority {priority}` section (deprecated post-migration; this ticket is the canonical source)

## Prerequisites

```yaml
blocks_on: []
soft_prereqs: []
mutex_with: []
schema_locks: []
live_state_requires: []
external_blockers: []
```

## Definition of Done

- [ ] Branch `feature/<OP-key>-{item['task_id'].lower().replace('.', '-')}`
- [ ] Tests pass locally + CI
- [ ] Gerrit Code-Review +2 (1 human + 1 AI per ADR-0003)
- [ ] Commit message contains `[<OP-key>]`
- [ ] Merge to develop

## Runner notes

- agent_class hint: {item['agent_class'] or default_class.split(':', 1)[1]}
- tier: (see label)
- worktree: claude-work / codex-work depending on class

---
**Migrated from TODO.md by `scripts/jira_bulk_migrate_remaining.py` (2026-05-07).**
**Pre-pickup, refine Acceptance Criteria + Prerequisites.**
"""

    labels = [
        "area:backend",  # default — operator can amend
        f"class:{item['agent_class']}" if item["agent_class"] else default_class,
        default_tier,
        f"priority:{priority.lower()}",
        "migrated-from-todo-bulk",
    ]
    payload = {
        "fields": {
            "project": {"key": "OP"},
            "summary": summary,
            "description": _adf_codeblock(description_md),
            "issuetype": {"name": "ストーリー"},
            "labels": labels,
        }
    }
    try:
        resp = _request("POST", "/rest/api/3/issue", payload)
        return resp["key"]
    except RuntimeError as e:
        print(f"    ✗ create failed for {item['task_id']}: {e}")
        return None


def _assign_to_sprint(sprint_id: int, ticket_keys: list[str]) -> None:
    if not ticket_keys:
        return
    for i in range(0, len(ticket_keys), 50):
        batch = ticket_keys[i:i+50]
        try:
            _request("POST", f"/rest/agile/1.0/sprint/{sprint_id}/issue", {"issues": batch})
        except RuntimeError as e:
            print(f"    ✗ sprint assign failed: {e}")


def main():
    target = set(PRIORITY_SPRINT_MAP.keys())
    print(f"Bulk migration target priorities: {sorted(target)}")

    text = TODO_PATH.read_text(encoding="utf-8")
    sections = parse_priority_sections(text, target)

    total_items = sum(len(v) for v in sections.values())
    print(f"Total items found: {total_items}")
    for p, items in sections.items():
        print(f"  {p}: {len(items)} items")

    print()
    print("=== creating tickets ===")
    by_sprint: dict[int, list[str]] = {sid: [] for sid, _, _ in PRIORITY_SPRINT_MAP.values()}
    created_count = 0
    skipped_count = 0
    for priority, items in sections.items():
        if not items:
            continue
        sprint_id = PRIORITY_SPRINT_MAP[priority][0]
        print(f"\n  {priority} → sprint {sprint_id} ({len(items)} items)")
        for item in items:
            key = _create_story(item, priority)
            if key:
                by_sprint[sprint_id].append(key)
                created_count += 1
                if created_count % 25 == 0:
                    print(f"    progress: {created_count} created so far")
            else:
                skipped_count += 1

    print(f"\n=== summary ===")
    print(f"  created: {created_count}")
    print(f"  skipped (existed/failed): {skipped_count}")

    print(f"\n=== assigning to sprints ===")
    for sid, keys in by_sprint.items():
        if keys:
            print(f"  sprint {sid}: {len(keys)} tickets")
            _assign_to_sprint(sid, keys)


if __name__ == "__main__":
    main()
