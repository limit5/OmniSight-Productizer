# Vendor BSP/SDK Mirror Onboarding SOP (vendor-mirrors-nda)

Date: 2026-06-11
Status: battle-tested — distilled from onboarding 8 boards in one session
(Radxa Q6A / ATK-DLRK3588 / ATK-DLIMX93 / Rockchip RK3576 / Genio 1200 /
ATK-DLAM62x / ATK-RV1126 / ATK-DLRV1126B).
Spec: docs/architecture/2026-06-10-vendor-mirrors-nda-bsp-sdk-mirror-epic-design.md
+ docs/adr/ADR-0043. EPIC OP-491; this SOP = C9 (OP-2104).

---

## 0. Invariants (never violate)

- **NDA-leak guard (ADR-0043 §6)**: vendor-mirrors-nda content never reaches the
  main Gerrit, the GitHub mirror, or any public host. No push-mirror on any
  vendor project (CI no-leak guard enforces). Review = in-group GitLab MR only.
- **Bots are NOT members** of the group; creator/owner = sora. New projects are
  created with `jobs_enabled=false&auto_devops_enabled=false` (mirror repos must
  not spawn pipelines — a stuck Auto-DevOps job once starved the runner).
- Every artifact gets a **catalog row** in `vendor-mirrors-nda/catalog`
  `mirror/<vendor>.yaml` (schema-CI-gated). Quote enum values (`"YES"`/`"NO"` —
  bare YES/NO are YAML booleans). `catalog_id` is kebab, **no dots**.
- **nda_posture is an operator decision per artifact** (YES/NO/CONDITIONAL).
  NO ⇒ never upload the blob; pointer-only row (`portal_url` required).

## 1. Acquire the artifact

| Source | Method |
|---|---|
| Windows drive (Baidu netdisk etc.) | `/mnt/d/...` (WSL2). **Copy to local ext4 first** for multi-GB files — drvfs reads are slow and you'll read 2-3× (sha + upload + extract). Beware `(1).zip` duplicate re-downloads (same size = dup, ignore). |
| Synology NAS share | `/sharing/<id>` is a JS SPA (curl gets 119/103) — use the **resolved direct URL** `/fsdownload/<id>/<filename>` (copy from browser download). Supports Range ⇒ `curl -C - --retry 8` resumable. Establish the cookie first: `curl -c jar https://host:5001/sharing/<id>`. |
| Public git (GitHub etc.) | Pull is allowed (§6 bans only push-to-public). For multi-GB repos use **server-side `import_url`** (instance `import_sources` is normally `[]` — temporarily flip to `["git"]` via admin API, create project with `import_url`, **restore to `[]`**, poll `import_status`). |

Always record **sha256 + size** of the delivered file (compute on local ext4 copy).

## 2. Characterize BEFORE deciding (mandatory)

```sh
file X                       # extension lies: .tar.gz has been bzip2 AND real gzip
tar tf X | head -25          # tar auto-detects compression
tar tf X | awk '...count...' # full enumerate: TOTAL entries + top dirs + .repo present?
```

Decision tree (all five shapes were hit in practice):

