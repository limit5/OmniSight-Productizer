---
audience: architect
status: accepted
date: 2026-04-28
priority: HD (post-BP) — sister ADR to hd-hardware-design-verification.md
related:
  - docs/design/hd-hardware-design-verification.md
  - docs/design/blueprint-v2-implementation-plan.md
  - docs/design/w11-w16-as-fs-sc-roadmap.md
  - TODO.md (Priority HD section, HD.16-HD.21)
---

# ADR — HD Daily Scenarios & Platform Pipeline

> **One-liner**：把 HD core ADR 的「客戶 design 分析」鏡頭拉開、補上**14 vendor × 50 SoC platform pipeline + 18 個漏寫的 daily scenario 叢集**（多客戶 NDA 隔離 / Lifecycle / CVE / 量產 / OTA / Bring-up / 跨 SoC port / Multi-SoC topology / Module-Lens-ISP tuning / Firmware blob / Compliance SBOM / Build farm / 部署多樣性 / 反假冒 / AI Companion）。對應 TODO 的 HD.16-HD.21。

---

## 1. 為何需要姊妹 ADR

`hd-hardware-design-verification.md`（Core ADR）規劃了 4 group / 15 phase 的核心能力：HDIR / EDA parser / PCB SI / 反向設計 diff / Sensor KB / AVL forced-sub / FW stack adaptor / HIL emulator / RAG / Compliance retest / Workspace UI / Multi-agent / Ledger / Test / Rollout。

但 Core ADR 預設**「我方一份 manifest pin、所有客戶一起用」「客戶單次上傳 design 系統一次性分析」**這樣的單客戶 / 單上傳 mental model。經第二輪深度盤點、發現以下**結構性漏點**：

1. **多客戶 / 多專案 / NDA 不對等**（最大架構漏點 — 影響 schema）
2. **5-10 年 product life cycle**（embedded 硬約束、整段沒寫）
3. **CVE / security update sync workflow**（沒寫）
4. **量產 / EMS / Factory programming / OTA**（沒寫）
5. **Bring-up workflow（首次通電 → kernel boot）**（HD 應該擁有但沒寫）
6. **跨 SoC port / DevKit-to-Production fork**（高頻起點漏寫）
7. **Sensor + Lens + Module 三方 + ISP tuning workflow**（KB 維度錯）
8. **Firmware blob ecosystem**（GPU / NPU / Codec / WiFi / BT — 全沒寫）
9. **Multi-SoC product topology**（main + co-processor 沒寫）
10. **Compliance / SBOM / functional safety qualification**（HD.10 寫得太淺）
11. **Build farm / cache / 三層隔離**（NDA-vs-NDA 互染風險沒處理）
12. **部署多樣性**（air-gapped / self-hosted / data residency / distributor proxy 沒寫）
13. **反假冒 / chip authenticity**（沒寫）
14. **AI Engineer Companion**（embedded 工程師 daily companion 沒寫）
15. **Vendor 生態複雜度**（非 GitHub source / vendor 多 branch / distributor 加值層）
16. **Lifecycle vendor 倒閉 / 收購 / EOL**（FocalTrans 等小 vendor 風險）
17. **License compliance / GPL audit**（ship-time 檢查沒寫）
18. **Per-tenant role matrix / patch propose-upstream toggle**（多人協作沒寫）

這 18 類大致分三層：架構性（影響 schema、進 Core ADR 補丁）/ workflow（新 phase 群組）/ 生態完整度（後續迭代）。本 ADR 把整套整合進**HD.16-HD.21 六個新 phase 群組**+ 對 Core ADR 的 schema 增量。

---

## 2. 14 Vendor × 50 SoC 預設清單

本系統首發即支援以下 SoC（未來會增減）：

