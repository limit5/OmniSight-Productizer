# Case 5 — Smart Conferencing Device on RK3588: full feature + implementation plan

**Date:** 2026-06-14 · **Board:** ATK-DLRK3588 (dual IMX415 4K, RKNPU 6 TOPS, MPP H.264/H.265, ES8323 audio codec, DSI touch panel, HDMI-in + HDMI-out, dual USB-C OTG/dwc3, eth + WiFi)
**Goal:** build one complete smart conferencing device ("會議機") end-to-end on this board.
**Reusable assets in hand:** `linux-uvc-uac-app` (UVC+UAC gadget — hardware-proven on this board), `rtsp-onvif-server` (RTSP/ONVIF — hardware-proven), skill packs `imaging` / `npu-detection` / `npu-pose` / `connectivity` (PoE/Modbus) / `ota` / `telemetry` / `security`.

---

## 1. Product definition — two operating modes

A premium smart conferencing device is best built as **one device, two modes** (sharing the camera/audio/AI core):

- **Mode A — USB conferencing camera + speakerphone (peripheral).** Plug into a PC/laptop over USB-C; the board appears as a 4K **UVC camera + UAC speakerphone**. The PC runs Zoom/Teams/Meet/any app; the *device* does the smarts (auto-framing, speaker tracking, noise suppression). Analog: Poly Studio P15, Logitech MeetUp. **This is the MVP core — it reuses the already-proven `uvc-uac-app`.**
- **Mode B — Standalone appliance (endpoint).** The device joins meetings itself via a native **WebRTC / SIP** client, driving an HDMI room display + local DSI touch UI + speaker/mic, with calendar one-tap-join. Analog: Zoom Room / Poly G7500. **Heavier; Phase 2+.**

Both modes share the **camera + audio + AI engine**; the conferencing transport is the swappable top layer.

---

## 2. Complete feature set (grouped by domain)

### A. Video / Camera
1. Dual 4K capture + ISP (AE/AWB/low-light) — `imaging`, rkisp
2. **AI group auto-framing** (detect all people → frame the group) — NPU
3. **Active-speaker tracking** (zoom/cut to current speaker) — audio DOA + face fusion
4. Presenter / speaker-framing mode
5. Digital **PTZ** (smooth pan/tilt/zoom via 4K crop)
6. **Dual-camera director** (wide + close-up, or 2 angles; auto-switch / PiP) — leverages BOTH IMX415
7. Content/whiteboard camera mode (2nd cam) + **HDMI-in content ingest**
8. HW H.264/H.265 encode (MPP), multi-stream (near + content + record)
9. Privacy: camera mute + tally state
10. Background blur/replace (NPU segmentation); auto light correction

### B. Audio
11. Mic capture (ES8323; **mic-array beamforming = add-on, gated**)
12. **AEC + noise suppression + AGC** (full-duplex speakerphone) — speexdsp/webrtc-audio-processing
13. Active-speaker **DOA** (direction of arrival) for camera tracking (needs mic array)
14. Speaker out / volume / mute
15. UAC audio gadget to host (Mode A)

### C. Connectivity / modes
16. **Mode A**: UVC 4K camera + UAC speakerphone to host — `uvc-uac-app`
17. UVC controls (PTZ/brightness/zoom) exposed to host app
18. **Mode B**: WebRTC client (browser/custom service join)
19. Mode B: SIP / H.323 interop client
20. Vendor room systems (Zoom Rooms / Teams Rooms / Meet HW) — **partnership/cert gated**
21. RTSP/ONVIF stream + record — `rtsp-onvif-server`
22. Wireless screen-share (Miracast/AirPlay) — stretch

### D. Local UI / UX
23. Touch home (DSI 1080×1920): join / dial / settings / status
24. **Calendar integration** (Exchange/Google) — meeting list + one-tap join
25. In-call controls: mute / cam / volume / layout / end
26. **HDMI output** to room display (self-view / far-end / content)
27. On-screen device + network status
28. Presence/proximity wake (camera person-detect) → wake from idle

### E. Power / hardware I/O
29. USB-C **PD** power negotiation — gated (PD circuitry)
30. **PoE** powering (PSE/PD add-on, e.g. TPS23861) — gated
31. HDMI-in capture (BYOM / content) — board has it
32. Power management: idle/sleep/wake

