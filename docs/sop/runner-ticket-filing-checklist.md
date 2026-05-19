# Runner ticket-filing checklist — single-page reference

Quick reference for filing a runner-pickable JIRA ticket without re-reading
every memory entry, conventions doc, or label schema. Companion to (not a
replacement for) `docs/sop/jira-ticket-creation-checklist.md` (verbose
checklist) and `docs/sop/jira-label-conventions.md` (label registry).

If you only have 30 seconds, skim §1 and copy the heredoc in §3.

---

## 1. 30-second cheatsheet — the 80% case

80% of filings are: **`tier:S` backend bug, single area, code+tests+lesson**.

```bash
# Adjust SUMMARY, areas, and description-file. Everything else is the
# default that the runner expects on a normal tier:S Story.
scripts/file_jira_ticket.py \
    --summary '[BOT][SCOPE] short imperative summary' \
    --description-file /tmp/desc.md \
    --priority High \
    --tier S \
    --class subscription-claude \
    --type bug \
    --areas backend,tests,docs
```

The script enforces the runner's invariants (issuetype = Story, area
whitelist, 4-AC presence via the label validator). Pass `--check` to
dry-run and print the resulting JQL without POSTing to JIRA.

---

## 2. The 10 filing rules

Each rule links to the memory it was captured in. Use the memory for
incident detail and the rule below for the short form.

1. **`area:*` must come from the 9-value whitelist.**
   `backend, frontend, devops, tests, db, docs, security, embedded,
   tooling` — anything else exits the runner with rc=1 in a silent
   pickup-revert-pickup loop (no JIRA comment posted). Authoritative
   set lives at `auto-runner-jira.py:RECOGNISED_AREAS` (line 140).
   → [[feedback_runner_recognized_areas]]

2. **`tier:S/M/L/X` is mandatory; `tier:X` = HUMAN-ONLY.**
   The runner JQL excludes only `tier:X` (`backend/agents/jira_dispatch.py`
   `PICKUP_JQL_TEMPLATE`, line 357). Anything with a HUMAN-ONLY AC, a
   `🔒` summary marker, a `runner:no-commits-expected` against an env
   that isn't permanently live, OR an unresolved blocker MUST be filed
   at `tier:X` — soft markers (`runner-blocked:*`, `release:approval-*`)
   are observability, not gates.
   → [[feedback_human_only_tickets_tier_x]]

3. **4-AC discipline: Code / Deploy / Integration / Exercised.**
   Every implementation ticket needs all four AC sections plus a
   `Go-Live` target. Sections that legitimately don't apply close with
   "N/A because <reason>". Skipping a section ships an AUDIT-23-style
   merged-but-not-deployed regression.
   → [[feedback_4_ac_discipline]]

4. **`area:*` must span every AC's directory.**
   A 4-AC ticket whose Code AC touches `backend/`, Deploy AC touches
   `docker-compose.prod.yml` (`devops`), and Integration AC adds a test
   in `backend/tests/` (`tests`) must be filed `area:backend,devops,tests`.
   Single-area filing wedges the pickup: the runner halts §11 the moment
   it discovers an unreachable AC. OP-1126 looped 19+ times on this.
   → [[feedback_ticket_area_must_span_all_4_ac_areas]]

5. **`type:meta` only for tickets that actually have children.**
   `type:meta` routes the capability matrix to the read-only safe-default
   `[mcp_search, memory_recall]`; the runner correctly refuses
   `gerrit_push` and reverts. Single-phase implementation tickets — even
   large ones — use `type:feature` or `type:bug`. Forbidden combination
   is encoded in `docs/sop/jira-label-schema.yaml`.
   → [[feedback_type_meta_routing]]

6. **`capability:enable=gerrit_push` required when the matrix denies it.**
   The capability matrix has a known leak: well-labelled `type:feature`
   Stories sometimes resolve to safe-default at pickup even when the
   matrix lookup in isolation returns the full code-edit set. Anything
   that needs to push must carry `capability:enable=gerrit_push`
   explicitly. Coord-override labels need an operator-approval comment
   per `docs/sop/jira-ticket-conventions.md` §9.
   → [[feedback_capability_safe_default_leak]]

7. **Pre-flight grep before filing AC with NEW-file targets.**
   For every `NEW: path/to/file.py` line: `git ls-tree -r
   refs/remotes/gerrit/develop -- path/to/file.py`. For every named
   class/function: `git grep -l 'ClassName' refs/remotes/gerrit/develop`.
   Skipping this filed OP-1145 over the already-shipped OP-1103 surface;
   324 lines got overwritten before Change #625 was abandoned.
   → [[feedback_filing_existing_impl_check]]

