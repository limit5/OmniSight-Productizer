"""Migrate MP / RPG / FX2 active items from TODO.md to JIRA tickets.

Per docs/sop/jira-ticket-conventions.md acceptance step 1. Reads TODO.md,
finds active-priority Wave sections (e.g. ``### MP.W1 — ...``), creates
one JIRA Epic per Wave + one Story per sub-item under that Epic.

Field inference per ticket:
- Component:    MP / RPG / FX2 (from priority prefix)
- class:X:      from inline [class:X] tag in TODO row (else 'unassigned')
- tier:         heuristic from text — see _infer_tier()
- area:X:       heuristic from text — see _infer_areas()
- fix_version:  MP→v0.4.0, RPG→v0.5.0, FX2→backlog (operator can adjust)
- Epic Link:    parent Wave's ticket key (created in same run if new)

Idempotency: matches by Summary (task_id prefix), skips if exists.

Run::

    python3 scripts/jira_migrate_active_tickets.py --dry-run --filter MP.W1
    python3 scripts/jira_migrate_active_tickets.py --filter MP.W1
    python3 scripts/jira_migrate_active_tickets.py --filter MP             # all MP waves
    python3 scripts/jira_migrate_active_tickets.py                         # all 191
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import urllib.error
from base64 import b64encode
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TODO_PATH = REPO_ROOT / "TODO.md"
CRED_ENV = Path("~/.config/omnisight/jira-claude.env").expanduser()
CRED_TOKEN = Path("~/.config/omnisight/jira-claude-token").expanduser()


# ── JIRA API helpers (from jira_seed_example_tickets.py) ──────────


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in CRED_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _auth_header() -> str:
    env = _load_env()
    email = env["OMNISIGHT_JIRA_CLAUDE_EMAIL"]
    token = CRED_TOKEN.read_text().strip()
    raw = f"{email}:{token}".encode()
    return "Basic " + b64encode(raw).decode()


def _base_url() -> str:
    return _load_env()["OMNISIGHT_JIRA_SITE_URL"].rstrip("/") + "/rest/api/3"


def _project_key() -> str:
    return _load_env().get("OMNISIGHT_JIRA_PROJECT_KEY", "OP")


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = _base_url() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": _auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        raise RuntimeError(f"{method} {path} → {e.code}: {body_text}") from e


def _adf_from_markdown_blob(md: str) -> dict:
    return {
        "type": "doc", "version": 1,
        "content": [{
            "type": "codeBlock",
            "attrs": {"language": "markdown"},
            "content": [{"type": "text", "text": md}],
        }],
    }


def _existing_ticket_key(summary_prefix: str) -> str | None:
    jql = f'project = "{_project_key()}" AND summary ~ "{summary_prefix[:50]}"'
    resp = _request("POST", "/search/jql", {
        "jql": jql, "fields": ["summary"], "maxResults": 20,
    })
    for issue in resp.get("issues", []):
        if issue["fields"]["summary"].startswith(summary_prefix):
            return issue["key"]
    return None


# ── TODO parsing ──────────────────────────────────────────────────


CHECKBOX_RE = re.compile(r"^- \[ \]\s+(.+)$")
TASK_ID_RE = re.compile(r"^([A-Z]+\d*\.[WD]\d+(?:\.\w+)?)\s+")
# Accept ### (Wave) and #### (sub-Wave like RPG MUST/progressive sections).
# RPG.W12-W21 in TODO.md use #### (H4) under "### MUST-have for v0.5.0" group header.
WAVE_HEADER_RE = re.compile(r"^#{3,4}\s+([A-Z]+\d*\.[WD]\d+[A-Z]?)\s+—\s+(.+?)(?:\(|$)")
CLASS_TAG_RE = re.compile(r"\[class:([a-z\-]+)\]")
FILE_PATH_RE = re.compile(r"`([^`]*\.(?:py|tsx?|md|sql|yaml|yml|json|toml|ini|sh))`")


@dataclass
class TodoItem:
    task_id: str            # MP.W1.1
    title: str              # rest of line after task_id, [class] stripped
    agent_class: str        # from inline tag
    raw_line: str
    files: list[str] = field(default_factory=list)


@dataclass
class TodoWave:
    wave_id: str            # MP.W1
    title: str              # "Backend orchestrator + quota tracker"
    raw_header: str
    intro_prose: str        # any "> " prose between header and first item
    items: list[TodoItem] = field(default_factory=list)


def parse_active_waves(text: str) -> list[TodoWave]:
    """Parse TODO.md, return Wave list for MP / RPG / FX2 priorities only."""
    lines = text.splitlines()
    waves: list[TodoWave] = []
    current: TodoWave | None = None
    intro_buf: list[str] = []
    in_intro = False

    for line in lines:
        m_wave = WAVE_HEADER_RE.match(line)
        if m_wave:
            if current is not None:
                current.intro_prose = "\n".join(intro_buf).strip()
                waves.append(current)
            wave_id = m_wave.group(1)
            if not any(wave_id.startswith(p) for p in ("MP.", "RPG.", "FX2.")):
                current = None
                intro_buf = []
                in_intro = False
                continue
            current = TodoWave(
                wave_id=wave_id,
                title=m_wave.group(2).strip().rstrip("（"),
                raw_header=line,
                intro_prose="",
            )
            intro_buf = []
            in_intro = True
            continue

        if current is None:
            continue

        m_box = CHECKBOX_RE.match(line)
        if m_box:
            in_intro = False
            body = m_box.group(1).strip()
            cls_match = CLASS_TAG_RE.search(body)
            agent_class = cls_match.group(1) if cls_match else "unassigned"
            body_clean = CLASS_TAG_RE.sub("", body).strip()
            tid_match = TASK_ID_RE.match(body_clean)
            if not tid_match:
                continue
            task_id = tid_match.group(1)
            title = body_clean[tid_match.end():].strip()
            files = FILE_PATH_RE.findall(body_clean)
            current.items.append(TodoItem(
                task_id=task_id,
                title=title,
                agent_class=agent_class,
                raw_line=line,
                files=files,
            ))
            continue

        if in_intro and line.startswith(">"):
            intro_buf.append(line.lstrip("> ").rstrip())

    if current is not None:
        current.intro_prose = "\n".join(intro_buf).strip()
        waves.append(current)
    return waves


# ── Field inference ───────────────────────────────────────────────


def _component(task_id: str) -> str:
    if task_id.startswith("MP."):
        return "MP"
    if task_id.startswith("RPG."):
        return "RPG"
    if task_id.startswith("FX2."):
        return "FX2"
    raise ValueError(f"unknown priority for {task_id}")


def _fix_version(component: str) -> str:
    return {"MP": "v0.4.0", "RPG": "v0.5.0", "FX2": "backlog"}.get(component, "backlog")


def _infer_tier(item: TodoItem) -> str:
    """Heuristic tier from text + file count.

    tier:S — 1 file, no test, no migration, ≤ 1 line description
    tier:M — default (most items)
    tier:L — alembic migration + tests + cross-module
    """
    text = item.title.lower()
    file_count = len(item.files)

    # tier:L signals
    if "alembic" in text and ("migration" in text or "table" in text or "schema" in text):
        if "test" in text or file_count >= 3:
            return "tier:L"
    if file_count >= 4:
        return "tier:L"
    if "週" in text or "day" in text or "weeks" in text:
        # explicit time hint
        if any(d in text for d in ("3 day", "3-day", "5 day", "1 週", "2 週")):
            return "tier:L"

    # tier:S signals
    if file_count == 1 and "test" not in text and "migration" not in text:
        if len(item.title) < 80:
            return "tier:S"

    return "tier:M"


def _infer_areas(item: TodoItem) -> list[str]:
    """Multi-label area inference from file paths + keywords."""
    areas: set[str] = set()
    for fp in item.files:
        if fp.startswith("backend/"):
            areas.add("area:backend")
        if fp.startswith("backend/alembic/") or "alembic" in fp:
            areas.add("area:db")
        if fp.startswith("backend/tests/") or "/tests/" in fp or "test_" in fp.split("/")[-1] or fp.endswith(".test.tsx") or fp.endswith(".test.ts"):
            areas.add("area:tests")
        if fp.startswith("components/") or fp.startswith("lib/") or fp.startswith("app/"):
            areas.add("area:frontend")
        if fp.startswith("docs/"):
            areas.add("area:docs")
        if fp.startswith("scripts/"):
            areas.add("area:tooling")

    # keyword fallbacks
    text = item.title.lower()
    if not areas:
        if "alembic" in text or "schema" in text or "migration" in text:
            areas.add("area:db")
            areas.add("area:backend")
        elif any(k in text for k in ("frontend", "component", "tsx", "react", "ui ", " ui")):
            areas.add("area:frontend")
        elif "doc" in text or "guide" in text or "runbook" in text:
            areas.add("area:docs")
        elif "test" in text:
            areas.add("area:tests")
        else:
            areas.add("area:backend")  # safe default

    # All alembic / schema / migration tickets require contract tests
    # (per ADR 0001 + lessons from OP-16 first run 2026-05-06: tests
    # were missing from the prompt-permitted area, blocking codex from
    # writing the AC-required test file). Auto-add area:tests anytime
    # the ticket touches alembic / schema work.
    if "alembic" in text or "schema" in text or "migration" in text or any("alembic" in fp for fp in item.files):
        areas.add("area:tests")

    return sorted(areas)


# ── Description rendering ─────────────────────────────────────────


def _epic_description(wave: TodoWave) -> str:
    return f"""## Wave goal
{wave.title}

