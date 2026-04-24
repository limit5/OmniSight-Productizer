# JIRA Inbound Automation — Operator Guide

> Y-prep.3 (#289) — operator-facing reference for the JIRA → OmniSight
> automation pipeline that landed in `backend/jira_event_router.py` +
> `backend/routers/webhooks.py::_on_jira_event`.

OmniSight subscribes to a JIRA Cloud (or Server / Data Center) webhook and
turns three inbound event shapes into three concrete automation actions.
This page is the single reference an operator needs to:

1. understand **which** JIRA event triggers **what** OmniSight side-effect,
2. paste a **minimal payload example** when reproducing or hand-crafting
   curl traffic, and
3. configure JIRA's webhook **filter / event selector** so the JIRA side
   only sends the three events we actually consume — saving outbound
   bandwidth and shielding the dispatcher from noise (every JIRA project
   change pushes ~12 webhook event types by default; we route 3, drop the
   rest as `unhandled`).

---

## 1. Endpoint + auth

| Field | Value |
|---|---|
| **Method** | `POST` |
| **URL** | `https://<your-host>/api/v1/webhooks/jira` |
| **Auth header** | `Authorization: Bearer <jira_webhook_secret>` |
| **Content-Type** | `application/json` |

The shared secret is stored in `Settings.jira_webhook_secret` and rotated
through the **Notifications → JIRA → Rotate Webhook Secret** UI (one-time
view; mirrored across workers via Redis SharedKV — Y-prep.2 contract).
The webhook is **rejected with 401** on a digest mismatch and **503** if
no secret has ever been configured. Rotation is hot — no backend restart
needed.

The dispatcher fires for **every** authenticated event regardless of
whether an internal `Task` row matches the issue key. The legacy
status-sync path (which DOES require a matching Task) runs independently
in the same request.

---

## 2. The three triggers

Each trigger maps `webhookEvent` (top-level string in the payload) →
handler in `backend/jira_event_router.py::ROUTES` → automation action.

### Trigger 1 — `comment_created` (or `comment_updated`) → emit `jira_command`

**Fires when:** a JIRA comment body starts with `<prefix><word>`
(default prefix `/`, configurable via `OMNISIGHT_JIRA_COMMAND_PREFIX`).

**Action:** publishes a `jira_command` event on the global event bus
(`{issue_key, command, args, author, comment_id}`). The O5 IntentSource
(and any future CATC consumer subscribed to the `jira_command` topic) is
expected to spawn an agent.

**Audit row:** `action='jira.command_received'`,
`entity_kind='jira_event'`, `entity_id=<ISSUE-KEY>`,
`after.command=<command>`, `after.args=<first 200 chars of args>`,
`after.author=<displayName or name>`, `after.tenant_id=<live tenant>`.

`comment_updated` routes to the same handler so an operator's edited
`/command` is re-evaluated. Comments without the prefix (or bare `/`,
or empty body) return `{status: ignored, reason: no_command_prefix}`
and are silently dropped — no audit row, no bus publish.

#### Minimal payload — POSITIVE

```json
{
  "webhookEvent": "comment_created",
  "issue": { "key": "OPS-42" },
  "comment": {
    "id": "10100",
    "body": "/regen-tarball release-2026-q2",
    "author": { "displayName": "Alice Liu", "name": "alice" }
  }
}
```

→ bus publishes `jira_command` with
`{"issue_key":"OPS-42","command":"regen-tarball","args":"release-2026-q2",
"author":"Alice Liu","comment_id":"10100"}`.

#### Minimal payload — NEGATIVE (no prefix → silently dropped)

```json
{
  "webhookEvent": "comment_created",
  "issue": { "key": "OPS-42" },
  "comment": {
    "id": "10101",
    "body": "Looks good to me, merging now.",
    "author": { "displayName": "Alice Liu" }
  }
}
```

→ handler returns `{"status":"ignored","reason":"no_command_prefix"}`.

---

### Trigger 2 — `jira:issue_updated` (status → `Done` / `Closed`) → artifact packaging

**Fires when:** the `changelog.items[]` contains an entry with
`field == "status"` whose `toString` is in the **done-statuses
whitelist** (default `Done,Closed`; configurable via
`Settings.jira_done_statuses` CSV — see §4).

**Action:** spawns `backend.routers.webhooks._package_merged_artifacts(
"jira:<ISSUE-KEY>", <summary>)` via `asyncio.create_task` — the same
release-tarball pipeline that the Gerrit `change-merged` hook uses.
Fire-and-forget; failures are logged and don't block the webhook
response.

**Audit row:** `action='jira.status_transitioned'`,
`before.status=<from_status>`, `after.status=<to_status>`,
`after.artifact_packaging='spawned'`, `after.tenant_id=<live tenant>`.

A status transition into a non-whitelisted status (e.g. `In Review`)
returns `{status: ignored, reason: status_not_whitelisted, from, to}`;
no packaging, no audit row.

#### Minimal payload — POSITIVE

```json
{
  "webhookEvent": "jira:issue_updated",
  "issue": {
    "key": "OPS-42",
    "fields": { "summary": "Ship 0.2.0 release tarball" }
  },
  "changelog": {
    "items": [
      { "field": "status", "fromString": "In Progress", "toString": "Done" }
    ]
  }
}
```

→ spawns `_package_merged_artifacts("jira:OPS-42", "Ship 0.2.0 release tarball")`.

#### Minimal payload — NEGATIVE (non-whitelisted status)

```json
{
  "webhookEvent": "jira:issue_updated",
  "issue": { "key": "OPS-42", "fields": { "summary": "Ship 0.2.0" } },
  "changelog": {
    "items": [
      { "field": "status", "fromString": "In Progress", "toString": "In Review" }
    ]
  }
}
```

→ handler returns
`{"status":"ignored","reason":"status_not_whitelisted","from":"In Progress","to":"In Review"}`.

---

### Trigger 3 — `jira:issue_created` (intake label) → CATC intake

**Fires when:** the new issue carries a label matching the configured
**intake label** (default `omnisight-intake`; configurable via
`Settings.jira_intake_label` — see §4).

**Action:** calls `backend.intent_bridge.on_intake_queued(
parent=<ISSUE-KEY>, vendor="jira", cards_with_task_ids=[],
dag_id="jira-intake:<ISSUE-KEY>")`. The bridge flips the parent ticket
to `in_progress` and a follow-up orchestrator run can attach sub-tasks.

**Audit row:** `action='jira.intake_triggered'`,
`before.labels=<full label list>`, `after.intake_label=<matched label>`,
`after.vendor='jira'`, `after.tenant_id=<live tenant>`.

An issue without the intake label returns
`{status: ignored, reason: missing_intake_label, labels}`; no bridge
call, no audit row.

#### Minimal payload — POSITIVE

```json
{
  "webhookEvent": "jira:issue_created",
  "issue": {
    "key": "OPS-501",
    "fields": {
      "summary": "Investigate p1 latency spike on /api/v1/dashboard",
      "labels": ["omnisight-intake", "p1", "observability"]
    }
  }
}
```

→ calls `intent_bridge.on_intake_queued(parent="OPS-501", vendor="jira",
cards_with_task_ids=[], dag_id="jira-intake:OPS-501")`.

#### Minimal payload — NEGATIVE (missing label)

```json
{
  "webhookEvent": "jira:issue_created",
  "issue": {
    "key": "OPS-502",
    "fields": {
      "summary": "Tweak login button copy",
      "labels": ["ui", "p3"]
    }
  }
}
```

→ handler returns
`{"status":"ignored","reason":"missing_intake_label","labels":["ui","p3"]}`.

---

## 3. Tenant context

Inbound webhooks have no user session, so the dispatcher explicitly
scopes the request to the `t-default` tenant via
`db_context.set_tenant_id("t-default")` (with `prior_tenant` capture +
`finally`-restore for reentrancy safety). Audit rows land on
`audit_log.tenant_id = 't-default'`, the actor field reads
`jira_event_router/t-default`, and `after_json::jsonb ->> 'tenant_id'`
self-documents the same value.

This is the single Y4 swap-point: when per-tenant JIRA instances land,
that one line becomes
`set_tenant_id(derive_tenant_from_event(event))` and the rest of the
audit / bus / handler chain inherits the live tenant automatically.

---

## 4. Configuring the routing knobs

Three behaviours are tunable. The two label / status knobs flow through
the **Notifications tab** UI and are mirrored across workers via Redis
SharedKV (Y-prep.3 checkbox 4); the command-prefix knob is
deploy-time-only.

| Knob | Settings field | Env var | UI? | Default |
|---|---|---|---|---|
| Intake label | `jira_intake_label` | `OMNISIGHT_JIRA_INTAKE_LABEL` | Notifications → JIRA | `omnisight-intake` |
| Done-statuses (CSV) | `jira_done_statuses` | `OMNISIGHT_JIRA_DONE_STATUSES` | Notifications → JIRA | `Done,Closed` |
| Comment prefix | — | `OMNISIGHT_JIRA_COMMAND_PREFIX` | none | `/` |

Resolution order is **`settings.<field>` → `OMNISIGHT_*` env → built-in
default** (first non-empty wins). A Notifications-tab edit on worker-A
is picked up by workers B/C/D on the next inbound webhook (each
`jira_webhook` request runs `_overlay_runtime_settings()` at the top —
single Redis HGETALL).

To **disable** intake routing entirely, set `jira_intake_label` to a
sentinel that no real label uses (e.g. `__disabled__`).

To **expand** the done-statuses whitelist (e.g. add `Released`):
`jira_done_statuses = "Done,Closed,Released"`.

---

## 5. Configuring the JIRA-side webhook filter (bandwidth + noise)

JIRA's default webhook config sends **every** project event to the
endpoint. We only handle three event kinds — the rest are routed to
`{status: unhandled}` and dropped. Filter at the JIRA side to:

- cut outbound JIRA → OmniSight bandwidth (no payload sent at all for
  ignored events),
- shrink the dispatcher's audit-log noise floor,
- shield against accidental triggering by unrelated workflow changes.

### A. JIRA Cloud — System WebHooks UI

1. **Settings (gear icon) → System → WebHooks → Create a WebHook**.
2. **Name**: `omnisight-automation`.
3. **Status**: Enabled.
4. **URL**: `https://<your-host>/api/v1/webhooks/jira`.
5. **Description** (optional): `Y-prep.3 #289 — comment / status / intake routing`.
6. **JQL filter (issue scope)** — restrict which issues fire the webhook.
   This is the single biggest bandwidth win:

   ```jql
   project in (OPS, INFRA, AGENT)
     AND (
       labels = "omnisight-intake"
       OR status changed TO ("Done", "Closed")
       OR issueFunction in commented("after startOfDay()")
     )
   ```

   Adjust `project in (...)` to your real project keys. The three
   `OR` clauses mirror the three handler triggers — JIRA evaluates the
   filter per event and skips the POST entirely on no-match.

7. **Events** — tick **only**:
   - **Issue → created** (drives Trigger 3)
   - **Issue → updated** (drives Trigger 2 — done-status transitions)
   - **Comment → created** (drives Trigger 1)
   - **Comment → edited** (drives Trigger 1 — re-evaluates edited `/command`)

   Leave **all other** event boxes UNTICKED (worklog, version,
   attachment, sprint, board, project, link, etc.). Each unticked box
   is one fewer event class JIRA pushes us, which is the no-code form
   of an event-allowlist firewall.

8. **Custom HTTP headers**:
   - `Authorization: Bearer <your jira_webhook_secret>`
   - `Content-Type: application/json` (usually default, set if blank).

9. **Issue body** / **Comment body**: leave `Send the issue body in the
   webhook` and `Send the comment body in the webhook` ENABLED — both
   handler 1 (`comment.body`) and handler 2 (`changelog.items[]`)
   need them. Without them the dispatcher sees empty fields and
   returns `ignored / no_command_prefix` or `ignored / no_status_change`.

### B. JIRA Server / Data Center

The same UI lives at **Administration → System → Advanced →
WebHooks** (path varies by version). Same JQL + event-tick recipe
applies. SDC additionally lets you tick `comment_updated` directly
(Cloud rolls it into "Comment → edited"); both map to the same
OmniSight handler.

### C. Verify the filter is working

Fire a test event from the JIRA UI (transition a sample ticket into
`Done`) and inspect the audit log:

```sql
SELECT id, action, entity_id, after_json::jsonb -> 'tenant_id'
  FROM audit_log
 WHERE action LIKE 'jira.%'
 ORDER BY id DESC
 LIMIT 10;
```

A correctly-filtered webhook produces exactly **one** `jira.*` audit
row per real trigger; if the filter is too loose, you'll see a tail of
`unhandled`-shaped POSTs in the backend log without a matching audit
row — that's the symptom of "events ticked but no handler".

---

## 6. Curl recipe for local smoke

To exercise the dispatcher end-to-end against a running OmniSight stack
(replace `<host>` and `<secret>`):

```bash
curl -sS -X POST "https://<host>/api/v1/webhooks/jira" \
  -H "Authorization: Bearer <secret>" \
  -H "Content-Type: application/json" \
  -d '{
    "webhookEvent": "comment_created",
    "issue": {"key": "OPS-SMOKE"},
    "comment": {
      "id": "1",
      "body": "/ping smoke-test",
      "author": {"displayName": "smoke-bot"}
    }
  }'
```

Expected 200 response: `{"status":"ok","message":"No matching task"}`
(the legacy status-sync path returns this when no internal `Task` row
matches the issue key — the dispatcher path still fires and writes its
audit row independently).

Then verify:

```sql
SELECT actor, action, after_json
  FROM audit_log
 WHERE action = 'jira.command_received'
   AND entity_id = 'OPS-SMOKE'
 ORDER BY id DESC LIMIT 1;
```

Should show
`actor=jira_event_router/t-default`,
`action=jira.command_received`,
`after_json` containing `command=ping`, `args=smoke-test`,
`author=smoke-bot`, `tenant_id=t-default`.

---

## 7. Troubleshooting matrix

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Invalid token` | Webhook secret mismatch | Re-rotate via Notifications tab; copy fresh value into JIRA webhook header |
| `503 Jira webhooks not configured` | `jira_webhook_secret` empty | Run the Notifications → JIRA → Rotate flow once |
| Webhook lands but no audit row | JIRA `Issue body` / `Comment body` toggle disabled | Re-enable both in JIRA webhook config |
| Comment with `/cmd` produces no audit | Body has invisible leading whitespace OR mismatched prefix | Use `OMNISIGHT_JIRA_COMMAND_PREFIX` if you need a non-`/` prefix |
| Status → `Done` produces no audit | `jira_done_statuses` overridden to a different CSV | Check Notifications → JIRA → Done Statuses; default is `Done,Closed` |
| Intake label set but no audit | Label spelling drift (case-sensitive) | JIRA labels are case-sensitive — `Omnisight-intake` ≠ `omnisight-intake` |
| Audit row arrives but action looks wrong | Multiple JIRA webhook entries pointing at the same URL | Consolidate to a single webhook in JIRA admin to avoid duplicate POSTs |

---

## 8. Reference

- Dispatcher: `backend/routers/webhooks.py::_on_jira_event` (around line 761)
- Handlers: `backend/jira_event_router.py` (`ROUTES` table, lines ~414)
- Tests:
  - `backend/tests/test_jira_event_router_handlers.py` — 6 dispatch-shape contract tests
  - `backend/tests/test_jira_event_router_tenant_audit.py` — 6 tenant + audit chain tests
  - `backend/tests/test_webhooks.py` — 31 Gerrit/JIRA dispatcher contract tests
- Settings UI: Notifications tab → JIRA section
  (`components/omnisight/integration-settings.tsx`)
