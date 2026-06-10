# vendor-mirrors-nda — BSP/SDK Mirror System EPIC — Design v1

Date: 2026-06-10
Author: claude (sora session)
Status: v1 — ready for operator +2 → wave filing
EPIC: OP-491 (refined from HD.16.4 seed)
ADR: docs/adr/ADR-0043-vendor-mirror-nda-isolation-and-mirror-overlay-model.md
Related (read first):
  - configs/embedded_catalog/cross-toolchain.yaml (the seeded `vendor-mirrors-nda.omnisight.local` convention)
  - configs/embedded_catalog/_schema.yaml + backend/alembic/versions/0052_catalog_seed.py (the catalog this mirror references)
  - backend/agents/product_source.py (pin-by-immutable-ref + credential-indirection precedent)
  - docs/architecture/2026-06-02-embedded-platform-phase0-case4-epic-design.md (the 8 wave-2 leaves gated on OP-491)
  - OP-2077 (SDK inventory + NDA decision rows) · OP-2078 (storage primitive)

---

## 1. Problem

The embedded line needs real vendor BSP/SDK artifacts (Rockchip / Qualcomm / MediaTek /
Ambarella / HiSilicon / …) to build per-board Yocto images and flash EVKs. Today they exist
only as local files on the operator's machine; the catalog already *names* an NDA mirror host
(`https://vendor-mirrors-nda.omnisight.local/...`) that **does not exist**. The artifacts are:
under per-vendor NDA with **mixed** redistribution terms; large (~15GB each, ~60GB total);
needed by **multiple** projects; and sometimes **locally patched** (BSP meta-layers carry
fixes that aren't upstreamable yet). A naive "git repo of tarballs" or "throw it in the
Container Registry" approach fails on size, on NDA isolation, on cross-project sharing, and on
the patch-without-diverging requirement.

## 2. Goal + Non-Goals

**Goal**: a real, isolated, access-controlled GitLab mirror backing the seeded convention,
project-agnostic across consumers, with a mirror+overlay collaboration model. Resolves OP-2078
(storage primitive) and consumes OP-2077 (NDA decision rows). Unblocking it unblocks the 8 P0
wave-2 leaves (Qualcomm/MediaTek) gated on OP-491.

**Non-Goals**: not a public/customer distribution channel (default-closed; ADR-0043 §🔑);
not a replacement for Productizer's product catalog (the two-catalog split, §4); not full Git
Flow on vendor upstreams (mirror+overlay only); not a per-tenant runtime artifact store
(that's the delivery/product-source layer).

## 3. Confirmed decisions (operator)

1. NDA re-host posture = per-vendor mixed **YES / NO / CONDITIONAL**.
2. Location = **separate isolated** private group `vendor-mirrors-nda` (no `omnisight` inheritance).
3. Consumers = broad + **project-agnostic** (external contractors → per-vendor scoping).
4. Git-shaped BSP layers = **mirror + overlay** branches (NOT Git Flow).

## 4. Architecture

### 4.1 GitLab topology — subgroup-per-vendor, project-per-artifact-shape
```
vendor-mirrors-nda/                  (top isolated private group — membership root)
├─ catalog/                          (project-agnostic manifest repo — §4.3)
├─ _ci-templates/                    (shared drift-guard CI includes)
├─ qualcomm/                         (subgroup = access-scope boundary)
│  ├─ qcs6490-bsp                    (git-shaped: vendor/* + omnisight/* branches)
│  ├─ qcs6490-linux-sdk              (blob-shaped: thin repo + Generic Packages)
│  └─ qcs6490-npu-runtime            (blob-shaped)
├─ mediatek/ ( genio1200-bsp = meta-mediatek-bsp+meta-rity overlay ; genio1200-iot-yocto-sdk )
└─ rockchip-nda/ … ambarella/ … hisilicon/ …   (per OP-491 vendor list)
```
- **Subgroup-per-vendor**: GitLab access inherits down the tree → a Rockchip-only contractor
  added to `rockchip-nda/` at Reporter sees only that subgroup and cannot even enumerate
  `qualcomm/`. Per-vendor isolation falls out of the topology (R3). Per-vendor-SoC subgroups
  are a non-breaking later split if one vendor ever needs per-SoC contractor scoping.
- **Project-per-artifact-shape**: git-shaped (BSP meta-layers, kernel trees — full history,
  rebased overlays) and blob-shaped (15GB tarballs) have incompatible storage/lifecycle and
  must not share a repo.

### 4.2 Blob storage = GitLab Generic Packages on a dedicated volume (resolves OP-2078)
CR is wrong (prod-image-shared Synology quota, layered-dedup optimized — ADR-0038/0042).
Generic Packages > live S3-on-Synology because it reuses the *same* group/subgroup ACL as git
(no second IAM surface to drift — a contractor-over-scope vector), the same group token for
upload/download, and the sha256 the drift-guard verifies is computed over the exact uploaded
artifact. **~60GB mitigation**: a dedicated Synology share/volume separate from the prod CR
quota (an SDK upload must not evict prod image layers); retention N=2 versions per line;
pointer-only (NDA-NO) vendors store ~0 blob → live footprint likely ~30GB. S3-on-Synology =
backup tier only (§7 R6).

### 4.3 Project-agnostic catalog — two catalogs joined by `entry.id`
Keep Productizer's `configs/embedded_catalog/` + alembic 0052 canonical for *upstream/product
facts*; add a standalone `vendor-mirrors-nda/catalog` repo canonical for *mirror facts*. They
are two catalogs joined by the existing `entry.id` kebab slug (already unique, already the PK).
omnisight-ai-core + external contractors **cannot** depend on Productizer's Postgres/alembic, so
the mirror catalog is plain YAML in a git repo, pinned by SHA by every consumer (incl.
Productizer) via the `product_source.py` `pinned_ref` discipline.

`vendor-mirrors-nda/catalog/_mirror_schema.yaml` (Draft 2020-12, same discipline as
`configs/embedded_catalog/_schema.yaml`). One entry per `catalog_id`:

```yaml
# vendor-mirrors-nda/catalog/mirror/qualcomm.yaml
schema_version: 1
vendor: qualcomm
entries:
  - catalog_id: qualcomm-qcs6490-aarch64    # FK → embedded_catalog entry.id
    nda_posture: YES                          # YES | NO | CONDITIONAL
    artifact_shape: blob                      # blob | git
    mirror_url: "http://sora.services:49154/vendor-mirrors-nda/qualcomm/qcs6490-linux-sdk/-/packages/generic/qcs6490-linux-sdk/1.x/qcs6490-toolchain.tar.xz"
    sha256: "<64-hex>"                         # required for re-hosted blobs
    size_bytes: 536870912
    overlay_ref: null                         # tag/SHA on omnisight/* (git shape)
    vendor_ref: null                          # the vendor/* tag the overlay rebased onto
    portal_url: null                          # required + reachable when posture=NO
    license_clause: "QCS6490 Linux SDK redistribution §4.2 — internal re-host permitted"
    bot_pull: allow                           # allow | deny  (may bots fetch this blob in CI?)
    last_verified: "2026-06-10"
```

**Posture invariants** (CI-enforced, §4.6):

| posture | `mirror_url` | `sha256` | `portal_url` |
|---|---|---|---|
| YES | required | required | optional |
| NO | **must be null** | null | **required + reachable** |
| CONDITIONAL | required iff a sub-clause permits this artifact; else null | required-when-mirror_url-set | required-when-mirror_url-null |

The Productizer side gets only a `metadata.mirror_catalog_id: true` convention so its
drift-guard can assert (§4.6.6) that every NDA-host `install_url` has a matching mirror row —
without dragging the 30+ non-NDA OSS entries into the mirror schema.

### 4.4 Access control
| Class | Identity | Role | Scope |
|---|---|---|---|
| Core embedded engineers | named users | Developer | all vendors (author overlays) |
| Catalog maintainers | named users | Maintainer | `catalog/` |
| AI runner bots (claude-bot/codex-bot) | bot users | **Reporter** | only `bot_pull: allow` vendors |
| Per-customer repos | service acct/customer | Reporter | only their SoC's vendor |
| External contractors | named ext user, **expiring** | Reporter | one vendor subgroup |
| Sibling projects (ai-core) | sibling service acct | Reporter | `catalog/` + needed vendors |

- **Bots are NOT auto-members** of `vendor-mirrors-nda` (the separate group exists precisely so
  the `omnisight`-Maintainer role doesn't inherit). Explicit operator invariant.
- **Bot NDA-blob pull**: only for `bot_pull: allow` vendors AND only with the bwrap jail in an
  egress-restricted profile (allow-list the Generic Packages endpoint, deny the rest). The blob
  came from inside and the jail can't phone home → "bot touches NDA blob" is defensible.
  `bot_pull: deny` = human-build-only. Credentials via group deploy token through
  `git_account_ref` indirection — never inlined (mirrors `product_source.py`).
- **Pointer-only (NDA-NO) in an automated build = fail closed + loud**: the resolver raises a
  typed `PointerOnlyArtifactError` carrying `portal_url` + expected sha256 + bench stage path;
  the operator pre-stages the artifact, the runner verifies sha256 before use. Modeled on the
  existing `beaglebone-debian-image` (`install_method: noop`, `metadata.manual_step: true`).

### 4.5 Collaboration / governance for mirror+overlay git repos
```
vendor/<upstream>                      tracks upstream verbatim, NEVER patched
vendor/<upstream>/<tag>                immutable imported-snapshot tags
omnisight/<upstream>                   our overlay, rebased onto vendor tags
omnisight/<upstream>/<tag>-omni.<N>    the ONLY ref consumers may pin
```
Workflows: **(a)** import upstream snapshot → push `vendor/*` + tag (human Developer; bots
can't push); **(b)** `git rebase --onto vendor/<new-tag> vendor/<old-tag> omnisight/<name>`,
overlay minimal (one-patch-per-commit + non-upstreamable rationale header); **(c)** after merge,
cut `omnisight/<name>/<tag>-omni.N`; **(d)** a catalog MR sets `overlay_ref`/`vendor_ref` (git)
or uploads the Generic Package + `sha256` (blob), CI blocks unless the pin resolves.

**Review = GitLab MR INSIDE the isolated group. NEVER the main Gerrit, NEVER the GitHub
mirror** (ADR-0043 §6 — the #1 leak-prevention invariant). Two-person approval (1 core eng + 1
catalog maintainer; bots comment, not approve); any `nda_posture`/`license_clause` change
requires the OP-2077 distillation owner as a required approver.

Consuming projects pin `omnisight/...-omni.N` via `product_source.py` `pinned_ref`, or a
`third_party/` git submodule locked to that tag's SHA (implements the documented-but-
unimplemented `third_party/` pattern, wired into `yocto/meta-omnisight-camera/` bblayers).

### 4.6 CI / drift-guard (reuse BS.1.5 doctrine — fast lint + deeper contract shard)
A `pin-resolver` job in `catalog/` + a cross-repo variant in Productizer:
1. **Schema lint** — each `mirror/*.yaml` vs `_mirror_schema.yaml`; `catalog_id` cross-file
   unique; posture invariants (§4.3 table) hold.
2. **Blob sha256 resolves** — GET each `artifact_shape: blob` Generic Package, verify
   downloaded sha256 == manifest.
3. **Overlay pin resolves** — each `artifact_shape: git`: `overlay_ref` exists & descends from
   the recorded `vendor_ref` (proves a real rebase, not a stale tag).
4. **Pointer-only integrity** — each `nda_posture: NO`: `mirror_url` null (no accidental
   re-host) + `portal_url` present & reachable.
5. **No-leak guard** — no `vendor-mirrors-nda/*` project has a push-mirror configured (GitLab
   API check) — prevents R2 leak by config drift.
6. **Cross-repo lock-step (Productizer side)** — every NDA-host `install_url` in
   `embedded_catalog` has a matching pinned `catalog_id` in the mirror catalog.

Built on the same stdlib+yaml+jsonschema footprint as `scripts/check_catalog_schema.py`
(fast lint in the gate slot; network checks #2/#4/#5 in a longer shard). Editing the manifest
without re-uploading the blob, or bumping a blob without updating sha256, lights up red —
exactly the BS.1.5 "edit one without the other is CI-red" contract.

## 5. EPIC structure — META OP-491 + waves

```
META OP-491 vendor-mirrors-nda mirror+overlay infra  [tier:X]
├── Sub-EPIC: group + access (Wave L0, operator)        [tier:X]
│   ├── O1 create isolated group + per-vendor subgroups + catalog/ + _ci-templates/
│   ├── O2 dedicated Synology Generic-Packages volume  (= OP-2078 storage decision)
│   ├── O3 ACL per §4.4 + expiring contractor membership + MR two-person approval rules
│   ├── O4 backup group + package volume to S3-on-Synology cold tier
│   └── O6 verify NO push-mirror on any vendor project (then §4.6.5 enforces forever)
├── Sub-EPIC: catalog + schema (Wave L0, codex)          [tier:S]
│   ├── C1 catalog/_mirror_schema.yaml
│   ├── C2 catalog/mirror/{qualcomm,mediatek,…}.yaml seed rows (from OP-2077 decision rows)
│   ├── C7 this design doc        ├── C8 ADR-0043
├── Sub-EPIC: CI + resolver (Wave L1, codex)             [tier:S]
│   ├── C3 catalog/ci/check_mirror_schema.py + ci/pin_resolver.py
│   ├── C4 backend/tests/test_mirror_catalog_crossref.py (cross-repo lock-step)
│   └── C5 backend/agents/mirror_artifact.py resolver (+ PointerOnlyArtifactError)
├── Sub-EPIC: first uploads (Wave L2, operator)          [tier:X]
│   └── O5 upload NDA-YES blobs (Qualcomm QCS6490 + MTK Genio1200) → unblocks P0 wave-2 leaves
└── Sub-EPIC: consumption wiring (Wave L3, codex+operator)[tier:S/X]
    ├── C6 third_party/ submodule wiring + .gitmodules → yocto bblayers
    ├── C9 docs/operations/2026-06-10-vendor-mirror-nda-sop.md
    └── first overlay-rebase dry-run
```
Filing disciplines: tier:X for operator/META; `agent:auto class:subscription-codex` for codex
leaves; ≤3 same-class agent:auto per batch (H10); 4-AC each; gates in labels/blockedBy not
prose (H9); area labels span every deliverable dir (backend/devops/docs/embedded/tests).

## 6. Alignment with open tickets
- **OP-2078** (storage primitive) — resolved here = Generic Packages on a dedicated Synology
  volume. Close it pointing at §4.2 once O2 lands.
- **OP-2077** (SDK inventory + NDA YES/NO/CONDITIONAL rows) — feeds the C2 mirror catalog seed.
  The mirror catalog's `nda_posture` + `license_clause` columns are the durable home for those
  decision rows.

## 7. Risk register
| ID | Risk | Sev | Mitigation |
|---|---|---|---|
| R2 | NDA leak onto GitHub/Gerrit | **Crit** | isolated group, no push-mirror (O6 + §4.6.5), GitLab-MR-only review, bots not inherited |
| R1 | Synology 60GB quota / backup | High | dedicated volume (O2), N=2 retention, pointer-only ~0 blob |
| R3 | contractor over-scope | High | subgroup inheritance + expiring membership + single ACL surface |
| R4 | bot NDA exfil in net-allowed jail | High | `bot_pull` gate + egress-restricted jail; deny = human-only; pointer-only fails closed |
| R5 | overlay-rebase conflicts on churny vendor kernels | Med | minimal one-patch-per-commit overlay; consumers pin tags only |
| R6 | single-NAS SPOF | High | S3 cold backup (O4); NDA-NO `portal_url` is itself a recovery path |
| R7 | two-catalog drift | Med | §4.6.6 cross-repo guard; Productizer pins mirror catalog by SHA |
| R8 | license misclassification | High | required `license_clause` + OP-2077-owner required approver |

## 8. META-close criterion
Isolated group live; ≥1 NDA-YES vendor uploaded as Generic Packages + ≥1 pointer-only vendor
recorded; pin-resolver CI green incl. the no-leak guard; a real QCS6490 Yocto build pulls the
BSP via a pinned `third_party/` submodule + the toolchain via a resolved Generic Package and
builds `omnisight-camera-image` (regression-gated by the existing L00.5 qemu-Yocto smoke).
Real-hardware exercise on each board remains a P0.E.2 follow-up (this EPIC delivers the supply
chain, not the per-board bring-up).
