---
id: ADR-0043
title: Vendor BSP/SDK mirror — NDA isolation + mirror/overlay collaboration model
status: Proposed
date: 2026-06-10
---

# ADR-0043 — `vendor-mirrors-nda`: NDA isolation & mirror+overlay model

**Status**: Proposed (2026-06-10). Fixes the storage/isolation/collaboration model for vendor BSP/SDK artifacts before any NDA blob is uploaded.

**Relates**: ADR-0042 (image distribution / GitLab CR), the 5-branch Git Flow + 3-remote governance (GitLab primary → GitHub one-way mirror; Gerrit review layer), OP-491 (vendor-mirrors-nda seed), OP-2077 (SDK NDA decision rows), OP-2078 (storage primitive). Full design: `docs/architecture/2026-06-10-vendor-mirrors-nda-bsp-sdk-mirror-epic-design.md`.

## Context

The embedded line (Phase 0 + Case 4/5 + P0.E.2 real-hardware acceptance) needs vendor BSP/SDK artifacts (Rockchip / Qualcomm / MediaTek / …) on hand to build per-board Yocto images. The catalog already points NDA artifacts at `https://vendor-mirrors-nda.omnisight.local/...` (`configs/embedded_catalog/cross-toolchain.yaml`), but no such mirror exists. These artifacts are (a) under per-vendor NDA with mixed redistribution terms, (b) large (~15GB each, ~60GB total), (c) needed by **multiple** projects (OmniSight-Productizer, omnisight-ai-core, future per-customer repos, external contractors, sibling internal projects), and (d) sometimes locally patched (BSP meta-layers).

Operator-confirmed: per-vendor mixed NDA posture (YES/NO/CONDITIONAL); a **separate isolated** GitLab group (not a subgroup of `omnisight`); broad project-agnostic consumption with per-vendor contractor scoping; and a mirror+overlay (not Git Flow) model for the git-shaped BSP layers.

## Decision

1. **Isolation** — a separate top-level private GitLab group `vendor-mirrors-nda`, with its own membership root (no inheritance from `omnisight` id 42). Subgroup-per-vendor is the access-scope boundary; project-per-artifact-shape inside it (git-shaped BSP layers vs blob-shaped SDK tarballs).

2. **Storage primitive = GitLab Generic Packages on a dedicated Synology volume** (resolves OP-2078). NOT the Container Registry (prod-image-shared quota, layered-dedup optimized — ADR-0038/0042). NOT live S3-on-Synology (a second IAM surface that drifts from group ACL). S3-on-Synology is the **backup** tier only.

3. **Two catalogs joined by `entry.id`** — Productizer's `configs/embedded_catalog/` + alembic 0052 stays canonical for *upstream/product facts*; a standalone `vendor-mirrors-nda/catalog` repo (plain YAML, no DB) is canonical for *mirror facts* (`nda_posture`, `mirror_url`, `sha256`, `overlay_ref`, `license_clause`, `portal_url`, `bot_pull`). Every consumer — including Productizer — **pins the mirror catalog by SHA** (the existing `product_source.py` `pinned_ref` discipline). omnisight-ai-core and contractors can consume it without touching Productizer's Postgres/alembic.

4. **NDA posture is per-vendor mixed, CI-enforced**: `YES` ⇒ re-host blob (`mirror_url`+`sha256` required); `NO` ⇒ pointer-only (`mirror_url` null, `portal_url` required + reachable); `CONDITIONAL` ⇒ per-artifact split. An automated build hitting a `NO` entry **fails closed** with a typed `PointerOnlyArtifactError`; the operator pre-stages the artifact on the bench and the runner verifies sha256 (mirrors the existing `beaglebone-debian-image` `noop`+`manual_step` precedent).

5. **Mirror + overlay branching** for git-shaped layers: `vendor/<upstream>` tracks upstream verbatim (never patched); `omnisight/<upstream>` carries our minimal patches rebased onto vendor tags; consumers pin only `omnisight/<upstream>/<tag>-omni.N` tags (via `pinned_ref` or a `third_party/` submodule).

6. **Review stays inside the isolated group as GitLab MRs — NEVER the main Gerrit, NEVER the GitHub mirror.** This is the load-bearing leak-prevention invariant: the main Gerrit + GitLab→GitHub one-way mirror are shared/partly-public surfaces (ADR-0042); NDA diffs must not reach them. A CI guard asserts no `vendor-mirrors-nda/*` project has a push-mirror configured. Two-person MR approval (1 core eng + 1 catalog maintainer); bots may comment, not approve.

7. **AI runner bots are Reporter-only and not auto-members.** A bot may pull a vendor's NDA-YES blob in CI only when that vendor is flagged `bot_pull: allow` AND the build runs the bwrap jail in an egress-restricted profile (allow-list only the Generic Packages endpoint). `bot_pull: deny` vendors are human-build-only. Credentials via a group deploy token through the existing `git_account_ref` indirection — never inlined.

## 🔑 Mitigation policy — the NDA-leak trigger

**INVARIANT**: no `vendor-mirrors-nda/*` content (git or blob) may ever reach the main Gerrit, the GitLab→GitHub one-way mirror, or any public surface. Enforced by: (a) separate group with no membership inheritance; (b) overlay review via in-group GitLab MR only; (c) a CI no-leak guard that fails if any vendor project has a push-mirror; (d) `license_clause` + OP-2077-owner required-approver on any posture change. If a future need arises to expose any vendor artifact externally, that requires explicit vendor written permission re-confirmed at that point — the default is closed.

## Consequences

- **Positive**: one ACL surface (GitLab group) for both git + blobs; project-agnostic catalog consumable without Productizer's DB; contractor scoping is a single subgroup-membership; NDA-leak paths are closed-by-construction and CI-guarded; local BSP patches are possible without upstream divergence.
- **Costs**: a second GitLab group + a dedicated Synology volume to operate + back up; overlay rebases require human review (bots can't author); pointer-only NDA artifacts need an operator bench-staging step in automated builds.
- **Risks retained (mitigated in the design §7)**: single-NAS SPOF (S3 cold backup + portal_url recovery), overlay-rebase conflicts on churny vendor kernels (minimal one-patch-per-commit overlays), license misclassification (required `license_clause` + required approver).
