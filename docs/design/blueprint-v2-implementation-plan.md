# ADR-001: Blueprint V2 實施計畫（Enterprise Multi-Agent Software Factory）

## Metadata

| 欄位 | 值 |
|---|---|
| 日期 | 2026-04-24 |
| 狀態 | **Accepted** — 2026-04-24 operator 核准，進入實施階段 |
| 作者 | Agent-software-beta / nanakusa sora / Claude Opus 4.7 |
| 藍圖來源 | `docs/design/enterprise-level-multi-agent-software-factory-architecture.md` |
| 前置里程碑 | Phase-3-Runtime-v2 `deployed-verified` (2026-04-24, commit `5fa5c482`) |
| 執行模式 | **單一序列、不平行**（operator 決策）|
| 預估工時 | **6-8 個月 wall-clock**（單人單序）|
| 核心變更 | Agent topology 從 10 flat types → 21 Guild × 4 Plane |

---

## 1. 執行摘要（TL;DR）

本 ADR 將藍圖 `enterprise-level-multi-agent-software-factory-architecture.md` 切成 **12 個 Phase（A-L）** 逐步落地。

**重要前提已驗證**：OmniSight 現有基礎建設（Multi-tenancy / Auth hardening / HA / Multi-worker / Event-driven orchestration / Web+Software vertical）**全部已完成且穩定**，Blueprint 的地基比初估更穩 — 衝突面從原先盤點的 16 項降到實際需要主動解決的 **9 項**，其餘 7 項自然消解（因為那些子系統都已 Ship）。

**執行模式**：**單一序列、不平行**（operator 決策，不動用 Team 2）。所有 TODO 項目 + Blueprint 12 Phase 全部依序推進。

**關鍵節奏**（operator 指定 Window 0 順序）：
- **Window 0-1 Priority Q**（~1.5 週）：Multi-device parity 剩餘 Q.2-Q.8（7 項 × 含 E2E harness）— 安全 UX 紅線
- **Window 0-2 Phase 4**（~1 週）：Dashboard polling consolidation（`/dashboard/summary` aggregator）
- **Window 0-3 Phase 5**（~2-3 週）：Multi-account forge integrations（GitHub/GitLab/Gerrit/JIRA 多帳號）
- **Window 0-4 Phase 5b**（~1 週）：LLM API key persistence（DB-backed + Fernet encrypted）
- **Window 0-5 Z**（~3.5d）：LLM Provider Observability（rate-limit + balance + pricing + UI）
- **Window 0-6 Y-prep**（~2.5d）：Gerrit/JIRA integration hardening（3 顆 webhook + secret rotation）
- **Window 1（~6-8 週）**：Blueprint 主線低風險優先 — Phase A → I → B → F → H
- **Window 2（~8-12 週）**：Blueprint 深度整合 — Phase C → D → G → J → K → L
- **Phase E（GraphRAG / Neo4j）**：**延後到 v1.0 後**

**總計**：Window 0 = ~7-9 週；Blueprint 主線 = ~14-20 週；**合計約 6-8 個月 wall-clock**。

---

## 2. 已決策清單

| # | 決策 | 狀態 |
|---|---|---|
| D1 | 合規矩陣（醫療/車載/工控/軍規）必做且精實，對外宣稱一律為「輔助檢查（Auxiliary Check）」避法律敞口 | ✅ 已決 |
| D2 | Neo4j / GraphRAG **延後到 v1.0 之後** | ✅ 已決 |
| D3 | Per-Guild model mapping 走**混合三態旗標**（`enforce` / `warn` / `advisory`）| ✅ 已決 |
| D4 | 舊 10 agent_type **全數保留**（合併或改名，一個不少）；新 21 Guild 全數上線 | ✅ 已決 |
| D5 | Phase-3-Runtime-v2 `deployed-verified` 完成 → Blueprint 開工門檻達成 | ✅ 已完（2026-04-24）|
| D6 | 16 項衝突點 → 9 項需主動解決（見 §4） | ✅ 已盤點 |

---

## 3. 21 Guild 合併地圖

### 3.1 合併原則
1. **舊 agent_type 一個不少** — 透過合併、改名、拆分三種手段全數轉型
2. **Guild ID 用 kebab-case 短名**（`bsp`, `hal`, `algo-cv`, `ux`, ...）
3. **舊 enum → Guild ID** 透過 alias table 做 3-6 個月雙寫過渡
4. **檔案位置遷移**：`configs/roles/{firmware,software,...}/*.skill.md` → `configs/guilds/{bsp,hal,...}/*.skill.md`，舊路徑保留 symlink 3 個月

### 3.2 完整對照表

#### 🧠 Command & Control Plane（5 個）

| # | Guild ID | 全名 | 預設 Model | 舊來源 |
|---|---|---|---|---|
| 1 | `architect` | Global / Domain Architect | Opus 4.7 | ⭐ 新（`software-architect.md` 提升） |
| 2 | `sa-sd` | System Analyst / Designer | Sonnet 4.6 | ⭐ 新（`database-optimizer.md` 併入） |
| 3 | `ux` | UX Designer | Gemini 3.1 Pro | ⭐ 新（`ui-designer.md` + `mobile-ui-designer.md` 提升） |
| 4 | `pm` | Project Manager | Sonnet 4.6 | ⭐ 新（吸收舊 `general` 的調度邏輯） |
| 5 | `gateway` | T-shirt Router / Gateway | Haiku 4.5 / Gemma 4 | 改名自舊 `general` |

#### 🔧 Layer 1 — BSP / OS / Firmware（2 個）

| # | Guild ID | 全名 | 預設 Model | 舊來源 |
|---|---|---|---|---|
| 6 | `bsp` | BSP & System Worker | Sonnet 4.6 | 舊 `firmware` 拆半 + `firmware/bsp.skill.md` |
| 7 | `hal` | Firmware & HAL Worker | Sonnet 4.6 | 舊 `firmware` 拆半 + `firmware/hal.skill.md` |

#### 🎥 Layer 2 — Multimedia & Math（4 個）

