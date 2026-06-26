# Case 5 EPIC B — two-way call COMPLETION — ticket bodies **v2** (post audit + blind-test)

**Date:** 2026-06-27. Supersedes `2026-06-26-...-epicB-twoway-ticket-bodies.md`.
Revised after the SOP Stage-4 codex audit + Stage-5 blind-test (both independent,
strongly convergent) AND two product decisions by the operator:
- **D-COEXIST = COEXIST**: a two-way call MUST run while the device is still a PC
  USB UVC camera + UAC speakerphone. This OVERRIDES transport.h's current
  "webrtc mode mutually exclusive with the UVC/UAC gadget at device-config level."
- **D-INTEROP = SIP + H.323 in v1**: first completion does WebRTC + SIP (pjsip)
  AND H.323. pjsip is SIP-only → H.323 needs a SEPARATE stack (net-new buildroot
  package + worker).

## Grounding corrections the audit surfaced (verified)
1. **Daemon spawn glue ALREADY EXISTS** — `src/transport/transport.c` already
   renders `conf_transport_webrtc_client_launch_spec` AND `conf_transport_sip_*`
   launch-specs and declares `conf_transport_sip_client_start`; control_api has
   `/v1/call/start|end` + `conf_control_call`. **Tickets must scope to the WORKER
   binaries honoring `--spec`, NOT to rebuilding daemon spawn/launch glue.** Any
   daemon change is a THIN wire (control→transport plane-select), not new spawn
   plumbing. Add explicit "do not modify the existing launch-spec format" notes.
2. **WebRTC audio is NOT wired** — `conf-webrtc-app/src/pipeline.c` builds a
   VIDEO-ONLY webrtcbin graph; far video is a `filesink` stub; spec.h HAS near/far
   audio fields but the pipeline has no Opus legs. A naive "call connects" check
   passes with zero audio. Every webrtc media AC must assert bidirectional Opus
   audio AND far-video sink replacing filesink.
3. **No backend-select field in spec.h** — B1's transport backend selection must
   either add the field (name/default/validation) or derive from signaling_url.
4. **H.323 ≠ pjsip** — buildroot has libpjsip (SIP) + libwebsockets + libsoup3,
   but NO h323 stack. H.323 = net-new buildroot package (h323plus or ooh323c).

## Decisions (frozen)
- **B-MI0 signaling** = self-built lightweight JSON-over-WS server is DEFAULT +
  pluggable external-SDK adapter (unchanged from v1). Protocol must be fully
  specified (envelope, peer/room scoping, offer-collision/glare policy,
  end-of-candidates, duplicate-peer, disconnect/leave) with golden vectors in
  concrete files, consumed by tests (not hand-copied).
- **D-COEXIST = coexist** → see B-AUD (the audio-ownership/AEC ticket).
- **D-INTEROP = SIP + H.323** → B3* (SIP) + B6* (H.323).
- **AEC topology (D-AEC)**: the call worker MUST NOT open es8388 directly (that
  fights the uvc-uac-app bridge → echo/dead-mic). Decided: the near-audio for a
  call is taken from the SAME already-AEC'd mic stream the UAC bridge produces
  (fan-out, like the ASR tap), and far-audio is mixed back without a second AEC.
  Captured in B-AUD; referenced by B1-audio + B3b + B6b.

---

## Lane 0 — buildroot deps (hand, tier:X)
### B0a — enable libwebsockets (defconfig flip)
- Files: atk buildroot defconfig `BR2_PACKAGE_LIBWEBSOCKETS=y` + the omnisight-camera-sdk
  conf-webrtc worker package DEPENDENCIES += libwebsockets. AC: defconfig diff
  committed; `libwebsockets.so` in target; **the rebuilt conf-webrtc-app actually
  links libwebsockets** (readelf NEEDED), not merely sysroot presence. Go-Live: B1.
### B0b — enable libpjsip (defconfig flip)
- Files: `BR2_PACKAGE_LIBPJSIP=y` + its media codec options needed for H.264/Opus
  RTP (verify pjsip package sub-options, not just the bare flag). AC: pjsip libs in
  target + conf-sip-app links them; the H.264/Opus codec libs pjsip needs resolve.
  Go-Live: B3a.
### B0c — NEW buildroot package for an H.323 stack (net-new, BIG)
- Goal: an H.323 protocol stack on the image (h323plus or ooh323c — pick the
  lighter cross-compilable one). Buildroot has NONE today.
- Files: NEW buildroot package (Config.in + .mk) under omnisight-camera-sdk
  br2-external (mirror the uvc-uac-app/conference-appliance package pattern,
  OP-2461/2434); defconfig flip to enable it.
- AC: package cross-builds for aarch64 (glibc 2.41); lib + headers resolve in the
  staging sysroot; a trivial link test. Go-Live: B6a. **Scope/risk: largest 0-lane
  item — H.323 stacks are heavy C++; budget accordingly.**