| Vendor | SoC | Tier | License |
|--------|-----|------|---------|
| **Rockchip** | RV1103/B / RV1106/B / RV1126/B / RK3562/J / RK3568 / RK3572 / RK3576 / RK3588/S/M/J / RK3688 | Tier 1 共用（aarch64 主流）/ Tier 1 共用（armhf RV 系） | gpl_with_blobs |
| **TI** | AM625X | Tier 1 Linaro aarch64 | open (Yocto BSP) |
| **NXP** | i.MX93 / i.MX6ULL / i.MX913 | Tier 1 Linaro（aarch64 / armhf） | open (Yocto + IMX BSP) |
| **MTK** | Genio 350 / 520 / 720 / 1200 | Tier 1（Genio 350-720）/ Tier 3（Genio 1200 NDA bundle） | open + nda partial |
| **nVidia** | Jetson Nano / Orin Nano / Orin NX / AGX Orin | Tier 3 vendor-locked (L4T) | open (NDA bits) |
| **Novatek** | NT98530 / NT98528 / NT98566 / NT98560 | Tier 2 vendor-tuned | nda |
| **Ambarella** | CV2S / CV22S / CV25S / CV5S / CV52S / CV72S / CV75S / CV7 | Tier 2 vendor-tuned (大量 SIMD 客製) | nda |
| **HiSilicon** | Hi3559A / Hi3519A / Hi3516DV / Hi3516CV | Tier 2 vendor-tuned (uClibc / glibc fork) | nda + EOL（部分） |
| **Realtek** | RTS3901 / RTS3902 / RTS3916N / RTS3918N | Tier 2 vendor-tuned | nda |
| **SigmaStar** | SSD268G | Tier 2 vendor-tuned (MStar lineage) | nda |
| **Qualcomm** | QCS6490 / QCS8550 | Tier 1 partial / Tier 3 (Hexagon LLVM partial) | open + nda mix |
| **Sunplus** | C3V | Tier 2 vendor-tuned | nda |
| **ST** | STM32MP135 / STM32MP157 | Tier 1 Linaro armhf | open (OpenSTLinux) |
| **FocalTrans** | FT-C600h | Tier 2 vendor-tuned | nda（小眾，sunset 風險） |
| **Broadcom** | BCM2712 (RPi 5) / BCM2711 (RPi 4) | Tier 1 Linaro aarch64 / armhf | open（RPi 完全開放） |

合計 **14 vendor、50+ SoC variants**。

### 2.1 增減策略

- **新增 SoC**：走 HD.16.16 Add-Platform Wizard（5 step：Identify → Source → Toolchain → Spec draft via vision LLM → Onboard）
- **撤掉 SoC**：spec 改 `lifecycle.status: sunset`、frozen archive policy 啟動、客戶端 catalog 移歸檔區但繼續可用
- **vendor 換 hosting**：source connector 自動切換（GitConnector → GerritConnector）、mirror migration 走 HD.16.8 抽象層

---

## 3. Git Repo 拓撲

```
github.com/omnisight/                        (public org)
├─ platforms-manifest                        # meta-repo, YAML manifest
├─ platforms-spec                            # per-SoC platform.yaml registry
├─ vendor-mirrors-public/                    # open-license vendor SDK pristine
│  ├─ rockchip-{rv1103,rv1106,rv1126,rk3562,...}-bsp
│  ├─ ti-am625x-processor-sdk-linux
│  ├─ nxp-{imx93,imx6ull,imx913}-yocto
│  ├─ mtk-genio-{350,520,720}-bsp
│  ├─ nvidia-jetson-l4t
│  ├─ rpi-firmware
│  ├─ stm32mp-openstlinux
│  └─ qualcomm-qcs-{6490,8550}-bsp           # public bits only
├─ vendor-patches/                           # our delta on pristine
│  ├─ rockchip-rk3588-patches
│  └─ ...
└─ vendor-toolchains-meta/                   # toolchain manifest

github.com/omnisight-private/                (private org, NDA-only)
├─ vendor-mirrors-nda/
│  ├─ ambarella-cv{2s,22s,25s,5s,52s,72s,75s,7}-sdk
│  ├─ hisilicon-hi{3559a,3519a,3516dv,3516cv}-sdk
│  ├─ novatek-nt{9853x,9856x}-sdk
│  ├─ sigmastar-ssd268g-sdk
│  ├─ realtek-rts{3901,3902,3916n,3918n}-sdk
│  ├─ mtk-genio-1200-nda
│  ├─ sunplus-c3v-sdk
│  ├─ focaltrans-ft-c600h-sdk
│  └─ qualcomm-qcs-{6490,8550}-nda-bits
├─ vendor-patches-nda/                       # NDA SDK 對應 patches
└─ customer-overlays/                        # per-customer 私有 patch（HD.17.3）
   ├─ <customer-id-1>/
   └─ <customer-id-2>/
```

### 3.1 為什麼這樣分（4 層分離 + 1 個 manifest 串聯）

- **Vendor pristine 不污染**：mirror = 上游 HEAD + commit hash 對得上 vendor release，IP 線清楚、客戶 audit 拿得出證據
- **Patch stack 獨立 review**：我方所有 fix 在 patches repo、走 PR + O7 dual-sign
- **NDA 物理隔離**：public org 看不到 NDA repo URL、PEP gateway 同步擋
- **Customer overlay 私有化**：客戶 patch 存 private-per-customer、ACL 鎖 tenant
- **增減 SoC = 改 manifest**：不動 repo 結構

### 3.2 三層 Manifest 疊加（HD.17.1）

