# JIRA ticket creation checklist (runner-pickable tickets)

## REQUIRED — runner visibility

- [ ] **issuetype = `Story`** (NOT `Task`). Runner JQL filter:
      `backend/agents/jira_dispatch.py:137 — AND issuetype = Story`
      Task-type tickets are silently invisible to the runner.

- [ ] **`class:` label exactly one of**: `subscription-codex` /
      `subscription-claude` / `api-anthropic` / `api-openai`. The label
      determines which runner picks it; mismatched class means never picked.

- [ ] **`tier:` label one of**: `S` / `M` / `L` / `X`. `tier:X` is META-only
      and excluded by runner JQL `labels not in ("tier:X")`; use S/M/L for
      runnable tickets.

- [ ] **`area:` labels MUST cover every domain referenced by AC + Files /
      Paths**. The runner prompt converts `area:X` labels into:
      `Stay strictly within these boundaries. Do NOT introduce changes to
      <every-area-NOT-listed>.`
      If AC says "Lesson appended to the generated lesson index" but
      label is `area:backend` only, CLI will correctly halt as out-of-scope.

## Common multi-area patterns

| Type of work                          | Required `area:` labels             |
| ------------------------------------- | ------------------------------------ |
| Backend feature + tests               | `backend, tests`                     |
| Backend + lessons-learned entry       | `backend, docs`                      |
| Backend feature + tests + lesson      | `backend, docs, tests`               |
| Frontend component + tests + docs     | `frontend, docs, tests`              |
| systemd timer / cron service          | `devops, docs, tests`                |
| Pure docs (e.g. SOP, runbook)         | `docs`                               |
| Cross-cutting (e.g. AI Reviewer)      | `backend, gerrit, docs, tests`       |

## RECOMMENDED

- [ ] Use `scripts/file_jira_ticket.py` (auto-validates the required fields
      and warns on `area:` mismatch by parsing description text).

- [ ] Each AC item cites concrete evidence (file:line / test name /
      Change-Id) per `docs/sop/jira-ticket-conventions.md` §3.

- [ ] Files / Paths section lists exact paths the implementer will touch.

- [ ] Spec references section links to parent META + sibling tickets.

## Anti-patterns (auto-rejected)

- AC says "looks right" / "should work" (vague; fails §3 DoD spirit).
- AC requires touching files in domains NOT in `area:` labels.
- `class:subscription-claude` + `tier:X` (X tickets are not pickable).
- Description lacks Files / Paths section AND has no `scope:` label
  (R1 file-mutex / R4 coordinator cannot predict target files).
