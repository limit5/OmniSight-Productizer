# Case 4 FT-C600 reference-bundle runbook -- Case 2 T2C.2 handoff

**Date filed:** 2026-06-03
**Ticket:** OP-1956 (Case 4 / C4-E.C)
**Parent:** OP-1929 (FT-C600 per-EVK Sub-EPIC)
**Status:** operator-runnable reference-bundle template; hardware acceptance
executes when the FT-C600 EVK is on the bench

---

## Why this runbook exists

Case 4 C4-E is the FT-C600 per-EVK BSP/SDK lane for the embedded camera
platform. Unlike the other Case 4 EVKs, FT-C600 already has a Case 2
customer-delivery handoff for `limit5/UVCCamera_Qt`: OP-1914 / T2C.2
documents the `customer:ft-c600` product-feed route, the expected GitHub
repository, and the current-good Case 2 base.

This runbook turns that Case 2 product-feed handoff into the Case 4 delivery
bundle checklist. It does not create new backend routes, CI jobs, schemas, or
tooling. The operator assembles one reference bundle, verifies it still points
at the Case 2 FT-C600 product source, signs and checksums the package, then
records customer-side acceptance once the physical EVK run is exercised.

**Scope lock:** this document is FT-C600 only. Other Case 4 EVKs keep their own
file-disjoint C4-X.C runbooks.

---

## Step 0 -- Confirm the upstream Case 2 product feed

Before packaging, run the Case 2 T2C.2 checks from
`docs/operations/2026-06-02-case2-uvccamera-qt-product-feed-runbook.md`:

1. Confirm the `git_accounts` row for `customer:ft-c600` exists.
2. Confirm it points at `https://github.com/limit5/UVCCamera_Qt.git`.
3. Confirm the next B2 contribution starts from the merged T2C.1 SHA
   `6302bc93` or a later commit on `main`.

Expected result: the FT-C600 product-feed row is operational before any Case 4
bundle is announced to the customer. If the row is missing, duplicated, or
points at a non-FT-C600 repo, stop and resolve the Case 2 feed first. Do not
paper over a product-feed mismatch by adding a second Case 4-only source.

---

## Step 1 -- Assemble the FT-C600 reference bundle

Create a release directory with this layout:

```text
ft-c600-reference-bundle/
  README.md
  manifest.json
  bsp/
    ft-c600-kernel-image.bin
    ft-c600-devicetree.dtb
    ft-c600-rootfs-image.tar.zst
  sdk/
    include/
    lib/
    samples/
      uvc-stitching-sample/
  apps/
    uvc-stitching
  case2/
    UVCCamera_Qt-ref.txt
    product-feed-runbook.md
  licenses/
    SPDX.json
    vendor-blob-notices.md
  acceptance/
    customer-checklist.md
    hardware-smoke-template.md
```

Required contents:

| Path | Required value |
| --- | --- |
| `README.md` | Customer-facing install summary and support contact |
| `manifest.json` | Bundle version, OP ticket, build timestamp, source refs, and artifact SHA256 values |
| `bsp/` | FT-C600 kernel image, device tree, and rootfs image from the C4-E BSP package |
| `sdk/` | Customer headers, libraries, and sample app for the stitching pipeline |
| `apps/uvc-stitching` | Case 4 stitching binary built for FT-C600 |
| `case2/UVCCamera_Qt-ref.txt` | `limit5/UVCCamera_Qt` URL plus the T2C.1-or-later commit SHA used as the reference |
| `case2/product-feed-runbook.md` | Copy of the Case 2 T2C.2 runbook for customer-delivery traceability |
| `licenses/` | SPDX JSON plus vendor blob notices |
| `acceptance/` | Customer-side checklist and hardware-smoke recording template |

Use `${VARS}` for operator-local paths, serial numbers, customer secrets, and
vault references. Do not inline tokens, private URLs, or NDA-only vendor text.

---

## Step 2 -- Stitch Case 2 and Case 4 provenance

Write `manifest.json` with explicit provenance for both source lanes:

```json
{
  "bundle": "ft-c600-reference-bundle",
  "ticket": "OP-1956",
  "customer_scope": "customer:ft-c600",
  "case2_product_feed": {
    "ticket": "OP-1914",
    "repository": "https://github.com/limit5/UVCCamera_Qt.git",
    "minimum_reference_sha": "6302bc93"
  },
  "case4_delivery": {
    "parent": "OP-1929",
    "evk": "FT-C600",
    "soc": "Fullhan MC6358",
    "bundle_kind": "reference-bundle"
  },
  "artifacts": []
}
```

Then fill `artifacts[]` with one row per delivered file:

```json
{
  "path": "apps/uvc-stitching",
  "sha256": "<sha256>",
  "source_ref": "<gerrit-change-or-git-sha>",
  "role": "case4-stitching-binary"
}
```