```yaml
# base.yaml — 我方 baseline，所有客戶共用起點
platforms:
  - id: rk3588
    spec: platforms-spec/rockchip/rk3588.yaml
    mirror_pin: abc123def       # vendor pristine commit
    patch_pin: f00ba12          # 我方 baseline patch
    toolchain_ref: linaro-aarch64-13.2

# customer-overlay/<id>.yaml — per-customer 客製 layer
platforms:
  - id: rk3588
    customer_patches: rockchip-rk3588-customer-acme   # ACME 客戶私有 patch
    customer_patch_pin: aabbcc

# project-pin/<id>.yaml — per-project SDK 版本鎖（不可逆）
platforms:
  - id: rk3588
    inherits: customer-overlay/acme.yaml
    locked_at: 2026-04-15T10:00:00Z
    immutable: true                                   # production tag 永不刪
```

實際 build 時 manifest resolver 依序套疊：base → overlay → pin。

---

## 4. Toolchain 三層分類詳解

| Tier | 對應 vendor | Storage | Sync 節奏 |
|------|-------------|---------|----------|
| **Tier 1 Shared Canonical** | Rockchip / TI / NXP / MTK Genio 350-720 / nVidia host-side / RPi / ST / Qualcomm public bits | OCI image + SHA256 lock，rebuild from source recipe + pin commit | 半年 1 次 |
| **Tier 2 Vendor-Tuned** | HiSilicon / Ambarella / Novatek / SigmaStar / Realtek / Sunplus / FocalTrans / MTK Genio 1200 NDA | Vendor binary tarball mirror to private artifact store（S3 + signed URL or JFrog Artifactory），不重 build | Vendor 出新版時 sync |
| **Tier 3 Vendor-Locked** | NVIDIA Jetson L4T / Qualcomm Hexagon DSP | OCI image，**version 必須 = SDK version、不能 mix** | SDK 同節奏 |

### 4.1 Toolchain 共用率分析

- **Tier 1 共用**：~30 顆 SoC 共用 ~5 條 Tier 1 toolchain（aarch64 / armhf / arm-none-eabi / RISC-V 預留 / aarch64-musl 預留）
- **Tier 2** ~12 顆 SoC、各廠 vendor-tuned 不互通
- **Tier 3** ~6 顆 SoC、SDK 同節奏

→ **Tier 1 共用節省 ~80% toolchain 維運成本**。

---

## 5. Upstream Sync Pipeline（5-stage + 5-level escalation）

```
[Daily cron / vendor webhook] → vendor-sync-bot 派發
   │
   ▼ Stage 1: Vendor upstream check
   ├─ 拉 vendor 真實 source（Git / Gerrit / FTP / NDA portal / S3 / ManualUpload — HD.16.8 abstract connector）
   ├─ diff vs 我方 mirror HEAD
   └─ no change → noop / changed → Stage 2
   │
   ▼ Stage 2: Mirror update PR
   ├─ NDA mirror：先跑 secret scanner（防 vendor 不小心夾 customer key）
   ├─ 開 PR 到 vendor-mirrors-* repo（pristine、auto-merge）
   │
   ▼ Stage 3: Patch rebase 試
   ├─ 把 vendor-patches 的 patch series 重套到新 mirror HEAD
   ├─ clean apply → Stage 4
   └─ conflict → 派 BP.B Guild 的 vendor-rebase-bot → 通知 maintainer
   │
   ▼ Stage 4: Integration build + HD cross-check
   ├─ 1 顆 representative dev board 的 build smoke
   ├─ HD HW↔FW cross-check（DTS / driver / sensor 對齊）
   ├─ HD sensor KB 比對（vendor 是否新增 / 移除 sensor 支援）
   ├─ ABI break detection（Tier 2/3 重點）
   └─ green → Stage 5 / red → block + 派工
   │
   ▼ Stage 5: Manifest bump + release
   ├─ 開 PR 到 platforms-manifest，bump pinned_commit
   ├─ O7 dual-sign：merger-agent-bot +2 + non-ai-reviewer +2
   ├─ tag release（per-platform tag，如 rk3588-2026.05.01）
   └─ 廣播到 Platform Catalog UI 的 sync feed
```

### 5.1 五級 Escalation

| Level | 狀況 | 處理 |
|-------|------|------|
| **L1** | vendor pristine 純改、patch 套得上、build 過 | 全自動 + dual-sign + tag |
| **L2** | patch rebase clean 但 minor warning | auto-merge + 標 yellow |
| **L3** | patch rebase conflict | 派 vendor-rebase-bot 嘗試 → 失敗交 human |
| **L4** | build red / cross-check 抓到 ABI break | 鎖在 staging branch、不 bump manifest |
| **L5** | NDA leak detected (secret scan 抓到) | **自動 revert mirror PR + alert + audit ledger（R41）** |

### 5.2 Distributor Layer 處理

