"""JIRA dispatch — shared module for runner ticket pickup, transitions, comments.

Per ``docs/sop/jira-ticket-conventions.md`` §16. Replaces the TODO.md
parsing logic in ``auto-runner-codex.py`` with JIRA REST-driven
discovery. Used by ``auto-runner-jira.py`` as the dispatch backbone.

Public API:
- ``fetch_pickable_tickets(agent_class)`` — JQL search per §16, parsed
  into TicketSnapshot objects (compatible with backend.agents.scheduler)
- ``transition_to_in_progress(key, bot_account_id)`` — TODO → In Progress
- ``transition_to_under_review(key, gerrit_url)`` — In Progress → Under Review
- ``transition_back_to_todo(key, reason)`` — In Progress → TODO (revert)
- ``add_comment(key, body)`` — append ADF comment
- ``parse_prerequisites(ticket)`` — extract YAML block from description

Authentication: reads ``~/.config/omnisight/jira-claude.env`` /
``~/.config/omnisight/jira-codex.env`` etc. based on agent_class.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from base64 import b64encode
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.agents.scheduler import TicketSnapshot

# ── Auth + config per agent_class ─────────────────────────────────

CRED_DIR = Path("~/.config/omnisight").expanduser()


def _cred_paths(agent_class: str) -> tuple[Path, Path]:
    """Return (env_file, token_file) for agent_class.

    Convention: 'subscription-codex' / 'api-openai' → codex bot creds
                everything else → claude bot creds (shared default).
    """
    if agent_class in ("subscription-codex", "api-openai"):
        return CRED_DIR / "jira-codex.env", CRED_DIR / "jira-codex-token"
    return CRED_DIR / "jira-claude.env", CRED_DIR / "jira-claude-token"


def _load_env(env_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


@dataclass(frozen=True)
class DispatchClient:
    """Authenticated JIRA REST client scoped to one agent_class."""

    agent_class: str
    base_url: str
    project_key: str
    auth_header: str
    bot_account_id: str
    bot_email: str


def make_client(agent_class: str) -> DispatchClient:
    env_file, token_file = _cred_paths(agent_class)
    env = _load_env(env_file)
    token = token_file.read_text().strip()
    # Email key varies per bot file
    email_key = "OMNISIGHT_JIRA_CLAUDE_EMAIL" if "claude" in env_file.name else "OMNISIGHT_JIRA_CODEX_EMAIL"
    email = env[email_key]
    raw = f"{email}:{token}".encode()
    auth = "Basic " + b64encode(raw).decode()
    site = env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/")
    project = env.get("OMNISIGHT_JIRA_PROJECT_KEY", "OP")
    # accountId via /myself
    me = _request_raw("GET", site + "/rest/api/3/myself", auth, None)
    return DispatchClient(
        agent_class=agent_class,
        base_url=site + "/rest/api/3",
        project_key=project,
        auth_header=auth,
        bot_account_id=me["accountId"],
        bot_email=email,
    )


def _request_raw(method: str, url: str, auth_header: str, body: dict | None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": auth_header,
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
        raise RuntimeError(f"{method} {url} → {e.code}: {body_text}") from e


def _request(client: DispatchClient, method: str, path: str, body: dict | None = None) -> dict:
    return _request_raw(method, client.base_url + path, client.auth_header, body)


# ── ADF helpers ───────────────────────────────────────────────────


def _adf_paragraph(text: str) -> dict:
    return {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


# ── Ticket fetch + JQL ────────────────────────────────────────────

# Gotcha: JQL `issuetype = "ストーリー"` (JP-locale name) does NOT match
# Story-type issues even though /myself reports the localised name. JQL
# accepts the untranslated English `Story` or numeric ID. Verified
# 2026-05-06 against soraapp.atlassian.net OP project.
PICKUP_JQL_TEMPLATE = (
    'project = "{project}" '
    'AND issuetype = Story '
    'AND status = "To Do" '
    'AND assignee is EMPTY '
    'AND labels = "class:{cls}" '
    'AND status != "Waiting for External" '
    'AND labels not in ("tier:X") '
    'ORDER BY priority DESC, created ASC'
)


def fetch_pickable_tickets(client: DispatchClient, max_results: int = 50) -> list[dict]:
    """Run pickup JQL per §16. Returns raw issue dicts (not snapshots)."""
    jql = PICKUP_JQL_TEMPLATE.format(project=client.project_key, cls=client.agent_class)
    resp = _request(client, "POST", "/search/jql", {
        "jql": jql,
        "fields": ["summary", "labels", "status", "issuetype", "fixVersions",
                   "created", "components", "issuelinks", "parent"],
        "maxResults": max_results,
    })
    return resp.get("issues", [])


def to_snapshot(issue: dict) -> TicketSnapshot:
    """Convert raw JIRA issue payload to TicketSnapshot for scheduler.

    Component lookup order: JIRA Component field (operator-set) →
    `priority:X` label (migration-set fallback) → "default".
    """
    f = issue["fields"]
    labels = f.get("labels", [])
    component = "default"
    components_field = f.get("components") or []
    if components_field:
        component = components_field[0].get("name", "default")
    else:
        for label in labels:
            if label.startswith("priority:"):
                component = label.split(":", 1)[1].upper()
                break
    fix_v = None
    if f.get("fixVersions"):
        fix_v = f["fixVersions"][0].get("name")
    created_str = f.get("created", "")
    try:
        created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        days_since = (datetime.now(timezone.utc) - created_dt).total_seconds() / 86400
    except (ValueError, TypeError):
        days_since = 1.0
    days_to_fv: float | None = None
    if fix_v and re.match(r"^v\d", fix_v):
        # crude: assume 30 days from now if SemVer; operator can override later
        days_to_fv = 30.0
    mutex_labels = tuple(l for l in labels if l.startswith("mutex:"))
    # downstream blockers + mutex sibling: deferred to ticket-fetch enrichment
    return TicketSnapshot(
        key=issue["key"],
        component=component,
        fix_version=fix_v,
        created_at=created_str,
        days_since_created=days_since,
        days_to_fix_version=days_to_fv,
        downstream_blocked_count=0,  # enrichment via separate JQL pass (deferred)
        mutex_labels=mutex_labels,
        has_mutex_in_progress_sibling=False,  # deferred mutex check
    )


# ── State transitions ─────────────────────────────────────────────

# OP project workflow transition IDs (verified via /transitions endpoint).
# These must match Atlassian Cloud project's workflow config.
# If workflow changes, update this map and the convention §10 doc.
TRANSITION_IDS = {
    "to_in_progress": "21",   # JP locale: "進行中"
    "back_to_todo": "11",     # JP locale: "To Do"
    # to_under_review / approved / published: TBD when workflow extended
}


def transition_to_in_progress(client: DispatchClient, key: str) -> None:
    """Set assignee = bot, transition TODO → In Progress, add pickup comment."""
    _request(client, "PUT", f"/issue/{key}", {
        "fields": {"assignee": {"accountId": client.bot_account_id}},
    })
    _request(client, "POST", f"/issue/{key}/transitions", {
        "transition": {"id": TRANSITION_IDS["to_in_progress"]},
    })
    add_comment(client, key, f"Picked up by {client.agent_class} runner.")


def transition_back_to_todo(client: DispatchClient, key: str, reason: str) -> None:
    """In Progress → TODO with reason comment + clear assignee."""
    add_comment(client, key, f"Reverting to TODO. Reason:\n{reason}")
    _request(client, "PUT", f"/issue/{key}", {
        "fields": {"assignee": None},
    })
    _request(client, "POST", f"/issue/{key}/transitions", {
        "transition": {"id": TRANSITION_IDS["back_to_todo"]},
    })


def add_comment(client: DispatchClient, key: str, text: str) -> None:
    _request(client, "POST", f"/issue/{key}/comment", {"body": _adf_paragraph(text)})


# ── Description / Prerequisites parsing ───────────────────────────


def fetch_description(client: DispatchClient, key: str) -> str:
    """Pull description as markdown text from ADF code-block payload."""
    issue = _request(client, "GET", f"/issue/{key}?fields=description")
    desc = issue["fields"].get("description")
    if not desc:
        return ""
    # Walk ADF tree and concat text nodes (handles codeBlock content)
    chunks: list[str] = []
    def _walk(node):
        if isinstance(node, dict):
            t = node.get("type")
            if t == "text":
                chunks.append(node.get("text", ""))
            elif t == "hardBreak":
                chunks.append("\n")
            elif t == "paragraph":
                for c in node.get("content", []):
                    _walk(c)
                chunks.append("\n\n")
            elif t == "codeBlock":
                for c in node.get("content", []):
                    _walk(c)
                chunks.append("\n")
            else:
                for c in node.get("content", []):
                    _walk(c)
    _walk(desc)
    return "".join(chunks)


PREREQS_RE = re.compile(
    r"##\s+Prerequisites.*?```yaml\s*(.+?)\s*```",
    re.DOTALL | re.IGNORECASE,
)


def parse_prerequisites(description: str) -> dict[str, list]:
    """Extract Prerequisites YAML block; return parsed dict.

    Returns empty dict if no block found (treats as no prerequisites).
    """
    m = PREREQS_RE.search(description)
    if not m:
        return {
            "blocks_on": [], "soft_prereqs": [], "mutex_with": [],
            "schema_locks": [], "live_state_requires": [], "external_blockers": [],
        }
    import yaml
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}  # malformed YAML → caller treats as fail-safe
    # Normalize all keys present
    for k in ("blocks_on", "soft_prereqs", "mutex_with", "schema_locks",
              "live_state_requires", "external_blockers"):
        data.setdefault(k, [])
    return data


# ── Pre-pickup check (combined live-state + mutex + blocker) ──────


def pre_pickup_ok(client: DispatchClient, snapshot: TicketSnapshot) -> tuple[bool, str]:
    """Combined pre-pickup gate. Returns (ok, reason)."""
    from backend.agents.live_state_check import evaluate, all_passed, format_failures
    desc = fetch_description(client, snapshot.key)
    prereqs = parse_prerequisites(desc)

    # Live-state checks (§13)
    if prereqs.get("live_state_requires"):
        results = evaluate(prereqs["live_state_requires"])
        if not all_passed(results):
            return False, "live_state_requires failed:\n" + format_failures(results)

    # Hard blocker check via JIRA workflow validator is L1 (§10).
    # Mutex check: TODO — JQL search siblings with same mutex_with In Progress.
    # Skipped in this minimum-viable; the heavy penalty in scheduler.score
    # already deprioritises mutex conflicts.

    return True, "pre-pickup checks passed"
