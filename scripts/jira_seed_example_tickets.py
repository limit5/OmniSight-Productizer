"""Seed 3 example JIRA tickets for ticket-convention UI verification.

Per ``docs/sop/jira-ticket-conventions.md`` acceptance criterion 2.
Creates one ticket each:

  1. MP example — operational backend Story (Appendix A)
  2. RPG example — backend ticket from RPG epic
  3. META retrospective — process work demo (Appendix B)

Idempotent: checks if a ticket with the exact Summary already exists
before creating; returns existing key if found.

Uses Claude bot credentials at ~/.config/omnisight/jira-claude.env.
JIRA Atlassian Cloud requires Description / Comment in ADF (Atlassian
Document Format) — markdown blob is wrapped in a doc envelope.

Run::

    python3 scripts/jira_seed_example_tickets.py
    python3 scripts/jira_seed_example_tickets.py --dry-run

After creation, operator visually verifies the 3 tickets in JIRA UI
(list view + board view + detail view) per acceptance criterion 3.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from base64 import b64encode
from pathlib import Path

CRED_ENV = Path("~/.config/omnisight/jira-claude.env").expanduser()
CRED_TOKEN = Path("~/.config/omnisight/jira-claude-token").expanduser()


def _load_env() -> dict[str, str]:
    """Parse the env file (KEY=VALUE per line)."""
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
    env = _load_env()
    return env["OMNISIGHT_JIRA_SITE_URL"].rstrip("/") + "/rest/api/3"


def _project_key() -> str:
    return _load_env().get("OMNISIGHT_JIRA_PROJECT_KEY", "OP")


def _request(method: str, path: str, body: dict | None = None) -> dict:
    """Issue a JIRA REST call. Returns parsed JSON or raises."""
    url = _base_url() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
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


# ── ADF builders ─────────────────────────────────────────────────


def _adf_text(text: str) -> dict:
    """Wrap a single paragraph of plain text in ADF."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def _adf_from_markdown_blob(markdown: str) -> dict:
    """Wrap full markdown text as one code-block paragraph in ADF.

    JIRA renders ADF code blocks fine for our purpose (preserves
    formatting). For real description rendering, a markdown→ADF
    converter would be ideal but is out of scope for skeleton seed.
    """
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "codeBlock",
                "attrs": {"language": "markdown"},
                "content": [{"type": "text", "text": markdown}],
            }
        ],
    }


# ── Ticket payloads ──────────────────────────────────────────────


MP_EXAMPLE_SUMMARY = "MP.W1.1 — provider_orchestrator.py central registry [example]"

MP_EXAMPLE_DESCRIPTION = """## Goal
Central registry of `ProviderAdapter` + routing + circuit breaker.
Foundation for cap-aware multi-provider dispatch (per ADR-0007).

## Acceptance Criteria
- [ ] `ProviderAdapter` ABC defined with register / dispatch / health hooks
- [ ] Anthropic + OpenAI subscription adapter shells wired
- [ ] Circuit breaker trips on 5 consecutive 429
- [ ] All CI green (drift guards, contract tests)

## Files / Paths
- backend/agents/provider_orchestrator.py (new, ~250 LOC)
- backend/tests/test_provider_orchestrator.py (new, ~30 test)

## Spec references
- ADR-0007 §Architecture: Provider Orchestrator
- TODO.md MP.W1.1 (will be Archived after JIRA migration)

## Prerequisites

```yaml
blocks_on:
  - <OP-key for MP.W0 schema>   # placeholder until W0 ticket exists

soft_prereqs: []

mutex_with:
  - mutex:alembic-chain-head

schema_locks: []

live_state_requires:
  - alembic_head: "0198"
  - command_succeeds: "python3 -c 'import yaml; yaml.safe_load(open(\\"config/agent_class_schema.yaml\\"))'"

external_blockers: []
```

## Definition of Done
- [ ] feature/OP-XXXX-mp-w1-1-orchestrator branch
- [ ] Tests pass locally + CI
- [ ] Gerrit Code-Review +2 (1 human + 1 AI per ADR-0003)
- [ ] Commit message contains `[OP-XXXX]`
- [ ] Merge to develop (per ADR-0001)

## Runner notes
- agent_class hint: api-anthropic
- tier: M (4-12 hour scope)
- worktree: claude-work

---
**This is a seed example for ticket-convention UI verification (acceptance criterion 2).**
**Not for execution. Operator may delete or amend after review.**
"""

