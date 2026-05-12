# Event-Driven Release Runbook

> **Ticket**: OP-954 (H9 — Sprint H close-out)
>
> **Audience**: release operator deciding whether to run the release
> conductor in L1, L2, or L3 mode and recovering when L3 events degrade.
>
> **Status**: operator handoff for ADR-0018. Use with
> `docs/operations/release-conductor-runbook.md`; this file does not replace
> the G1 release META creation procedure.

## 0. Mental model — events wake the graph

The release conductor still starts with the G1 model: one `RELEASE-vX.Y.Z`
META plus child tickets linked by `blockedBy`. JIRA remains the source of
truth for release order and eligibility.

L3 adds wakeups:

- Gerrit and JIRA webhooks tell the worker that a graph edge may now be
  eligible.
- internal canary and SLO events update the release state machine.
- the durable queue and worker classify each event as `done`, `failed`, or
  dead-letter.
- G3 cron remains the fallback consistency path when a webhook is missed.

Do not debug L3 as a standalone conductor. Debug it as a scheduler over the
JIRA graph.

## 1. Mode choice

| Mode | Use when | Operator expectation |
| --- | --- | --- |
| L1 — manual graph | New release class, incident response, unknown L3 health, or first recovery after failed cutover. | Operator watches JIRA children and performs release actions by hand. No reliance on event wakeups. |
| L2 — poll-driven conductor | Normal release when webhooks are unavailable, stale, or intentionally disabled. | G1/G3 graph and cron advance at polling cadence. More latency, fewer moving parts. |
| L3 — event-driven conductor | Standard release after ADR-0018 acceptance, H8 GO verdict, and healthy webhook sources. | Events should wake eligible work within seconds while preserving G3 fallback. |

Default to L2 if the mode is ambiguous. L3 is a latency optimization over a
working graph; it should not be required to keep a release safe.

## 2. Pre-flight for L3

Before choosing L3 for a release, verify:

1. The release META exists and the child chain is wired by the G1 runbook.
2. ADR-0018 is accepted and no open blocker overrides the H8 GO verdict.
3. The previous release META is not live unless the operator intentionally
   runs a hotfix path.
4. Webhook source health is current:
   - Gerrit source not stale beyond 5 minutes.
   - JIRA source not stale beyond 2 minutes.
   - internal canary/SLO publishers are running in the same backend process.
5. The durable event worker is processing rows and the dead-letter count is
   zero or explained.
6. The operator approval UI is reachable.
7. Notification/dashboard surfaces are displaying read-only state that
   agrees with the JIRA graph.

If any item fails, use L2 until the source is recovered or deliberately
waived.

## 3. Operating L3

1. Instantiate or identify the release META using
   `docs/operations/release-conductor-runbook.md`.
2. Confirm the first child is eligible in JIRA.
3. Start the release as usual; the runner and release tooling still operate
   against JIRA.
4. Watch the event queue, release state, and operator dashboard together.
5. When approval is requested, use the H4 approval UI/API. Do not advance by
   editing JIRA status unless L3 is degraded and the operator has switched
   to L1/L2.
6. After each stage, confirm the dashboard's current stage matches the JIRA
   child status.
7. If a webhook source goes stale, switch to L2 and let the G3 cron path
   re-derive state from JIRA/Gerrit.

The operator should not manually replay individual webhook payloads during a
normal release. Replays are recovery actions and must cite the event id.

## 4. L1 manual graph path

Use L1 when automation is untrusted or unavailable.

Checklist:

- Open the release META and child chain in JIRA.
- Verify the current blocker is `公開済み`.
- Move only the next eligible child forward.
- Record any operator override in a JIRA comment on the META.
- Do not depend on dashboard state if it disagrees with JIRA.
- File a follow-up if the manual path was needed because L3 produced an
  unexplained event result.

L1 is slower but safest during uncertainty because it removes event timing
from the release decision.

## 5. L2 poll-driven path

Use L2 when the graph is healthy but event wakeups are stale or noisy.

Checklist:

- Leave the release META and child chain intact.
- Disable or ignore L3 wakeups for the affected source.
- Let G3 cron and the runner pickup loop observe JIRA at the normal cadence.
- Keep notification and dashboard output read-only.
- Re-enable L3 only after source health is current and no dead-letter rows
  remain unexplained.