8. **Promoting a `[HOLD]` ticket: rewrite BOTH labels AND description.**
   The runner reads three independent HOLD signals: summary prefix
   `[HOLD]`, description body "NO agent:auto — operator-scheduled", and
   AC SKELETON placeholder text. Flipping labels alone leaves a
   label/content conflict that surrenders 36% of the time per §11.
   Always rewrite summary → `[BOT]`, swap the "NO agent:auto" paragraph,
   and replace the SKELETON with real 4-AC content.
   → [[feedback_backlog_promotion_must_rewrite_description]]

9. **Dependency refresh: delete old Blocks links BEFORE adding new.**
   JIRA's `/rest/api/3/issueLink` has no cycle detection. Additive
   `POST /issueLink` calls during a chain refresh silently create
   cycles; the dep-resolver then auto-stamps `runner-blocked:waiting-*`
   on every node and the whole chain wedges. Always `GET issuelinks` →
   diff → `DELETE` the stale → `POST` the new → verify the graph is a
   DAG.
   → [[feedback_dependency_refresh_must_delete_old_links]]

10. **Never unset assignee on a ticket a runner might be working.**
    Runners snapshot the assignee at pickup and re-check at pre-submit.
    Operator cleanup scripts that `PUT assignee: null` on `In Progress`
    (進行中) tickets — or on To-Do tickets reassigned in the last 30
    min — trip the TOCTOU guard and destroy the workspace + any code
    the CLI wrote. Strip `claim:*` / `runner-stoploss:*` freely; leave
    `assignee` alone.
    → [[feedback_never_unset_assignee_on_running_ticket]]

---

## 3. Minimal viable filing template

Copy, edit the `# EDIT:` lines, run. Comments explain why each section
exists; delete them before piping to `--description-file`.

```bash
cat > /tmp/desc.md <<'EOF'
# EDIT: 1-line context — what is broken, why this ticket exists.

## Files / Paths
# EDIT: every path the implementer will touch. Drives area-span check (rule 4).
- backend/foo/bar.py
- backend/tests/test_bar.py
- docs/sop/lessons/L-OP-XXXX-...md

## Spec / parent references
# EDIT: link META + sibling tickets so the reviewer has context.
- META: OP-NNNN
- Sibling: OP-NNNN

## Acceptance criteria (4-section discipline — AUDIT-23 protection)

### 1. Code AC — 寫好了
- [ ] EDIT: modules / functions / unit tests landed.

### 2. Deploy AC — 跑起來了
- [ ] EDIT: container up / systemd unit active / dependency installed.
- [ ] (or "N/A because pure-library change" if truly deploy-less)

### 3. Integration AC — 串起來了
- [ ] EDIT: feature wired into upstream caller; downstream consumer verified.

### 4. Exercised AC — 真的有人用
- [ ] EDIT: N invocations / M log lines / K days uptime observed.

## Go-Live target
# EDIT: absolute date or "T+Nd from META start".
2026-MM-DD
EOF

# Dry-run first; the script prints the runner-visible JQL so you can
# confirm the ticket WILL be picked once filed.
scripts/file_jira_ticket.py \
    --summary '[BOT][SCOPE] short imperative summary' \
    --description-file /tmp/desc.md \
    --priority High \
    --tier S \
    --class subscription-claude \
    --type bug \
    --areas backend,tests,docs \
    --check

# When happy, drop --check to actually POST.
```

Notes:
- `--class subscription-claude` vs `subscription-codex` decides which
  bot account runs the work. `api-anthropic` / `api-openai` route to
  the cloud-API runners instead.
- `--type` ∈ `{bug, feature, docs, meta}`. Picking `meta` for anything
  without children trips rule 5 — don't.
- For pushable tickets that the matrix may safe-default on, add
  `capability:enable=gerrit_push` to the label set (rule 6). The
  filing script doesn't accept this flag directly; edit the resulting
  ticket on the JIRA web UI or extend `_labels()` per
  `docs/sop/jira-label-conventions.md`.

---

## 4. Common runner-side failure modes + rescue commands

