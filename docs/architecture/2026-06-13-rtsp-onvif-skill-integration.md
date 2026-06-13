# ipcam skill pack — prebuilt rtsp-onvif-server integration (R5.3)

Status: delivered 2026-06-13 (OP-2164). Repo: OmniSight-Productizer.
Server sub-project: `omnisight/rtsp-onvif-server` (GitLab project 45).

## Why this leaf reconciles two packs

There was already an `ipcam` skill pack whose scaffolds *generate* an
RTSP/ONVIF stack from scratch (live555/gstreamer dual-backend,
`rtsp_server_scaffold.py`, `onvif_device_scaffold.py`, …). The whole
point of building `rtsp-onvif-server` — a real, tested, packaged server
(RTSP + ONVIF Device/Media/Media2/Events/Imaging + WS-Discovery +
recording/search/replay, 14 C suites + 8 conformance tests, Yocto + Buildroot
packaging) — is **reuse over regeneration**.

Rather than add a competing `skill-rtsp-onvif` pack (two overlapping
IPCAM packs = confusion + double maintenance), R5.3 adds a **second,
preferred path to the existing `ipcam` pack**: consume the prebuilt
server release. The from-scratch scaffolds remain for cases a release
cannot cover.

## What R5.3 ships

- `configs/skills/ipcam/scaffolds/prebuilt_rtsp_onvif_integration.py` —
  given (product, SoC, release tag, packaging), renders:
  - a **product manifest** pinning the server repo + release tag + the
    board HAL profile (by SoC; `hal_profile_verified` is false for SoCs
    without a dedicated profile, which fall back to `generic-v4l2`),
  - the on-device **`config.yaml`** (device identity + board profile),
  - **packaging glue**: Yocto (`IMAGE_INSTALL` + `SRCREV` pin against the
    R5.1 meta-layer) or Buildroot (`BR2_PACKAGE_*` against the R5.2
    external tree),
  - a **smoke checklist** (discovery → GetStreamUri → RTSP DESCRIBE).
- `configs/skills/ipcam/tasks.yaml` — a `prebuilt_server_integration`
  task (the preferred path when a release covers the product).
- `backend/tests/test_ipcam_prebuilt_integration.py` — unit coverage.

## SoC → HAL profile

`rk3588 / rk3576 / qcs6490 / rv1126` map to their dedicated server HAL
profiles (server R3.2 / R1). Other SoCs fall back to `generic-v4l2`
(software encode) with a loud warning; a real board profile is a
server-side R6 HIL bring-up.

## Release pinning

Products pin a server release tag (R5.4 cuts the first, `v0.1.0`,
following the `v0.1.0` + `-omni.N` discipline from the vendor-mirror
model). Yocto pins via `SRCREV`, Buildroot via the package version.

## Deferred: embedded_catalog + capability-matrix row (OP-2166)

A catalog/matrix DB row for `rtsp-onvif-server` is **not** included here:
`backend/tests/test_catalog_schema.py` (BS.1.5) asserts the
`configs/embedded_catalog` yaml entry set equals the alembic `0052`
`_SEED_ENTRIES` set, and `0052` is an immutable merged migration. Adding
the entry requires a new migration (`install_method: noop` + git-source
manual_step, per the `beaglebone-debian-image` precedent) plus alignment
of the drift-guard's expected seed. Tracked in OP-2166.