部分 vendor（Ambarella / HiSilicon / SigmaStar / Novatek / Realtek）在台灣常透過代理商（Maxim Group / 大聯大 / 文曄）拿 SDK：

```yaml
sdk:
  source: distributor              # vendor_direct | distributor | community
  distributor: maxim_group
  distributor_pinned: r4.2-mxm03
  vendor_equivalent: r4.2
  drift_from_vendor: minor         # none | minor | major
```

當 `drift_from_vendor != none`、Sync workflow 多跑一階段：把代理商版 vs 原廠版 diff 跑出來、列入 audit ledger（責任分流）。

---

## 6. 18 Daily Scenario 叢集深度規劃

### A. 多客戶 / 多專案 / NDA 隔離 → HD.17

**實況**：50 客戶 × 100 專案 × 50 SoC、每專案各自 SDK pin、客戶間 patch 不可見、NDA 不對等隔離、build cache 不互滲。

**架構決策**：
- **三層 manifest 疊加**（§3.2）取代「單一 pin」假設
- **per-project SDK pin 不可逆**：production tag commit + toolchain OCI image 雙鎖、retention=forever
- **customer overlay 進 private repo**、ACL 鎖 tenant
- **NDA boundary enforcement** 滲透到 catalog UI / PEP gateway / build farm cache（三層 cache 隔離 — §J）
- **Per-tenant role matrix**：HW / FW / ISP tuning / Build / QA / 法務 / Upstream-propose 7 種 role
- **Patch propose-upstream toggle**：per-patch 決定要不要回上游（保護客戶 IP）

### B. Lifecycle / LTS / EOL → HD.18.1-18.4

**實況**：embedded IPC / dashcam / 工業設備生命週期 5-10 年、Hi3516CV 已 EOL、客戶 product 還在 field 跑、vendor 收購 / 倒閉 / 停業（FocalTrans 風險）、客戶量產 image 已 ship 之後 CVE 要 backport。

**架構決策**：
- spec 內 `lifecycle.status` + `lifecycle.support_until`
- **frozen archive policy**：sunset 後 mirror + toolchain image 移 cold storage（10 年保留、可付費取出）
- **OCI image 永久鎖**：每個 production tag 對應的 toolchain 不刪、registry retention=forever
- **年度 reproducibility audit**：隨機抽 5 顆 sunset SoC 重 build、bit-exact 重現驗證

### C. CVE / 漏洞 sync → HD.18.5-18.10

**實況**：「我這條 product line 用 RK3588 + Linux 5.10、上週那個 io_uring CVE 影響我嗎？」是客戶最痛問句。

**架構決策**：
- **CVE feed ingestion**：訂閱 NVD / OSV / 各 vendor security advisory
- **CVE → SBOM 自動關聯**：列影響的 platform / 客戶 / project / 已 ship device fleet
- **Auto-PR backport bot**：vendor 推 fix → 自動產生 customer-overlay backport patch
- **EOL community-backport pool**：對 Hi3516CV 等 EOL SoC best-effort（明示不擔保）
- **SLO**：high-severity CVE 14 天內出 backport 提案（R43 mitigation）

### D. 量產 / EMS / Factory Programming → HD.21.1

**實況**：R&D image → mass production handoff、EMS / ODM 拿 image 燒進 chip、per-device unique injection（MAC / serial / cert / TPM key / per-device sensor calibration）、量產線測試夾具整合。

**架構決策**：
- **Production Handoff workflow**：R&D 鎖 → Production tag（不可逆）→ EMS access bundle（含 flashing tool config + per-device data injection script + golden test sequence）
- **Vendor flashing tool integration**：rkdeveloptool / fastboot / NXP MfgTool / SP_FlashTool / Ambarella amboot
- **量產 vs 工程版自動隔離**：production tag 自動 strip engineering boot key、強制走 signed boot chain

### E. OTA / Field Update → HD.21.2

**實況**：10 萬台 device 在 field、要 patch、frequent OTA、Yocto SWUpdate / Mender / RAUC / AOSP A/B / 自家 OTA — stack 多元。

**架構決策**：
- **HD 不直接做 OTA agent**（生態太碎）、但做 **OTA package generator**：build output → SWUpdate / Mender / RAUC / AOSP A-B image bundle 自動產出
- **Signature pipeline**：簽名走 AS Token Vault 同 KMS
- **Delta update**：頻寬有限 IoT camera 走 delta
- **Rollback declaration**：A/B / dual-bank / single-image 三策略對齊 spec

### F. Bring-up / First-boot → HD.19.1-19.4

**實況**：第一次拿到 production PCB、power-on → boot ROM → first console → kernel boot → userspace；70% bring-up 卡關是 power / clock / I2C / DDR — 全是 HD 可加值的領域。

