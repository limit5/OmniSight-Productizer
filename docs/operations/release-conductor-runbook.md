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

## 4. Daily cron operator labels

`scripts/release_conductor_cron.sh` normally creates a release META
only after the fixVersion is SemVer-shaped, no live `RELEASE-vX.Y.Z`
META exists, and the OP-868 milestone acceptance check is green.
Operators may override that cron decision by adding exactly one
release-conductor label to the JIRA fixVersion. If the JIRA UI exposes
version labels, use that field; otherwise place the token as a standalone
word in the fixVersion Description:

| Label | Cron behavior | Audit event |
|---|---|---|
| `release:skip-auto-conductor` | Ignore this fixVersion. The cron does not run acceptance or instantiate a META. | `OperatorSkipAutoConductor` |
| `release:force-create` | Create the META even when acceptance is not green. Existing META checks still run first. | `OperatorForceCreate` then `ReleaseMetaInstantiated` |

`release:force-create` is an operator override, not a substitute for
acceptance evidence. The cron prepends an `OPERATOR OVERRIDE WARNING`
section to the generated META description before calling
`scripts/instantiate_release_meta.py`, so every downstream reviewer can
see that the milestone gate was bypassed at creation time.

The labels are mutually exclusive. If both are present, the cron
refuses to act, emits `LabelConflictBothSet`, and alerts through
`RELEASE_CONDUCTOR_NOTIFY_CMD` when that hook is configured. If any
other release-conductor label is present, for example a typo such as
`release:force_create`, the cron refuses with `LabelInvalid` and alerts
the operator.

Recovery is label-only: add, remove, or correct the fixVersion label in
JIRA and wait for the next daily cron run. No code change or manual
state-file edit is required.

## 5. Error catalog + exit codes

| Exit | Error class | Meaning | Recovery |
|---|---|---|---|
| 0 | — | META + 13 children created and wired | proceed to R1 |
| 1 | `MetaAlreadyExists` | A META for `vX.Y.Z` already exists | inspect `OP-...`; if it was Archived, rerun with `--force` |
| 2 | `TemplateYAMLInvalid` | Schema validation failed | fix `config/release_template.yaml`; rerun `--dry-run` |
| 3 | `BlockedByWiringFailed` | Tickets created but link wiring crashed | follow §6 (Rollback) |
| — | `LabelConflictBothSet` | `release:skip-auto-conductor` and `release:force-create` are both on the fixVersion | remove one label; the next cron run will re-evaluate |
| — | `LabelInvalid` | The fixVersion has an unknown or malformed release-conductor label | correct the label; the next cron run will re-evaluate |

## 6. Rollback

If the script aborts after ticket creation but before completing the
link wiring, it writes a rollback file:

```text
/tmp/release-rollback-vX.Y.Z.json
```

The file contains every ticket key created during the run. Two
supported recovery paths:

### 6a. Operator deletes by hand

```bash
jq -r '.ticket_keys | to_entries[] | .value' \
    /tmp/release-rollback-vX.Y.Z.json | while read key; do
  echo "Deleting $key"
  # Open https://soraapp.atlassian.net/browse/$key and use
  # the JIRA UI to delete (we deliberately do NOT auto-delete).
done
```

### 6b. Cleanup script (G2, planned)

`scripts/release_rollback.py` (not yet shipped) will consume the
JSON and call DELETE for each key. Track it under the G2 follow-up.

After cleanup, re-run `scripts/instantiate_release_meta.py
--version vX.Y.Z` from scratch.

## 7. `--force` semantics

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

## 8. Template versioning

`config/release_template.yaml` carries a top-level `version: "v1"`.
Bump it whenever:

- The child count changes (today: exactly 13).
- A child's `r_id`, area mix, tier, or class field changes.
- The `blockedBy` chain topology changes (e.g., adding a parallel
  branch).

The script records the template version on the META so retrospectives
can correlate cycle-time data against a specific template revision.

## 9. Drift guards

| Guard | Purpose |
|---|---|
| `backend/tests/test_release_template_engine.py::test_child_label_drift_detector_matches_template` | Pins the canonical label set on each child |
| `backend/tests/test_release_template_engine.py::test_default_template_has_exactly_thirteen_children` | Pins the child count |
| `backend/tests/test_release_template_engine.py::test_default_template_blocked_by_chain_is_sequential` | Pins the chain topology |
| `scripts/check_issuelink_post_callers.py` | Prevents new raw `POST /issueLink` callers that could invert the direction (L-OP-874) |

If any of these guards fail after a template edit, halt and reconcile
before shipping the edit.

## 10. After the META lands

The operator's daily loop on a live release:

1. Watch the runner pick up `R1`, run it to Published.
2. The gate releases `R2`; runner picks it up; loop.
3. After `R13` reaches Published, the META auto-transitions to
   Published per `docs/sop/jira-ticket-conventions.md` §10. (If it
   doesn't, file a BLOCKER ticket — that's a workflow bug, not a
   release-conductor bug.)
4. The retrospective (per R13 AC) lands under
   `docs/retrospectives/<date>-vX.Y.Z.md` and the release closes.

## 10. Transition Notifications

