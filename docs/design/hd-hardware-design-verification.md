---
audience: architect
status: accepted
date: 2026-04-25
priority: HD (post-BP)
related:
  - docs/design/blueprint-v2-implementation-plan.md
  - docs/design/w11-w16-as-fs-sc-roadmap.md
  - docs/design/as-auth-security-shared-library.md
  - docs/design/bs-blueprint-spec-system.md
  - TODO.md (Priority HD section)
---

# ADR — Priority HD: Hardware Design Verification & Differential Analysis

> **One-liner**：把 OmniSight 從「embedded AI camera multi-agent dev command center」推進到「**HW + FW co-verification platform**」— 解析 7 大 EDA、cross-check 4 大 firmware stack、深度知識庫覆蓋 10 大 CMOS sensor 廠商、提供 reference design diff / forced-AVL substitution / HIL / compliance retest 全工作流。

---

## 1. 背景與問題陳述

OmniSight 的核心市場定位是**嵌入式 AI camera 開發 multi-agent 平台**。在實務上、客戶面對的痛點不是 software side（那是 GitHub / Cursor / Linear 已經卷到底的市場）、而是**HW ↔ FW 介面的整合困難**：

### 1.1 客戶實況（4 大痛點）

1. **Reference design 換料 → 全錯**
   客戶拿 SoC vendor 提供的 reference design、為了成本/料件可得性換 ~30% BOM、結果：
   - schematic 對不上 layout（refdes 改名、footprint 換版）
   - 暫存器設定錯誤（換 sensor 後 register map 完全不同）
   - 電壓 / 時序全錯（PMIC 換型、power-up sequence 變、MCLK source 變）
   - 韌體無從下手 — 也沒有自動化方式驗證
   - 結果：tape-out 後 N 次 re-spin、上市時間 +6 個月
2. **AVL 之外硬塞 CMOS sensor**
   同個 SoC 平台、客戶因為缺料 / 成本 / 性能要求、硬選不在 AVL 的 sensor。**這不是「driver 換一下」這麼簡單** — 換 sensor 等於：
   - register map（從 SONY IMX 系到 OV / GalaxyCore 是兩個世界）
   - I2C address + multi-cam isolation 策略
   - power rail 數量 + 順序 + 電壓
   - clock requirement（MCLK / PCLK / PIXCLK / sys_clk）
   - HDR scheme（DOL vs stagger vs line-interleave）
   - LSC / color matrix / AE / AWB 全部要重 tune
   - 已知 errata（sensor vendor datasheet 不會主動公布）
   全部要重做。底層控制完全變了個樣。
3. **多層板走線問題、EDA DRC 過了但實機掛**
   crosstalk / 阻抗不連續 / return path 斷裂 / 差動對 length mismatch / 參考平面破洞 — EDA 軟體（Altium / KiCad / OrCAD）的 DRC PASSED、但實機跑起來 frame drop / I2C NAK / DDR ECC error。EMC / 安規 retest 後才發現問題、retest 已經花掉 USD $30k+ + 4 週 turnaround。
4. **Schematic vs 原理圖 vs 實際 layout 三方對不上**
   block diagram → schematic → layout 每層轉譯都會走樣（refdes 改、footprint 改、layer 改、net 改）。韌體工程師 debug 時找不到真實 ground truth、要靠 vendor FAE 解 → 來回 1 週起跳。

### 1.2 既有 OmniSight 系統現況

到 BP 完工為止、OmniSight 涵蓋：
- multi-agent 工作流（BP）
- workspace 階層（Y6）
- live sandbox preview（W14）
- catalog（BS）
- auth / token vault（AS）
- file storage（FS）
- session continuity（SC）
- guild & dual-sign（O7 / BP.B）
- audit ledger（N10）
- RAG（R20 Phase 0）
- PEP gateway（pricing / metering）

**完全沒有任何 HW / FW 解析能力**。HD 是這個 gap 的填補。

---

## 2. 決策（Decision）

採用 **完整路線**（不走 MVP）、排程 **在 BP 完工後** 啟動，~12 週交付。

### 2.1 為什麼走完整路線而非 MVP

