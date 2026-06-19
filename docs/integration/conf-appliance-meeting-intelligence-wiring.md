# Wiring: conference appliance → backend meeting intelligence

End-to-end path that turns on-board ASR into post-meeting intelligence. The
two ends were built separately (appliance MI lane OP-2244..2248; backend
BI0-4 OP-2238..2243); this is the verified contract + the activation runbook.

```
 IMX415 mic → on-board RKNN whisper (conf-asr)
   → transcript segments (control_api ring)
   → conf transcript_ingest (spool + retry, Bearer auth)
   → POST {backend}/api/v1/meetings/{id}/segments        (BI0 ingest, stored)
   → POST {backend}/api/v1/meetings/{id}/summary|translate|action-items|suggestions
                                                          (BI1-4 over stored text)
   → frontend meeting view (app/meetings/[id])
```

## Contract (verified, locked by a test)

Appliance `conf_ti_build_body` / `conf_ti_append_segment` emit:

```http
POST {base}/meetings/{meeting_id}/segments HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json

{"session_id":"<sid>","segments":[
  {"segment_seq":N,"start_ms":N,"end_ms":N,"text":"…","language":"en",
   "confidence":0.820,"is_final":true,"source":"onboard-rknn"}, …]}
```

This is byte-compatible with the backend `TranscriptIngestRequest` /
`TranscriptSegmentIn` models (`POST /meetings/{id}/segments`,
`backend/routers/transcripts.py`). The lock test
`backend/tests/test_meeting_intelligence_appliance_contract.py` feeds the literal
appliance JSON into the backend models — it fails if either side drifts.

Idempotency: the UNIQUE `(tenant_id, meeting_id, session_id, segment_seq)` key
makes reconnect-replay safe; a final supersedes a same-seq partial, a partial
after a final is rejected (never downgrades). The appliance mints a fresh
`session_id` per reconnect so replays carry the same triplet.

## Activation runbook

### Backend (productizer)
All meeting-intelligence surfaces are **disabled by default**. Enable per
environment (cost/privacy decision):

```
OMNISIGHT_TRANSCRIPT_INGEST_ENABLED=1     # BI0 ingest (if gated in your build)
OMNISIGHT_MEETING_SUMMARY_ENABLED=1       # BI1
OMNISIGHT_MEETING_TRANSLATE_ENABLED=1     # BI2
OMNISIGHT_MEETING_ACTION_ITEMS_ENABLED=1  # BI3
OMNISIGHT_MEETING_SUGGESTIONS_ENABLED=1   # BI4
```
(`backend/config.py` — strict `extra=forbid`, so the flags must be declared
fields, which they are.) BI1-4 call the house LLM adapter; ensure a provider is
configured (`settings.llm_provider`) or they return empty/degraded.

Provision an **operator-role API key** for the appliance and scope it to the
meeting's tenant (`backend/api_keys.py`; `auth.require_operator` accepts the
`Authorization: Bearer` key). Cross-tenant `meeting_id` → 404 (existence is not
leaked).

### Appliance (conference-appliance)
Point the transcript ingester at the backend `/api/v1` base and give it the key:

```
CONF_TRANSCRIPT_INGEST_URL=https://<backend-host>/api/v1
CONF_TRANSCRIPT_INGEST_TOKEN=<the operator API key>
```
(`src/transcript_ingest/transcript_ingest.c` — env-driven; the path
`…/meetings/{id}/segments` is appended automatically.) The meeting_id is the
session/meeting label the daemon assigns.

## Verify end-to-end

1. Backend up with the flags + an LLM provider; appliance env set.
2. Start a call on the board → speak → confirm rows land:
   `GET /api/v1/meetings/{id}/segments?final_only=true`.
3. Generate insights:
   `POST /api/v1/meetings/{id}/summary` (and `/action-items`, `/suggestions`,
   `/translate` with `{"target_language":"…"}`).
4. View in the frontend at `/meetings/{id}`.

## Privacy

Transcripts are speech-derived and sensitive. BI0b (OP-2239) wires retention
(`meetings.retention_until` + sweep, `backend/transcripts_retention.py`), DSAR
erasure/portability, and per-tenant audit-chain coverage. Keep retention short
and the flags off where not needed.