Every release child transition into Published / `公開済み` is matched by
the JIRA/SSE event handler and passed to
`backend.agents.release_notifications`. The helper renders a payload
with the release version, child name, and META link, then reuses the
OP-721 notification bridge for immediate Slack + email fan-out.

Routing lives in `config/release_notification_routing.yaml`:

- `prod` releases route to `#releases-prod` and
  `releases@sora.services`.
- `rc` releases route to `#releases-rc` and
  `releases@sora.services`.
- If the routing file is missing or unreadable, the handler logs
  `RoutingConfigMissing` and falls back to `#omnisight-releases`.

Bridge failures are fire-and-forget by design. The handler logs
`NotificationBridgeDown`, records `notification.outcome=bridge_down`
in the release event result, and does not block the state transition.

Operational check for R8: after the approval child reaches
`公開済み`, confirm a Slack message appears in the configured release
channel and includes the `RELEASE-vX.Y.Z` version, the `R8` child
summary, and the META ticket URL.

## 11. Audit DB connectivity smoke test (D5 / OP-964 AUDIT-16)

The D5 develop→main auto-promote
(`scripts/auto_promote_develop_to_main.sh` →
`backend.agents.auto_promote_main`) writes exactly one `release_audit`
row per run — the durable forensic trail for "did `main` move, and
why". The audit sink connects through `backend.audit`, i.e. via
`OMNISIGHT_DATABASE_URL`. A systemd unit does **not** inherit the login
shell environment, so the cron reads the DSN from
`/home/user/.config/omnisight/release-audit.env`
(`EnvironmentFile=-` in `deploy/systemd/auto-promote-develop.service`).
If that file is absent or the DSN is wrong, the sink silently falls
back to the local SQLite default and the pg-primary `release_audit`
table never gets the row — the OP-925 R3 failure mode.

Run this smoke test on the runner host after provisioning, after
rotating the Postgres credentials, and as the first triage step when an
R3 attempt reports a missing audit row:

```bash
# 1) psql present? scripts/setup-dev-env.sh installs `postgresql-client`.
which psql || sudo apt-get install -y postgresql-client

# 2) DSN configured for the cron?
test -f /home/user/.config/omnisight/release-audit.env \
  && grep -q '^OMNISIGHT_DATABASE_URL=' /home/user/.config/omnisight/release-audit.env \
  || echo "MISSING: create release-audit.env with OMNISIGHT_DATABASE_URL=postgresql+asyncpg://..."

# 3) Reachable + table exists? (load the same env the cron uses)
set -a; . /home/user/.config/omnisight/release-audit.env; set +a
# psql wants the libpq URL form, not the SQLAlchemy +asyncpg form:
PSQL_URL="${OMNISIGHT_DATABASE_URL/+asyncpg/}"
psql "$PSQL_URL" -c 'SELECT 1'
psql "$PSQL_URL" -tAc \
  "SELECT outcome, ts FROM release_audit ORDER BY ts DESC LIMIT 1"

# 4) Async-driver path the cron actually uses:
python3 -c "import asyncpg, asyncio, os; \
  asyncio.run(asyncpg.connect(os.environ['OMNISIGHT_DATABASE_URL'].replace('+asyncpg','')))" \
  && echo "asyncpg OK"
```

Expected: step 3 prints `1` and the most-recent `release_audit` row;
step 4 prints `asyncpg OK`. Failure modes and fixes:

| Symptom | Cause | Fix |
|---|---|---|
| `psql: command not found` | `postgresql-client` not installed | `sudo apt-get install -y postgresql-client` (now in `scripts/setup-dev-env.sh`) |
| `MISSING: create release-audit.env …` | cron has no DSN → writes to local SQLite | create `/home/user/.config/omnisight/release-audit.env` (mode 0600) with `OMNISIGHT_DATABASE_URL=postgresql+asyncpg://…@pg-primary:5432/omnisight` |
| `could not connect to server` / asyncpg timeout | wrong host/port/creds, or pg-primary unreachable from the runner net | verify the DSN against `git_accounts` / the pgvector primary; check firewall between runner host and pg-primary |
| `relation "release_audit" does not exist` | alembic not applied on the target DB | run `alembic upgrade head` (migration `0207_release_audit`) against that DB |

After a green smoke test, re-run the D5 step (`systemctl --user start
auto-promote-develop.service` or `bash scripts/auto_promote_develop_to_main.sh`)
and confirm a new `release_audit` row whose `outcome` reflects the run
result — see the `release_audit_outcome_chk` enum in
`backend/alembic/versions/0207_release_audit.py`
(`promoted` / `noop` / `milestone_not_accepted` / `ff_not_possible` /
`push_rejected`).

## 12. References

- `scripts/instantiate_release_meta.py` (this script)
- `config/release_template.yaml` (per-child template)
- `config/release_notification_routing.yaml` (Slack/email routing)
- `config/release_meta_description.md.template` (META description template)
- `docs/sop/lessons/L-OP-874-jira-blockedby-direction-trap.md`
- `docs/operations/release-cut-runbook.md` (R1 implementation reference)
- `docs/operations/release-runbook.md` (R6-R10 implementation reference)
- `docs/operations/release-notes-runbook.md` (R11 implementation reference)
- Memory: `reference_release_workflow.md` (META-as-state-machine summary)