| Shape | Signals | Recipe |
|---|---|---|
| **A. Pure source tarball(s)** (i.MX93) | small, plain kernel/u-boot trees | plain-git mirror+overlay (§4), no LFS |
| **B. repo-tool SDK** (RK3588, RV1126B) | `.repo/` + `repo.sh`; only some dirs pre-checked-out | blob (§3) + extract → `python3 .repo/repo/repo sync -l -j4` (vendor's own local checkout — **never strip `.repo` before this**) → strip `.repo` + nested `.git` → LFS git tree (§4) |
| **C. Flat built SDK with build output** (RK3576) | no `.repo`; >1M entries; `buildroot/output`, `dl`, `rockdev` | blob (§3) + extract with `--exclude` build output → verify count drops to git-manageable → LFS git tree |
| **D. Flat clean SDK** (RV1126, AM62x) | no `.repo`; 100-250k entries; no output dirs | blob + direct LFS git tree (no excludes) |
| **E. AOSP/ALPS scale** (Genio 1200) | `.repo` with AOSP projects; working tree ≳1M files | **blob-only**. A mono-git tree is infeasible; the git structure lives inside the blob (consume: extract → `repo sync -l`). |

**Hard rule: measure file count before attempting a git tree.** >~1M files ⇒
blob-only. Companion docs/HW-reference packs (Genio IoT.zip, AM62x 光盘 docs)
⇒ small LFS docs repo. Example-code tarballs ⇒ plain-git mirror+overlay.
Public tools (Ubuntu ISO, IDE installers) ⇒ do NOT mirror.

## 3. Blob upload (Generic Package)

```sh
curl -H "PRIVATE-TOKEN: $TOK" --upload-file X \
  "http://sora.services:49154/api/v4/projects/<id>/packages/generic/<pkg>/<ver>/<filename>"
```
- Use **HTTP :49154** — the TLS proxy :49156 504s on long uploads.
- `generic_packages_max_file_size` plan-limit: currently **42 GiB** (was 5;
  raise via `PUT /application/plan_limits?plan_name=default&...` if a blob exceeds it).
- There is **no SSH path** for packages (HTTP only; LFS transfer is HTTP too).
- Keep the **vendor's original filename** (even when the extension lies).
- Verify: `GET /projects/<id>/packages` + package_files sha256 matches yours.

## 4. Git tree (mirror + overlay)

```
vendor/<name>                 verbatim vendor content — NEVER patched
vendor/<name>/<ver>           immutable snapshot tag
omnisight/<name>              our overlay (patches), rebased onto vendor tags
omnisight/<name>/<ver>-omni.N the ONLY ref consumers pin
```
1. Extract; for shape B run `repo sync -l` FIRST; strip `.repo` + nested `.git`
   (`find . -name .git -exec rm -rf {} +`).
2. **Sanity check (mandatory): extracted file count ≈ tarball entry count.**
   A 363MB tarball that yields 20 files means the extract silently failed —
   abort before commit. Build the abort into the script (`[ $FC -lt N ] && exit`).
3. `git init -b vendor/<name>`; set identity (`git tag -a` needs it in fresh
   containers); `gpgSign false`; `git lfs install --local`.
4. `.gitattributes`: LFS by binary extension (archives, `*.a/so/o/ko`, images
   `*.img/ext4/bin`, ML models, `*.deb`, `*.pdf`, fonts, media) + binary-heavy
   dirs (`prebuilts/**`) + named giants (`vmlinux`). NOT `*.h/c/xml` (kernel
   has >5MB source headers).
5. Commit (`[OP-491] ...` + co-author trailers) → tags → push **LFS first**
   (`git lfs push --all origin vendor/<name>`) then branches+tags, via :49154.
   Filter token from logged output: git-lfs's locking hint **prints the remote
   URL including the token** — always `| grep -ivE 'glpat|oauth2|token'`.
6. Default branch → the `omnisight/*` branch.
7. **Verify LFS server-side via the batch API** (project statistics lag async —
   0.02GB right after a 7GB push is normal):
   `POST .../info/lfs/objects/batch {"operation":"download","objects":[{oid,size}]}`
   ⇒ expect a download action.
8. Force-push corrections: GitLab auto-protects the first-pushed branch —
   `DELETE /projects/<id>/protected_branches/<name>` first.

## 5. Catalog row + CI

Add to `vendor-mirrors-nda/catalog` `mirror/<vendor>.yaml` (blob row: mirror_url
+ sha256 + size_bytes; git row: vendor_ref + overlay_ref). Validate locally
(`python3 ci/check_mirror_schema.py`), push — catalog CI (schema-lint +
pin-resolver + strict) runs on the group runner. pin-resolver verifies blobs by
**1-byte Range GET** (reachability+size; sha is verified at upload time).

## 6. Upstream updates

- **Git upstreams** (Radxa): the scheduled daily sync (ci-templates,
  `sync/upstream-mirrors.yaml`) ls-remote-detects new tags, pushes `vendor/*`,
  opens a rebase-overlay issue. Add new mirrors to the yaml.
- **Tarball-release vendors** (AlienTek, Rockchip, MTK): new release = repeat
  this SOP with the new version (new blob version + new `vendor/<name>/<ver>`
  tag + rebase overlay + bump catalog row). No polling possible.

## 7. Quick reference — what exists today

| Project | Shape | Contents |
|---|---|---|
| radxa/kernel · radxa/linux-qcom | B(import)+A | QCS6490 kernel mirror + packaging overlay |
| alientek/atk-dlrk3588-linux6.1-sdk | B | blob 8.5GB + LFS tree 18G/233k files |
| alientek/atk-dlimx93-{uboot,kernel} | A | plain-git mirror+overlay |
| rockchip-nda/rk3576-linux-sdk | C | blob 18GB + excluded-output tree 14G/183k |
| mediatek/genio1200-alps-sdk · -docs | E + docs | blob-only 39GB + docs LFS repo |
| alientek/atk-dlam62x-{sdk,docs,examples} | D + docs + A | TI-SDK tree + docs + Qt examples |
| alientek/atk-rv1126-sdk | D | blob 5.5GB + LFS tree 146k files |
| alientek/atk-dlrv1126b-sdk | B | blob 6.7GB + LFS tree 219k files |

Operator prerequisites (once): group runner online (this WSL box, user-mode
systemd + linger), `VMNDA_SYNC_TOKEN` group CI var, daily schedule on
ci-templates, git-lfs user-local at `~/.local/bin/git-lfs`.