| # | Guild ID | 全名 | 預設 Model | 舊來源 |
|---|---|---|---|---|
| 8 | `algo-cv` | Algorithm & Computer Vision | Opus 4.7 | 舊 `software/algorithm.skill.md` + 計算密集 |
| 9 | `optical` | Optical Engineering | Sonnet 4.6 | 舊 `mechanical` 改名擴充 + `firmware/mechanical.skill.md` |
| 10 | `isp` | Image Quality / ISP | Sonnet 4.6 | 舊 `firmware/isp.skill.md` 提升層級 |
| 11 | `audio` | Audio & Acoustics | Sonnet 4.6 | ⭐ 新（I2S / 音訊相關從 `firmware` 抽出） |

#### 💻 Layer 3 — App / Backend / Infra（3 個）

| # | Guild ID | 全名 | 預設 Model | 舊來源 |
|---|---|---|---|---|
| 12 | `frontend` | Frontend & GUI | Sonnet 4.6 | 舊 `software` web/mobile UI + `web/*` 5 檔 + `mobile/*` 6 檔 + C26 HMI |
| 13 | `backend` | Backend & Cloud Services | Sonnet 4.6 | 舊 `software` + `backend-{python,go,node,java,rust}.skill.md` |
| 14 | `sre` | Infrastructure & SRE | Sonnet 4.6 | 舊 `devops` 改名 + `sre.md` + `devops/{cicd,manufacturing}.skill.md` |

#### 🧪 Layer 4 — QA & Integration（1 個）

| # | Guild ID | 全名 | 預設 Model | 舊來源 |
|---|---|---|---|---|
| 15 | `qa` | QA & Integration | Sonnet 4.6 | 舊 `validator/sdet.skill.md` + `debugger.skill.md` + `support.skill.md` |

#### 🛡️ Armed Audit Plane（4 個）

| # | Guild ID | 全名 | 預設 Model | 舊來源 |
|---|---|---|---|---|
| 16 | `auditor` | Security & Compliance Auditor | Opus 4.7 | 舊 `reviewer` + `validator/security.skill.md` + `code-reviewer.md` + `security-engineer.md` |
| 17 | `redteam` | Red Team Hacker | Grok 4.2 | ⭐ 新 |
| 18 | `forensics` | Context Absorber / Log Analyst | Gemini 3.1 Pro | ⭐ 新（海量 crash dump 分析） |
| 19 | `intel` | SecOps Threat Intelligence | Gemini 3.1 Pro / Sonnet 4.6 | ⭐ 新（CVE feed + Zero-day） |

#### 📄 Specialty（2 個）

| # | Guild ID | 全名 | 預設 Model | 舊來源 |
|---|---|---|---|---|
| 20 | `reporter` | Reporter / Documentation | Haiku 4.5 | 舊 `reporter` 保留 + 5 skill files + `technical-writer.md` |
| 21 | `custom` | Operator-defined Custom Slot | N/A | 舊 `custom` 保留 |

### 3.3 舊 enum 遷移矩陣

| 舊 `agent_type` | 遷移策略 | 新 Guild ID(s) |
|---|---|---|
| `firmware` | 拆分 | `bsp` + `hal` |
| `software` | 拆分 | `algo-cv` + `frontend` + `backend` |
| `validator` | 拆分 | `qa` + `auditor`（安全類歸 auditor）|
| `reviewer` | 合併 | `auditor`（核心職能）|
| `general` | 改名 | `gateway`（部分吸收到 `pm`）|
| `devops` | 改名 | `sre` |
| `mechanical` | 改名擴充 | `optical` |
| `manufacturing` | 併入 | `sre`（`sre/manufacturing.skill.md`） |
| `reporter` | 保留 | `reporter` |
| `custom` | 保留 | `custom` |

**驗證**：舊 10 個 agent_type → 新 21 Guild，每一條舊路徑都有明確新歸屬，**零遺失**。

---

## 4. 16 項衝突決議登記冊

### 🔴 高阻塞（3 項）

| # | 衝突 | 決議 |
|---|---|---|
| A1 | Phase-3-Runtime-v2 觀察窗未完 | **✅ 已解**（2026-04-24 verify 通過）|
| A2 | `agent_type` enum 歷史資料 backward-compat | **雙寫 3-6 個月**：alembic 新增 `guild_id` 欄位保留舊欄位；讀走 guild_id（透過 alias view），寫兩欄；舊欄位 6 個月後刪 |
| A3 | 合規宣稱法律責任 | **一律標「Auxiliary Check」**：module header、函數名 `_auxiliary_` 前綴、API response 強制包 `{"audit_type": "advisory", "requires_human_signoff": true}` |

### 🟠 中阻塞（7 項，需設計調整）

| # | 衝突 | 決議 |
|---|---|---|
| B1 | PEP Gateway (R0) per-Guild 差異化 | policy matrix 加 `guild_id` 維度 + policy 繼承；`backend/pep.py` 擴充 ~400 LOC |
| B2 | O4 Orchestrator Gateway vs T-shirt Gateway 重疊 | **T-shirt 前置於 O4**：JIRA → T-shirt → O4 → CATC；O4 擴 ~150 LOC |
| B3 | Token Budget per-Guild 2-4x 膨脹 | Budget 拆成 per-Guild buckets；90% downgrade 不跨 Guild、觸發 PM 切更小粒度；`budget.py` 重寫 ~300 LOC |
| B4 | Skill Packs（X5-X9 + D1 + W6-W8）歸 Guild | `SKILL_HOOK_TARGETS` → `GUILD_DEFAULT_TARGETS`；既有 X/W/D pack 測試鍵值同步，~50 LOC per pack |
| B5 | C26 HMI Framework vs `frontend` Guild | `frontend` Guild 下分 `web`/`mobile`/`hmi` sub-skill；`/api/v1/hmi/*` 保留向前相容 |
| B6 | Slash commands `/spawn firmware` | alias map：舊命令提示「已拆為 `bsp`/`hal`」；3 個月 deprecated 期；`slash_commands.py` ~80 LOC |
| B7 | R0-R4（PEP/ChatOps/Entropy/Scratchpad/Snapshot）需 Guild-aware | 5 個 module 加 `guild_id` label（non-breaking additive）；~60 LOC × 5 |

### 🟢 低阻塞（6 項，局部調整）

