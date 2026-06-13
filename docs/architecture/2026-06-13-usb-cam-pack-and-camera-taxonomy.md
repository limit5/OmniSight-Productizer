# usb-cam pack + the camera product taxonomy (decoupling decision)

Status: decided + delivered 2026-06-13. Repo: OmniSight-Productizer.
Components: `omnisight/rtsp-onvif-server` (GitLab 45), `omnisight/uvc-uac-app`
(GitLab 46).

## The two independent axes

The earlier confusion ("bury uvc under ipcam, or its own pack?") dissolves
once two axes are kept separate:

1. **Components (reusable assets)** — already decoupled, and stay that way:
   - `rtsp-onvif-server` = network camera (RTSP/ONVIF over Ethernet).
   - `uvc-uac-app` = USB camera+mic gadget (the board *is* a USB device).
   These are independent repos, independent releases, zero cross-dependency.

2. **Skill packs (product archetypes)** — the actual decision. A pack maps
   to *what the customer wants to build*, not to a component. The
   scaffolder stamps a product from a pack; packs *consume* component
   releases via a prebuilt integration.

## Decision

| Customer product | pack | consumes |
|---|---|---|
| Pure USB camera (laptop module / Windows Hello / AI streaming stick) | **`usb-cam`** (new) | uvc-uac-app |
| Network IP camera | `ipcam` (existing) | rtsp-onvif-server |
| Dual-function (conferencing, medical/industrial) | **composition** (`depends_on_skills: [ipcam, usb-cam]`) | both |

- **Not** buried under `ipcam`: `ipcam` semantically means *network camera*;
  forcing a USB-only product through it inherits RTSP/ONVIF/recording/
  discovery baggage and wrong defaults. Single-function products must be
  clean — this alone vetoes burying.
- **Not** one fat merged pack: the dual case is *composition* (the platform
  already supports `depends_on_skills`), not a third heavyweight pack.
- **No umbrella pack yet**: a thin `av-camera` umbrella is justified only
  when a real dual-function customer appears; until then, compose.

## The `usb-cam` pack (this change)

`scaffolds/prebuilt_uvc_uac_integration.py` mirrors the ipcam pack's
prebuilt integration: given (product, SoC, release tag, packaging, MODE),
it renders the product manifest (pinned firmware + board profile), the
on-device profile, packaging glue (Yocto/Buildroot/Debian), and a smoke
checklist. **MODE** encodes the product split directly:

- `UVC_ONLY` — pure camera, no audio endpoint (the laptop-cam / AI-stick
  segment that does **not** need ipcam features).
- `UVC_UAC` — camera + mic/speaker (conferencing, medical/industrial).

VPU-awareness: no-VPU SoCs (i.MX93, AM62x) render MJPEG/uncompressed only,
with a product-decision note. Unprofiled SoCs render a `(BOARD)` stub +
a loud HIL-validation warning.

## The dual-function engineering substrate (the real gap — OP-2168)

"Dual-function" is **not** "install both packs." A device that is both an
IP cam and a USB cam must **share one `sensor → ISP → encode` capture
pipeline and fan out to two sinks** (a network/RTSP sink and a USB/UVC
sink) — you cannot run two independent capture pipelines fighting one
sensor. Good news: both components' front ends already agree (DMABUF
`camera → encoder`); the divergence is only the sink. So the future
umbrella's only real job is the fan-out glue.

But there is a genuine **capability gap below the application layer**, on
high-end SoCs: RK3588 has **dual ISP + multiple MIPI-CSI inputs**, so the
real opportunity (and complexity) is multi-path capture pushed to the
hardware limit (multiple sensors / multiple full-res streams through both
ISPs simultaneously). We have **no proven skill and no on-hardware
validation** that we can saturate this. This is a BSP/ISP/media-controller
(`rkisp`/`rkcif`/V4L2 media graph) concern that sits *under* both
components — neither owns the multi-sensor topology today. Low-end SoCs:
single-path / serialized is acceptable. High-end: multi-path is the
differentiator we have not proven. Tracked as **OP-2168** (research +
HIL, hardware-gated). It is the substrate the `av-camera` umbrella's
fan-out will eventually stand on — design the encoder DMABUF interfaces so
fan-out is "attach," not "rewrite," but do not build it on WSL.

## SoC coverage (separate, third axis)

uvc-uac-app targets RK3588 first (a high-end board picked to prove
end-to-end), with a HAL designed so a new SoC = one profile. Whether it
reaches down to lower SoCs (RV1126) or other high-end parts is a
**board-profile + HIL** matter (component R6-style matrix), independent of
pack decomposition. Do not conflate it with the pack decision.