## Lane 1 — WebRTC plane (repo:conference-appliance, tier:M)
### B1 — WebRTC signaling client in conf-webrtc-app
- Goal: replace main.c's log-and-loop with a real libwebsockets signaling client
  (B-MI0 protocol): connect signaling_url, join room/peer, SDP offer/answer +
  trickle ICE into the EXISTING `pipeline.c` webrtcbin graph.
- Files: `workers/conf-webrtc-app/src/` (new signaling module + wire into
  pipeline.c's webrtcbin on-negotiation-needed/on-ice-candidate; main.c runs the
  client). Pluggable transport iface: `ws` (default, B2) + `sdk` (stub). Backend
  select field added to spec.h (name/default/validation) OR derived from URL.
- Runner anchors: media STAYS in the existing pipeline.c webrtcbin graph — do NOT
  replace the graph or add a second media stack; do NOT touch the daemon
  launch-spec format. Consume B-MI0 golden vectors from their files in a
  state-machine test (offer→answer→ice→connected, mock WS).
- AC: signaling+SDP/ICE+transport-abstraction committed (gated gstreamer-webrtc-1.0
  + libwebsockets); negative paths (bad URL, WS reconnect/leave, server
  disconnect, no ICE servers, malformed JSON); selftest drives the SM vs a mock
  server, NO live media. blockedBy B0a, B-MI0.
### B1-audio — add bidirectional Opus audio + real far-video sink to pipeline.c
- Goal: close the audio gap. Add near-audio (from B-AUD fan-out → Opus → sendrecv)
  + far-audio (sendrecv → Opus decode → playback via B-AUD mix-back) legs to
  pipeline.c, and replace the far-video `filesink` stub with the real sink
  (fifo/shm → compositor/touch_ui InCall view).
- AC: pipeline.c SDP has audio+video sendrecv pads; selftest asserts the pads
  exist + far-video sink is not filesink. blockedBy B1, B-AUD.

## Lane 2 — signaling server (Productizer backend, NO repo: label)
### B2 — self-built lightweight signaling server + dual-end harness
- Goal: the default WS signaling server B1 connects to (rooms/peers, relays
  offer/answer/ice; no media on the server).
- Files: NEW backend module **in the existing backend framework** (verify it's
  FastAPI before assuming; if not, use whatever the backend is) — a WS endpoint +
  in-memory room registry; dual-end test harness. Protocol = B-MI0 EXACTLY, incl.
  recipient/peer targeting fields so >2-peer relay is unambiguous. Auth: reuse the
  backend's existing auth/tenant guard; define how WS auth is passed + rejected.
- AC: endpoint + registry committed; route registered; dual-end harness (A's offer
  → B, B's answer+ice → A). AC-Exercised (real conf-webrtc-app connects) is a
  CROSS-TICKET gate → blockedBy B-MI0 **and** B1 (or move that AC to B5a).

## Lane 3 — SIP plane (repo:conference-appliance, tier:M) — split from v1's B3
### B3a — NEW conf-sip-app worker: scaffold + REGISTER/INVITE (signaling only)
- Goal: net-new `workers/conf-sip-app/` (CMake + `--spec` parser mirroring
  `conf_transport_sip_client_config` + install to bin + selftest) using pjsip;
  account REGISTER to a registrar + inbound/outbound INVITE call-leg state machine.
  NO media yet. Honor the EXISTING daemon launch-spec (do not change it).
- AC: builds (host stub + board pjsip target gated on libpjsip); selftest
  REGISTER + handles an INVITE against `pjsua`/mock (signaling only: REGISTER,
  INVITE, 200/ACK, BYE). blockedBy B0b.
### B3b — SIP RTP media (Opus audio + H.264 video) wired to the device audio path
- Goal: two-way RTP on an established SIP call — near audio Opus (from B-AUD
  fan-out), near video H.264 (mpph264enc), far audio → mix-back (B-AUD), far video
  → mppvideodec → InCall sink. State how RTP audio joins the es8388/UAC path and
  whether ASRTAP01 captions should see SIP far/near audio.
- AC: media loop verified (not just `--null-audio` signaling); bidirectional RTP.
  blockedBy B3a, B-AUD.

## Lane 6 — H.323 plane (repo:conference-appliance, tier:M) — NEW (D-INTEROP)
### B6a — NEW conf-h323-app worker: scaffold + register/call signaling
- Goal: net-new H.323 worker using the B0c stack — gatekeeper register (if any) +
  H.225/H.245 call setup state machine; honor a daemon launch-spec (add the SIP-
  parallel h323 launch-spec to transport.c IF not already present — check first).
- AC: builds gated on the B0c package; selftest sets up a call against an H.323
  endpoint/`ohphone`-class peer (signaling only). blockedBy B0c.
### B6b — H.323 RTP media (audio + H.264) wired to the device audio path
- Goal: two-way H.323 media; same B-AUD fan-out/mix-back as SIP. blockedBy B6a, B-AUD.

## Lane C — control + audio glue (repo:conference-appliance, tier:M)
### B-C1 — call-runtime SELECTION via control_api/spec (the audit's "missing ticket")
- Goal: define + implement how `/v1/call/start` picks the plane (webrtc | sip |
  h323) and per-call config (room/peer/ice OR sip account/target OR h323 target),
  driving `conf_control_call.state` and spawning the right worker via the EXISTING
  transport launch-specs. THIN daemon wire — no new spawn plumbing.
- Files: `src/ui/api/control_api.c` + `src/transport/transport.c` (select only).
- AC: start payload schema (plane + per-plane fields) documented + validated;
  /v1/call/start spawns the correct worker + state reflects it; selftest per plane
  with mocks. blockedBy B-MI0 (payload contract).
### B-AUD — coexistence audio ownership + AEC topology (the D-COEXIST core)
- Goal: let a call run WHILE the UAC gadget is live (D-COEXIST), with NO double-AEC
  / echo. uvc-uac-app's AudioBridge remains the SOLE es8388 owner; it fans out the
  already-AEC'd near-mic to the call worker (reuse the ASR-tap fan-out pattern,
  ASRTAP01-style shm or a second tap) AND accepts a far-audio return stream to mix
  into es8388 playback. The call workers consume these taps — they NEVER open
  es8388/UAC directly. OVERRIDE transport.h's mutual-exclusion (call + gadget
  coexist).