The Case 2 `minimum_reference_sha` is a floor, not a pin. If the live
`UVCCamera_Qt` `main` branch is newer, record the newer commit in
`case2/UVCCamera_Qt-ref.txt` and keep `minimum_reference_sha` unchanged so
operators can still see the T2C.1 closure boundary.

---

## Step 3 -- Produce checksums and signature

From the parent directory of `ft-c600-reference-bundle/`, create the archive
and verification files:

```bash
tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --owner=0 --group=0 \
  --numeric-owner -caf ft-c600-reference-bundle.tar.zst \
  ft-c600-reference-bundle/

sha256sum ft-c600-reference-bundle.tar.zst \
  > ft-c600-reference-bundle.tar.zst.sha256

cosign sign-blob \
  --output-signature ft-c600-reference-bundle.tar.zst.sig \
  --output-certificate ft-c600-reference-bundle.tar.zst.pem \
  ft-c600-reference-bundle.tar.zst
```

Expected:

- The archive contains exactly one top-level directory:
  `ft-c600-reference-bundle/`.
- `sha256sum -c ft-c600-reference-bundle.tar.zst.sha256` exits `0`.
- The cosign certificate identity matches the approved release-signing
  identity for the customer lane.
- `manifest.json` includes the archive SHA256 and the signature file names.

If the customer lane does not yet have a cosign identity, stop at checksum
generation and file the signing dependency. Do not ship an unsigned wave-2 BSP
package as customer-ready.

---

## Step 4 -- Exercise the FT-C600 hardware smoke

When the FT-C600 EVK is available, run the customer-visible smoke on the same
bundle that will be delivered:

```bash
export BUNDLE_DIR=${PWD}/ft-c600-reference-bundle
export UVC_NODE=/dev/video0
export DISPATCHER_SOCKET=/run/uvc-xu-dispatcher.sock

${BUNDLE_DIR}/apps/uvc-stitching \
  --uvc-node "${UVC_NODE}" \
  --dispatcher-socket "${DISPATCHER_SOCKET}" \
  --smoke
```

Record the result in `acceptance/hardware-smoke-template.md`:

| Evidence | Required value |
| --- | --- |
| EVK | FT-C600 physical board identifier, with serial redacted if needed |
| Bundle | Archive name and SHA256 |
| UVC node | `/dev/videoN` path used for smoke |
| Dispatcher | Socket path and FT-C600 adapter / GUID result |
| Stitching app | command, exit code, and final pass/fail line |
| Customer observer | operator or customer name / role, not personal secrets |

If the board is not present, leave this step explicitly marked
`DEFERRED: waiting for FT-C600 EVK arrival`. That is acceptable for the
documentation handoff, but it does not close the physical Exercised gate.

---

## Step 5 -- Customer-side delivery acceptance

Use the T2C.2 pattern: acceptance is a checklist plus evidence, not a hidden
operator assertion. The customer acceptance packet should contain:

1. The archive: `ft-c600-reference-bundle.tar.zst`
2. The checksum: `ft-c600-reference-bundle.tar.zst.sha256`
3. The signature and certificate: `.sig` and `.pem`
4. The completed `acceptance/customer-checklist.md`
5. The completed `acceptance/hardware-smoke-template.md`, or a clear
   `DEFERRED` marker if hardware has not arrived
6. The Case 2 product-feed reference: `case2/UVCCamera_Qt-ref.txt`

Customer-side acceptance is complete when the recipient verifies the checksum,
verifies the signature, unpacks the archive, confirms the Case 2 product-feed
reference, and records the FT-C600 smoke outcome or the hardware deferral.

---

## What this runbook DOES NOT do

- Does NOT change backend `git_accounts`, ProductSource, runner routing, CI,
  schemas, databases, security policy, or tooling.
- Does NOT create or modify FT-C600 BSP binaries, stitching binaries, flash
  scripts, Yocto recipes, or tests.
- Does NOT bypass the Case 2 T2C.2 product-feed checks.
- Does NOT certify real hardware acceptance before the FT-C600 EVK has been
  exercised.
- Does NOT cover ATK-DLRK3588, ATK-DLRV1126, Radxa Dragon Q6A, or MediaTek
  Genio 1200-EVK deliverables.

---

## Cross-references

- `docs/operations/2026-06-02-case2-uvccamera-qt-product-feed-runbook.md` --
  Case 2 / T2C.2 product-feed route that this bundle cross-links.
- `docs/operations/2026-06-XX-embedded-bringup-sop.md` -- Phase 0 embedded
  bring-up SOP shape and physical-board evidence pattern.
- `docs/architecture/2026-06-02-embedded-platform-phase0-case4-epic-design.md`
  -- Case 4 Phase 1A structure; C4-E FT-C600 may collapse into a
  reference-bundle of Case 2 deliverables.
- OP-1914 -- Case 2 T2C.2 FT-C600 product-feed runbook.
- OP-1929 -- FT-C600 per-EVK Sub-EPIC.