## Intro (from TODO.md)
{wave.intro_prose if wave.intro_prose else '_(no intro prose)_'}

## Items in this Wave
{chr(10).join(f'- {item.task_id} {item.title[:80]}' for item in wave.items)}

---
**This Epic was migrated from TODO.md by `scripts/jira_migrate_active_tickets.py`.**
**Sub-items are linked as Stories under this Epic.**
"""


def _story_description(item: TodoItem, wave: TodoWave) -> str:
    component = _component(item.task_id)
    files_section = (
        "\n".join(f"- `{fp}`" for fp in item.files)
        if item.files else "_(no explicit file paths in TODO; refine pre-pickup)_"
    )

    spec_refs = {
        "MP": "ADR-0007 — Multi-Provider Subscription Orchestrator",
        "RPG": "ADR-0008 — Agent RPG Class & Skill Leveling",
        "FX2": "docs/audit/2026-05-06-deep-audit.md",
    }.get(component, "")

    return f"""## Goal
{item.title}

## Acceptance Criteria
- [ ] _(operator: refine before pickup — TODO.md source line is the seed)_
- [ ] All CI green (drift guards, contract tests)

## Files / Paths
{files_section}

## Spec references
- {spec_refs}
- Parent Wave: {wave.wave_id} — {wave.title}
- TODO.md source row (deprecated post-migration)