| Symptom | Likely cause | Rescue |
|---|---|---|
| Runner picks ticket → exits rc=1 → re-picks → repeats, no JIRA comment | Unknown `area:*` label (rule 1) | Tail `/home/user/work/sora/logs/runner/{claude,codex}-bot-*.log`. Fix `area:` to whitelist value. |
| Two runners both claimed the ticket; duplicate Gerrit pushes | Pickup mutex race despite OP-977 fencing | `omnisight-runner-rescue dump --operator <me>` to see live leases. `release <lease_id> --reason "..." --operator <me>` to surrender the loser. See `docs/sop/runner-pickup-mutex.md`. |
| `claim:default` / `claim:<uuid>` label stuck after revert | Stale claim — runner died mid-pickup OR auto-strip falsified (see [[feedback_capability_safe_default_leak]] N+9..N+21) | `add_comment` + `remove_label('claim:default')` via `backend/agents/jira_dispatch.py` helpers. NEVER use it to unset assignee (rule 10). |
| Ticket in `进行中` but `runner-blocked:waiting-OP-XXXX` label and predecessor is closed | Dep-resolver stale, or cycle in Blocks graph (rule 9) | Audit `GET /issue/{key}?fields=issuelinks` for cycles. `DELETE` cycle edges. The waiting-label clears on next runner tick. |
| Runner reverts immediately with `[runner-no-commits-from-cli]` after a clean §11 surrender | Compound: amended labels didn't unstick the pickup + no-commits guard fired | Don't repeat the clean §11. Commit the in-area portion of the AC (breaks the no-commits guard), post per-AC ✓/✗ verification, exit clean. See [[feedback_ticket_area_must_span_all_4_ac_areas]] (OP-1126 5th-pickup recovery). |
| Runner picked a ticket it shouldn't have (HUMAN-only AC, env not live, predecessor open) | Filed at `tier:S` instead of `tier:X` (rule 2) | Post per-AC ✗ comment with concrete evidence (curl NXDOMAIN / predecessor status / etc.), call `transition_back_to_todo(c, KEY, reason)` per `jira-ticket-conventions.md` §11. Flag to operator that `tier:S` → `tier:X` re-tier is needed (operator authority — do NOT self-apply). |
| Capability matrix returned safe-default `[mcp_search, memory_recall]` on a normal `type:feature` ticket | Rule 6 leak | Post AC verification ✗, revert §11. Add `capability:enable=gerrit_push` (with operator-approval comment) when re-filing. |

---

## 5. Cross-references

Primary memories (rules above link to each):
- [[feedback_runner_recognized_areas]]
- [[feedback_human_only_tickets_tier_x]]
- [[feedback_4_ac_discipline]]
- [[feedback_ticket_area_must_span_all_4_ac_areas]]
- [[feedback_type_meta_routing]]
- [[feedback_capability_safe_default_leak]]
- [[feedback_filing_existing_impl_check]]
- [[feedback_backlog_promotion_must_rewrite_description]]
- [[feedback_dependency_refresh_must_delete_old_links]]
- [[feedback_never_unset_assignee_on_running_ticket]]

Scripts / runtime:
- `scripts/file_jira_ticket.py` — filing CLI; enforces issuetype=Story,
  area-whitelist, area-match-vs-description warning.
- `scripts/jira_label_validator.py` — label-schema validator (4-AC
  presence, forbidden combinations).
- `auto-runner-jira.py` — runner entry-point; `RECOGNISED_AREAS`
  (line 140) is the authoritative area whitelist.
- `backend/agents/jira_dispatch.py` — JIRA client + pickup JQL
  (`PICKUP_JQL_TEMPLATE`, line 357), atomic-claim mutex.
- `backend/agents/capability_matrix.py` — `(type × area × tier)` →
  enabled capability set; consumer of `capability:enable=` /
  `capability:disable=` overrides.

Companion SOPs:
- `docs/sop/jira-ticket-creation-checklist.md` — verbose filing
  checklist (multi-area patterns, anti-patterns).
- `docs/sop/jira-ticket-conventions.md` — §3 (DoD spirit), §9
  (coord-override approval), §11 (discovered-dependency surrender),
  §16 (pickup JQL).
- `docs/sop/jira-label-conventions.md` — label registry (prose).
- `docs/sop/jira-label-schema.yaml` — label registry
  (machine-parseable).
- `docs/sop/runner-pickup-mutex.md` — fencing-token claim mechanism,
  rescue invariants.
- `docs/sop/runner-rescue-cli.md` — `omnisight-runner-rescue` dump /
  release / reset operator runbook.