RPG_EXAMPLE_SUMMARY = "RPG.W1.1 — agent_character_card alembic migration [example]"

RPG_EXAMPLE_DESCRIPTION = """## Goal
alembic migration creating `agent_character_card` table per ADR-0008
identity model: agent_id / class / instance_suffix / guild / level /
xp / specialization_label / style_fingerprint / created_at.

## Acceptance Criteria
- [ ] alembic revision file with correct chain (`down_revision`=current head)
- [ ] PostgreSQL + SQLite test passes
- [ ] RLS policy attached if multi-tenant (per ADR-0006 patterns)
- [ ] Index on (agent_id) + (guild, class) for routing lookups

## Files / Paths
- backend/alembic/versions/XXXX_agent_character_card.py (new)
- backend/tests/test_alembic_XXXX_agent_character_card.py (new)

## Spec references
- ADR-0008 §Identity model
- ADR-0008 §Memory hierarchy → L1 stat sheet
- TODO.md RPG.W1.1

## Prerequisites

```yaml
blocks_on: []
soft_prereqs:
  - <OP-key for BP.B>   # Guild enum value list ideal but not required for schema

mutex_with:
  - mutex:alembic-chain-head

schema_locks:
  - source_priority: BP.B
    target: backend/agents/guild_registry.py
    contract: enum_value_list
    drift_guard_test: tests/test_guild_registry_byte_equal.py

live_state_requires:
  - alembic_head: "<current head>"

external_blockers: []
```

## Definition of Done
- [ ] feature/OP-YYYY-rpg-w1-1-character-card branch
- [ ] Migration applies cleanly on PG + SQLite
- [ ] Gerrit +2 + commit references OP-YYYY
- [ ] Merge to develop

## Runner notes
- agent_class hint: api-anthropic
- tier: M (4-12 hour scope)

---
**Seed example for UI verification. Not for execution.**
"""

# ── B6 feature-list JSON example payload (OP-835) ────────────────
#
# Five sample tickets, each carrying a ``## Feature list (JSON)``
# section in the description, so operators can verify the dual-mode
# parser end-to-end. The schema is the one consumed by
# ``backend.agents.feature_list_parser.parse_feature_list_json``:
#
#   [{"id": "FL<n>",
#     "description": "<what must hold>",
#     "verify": "test_<name> passes" | "manual:operator-check"}]
#
# Edit-time gotcha: keep the JSON valid (no trailing commas, no
# comments). The seed script does NOT validate; that happens at
# pickup time inside the parser.