**架構決策**：
- **Bring-up Checklist generator**：依 SoC boot chain 客製
- **Live console capture**：W14 Live Sandbox Preview 內接 USB-serial / J-Link probe → 即時 boot console → AI parse 卡點
- **Power-on sequence verifier**：上傳示波器 capture → 比對 schematic 設計 sequence
- **70% 卡點 decision tree**：power / clock / I2C / DDR 對 schematic 提示

### G. 跨 SoC Port + DevKit-to-Production → HD.19.5-19.8

**實況**：兩個高頻起點 — (1) 跨 SoC 升級換型（RK3568 → RK3588 性能升級 / Ambarella → Rockchip 缺貨潮）；(2) 90% 客戶從 DevKit 起步（Rock 5B / Jetson Orin Devkit / RPi 5）→ 改 BOM / 縮 PCB → production board。

**架構決策**：
- **Cross-SoC port assistant**：兩 platform spec diff → 必修項清單 + 工時估算 + risk flag
- **跨 vendor 換型專屬路徑**：缺貨潮高頻換型預載 known migration recipe
- **DevKit catalog**：spec 加 `reference_devkits: []`
- **DevKit fork wizard**：「以 Rock 5B 為起點 fork」一鍵進 HD.4 reference diff workflow

### H. Sensor + Lens + Module + ISP Tuning → HD.20.3-20.6

**實況**：HD.5 Sensor KB 維度錯。同 IMX415 + 不同 module 廠（舜宇 / 大立光 / 玉晶光 / 三月光電 / 立景光電）+ 不同 lens（廣角/長焦/魚眼）= LSC 完全不同。ISP tuning 1-2 週 / sensor、產 vendor binary blob、版本管理難。

**架構決策**：
- **HD.5 Sensor KB schema 升維**：`(sensor, lens, module_vendor) → tuning_session_id`
- **Module 廠 KB**：5 大 module vendor metadata
- **ISP Tuning Workbench**：lab 設備 metadata + tuning binary 版本 + before/after compare + 每 iteration 進 N10 ledger
- **Lab 環境**：24-color chart / lux box / DXO test patterns / golden image 對齊

### I. Multi-SoC Topology + Firmware Blob → HD.20.1-20.2 + HD.20.7-20.10

**實況**：embedded 系統實況 — (1) 多 SoC 共構（RK3588 + STM32MP135 / iMX9 + iMX RT / Ambarella + STM32 GPS-IMU）；(2) vendor SDK 含一堆 closed-source blob（Mali GPU userland / Hexagon DSP firmware / NPU model / Codec lib / WiFi/BT firmware）— 各自 license + redistribution 限制 + cadence 與 SDK 不同步。

**架構決策**：
- **Multi-SoC project schema**：`project.platforms: [main, co_processor_1, ...]`
- **Inter-SoC interface declaration**：UART / SPI / RPMSG / IPC mailbox 進 schematic cross-check
- **Firmware blob spec**：`firmware_blobs[]` field、每筆獨立 pin
- **(BSP-ver, blob-ver) 兼容矩陣**：spec 顯示「此 BSP 相容的 blob 版本範圍」、catalog UI 警示 incompatible
- **Blob license tag**：每筆 blob 標 license + redistribution 限制（防誤散布）

### J. Compliance / SBOM / Functional Safety → HD.21.3

**實況**：HD.10 寫得太淺。EU CRA 2027 強制 SBOM + signed firmware；US EO 14028 SBOM；ETSI EN 18031；車用 ISO 26262 toolchain qualification（TQL 1-3、必須用 GHS / WindRiver / Mentor Sourcery 而非 Linaro）；醫療 IEC 62304；航太 DO-178C；中國等保 2.0。

**架構決策**：
- **目標市場 selector**：客戶選 EU CRA / US EO 14028 / ETSI EN 18031 / 車用 ISO 26262 / 醫療 IEC 62304 / 航太 DO-178C / 中國等保 2.0
- **Qualified toolchain alts**：spec 加 `qualified_toolchain_alts: []`（同 SoC R&D 用 Linaro、量產過 ISO 26262 用 GHS）
- **SBOM 自動生成 + 簽名**：CycloneDX + SPDX 雙格式
- **License Auditor**：ship 前掃 SBOM、列 GPL component + 是否履行 obligation、conflict 直接 block production tag
- **Functional safety toolchain qualification (TQL)**：分級 1-3 對齊 spec
- **R44 mitigation**：明示「decision support / lab certification 仍需 third-party」

### K. Build Farm / Cache 三層隔離 → HD.21.4

**實況**：Yocto build 一顆 SoC 8GB RAM + 50GB disk + 30-90 min。50 客戶並行 → 需 build farm + 排隊 + ccache / sstate-cache 共享。但 **NDA project 不能共用 cache**（NDA-A 的 cache leak NDA-B 是 R41）。

