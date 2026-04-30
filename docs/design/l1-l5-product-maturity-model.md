---
audience: architect + business
status: accepted
date: 2026-04-30
priority: maturity-meta（CL + L4 + L5 三個新 priority + 既有 priority L1-L3 mapping）
related:
  - TODO.md (Priority CL / Priority L4 / Priority L5 / 既有所有 priority 的 L-level annotation)
  - docs/design/blueprint-v2-implementation-plan.md (BP)
  - docs/design/hd-hardware-design-verification.md (HD)
  - docs/security/ks-multi-tenant-secret-management.md (KS)
  - docs/design/wp-warp-inspired-patterns.md (WP)
  - docs/legal/oss-boundaries.md
---

# ADR — L1-L5 Product Maturity Model（OmniSight 產品成熟度分層 + 對應 priority 路線）

> **One-liner**：把 OmniSight 三條產品線（Type A Web SaaS gen / Type B Embedded HW / Type C Multi-agent dev tool）對「**真實落地能力**」分成 L0-L5 六層、把既有路線（AS / W / FS / SC / WP / BP / KS / HD / Q / I / Z / Y / N 等）標明各自服務哪一層、補上**Priority CL（Commercial Launch / 最後一哩）+ Priority L4（Beyond-Commercial Excellence）+ Priority L5（Category-Defining R&D）**填補 L3 ceiling 之後的真空。

---

## 1. 為何需要這份 ADR

之前 priority 規劃預設「routine 跑完 = production-ready」。但 2026-04-30 deep audit 後發現：

- **L3（commercial-grade）不是工程完工 = 自動到達**、需 SOC 2 / SLA / billing / customer success 等**路線圖外的最後一哩**
- **L3 也只是「能合法上市」入場券、不是「best-in-class」** — 真實差異化在 L4（Beyond-Commercial Excellence）
- **L5（Category-Defining R&D）是定義產業的力量**、需 12-24 個月 R&D 投入

如果不把這層階梯寫進 roadmap、外部溝通 / 商務承諾 / 工程優先級會永遠模糊。本 ADR 把 L0-L5 變成 OmniSight 的官方共同語言。

---

## 2. L0-L5 六層定義

| Level | 名稱 | 判準 | OmniSight 三產品線中誰可達到 |
|-------|------|------|---------------------------|
| **L0** | Toy / Demo（玩具）| 能跑能看、給人看 demo；崩在邊緣情況、不能持續運作 | 任何 priority 開工初期 |
| **L1** | Prototype（原型）| 跑得起來、內部 / 1-10 友善客戶用；缺 auth / billing / monitoring / scaling | W11-W16 完工後的 Type A、HD 部分 phase 完工後的 Type B |
| **L2** | Production-Ready（上線）| 真實付費用戶可用、有 monitoring / SLA / 基本 observability；**不一定過合規** | 路線（AS / W / FS / SC / WP / BP / KS / HD）+ 部分 CL 後 |
| **L3** | Commercial-Grade（商業級）| 過 SOC 2 / ISO 27001 等認證；多租戶 + data residency；完整 support / pricing tier；可服務 enterprise / 受監管產業 | 路線 + Priority CL 完工後 |
| **L4** | Beyond-Commercial Excellence（卓越級）| 具差異化護城河（reproducibility / provenance DAG / adversarial robustness / industry cert inheritance / real-time collab / functional safety formal verification 等 10 項）| Priority L4 後（路線 + CL + L4 全綠）|
| **L5** | Category-Defining R&D（定義產業）| 重新定義產業的能力（HW/SW co-sim / long-term agent memory / side-channel security / multi-agent simulation / digital twin）| Priority L5 後（漫長 R&D） |

### 2.1 三產品線（重申）