- HD 的差異化護城河來自 vendor matrix 廣度（7 EDA × 4 FW × 10 sensor vendor）。**MVP 只做 KiCad + 1 sensor 的版本沒有商業意義** — 客戶只用一兩家 EDA / 一兩家 sensor 的場景幾乎不存在。
- HW 領域的「半套」工具反而比沒工具差 — 工程師會懷疑系統 coverage、放棄信任、退回 vendor FAE 路徑。
- 完整路線的時間花費（~12 週 vs MVP ~5 週）值得 — 因為 BP 結束後 multi-agent 平台已經 ready，HD 只需要在平台上跑、不用重做平台。

### 2.2 為什麼排在 BP 之後（路線 1）

- BP 提供 multi-agent 工作流、HD 重度依賴（HD.12 的 `hd-parser-bot` / `hd-diff-bot` / `hd-sensor-swap-bot` 都是 BP.B Guild 內的 agent）
- BP 提供 PEP gateway 計量（HD parser / HIL 是計算密集、需計量）
- BP 提供 O7 dual-sign（HW 變動責任重、不能 AI 單獨 +2 ship）
- BP 結束後 schedule slack 較大、可承接 12 週的 HD 工作量

### 2.3 為什麼不採取「外接 EDA tool plugin」路徑

考慮過：寫 KiCad / Altium plugin、讓 HD 跑在 EDA tool 內。否決理由：
- 客戶的 EDA tool 不在我們的 sandbox 內、跑 plugin 等於把客戶 IP（schematic）解到客戶機器、無法走 R20 Phase 0 RAG / N10 ledger / O7 dual-sign 等既有基礎
- 維護 7 個 vendor 的 plugin 比維護 7 個 parser 更貴（plugin 要追每個 vendor 的 API breaking change）
- 客戶的 EDA tool 版本參差不齊、plugin 兼容矩陣爆炸

走 **解析 export file 為 HDIR 中間表示** 是更可控的路徑。

---

## 3. 架構概觀

```
┌─────────────────────────────────────────────────────────────────────┐
│                    HD (Hardware Design Verification)                 │
└─────────────────────────────────────────────────────────────────────┘
   │
   ├─── Group A: Ingestion (HD.1 – HD.4) ───────────────────────
   │    EDA parsers (KiCad / Altium / OrCAD / PADS / Mentor / Eagle / IPC-2581 / ODB++ / vision LLM fallback)
   │            │
   │            ▼
   │    HDIR (Hardware Design IR) — schematic + PCB layout schema
   │            │
   │            ▼
   │    PCB Signal Integrity Analyzer (HD.2)  → finding board
   │    Schematic-Layout Consistency (HD.3)   → finding board
   │    Reference Design Diff Engine (HD.4)   → impact analysis
   │
   ├─── Group B: Sensor & AVL (HD.5 – HD.6) ───────────────────
   │    CMOS Sensor Knowledge Base (10 vendors)
   │    AVL Forced-Substitution Workflow
   │
   ├─── Group C: Cross-check (HD.7 – HD.9) ────────────────────
   │    Firmware Stack Adaptor (Linux+Yocto / +BR / AOSP / RTOS)
   │            ↕ HW↔FW cross-check engine
   │    HIL Emulator (sensor / PMIC / clock tree)
   │    RAG over HD corpus (datasheet / errata / ref manual)
   │
   └─── Group D: Workflow + UI (HD.10 – HD.15) ────────────────
        Compliance Retest Plan Generator
        HD Workspace UI (Y6 tile)
        HD Multi-Agent Workflow (BP.B Guild bots)
        HD Audit Ledger (N10 integration)
        HD Test Suite + Golden Fixtures
        HD Rollout / Runbook / Risk Register
```

### 3.1 整合既有基礎（不重造輪子）

| 既有元件 | HD 重用方式 |
|----------|-------------|
| Y6 Workspace | HD project = Y6 project 子型，dashboard 加 HD tile |
| W14 Live Sandbox Preview | schematic / layout viewer + HIL session UI 全部塞進 W14 同一容器 |
| R20 Phase 0 RAG | datasheet / errata / SoC reference manual 都進 R20 vector store |
| BS Catalog | reference design + sensor KB spec card 用 BS 同一 catalog 模板 |
| AS Token Vault | 客戶 schematic 加密用同一 per-tenant Fernet 策略 |
| BP.B Guild | hd-parser-bot / hd-diff-bot / hd-sensor-swap-bot / hd-fw-sync-bot 進 Guild |
| O7 dual-sign | HD bot output 走 merger-agent-bot +2 + non-ai-reviewer +2 |
| PEP gateway | HD parser / diff / HIL 走 PEP 計量 |
| N10 ledger | 所有 HD 行為寫 N10 hash chain |

