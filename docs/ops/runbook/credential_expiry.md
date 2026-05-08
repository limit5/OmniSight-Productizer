# `credential_expiry`

| field | value |
|-------|-------|
| Severity | `WARN` (30-day band) → `DEGRADED` (7-day) → `CRITICAL` (1-day or already expired) |
| Source | T6 — `scripts/credential_expiry_check.py` |
| Tier owner | the credential's `owner` (per `configs/credentials.yaml`) |
| Parent META | OP-721 (closes the silent-credential-expiry gap surfaced when the Gerrit HTTP password expired between sessions) |

## What triggers it

`scripts/credential_expiry_check.py` runs daily at 09:00 from the
`omnisight-credential-expiry-check.timer` systemd unit. It reads
the inventory at `configs/credentials.yaml` (schema mirrored in
`configs/credentials.example.yaml`), computes
`days_until_expiry = expiry_date - today` for every entry, and
classifies into bands:

* `days_until_expiry > 30` → no alert.
* `7 < days_until_expiry ≤ 30` → `WARN` (band: warning).
* `1 < days_until_expiry ≤ 7` → `DEGRADED` (band: urgent).
* `0 < days_until_expiry ≤ 1` → `CRITICAL` (band: critical).
* `days_until_expiry ≤ 0` → `CRITICAL` (band: expired).
* `expiry: 'never'` → silently skipped (e.g. ed25519 SSH keys
  with no policy expiry).

The bands are **inclusive at the upper edge** so setting expiry =
today + 30d fires today's run (boundary case). The script
distinguishes *expired* from *critical-band* in its JSON output;
both share severity `CRITICAL` so the operator response is the
same.

The same `code=credential_expiry` is used across all bands —
**severity** differentiates urgency. T1's dedup key is
`(code, severity)` so each band has its own dedup window;
escalations across bands (e.g. 7→1 day) deliver a fresh
notification.

## Severity rationale

| Band | Severity | Why |
|------|----------|-----|
| 30-day | `WARN` | Plenty of runway; emails/JIRA only, no Slack/LINE noise |
| 7-day | `DEGRADED` | Adds email; should be in the on-call queue |
| 1-day | `CRITICAL` | Pages everywhere; outage imminent |
| Expired | `CRITICAL` | Outage already happening for some pipeline |

The whole point of T6 is to never let a credential silently
expire — the OP-689 family included exactly that failure (Gerrit
HTTP password expired, runner pushes broke, no one noticed).

## Immediate action

The action depends on the credential `type` field carried in the
alert context. Cross-reference
`docs/operations/credential_rotation_runbook.md` for the
per-type rotation steps; the summary:

| `type` | Rotation owner | Reference |
|--------|----------------|-----------|
| `jira_token` | bridge-maintainer | `docs/operations/credential_rotation_runbook.md` §jira_token; memory `reference_jira_atlassian.md` |
| `gerrit_http` | gerrit-admin | `docs/operations/credential_rotation_runbook.md` §gerrit_http |
| `ssh_key` | the key's `owner` | `docs/operations/credential_rotation_runbook.md` §ssh_key |
| `api_key` | service owner | `docs/operations/credential_rotation_runbook.md` §api_key |
| `encryption_key` | security-lead | `docs/operations/credential_rotation_runbook.md` §encryption_key |

Generic flow regardless of type:

1. **Read the alert context.** It includes `id`, `type`,
   `expiry`, `owner`, and the per-type `runbook` link.
2. **Rotate the credential** via the per-type runbook.
3. **Update the inventory** — set `last_rotated_at: <today>` and
   `expiry: <new_date>` in `configs/credentials.yaml`. Commit;
   the change goes through Gerrit Code Review like everything
   else.
4. **Re-run the check** to confirm the alert clears:

       python scripts/credential_expiry_check.py --format json \
           | jq '.alerts | map(select(.id == "<the-id>")) | length'

   Expect `0`.

## Root-cause investigation

`credential_expiry` is **not a failure** — it's the system doing
its job. There is no root cause to investigate. The relevant
failure mode is "the alert reaches the operator but rotation
doesn't happen in time":

* Was the 30-day warning ignored? Check `acknowledged_at` /
  on-call log entries for the `WARN`-band emission.
* Did the inventory miss this credential entirely, surfacing as a
  surprise expiry? File a META ticket — every operator credential
  must have an inventory entry per OP-727 AC.
* Did `expiry: never` hide a credential that *does* have a
  rotation policy? Re-classify the inventory entry; `'never'` is
  for true policy-exempt keys only.

## Escalation

* `WARN` band (30-day): rotate within the month. Don't escalate.
* `DEGRADED` band (7-day): schedule rotation this week. Notify
  on-call.
* `CRITICAL` band (1-day or expired): rotate **today**. Page the
  credential `owner`; if no response within 2h, escalate to
  on-call lead.
* Expired credential causing visible outage (e.g.
  [`gerrit_client_ssh_failed`](gerrit_client_ssh_failed.md) on the
  same key): treat both alerts as one incident; rotate, verify the
  pipeline recovers, postmortem.