| Type | 對應 priority cluster | 客戶用來做什麼 |
|------|----------------------|----------------|
| **Type A — Web SaaS app generation** | W11-W16 / FS / SC | 客戶生成可上線 SaaS（Lovable / Replit / Bolt 競品定位）|
| **Type B — Embedded AI camera 產品** | HD（21 phase）| 客戶從 reference design 走到量產（schematic 驗證 / sensor swap / bring-up / 合規 retest）|
| **Type C — Multi-agent dev command center** | BP / Y / Z / 多 agent infra | 客戶當 internal dev / agent dispatch 平台 |

---

## 3. 既有 priority 對應 L-level（mapping table）

> 此表是 routing — 哪個 priority 把 OmniSight 推到哪一層、屬哪一種 product type。

| Priority | 服務 type | 主要服務 L-level | 為何 |
|----------|----------|----------------|------|
| **BS Bootstrap & Catalog** | A / B / C | L0→L1（共用基礎）| 安裝 / catalog / vertical-aware setup |
| **AS Auth & Security shared library** | A / B / C | L1→L2 | OAuth / Turnstile / token vault — 所有 type 用 |
| **W11-W16 Web vertical（Lovable）** | A | L0→L1 | 前端生成（後端缺、L2 還達不到）|
| **FS Full-Stack generation** | A | L1→L2 | DB / Auth provisioning / Object storage / Email / Jobs / Search / Billing — 補 W 後端 |
| **SC Security Compliance for generated apps** | A | L2 | OWASP / SAST / DAST / Bot 防禦 / 法規 |
| **WP Warp-inspired Patterns** | A / B / C | L1→L2 / agent quality 升 | Block / Skills / Diff-validation / Project-context / Feature flag — agent DX 基礎 |
| **BP Multi-agent platform deepening** | C 主、A / B 受惠 | L2 | Multi-agent + Guild + dual-sign + LLM merge conflict 仲裁 |
| **KS Multi-tenant secret management** | A / B / C | L2→L3（multi-tenant 上線必過）| Envelope encryption / CMEK / BYOG proxy |
| **HD Hardware Design Verification** | B 主 | L0→L2（B 從零起、~20 週後到 L2）| 7 EDA × 4 FW × 10 sensor × 14 SoC vendor matrix |
| **Q Multi-Device Parity** | A / C | L2 | Multi-device session / state sync |
| **I Multi-tenancy Foundation** | A / B / C | L2 (gate)→L3 (with KS.1) | RLS / 多租戶資料隔離 |
| **G Ops / Reliability HA** | A / B / C | L2 | HA / blue-green / SLA 基礎 |
| **H Host-aware Coordinator** | A / B / C | L2 | 自適應調度 / token bucket |
| **N Dependency Governance** | A / B / C | L2 | Dependency lifecycle / lockfile audit |
| **O Enterprise Event-Driven Multi-Agent** | C | L2 / scaling | 分散式 worker pool / Redis 互斥 |
| **R Enterprise Watchdog & DR** | A / B / C | L2 / L3 | 災難復原 / UI 強化 |
| **S2 Security Hardening Phase 2** | A / B / C | L2 / L3 | Anti-Reconnaissance / Zero-day Mitigation / UBA |
| **T Billing & Payment Gateway** | A / C | L2→L3（commercial 必過）| Stripe / ECPay / PayPal 計費 |
| **V Visual Design Loop + Workspace** | A | L1→L2 | v0.dev / Codex 體驗層 |
| **W Web Platform Vertical** | A | L0→L1 | Next.js / Nuxt 前端生態 |
| **P Mobile App Vertical** | A 延伸 | L0→L1 | iOS / Android |
| **X Pure Software Application** | A 延伸 | L0→L1 | 後端服務 / CLI / 桌面 |
| **L Bootstrap Wizard** | A / B / C | L0→L1 | 一鍵新機到公網 |
| **Y Tenant Ops & Multi-Project Hierarchy** | A / B / C | L1→L2 | 多用戶 / 多專案運營面板 |
| **Z LLM Provider Observability** | A / B / C | L2 | 餘額 / rate-limit / 真實用量可視化 |
| **ZZ Claude-Code-Style Agent Observability** | C | L2 | Agent 黑盒開玻璃盒 |
| **(NEW) CL Commercial Launch** | A / B / C | **L2→L3**（最後一哩）| SOC 2 / SLA / Customer Success / 規模壓測 / Marketing 等 |
| **(NEW) L4 Beyond-Commercial Excellence** | A / B / C | **L3→L4** | Reproducibility / Provenance DAG / Adversarial robustness / Industry cert inheritance 等 10 項 |
| **(NEW) L5 Category-Defining R&D** | A / B / C | **L4→L5** | HW/SW co-sim / Long-term memory / Side-channel security 等 5 項 R&D 大項 |