**架構決策**：
- **Tier 1 cache**：open project 共享 ccache / sstate-cache
- **Tier 2 cache**：per-tenant 獨享
- **Tier 3 cache**：per-NDA 獨享
- **Distributed build (icecc / distcc)** + quota 控管
- **Reproducibility verification**：同 source + 同 toolchain → 同 hash

### L. 部署多樣性 → HD.21.5

**實況**：之前預設「我方 SaaS、客戶連網用」。實況：air-gapped 客戶（軍工 / 國防 / 中國敏感 / 金融）/ self-hosted in customer VPC / per-region data residency / distributor as proxy。

**架構決策**：
- **Offline bundle export**：platform spec + SDK mirror snapshot + toolchain OCI 打包成單一 tarball（air-gapped 客戶下載 / 隨身碟）
- **Self-hosted edition**：整套 docker-compose + 每年 license activation token
- **Per-region data residency**：中國 / 歐盟 GDPR / 美國 FedRAMP 走 PEP gateway region routing
- **Distributor proxy mode**：代理商代客戶用、client 不直接接觸我方 SaaS

### M. 反假冒 / Authenticity → HD.21.6

**實況**：灰市 chip / 仿造 / 翻新 / 降級鎖 / supply chain attack；anti-tampering 在 field 被 dump firmware。

**架構決策**：
- **Chip authenticity verifier**：產線 / RMA 連 device 跑 challenge → 從 OTP / boot ROM / die ID 驗真偽
- **Supply chain attack 偵測**：vendor SDK diff 對不上 vendor 簽名 → block
- **Anti-tampering**：secure boot + encrypted rootfs 套件化（生態各 SoC 套件對照）

### N. AI Engineer Companion → HD.21.7

**實況**：embedded 工程師上手新 SoC 要 1-2 週讀 datasheet、卡關常 1 天/題。AI 應該是 daily companion。

**架構決策**：
- **Datasheet RAG inline 助手**：「I2C2 是不是支援 fast-mode plus 1 MHz」直接回 + page reference
- **Bring-up 學徒 agent**：跑 checklist + 卡關提示 + 對 schematic 反查
- **Code generation**：「給我 RK3588 RTSP minimal example」用 platform spec + 客戶範例
- **Sensor swap consultation**：直接接 HD.5 KB
- **聊天介面 Y6 inline summon**：所有 HD KB 統一 chat surface
- **R45 mitigation**：所有 register addr / bit field 答覆強制走 RAG retrieval + datasheet page reference、找不到出處拒答

### O. Vendor 生態複雜度 → HD.16.8-16.10

- **Abstract source connector**：5 種（Git / Gerrit / FTP / S3 / ManualUpload）
- **Vendor 多 branch**：Rockchip mainline / bsp / downstream / customer-specific — spec 明示 `vendor_branch_strategy`
- **Distributor 加值層**：spec 三元 source（vendor_direct / distributor / community）+ drift tracking

### P. Per-tenant Role + Patch Privacy → HD.17.6-17.7

- **7 種 role**：HW / FW / ISP tuning / Build / QA / 法務 / Upstream-propose
- **Patch propose-upstream toggle**：per-patch 決定（保護客戶 IP）
- **Catalog UI 依 role 過濾**

---

## 7. UI/UX 補充規劃

Core ADR 沒寫完整 UI、本 ADR 補。**6 view + 1 console**：

### 7.1 Platform Catalog（client-facing）

- Y6 sub-tile「Platforms」入口
- Filter sidebar：Vendor (14) / Use case (IPC / Dashcam / AI Camera / SBC / Industrial / Robotics / Drone) / Performance tier (Low / Mid / High / Flagship) / AI capability (None / Sub-1 / 1-5 / 5+ TOPS) / Firmware stack / License (Open / Commercial / NDA) / Status (GA / Beta / Alpha / Sunset / Frozen)
- 每張卡：SoC + vendor logo + AI TOPS + status badge + last sync timestamp + license tag
- NDA 卡 hover 「Sign NDA to unlock」

### 7.2 Platform Detail（5 tabs）

| Tab | 內容 |
|-----|------|
| **Overview** | spec.yaml render + reference design list + 適用 use case |
| **SDK & Toolchain** | 鎖定的 SDK commit + toolchain image SHA256 + 「local snapshot」下載按鈕 |
| **Patches** | 我方 patch series（含 propose-upstream 狀態）|
| **Sensors (HD)** | supported sensor list + (sensor, lens, module) triple |
| **Upstream Sync Log** | 時序 feed：「2026-04-25 vendor pushed v1.2.3 — patch rebase clean — green」 |

### 7.3 Upstream Sync Dashboard（operator-only）