| # | 衝突 | 決議 |
|---|---|---|
| C1 | Bootstrap Wizard (L1-L8) 已完，需加 Guild 步驟 | 新增 Step 6「Guild Enablement」；預設 21 Guild 全開，operator 可取消冷門；~200 LOC |
| C2 | Multi-tenancy（Priority I 已完）vs Guild 配置 | Guild def 走 global，enabled 列表 + model override 走 `tenant_guild_config` 表；新 alembic ~100 LOC |
| C3 | Test 執行時間暴漲 | CI shard 4→8-way + pytest marker 分級（critical / guild_loadout / compliance）|
| C4 | `C0 ProjectClass enum` vs `Target_Triple` | 三維正交並存（ProjectClass = 業務領域 / Target_Triple = 編譯目標 / T-shirt = 規模），無衝突 |
| C5 | Notification 4-tier (L1-L4) vs Red Card | Red Card 映射到 L3 Jira + L4 PagerDuty；`notifications.py` 加 `is_red_card` bool ~50 LOC |
| C6 | 37 個 role skill 檔物理位置 | 遷 `configs/roles/` → `configs/guilds/`；舊路徑保 symlink 3 個月；腳本 ~1 天 |

---

## 5. 12-Phase 實施路線圖

### Phase A — 4 Templates + Pydantic Schema（1-2 週）

**範圍**：Spec Template / Task Template / Impl Template / Review Template 的 Pydantic 定義 + FastAPI validation + state machine + cognitive load scanner（fan-in/out/mock limit）。

**前置**：無（獨立新 module）

**交付**：
- `backend/templates/{spec,task,impl,review}.py` — 4 個 Pydantic model
- `backend/cognitive_load.py` — fan-in/out/mock 量化器
- `backend/template_validator.py` — Attention-enforcement 中介層
- `backend/tests/test_templates.py` — ~120 unit test

**工時**：1-2 週 / 3-5 commits / 單 session 可完

**風險**：低 — 純 additive，不動現有 topology

---

### Phase B — Guild 重組 + AGENT_TOOLS 拆分（2-3 週）

**範圍**：`AGENT_TOOLS: dict[str, list]` 從 10 key → 21 key；每 Guild 專屬 tool loadout；`configs/roles/` → `configs/guilds/`；alembic 0019 加 `guild_id` 欄位 + 雙寫 migration。

**前置**：Phase A（Templates 格式用於 Guild spec sheet）

**交付**：
- `backend/agents/guilds/` 新目錄，21 個 Guild definition 檔
- `backend/agents/tools.py` 重構 AGENT_TOOLS → GUILD_TOOLS (+舊 alias)
- alembic 0019：`workflow_run` / `debug_findings` / `audit_log` 各加 `guild_id TEXT`
- `backend/agents/nodes.py` 重構：`_specialist_node_factory` → `_guild_node_factory`，同時接受舊 `agent_type` 別名
- `configs/guilds/{architect,sa-sd,ux,pm,gateway,bsp,hal,algo-cv,optical,isp,audio,frontend,backend,sre,qa,auditor,redteam,forensics,intel,reporter,custom}/` 共 21 目錄
- 舊 `configs/roles/` symlink 保留

**工時**：2-3 週 / 15-20 commits / 3-5 sessions

**風險**：🟠 中 — backward-compat 雙寫 3-6 個月需嚴謹；alembic 遷移不可逆點

---

### Phase C — T-shirt Gateway + S/M/XL 三條 Topology（2-3 週）

**範圍**：`backend/agents/graph.py`（230 LOC）重寫支持 3 種 DAG topology；新增 `backend/routers/orchestrator_gateway.py` 前置 T-shirt sizing。

**前置**：Phase B（Guild 可被路由）

**交付**：
- `backend/graph_topology.py` — S/M/XL 三種 DAG builder
- `backend/t_shirt_sizer.py` — Haiku/Gemma 調 LLM 評估 S/M/XL
- `backend/routers/orchestrator.py` 加 sizing 前置層
- `GraphState` 加 `size: Literal["S","M","XL"]` 欄位
- `backend/tests/test_topology_smxl.py` — ~90 test

**工時**：2-3 週 / 10-12 commits

**風險**：🟠 中 — graph.py 全重寫，需 feature flag `OMNISIGHT_TOPOLOGY_MODE=legacy|smxl`

---

### Phase D — 4 產業合規矩陣（輔助檢查版）（2-3 週 + 法務週）

**範圍**：Medical / Automotive / Industrial / Military 4 個合規模組，**全部標 Auxiliary**；Auditor Guild 動態掛載 compliance matrix。

**前置**：Phase B（Auditor Guild 存在）

**交付**：
- `backend/compliance_matrix/medical.py` — IEC 62304 / ISO 13485 / HIPAA auxiliary check
- `backend/compliance_matrix/automotive.py` — ISO 26262 / MISRA C / AUTOSAR auxiliary check
- `backend/compliance_matrix/industrial.py` — IEC 61508 / SIL auxiliary check
- `backend/compliance_matrix/military.py` — DO-178C / MIL-STD-882E auxiliary check
- 每個 module header 強制 disclaimer `"This is an auxiliary check tool. AI-assisted output MUST be reviewed by a human certified engineer."`
- 新審計技能 10+ 個（全以 `_auxiliary_` 前綴命名）
- API response schema 強制包 `audit_type="advisory"` + `requires_human_signoff=true`
- `backend/tests/test_compliance_matrix.py` — ~80 test

**工時**：2-3 週 + 法務 review 1 週

**風險**：🔴 高 — 涉及法律責任，**需 operator 確認 legal review 已進行**

---

### Phase E — GraphRAG / Neo4j

> **⏸️ 延後到 v1.0 之後**（D2 已決）

---

### Phase F — 混合三態 Model Mapping（1 週）

**範圍**：`OMNISIGHT_MODEL_MAPPING_MODE=enforce|warn|advisory` 三態；per-Guild 預設 model；違反時依 mode 拒絕/告警/僅日誌。

**前置**：Phase B（Guild exists）

**交付**：
- `backend/agents/llm.py::get_llm()` 加 mapping guardrail ~50 LOC
- `backend/guild_model_map.py` — 21 Guild 預設 model 對照表
- `configs/model_mapping.yaml` — operator 可改寫
- Prometheus metric `omnisight_model_mapping_violation_total{guild_id,mode}`
- `backend/tests/test_model_mapping.py` — ~40 test