### F. AI (NPU differentiators)
33. People detection + counting (occupancy) — `npu-detection`
34. Auto-framing engine (orchestrates detection → PTZ targets)
35. Active-speaker fusion (audio DOA + face) — `npu-pose` + DOA
36. Background segmentation (blur/replace)
37. Gesture control (raise-hand) — stretch
38. Live captions / transcription — stretch (on-device small ASR or cloud)
39. Meeting analytics (attendance, talk-time) — stretch

### G. System / management / security
40. Zero-touch provisioning / enrollment
41. **OTA** updates — `ota`
42. Remote fleet management + health — `telemetry`
43. Device identity + secure boot + encrypted media (SRTP) — `security`
44. Diagnostics / logs
45. Config (resolution / framing sensitivity / audio tuning)

---

## 3. Phased implementation (runner-ticket-ready)

### Phase 0 — Foundation & bring-up (verify the device-side I/O)
- **P0.1** Audio bring-up: capture from ES8323 (card 3), playback to ES8323; loopback test; pick ALSA device map. *(runner + on-board test)*
- **P0.2** Camera pipeline: both IMX415 → ISP → NV12 → MPP H.264 encode; confirm dual-stream. *(reuse imaging; on-board)*
- **P0.3** NPU runtime: rknn model load + run a person-detector at ≥15fps on a 1080p down-scaled stream. *(npu-detection)*
- **P0.4** Repo scaffold: new subproject `conference-appliance` (or extend uvc-uac-app) — modules: `media/`, `audio/`, `ai/`, `transport/`, `ui/`, `mgmt/`. CMake + cross/native build (reuse the debian:11 toolchain + atkctl/atklink workflow).

### Phase 1 — MVP: USB smart conferencing camera + speakerphone (Mode A) — **the demoable product**
- **P1.1** UVC camera gadget: 4K/1080p stream from IMX415 via `uvc-uac-app` (apply the merged dwc3-gadget node-detect fix; bind once — avoid the dwc3 churn hang).
- **P1.2** UAC speakerphone gadget: mic→host + host→speaker through `uvc-uac-app` UAC; wire ES8323.
- **P1.3** AEC + noise suppression + AGC on the mic path (webrtc-audio-processing).
- **P1.4** AI group auto-framing v1: person-detect → bounding box of all people → smooth digital PTZ crop fed into the UVC stream.
- **P1.5** UVC PTZ controls exposed to host (so the meeting app can pan/zoom).
- **P1.6** Tally/privacy state + basic config file.
- **P1.7 (exit gate / demo):** plug board USB-C into a PC → Zoom/Teams/Meet sees a 4K camera + speakerphone with working auto-framing + echo-free audio.

### Phase 2 — Standalone appliance (Mode B) + local UX
- **P2.1** WebRTC client (libwebrtc / GStreamer webrtcbin) — join a room, send near video (MPP-encoded) + audio, receive far-end.
- **P2.2** HDMI-out compositor: far-end + self-view + content layout → room display.
- **P2.3** DSI touch UI — **dual implementation (customer choice), both against one shared local control API**:
  - **P2.3-API** Local control/IPC API (gRPC or local HTTP/WebSocket) exposing call state, roster, framing, audio, settings — the single contract both UIs bind to.
  - **P2.3a** Qt6 touch UI (primary; reuse `UVCCamera_Qt` Qt experience).
  - **P2.3b** Flutter-embedded touch UI (same screens, same control API).
- **P2.4** Calendar integration + one-tap join.
- **P2.5** SIP/H.323 interop (pjsip) — optional second transport.
- **P2.6** Active-speaker tracking (needs mic-array add-on for DOA; fallback: face-based).
- **P2.7** Dual-camera director + HDMI-in content sharing.

### Phase 3 — Productization / management / advanced AI
- **P3.1** OTA + remote fleet management + telemetry + health.
- **P3.2** Provisioning / zero-touch enrollment + device identity / secure boot / SRTP.
- **P3.3** Background blur/replace, occupancy analytics, gesture, captions (each independent).
- **P3.4** Vendor room-system enrollment (Zoom/Teams/Meet) — **partnership track**.
- **P3.5** Power: USB-C PD + PoE bring-up — **hardware track**.

---

