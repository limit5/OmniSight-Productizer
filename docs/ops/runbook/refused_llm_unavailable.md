# `refused_llm_unavailable`

| field | value |
|-------|-------|
| Severity | `CRITICAL` (per-code override in `configs/error_pager.yaml`) |
| Source | T3 forwarder — `scripts/journal_error_forwarder.py` (record originates in bridge LLM gateway) |
| Tier owner | bridge-maintainer |
| Parent META | OP-721 |

## What triggers it

The bridge logs `event="refused_llm_unavailable"` at level `ERROR`
when it deliberately **refuses** to call the LLM because the
backing service (Anthropic / OpenAI / local Ollama) is unreachable
or returning non-recoverable status. The bridge prefers refusing to
falling back silently to a different model — silent fallback was the
2026-04-24 prod hang root cause (see lessons-learned L-2026-04-24).

T3 promotes this to `CRITICAL` because:

* the bridge's downstream pipeline is **paused** as long as the
  refusal stands;
* there is no in-process retry that will fix it — the LLM provider
  is the outage;
* every minute of refusal accumulates a backlog of un-reviewed
  changes / un-classified events.

## Severity rationale

`CRITICAL` triggers JIRA + email + Slack + LINE fanout and the
`OMNISIGHT_NOTIFIER_CRITICAL_REPAGE_SECONDS` re-page loop. Operator
response window: ≤15 min. Beyond that, the LLM-backed pipeline
visibly stalls (no new `proactive_merger_thread_spawned`,
`patchset_review_started`, etc.).

`P0` is reserved for "the bridge can't even refuse" (`unit_not_loaded`
class). `CRITICAL` covers the "bridge healthy, upstream sick" case.

## Immediate action

1. **Identify which provider failed.** The alert context carries
   `provider` and `last_error`. Common shapes:

   * `provider=anthropic`, `last_error=credit_low` →
     [Anthropic credit balance exhausted]. This was the 2026-04-24
     incident. **Top up the account immediately**, then bounce the
     LLM gateway:

         systemctl --user restart gerrit-jira-bridge

   * `provider=anthropic`, `last_error=overloaded_error` → wait;
     do **not** fall back to a different model. Repagent will
     retry on the next bridge tick.
   * `provider=ollama`, `last_error=connection refused` → local
     ollama is down. `systemctl --user restart ollama` then bridge.
   * `provider=openai`, `last_error=401` → API key rotated /
     revoked; rotate + restart per
     `docs/ops/llm_credentials.md`.

2. **Confirm recovery.** Either of:

   * The next `proactive_merger_thread_spawned` event in the bridge
     log; or
   * a manual `python -c 'from backend.agents.llm_gateway import
     ping; print(ping())'` returning `200 OK`.

   Both are equivalent — the manual ping is faster but doesn't
   exercise the bridge's queue.

3. **Acknowledge** the alert in the operator dashboard once the
   bridge resumes.

## Root-cause investigation

* Cross-check `docs/ops/llm_credentials.md` — has any credential
  rotated recently? T6 (`credential_expiry`) should have warned
  ≥7 days before expiry; if it didn't, file a follow-up against
  T6 inventory drift.
* Cross-check the provider's status page (Anthropic status,
  OpenAI status). Match the timestamp of the alert against
  reported incidents.
* For self-hosted Ollama, check
  `docs/ops/smoke-test-a2-2026-04-24.md` Finding #3 (this exact
  failure mode is documented end-to-end).

## Escalation

* Auto-resolves within 15 min (e.g. provider transient): close
  alert; no action needed beyond an entry in the on-call log.
* Persists ≥30 min: escalate to bridge-maintainer + LLM-credentials
  owner. If the provider is down for the long-haul, switch the
  bridge to a fallback **route** (different provider, same model
  class) per `docs/ops/llm_credentials.md` §fallback. **Do not**
  switch to a different *model class* without an ADR — silent
  capability degradation is what this code is here to prevent.
* Persists ≥2h: open a postmortem (template:
  `docs/retrospectives/YYYY-MM-DD-llm-outage.md`).