**工時**：1 週 / 3-4 commits

**風險**：🟢 低 — 純新增 + 旗標控制

---

### Phase G — TDD Dual-Patchset 自動化（Gerrit Hook）（1-2 週）

**範圍**：Patchset A（純 test）→ Gerrit 推送 → Patchset B（純實作）Commit 加 `Depends-On: <Patchset-A-Change-Id>` 強制信任鏈。

**前置**：Phase B（QA Guild 存在）+ O7 Submit-Rule（已完）

**交付**：
- `backend/gerrit_tdd.py` — 雙 patchset 產生器
- `backend/hooks/gerrit_depends_on.py` — Commit 欄位驗證 hook
- `Gerrit submit-rule` 擴展：Depends-On 未通過 → refuse submit
- `backend/tests/test_gerrit_tdd.py` — ~35 test

**工時**：1-2 週 / 5-6 commits

**風險**：🟢 低 — O6/O7 基建已在

---

### Phase H — 3 級懲罰階梯 + Red Card 熔斷（1 週）

**範圍**：CI hard rejection（已有）→ Cognitive penalty prompt 回注（新）→ Red Card 3 連 -1 熔斷 API 權限（新）。

**前置**：R0 PEP（已完）+ R2 Semantic Entropy（已完）+ Watchdog（已完）

**交付**：
- `backend/cognitive_penalty.py` — 將 CI report 轉為警告 prompt 回注
- `backend/red_card.py` — 3 連 `Verified -1` → 斷 API + Jira `[BLOCKED]`
- Notification 加 `is_red_card` bool
- `backend/tests/test_red_card.py` — ~25 test

**工時**：1 週 / 3 commits

**風險**：🟢 低

---

### Phase I — SecOps Threat Intel Agent（1-2 週）

**範圍**：Gemini 3.1 Pro / Sonnet 4.6 驅動的 CVE feed / zero-day scanner，聯動 Auditor + Red Team。

**前置**：無（獨立新 agent）

**交付**：
- `backend/secops_intel.py` — `search_latest_cve()` / `query_zero_day_feeds()` / `fetch_latest_best_practices()`
- `configs/guilds/intel/` — skill pack
- 整合點：Integration Engineer pre-install 觸發、Architect pre-blueprint 觸發
- `backend/tests/test_secops_intel.py` — ~30 test

**工時**：1-2 週 / 4-5 commits

**風險**：🟢 低

**與 TODO 重疊**：
- 與 `S2-8 GitHub Repo 安全 + Secret Scanning` 功能有部分重疊 → 合併
- 與 `N2 Renovate 自動 PR` CVE 審查有協同 → 互補

---

### Phase J — Self-healing Docs Watchdog（1 週）

**範圍**：代碼 merge master 後自動反向更新 Markdown 技術文件 / Swagger / ER Diagram。

**前置**：Phase B（Reporter Guild 存在 + openapi.json auto-gen 已有）

**交付**：
- `backend/self_healing_docs.py` — 偵測 API change → 更新 Swagger + architecture.md
- `backend/hooks/post_merge_docs.py` — git post-merge hook
- `backend/tests/test_self_healing_docs.py` — ~20 test

**工時**：1 週 / 3 commits

**風險**：🟢 低，屬 polish，可延後

---

### Phase K — Frontend 6 component 重組（2-3 週）

**範圍**：18 個 omnisight component 中 6 個受 Guild 影響的重寫（`agent-matrix-wall` / `operations-console` / `pipeline-timeline` / `orchestration-panel` / `ops-summary-panel` / `integration-settings`）。

**前置**：Phase B（Guild IDs exist）+ **Phase 4 Dashboard aggregator**（TODO A3 Phase 4，非藍圖 Phase — 避免前端雙重重寫）

**交付**：
- 6 component 加 Guild dimension + compliance badge
- 新 `components/omnisight/guild-topology-view.tsx`（S/M/XL 顯示切換）
- 新 `components/omnisight/compliance-matrix-badge.tsx`
- 前端 Jest test 擴充 ~60 test

**工時**：2-3 週 / 10-15 commits

**風險**：🟠 中 — 受 Phase 4 阻塞

---

### Phase L — Test 調整 + 新增（2-3 週，可部分並行）

**範圍**：既有 957 test 中 ~200 要調整；Blueprint 新增 ~680 test（各 phase 已攤在自己預算，本 phase 是聚合收尾）。

**前置**：A-K 各 phase 自己的 test 已寫

**交付**：
- pytest marker 分級：`@critical`（~200）/ `@guild_loadout`（~400）/ `@compliance`（~200）
- CI workflow 分三階段跑（critical 5min / loadout 30min / compliance 60min）
- Coverage gate 套到 new module

**工時**：2-3 週（大部分 phase A-K 中攤掉）

**風險**：🟢 低

---

### Phase 彙總

| Phase | 內容 | 工時 | 前置 | 風險 |
|---|---|---|---|---|
| **A** | 4 Templates + Cognitive Load | 1-2 週 | — | 🟢 |
| **B** | Guild 重組 + AGENT_TOOLS | 2-3 週 | A | 🟠 |
| **C** | T-shirt Gateway + S/M/XL | 2-3 週 | B | 🟠 |
| **D** | 4 合規矩陣（輔助）| 2-3 週 + 法務 | B | 🔴 |
| **E** | GraphRAG / Neo4j | **延後 v1.0+** | — | — |
| **F** | Model Mapping 三態旗標 | 1 週 | B | 🟢 |
| **G** | TDD Dual-Patchset | 1-2 週 | B | 🟢 |
| **H** | 3 級懲罰 + Red Card | 1 週 | R0/R2/Watchdog（已完）| 🟢 |
| **I** | SecOps Threat Intel | 1-2 週 | — | 🟢 |
| **J** | Self-healing Docs | 1 週 | B | 🟢 |
| **K** | Frontend 6 component | 2-3 週 | B + TODO Phase 4 | 🟠 |
| **L** | Test 分級聚合 | 2-3 週 | A-K | 🟢 |
| **合計** | | **~18-25 週**（單人）| | |

---

## 6. 與 TODO.md 現有項目的交集分析（核心章節）

### 6.1 TODO 整體狀態盤點

