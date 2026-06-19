# Staging activation + smoke: meeting intelligence (BI0-4)

How to turn on and smoke-test the meeting-intelligence stack (BI0 transcript
ingest + BI1-4 summary/translate/action-items/suggestions) on staging.

Companion to `docs/integration/conf-appliance-meeting-intelligence-wiring.md`
(the appliance↔backend contract) and `scripts/meeting_intelligence_smoke.py`
(the end-to-end smoke driver).

## Prerequisite: staging must run a build that contains BI0-4

BI0-4 landed on develop as OP-2238..2243. A staging image **older than those
merges has no `/meetings*` routes** — the smoke 404s at "meeting opened". Check
the running staging backend:

```sh
docker exec omnisight-staging-backend-a-1 python -c \
  "import backend.main as m; print(sorted({r.path for r in m.app.routes if 'meetings' in getattr(r,'path','')}))"
```

If that prints `[]`, deploy a candidate built from a develop SHA at/after
OP-2243 first (candidate build is operator/CI-only — `glab ci run -b develop
--variables-env CANDIDATE_SHA:<40hex>`; then the deploy-to-staging overlay
recipe in the staging-access notes). Re-check before continuing.

## 1. Enable the flags on staging

BI0 ingest has **no flag** (always on; tenant-scoped). Only BI1-4 are gated,
disabled by default. Add to `deploy/staging/.env`:

```
OMNISIGHT_MEETING_SUMMARY_ENABLED=1       # BI1
OMNISIGHT_MEETING_TRANSLATE_ENABLED=1     # BI2
OMNISIGHT_MEETING_ACTION_ITEMS_ENABLED=1  # BI3
OMNISIGHT_MEETING_SUGGESTIONS_ENABLED=1   # BI4
```

(`Settings` is strict `extra=forbid`; these are declared fields so they parse.)
BI1-4 call the house LLM adapter — staging must have a provider configured
(`OMNISIGHT_LLM_PROVIDER` + key, or ollama) or the endpoints return reachable
-but-empty (the smoke marks that WARN, not FAIL).

Restart staging to pick up the env:

```sh
systemctl --user restart omnisight-staging-compose.service
```

## 2. Provision an operator API key (the appliance's bearer)

The appliance posts with `Authorization: Bearer <token>`; the backend accepts an
operator-role API key (`backend/api_keys.py`). Mint one scoped to the meeting's
tenant and hand it to the appliance as `CONF_TRANSCRIPT_INGEST_TOKEN`, with
`CONF_TRANSCRIPT_INGEST_URL=https://<staging-host>/api/v1`.

## 3. Run the smoke

```sh
BI_SMOKE_BASE="https://<staging-host>/api/v1" \
BI_SMOKE_TOKEN="<operator API key>" \
python3 scripts/meeting_intelligence_smoke.py            # add --require-llm to fail on empty insights
```

The smoke: opens a meeting, ingests appliance-shaped segments (partial→final
supersede), replays for idempotency, reads finals back in order, then generates
all four insights. Exit 0 = required (wiring) checks green.

### Tiers

* **REQUIRED** — meeting open, ingest accept/supersede, idempotent replay,
  ordered read-back. These prove the appliance→backend path.
* **BEST-EFFORT** — the four LLM insights. Green with a provider; WARN (reachable
  but empty) without; `--require-llm` promotes WARN→FAIL once a provider is on.

## Verified locally (2026-06-19)

Ran the smoke against a throwaway PG + the develop-head backend (BI0-4) with the
flags on and no LLM provider: REQUIRED tier all green —
`meeting opened (201)`, `ingest accepted=3 superseded=1`, `replay deduped=4`,
`read-back ordered, 3 final segments`; the four insight endpoints reachable
(empty, no provider). On staging with a provider the insight tier goes green too.

## Rollback

Set the four flags back to `0` (or remove) and restart — BI1-4 return 404
"feature not enabled"; BI0 ingest keeps storing (harmless, tenant-scoped).
Keep retention short (BI0b `meetings.retention_until` + sweep) — transcripts are
speech-derived and sensitive.
```
