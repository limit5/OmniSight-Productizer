# SIP Library Pick Spike

**Ticket:** OP-1999
**Date:** 2026-06-04
**Scope:** C5.D-SIP.0 spike for Case 5 Phase 1B Wave-4. Compare `pjproject`
(PJSIP) and GNU `libosip2`/`libexosip2` for the ATK-DLRK3588 RK3588 aarch64
SIP conference-device path. No SIP stack integration was written.

## Summary

**Recommendation: choose `pjproject` / PJSIP for C5.D-SIP.1, pending license
approval.**

PJSIP is the better technical fit for the RK3588 SIP track because it covers
the full SIP endpoint stack needed by the follow-on leaves: SIP signaling, SDP,
RTP/RTCP media, STUN, TURN, and ICE. That matches C5.D-SIP.1 through
C5.D-SIP.3 without adding a second media/NAT library family before the first
PBX interop test.

GNU `libosip2` plus `libexosip2` remains attractive when the product only needs
a small SIP parser, transaction layer, or basic user-agent signaling shell. It
is much smaller and easier to reason about, but eXosip2 explicitly omits RTP,
audio interfaces, and SDP negotiation. Choosing it would push media, RTCP,
STUN/TURN/ICE, and SDP offer/answer ownership into Case 5 application code or
additional libraries such as oRTP/libnice. That is too much integration
surface for the first enterprise-PBX path.

Two spike findings need operator attention before C5.D-SIP.1 starts:

- The ticket shorthand says PJSIP is BSD licensed, but upstream PJSIP
  documentation says the current project is GPL-or-proprietary. Treat this as
  a legal-review gate before shipping a proprietary firmware image.
- Scarthgap Layer Index evidence shows `libosip2` and `libexosip2` in
  `meta-networking`; `pjproject` appears in a third-party scarthgap layer,
  not in stock `meta-openembedded` / `meta-networking`. Treat the PJSIP recipe
  as a recipe-port/vendor-pin task, not a free in-tree dependency.

## Scope Anchors

- Design ref: `docs/architecture/2026-06-03-case5-conference-device-epic-design.md`
  section 3, C5.D-SIP.0.
- Parent: OP-1980.
- Blocks: META OP-1973.
- Follow-on leaves: C5.D-SIP.1 SIP REGISTER + INVITE/BYE signaling,
  C5.D-SIP.2 SDP negotiation + RTP/RTCP media plane, C5.D-SIP.3 PBX interop
  test against Asterisk and FreeSWITCH.
- Integration exercise: qemu can exercise userspace build and SIP loopback;
  real RK3588 media and Ethernet behavior remain a follow-up META.

## Decision Matrix

| Criterion | PJSIP / pjproject | GNU osip2 + eXosip2 |
| --- | --- | --- |
| Primary shape | Full SIP, media, and NAT traversal SDK | Minimal SIP parser/transaction stack plus higher-level SIP UA helper |
| License fit | Risk: upstream documents GPL-or-proprietary, not BSD | `libosip2` is LGPL; `libexosip2` is GPL-or-commercial |
| Approximate size | Large: Open Hub reports about 811k code lines for full pjproject, with C/C++ dominant | Small: Debian source stats for older `libosip2` report about 20k ANSI C SLOC; eXosip adds a modest UA layer |
| RK3588 build fit | Medium: C/autotools/CMake-capable upstream, but broad optional dependency graph | Good: C/autotools shape, small dependency set, libc-first posture |
| Memory footprint | Medium: tunable pool model; PJSIP documents PJNATH per-call ICE heap reduction from about 76 KB peak to about 21 KB in one experiment | Low for signaling only; total system grows once RTP, RTCP, NAT traversal, and SDP negotiation libraries are added |
| STUN/TURN/ICE | Built in through PJNATH | Not built into osip/eXosip stack; add separate NAT traversal library |
| RTP/RTCP media plane | Built in through PJMEDIA | Not included by eXosip2; add separate RTP/RTCP media library |
| SDP handling | Built into PJSIP/PJMEDIA path | osip has SDP parser pieces; eXosip2 says it does not contain SDP negotiation |
| PBX interop risk | Lower: mature endpoint stack already used in SIP products | Higher: more application-owned behavior must be matched to Asterisk/FreeSWITCH expectations |
| Yocto recipe fit | Yellow: third-party scarthgap recipe exists; stock meta-networking recipe not found | Green/yellow: stock `meta-networking` has `libosip2` and `libexosip2`, but GPL/commercial eXosip must be reviewed |
| Recommendation | **Pick, if license is cleared** | Keep as fallback for a signaling-only implementation |