| Priority | 已完成 | 未完成 | 阻塞 | 比例 |
|---|---|---|---|---|
| A | 11 | 0 | 3 | ✅ 92% |
| B | 125 | 0 | 1 | ✅ 99% |
| C | 158 | 0 | 0 | ✅ 100% |
| D | 6 | 123 | 0 | ⚪ 5% |
| E | 0 | 65 | 0 | ⚪ 0% |
| F | 0 | 10 | 0 | ⚪ 0% |
| G | 38 | 0 | 0 | ✅ 100% |
| H | 21 | 16 | 0 | 🟡 57% |
| I | 40 | 0 | 0 | ✅ 100% |
| J | 22 | 0 | 0 | ✅ 100% |
| K | 34 | 0 | 0 | ✅ 100% |
| L | 45 | 0 | 1 | ✅ 98% |
| M | 35 | 0 | 0 | ✅ 100% |
| N | 42 | 0 | 2 | ✅ 95% |
| O | 81 | 0 | 0 | ✅ 100% |
| P | 48 | 10 | 0 | 🟡 83% |
| Q | 4 | 31 | 0 | 🟡 11% |
| R | 49 | 37 | 0 | 🟡 57% |
| S | 21 | 35 | 0 | 🟡 38% |
| T | 0 | 85 | 0 | ⚪ 0% |
| V | 44 | 12 | 0 | 🟡 79% |
| W | 51 | 0 | 0 | ✅ 100% |
| X | 39 | 0 | 0 | ✅ 100% |
| Y | 0 | 76 | 0 | ⚪ 0% |
| Z | 0 | 31 | 0 | ⚪ 0% |

**關鍵觀察**：與 Blueprint 可能衝突的**基礎建設**（I/J/K/M/O/G/W/X 八大支柱）全數 100% 完成並穩定。Blueprint 實施的地基已備齊。

---

### 6.2 必須先完成的 TODO 項目（Blueprint 前置）

這些 TODO 項目**必須在 Blueprint 對應 Phase 開工前完成**，否則會引發雙重重寫或設計邏輯斷裂。

| TODO 項目 | 狀態 | Blocks | 原因 |
|---|---|---|---|
| **A3 row 58** CF WAF Custom Rule 5 清理 | ⚪ 未完成 | — | 獨立、30 秒 dashboard 動作，做了避免後續部署 noise |
| **A2** L1-05 Prod smoke test（2 DAG）| ⚪ 未完成 | Blueprint 開工 | 驗證現有 v0.1.0 production 穩定性，避免 Blueprint 動工時誤判 regression |
| **Phase 4** Dashboard polling consolidation（A3 下子項）| ⚪ 未完成 | Blueprint Phase K | 前端 Guild 重組要加 Guild 維度到 aggregator；若 Phase 4 未完就動 K，會發生「先雙倍工作量、再推倒重來」|
| **Phase 5** Multi-account forge（A3 下子項）| ⚪ 未完成 | Blueprint Phase B | Guild-scoped credential routing 需要 Phase 5 的 `url_patterns` 基底；若 Phase 5 沒做，Phase B 要自己生一份 multi-account schema |
| **Phase 5b** LLM API key persistence（A3 下子項）| ⚪ 未完成 | Blueprint Phase F | per-Guild model mapping 在 UI 層要 rotate key；若 5b 沒完，Guild mapping 配置只能 runtime-only |
| **Y-prep** Gerrit/JIRA integration hardening | ⚪ 未完成（3 顆）| Blueprint Phase G | TDD dual-patchset 依賴 Gerrit 入站 webhook routing 已硬化 + JIRA secret rotate API；未 harden 會引入 flaky |
| **B12** UX-CF-TUNNEL-WIZARD | 🟡 部分完成 | — | 不阻塞 Blueprint，但 A3 部分 follow-up |

**總工時（必須先做）**：~4-5 週

---

### 6.3 會被 Blueprint 淘汰 / 取代的 TODO 項目（建議**別動**）

這些 TODO 項目的內容會被 Blueprint Phase B / C 重寫覆蓋，**若現在做等於白工**。

| TODO 項目 | 狀態 | 被 Blueprint 覆蓋的 phase | 建議 |
|---|---|---|---|
| **B8** DAG toolchain enum / autocomplete | 未確認 | Phase B Guild ID 重定義 | ⏸️ 暫緩，Phase B 落地後再做 |
| **B16** Role Skill 強化 — Cherry-pick Agency-Agents + Pattern Upgrade | 未確認 | Phase B Guild 重組全重寫 | ⏸️ 取消，Phase B 會從頭設計 |
| **C0** L4-CORE-00 ProjectClass enum + multi-planner routing | ⚪ 未完成 | Phase C T-shirt Gateway 正交並存 | 🔄 不淘汰但要協調：ProjectClass = 業務領域 / Target_Triple = 編譯目標 / T-shirt = 規模，三維並存 |
| **V 系列部分** Visual Design Loop 剩餘 12 項（V3 / V6-V9 等）| 🟡 79% 完成 | Phase B 後重組（agent type 變）| ⏸️ 暫緩剩餘，等 Phase B 落地 |
| **D3-D29** Skill packs（**D1 + D2 pilot 豁免**）| ⚪ 未開工 | Phase B Guild 歸屬重定義 | ⏸️ **批次暫緩**，Phase B 後重啟 |
| ~~**D2-D29**~~ | — | — | **修訂 2026-04-24**：原估「per pack 省 30% 工時」過度保守；實測 skill pack 是 per-product vertical 產物（類似 X5/W6），Guild 為 agent topology，兩者正交；rework 實際 ~5-10%（只改 `SKILL_HOOK_TARGETS` dict key + test parametrize）。D2 SKILL-IPCAM 已 ship 兩 sub-item（D2.1 RTSP scaffold + D2.2 ONVIF Profile S，108 test + live smoke 過）、追認 pilot 身份與 D1 對稱 |

**總節省工時**：~25-35 週（D3-D29 暫緩；D2 已 pilot 豁免，工時花費計入當下而非 Window 3）

---

### 6.4 會被 Blueprint 阻塞的 TODO 項目（必須等 Blueprint 做完）

這些 TODO 項目若現在動會遭遇 Blueprint 動到同區域 → **建議等 Blueprint 至少 Phase B 完成後再動**。

