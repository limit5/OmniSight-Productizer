# OmniSight Operator Alert Runbook Index

This directory holds **one runbook page per alert `code`** emitted by
the META OP-721 silent-failure pipeline (T1 notifier + T2 watchdog +
T3 journal forwarder + T4 canary + T5 drift scanner + T6 credential
expiry tracker).

Every alert that reaches an operator carries a `code` field. This index
lists every such code in alphabetical order, with a one-line summary
and a link to the full per-code page.

## URL convention

Each runbook page lives at:

    docs/ops/runbook/<code>.md

When rendered, the canonical URL is:

    https://internal.docs/omnisight/ops/runbook/<code>

The T1 notifier (`backend/agents/operator_notifier.py`) injects this
URL into every outbound `Notification` so the body of every JIRA /
email / Slack / LINE alert contains a `Runbook: <url>` line under the
context block. Operators click through to land on the matching page
below — no guessing, no grep.

If the notifier produces an alert with a code that is **not** listed
here, the runbook drift linter (see [Drift detection](#drift-detection))
fails CI; that is the contract behind AC #3.

## How to read a runbook page

Every page in this directory follows the same five-section template:

1. **What triggers it** — exact emit-site (file:function), the
   condition that fires the alert, and which severity tier it carries.
2. **Severity rationale** — why it's `WARN` / `DEGRADED` / `CRITICAL`
   / `P0` and what the implied operator response time is.
3. **Immediate action** — first 5-minute response. Manual mitigation
   or rollback steps that are safe to run before root-cause is known.
4. **Root-cause investigation** — where to look next. Log paths,
   relevant scripts, dashboards, and the exact flag/state to inspect.
5. **Escalation** — who owns the failure mode (named role, not a
   person) and the threshold beyond which oncall hands off.

If the template ever changes, update **every** page in lockstep —
template drift is one of the issues the linter (below) catches.

## Drift detection

AC #3 ("index lists 100% of codes") is enforced by a future linter
job that:

* parses every `notify(...)` / `code=` literal in `backend/agents/`
  and `scripts/`,
* parses the alphabetical bullet list in this index,
* fails if the two sets differ.

The linter is tracked as a follow-up devops/tooling ticket because it
crosses out of the `docs` area boundary that scoped OP-728. Until it
lands, this index is the manual source of truth — when adding a new
alert code, you MUST add a row here and a corresponding `<code>.md`
page in the same change.

## Tier source map

Which T-tier emits which codes (for context — the per-page sections
below are the operator-facing copy):

| T  | Source | Codes emitted |
|----|--------|---------------|
| T1 | `backend/agents/operator_notifier.py` (OP-722) | _none_ — T1 is the dispatcher; codes come from T2-T6. T1's only own emission is the canary self-test code (`notifier:canary:<id>`), which is intentionally throwaway and unindexed. |
| T2 | `scripts/daemon_watchdog.py` (OP-723) | `daemon_silent`, `unit_not_loaded`, `notifier_dispatch_failed` |
| T3 | `scripts/journal_error_forwarder.py` (OP-724) | `audit_pool_not_initialised`, `audit_write_failed`, `gerrit_client_ssh_failed`, `refused_llm_unavailable`, `transient_5xx_retry_succeeded` (forwarded from journal records — full set is open by design but the curated five are the ones the T3 config explicitly allow-lists or promotes) |
| T4 | `scripts/canary_pipeline.py` (OP-725) | `canary_pipeline_check_failed` |
| T5 | `scripts/drift_scanner.py` (OP-726) | `bridge_drift`, `image_drift`, `main_branch_drift`, `refs_meta_config_drift`, `schema_drift` |
| T6 | `scripts/credential_expiry_check.py` (OP-727) | `credential_expiry` (severity differentiates the band: `WARN` = 30-day, `DEGRADED` = 7-day, `CRITICAL` = 1-day or expired) |

## Alphabetical code index

Every `code` value that may reach an operator. **Each entry has a 1-page
runbook** under `docs/ops/runbook/<code>.md`.

- [`audit_pool_not_initialised`](audit_pool_not_initialised.md) — boot-time
  audit-logger race during the first 60s of bridge startup; allow-listed
  by default, fires only if the suppression is removed.
- [`audit_write_failed`](audit_write_failed.md) — audit-trail write
  failed outside of the boot race; compliance-impacting.
- [`bridge_drift`](bridge_drift.md) — deployed gerrit-jira-bridge
  checkout has drifted from the expected SHA on the deploy host.
- [`canary_pipeline_check_failed`](canary_pipeline_check_failed.md) —
  hourly synthetic Gerrit push did not surface the expected
  `proactive_merger_thread_spawned` event in the bridge log.
- [`credential_expiry`](credential_expiry.md) — a tracked credential
  is within the 30 / 7 / 1-day expiry band (severity differentiates).
- [`daemon_silent`](daemon_silent.md) — a daemon's heartbeat event has
  not been observed within the configured silence threshold.
- [`gerrit_client_ssh_failed`](gerrit_client_ssh_failed.md) — bridge
  could not reach Gerrit over SSH; Track C pipeline halted.
- [`image_drift`](image_drift.md) — pinned container image SHA does
  not match what the deploy host actually ran.
- [`main_branch_drift`](main_branch_drift.md) — local checkout's
  `main` is out of sync with `origin/main` on the deploy host.
- [`notifier_dispatch_failed`](notifier_dispatch_failed.md) — T2
  watchdog could not call the T1 notifier import; alert fell back to
  the file sink only.
- [`refs_meta_config_drift`](refs_meta_config_drift.md) — Gerrit
  `refs/meta/config` has drifted from the committed sample.
- [`refused_llm_unavailable`](refused_llm_unavailable.md) — bridge
  refused to call the LLM because the backing service was unreachable;
  silently degrades correctness if not paged.
- [`schema_drift`](schema_drift.md) — DB `alembic_version` head does
  not match the alembic head committed to the repo.
- [`transient_5xx_retry_succeeded`](transient_5xx_retry_succeeded.md) —
  observability-only marker on a successful retry path; allow-listed
  by default, fires only if the suppression is removed.
- [`unit_not_loaded`](unit_not_loaded.md) — `systemctl is-enabled`
  failed for a tracked daemon; the service was never installed or has
  been disabled.

---

**Coverage check (manual until the linter lands):** 15 codes above ⇔
T2 (3) + T3 (5 curated) + T4 (1) + T5 (5) + T6 (1) = 15. ✅

**META ticket:** OP-721. **This page:** OP-728 (T7).