## Library Notes

### PJSIP / pjproject

Upstream describes PJSIP as a C multimedia communication library implementing
SIP, SDP, RTP, STUN, TURN, and ICE, combining SIP signaling with media and NAT
traversal:
`https://docs.pjsip.org/en/2.15.1/overview/intro.html`.

Fit points and risks:

- PJSIP groups the SIP stack, PJMEDIA media stack, and PJNATH NAT traversal
  stack under one project, mapping directly to REGISTER, INVITE/BYE, SDP,
  RTP/RTCP, STUN/TURN/ICE, and PBX interop.
- The media plane can start narrow: PCMU/PCMA or Opus audio first, then add
  H.264/VP8 video once C5.D-WebRTC media and C5.E sync work are clearer.
- PJNATH memory is configurable; PJSIP's ICE heap guide reports one test
  reducing peak per-call PJNATH heap from about 76 KB to about 21 KB.
- License is the main risk. Upstream says GPL or alternative proprietary
  license. Do not assume BSD for product firmware.
- The full project is larger than the ticket shorthand. Open Hub reported about
  811k code lines for current pjproject. C5.D-SIP.1 should build only required
  features and disable unused codecs/UI.
- A scarthgap `pjproject` recipe is visible in the Layer Index under the
  third-party `de-ensc-bpi-router` layer. Port or vendor a pinned recipe only
  after license clearance.

### GNU osip2 + eXosip2

GNU describes oSIP as an LGPL SIP implementation, generally used with eXosip2,
with no dependencies except the standard C library:
`https://www.gnu.org/software/osip/`.

Antisip's eXosip2 documentation describes eXosip2 as a GPL-or-commercial
library extending oSIP with a higher-level SIP RFC 3261 API for registrations,
INVITE/re-INVITE, UPDATE, REFER, event packages, publication, and messaging:
`https://www.antisip.com/doc/exosip2/index.html`.

Fit points and risks:

- Small C codebase and libc-first design make RK3588 cross-build risk low.
- `meta-networking` in scarthgap already lists `libosip2` 5.3.1 and
  `libexosip2` 5.3.0 recipes, so recipe availability is better than PJSIP.
- Good fit for a signaling-only SIP gateway, registrar client, or test harness.
- eXosip2 explicitly does not contain RTP, audio interface, or SDP negotiation.
  C5.D-SIP.2 would need an additional media/NAT library stack before a real
  call can pass RTP/RTCP.
- License still needs review: `libosip2` is LGPL, but `libexosip2` is
  GPL-or-commercial.
- Splitting SIP signaling, SDP negotiation, RTP/RTCP, and ICE across multiple
  libraries increases PBX interop debugging cost on the first implementation.

## Yocto Recipe Fit

Scarthgap Layer Index checks:

| Package | Scarthgap evidence | Fit |
| --- | --- | --- |
| `libosip2` | Layer Index recipe search lists `libosip2` 5.3.1 in `meta-networking` | Green for stock layer availability |
| `libexosip2` | Layer Index recipe search lists `libexosip2` 5.3.0 in `meta-networking` | Green for stock layer availability; license still yellow |
| `pjproject` | Layer Index shows scarthgap `pjproject` recipes in the third-party `de-ensc-bpi-router` layer, not stock `meta-networking` | Yellow: use as recipe reference, but port/pin intentionally |
Recommended recipe posture for C5.D-SIP.1:

1. Gate PJSIP license first.
2. If cleared, add a pinned `pjproject` recipe or bbappend based on a reviewed
   scarthgap recipe, with unused codecs, SDL, V4L2 capture, and sample apps off.
3. Keep OpenSSL/GnuTLS, SRTP, RTP/RTCP, and PJNATH options explicit in
   `PACKAGECONFIG`; do not let recipe defaults decide NAT/media behavior.