- 50+ SoC sync 狀態總覽
- Group by L1-L5 status
- Click cell → 跳到該 SoC sync workflow log
- 一鍵動作：「retry sync」/「escalate to BP.B Guild」/「manual override（含 audit）」

### 7.4 NDA & License Center（per-tenant）

- 列出可簽 NDA（9 家）
- 每筆顯示：覆蓋 SoC / 簽署狀態 / 過期日 / 文件下載
- 簽署流程：上傳 signed PDF → operator approve → audit ledger → catalog 自動解鎖
- 過期前 30 天通知 + 過期當天自動鎖回

### 7.5 Bring-up Workbench

- 上傳 PCB → 系統產 per-revision bring-up checklist
- 接 USB-serial / J-Link probe → live capture boot console → AI parse
- 4 大類卡點 decision tree（power / clock / I2C / DDR）

### 7.6 Compliance Center

- 客戶選目標市場 → 系統列必過項 + retest plan + qualified toolchain 切換建議
- SBOM 自動生成 + License Auditor 結果

### 7.7 AI Companion（Y6 inline）

- 聊天介面、所有 HD KB 統一 surface
- Datasheet 問答 / Bring-up 學徒 / Code gen / Sensor swap consultation

---

## 8. SKILL-* 增量（HD.16-HD.21 對 BP.B Guild registry 的新增）

| Skill ID | 描述 | 來源 phase |
|----------|------|-----------|
| `SKILL_HD_PLATFORM_RESOLVE` | 客戶 design 上傳後解析 SoC mark → 鎖 platform spec | HD.16 |
| `SKILL_HD_VENDOR_SYNC` | 跑 5-stage upstream sync pipeline | HD.16 |
| `SKILL_HD_VENDOR_REBASE` | patch rebase 衝突自動嘗試（L3 escalation） | HD.16 |
| `SKILL_HD_NDA_GATE` | NDA boundary enforcement 檢查 | HD.16 |
| `SKILL_HD_CUSTOMER_OVERLAY` | per-customer overlay manifest 解析 | HD.17 |
| `SKILL_HD_LIFECYCLE_AUDIT` | 年度 reproducibility audit | HD.18 |
| `SKILL_HD_CVE_IMPACT` | CVE feed → SBOM 影響面分析 | HD.18 |
| `SKILL_HD_CVE_AUTO_BACKPORT` | vendor patch → customer-overlay 自動 backport 提案 | HD.18 |
| `SKILL_HD_BRINGUP_CHECKLIST` | 依 SoC 產 bring-up checklist | HD.19 |
| `SKILL_HD_BRINGUP_LIVE_PARSE` | live boot console → AI parse 卡點 | HD.19 |
| `SKILL_HD_PORT_ADVISOR` | 跨 SoC port 必修項清單 + 工時 | HD.19 |
| `SKILL_HD_DEVKIT_FORK` | DevKit reference → customer fork 起點 | HD.19 |
| `SKILL_HD_ISP_TUNING_DIFF` | ISP tuning binary before/after | HD.20 |
| `SKILL_HD_BLOB_COMPAT` | (BSP-ver, blob-ver) 兼容矩陣查 | HD.20 |
| `SKILL_HD_PRODUCTION_BUNDLE` | EMS access bundle 產出 | HD.21 |
| `SKILL_HD_OTA_PACKAGE_GEN` | OTA bundle generation | HD.21 |
| `SKILL_HD_SBOM_GENERATE` | SBOM CycloneDX + SPDX | HD.21 |
| `SKILL_HD_LICENSE_AUDIT` | ship-time license conflict 檢查 | HD.21 |
| `SKILL_HD_AUTHENTICITY_VERIFY` | chip authenticity challenge | HD.21 |
| `SKILL_HD_AI_COMPANION` | 統一 chat surface skill | HD.21 |

---

## 9. Migration 規劃（HD-Pipeline 0096-0105）

| Migration | 內容 |
|-----------|------|
| 0096 | `hd_platforms` / `hd_platform_specs` / `hd_vendor_mirrors` / `hd_vendor_patches` / `hd_toolchains` / `hd_nda_agreements` / `hd_sync_runs` |
| 0097 | `hd_customer_overlays` / `hd_project_pins` / `hd_role_assignments` |
| 0098 | `hd_cve_feeds` / `hd_cve_impacts` / `hd_backport_proposals` / `hd_lifecycle_audits` |
| 0099 | `hd_bringup_sessions` / `hd_port_advisors` / `hd_devkit_forks` |
| 0100 | `hd_multi_soc_topology` / `hd_modules` / `hd_lenses` / `hd_isp_tuning_sessions` / `hd_firmware_blobs` / `hd_blob_compat_matrix` |
| 0101 | `hd_production_bundles` / `hd_ota_packages` |
| 0102 | `hd_compliance_targets` / `hd_sbom` / `hd_license_audit` |
| 0103 | `hd_build_cache_tiers` |
| 0104 | `hd_offline_bundles` / `hd_authenticity_checks` |
| 0105 | `hd_companion_sessions` + 預留 |