---

## 4. HDIR (Hardware Design IR) 設計

HDIR 是 vendor-agnostic 中間表示、所有下游分析（diff / cross-check / RAG）都吃 HDIR。

### 4.1 Schematic 層

```python
@dataclass(frozen=True)
class Component:
    refdes: str                       # 'U1', 'C12', 'R34'
    part_number: str                  # 'IMX415-AAQR-C'
    footprint: str                    # 'BGA-129', 'QFN-48'
    value: str | None                 # '10uF', '4.7K', None for IC
    pins: list[Pin]                   # ordered, 1-indexed
    power_pin_groups: dict[str, list[int]]  # 'VDD' -> [3,4,5]
    vendor_part_id: str | None        # canonical vendor SKU
    coverage: Literal['full', 'partial', 'vision']

@dataclass(frozen=True)
class Net:
    name: str
    type: Literal['power', 'signal', 'gnd', 'differential']
    driver: tuple[str, int]           # (refdes, pin_idx)
    receivers: list[tuple[str, int]]
    impedance_target: float | None    # Ω, e.g. 100 for diff pair
    length_constraint: tuple[float, float] | None  # (min, max) mm
    coverage: Literal['full', 'partial', 'vision']
```

### 4.2 PCB Layout 層

```python
@dataclass(frozen=True)
class Layer:
    stack_order: int                  # 1 = top
    type: Literal['signal', 'plane', 'mixed']
    dielectric: float                 # Dk (relative permittivity)
    thickness_mm: float

@dataclass(frozen=True)
class Trace:
    net: str
    layer: int
    segments: list[tuple[Point, Point]]
    width_mm: float
    length_mm: float                  # cumulative
    via_count: int

@dataclass(frozen=True)
class Via:
    layer_from: int
    layer_to: int
    type: Literal['through', 'blind', 'buried']

@dataclass(frozen=True)
class Plane:
    layer: int
    net: str
    cutouts: list[Polygon]
```

### 4.3 Coverage 旗標

不同 vendor 的 export 完整度差很多。HDIR 必須用 `coverage` 標記：
- `full`：vendor binary parser 直接抽出
- `partial`：parser 抽出但部份 field 缺（例如 OrCAD 老版沒 differential pair metadata）
- `vision`：純靠 Claude Sonnet 4.6 vision 從 PDF / Gerber 渲染圖讀出、低信賴度

下游 diff 引擎遇到 vision-sourced node 時、自動降低 finding severity 並要求 human review。

---

## 5. EDA Vendor Coverage Matrix

| EDA Tool | Schematic Format | Layout Format | Parser 路徑 | 覆蓋難度 |
|----------|------------------|---------------|-------------|----------|
| **KiCad** | `.kicad_sch` (S-expr text) | `.kicad_pcb` (S-expr text) | 純 Python S-expr parser + `pcbnew` API | ⭐ 易 |
| **Altium** | `.SchDoc` (OLE binary) | `.PcbDoc` (OLE binary) | `olefile` + 反向工程 schema | ⭐⭐⭐ 中高 |
| **OrCAD Capture** | `.dsn` (ASCII s-expr) | — | s-expr parser | ⭐⭐ 中 |
| **OrCAD Allegro** | — | `.brd` (binary) | 走 IPC-2581 export 中介 | ⭐⭐⭐⭐ 極高（要客戶配合 export） |
| **PADS** | `.sch` | `.pcb` | 走 `.asc` ASCII export 中介 | ⭐⭐ 中 |
| **Mentor / Siemens Xpedition** | (proprietary) | (proprietary) | 走 IPC-2581 / ODB++ 中介 | ⭐⭐ 中（廠商級工具普遍 export 標準格式） |
| **Eagle** | `.sch` (XML) | `.brd` (XML) | XML parser | ⭐ 易 |
| **(fallback)** | PDF + Gerber 圖 | PDF + Gerber 圖 | Vision LLM (Claude Sonnet 4.6) | ⭐⭐⭐⭐ 高（confidence 標記） |

### 5.1 覆蓋策略
- **直接 binary parser 為主**：KiCad / Altium / Eagle / OrCAD Capture / PADS（5 家）
- **中介格式 fallback**：IPC-2581 + ODB++（2 個業界標準、Mentor / Allegro / 高階 EDA 都能 export）
- **Vision LLM 為最後 fallback**：客戶用 niche EDA / 無法 export 標準格式 / 只有 PDF。**不取代 binary parser、僅當補強**。