| TODO 項目 | 狀態 | 被 Blueprint 哪個 phase 阻塞 | 最早可動時點 |
|---|---|---|---|
| **D3-D29** 27 個 embedded skill packs | ⚪ 未開工 | Phase B（SKILL_HOOK_TARGETS dict key 對齊新 Guild ID）| Phase B 完 |
| **D2 SKILL-IPCAM** | 🟡 部分完成（D2.1 + D2.2 pilot 已 ship 2026-04-24）| 不阻塞（pilot 豁免，與 D1 對稱）| **可繼續推進**；Phase B 時走一般 dual-write 遷移路徑（A2 衝突決議覆蓋）|
| **E1-E15** 15 個 software track | ⚪ 未開工 | Phase B（`algo-cv` / `isp` / `optical` Guild 歸屬）+ Phase D（合規矩陣決定 track 的 audit profile）| Phase B + D 完 |
| **F1-F3** META bundles | ⚪ 未開工 | Phase B + D 完成後才能 meta | Phase B + D 完 |
| **V3 / V6-V9** Visual Design Loop 剩餘 | 🟡 79% | Phase B + K Frontend（UI designer agent 要 Guild-aware）| Phase B 完 |
| **T1-T9** Billing（部分）| ⚪ 未開工 | 弱耦合，T 可並行進行（Guild 維度可後加）| **不阻塞** |
| **Y 整系列** Tenant Ops | ⚪ 未開工 | Phase B + C2 `tenant_guild_config` 表設計 | Phase B 完 |
| **L9-L11** Quick-start 一鍵部署剩餘 | 🟡 部分完成 | Phase B + C1 Bootstrap Step 6 | Phase B 完 |

**總阻塞工時**：~50-80 週（主要來自 D/E/Y 三大系列）

---

### 6.5 可並行的 TODO 項目（兩條工作線同時推進）

這些 TODO 項目與 Blueprint **零衝突或低衝突**，可以指派給 Team 2 並行推進。

| TODO 項目 | 狀態 | 工時 | 與 Blueprint 關聯 |
|---|---|---|---|
| **Z 系列**（LLM Provider Observability Z.1-Z.5）| ⚪ 未開工 | 3.5d | 💚 完全不衝突，甚至是 Phase F model mapping 的天然補充 |
| **ZZ 系列**（Claude-Code Observability A/B/C wave）| ⚪ 未開工 | 5d | 💚 完全不衝突，agent observability 對 Blueprint 有加成（Guild 維度自動 propagate）|
| **Q.2-Q.8**（Multi-device parity）| ⚪ 未完成 | 6.5-7d | 💚 純前端 UX，與 Blueprint 無交集 |
| **S2-2/3/4/6/7/8** Security Hardening 剩餘 | 🟡 部分 | ~5d | 💚 S2-8 可與 Phase I SecOps 互補；S2-2/3/4 純網路層 orthogonal |
| **R4-R9** Watchdog & DR 剩餘 | 🟡 部分 | ~3 週 | 💚 R4 斷點續傳 / R5 HA / R6 Serverless orthogonal，R8/R9 與 Phase H red card 互補 |
| **H4a/b** Host-aware Coordinator 剩餘 | 🟡 57% | ~1 週 | 💚 純 infra 層，orthogonal |
| **T 系列** Billing | ⚪ 未開工 | 5 週 | 💚 per-tenant 計費 orthogonal；Guild 維度後加即可 |
| **P9-P12** Mobile vertical 剩餘 | 🟡 83% | ~4.5d | 💛 建議等 Phase B 完再做 P11 Android CLI + P12 MCP；P9 Flutter/RN 可並行 |

**總並行工時**：~10-15 週（若 Team 2 全力推進）

---

### 6.6 建議執行節奏（單序不平行 Timeline）

> **決策**：operator 2026-04-24 明示「單序不平行」— 不動用 Team 2，所有項目依序推進。
> **Window 0 順序** operator 指定：`Priority Q → Phase 4 → Phase 5 → Phase 5b → Z / Y-prep`