- Files: `uvc-uac-app` AudioBridge (add a call-audio fan-out tap + a far-audio
  return mixer input) [repo:uvc-uac-app] + the conf-*-app workers consume them
  [repo:conference-appliance]. Cross-repo; split per repo at filing.
- AC: with the UAC gadget streaming, a call worker reads near-audio via the tap +
  pushes far-audio to the mixer; on-bench loopback shows no echo (AEC once) + the
  PC USB mic/speaker still work simultaneously. blockedBy nothing (foundational —
  file early; B1-audio/B3b/B6b depend on it).

## Lane 4 — launcher UI (repo:omnisight-ui, tier:M)
### B4 — two-way call UI in the launcher
- Goal: Join / InCall (mute/hangup/framing) / Settings in the conference launcher
  app, over the A3a control_api UDS transport (`/v1/call/start|end` + `call`
  event); show call state + far-party; optionally pick plane if the product allows.
- **Blockers FIXED**: blockedBy A3b (done) + A3a control_api contract ONLY — built
  + tested against MOCKS, NOT B1/B3/B6 runtimes. (v1 wrongly blocked on B1/B3.)
- Files: extend the A3b ConferenceCall app + an A3a control_api UDS client (NOT
  "B0b" — that was a v1 typo; B0b is pjsip). Offscreen app test vs a mock socket.
- AC: call UI + control wiring committed; offscreen test drives start/end vs a mock
  control_api; call-state renders. AC-Exercised on board moves to B5*.

## Lane 5 — exercised gates (hand on-board, tier:X)
### B5a — WebRTC two-way real run — near/far A/V + AEC + captions + USB-gadget STILL live (D-COEXIST). blockedBy B1, B1-audio, B2, B-AUD.
### B5b — SIP two-way real run — REGISTER/INVITE/200/ACK/BYE + bidirectional RTP (audio AND H.264 video) + captions on SIP audio + gadget coexist. blockedBy B3a, B3b, B-AUD.
### B5c — H.323 two-way real run — call against a real H.323 endpoint/MCU + bidirectional A/V + gadget coexist. blockedBy B6a, B6b, B-AUD.

## Filing order
1. B-MI0 (protocol+vectors+selection payload) ∥ B0a ∥ B0b ∥ **B0c (start early — net-new H.323 pkg, long lead)** ∥ **B-AUD (foundational)**.
2. B1 (after B0a,B-MI0) ∥ B2 (after B-MI0) ∥ B3a (after B0b) ∥ B6a (after B0c).
3. B1-audio (after B1,B-AUD) ∥ B3b (after B3a,B-AUD) ∥ B6b (after B6a,B-AUD) ∥ B-C1 (after B-MI0).
4. B4 (after A3a/A3b contract — NOT the runtimes; mocks).
5. B5a (B1,B1-audio,B2,B-AUD) ; B5b (B3a,B3b,B-AUD) ; B5c (B6a,B6b,B-AUD).
Label policy + safe filing sequence (tier:X → add repo: → flip tier:M) + blocker
gating as the A-line. B2 = Productizer (no repo: label); B1/B1-audio/B3a/B3b/B6a/
B6b/B-C1 = repo:conference-appliance; B-AUD splits repo:uvc-uac-app + repo:conference-appliance;
B4 = repo:omnisight-ui; B0*/B5* hand-driven.

## Status
Audit + blind-test applied; 2 decisions frozen (coexist + SIP&H.323). ~16 tickets
(up from v1's 9 — coexistence audio + H.323 net-new stack added). Ready to file.