## Prerequisites

```yaml
blocks_on: []                       # operator: fill cross-wave deps
soft_prereqs: []
mutex_with: []                      # operator: add mutex:alembic-chain-head if migration
schema_locks: []
live_state_requires: []             # operator: add alembic_head if migration
external_blockers: []
```

## Definition of Done
- [ ] feature/OP-XXXX-{item.task_id.lower().replace('.', '-')} branch
- [ ] Tests pass locally + CI
- [ ] Gerrit Code-Review +2 (1 human + 1 AI per ADR-0003)
- [ ] Commit message contains `[OP-XXXX]`
- [ ] Merge to develop (per ADR-0001)

## Runner notes
- agent_class hint: {item.agent_class}
- tier: (see label)
- worktree: claude-work / codex-work depending on class

---
**Migrated from TODO.md by `scripts/jira_migrate_active_tickets.py`.**
**Pre-pickup, operator should refine Acceptance Criteria + Prerequisites.**
"""


# ── Ticket creation ───────────────────────────────────────────────


def _issue_type_for(role: str) -> dict:
    """role: 'epic' or 'story'. Uses Japanese-locale type names per JIRA gotcha."""
    return {"name": {"epic": "エピック", "story": "ストーリー"}[role]}


def _create_epic(wave: TodoWave, dry_run: bool) -> str:
    summary = f"{wave.wave_id} — {wave.title}"
    existing = _existing_ticket_key(f"{wave.wave_id} —")
    if existing:
        return f"{existing} (existing)"
    if dry_run:
        return f"<would-create-epic> {summary}"

    component = _component(wave.wave_id + ".0")  # any sub-id works for component lookup
    payload = {
        "fields": {
            "project": {"key": _project_key()},
            "summary": summary,
            "description": _adf_from_markdown_blob(_epic_description(wave)),
            "issuetype": _issue_type_for("epic"),
            "labels": [f"migrated-from-todo", f"priority:{component.lower()}"],
        }
    }
    resp = _request("POST", "/issue", payload)
    return resp["key"] + " (created)"


def _create_story(item: TodoItem, wave: TodoWave, epic_key: str | None, dry_run: bool) -> str:
    summary = f"{item.task_id} — {item.title[:200]}"
    existing = _existing_ticket_key(f"{item.task_id} —")
    if existing:
        return f"{existing} (existing)"

    component = _component(item.task_id)
    tier = _infer_tier(item)
    areas = _infer_areas(item)
    labels = [
        f"class:{item.agent_class}",
        tier,
        *areas,
        f"priority:{component.lower()}",
        "migrated-from-todo",
    ]

    if dry_run:
        return f"<would-create> {tier} class:{item.agent_class} {areas} summary={summary[:60]!r}"

    fields = {
        "project": {"key": _project_key()},
        "summary": summary,
        "description": _adf_from_markdown_blob(_story_description(item, wave)),
        "issuetype": _issue_type_for("story"),
        "labels": labels,
    }
    # Epic link via parent field (Atlassian Cloud post-2023 schema)
    if epic_key and not epic_key.startswith("<"):
        # extract pure key from "OP-NN (created)"
        clean_key = epic_key.split(" ")[0]
        fields["parent"] = {"key": clean_key}

    resp = _request("POST", "/issue", {"fields": fields})
    return f"{resp['key']} (created, tier={tier}, areas={areas})"


# ── CLI ───────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filter", help="Wave id prefix (MP / MP.W1 / RPG / FX2)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    todo_text = TODO_PATH.read_text(encoding="utf-8")
    waves = parse_active_waves(todo_text)
    if args.filter:
        # Exact match if filter looks like a full wave id (contains digit-after-dot),
        # else prefix-with-dot to avoid MP.W1 matching MP.W10-17.
        f = args.filter
        waves = [
            w for w in waves
            if w.wave_id == f or w.wave_id.startswith(f + ".")
        ]

    print(f"JIRA site: {_load_env()['OMNISIGHT_JIRA_SITE_URL']}")
    print(f"Project:   {_project_key()}")
    print(f"Mode:      {'dry-run' if args.dry_run else 'live'}")
    print(f"Filter:    {args.filter or '(all active)'}")
    print(f"Waves:     {len(waves)}")
    total_items = sum(len(w.items) for w in waves)
    print(f"Items:     {total_items}")
    print()

    for wave in waves:
        print(f"=== Wave {wave.wave_id} — {wave.title[:60]} ({len(wave.items)} items) ===")
        # Per docs/sop/jira-ticket-conventions.md §10a Epic existence invariant:
        # Epic must have ≥ 1 child Story. Skip empty Waves entirely.
        if not wave.items:
            print(f"  [SKIP] Wave has 0 active items — Epic existence invariant (§10a)")
            print()
            continue
        epic_result = _create_epic(wave, args.dry_run)
        print(f"  Epic: {epic_result}")
        for item in wave.items:
            story_result = _create_story(item, wave, epic_result, args.dry_run)
            print(f"    └─ {item.task_id}: {story_result}")
        print()

    epic_count = sum(1 for w in waves if w.items)  # empty Waves skipped per §10a
    print(f"Summary: {epic_count} epics, {total_items} stories.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