```
Window 0 — TODO 收尾 + Blueprint 前置 (~7-9 週)
├── W0.0  A3 row 58 CF WAF cleanup (30 秒) ← trivial，順手收
├── W0.0  A2 prod smoke test (~30 min)
├── W0.1  Priority Q 收尾 Q.2-Q.8 (~1.5 週) ← operator 指定 #1
│        ├─ Q.2 新裝置登入通知 (1d)
│        ├─ Q.5 Active device presence indicator (0.5d)
│        ├─ Q.8 Multi-device E2E harness (0.5d)
│        ├─ Q.3 Cross-device state sync 盤點 (1.5d)
│        ├─ Q.4 SSE event scope policy 審視 (1d)
│        ├─ Q.6 Draft persistence across devices (1d)
│        └─ Q.7 Optimistic concurrency 擴張 (1d)
├── W0.2  Phase 4 Dashboard aggregator (~1 週) ← operator 指定 #2
│        ├─ 4-1 /dashboard/summary aggregator endpoint
│        ├─ 4-2 Frontend demux (lib/api.ts + use-engine.ts)
│        ├─ 4-3 Poll 5s → 10s + SSE 互補
│        ├─ 4-4 Panel-local polling triage
│        ├─ 4-5 Rate limit 1200 → 300 回防禦值
│        └─ 4-6 Multi-tab 30 min soak test
├── W0.3  Phase 5 Multi-account forge (~2-3 週) ← operator 指定 #3
│        ├─ 5-1 alembic 0019 git_accounts table
│        ├─ 5-2 ~ 5-8 Backend resolver + CRUD + 4 platform sweep
│        ├─ 5-9 UI rewrite AccountManagerSection
│        ├─ 5-10 Deprecation docs
│        ├─ 5-11 Soak + security audit
│        └─ 5-12 (optional) OAuth flow 預留
├── W0.4  Phase 5b LLM API key persistence (~1 週) ← operator 指定 #4
│        ├─ 5b-1 alembic 0020 llm_credentials table
│        ├─ 5b-2 resolver refactor
│        ├─ 5b-3 CRUD endpoint + /test live probe
│        ├─ 5b-4 UI rewrite LLM PROVIDERS section
│        ├─ 5b-5 Legacy .env auto-migration
│        └─ 5b-6 Deprecation + docs
├── W0.5  Z LLM Provider Observability (~3.5d) ← operator 指定 #5
│        ├─ Z.1 rate-limit header 擷取 (0.5d)
│        ├─ Z.2 DeepSeek + OpenRouter balance (1d)
│        ├─ Z.3 Pricing YAML hot-reload (0.5d)
│        ├─ Z.4 UI per-provider roll-up (1d)
│        └─ Z.5 Tests + 支援度矩陣 (0.5d)
└── W0.6  Y-prep Gerrit+JIRA hardening (~2.5d) ← operator 指定 #5 (同 tier)
         ├─ Y-prep.1 Gerrit webhook 3-event routing test
         ├─ Y-prep.2 JIRA webhook secret rotation API
         └─ Y-prep.3 JIRA 入站 webhook 事件路由器

Window 1 — Blueprint 主線低風險優先 (~6-8 週)
├── Phase A: 4 Templates + Cognitive Load (1-2 週)
├── Phase I: SecOps Intel Agent (1-2 週)
├── Phase B: Guild 重組 + AGENT_TOOLS (2-3 週) ← 主結構變更
├── Phase F: Model Mapping 三態旗標 (1 週) ← after B
└── Phase H: 3 級懲罰 + Red Card (1 週)

Window 2 — Blueprint 深度整合 (~8-12 週)
├── Phase C: T-shirt Gateway + S/M/XL topology (2-3 週)
├── Phase D: 4 合規矩陣（輔助）(2-3 週)
│        ⚠️ 第三方公正單位 legal review（與開發並行進行，不阻塞）
├── Phase G: TDD Dual-Patchset (1-2 週)
├── Phase J: Self-healing Docs (1 週)
├── Phase K: Frontend 6 component (2-3 週) ← Phase 4 W0.2 已完，不阻塞
└── Phase L: Test 分級聚合 (2-3 週)

Window 3 — Backlog 收尾（Blueprint 完成後）
├── D3-D29: 27 個 skill packs（Phase B 完後對齊新 Guild ID，rework ~5-10% per pack）
│        （D2 SKILL-IPCAM 已 pilot 與 D1 對稱，sub-items 可在主線期間就地推進）
├── E1-E15: 15 個 software track
├── Y 系列: Tenant Ops (~5.5 週)
├── T 系列: Billing (~5 週)
├── V 系列剩餘: Visual Design Loop 12 項
├── F1-F3: META bundles
├── S2/R4-R9/H4/P9-12 等剩餘 orthogonal 項目
└── ZZ 系列: Agent observability 補強

Window 4 — 延後至 v1.0 後
└── Phase E: GraphRAG / Neo4j
```

**總 wall-clock**：Window 0 (~7-9 週) + Window 1 (~6-8 週) + Window 2 (~8-12 週) = **~21-29 週 ≈ 5-7 個月**（Blueprint 主線）

加上 Window 3 backlog 收尾，全部完成約 **9-12 個月 wall-clock**（含所有 TODO 項目）。

---

## 7. Feature Flag 與 Backward-compat 策略

### 7.1 Feature Flag 清單

```bash
# Blueprint 主線總開關
OMNISIGHT_BLUEPRINT_MODE=legacy|guild-preview|guild-enforce

# 各 Phase 獨立旗標
OMNISIGHT_TOPOLOGY_MODE=legacy|smxl              # Phase C
OMNISIGHT_MODEL_MAPPING_MODE=enforce|warn|advisory # Phase F
OMNISIGHT_COMPLIANCE_MATRIX_ENABLED=false|auxiliary # Phase D
OMNISIGHT_TDD_DUAL_PATCHSET=off|on                # Phase G
OMNISIGHT_RED_CARD_ENABLED=false|true             # Phase H
OMNISIGHT_SECOPS_INTEL_ENABLED=false|true         # Phase I
OMNISIGHT_SELF_HEALING_DOCS=off|on                # Phase J

# 遷移期控制
OMNISIGHT_GUILD_ALIAS_MODE=dual-write|guild-only  # Phase B 遷移期
```

### 7.2 Backward-compat 策略

| 項目 | 策略 | 期限 |
|---|---|---|
| 舊 `agent_type` enum | 雙寫 `agent_type` + `guild_id`，讀走 guild_id | 3-6 個月 |
| `AGENT_TOOLS` dict | 保留舊 key 當 alias，指向新 Guild loadout | 3 個月 |
| `configs/roles/` 路徑 | Symlink → `configs/guilds/` | 3 個月 |
| Slash commands `/spawn firmware` | alias map + 提示訊息 | 3 個月 |
| API `/agents/{agent_type}` endpoint | 保留 + 302 redirect 到 `/guilds/{guild_id}` | 3 個月 |

---

## 8. 風險登記冊

| # | 風險 | 嚴重度 | 機率 | 對策 |
|---|---|---|---|---|
| R1 | 合規宣稱被法律認為過強 | 🔴 高 | 🟡 中 | 所有 module header 強制 auxiliary disclaimer + **第三方公正單位 legal review**（operator 已決，2026-04-24）|
| R2 | `agent_type` 遷移導致歷史資料查詢斷裂 | 🟠 中 | 🟡 中 | 雙寫 3-6 個月 + alias view + 遷移前 DB snapshot |
| R3 | LLM cost 失控（Guild 鎖死 + 多 provider）| 🟠 中 | 🟢 低 | Phase F 混合三態旗標 + per-Guild budget bucket（B3 決議）|
| R4 | Provider outage（Grok/Gemini）整條 Guild 停擺 | 🟠 中 | 🟡 中 | 混合模式 `warn` 允許 fallback；prod 用 `enforce` 但強制配 fallback chain |
| R5 | Phase B 雙寫期間 bug 同步不一致 | 🟠 中 | 🟢 低 | 雙寫 + regression test + nightly `guild_id == agent_type_alias(old)` 校驗 |
| R6 | Blueprint 4-6 月窗期間 hotfix 難合併 | 🟡 低 | 🟢 低 | `feat/blueprint-v2` branch 每週 rebase on master |
| R7 | Test suite 時間暴漲拖慢 CI | 🟢 低 | 🟡 中 | CI shard 4→8-way + pytest marker 分級 |
| R8 | Neo4j 延後後重啟技術債 | 🟢 低 | 🟢 低 | v1.0 後開 ADR-002 重啟議題 |
| R9 | D1/D2 pilot 豁免後 D3-D29 陸續有人「跟進 pilot」推進造成範圍失控 | 🟡 低 | 🟡 中 | **明確 gate**：僅 D1 (SKILL-UVC) + D2 (SKILL-IPCAM) 享豁免（source_of_truth 在此 ADR R9 + TODO BP.W3.1 註腳）；D3+ 須 Phase B 完後才啟動；任何 agent session 若欲繞過須先提交 ADR 修訂 PR |

