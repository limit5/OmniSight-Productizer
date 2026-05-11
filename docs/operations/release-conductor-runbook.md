# Release Conductor Runbook

> **Ticket**: OP-937 (G1 — release template engine)
>
> **Audience**: release operator instantiating, monitoring, or
> recovering a `RELEASE-vX.Y.Z` META + 13 child pipeline.
>
> **Status**: canonical operator artifact for the JIRA-graph-as-state-
> machine release flow. The script
> `scripts/instantiate_release_meta.py` is the only supported way to
> create a release META.

## 0. Mental model — the JIRA graph IS the conductor

A production release is one META ticket (`RELEASE-vX.Y.Z`) plus 13
children (`R1`..`R13`). Each child is a runner-pickable Story that
declares its blocker via the JIRA `Blocks` link. The runner's
pre-pickup gate (`backend.agents.file_coordinator.has_unresolved_blockedby`)
enforces serial execution by walking that chain.

There is no external conductor service. The G2/G3/Sprint H wave will
add operator dashboards and a smarter scheduler on top, but the
underlying state machine is the JIRA graph itself.

| Concept | JIRA representation |
|---|---|
| Pipeline run | One `RELEASE-vX.Y.Z` META |
| Stage | One R-id child Story |
| Pickup gate | `Blocks` link, walked by `has_unresolved_blockedby` |
| Parent reference | `Relates` link from child → META |
| Audit log | META description + the `[release-template-engine]` comment |

## 1. Pre-flight

```bash
# 1) Identify the release version and confirm it is the next SemVer.
git fetch --tags gerrit origin
git tag --list 'v*' | sort -V | tail -5

# 2) Confirm the previous RELEASE-* META is fully Published.
jq_release_query='project = OP AND labels = "meta:release" '\
'AND status != Published ORDER BY created DESC'
# Run from the JIRA UI; should return either zero or only your
# in-flight release.

# 3) Sanity-check the template hasn't drifted.
python3 -m pytest backend/tests/test_release_template_engine.py
```

If the previous META is still open, halt and finish that release
first — concurrent release pipelines are not supported by the gate.

## 2. Dry-run

Always run `--dry-run` first. The script reads the template, renders
the plan, and exits without touching JIRA.

```bash
python3 scripts/instantiate_release_meta.py \
    --version v0.5.1-rc1 \
    --dry-run
```

The plan output prints:

- the META summary + labels,
- the 13 child summaries + label sets,
- 12 `blockedBy` edges (sequential R2→R1, R3→R2, …, R13→R12),
- 13 `Relates` edges (one child → META).

Read every line. The output is the source of truth for what
`--apply` will create.

## 3. Apply

```bash
python3 scripts/instantiate_release_meta.py --version v0.5.1-rc1
```

What the script does, in order:

1. Loads + validates `config/release_template.yaml`.
2. Renders the META description from
   `config/release_meta_description.md.template`.
3. Calls `find_existing_meta_key(client, version)` to enforce
   idempotency (JQL on `labels = "RELEASE-vX.Y.Z" AND labels =
   "meta:release"`).
4. Creates the META, then the 13 children in R-id order.
5. Wires the `blockedBy` chain via
   `backend.agents.file_coordinator.add_blocked_by` — intent-named to
   avoid the L-OP-874 direction inversion trap.
6. Wires `Relates` from each child to META via
   `backend.agents.file_coordinator.jira_create_issue_link
   (link_type="Relates")`. The raw `POST /issueLink` schema stays
   inside `file_coordinator.py` per the
   `scripts/check_issuelink_post_callers.py` lint rule.
7. Posts the `[release-template-engine]` comment on the META with the
   template version and the resolved `R-id → OP-key` map.

The terminal output prints the same map. Copy that block into the
release operator log; it is the single source of truth for which
ticket is which stage.

## 4. Error catalog + exit codes