---

## 6. CMOS Sensor Knowledge Base — 10 大廠商

| 廠商 | 主要型號（部份） | 主力定位 | HDR scheme |
|------|------------------|----------|------------|
| **SONY** | IMX179 / 219 / 258 / 415 / 462 / 678 / 900 | 高階 / 專業 / 安防 | DOL HDR |
| **OmniVision (OV)** | OV2640 / 5640 / 13855 / 48C / 64A40 | 全段 / 手機 / 安防 | Stagger HDR |
| **Samsung ISOCELL** | S5K3P9 / GN1 / HM3 | 手機高階 | Tetracell / Nonacell |
| **晶相光 SmartSens** | SC031GS / 2310 / 500AI / 8238 / 8330 / 850SL | 中低階 / IPC 安防主力 | 多種 |
| **原相 PixArt** | PAS6411 / PAJ6100 | sensor + tracking IC | 較少 HDR |
| **凌陽 Sunplus** | SPCA 系 | USB camera 主控 + 整合 sensor | — |
| **神盾 Egis** | (fingerprint + 部份 image) | biometric | — |
| **Nexchip 晶合** | (品牌 sensor 較少) | wafer fab + 自家 sensor | — |
| **格科威 GalaxyCore** | GC0308 / 2053 / 02M1 / 4653 | 手機中低階 + IoT 大宗 | 部份 |
| **思威特 SuperPix** | SP140 / 2509 / 8408 | 低階主流 | — |

### 6.1 Sensor Spec Card Schema

每顆 sensor 進 KB 含：
- `register_map_url` — datasheet section 連結 + 完整 register dump（在 RAG）
- `i2c_addr_default` — 含 multi-cam 隔離策略建議
- `power_rails[]` + `power_sequence_steps[]` — 順序敏感
- `mclk_required_mhz` — 通常 6/12/24/27 MHz
- `hdr_scheme` — null / stagger / dol / line_interleave
- `lsc_template_id` — Lens Shading Correction 起點 table
- `color_matrix_template_id` — CCM 起點
- `known_errata[]` — vendor 不主動公布的問題（如 IMX415 某 batch 在 60Hz flicker rejection 不完整）
- `silicon_revision` — KB entry 帶版本、避免 rev1/rev2 混淆（R37 風險）

### 6.2 Sensor Swap Impact Analyzer

給定 `from_sensor + to_sensor`、自動產出：
- register map diff（哪些 register addr / bit field 改了）
- I2C addr 衝突檢查
- power sequence diff（順序 / 電壓 / 時序）
- clock requirement diff（MCLK 從 24 → 12 MHz 要重設 PLL）
- HDR scheme 切換（DOL → stagger 整個 ISP pipeline 要重組）
- 預估 ISP retune 工時（hours）
- AVL 風險 flag（若違反 AVL）

---

## 7. Firmware Stack Adaptor — 4 大支援

| Stack | 解析重點 | HW↔FW 對齊路徑 |
|-------|----------|----------------|
| **Linux + Yocto** | DTS（pinmux / clock / I2C / power）/ Kconfig / `.bb` recipe / layer | schematic GPIO ↔ DTS pinmux node |
| **Linux + BuildRoot** | BR2 config / package list / postbuild | 同上 |
| **AOSP** | HAL / sensor module / ISP tuning XML / device.mk / BoardConfig.mk | schematic + camera HAL config |
| **RTOS / FreeRTOS / ThreadX** | CMSIS HAL / linker script / startup file / register-level init | schematic + 直接 register write |

### 7.1 HW↔FW Cross-Check Engine 規則

- schematic GPIO assignment vs DTS pinmux 對齊
- schematic I2C bus + addr vs driver 取用 addr 對齊
- schematic power rail 順序 vs driver power-on sequence 對齊
- schematic sensor MCLK source vs DTS clock node 對齊
- schematic interrupt pin vs driver IRQ 設定對齊

每條 rule violation 寫入 `hd_hwfw_crosscheck_findings` 表、severity 分級。

---

## 8. SKILL-* 增量

HD 在 BP.B Guild SKILL registry 新增：