B6_FEATURE_LIST_EXAMPLES: list[tuple[str, str, list[str]]] = [
    (
        "B6 example #1 — provider_orchestrator feature-list [example]",
        """## Goal
Wire `ProviderAdapter` registry per ADR-0007 (feature-list demo).

## Acceptance criteria
See ``## Feature list (JSON)`` below. Submit gate uses the structured
list; this freeform section is preserved for human readability only.

## Feature list (JSON)

```json
[
  {"id": "FL1", "description": "ProviderAdapter ABC defined", "verify": "test_provider_adapter_abc passes"},
  {"id": "FL2", "description": "Anthropic adapter registered", "verify": "test_anthropic_adapter_registered passes"},
  {"id": "FL3", "description": "Circuit breaker trips on 5×429", "verify": "test_circuit_breaker_threshold passes"},
  {"id": "FL4", "description": "Operator UI shows breaker state", "verify": "manual:operator-check"}
]
```

## Error catalog
- `adapter_register_conflict` — two adapters claim the same id

## State transitions
register → healthy → (5×429) → open → (cool-down) → half-open → healthy

## Recovery / rollback
Idempotent register; breaker state is in-memory only.

---
**B6 dual-mode seed example. Not for execution.**
""",
        ["class:subscription-claude", "tier:M", "area:backend", "example", "b6:feature-list"],
    ),
    (
        "B6 example #2 — locator handoff schema feature-list [example]",
        """## Goal
Freeze the Haiku→Sonnet handoff JSON schema per B7 prerequisites.

## Acceptance criteria
Structured below.

## Feature list (JSON)

```json
[
  {"id": "FL1", "description": "Schema file backend/agents/locator_handoff_schema.py exists", "verify": "test_locator_handoff_schema_imports passes"},
  {"id": "FL2", "description": "files[] / hypotheses[] / confidence keys present", "verify": "test_locator_handoff_required_keys passes"},
  {"id": "FL3", "description": "Reject >2k token payload", "verify": "test_locator_handoff_size_guard passes"}
]
```

## Error catalog
- `handoff_schema_violation` — coder receives payload missing required keys

## State transitions
locator_emit → schema_validate → coder_accept | reject_too_large

## Recovery / rollback
Locator can be re-invoked with reminder (max 1 retry).

---
**B6 dual-mode seed example. Not for execution.**
""",
        ["class:subscription-claude", "tier:S", "area:backend", "example", "b6:feature-list"],
    ),
    (
        "B6 example #3 — TODO.md staging guard feature-list [example]",
        """## Goal
Lesson-2 F20 staging guard: TODO.md `[x][G]` flips must be staged.

## Acceptance criteria
Structured below.

## Feature list (JSON)

```json
[
  {"id": "FL1", "description": "Helper writes [x][G] and stages it", "verify": "test_todo_md_stage_helper passes"},
  {"id": "FL2", "description": "Raise TodoNotStaged when forgotten", "verify": "test_todo_md_stage_guard_raises passes"},
  {"id": "FL3", "description": "Integration: full commit cycle", "verify": "test_todo_md_stage_integration passes"}
]
```

## Error catalog
- `TodoNotStaged` — F20 staging analogue (B6 mirrors this)

## State transitions
todo_flip → write → stage → commit | TodoNotStaged

## Recovery / rollback
Idempotent: re-running stage helper is safe.

---
**B6 dual-mode seed example. Not for execution.**
""",
        ["class:subscription-claude", "tier:S", "area:tooling", "example", "b6:feature-list"],
    ),
    (
        "B6 example #4 — bridge-state checklist item demo [example]",
        """## Goal
Demo a feature-list that includes a bridge-state correctness item
(F5 past-failure relevance).

## Acceptance criteria
Structured below.

## Feature list (JSON)

```json
[
  {"id": "FL1", "description": "Bridge in 'Approved' before submit", "verify": "manual:operator-check"},
  {"id": "FL2", "description": "Gerrit Change-Id stable across PS", "verify": "test_change_id_stable passes"}
]
```

## Error catalog
- `bridge_state_drift` — bridge != ticket state at submit time

## State transitions
pre_submit → bridge_check → submit | bridge_state_drift

## Recovery / rollback
Bridge poll is idempotent; submit is one-shot per Change-Id.

---
**B6 dual-mode seed example. Not for execution.**
""",
        ["class:subscription-claude", "tier:S", "area:backend", "example", "b6:feature-list"],
    ),
    (
        "B6 example #5 — manual-only verify rows demo [example]",
        """## Goal
Show that all-manual ``verify`` rows are legal (operator-driven AC).

## Acceptance criteria
Structured below.

## Feature list (JSON)

```json
[
  {"id": "FL1", "description": "UI affordance visible on staging", "verify": "manual:operator-check"},
  {"id": "FL2", "description": "Operator confirms no regressions on dashboard load", "verify": "manual:operator-check"}
]
```

## Error catalog
- `manual_verify_pending` — operator has not yet confirmed

## State transitions
pre_submit → checklist_inject → manual_pass → submit

## Recovery / rollback
Checklist is idempotent; re-injection produces identical payload.

---
**B6 dual-mode seed example. Not for execution.**
""",
        ["class:subscription-claude", "tier:S", "area:tooling", "example", "b6:feature-list"],
    ),
]


META_EXAMPLE_SUMMARY = "Retro example — drift:over-run pattern demo [example]"