---

## 4. 路線推進順序（含新 CL / L4 / L5）

```
Today (BS done, AS done, W in progress)
  ↓
W11-W16 (+WP.4) → FS → SC → WP-Wave-1 → BP (+WP.10) → KS.1 →
  ↓ (Type A / C 達 L2)
HD (~20 週、Type B 從 L0 → L2)
  ↓
Priority CL（commercial 最後一哩、~6-12 個月、SOC2 等需外部 audit）
  ↓ (三產品線達 L3 floor)
Priority L4（Beyond-Commercial、~12-18 個月、可平行）
  ↓ (達 L4)
Priority L5（Category-Defining R&D、~12-24 個月、各項可獨立啟動）
  ↓ (達 L5、定義產業地位)

獨立 commercial-driven：
  • KS.2 / KS.3 — 中型 enterprise / 銀行政府詢盤觸發
  • L4 / L5 部分項 — 客戶詢盤 / 商務 trigger 提前
```

**總時程（從 today）**：
- L2 floor：~31 週（路線跑完）
- L3 floor：+6-12 個月（CL 跑完、含 SOC 2 audit ~6 月）
- L4 ceiling：再 +12-18 個月
- L5 ceiling：再 +12-24 個月（部分項並行）
- **L0 → L5 全部到頂**：**~3-4 年**

---

## 5. Priority CL — Commercial Launch（最後一哩）

> **目標**：把路線完工後仍欠的「真實 commercial 上線必要條件」一次補齊。**不是工程 feature、是商務 + 合規 + 運營 readiness**。
>
> **migration**：CL 0126-0140（15 slots）
> **single knob**：`OMNISIGHT_CL_*` per phase 各自有

### CL.1 SOC 2 Type II 認證
- 與外部 auditor 簽約（Drata / Vanta / Secureframe 工具 + auditor）
- ~6 個月 audit 週期 + USD $50-150k cost
- 涵蓋：security / availability / confidentiality / processing integrity / privacy
- 整合既有 KS.4.7 readiness checklist
- 通過後 Type I → 1 年觀察期 → Type II

### CL.2 ISO 27001 認證
- 歐盟 / 政府客戶常要
- 與 SOC 2 ~70% 重疊、可同時跑（Drata 等工具支援）
- ~12 個月、+30% 成本

### CL.3 SLA 框架 + 補償條款
- 99.9% uptime SLA / 99.95% premium tier / 99.99% enterprise
- 補償條款：downtime > N% 退費 X%
- 整合 G priority HA + R priority DR
- legal review + 定型契約

### CL.4 Customer Support workflow + ticket system
- 三層支援（L1 / L2 / L3）
- ticket SLA（first-response / resolution）
- 知識庫（KB）+ 常見問題自助
- 整合工單系統（Zendesk / Intercom / 自建）

### CL.5 Pricing tier 落地（Stripe / ECPay / PayPal 串接）
- 整合 Priority T billing 的延伸
- Free tier / Pro / Team / Enterprise 四階
- 試用 / 退款 / 升降級流程
- per-tenant credit pool（與 PEP gateway 整合）

### CL.6 Marketing 資產
- Landing page / 產品 demo video / 客戶 case study
- 不是 dev TODO 內、但工程必準備 demo asset（HD reference design / Y6 sample workspace）
- 整合 BS.0.1 vertical-aware bootstrap demo