## 4. Honest gates (NOT pure-software / runner-buildable)
- **Mic-array beamforming + DOA speaker-tracking** — board has a 2-ch ES8323 codec, not a mic array. Needs an add-on mic-array board. Software can do AEC/NS/AGC on what's there; *spatial* speaker-tracking is gated → Phase 2.6 falls back to face-based until hardware lands.
- **PoE / USB-C PD** — needs PSE/PD circuitry (TPS23861 etc.). Phase 3.5 hardware track.
- **Zoom Rooms / Teams Rooms / Meet certified appliance** — vendor SDK + certification + partnership. Phase 3.4 business track, not code-only.
- **Physical privacy shutter / tally LED** — mechanical/electrical; software exposes the state only.

## 5. MVP recommendation (build first)
**Phase 0 + Phase 1** = a genuinely demoable, hardware-proven **USB 4K AI conferencing camera + speakerphone** that works with any PC meeting app — built almost entirely on assets already proven on this board (`uvc-uac-app` + NPU detect + MPP encode + ES8323 audio). This is the highest-value, lowest-risk first deliverable and the right scope to hand the runner first.

## 6. Suggested repo / build
New subproject `conference-appliance` (sibling of `uvc-uac-app`), depending on `uvc-uac-app` (gadget) + `rtsp-onvif-server` (stream/record), with `media/ audio/ ai/ transport/ ui/ mgmt/` modules. Build + deploy via the established debian:11 cross / native-on-board flow + `atkctl`/`atklink`. Per-phase work items above map 1:1 onto runner tickets (4-AC each: code / deploy / integration / exercised-on-RK3588).

---

## 7. First-wave decomposition (DECIDED 2026-06-14: P0+P1+P2, new repo, dual Qt+Flutter UI)

**Decisions locked:** first wave = **P0+P1+P2**; implementation in **new subproject `conference-appliance`** (sibling repo, depends on uvc-uac-app + rtsp-onvif-server); touch UI built **twice — Qt6 AND Flutter** — both against one shared local control API (customer choice).

**Prerequisite (before any runner ticket can land):**
- **PRE-1** Create GitLab repo `omnisight/conference-appliance` + scaffold: CMake top-level, modules `media/ audio/ ai/ transport/ ui/{api,qt,flutter}/ mgmt/`, debian:11 cross + native-on-board build, CI smoke, README. *(this is the seed ticket; everything blocks on it)*

**EPIC tree (each leaf = one runner ticket, 4-AC: code / deploy / integration / exercised-on-RK3588; area:* spans embedded/backend/frontend/tests as the AC requires):**

- **EPIC C5.P0 Foundation** → P0.1 audio bring-up (ES8323) · P0.2 dual-cam→MPP encode · P0.3 NPU person-detector ≥15fps · P0.4 repo modules+build (folds into PRE-1)
- **EPIC C5.P1 USB conferencing camera+speakerphone (MVP)** → P1.1 UVC 4K gadget (apply dwc3 node-detect fix, bind-once) · P1.2 UAC speakerphone · P1.3 AEC+NS+AGC · P1.4 AI group auto-framing v1 · P1.5 UVC PTZ controls to host · P1.6 tally/privacy+config · **P1.7 MVP exit-gate: PC sees 4K auto-framing echo-free camera (Zoom/Teams/Meet)**
- **EPIC C5.P2 Standalone appliance + UX** → P2.1 WebRTC client (webrtcbin+MPP) · P2.2 HDMI-out compositor · **P2.3-API shared control API** · **P2.3a Qt UI** · **P2.3b Flutter UI** · P2.4 calendar one-tap-join · P2.5 SIP/H.323 (pjsip) · P2.6 active-speaker tracking (face-based; DOA when mic-array lands) · P2.7 dual-cam director + HDMI-in content

**Sequencing / mutex notes (from runner SOP memory):**
- PRE-1 blocks everything. P0.* can parallelize (file-disjoint) once PRE-1 lands.
- P1.1 depends on the gadget layer; bind-once to dodge the dwc3 churn hang ([[project_atk_dlrk3588_board_access]] Bug #2).
- P2.3a/P2.3b both depend on P2.3-API but are file-disjoint from each other → parallel-safe.
- Every ticket exercised on the real RK3588 via `atkctl`/`atklink`; gate on hardware-exercised AC.
- Honest gates (mic-array DOA, PoE/PD, vendor cert) stay Phase 3 / hardware / partnership tracks — NOT in this wave.