合計 HD core (0080-0095) + HD pipeline (0096-0105) = 26 migrations 預留。

---

## 10. R-series 風險（R41-R45，與 Core ADR R36-R40 互補）

- **R41 NDA leak across tenants**：build farm cache / mirror access ACL 失誤 → NDA-A 客戶 leak NDA-B。**Mitigation**：build job 進獨立 OCI sandbox、cache 三層隔離（HD.21.4）、L5 escalation 自動 revert + alert、每季 NDA boundary audit。
- **R42 Vendor sunset / 倒閉 / 收購**：FocalTrans 等小 vendor 風險高、SDK 來源消失。**Mitigation**：frozen archive policy（HD.18.2，10 年 cold storage）+ OCI image 永久鎖（HD.18.3）+ 每年 reproducibility audit（HD.18.4）+ community-backport pool（HD.18.8）。
- **R43 CVE 響應遲緩**：vendor 推 fix 慢（HiSilicon EOL 完全不推）、客戶 fleet 暴露時間長。**Mitigation**：community-backport pool + auto-PR bot（HD.18.7）+ 主動通知（HD.18.9）+ SLO（high-severity 14 天內出 backport 提案）。
- **R44 Compliance 認證偽過**：客戶以為 SBOM / qualified toolchain 過了、實機認證 lab fail、責任反咬系統。**Mitigation**：明示「decision support / lab certification 仍需 third-party」+ qualified toolchain 必走 vendor signed binary（不允許 self-build 替代）+ audit trail。
- **R45 AI Companion 答錯 register addr**：bring-up 助手亂編 register、客戶照做 brick 機。**Mitigation**：所有 register addr / bit field 答覆強制走 RAG retrieval + datasheet page reference、找不到出處拒答（HD.9.4 invariant 繼承）。

---

## 11. Rollout 三波

| 波次 | 內容 | 對應 phase | 時程估算 |
|------|------|-----------|----------|
| **第一波** | 多客戶 NDA 隔離 / Lifecycle / CVE / Bring-up / Multi-SoC / Vendor 生態 — 影響 schema、必須一開始決定 | HD.16 / HD.17 / HD.18 / HD.19 / HD.20 (.1-.2) | ~6 週 |
| **第二波** | 量產 / OTA / port assistant / Sensor module + ISP tuning / 法規 SBOM / AI 助手 | HD.20 (.3-.10) / HD.21 | ~5 週 |
| **第三波** | Firmware blob 完整版 / Build farm cache / 部署多樣性 / 反假冒 / License auditor / 多人協作完整版 | HD.21 子模組 + 收尾 | ~3 週 |

**HD core (HD.1-HD.15) ~12 週 + Pipeline (HD.16-HD.21) ~14 週**、可平行（HD.1-HD.15 與 HD.16 在 BP 完工後可同時起跑）。整體合理估算 **~20 週**。

---

## 12. Open Questions

1. **NDA Center 是否走 DocuSign / HelloSign 整合**？還是手動上傳 PDF + operator approve？決策推遲到 HD.16.13 開工。
2. **Build farm 自建 vs 外包雲（GitHub Actions / GitLab Runner / BuildKite）**？Yocto 大型 build 在公雲不划算、可能要混合。決策推遲到 HD.21.4 開工。
3. **OTA package generator 收費模型**：per-package metered 還是 per-tenant flat？決策推遲到 PEP gateway 整合。
4. **AI Companion 模型選**：Sonnet 4.6 vs Opus 4.7 — bring-up 助手要快 / Sensor swap consultation 要深、可能 split。決策推遲到 HD.21.7 開工。
5. **DevKit catalog 與 SoC vendor 商務合作**：是否與 Rockchip / nVidia / RPi Foundation 建立 official partner、預載 reference design？這會大幅降低 onboarding 摩擦、但需商務談判。

---

## 13. 參考文件

- `docs/design/hd-hardware-design-verification.md`（HD core ADR）
- `TODO.md` Priority HD section（HD.16-HD.21 同步維護）
- `docs/design/blueprint-v2-implementation-plan.md`（BP 完工為 HD 啟動前提）
- `docs/design/w11-w16-as-fs-sc-roadmap.md`（HD 排程位於此 roadmap 之後）

---

## 14. Sign-off

- **Owner**: Agent-software-beta + nanakusa sora
- **Date**: 2026-04-28
- **Status**: Accepted（與 Core ADR 同步、pending BP 完工後啟動）
- **Next review**: BP 完工後、HD.16 開工前