---

## 9. 下一步動作清單

### 9.1 Operator 最終確認（Critical Path）— 2026-04-24 全數完成

- [x] **核准本 ADR** 作為 Blueprint 實施指導文件 *(2026-04-24 operator confirmed)*
- [x] **委外 legal review** — Phase D 合規宣稱將**由第三方公正單位進行 review** *(2026-04-24 operator confirmed)*
- [x] **執行模式**：**單一序列，不平行**（不動用 Team 2）*(2026-04-24 operator confirmed)*
- [x] **Window 0 順序**：`Priority Q → Phase 4 → Phase 5 → Phase 5b → Z / Y-prep` *(2026-04-24 operator confirmed)*

ADR 狀態：**Proposed → Accepted**（2026-04-24）

### 9.2 技術前置（Agent 可直接開工）

一旦 ADR 核准，Agent 可立即啟動：
1. 開 `feat/blueprint-v2` long-lived branch
2. Phase A `4 Templates` — 獨立新 module，零衝突
3. 並行 Phase I `SecOps Intel` — 零前置

### 9.3 阻塞但可排程

- Window 0 完成後觸發 Blueprint 主線（Phase A → B → C → ...）
- D3-D29 / E / Y 系列建議 Phase B 完成後批次重啟（rework ~5-10% per pack）
- D1 + D2 pilot 豁免：可在 Blueprint 主線期間就地推進（已 ship 部分會吃 Phase B 雙寫遷移，零額外 rework）

### 9.4 運營動作

- [ ] 更新 `README.md`「Key Features」加 Blueprint V2 章節
- [ ] `HANDOFF.md` archive 2026-04-17 以前 entry 到 `docs/handoff_archive/2026-04.md`
- [ ] 建立 `docs/blueprint-v2/` 子目錄放詳細 sub-phase plan（仿 `docs/phase-3-runtime-v2/` 模式）

---

## Appendix A: Guild → Skill 對照速查表

> 詳細內容在 Phase B 展開時填入 `configs/guilds/{id}/README.md`

```
architect  → decomposition, graph-rag-stub, rfc-impact, bdd-spec-gen
sa-sd      → db-normalization, openapi-gen, sequence-diagram, payload-schema
ux         → user-journey, wcag-audit, state-machine, a11y
pm         → dag-topology, jira-dispatch, cognitive-load-scan, batched-task
gateway    → t-shirt-size, route-pipeline, intent-translate
bsp        → device-tree, kernel-compile, memory-map, irq-config
hal        → datasheet-rag, cross-compile, memory-align, i2c-spi
algo-cv    → matrix-ops, parallax-triangulation, cv-transform
optical    → lens-shading, led-pwm, optical-simulate
isp        → ae-loop, y-plane-brightness, 3a-tune
audio      → i2s-write, buffer-underrun, acoustic-filter
frontend   → vite-build, mock-api, wcag, hmi-web (sub-skill)
backend    → grpc-protobuf, db-migration, concurrency-sim, (python/go/rust/node/java sub-skill)
sre        → terraform-plan, k8s-deploy, iac-security, (cicd/manufacturing sub-skill)
qa         → mock-server, pytest-sandbox, glue-code, tdd-dual-patchset
auditor    → compliance-matrix-auxiliary (medical/automotive/industrial/military sub-skill),
             misra-c, iec62304-trace, mcdc-100
redteam    → fuzz-api, kernel-fault-inject, boundary-break
forensics  → crash-dump-analyze, log-correlate
intel      → latest-cve, zero-day-feed, deprecation-check
reporter   → markdown-gen, ui-mockup, industrial-design, marketing-copy
custom     → operator-defined
```

---

## Appendix B: 4 Templates Pydantic 雛型

> 詳細 schema 在 Phase A 落地到 `backend/templates/*.py`

```python
# SpecTemplate (由 architect Guild 產出)
class SpecTemplate(BaseModel):
    schema_version: Literal["1.0.0"]
    system_boundaries: list[str]          # 至少 3 項
    hardware_constraints: list[str]       # 至少 3 項
    api_idl_schema: str                   # OpenAPI 3.0 / Protobuf / C++ Header
    bdd_executable_specs: str             # Gherkin 格式
    edge_cases_handled: list[str] = Field(..., min_length=3)

# TaskTemplate (由 pm Guild 產出)
class TaskTemplate(BaseModel):
    target_triple: str                    # eg "x86_64-pc-linux-gnu" / "aarch64-vendor-linux"
    allowed_dependencies: list[str]       # Coder 唯一可讀的合約檔
    max_cognitive_load_tokens: int        # 超過退回 pm 重新拆解
    guild_id: str
    size: Literal["S", "M", "XL"]

# ImplTemplate (由 Coder Guild 產出)
class ImplTemplate(BaseModel):
    source_code_payload: str
    compiled_exit_code: int               # 必須為 0
    time_complexity: str                  # Big-O 宣告
    target_triple: str                    # 與 TaskTemplate 一致

# ReviewTemplate (由 auditor Guild 產出)
class ReviewTemplate(BaseModel):
    audit_type: Literal["advisory"] = "advisory"    # 強制 auxiliary
    requires_human_signoff: Literal[True] = True    # 強制需人類簽字
    is_auxiliary_compliant: bool
    cyclomatic_complexity_score: int
    critical_vulnerabilities: list[dict]            # 非空則阻擋 merge
    compliance_matrix: Literal["medical","automotive","industrial","military","generic"]
```

---

**ADR 狀態**：Proposed — 待 operator 最終核准後進入 Accepted。

**下次修訂觸發**：(1) Phase A 完成後補實作細節；(2) Phase B 完成後更新 Appendix A；(3) 法務 review 結果影響 Phase D 設計時。
