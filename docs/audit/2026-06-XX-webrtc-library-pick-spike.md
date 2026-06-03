# WebRTC Library Pick Spike

**Ticket:** OP-1984
**Date:** 2026-06-03
**Scope:** C5.D-WebRTC.0 spike for Case 5 Phase 1B Wave-4. Compare
`libdatachannel`, `pion/webrtc`, and Google's `libwebrtc` for the ATK-DLRK3588
RK3588 aarch64 conference-device path. No WebRTC stack integration was written.

## Summary

**Recommendation: choose `libdatachannel` for C5.D-WebRTC.1.**

`libdatachannel` is the best fit for the first RK3588 WebRTC integration leaf
because it is native C++17 with C bindings, is intentionally smaller than
Google's reference tree, supports WebRTC Data Channels and media transport, and
maps cleanly to the existing Phase 0 `rockchip-rk35xx-aarch64-gcc-11`
toolchain catalog entry. It keeps the media path close to the existing embedded
C/C++ daemon style while avoiding the Chromium checkout and GN build system
burden of `libwebrtc`.

`pion/webrtc` remains a credible prototype or host-side interop test tool, but
it would introduce a Go runtime and Go module/vendor policy into the target
firmware path. That is not wrong, but it is a bigger convention break than this
WebRTC wave needs. Direct `libwebrtc` should be reserved for a later escalation
only if the first two options fail on browser interoperability, congestion
control, or hardware-codec plumbing.

The latency baseline for this leaf is therefore:

- Control-plane RTT target: `<= 1 ms typical` for an already-established
  same-LAN data-channel control message on RK3588.
- First qemu smoke expectation: connection setup and SDP/ICE plumbing only;
  qemu is not authoritative for sub-millisecond RTT.
- Real-board follow-up expectation: measure 1,000 request/echo samples on the
  ATK-DLRK3588 LAN path and report p50, p95, p99, and max.

## Scope Anchors

- Design ref: `docs/architecture/2026-06-03-case5-conference-device-epic-design.md`
  §3, C5.D-WebRTC.0.
- Parent: OP-1975.
- Blocks: META OP-1973.
- Follow-on leaves: C5.D-WebRTC.1 PeerConnection stack, C5.D-WebRTC.2
  STUN/TURN client and SDP generation, C5.D-WebRTC.3 outbound UVC/UAC media.
- Integration exercise: C5.F.1 qemu smoke for toolchain through WebRTC outbound;
  real RK3588 RTT measurement is deferred to hardware follow-up.

## Decision Matrix

| Criterion | libdatachannel | pion/webrtc | Google libwebrtc |
| --- | --- | --- | --- |
| Language/runtime | C++17 with C API | Pure Go | C++ reference implementation |
| RK3588 build fit | Good: CMake-friendly C/C++ dependency shape; cross with aarch64 gcc/sysroot | Good for pure Go cross-build, but introduces Go runtime/module policy | Poor for this leaf: Chromium depot tools, GN, Ninja, very large checkout |
| Yocto recipe fit | Good: package submodules or system deps; selectable TLS and ICE backends | Medium: Go module vendoring must be explicit and reproducible | Poor: vendor-heavy source tree and non-CMake build expectations |
| Runtime memory | Expected lowest of the three for embedded media/control role | Expected low-to-medium; Go runtime adds baseline RSS and GC behavior | Expected highest; reference stack pulls broad browser-era machinery |
| Latency floor | Good: native data-channel/media transport with minimal abstraction | Good: pure Go stack can be low-latency but GC pauses must be measured | Good technically, but build/runtime weight is excessive for first wave |
| Codec path | RTP/media transport; app supplies encoded frames. H264, VP8, VP9 packetizer/depacketizer headers present; Opus RTP can be carried as media | Direct RTP/RTCP API; README lists Opus, PCM, H264, VP8, VP9 packetizers | Broadest reference codec/congestion-control surface |
| STUN/TURN | ICE with STUN and TURN via libjuice or libnice backend | Full ICE, STUN, TURN over UDP/TCP/DTLS/TLS | Full reference support |
| Browser interop | Intended Firefox/Chromium/Safari compatibility | Mature server/embedded/browser interop community | Reference baseline |
| Operational risk | Medium: media encode/decode remains application-owned | Medium: new language/runtime in embedded firmware | High: source, build, and API churn burden |
| Recommendation | **Pick** | Keep as prototype fallback | Reject for C5.D-WebRTC.1 unless escalation is justified |

## Library Notes

### libdatachannel

Upstream describes `libdatachannel` as a standalone C/C++ implementation of
WebRTC Data Channels, WebRTC Media Transport, and WebSockets for GNU/Linux and
other POSIX platforms, with C bindings and a simplified JavaScript-like API:
<https://github.com/paullouisageneau/libdatachannel>.

Relevant fit points:

- Dependencies are selectable and embedded-friendly: OpenSSL, GnuTLS, or
  Mbed TLS for security; `libjuice` or `libnice` for ICE; `libsrtp` only when
  media support is enabled.
- The protocol stack includes SCTP data channels, SRTP media transport,
  DTLS/UDP, ICE, STUN, and TURN.