| Skill ID | 描述 | 暴露給 |
|----------|------|--------|
| `SKILL_HD_PARSE` | 解析 EDA file → HDIR | `hd-parser-bot` / 通用 agent |
| `SKILL_HD_DIFF_REFERENCE` | reference vs customer design diff | `hd-diff-bot` |
| `SKILL_HD_SENSOR_SWAP_FEASIBILITY` | sensor 替換可行性 | `hd-sensor-swap-bot` |
| `SKILL_HD_FW_SYNC_PATCH` | HW change → FW patch 清單 | `hd-fw-sync-bot` |
| `SKILL_HD_PCB_SI_ANALYZE` | PCB SI 分析 | `hd-parser-bot` |
| `SKILL_HD_HIL_RUN` | HIL session 執行 | `hd-fw-sync-bot` |
| `SKILL_HD_RAG_QUERY` | datasheet RAG 檢索 | 所有 HD agent |
| `SKILL_HD_CERT_RETEST_PLAN` | EMC / 安規 retest plan | `hd-cert-bot` |

---

## 9. HIL Emulator 架構

**Scope**：functional / register-level emulation。**不是** SPICE 級數位/類比 simulation（那是 Cadence / Mentor 賽道、不打）。

### 9.1 Emulator 元件
- **Sensor emulator**：emulate I2C register map + 假 frame buffer 給 ISP
- **PMIC emulator**：boot sequence + LDO enable / disable + over-current trip
- **Clock tree emulator**：crystal → PLL → domain clock 計算

### 9.2 Validation
- 選 3 顆 open-source reference board（OrangeCrab / Glasgow / TinyFPGA）跑實機 + emulator 同 firmware path、列出行為差異作為 validation suite
- 季度 re-run、若偏差 > threshold → 標 emulator 不可信
- emulator output 永遠帶 `confidence` 標記、不取代實機（R39 風險 mitigation）

---

## 10. Migration / Schema 規劃

| Migration | 內容 |
|-----------|------|
| 0080 | `hd_designs` / `hd_components` / `hd_nets` / `hd_pcb_traces` / `hd_pcb_planes` |
| 0081 | `hd_sensors` / `hd_sensor_register_maps` / `hd_sensor_errata` |
| 0082 | `hd_avl_entries` / `hd_avl_substitutions` |
| 0083 | `hd_firmware_assets` / `hd_hwfw_crosscheck_findings` |
| 0084 | `hd_hil_sessions` / `hd_hil_traces` |
| 0085 | `hd_cert_clauses` / `hd_retest_plans` |
| 0086 | `hd_ledger_entries` (整合 N10) |
| 0087-0095 | 預留 — sensor KB 擴充 / vendor 新增 / schema evolution |

### 10.1 Single-knob Rollback

`OMNISIGHT_HD_ENABLED=false`：
- HD UI tile 隱藏
- HD agent skill 從 Guild registry 移除
- HD API endpoint 回 410 Gone
- HD migrations 0080-0095 走 lazy create / no-op pattern（既有 BP / AS / FS / SC 0 影響）
- compat regression test 必過

---

## 11. Test Strategy

### 11.1 Parser Golden Tests
- 每個 EDA tool 至少 1 顆 open-source reference design 當 round-trip fixture
- KiCad: 官方 demo project
- Altium: 開源 ALTIUM 範例
- Eagle: SparkFun library
- OrCAD / PADS / Mentor: 走 IPC-2581 / ODB++ 中介、用業界 reference fixture

### 11.2 Diff Regression Tests
- 50+ scenario，每種 diff 類別（add / remove / swap / value / footprint / pin remap / net rename / layer / length）至少 3 顆

### 11.3 Sensor KB Validation
- 10 大 vendor、每家至少 3 顆 sensor 的 spec card 走 datasheet ground-truth 驗證
- 每季 re-validate（datasheet rev 更新時）

### 11.4 HW↔FW Cross-Check Golden Boards
- 3 顆 open board（OrangeCrab / Glasgow / TinyFPGA）schematic + DTS + driver 一起 commit fixture
- CI 鎖

### 11.5 Compat Regression
- HD 全套 disable 後、既有 BP / AS / FS / SC / W11-W16 0 回歸

---

## 12. Risk Register（R36-R40）

