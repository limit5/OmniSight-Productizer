# Case 5 two-way call — signaling protocol v1 (B-MI0)

**Date:** 2026-06-27 (**amended 2026-06-28**: appliance-always-offers role — see
"Offerer role" — after on-HW validation showed the appliance cannot be the SDP
answerer). The contract `conf-webrtc-app` (B1) and the self-built signaling
server (B2) BOTH implement. Frozen here; golden vectors in
`2026-06-27-case5-signaling-vectors-v1.json` (same dir) — tests consume the
vectors from that file, not hand-copied.

## Transport
- JSON text frames over a single WebSocket (`ws://` default; `wss://` when TLS is
  provisioned). One WS per peer per room. Endpoint path: `/<api>/signal` (B2
  defines the concrete mount under the existing backend; B1 takes the full
  `signaling_url` from spec.h verbatim — it does NOT construct the path).
- The server relays signaling ONLY; **no media ever touches the server** (pure
  SDP/ICE relay; media is P2P/TURN).
- Pluggable transport (B1): `ws` backend (this protocol, default) + `sdk` backend
  (external WebRTC PaaS adapter, stub returns not-implemented). Selected by a spec
  field (`signaling_backend`, default `ws`) or derived from the URL scheme.

## Message envelope
Every frame is a JSON object:
```
{ "v": 1, "type": "<type>", "room": "<room_id>", "from": "<peer_id>",
  "to": "<peer_id|null>", "payload": { ... } }
```
- `v` = protocol version (1). Server/clients MUST reject a mismatched `v` with an
  `error` (code `version_mismatch`) and close.
- `room` = room id (string). `from` = sender peer id (server stamps/validates on
  relay). `to` = target peer id for directed messages (offer/answer/ice), or
  `null`/absent for room-scoped (join/peers/leave). **`to` is REQUIRED on
  offer/answer/ice** so >2-peer rooms relay unambiguously (the audit's gap).
- `payload` = type-specific (below).

## Types
- `join` (client→server, room-scoped): `payload:{ "role": "appliance" | "client" }`
  (default `"client"` when absent). Client announces presence as `from` in `room`
  and declares its ROLE. Server replies with `peers` to the joiner and broadcasts
  a `peers` delta to the room. Exactly ONE `appliance`-role peer per room (the
  device that ran `/v1/call/start`); every browser/remote peer is `client`.
- `peers` (server→client): `payload:{ "peers": [ {"id":"<peer_id>","role":"appliance|client"}, ... ] }`
  — current room membership (excluding the recipient), each tagged with its role.
  The **appliance-role peer is the offerer** (see "Offerer role"); a client reads
  this list to learn which peer is the appliance (whose offer it must await).
- `offer` (client→server→client, directed): `payload:{ "sdp": "<sdp>" }`.
- `answer` (directed): `payload:{ "sdp": "<sdp>" }`.
- `ice` (directed): `payload:{ "candidate":"<cand>", "sdpMid":"<mid>",
  "sdpMLineIndex": <int> }`. **End-of-candidates** = one `ice` with
  `payload.candidate == ""` (empty string) — NOT a null/omitted candidate.
- `leave` (client→server or server→client, room-scoped): `payload:{}`. On WS close
  the server SYNTHESIZES a `leave` for that peer to the room.
- `error` (server→client): `payload:{ "code":"<code>", "msg":"<text>" }`. Codes:
  `version_mismatch`, `bad_envelope`, `room_full`, `duplicate_peer`,
  `unknown_peer` (relay target absent), `unauthorized`,
  `unexpected_offer` (a client offered to the appliance — clients answer, never offer).

## Semantics / edge policy (the audit's required corners)
- **Offerer role (appliance always offers)**: the `appliance`-role peer is the
  MANDATORY offerer — it creates an offer to every `client` peer that appears in
  its `peers` set; clients ALWAYS answer and MUST NOT offer to the appliance. This
  is a hard requirement, not a preference: the appliance's on-device WebRTC stack
  (gstreamer `webrtcbin`) cannot act as the SDP *answerer* — validated on
  ATK-DLRK3588 (2026-06-28), where as answerer `create-answer` yields `a=inactive`
  because webrtcbin will not bind the appliance's mpph264enc baseline-H264 send
  transceiver (`profile-level-id=42c028`, the level its 1080p encode needs) to a
  browser's constrained-baseline (`42e01f`) offer. As OFFERER the appliance emits
  42c028 and browsers leniently accept it (proven end-to-end, real Chrome). The
  server MUST reject a client→appliance `offer` with `error{unexpected_offer}` and
  NOT relay it. Glare therefore cannot arise in the appliance↔client topology
  (only the appliance offers). For a hypothetical client↔client leg (future
  multi-device / SFU), fall back to the perfect-negotiation tiebreaker: the
  LEXICOGRAPHICALLY-LOWER `peer_id` is the impolite offerer, the higher rolls back
  its own offer on collision. Clock-free (no timestamps → resumable).
- B1 (`conf-webrtc-app`) on the appliance joins with `role: "appliance"`, offers
  on each new `client` in its `peers` set, and never answers (the server guards
  the inbound-offer path above; the worker's answer code stays only for tests).
- **Duplicate peer**: a `join` with a `from` already present in the room →
  server replies `error{duplicate_peer}` and rejects the WS (does not evict the
  incumbent).
- **Unknown relay target**: directed message whose `to` is not in the room →
  server replies `error{unknown_peer}` to the sender; does not broadcast.
- **Disconnect**: WS close → server removes the peer, synthesizes `leave` to the
  room. Client B1: on WS drop, bounded-backoff reconnect (200ms→…→30s) and
  re-`join`; on re-join it tears down stale PeerConnections for peers no longer in
  the `peers` set.
- **No media on server**; **no auth bypass** — the WS carries the backend's
  existing auth/tenant token (B2 defines how; reject with `error{unauthorized}` +
  close on failure).

## /v1/call/start selection payload (control_api → daemon; consumed by B-C1)
`POST /v1/call/start` body selects the plane + carries per-plane config:
```
{ "plane": "webrtc" | "sip" | "h323",
  "webrtc": { "signaling_url":"", "room":"", "peer":"", "ice_servers":[ {"urls":[],"username":"","credential":""} ], "signaling_backend":"ws|sdk" },
  "sip":    { "registrar":"", "account":"", "auth_user":"", "auth_pass_ref":"", "target":"", "transport":"udp|tcp|tls", "dtmf":"rfc2833|info|inband" },
  "h323":   { "gatekeeper":"", "alias":"", "target":"" } }
```
- Exactly ONE of `webrtc|sip|h323` present, matching `plane`. The daemon validates,
  spawns the matching worker via the EXISTING transport launch-spec, and reflects
  `conf_control_call.state` (idle→dialing→connected→ended). Secrets are passed by
  reference (`*_ref`) never inline (no secrets in logs/state).
- `/v1/call/end` `{}` ends the active call (any plane).

## Coexistence note (D-COEXIST)
Signaling/selection is plane-agnostic to audio ownership. Per B-AUD, NO call
worker opens es8388/UAC directly — near audio comes from the uvc-uac-app fan-out
tap, far audio returns via its mixer — so a call coexists with the live USB
gadget. This protocol does not change for coexistence; it is enforced in B-AUD +
the media tickets.