- Media transport can be disabled at compile time, which gives C5.D-WebRTC.1 a
  small control-plane-first path before C5.D-WebRTC.3 adds UVC/UAC outbound
  media.
- The public C++ aggregate header includes media packetizer/depacketizer
  classes for H264, VP8, VP9, AV1, and H265 when `RTC_ENABLE_MEDIA` is set:
  <https://raw.githubusercontent.com/paullouisageneau/libdatachannel/master/include/rtc/rtc.hpp>.

Risks and mitigations:

- `libdatachannel` is not a video/audio encoder. C5.D-WebRTC.3 must feed
  already-encoded frames, likely from RK3588 hardware encoder/GStreamer/FFmpeg
  plumbing, then packetize and negotiate them.
- TURN behavior depends on ICE backend choice. Prefer `libnice` first if Yocto
  already carries a reliable recipe; otherwise vendor `libjuice` explicitly.
- C5.D-WebRTC.1 should pin the version and capture the chosen TLS/ICE backend
  in the recipe comment so later leaves do not silently change behavior.

### pion/webrtc

Upstream describes Pion WebRTC as a pure Go implementation of the WebRTC API:
<https://github.com/pion/webrtc>.

Relevant fit points:

- Cross-compilation to `linux/arm64` is straightforward for pure-Go packages.
- Pion's README lists PeerConnection, DataChannels, audio/video send/receive,
  full ICE, ICE restart, Trickle ICE, STUN, and TURN over UDP/TCP/DTLS/TLS.
- The same README lists direct RTP/RTCP access plus Opus, PCM, H264, VP8, and
  VP9 packetizers.

Risks and mitigations:

- The target firmware currently trends C/C++ for embedded daemon work. Pulling
  Go into the RK3588 image adds runtime, module vendoring, reproducibility, and
  support questions beyond this leaf.
- Go's GC is probably acceptable for a conference control plane, but the
  `<= 1 ms typical` control RTT claim must be measured on real hardware rather
  than inferred from host builds.
- Pion is still useful as a qemu/host interop reference because it is fast to
  build and can exercise signaling, SDP, ICE, and browser compatibility without
  committing the production firmware path to Go.

### Google libwebrtc

Google's native WebRTC documentation lists Windows, macOS, Linux, Android, and
iOS as supported platforms and uses `fetch webrtc`, `gclient sync`, GN, and
Ninja for source checkout and build:
<https://webrtc.github.io/webrtc-org/native-code/development/>.

Relevant fit points:

- It is the reference implementation and has the broadest browser-era feature
  surface.
- It is the strongest fallback if the product later needs exact Chromium
  behavior or reference congestion-control behavior.

Risks and mitigations:

- The documented checkout is large, including a 6.4 GB Linux checkout estimate
  before build output. That is an immediate recipe and CI concern for this
  small leaf.
- The documented build path is GN/Ninja. That is not aligned with the Phase 0
  CMake/Yocto pattern and would require a dedicated vendor-source strategy.
- The API and build graph are too broad for the current goal, which only needs
  a browser-compatible outbound media/control endpoint.

## Codec Support Assessment

Browser-facing WebRTC interoperability should target Opus for audio and H.264
Constrained Baseline or VP8 for video first. MDN's WebRTC codec guide points to
the browser-mandatory set: VP8 and H.264 Constrained Baseline for video, and
Opus plus G.711 PCMA/PCMU for audio:
<https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Formats/WebRTC_codecs>.

| Codec | Required for Case 5? | libdatachannel | pion/webrtc | libwebrtc |
| --- | --- | --- | --- | --- |
| Opus | Yes, primary audio target | RTP media can carry Opus; encoder external | README lists Opus packetizer | Reference support |
| H.264 | Yes, preferred RK3588 hardware path | H264 RTP packetizer/depacketizer headers present; encoder external | README lists H264 packetizer | Reference support |
| VP8 | Yes, browser fallback | VP8 packetizer/depacketizer headers present; encoder external | README lists VP8 packetizer | Reference support |
| VP9 | Nice-to-have, not first acceptance target | VP9 packetizer/depacketizer headers present; encoder external | README lists VP9 packetizer | Reference support |

Practical codec choice:

- First implementation target: Opus + H.264.
- Browser fallback target: Opus + VP8.
- VP9 should remain a negotiated fallback only after CPU, encoder, and SFU
  compatibility are measured.

## RK3588 Toolchain Fit

Phase 0 already has the relevant catalog entry:
`configs/embedded_catalog/cross-toolchain.yaml` defines
`rockchip-rk35xx-aarch64-gcc-11` as the Rockchip RK3576/RK3588 aarch64
toolchain, Linaro GCC 11.3 base, target triple `aarch64-linux-gnu`, glibc, no
NDA gate.

The existing RK35xx Yocto overlay also mirrors that contract in
`yocto/meta-omnisight-camera/recipes-core/uvcvideo-xu-dispatcher/uvcvideo-xu-dispatcher_%.bbappend`:

- `ROCKCHIP_RK35XX_TOOLCHAIN_ID = rockchip-rk35xx-aarch64-gcc-11`
- `ROCKCHIP_RK35XX_TARGET_TRIPLE = aarch64-linux-gnu`
- `ROCKCHIP_RK35XX_SYSROOT = ${RECIPE_SYSROOT}`
- RK3576/RK3588 CFLAGS and LDFLAGS append `--sysroot=${RECIPE_SYSROOT}`.

Expected gotchas for C5.D-WebRTC.1:

- Always resolve the platform toolchain through the catalog. Do not use host
  `gcc`, host `clang`, or a system-default CMake compiler.
- If a CMake toolchain file is present for RK35xx, pass it explicitly with
  `-DCMAKE_TOOLCHAIN_FILE=...`.
- Pass the Yocto sysroot to all C and C++ dependency builds, including TLS,
  SRTP, SCTP, and ICE backends.
- Pin dependency versions in the recipe. Do not fetch recursive submodules at
  build time without fixed revisions.
- Decide `libjuice` versus `libnice` once in the recipe. Mixing ICE backends
  across patchsets will make STUN/TURN behavior hard to compare.
- Keep media encode/decode outside the WebRTC library selection. The chosen
  library transports RTP; RK3588 hardware encoding is a separate C5.D-WebRTC.3
  and AV-sync concern.

## Latency Baseline

This spike did not measure RK3588 hardware latency; no EVK was available in the
runner environment. The baseline below is the target and measurement contract
for the follow-on leaves.

| Path | Target | Measurement point |
| --- | --- | --- |
| Control data-channel echo, same LAN, established PeerConnection | `<= 1 ms typical`, p95 recorded | App timestamp before send and after echo receive |
| ICE + DTLS setup | Informational only | Time from local offer to connected state |
| First outbound media RTP packet after track enabled | Informational in qemu; hardware value deferred | Timestamp from track enable to first RTP observed |
| Glass-to-glass AV latency | Out of scope for C5.D-WebRTC.0 | C5.E/C5.F hardware follow-up |

Recommended measurement harness for C5.D-WebRTC.1/C5.F.1:

1. Establish PeerConnection over a same-LAN STUN-less host-candidate path where
   possible; run a separate TURN-relayed profile for fallback coverage.
2. Open one ordered reliable DataChannel named `control`.
3. Send 1,000 fixed-size echo messages after the channel reports open.
4. Record p50, p95, p99, max, and any reconnect/ICE restart events.
5. Repeat with media track negotiated but idle, then with outbound H.264 or VP8
   RTP active, so media transport overhead is visible.

Pass condition for the real-board follow-up: p50 `<= 1 ms` and no sustained
tail above the operator-approved threshold for same-LAN control traffic. If TURN
relay is in the path, record it separately and do not compare it to the direct
LAN baseline.

## Recommendation

Proceed with `libdatachannel` in C5.D-WebRTC.1.

Required implementation posture for the next leaf:

- Add only a minimal PeerConnection/DataChannel skeleton first.
- Build it through the RK35xx catalog entry and Yocto sysroot.
- Configure STUN/TURN through explicit ICE server settings; do not hide network
  defaults in code.
- Keep codec encode/decode out of C5.D-WebRTC.1 except for SDP capability
  declarations needed by the skeleton.
- Capture qemu smoke evidence in C5.F.1 and defer real RTT numbers to a
  hardware follow-up if the ATK-DLRK3588 board is still unavailable.

This keeps Case 5 on the intended WebRTC wave path while preserving an escape
hatch: if `libdatachannel` fails browser interop or media transport acceptance,
use `pion/webrtc` for a controlled comparison before escalating to `libwebrtc`.

## Evidence

| Check | Result |
| --- | --- |
| Required spike file exists | This file: `docs/audit/2026-06-XX-webrtc-library-pick-spike.md` |
| Design section cross-reference | Verified: C5.D-WebRTC.0 is listed in `docs/architecture/2026-06-03-case5-conference-device-epic-design.md` §3 |
| Phase 0 toolchain catalog | Verified: `configs/embedded_catalog/cross-toolchain.yaml` contains `rockchip-rk35xx-aarch64-gcc-11` |
| RK35xx Yocto sysroot pattern | Verified: `uvcvideo-xu-dispatcher_%.bbappend` passes RK35xx toolchain id, target triple, and sysroot |
| Upstream libdatachannel feature source | Verified via upstream README and public headers |
| Upstream pion/webrtc feature source | Verified via upstream README |
| Upstream libwebrtc build-complexity source | Verified via WebRTC native development documentation |
| Runtime exercise | Deferred: no WebRTC integration code and no RK3588 EVK in this documentation-only spike |

## Conclusion

**GREEN for C5.D-WebRTC.1 planning.**

`libdatachannel` is the recommended production library for the RK3588 embedded
WebRTC path. It best matches the current C/C++ embedded conventions, the Phase 0
Rockchip aarch64 toolchain catalog, and the need for a small first
PeerConnection/DataChannel integration. Pion should remain available for fast
prototype and interop comparison. Google `libwebrtc` is technically strong but
too heavy for this leaf's build and Yocto constraints.