| ID | 風險 | Mitigation |
|----|------|-----------|
| **R36** | EDA vendor 升 major version、binary format 變、parser 死 | 每年 review 各 parser、版本上限明示；vision LLM fallback 永遠在線 |
| **R37** | Sensor KB 過時（vendor 改 register map / silicon rev） | KB 每筆帶 silicon revision、上傳 datasheet 時強制標 rev、AVL violation 時雙重 confirm |
| **R38** | 客戶 schematic 機密外洩（HDIR 存 raw → DB 入侵） | HDIR 走 AS Token Vault 同等加密（per-tenant Fernet）、N10 ledger 不含 raw bytes |
| **R39** | HIL emulator vs real board 行為偏差 | emulator output 永遠帶 confidence 標記、明示不取代實機、emulator validation suite 季度 re-run |
| **R40** | Forced-AVL substitution 法律責任 | 所有 substitution 提案明示「decision support only / final responsibility on operator」、ledger 完整保存決策路徑、提供 export 給客戶法務存檔 |

---

## 13. Rollout / Pricing / Operator Runbook

### 13.1 Rollout 順序
1. 內部 dogfood：先用 OrangeCrab / Glasgow / TinyFPGA 3 顆 golden board 跑 end-to-end
2. Beta：選 3 個內部熟識客戶（簽 NDA）做 reference vs customer diff scenario
3. GA：開放 PEP 計量、AVL violation workflow 上線、HIL 收費

### 13.2 Pricing Model（PEP Gateway 計量）
- **EDA parse**: per-file metered（按 component 數 + layer 數）
- **Diff engine**: per-diff metered（按變動點數）
- **Sensor swap analyzer**: per-pair metered（不論結果）
- **HIL session**: per-second runtime metered
- **Cert retest plan generation**: per-plan flat fee

決策：HD credit pool 與 BP credit pool **獨立** — 因為 HD 客群（嵌入式工程團隊）vs BP 客群（純軟工程團隊）的 procurement 路徑不同（HD 通常走 hardware budget、BP 走 SaaS budget）。

### 13.3 Operator Runbook

`docs/ops/hd_operator_runbook.md`（HD.15.2 交付）：
- 客戶上傳 reference + customer design 完整 SOP
- 各 EDA tool export 步驟（含截圖）
- AVL violation form 範本
- HIL session 啟動 / 中止 SOP
- Cert retest plan 客戶交付 SOP
- 故障排除 decision tree

---

## 14. Open Questions

1. **Vendor reference design 取得**：是否與 SoC vendor（Sunplus / Realtek / 瑞昱 / Novatek / SigmaStar 等）建立官方合作、把他們的 reference design 預先進 KB？這會大幅降低客戶 onboarding 摩擦、但需要商務談判（時程不在 HD 的 12 週內）。
2. **PCB SI 計算強度**：是否考慮把 PCB SI analyzer 的 heavy lifting 外包給 cloud GPU pool（類似 W14 sandbox 的 elastic compute）？決策推遲到 HD.2 開工後依實測 latency 決定。
3. **EDA plugin 版本**：未來是否補做 KiCad / Altium plugin（在 EDA 內按一個按鈕直接上傳到 OmniSight）？這是 ergonomics、不是核心。HD GA 後再考慮。
4. **AOSP HAL 深度**：camera HAL3 的支援深度（只看 metadata 還是要看 IMPL pipeline configuration）？決策推遲到 HD.7 AOSP 開工後與選定 dogfood 客戶討論。
5. **HD ↔ BS 重疊**：sensor KB spec card 的 schema 與 BS Catalog 的 spec template 是否合併？傾向 **不合併** — sensor KB 屬 HD private schema、不對外公開（含 vendor errata 等敏感 IP），與 BS catalog 對外發行的定位不同。

---

## 15. 參考文件

- `TODO.md` Priority HD section（同步維護）
- `docs/design/blueprint-v2-implementation-plan.md`（BP 完工為 HD 啟動前提）
- `docs/design/w11-w16-as-fs-sc-roadmap.md`（HD 排程位於此 roadmap 之後）
- `docs/design/as-auth-security-shared-library.md`（HDIR 加密重用 AS Token Vault 模式）
- `docs/design/bs-blueprint-spec-system.md`（HD sensor KB 模板參考 BS catalog 模板）
- `docs/audit/2026-04-27-deep-audit.md`（HD 規劃對齊 audit P1.4 「operator-driven 而非 CI-driven」紀律）

---

## 16. Sign-off

- **Owner**: Agent-software-beta + nanakusa sora
- **Date**: 2026-04-25
- **Status**: Accepted（pending BP 完工後啟動）
- **Next review**: BP 完工後、HD.1 開工前