### CL.7 Real-world device fleet validation（HD）
- 第一個 willing-to-take-risk 客戶 = beta tester
- 特殊 onboarding（client engineer + OmniSight engineer 混合 team）
- 客製 SLA + 風險共擔條款
- 從這客戶 1 顆量產 device → 100 顆 → 1000 顆漸進
- 結果反哺 HD 完工度（HD R36-R57 真實 mitigation 驗證）

### CL.8 規模壓測（10K+ tenant）
- 既有 Y10 是 1000 task 並發 unit test
- 真實 10K tenant 壓測（合成 tenant 資料 + 流量模擬）
- 抓 schema / cache / lock 規模 bug
- 整合 R priority watchdog

### CL.9 跨地理 data residency（中國 / 歐盟 / 美國）
- HD.21.5.3 PEP gateway region routing 是基礎
- 各地 6-12 個月法規對接：個資保護法 / GDPR / FedRAMP / 等保 2.0
- 各地需要 data center partner 或 self-host enabled
- 商務 + legal 主導、工程支援

### CL.10 Real-world security incident response 演練
- KS.4.6 runbook 是文件、需實演
- Tabletop exercise（模擬 incident、跑完 24h SOP）
- Red team / Purple team exercise（外部 + 內部聯合）
- 結果寫進 N10 audit + improve runbook iteration

### CL.11 Bug bounty + 第三方 pentest 持續啟動
- KS.4.4 / KS.4.5 列「準備 / 評估」、CL 階段實啟動
- HackerOne / Bugcrowd 簽約 + scope / payout 政策
- 每季 pentest（外部 + 內部 + RTO drill）
- 結果進 N10 + 公開 transparency report

### CL Definition of Done
- [ ] SOC 2 Type II / ISO 27001 認證取得
- [ ] SLA framework 上線、補償條款生效
- [ ] Customer support workflow + ticket system 運作
- [ ] Pricing tier 完整、可付費 / 退費 / 升降級
- [ ] Real-world device fleet 至少 3 客戶 / 1000 顆 device 驗過
- [ ] 10K+ tenant 規模壓測 0 critical regression
- [ ] 至少 1 個跨地理市場（中國 OR 歐盟 OR 美國 federal）法規對接完成
- [ ] Incident response 演練至少 1 輪、findings 修完
- [ ] Bug bounty + 季度 pentest 啟動

---

## 6. Priority L4 — Beyond-Commercial Excellence（10 項差異化）

> **目標**：跨過 L3 floor、建立**真實差異化護城河**。**部分項目越早建越省工**（reproducibility / provenance / adversarial robustness 是 invariant、不是 ship-able feature）。
>
> **migration**：L4 0141-0160（20 slots）
> **single knob**：`OMNISIGHT_L4_*` per phase 各自有
>
> **時程**：~12-18 個月、可 4-6 項並行

### L4.1 Determinism / Reproducibility Framework
> 同 input → 同 output forever。LLM seed / temperature / prompt / tool result 全鎖、HDIR 鎖、toolchain OCI 鎖、SBOM 雙向 hash 鏈。
- LLM 層：`temperature=0` + 鎖 `seed`（Anthropic / OpenAI 都支援）+ rerun 同 seed → bit-equal
- Agent 層：tool result 全 cache、二次跑同 task 走 cache、不打 LLM
- Output 層：每 artifact 有 deterministic hash、與 input + agent + model 三方綁
- HD 層：HDIR + toolchain OCI + SBOM 鎖鏈、5 年後重 build bit-equal
- alembic 0141 — `deterministic_runs` / `artifact_hashes` 表