L2 is the supported fallback for `WebhookSourceUnreachable`. It trades
latency for fewer dependencies.

## 6. L3 event-driven path

Use L3 when pre-flight is green.

Healthy behavior:

- Gerrit/JIRA events persist into the queue once per immutable event id.
- Duplicate deliveries short-circuit through idempotency.
- Out-of-order events reconcile against the JIRA graph.
- canary stage events update release state.
- failed canary/SLO events halt forward progress rather than auto-rolling
  back unrelated stages.
- G3 cron can still re-derive the graph if an event is missed.

Residual caveats from H8:

- `operator.approval.{granted,aborted}` needs an explicit dispatch row before
  the worker can classify approval events without a stub.
- build, stage, and publish edges are not all fully event-driven yet; some
  latency remains polling-bound.

These caveats are not reasons to avoid L3, but they are reasons to keep the
JIRA graph and G3 fallback visible during the first production release.

## 7. Escalation paths

| Symptom | First action | Escalate if |
| --- | --- | --- |
| Gerrit source stale | Switch to L2; inspect webhook ingress and tunnel health. | stale > 5 minutes and G3 fallback also fails |
| JIRA source stale | Switch to L2; inspect JIRA webhook auth and recent changelog delivery. | stale > 2 minutes and child eligibility is ambiguous |
| Dead-letter row appears | Pause L3 for that release; inspect `(source, event_type, event_id)` and handler result. | event type is known or repeats after one replay |
| Approval click records decision but no worker outcome | Continue only if state machine recorded the decision; file the operator-event dispatch follow-up. | dashboard or runner gate disagrees with state machine |
| Dashboard disagrees with JIRA | Trust JIRA; treat dashboard as stale read model. | mismatch persists after one poll interval |
| SLO breach or canary rollback event | Halt forward progress and follow the canary/SLO rollback runbook. | release remains eligible in JIRA despite the halt signal |

If the release safety state is unclear, stop at L1. Release correctness
outranks event-driven latency.

## 8. Recovery and rollback

### Missed webhook

1. Confirm the current JIRA state.
2. Switch to L2.
3. Let G3 cron re-derive the missing conclusion.
4. Record the source and event window in the release retrospective.

### Duplicate event

1. Confirm the duplicate has the same `(source, event_id)`.
2. Verify the cached handler result was returned.
3. No operator action is needed unless a duplicate notification or comment
   escaped idempotency.

### Out-of-order event

1. Read the current JIRA child status.
2. Trust JIRA over event arrival order.
3. If the handler marked the row `done` with a no-op classification, proceed.
4. If it dead-lettered, pause L3 and inspect the handler.

### Load-test regression

If a future H8-style rerun sees event loss, dead-letter growth, or p95
latency above the coordination budget, apply ADR-0018's
`LoadTestVerdictReject` path:

- keep G3 cron as the supported conductor,
- do not cut over L3 for production release control,
- file follow-up work with the failed case and event ids,
- rerun the load test before reopening the cutover decision.

## 9. H8 dry-run evidence

The operator dry-run basis for this handoff is
`docs/research/h8-l3-load-test-2026-05.md`:

| Dry-run case | Result |
| --- | --- |
| Synthetic happy run | `v0.99-h-rc1` reached `done` with 1 operator click and 0 JIRA touches. |
| Five-release burst | 42 events across five releases reached `done`; 0 failed/dead-letter rows. |
| Latency gate | p95 event-to-handler latency was 10.908 ms against a 1000 ms target. |

Operator decision: **GO for L3 cutover with the caveats in §6**.

## 10. First production L3 retrospective checklist

After the first live release that uses L3, record:

- release META key and version,
- L1/L2/L3 mode changes,
- webhook stale windows by source,
- dead-letter row count and event ids,
- operator approval count,
- manual JIRA touch count,
- whether dashboard state matched JIRA,
- whether G3 fallback was invoked,
- whether the release reached `公開済み`,
- follow-up tickets for any caveat that affected the operator.

This evidence belongs in the RELEASE retrospective, not in the Sprint H
retrospective.