4. If PJSIP license is not cleared, choose `libosip2`/`libexosip2` only for
   signaling and file a discovered dependency for media/NAT traversal selection.

## RK3588 Toolchain Fit

Phase 0 already has the relevant catalog entry: `rockchip-rk35xx-aarch64-gcc-11`
is the Rockchip RK3576/RK3588 aarch64 toolchain, Linaro GCC 11.3 base, target
triple `aarch64-linux-gnu`, glibc, and no NDA gate.

The existing RK35xx Yocto overlay mirrors that contract in
`yocto/meta-omnisight-camera/recipes-core/uvcvideo-xu-dispatcher/uvcvideo-xu-dispatcher_%.bbappend`:

- `ROCKCHIP_RK35XX_TOOLCHAIN_ID = rockchip-rk35xx-aarch64-gcc-11`
- `ROCKCHIP_RK35XX_TARGET_TRIPLE = aarch64-linux-gnu`
- `ROCKCHIP_RK35XX_SYSROOT = ${RECIPE_SYSROOT}`
- RK3576/RK3588 CFLAGS and LDFLAGS append `--sysroot=${RECIPE_SYSROOT}`.

Expected gotchas:

- Always resolve the platform toolchain through the catalog. Do not use host
  `gcc`, host `clang`, or a system-default CMake compiler.
- If a CMake toolchain file exists for RK35xx, pass it explicitly; for
  autotools, export `--host=aarch64-linux-gnu` plus sysroot-aware tool vars.
- Scarthgap's target libc is glibc, which fits PJSIP/osip2 better than a musl
  target, but do not link host OpenSSL, SRTP, ALSA, or codec libraries.
- Keep optional sound-device and video-device backends off in the first qemu
  smoke unless they are backed by target sysroot packages.
- Capture one recipe-level configuration log showing target triple, sysroot,
  disabled options, and selected TLS/SRTP/NAT packages.

## QEMU and Real-Hardware Exercise

This spike is documentation-only and did not run SIP code in qemu.

Recommended qemu smoke for C5.D-SIP.1/C5.F.1: build through the RK35xx
toolchain/sysroot path, boot with virtio-net, register against local Asterisk or
FreeSWITCH, place one null-audio INVITE/BYE loopback call with static RTP ports,
and record SIP transaction logs, RTP packet counters, and process RSS.

Real ATK-DLRK3588 follow-up remains required for Ethernet jitter, USB UAC/UVC
media capture, hardware video encoding, and repeated-call thermal/memory
behavior.

## Evidence

| Check | Result |
| --- | --- |
| Required spike file exists | This file: `docs/audit/2026-06-XX-sip-library-pick-spike.md` |
| Design section cross-reference | Verified: C5.D-SIP.0 is listed in `docs/architecture/2026-06-03-case5-conference-device-epic-design.md` section 3 |
| Phase 0 toolchain catalog | Verified: `configs/embedded_catalog/cross-toolchain.yaml` contains `rockchip-rk35xx-aarch64-gcc-11` |
| RK35xx Yocto sysroot pattern | Verified: `uvcvideo-xu-dispatcher_%.bbappend` passes RK35xx toolchain id, target triple, and sysroot |
| PJSIP feature source | Verified via upstream PJSIP overview and ICE heap guide |
| PJSIP license source | Verified via upstream PJSIP license documentation: GPL-or-proprietary |
| osip/eXosip feature source | Verified via GNU oSIP and Antisip eXosip2 documentation |
| Yocto recipe source | Verified via OpenEmbedded Layer Index scarthgap searches |
| Runtime exercise | Deferred: no SIP integration code and no RK3588 EVK in this documentation-only spike |

## Conclusion

**GREEN for C5.D-SIP.1 planning with a license gate.**

Use PJSIP as the production SIP stack if the GPL/proprietary license question is
cleared. It is heavier and requires deliberate Yocto recipe work, but it avoids
splitting SIP signaling, SDP, RTP/RTCP, STUN, TURN, and ICE across multiple
libraries in the first PBX path. If PJSIP cannot pass licensing, fall back to
`libosip2`/`libexosip2` for signaling only and file a separate media/NAT
dependency before attempting C5.D-SIP.2.