### L4.2 Tamper-Evident Provenance DAG
> N10 ledger 升級成 DAG。每 artifact 反查 source / agent / model / parameters / human approver、構成完整 reasoning chain。
- DAG schema：`(artifact_id, parent_artifacts[], producer_agent, model, params, human_approver, timestamp, integrity_hash)`
- Hash chain：每 node 鏈到 parent + N10 root、tamper 即測
- UI：artifact detail panel 一鍵展開完整 DAG（祖先 / 後代 / 旁系）
- 整合 KS.1 audit + WP.9 shareable_objects + N10
- alembic 0142 — `provenance_dag_nodes` / `provenance_dag_edges`

### L4.3 Adversarial Robustness Suite
> Prompt injection / jailbreak / supply chain / 零日攻擊持續紅隊測試。AI agent 核心安全前沿。
- Prompt injection：input scan + output scan（已知 patterns + ML detector）
- Jailbreak：標準 jailbreak set（DAN / Sydney / etc.）回歸測試
- Supply chain：dependency / model / training data attestation 鏈
- Continuous red-team：CI 加 adversarial test stage、每 PR 跑
- 整合 SC + KS.4 + S2

### L4.4 Industry Certification Pack Inheritance
> Generated app 自動繼承 OmniSight 的合規 posture。
- HIPAA pack：BAA template + audit log + encryption defaults + access control template
- PCI-DSS pack：cardholder data scope + tokenization defaults + scan compliance
- FedRAMP pack：federal hosting + access control + continuous monitoring
- GDPR / 個保法 pack：data subject rights + consent / DSAR 流程模板
- 客戶選 pack → generated app 自動帶 baseline 配置
- alembic 0143 — `cert_packs` / `tenant_cert_subscriptions`

### L4.5 Field Telemetry → AI Insights Pipeline（Type B 主）
> Device fleet 回傳遙測、ML 分析、自動推 firmware 改進建議。Closes the loop、形成 data moat。
- Telemetry SDK（OmniSight 提供給 generated firmware embed）
- 後端 ingestion（Kafka / Pulsar / 自建 streaming）
- ML 分析（anomaly detection / trend analysis）
- 自動建議 firmware patch（與 HD.18.7 auto-PR backport 整合）
- 客戶 dashboard 顯示 fleet health
- alembic 0144 — `device_telemetry` / `field_insights`

### L4.6 Real-Time Human + Agent Collaboration
> 多人 + 多 agent 同 workspace、CRDT-based、presence、conflict resolution。
- CRDT layer（Yjs / Automerge）建在 WP.1 Block model 之上
- Presence indicator（誰在看 / 改 / 跑 agent）
- Conflict resolution（agent 改 vs human 改）
- 整合 Q multi-device parity + WP.1 Block
- alembic 0145 — `crdt_state` / `presence_events`

### L4.7 Functional Safety Formal Verification（Type B 主）
> 不只 ISO 26262 文件、跑 model checker / proof assistant 驗證 safety-critical path。
- 整合 TLA+ / Coq / Isabelle / Lean / Why3
- HD bring-up code 走 formal proof（power sequence / clock / safety check）
- 與 HD.10 compliance retest plan 配對升級
- 客戶可選 ASIL-B/C/D 不同 rigor
- 與 ISO 26262 / IEC 62304 / DO-178C 對齊

### L4.8 Skill / Agent Marketplace with Cryptographic Attestation
> WP.2 skills loader 升級。Third-party skill / SBOM / 簽章 / reputation / audit trail。
- Skill SBOM：每 skill 帶 SBOM（依賴 / model / prompt）
- Sigstore / Cosign 簽章
- Marketplace UI：browse / install / review / report
- Reputation system（download / review / verified-by-OmniSight）
- 整合 WP.2 + KS.4 OSS attestation

### L4.9 Cost-per-Decision Meta-LLM Optimization
> 自動選最便宜夠用的模型。Z observability 升級成 auto-decision。
- Per-task confidence threshold + cost ceiling
- Meta-LLM 預估 task 難度 → 動態選 Anthropic / OpenAI / Gemini / Ollama
- Fallback chain：嘗試便宜模型、信心不足升級貴模型
- 學習：歷史 task → 模型選擇 → 結果品質 retrospective
- 整合 Z + BP