| Exit | Error class | Meaning | Recovery |
|---|---|---|---|
| 0 | — | META + 13 children created and wired | proceed to R1 |
| 1 | `MetaAlreadyExists` | A META for `vX.Y.Z` already exists | inspect `OP-...`; if it was Archived, rerun with `--force` |
| 2 | `TemplateYAMLInvalid` | Schema validation failed | fix `config/release_template.yaml`; rerun `--dry-run` |
| 3 | `BlockedByWiringFailed` | Tickets created but link wiring crashed | follow §5 (Rollback) |

## 5. Rollback

If the script aborts after ticket creation but before completing the
link wiring, it writes a rollback file:

```text
/tmp/release-rollback-vX.Y.Z.json
```

The file contains every ticket key created during the run. Two
supported recovery paths:

### 5a. Operator deletes by hand

```bash
jq -r '.ticket_keys | to_entries[] | .value' \
    /tmp/release-rollback-vX.Y.Z.json | while read key; do
  echo "Deleting $key"
  # Open https://soraapp.atlassian.net/browse/$key and use
  # the JIRA UI to delete (we deliberately do NOT auto-delete).
done
```

### 5b. Cleanup script (G2, planned)

`scripts/release_rollback.py` (not yet shipped) will consume the
JSON and call DELETE for each key. Track it under the G2 follow-up.

After cleanup, re-run `scripts/instantiate_release_meta.py
--version vX.Y.Z` from scratch.

## 6. `--force` semantics

`--force` is **only** honoured when the existing META is in an
Archived state. In every other state, the script still refuses and
exits with `MetaAlreadyExists` to protect the operator from
double-instantiation against a live META.

Use cases for `--force`:

- The previous META was Archived after a deploy abort, and the
  operator wants a fresh instance for the same version.
- Disaster-recovery rebuild after JIRA-side schema corruption.

Do NOT use `--force` to clobber an in-flight META. Delete the
existing META manually, drop the rollback file, then rerun without
`--force`.

## 7. Template versioning

`config/release_template.yaml` carries a top-level `version: "v1"`.
Bump it whenever:

- The child count changes (today: exactly 13).
- A child's `r_id`, area mix, tier, or class field changes.
- The `blockedBy` chain topology changes (e.g., adding a parallel
  branch).

The script records the template version on the META so retrospectives
can correlate cycle-time data against a specific template revision.

## 8. Drift guards

| Guard | Purpose |
|---|---|
| `backend/tests/test_release_template_engine.py::test_child_label_drift_detector_matches_template` | Pins the canonical label set on each child |
| `backend/tests/test_release_template_engine.py::test_default_template_has_exactly_thirteen_children` | Pins the child count |
| `backend/tests/test_release_template_engine.py::test_default_template_blocked_by_chain_is_sequential` | Pins the chain topology |
| `scripts/check_issuelink_post_callers.py` | Prevents new raw `POST /issueLink` callers that could invert the direction (L-OP-874) |

If any of these guards fail after a template edit, halt and reconcile
before shipping the edit.

## 9. After the META lands

The operator's daily loop on a live release:

1. Watch the runner pick up `R1`, run it to Published.
2. The gate releases `R2`; runner picks it up; loop.
3. After `R13` reaches Published, the META auto-transitions to
   Published per `docs/sop/jira-ticket-conventions.md` §10. (If it
   doesn't, file a BLOCKER ticket — that's a workflow bug, not a
   release-conductor bug.)
4. The retrospective (per R13 AC) lands under
   `docs/retrospectives/<date>-vX.Y.Z.md` and the release closes.

## 10. References

- `scripts/instantiate_release_meta.py` (this script)
- `config/release_template.yaml` (per-child template)
- `config/release_meta_description.md.template` (META description template)
- `docs/sop/lessons/L-OP-874-jira-blockedby-direction-trap.md`
- `docs/operations/release-cut-runbook.md` (R1 implementation reference)
- `docs/operations/release-runbook.md` (R6-R10 implementation reference)
- `docs/operations/release-notes-runbook.md` (R11 implementation reference)
- Memory: `reference_release_workflow.md` (META-as-state-machine summary)