META_EXAMPLE_DESCRIPTION = """## Retrospective example for ticket-convention demo

**This is a seed demonstrating §14 META retrospective format.**
**Not auto-generated by drift detector — manually created for UI review.**

### Drift signal (would be auto-filled by drift detector)
- Tier target: tier:M (≤ 12 hour)
- Actual: 29 hour (hypothetical)
- Ratio: 2.4x over target

## Required structured fields

```yaml
situation: |
  Hypothetical: a tier:M ticket to add MFA enforcement was estimated as
  pure backend wiring. Actual scope expanded into auth-baseline allowlist
  changes + 3 frontend test refactors (cross-area drift not declared).

divergence: |
  Pre-pickup: declared as area:backend, no frontend hint.
  Mid-execution: discovered the MFA challenge needs a UI affordance,
  triggering a frontend touch. Ticket was completed with cross-area
  changes despite single area:backend label.

root_cause: |
  Missing pre-pickup AC review by reviewer. The "area" label was set by
  ticket author based on description prose, not on a forensic file-list
  walk. Cross-area work wasn't flagged at creation.

contributing: |
  - Convention §5 prompt injection enforced area:backend only, but
    runner had no mechanism to halt-and-revert when frontend touch
    became necessary.
  - No checklist for ticket author "did you trace through every file
    you'll touch?".

concrete_fix: |
  - Add §11 discovered-dependency clause for area discovery: when
    runner finds out-of-area touch needed, it must halt + comment +
    revert to TODO with proposed area amendment.
  - Add ticket-author checklist to §3 (or new §3a): "list every file
    you expect to touch; for each, confirm its area label".

verification: |
  Next 5 tier:M tickets: track ratio. Target: < 1.5 for 80% of them.
  Track area-amendment comments via JQL label query as leading indicator.
```

## Prerequisites

```yaml
blocks_on: []
soft_prereqs: []
mutex_with: []
schema_locks: []
live_state_requires: []
external_blockers:
  - approval_required: non-source-agent-plus-one
```

---
**Seed example for UI verification. Not for execution.**
"""


def _existing_ticket_key(summary: str) -> str | None:
    """JQL search by summary; return key if found else None.

    Uses /rest/api/3/search/jql (the post-2025 replacement for the
    deprecated /search endpoint).
    """
    jql = f'project = "{_project_key()}" AND summary ~ "{summary[:50]}"'
    resp = _request("POST", "/search/jql", {"jql": jql, "fields": ["summary"], "maxResults": 5})
    for issue in resp.get("issues", []):
        if issue["fields"]["summary"] == summary:
            return issue["key"]
    return None


def _create_issue(summary: str, description_md: str, labels: list[str]) -> str:
    """Create a Story-type ticket with the given summary + description + labels.

    Issue type uses Japanese localised name "ストーリー" (Story) per the
    OP project's locale (gotcha #1 in JIRA reference memory).
    """
    payload = {
        "fields": {
            "project": {"key": _project_key()},
            "summary": summary,
            "description": _adf_from_markdown_blob(description_md),
            "issuetype": {"name": "ストーリー"},
            "labels": labels,
        }
    }
    resp = _request("POST", "/issue", payload)
    return resp["key"]


def seed_or_get(summary: str, description_md: str, labels: list[str], dry_run: bool) -> str:
    existing = _existing_ticket_key(summary)
    if existing:
        return f"{existing} (existing)"
    if dry_run:
        return f"<would-create> {summary}"
    return _create_issue(summary, description_md, labels) + " (created)"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    examples = [
        (
            MP_EXAMPLE_SUMMARY,
            MP_EXAMPLE_DESCRIPTION,
            ["class:api-anthropic", "tier:M", "area:backend", "area:db", "example"],
        ),
        (
            RPG_EXAMPLE_SUMMARY,
            RPG_EXAMPLE_DESCRIPTION,
            ["class:api-anthropic", "tier:M", "area:backend", "area:db", "example"],
        ),
        (
            META_EXAMPLE_SUMMARY,
            META_EXAMPLE_DESCRIPTION,
            ["class:subscription-claude", "tier:S", "area:docs", "meta:retrospective", "drift:over-run", "example"],
        ),
        *B6_FEATURE_LIST_EXAMPLES,
    ]

    print(f"JIRA site: {_load_env()['OMNISIGHT_JIRA_SITE_URL']}")
    print(f"Project:   {_project_key()}")
    print(f"Mode:      {'dry-run' if args.dry_run else 'live'}")
    print()

    for summary, description, labels in examples:
        result = seed_or_get(summary, description, labels, args.dry_run)
        print(f"  {result}\n    summary: {summary}")
        print()

    print("Done. Operator: open each in JIRA UI to verify list / board / detail rendering.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