### L4.10 Multi-Region Edge Deployment Strategy（Type A 主）
> Generated app 可 deploy 到 Cloudflare Workers / Deno Deploy / Fly.io / Vercel Edge / AWS Lambda@Edge。
- Adapter 層：generated app 程式 portable 跨 edge runtime
- 部署 wizard：客戶選 target → 自動 deploy
- Region routing：依用戶位置 → nearest edge
- Cost / latency 對比 dashboard
- 整合 FS + SC + 跨地理 CL.9

### L4 Definition of Done
- [ ] L4.1-L4.10 各自 GA、knob disable 退化乾淨
- [ ] R63-R69 risks 全 mitigated
- [ ] 至少 3 個 L4 features 對外公布為差異化賣點

---

## 7. Priority L5 — Category-Defining R&D（5 項 R&D 大項）

> **目標**：定義產業的能力。每項是 12-24 個月 R&D 投入、需 domain expert 招募、不是 BAU 工程。
>
> **migration**：L5 0161-0185（25 slots）
> **single knob**：`OMNISIGHT_L5_*` per phase 各自有
>
> **時程**：12-24 個月每項、可獨立啟動

### L5.A Hardware-Software Co-Simulation Pre-Tape-Out（Type B）
> Full SoC 模擬。客戶 tape-out 前完整 HW/SW 整合驗證。HD.8 HIL 升級。
- SystemVerilog / VHDL → Verilator / Icarus 整合
- ARM Fast Models 整合（SoC core）
- Sensor + PMIC + memory 模型整合
- Firmware / driver / kernel 在模擬上跑、抓 race conditions / boot bugs
- Tape-out risk reduction：~30% bring-up bug 在 silicon 前抓出
- 競品：Synopsys VCS / Cadence Xcelium 是 enterprise tool、OmniSight 民主化
- alembic 0161-0163

### L5.B Long-Term Agent Memory + Continual Learning（Type C）
> Agent 跨年記憶。Project context / decision history 自動累積、不需 prompt 重灌。
- Vector store + episodic memory + semantic compression
- 整合 R20 Phase 0 RAG + WP.5 project-context walker
- 持續學習：agent 學客戶 codebase patterns、decision style
- 競品：Mem0 / Letta 雛形、缺 dev workflow 整合
- 隱私：per-tenant 隔離 + KS.1 envelope + 客戶可 export / delete
- alembic 0164-0167

### L5.C Side-Channel + Fault Injection Security Analysis（Type B）
> Power / EM / Glitch attack 自動 analyze。給高安全 IoT。
- Power analysis（DPA / SPA）oracle
- EM emanation analysis
- Voltage glitching simulation
- Fault injection model（單 bit / multi-bit）
- 整合 HD.20.7 firmware blob analysis
- 競品：Riscure / Synopsys SecureIC 是專業工具、OmniSight 整合進 dev workflow
- 客戶：軍工 / 金融 / 醫療嵌入式
- alembic 0168-0170

### L5.D Multi-Agent Simulation + Adversarial Red-Teaming Framework
> Agent vs agent。自動 stress test 系統、ML-driven attack synthesis。AI safety 前沿。
- Adversarial agent 池（攻擊者 / 防禦者）
- 持續對抗訓練
- ML-driven attack synthesis（不只已知 patterns、發掘新攻擊面）
- Findings 自動進 L4.3 adversarial robustness suite
- 整合 SC + KS.4
- 競品：Anthropic / OpenAI 內部用、商品化空白
- alembic 0171-0175

### L5.E Real-Time Digital Twin for Field Devices（Type B）
> 每客戶 device 的活模型。Predictive maintenance、anomaly forecast。
- Per-device 數位模型（physics + behavior + degradation）
- Real-time telemetry → twin update
- Forecast：N 月後 sensor 退化 / 元件壽命終止
- 客戶 fleet management dashboard：哪些 device 即將失效
- 整合 L4.5 field telemetry pipeline
- 競品：PTC ThingWorx / Siemens MindSphere 是 enterprise IoT、OmniSight + AI integration 民主化
- alembic 0176-0180

### L5 Definition of Done
- [ ] L5.A-L5.E 至少 3 項 GA（其餘可 R&D 持續）
- [ ] R70-R75 risks 全 mitigated
- [ ] 至少 1 個 L5 feature 形成業界標準（與 IEEE / ISO / IETF / Linux Foundation 等合作標準制定）
- [ ] 學術 / 研究機構 / 標準制定組織建立合作

---

## 8. R-Series Risks（R63-R75）

### CL 風險 R63-R66
- **R63 SOC 2 audit 失敗**（finding 過多、首次 audit 失敗 → 6 月延期）。Mitigation：簽約前內部 readiness scan + Drata 等工具預跑、findings 修完才送 audit。
- **R64 SLA 補償炸成本**（dual-active 不夠、incident 多 → 退費爆）。Mitigation：SLA 階梯設保守、初期不開最高 tier、累積 6 月 uptime 數據再開放。
- **R65 第一個 beta 客戶 burn**（HD 第一個客戶量產失敗 → 連帶責任）。Mitigation：客製 risk acceptance form + insurance + OmniSight engineer on-site 駐場。
- **R66 跨地法規誤判**（中國 / 歐盟法規進入錯誤）。Mitigation：簽約前 local legal counsel review、不自行解讀、按地法律事務所 retainer。

### L4 風險 R67-R69
- **R67 Determinism 假象**（鎖 seed 但 LLM provider 升版改 sampling → 看似 deterministic 實際 drift）。Mitigation：每月 deterministic regression test、provider 升版前 alert、維持多 provider 驗證冗餘。
- **R68 Provenance DAG 規模爆炸**（百萬 artifact 後 DAG 查詢慢）。Mitigation：分層 storage（hot / warm / cold）+ DAG 摘要 + 分頁 query。
- **R69 Cert pack 過時**（HIPAA / PCI / FedRAMP 標準改、客戶用舊 pack 認知錯）。Mitigation：cert pack 帶 valid_until 強制 expire、每季與 compliance team 同步、外部 legal review 季度走。

### L5 風險 R70-R75
- **R70 HW/SW co-sim 結果 vs real silicon 偏差**（client tape-out 後仍崩、信任崩）。Mitigation：明示「reduce risk 不取代」、每 fabric 與真實 silicon 比對、accuracy 公開報告。
- **R71 Long-term memory 客戶 IP leak**（agent 記憶污染跨 customer）。Mitigation：per-tenant memory 嚴格 KS.1 envelope、嚴禁 cross-tenant memory 引用、季度 audit。
- **R72 Side-channel 分析誤判**（false positive 太多、客戶失信）。Mitigation：confidence + accuracy disclosure、與 Riscure / Synopsys 等工具結果比對、不取代 lab。
- **R73 Adversarial red-team 攻擊外洩**（內部 red-team 工具被外洩 → 變成 weapon）。Mitigation：framework 程式碼分級存取（IP-classified）、沒對外發布、研究 paper 才發。
- **R74 Digital twin 預測失準**（forecast 客戶 device 何時失效但實際早 / 晚 → 客戶損失）。Mitigation：confidence interval 必顯示、不掩蓋 uncertainty、客戶教育。
- **R75 R&D budget 不足**（L5 5 項全做需多年、需 funding）。Mitigation：每項獨立啟動、商務 trigger 觸發特定項、不一次全跑、與 academic / 政府 grant 合作。

---

## 9. Schedule Integration

### 9.1 Sequential 必過項
- L0 → L1：BS / AS / W / FS（基礎設定 + frontend 完整）
- L1 → L2：SC / WP / BP / KS / Q / I / G（多租戶 + auth + agent quality + 後端完整）
- L2 → L3：CL（最後一哩）
- L3 → L4：L4 ten phases（可平行）
- L4 → L5：L5 five R&D（獨立啟動）

### 9.2 Type-specific timelines

| Type | L0 → L1 | L1 → L2 | L2 → L3 | L3 → L4 | L4 → L5 |
|------|---------|---------|---------|---------|---------|
| **A Web SaaS** | W11-W16 完工 | FS + SC + WP-Wave-1 + BP + KS.1 完工 | CL 6-12 月 | L4 12-18 月 | L5.D / L5.E 部分項 |
| **B Embedded** | HD 部分 phase 完工（~5 週後）| HD 全 21 phase 完工（~20 週）+ KS.1 | CL 6-12 月（含 real device fleet validation）| L4 12-18 月（含 L4.5 / L4.7 主要對 B） | L5.A / L5.C / L5.E 主要對 B |
| **C Multi-agent dev** | BS + AS + WP-Wave-1 完工（now-ish）| Q + I + Z + ZZ + BP + KS.1 完工 | CL 6-12 月 | L4 12-18 月 | L5.B / L5.D 主要對 C |

### 9.3 並行機會
- L4.1（Determinism）/ L4.2（Provenance DAG）/ L4.3（Adversarial robustness）= **cross-cutting invariants、越早建越省工** — 建議與 CL 並行、不等 CL 完工
- L4.4-L4.10 = feature-level、依商務 trigger 滾動
- L5.A-L5.E = 獨立 R&D track、各自啟動

---

## 10. Migration Plan

| Range | 內容 |
|-------|------|
| 0126-0140 | CL — SOC 2 readiness / SLA / support / billing / fleet / 規模 / data residency / IR / bounty (15 slots) |
| 0141-0160 | L4 — determinism / provenance / adversarial / cert pack / telemetry / collab / formal verify / marketplace / meta-LLM / edge (20 slots) |
| 0161-0180 | L5 — co-sim / long-term memory / side-channel / multi-agent sim / digital twin (20 slots) |
| 0181-0199 | 預留 |

---

## 11. Open Questions

1. **CL.1 SOC 2 audit 啟動時機**：HD 完工後立即 vs 提早與 KS.1 並行？傾向 KS.1 完工後立即、與 HD 並行。決策推遲到 KS.1 開工前。
2. **L4.1 Determinism 對 LLM 自身 nondeterminism 的妥協程度**：seed-locked LLM 仍有極小變異、目標是 99% reproducibility 還是 100%？決策推遲到 L4.1 開工。
3. **L4.5 Field telemetry 的客戶資料隱私平衡**：telemetry 帶多細節（session / decisions vs 純 health metric）？決策推遲到第一個 fleet 客戶詢盤。
4. **L5.A HW/SW co-sim 商務模式**：含在 OmniSight HD subscription 還是獨立 pricing？傾向獨立（成本高、客戶群不同）。決策推遲到 L5.A POC 完成。
5. **L5.B Long-term memory 與 enterprise customer 資料 ownership**：客戶離開後 memory 怎麼處理？傾向客戶 export + 我方 delete + audit cert。決策推遲到 L5.B 開工。

---

## 12. 參考文件

- `TODO.md` Priority CL / L4 / L5 + 既有所有 priority 的 L-level annotation
- `docs/design/blueprint-v2-implementation-plan.md` BP
- `docs/design/hd-hardware-design-verification.md` HD
- `docs/security/ks-multi-tenant-secret-management.md` KS
- `docs/design/wp-warp-inspired-patterns.md` WP
- `docs/legal/oss-boundaries.md`

---

## 13. Sign-off

- **Owner**: Agent-software-beta + nanakusa sora
- **Date**: 2026-04-30
- **Status**: Accepted（CL / L4 / L5 三 priority 進路線、L1-L3 mapping 進 TODO 既有 priority annotation）
- **Next review**: KS.1 完工後 / CL 啟動前；L4.1-L4.3 並行啟動評估點
