---
audience: operator + architect
status: accepted
date: 2026-05-01
priority: AB — Anthropic API + Batch Mode（加速 OmniSight 自身開發）
related:
  - TODO.md (Priority AB / Priority Z 既有 LLM observability)
  - backend/llm_adapter.py (既有 Anthropic provider integration)
  - backend/security/token_vault.py (AS Token Vault — 存 API key)
  - docs/integrations/llm-observability.md
---

# ADR — Anthropic API 模式 + Batch 模式遷移與整合（加速 OmniSight 自身開發）

> **One-liner**：把 OmniSight 自身的 dev workflow 從 Claude 訂閱版（Pro / Max plan via Claude Code CLI）切換到 **Anthropic API key + Batch API**，用批次執行 + 50% 折扣 + multi-agent dispatch 跑 TODO 規劃中的大量任務。涵蓋：(1) 工具 schema 完整盤點 / (2) Messages API 即時 tool calling / (3) Batch API 批次 tool calling / (4) 外部工具 / MCP / subprocess 對接清單 / (5) 成本與 rate-limit 估算 / (6) 訂閱版 → API 切換 SOP。

---

## 1. 背景與決策

### 1.1 為什麼從訂閱版切到 API + Batch

**訂閱版（Claude Pro / Max plan）限制**：
- **個人用**（Anthropic ToS individual-use 條款），不適合 fleet workload
- **rate limit**：Pro ~50 messages / 5h、Max 5x ~250 messages / 5h、Max 20x ~1000 messages / 5h
- **無 batch**、無 tool calling 控制、無 cost observability
- **跑 OmniSight TODO 大量 agent 任務會撞牆**（HD parser 100+ schematic / WP block model migration / L4 invariant audit / L5 R&D batch）

**API + Batch 模式優勢**：
- **無個人用限制**、適合 fleet workload
- **Tier 4 rate limit**：~5K RPM Sonnet / ~4K RPM Opus
- **Batch API**：100K messages / batch、24h window、**50% 折扣**
- **完整 tool calling 控制**：自定義 tools、tool_use / tool_result 雙向
- **Prompt caching**：cached input 90% 折扣（$0.30/MTok）、適合長 context 重複叫用
- **Cost observability**：per-token billing、進 N10 ledger 可 audit

**戰略決定**：
- **OmniSight 自身開發 workflow** 切到 API + Batch（本 ADR 範圍）
- **OmniSight 對外提供給客戶的 LLM 整合** 維持 multi-provider（Anthropic / OpenAI / Gemini / xAI / Groq / DeepSeek / Together / OpenRouter / Ollama）— 這部分既有 `backend/llm_adapter.py` 已 ship、不變

### 1.2 既有系統現況

OmniSight backend 已支援 Anthropic API：
- `backend/llm_adapter.py:124-134` — `ChatAnthropic` 走 LangChain
- `backend/security/token_vault.py` — AS Token Vault 加密存 API key
- 既有 8 provider tool calling 走 `bind_tools()` + `tool_call()` adapter（已稽核、Z.6 審計後 7/8 provider 全綠、Ollama 待補）

**缺**：
- **Batch API 整合**（既有只走 Messages API real-time）
- **Tool schema 完整盤點文件**（散落 code、無 canonical reference）
- **Batch task queue + dispatcher**（要排程 100+ 任務）
- **Cost / rate-limit 估算工具**（避免不知不覺燒爆）
- **MCP / subprocess 工具對接清單**（KiCAD-MCP / vision-parse / 等）

---

## 2. 第 1 段 — 工具 Schema 完整盤點

### 2.1 Claude Code 內建工具（OmniSight 自身用 = operator 視角）

OmniSight operator 用 Claude Code CLI / API 開發時、Claude 會用以下工具集。每個工具的 schema 直接對應 Anthropic API `tools=[]` 參數。

| 工具 | 功能 | 主要參數 | OmniSight 對應後端 |
|------|------|---------|------------------|
| **Bash** | 執行 shell 命令 | `command: str`, `description?: str`, `timeout?: int`, `run_in_background?: bool` | 直接 sandbox subprocess（與 BP agent harness 整合）|
| **Read** | 讀檔案（含 image / PDF / notebook） | `file_path: str (abs)`, `offset?: int`, `limit?: int`, `pages?: str` | 直接讀 OmniSight workspace fs |
| **Edit** | 字串精確替換改檔 | `file_path: str`, `old_string: str`, `new_string: str`, `replace_all?: bool` | 走 WP.3 diff-validation cascade（4-tier ladder）|
| **Write** | 寫新檔 / 全覆寫 | `file_path: str`, `content: str` | 走 KS.1 envelope 加密（若 customer secret） |
| **Glob** | 檔名 pattern 搜 | `pattern: str`, `path?: str` | 直接 fs |
| **Grep** | 內容 ripgrep | `pattern: str`, `path?: str`, `glob?`, `type?`, `-i?`, `-n?`, `output_mode?` | 走 ripgrep |
| **Agent** | 派 sub-agent | `description: str`, `prompt: str`, `subagent_type?: str`, `model?`, `run_in_background?: bool`, `isolation?: "worktree"` | 走 BP.B Guild dispatch + WP.10 fleet UI lanes |
| **WebFetch** | 抓 URL | `url: str`, `prompt: str` | 走 R20 RAG corpus + cache |
| **WebSearch** | 網搜 | `query: str`, `allowed_domains?`, `blocked_domains?` | （deferred、按需 ToolSearch 載入）|
| **ToolSearch** | 載入 deferred tool schema | `query: str ("select:name1,name2" / 關鍵字)`, `max_results?: int` | OmniSight 自家 tool registry 讀 |
| **TaskCreate / TaskGet / TaskList / TaskOutput / TaskStop / TaskUpdate** | 後台 task 管理 | 各自 schema | 走 BP.B Guild 整合 |
| **Skill** | 叫用 skill | `skill: str`, `args?: str` | 走 WP.2 skills loader |
| **ScheduleWakeup** | 排程下次喚醒 | `delaySeconds: int`, `prompt: str`, `reason: str` | 走 OmniSight cron / scheduler |
| **CronCreate / CronDelete / CronList** | cron 管理 | 各自 schema | 走 OmniSight scheduler |
| **EnterPlanMode / ExitPlanMode** | plan 模式進出 | (mode 相關) | 走 BP plan workflow |
| **EnterWorktree / ExitWorktree** | git 隔離工作樹 | (worktree 相關) | 走 BP isolation |
| **NotebookEdit** | jupyter 改 cell | `notebook_path`, `cell_id`, `new_source`, `cell_type?`, `edit_mode?` | 走 W14 Live Sandbox |
| **Monitor** | 串流 background process | (process 監看) | 走 BP execution monitor |
| **PushNotification** | 推播 | (通知) | 走 OmniSight notification stack |
| **RemoteTrigger** | 遠端觸發 | (遠端) | 走 OmniSight webhook |
| **AskUserQuestion** | 問用戶 | (UI prompt) | 走 OmniSight chat UI |
| **ListMcpResourcesTool / ReadMcpResourceTool** | MCP resource 管理 | (MCP 介面) | 走 MCP 整合層 |
| **ExitWorktree** | 結束 worktree | (worktree) | 同上 |

**完整 schema** 寫進 `backend/agents/tool_schemas.py`（AB.1.x 落地）、走 type-check 驗證、CI 鎖。

### 2.2 OmniSight 自家 SKILL_* 工具

BP.B Guild 內登錄的 skill（既有 + WP.2 升級後新格式）：

| Skill ID | 功能 | 來源 priority |
|----------|------|--------------|
| `SKILL_HD_PARSE` | 解析 EDA file → HDIR | HD.1 |
| `SKILL_HD_DIFF_REFERENCE` | reference vs customer design diff | HD.4 |
| `SKILL_HD_SENSOR_SWAP_FEASIBILITY` | sensor 替換可行性 | HD.5 |
| `SKILL_HD_FW_SYNC_PATCH` | HW change → FW patch 清單 | HD.7 |
| `SKILL_HD_PCB_SI_ANALYZE` | PCB SI 分析 | HD.2 |
| `SKILL_HD_HIL_RUN` | HIL session 執行 | HD.8 |
| `SKILL_HD_RAG_QUERY` | datasheet RAG 檢索 | HD.9 |
| `SKILL_HD_CERT_RETEST_PLAN` | EMC / 安規 retest plan | HD.10 |
| `SKILL_HD_PLATFORM_RESOLVE` | SoC mark → platform spec | HD.16 |
| `SKILL_HD_VENDOR_SYNC` | upstream sync pipeline | HD.16 |
| `SKILL_HD_CUSTOMER_OVERLAY` | per-customer overlay 解析 | HD.17 |
| `SKILL_HD_LIFECYCLE_AUDIT` | 年度 reproducibility audit | HD.18 |
| `SKILL_HD_CVE_IMPACT` | CVE feed → SBOM 影響分析 | HD.18 |
| `SKILL_HD_CVE_AUTO_BACKPORT` | CVE auto-PR backport | HD.18 |
| `SKILL_HD_BRINGUP_CHECKLIST` | bring-up checklist 產出 | HD.19 |
| `SKILL_HD_BRINGUP_LIVE_PARSE` | live boot console parse | HD.19 |
| `SKILL_HD_PORT_ADVISOR` | 跨 SoC port 工時 | HD.19 |
| `SKILL_HD_DEVKIT_FORK` | DevKit fork 起點 | HD.19 |
| `SKILL_HD_ISP_TUNING_DIFF` | ISP tuning before/after | HD.20 |
| `SKILL_HD_BLOB_COMPAT` | blob 兼容矩陣 | HD.20 |
| `SKILL_HD_PRODUCTION_BUNDLE` | EMS access bundle | HD.21 |
| `SKILL_HD_OTA_PACKAGE_GEN` | OTA bundle gen | HD.21 |
| `SKILL_HD_SBOM_GENERATE` | SBOM CycloneDX + SPDX | HD.21 |
| `SKILL_HD_LICENSE_AUDIT` | ship-time license check | HD.21 |
| `SKILL_HD_AUTHENTICITY_VERIFY` | chip authenticity | HD.21 |
| `SKILL_HD_AI_COMPANION` | 統一 chat surface | HD.21 |

每 skill 進 Anthropic API tools 陣列、schema 自動從 markdown frontmatter 推導（WP.2 loader）。

### 2.3 工具 schema 文件化策略

- **canonical doc**：`backend/agents/tool_schemas.py`（type-stamped Pydantic / TypedDict）
- **markdown reference**：`docs/agents/tool-reference.md`（每工具 2-3 段、user-facing）
- **CI 鎖**：schema 改動必更新 reference doc（pre-commit hook）
- **Anthropic API 對接**：runtime 自動 serialize Pydantic → tools=[]

---

## 3. 第 2 段 — Anthropic Messages API 即時 Tool Calling

### 3.1 API 形狀（Real-time mode）

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-ant-...")

response = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=8192,
    tools=[
        {
            "name": "Read",
            "description": "Read a file from filesystem",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute path"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["file_path"],
            },
        },
        # ... rest of tools
    ],
    messages=[
        {"role": "user", "content": "Read TODO.md and summarize Priority HD"},
    ],
)
```

### 3.2 Tool use loop

```python
while response.stop_reason == "tool_use":
    tool_calls = [b for b in response.content if b.type == "tool_use"]
    tool_results = []
    for call in tool_calls:
        result = execute_tool(call.name, call.input)  # OmniSight 後端執行
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": result,
            "is_error": False,
        })
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=8192,
        tools=[...],
        messages=[
            *prior_messages,
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results},
        ],
    )
```

### 3.3 與既有 `backend/llm_adapter.py` 的關係

**不重做**、走 LangChain `ChatAnthropic` 既有路徑（已 ship、與 8 provider 統一介面）。**新增**：
- `backend/agents/anthropic_native_client.py`：純 Anthropic SDK 直連（給 batch + 高效能 path 用、繞 LangChain 抽象）
- `backend/agents/tool_schemas.py`：tool schema 中央 registry
- `backend/agents/tool_dispatcher.py`：工具執行 router（接 Bash / Read / Edit / Skill / MCP）

---

## 4. 第 3 段 — Anthropic Batch API 批次 Tool Calling

### 4.1 Batch API 形狀

```python
batch = client.messages.batches.create(
    requests=[
        {
            "custom_id": "task_001",
            "params": {
                "model": "claude-opus-4-7",
                "max_tokens": 8192,
                "tools": [...],
                "messages": [{"role": "user", "content": "..."}],
            },
        },
        # up to 100,000 requests, 256 MB total
    ]
)

# Poll until done
while batch.processing_status != "ended":
    time.sleep(60)
    batch = client.messages.batches.retrieve(batch.id)

# Stream results
for result in client.messages.batches.results(batch.id):
    if result.result.type == "succeeded":
        process(result.custom_id, result.result.message)
```

### 4.2 Batch 限制與適用場景

| 限制 | 值 |
|------|------|
| 單 batch 最大 request | 100,000 |
| 單 batch 最大 size | 256 MB |
| 處理時間窗 | 24 小時（通常 < 1 小時完成） |
| 折扣 | input + output **皆 50%** |
| Tool use | **完整支援**、每 request 內走完整 multi-turn loop |
| Streaming | **不支援**（async result） |
| Real-time | **不適合**（最少分鐘級延遲）|

**適合 batch 的 OmniSight 任務**：
- ✅ HD parser 跑 100+ schematic（HD.1 / HD.4）
- ✅ HD sensor KB 抽 100+ datasheet（HD.5.13 + vision-parse）
- ✅ HD CVE impact 掃 1000+ device（HD.18.6）
- ✅ WP block model 大量 retrofit migration
- ✅ L4.1 deterministic regression test（每月跑 N 千 task）
- ✅ L4.3 adversarial CI（每 PR 跑 100+ jailbreak set）
- ✅ L4.5 field telemetry batch analysis
- ✅ L5.D multi-agent simulation 大量對抗訓練
- ✅ TODO 內 `[ ]` 大批 routine 任務（用 LLM 自動 propose patch）

**不適合 batch**：
- ❌ Operator 即時對話（chat UI）
- ❌ HD bring-up live console parse（real-time）
- ❌ W14 Live Sandbox preview
- ❌ Customer-facing on-demand request

### 4.3 Batch dispatcher 架構

```
[Operator / scheduled trigger]
   │
   ▼
Batch Task Queue (Redis / Postgres queue)
   │
   ├── Group by model (Opus / Sonnet / Haiku)
   ├── Group by tool requirement
   └── Chunk to 100K/batch
   │
   ▼
[Batch Dispatcher worker]
   │
   ├── Anthropic batches.create
   ├── Store batch_id + custom_id mapping
   └── Schedule poll every 60s
   │
   ▼
[Result Processor]
   │
   ├── batches.results stream
   ├── Per custom_id → original task callback
   └── Write back to OmniSight DB + N10 audit
```

**alembic 0181**：`batch_tasks` / `batch_runs` / `batch_results` 表

---

## 5. 第 4 段 — 外部工具 / Skill / MCP Server 對接清單

OmniSight 跑 batch agent 時、Claude 會經 tool_use 呼叫以下外部資源（透過我方 dispatcher subprocess / MCP / REST）：

### 5.1 已規劃 / 待整合

| 類別 | 工具 / 服務 | 整合方式 | 對應 priority | License 邊界 |
|------|------------|---------|-------------|-------------|
| **EDA parser** | KiCAD-MCP-Server (mixelpixx) | Docker sandbox + MCP STDIO | HD.1.2a | MIT direct |
| | KiCad-MCP (lamaalrajih) | Docker fallback + MCP | HD.1.2e | MIT direct |
| | kicad-skip | Python LGPL dynamic link | HD.1.2b | LGPL OK |
| | Altium-Schematic-Parser (a3ng7n) | Python lib MIT direct | HD.1.3a | MIT direct |
| | altium2kicad (thesourcerer8) | **Perl subprocess** | HD.1.3b | **GPL boundary**（R57） |
| | OpenOrCadParser (Werni2A) | C++ + pybind11 | HD.1.4a | MIT direct |
| | gerbonara | Python Apache direct | HD.1.11a | Apache direct |
| | pygerber | Python MIT direct | HD.1.11b | MIT direct |
| | OdbDesign (nam20485) | **Docker sidecar REST** | HD.1.12a | **AGPL boundary**（R57） |
| | ODBPy (ulikoehler) | Python Apache fallback | HD.1.12c | Apache direct |
| **Datasheet vision** | vision-parse (iamarunbrahma) | Python MIT direct | HD.5.13a | MIT direct |
| **Circuit DSL** | SKiDL (devbisme) | Python MIT direct | HD.6.9 | MIT direct |
| **Embedded** | pyFDT (molejar) | Python Apache direct | HD.7.1a | Apache direct |
| | python-devicetree (Zephyr) | Python Apache direct | HD.7.1a | Apache direct |
| | ldparser (pftbest) | Python MIT direct | HD.7.4b | MIT direct |
| **Other MCP** | claude_ai_Figma__* | MCP HTTP | （UI design）| 既有 MCP |
| | claude_ai_Gmail__* / Google_Calendar__* / Google_Drive__* | MCP HTTP | （integration）| 既有 MCP |

### 5.2 工具註冊到 Anthropic tools=[] 的策略

1. **Local-only tools**（Bash / Read / Edit / Write / Glob / Grep / Skill）→ 永遠帶
2. **Subprocess wrapper tools**（KiCad-MCP / altium2kicad / OdbDesign）→ 按 task type 動態選 + sandbox flag
3. **MCP server tools**（Figma / Gmail / 等）→ 按需透過 ToolSearch lazy load
4. **OmniSight SKILL_HD_***（26 個）→ 按 task domain 動態選

**OmniSight tool dispatcher**（AB.5 落地）：每 task 啟動時根據 `task.kind` 從 registry 拿適用工具集、不全帶（避免 prompt 爆炸）。

---

## 6. 第 5 段 — 成本 / Token / Rate-Limit 估算

### 6.1 Anthropic 定價（2026-04 當前、API tier 4）

| Model | Input ($/MTok) | Output ($/MTok) | Cached input ($/MTok) | Batch input | Batch output |
|-------|---------------|----------------|---------------------|-------------|--------------|
| **Claude Opus 4.7** | $15 | $75 | $1.50 (90% off) | $7.50 | $37.50 |
| **Claude Sonnet 4.6** | $3 | $15 | $0.30 (90% off) | $1.50 | $7.50 |
| **Claude Haiku 4.5** | $1 | $5 | $0.10 | $0.50 | $2.50 |

**Cache write 加價**：寫 cache 比正常 input 貴 25%（$3.75/MTok Sonnet）— 但只寫一次、後續 90% off 讀取。

### 6.2 Rate Limit（Tier 4、無 commercial 申請的最高 tier）

| Model | RPM | Input TPM | Output TPM |
|-------|-----|-----------|-----------|
| Opus 4.7 | 4,000 | 8M | 1M |
| Sonnet 4.6 | 5,000 | 16M | 10M |
| Haiku 4.5 | 5,000 | 32M | 20M |

**Batch API rate limit 獨立計**、不吃 real-time TPM/RPM。

### 6.3 OmniSight 任務 cost 估算

每 task type 的典型 token 量 + 成本（單次 + 1000× 批次）：

| Task type | 模型 | Input tokens | Output tokens | 單次成本 | 批次 1000× cost (50% off) |
|-----------|------|--------------|---------------|---------|------------------------|
| **HD.1 schematic parse**（單檔）| Sonnet 4.6 | ~5K（含 tool 結果）| ~2K | ~$0.045 | **~$22** |
| **HD.4 reference diff**（兩 design 比對）| Opus 4.7 | ~15K | ~5K | ~$0.6 | **~$300** |
| **HD.5.13 datasheet vision**（PDF 抽 spec）| Sonnet 4.6 | ~10K（PDF + prompt）| ~3K | ~$0.075 | **~$37.5** |
| **HD.18.6 CVE impact** | Sonnet 4.6 | ~3K | ~1K | ~$0.024 | **~$12** |
| **L4.1 determinism regression** | Sonnet 4.6 | ~5K | ~2K | ~$0.045 | **~$22.5** |
| **L4.3 adversarial CI** | Sonnet 4.6 | ~4K（含 jailbreak prompt）| ~1K | ~$0.027 | **~$13.5** |
| **TODO `[ ]` routine 任務 batch** | Sonnet 4.6 | ~8K | ~3K | ~$0.069 | **~$34.5** |

**重度 dev workflow 月度估算**：
- 假設 OmniSight 自身開發每月 ~10K agent task（含 ad-hoc + scheduled batch + CI）
- 平均 Sonnet 4.6、~5K input + ~2K output、~$0.045/task real-time
- 50% 走 batch（5K task × $0.0225 = $112.5）+ 50% real-time（5K × $0.045 = $225）
- **月 LLM cost 估 ~$340**（含 cache 命中折扣後 ~$200-250）

**Cost vs 訂閱版對比**：
- Max plan 5x ≈ $200/月 / 一個人 / ~250 messages per 5h；fleet workload 撞牆
- API + Batch ≈ $200-300/月、無人數限制、可平行 100+ task

→ **同價位、能力跨數量級提升**。

### 6.4 Cost guard

OmniSight 內建：
- **Per-batch budget**：dispatcher submit 前估算 cost、超 cap fail
- **Daily / monthly cap**：超 cap 自動 throttle（已存 Z.6 spend anomaly detector）
- **Per-task type cap**：HD parser batch / L5 R&D batch 各有獨立 budget
- **Alert**：80% / 100% / 120% 三階提醒
- 整合 Z + N10 audit

---

## 7. 第 5 段 — 訂閱版 → API 切換 SOP

### 7.1 切換前準備

- [ ] **取得 Anthropic API key**：console.anthropic.com → API Keys → Create Key
- [ ] **設定 Anthropic Workspace**（多 key 隔離 + budget）：建議 dev / batch / production 三 workspace
- [ ] **設定 spend limit + alert email**（防 R63 燒爆）
- [ ] **API tier upgrade**：申請 Tier 4（需歷史 spend、Anthropic 自動評估）

### 7.2 OmniSight 內配置

```
Settings → Provider Keys → Anthropic
  → Mode: API mode（取消「Use Claude Code subscription」勾選）
  → API Key: sk-ant-... （走 AS Token Vault 加密存）
  → Workspace: dev / batch / production 任選（或新建）
  → Default model: claude-opus-4-7（real-time）/ claude-sonnet-4-6（batch）
  → Batch enabled: ✅
  → Cost guard:
       Per-batch budget cap: $50
       Daily cap: $30
       Monthly cap: $500
```

### 7.3 既有功能影響

| 功能 | 訂閱版行為 | API 模式行為 |
|------|----------|-------------|
| Claude Code CLI 互動 | 走訂閱 quota | 走 API（per-token billing） |
| OmniSight backend agent dispatch（BP） | （訂閱不支援、本來就走 API key） | 不變 |
| TokenUsageStats 顯示 | 顯示「Claude Code session」 | 顯示 per-token cost |
| Z provider observability | 部分（訂閱 cost 看不到）| 完整（balance + rate-limit + per-token） |
| Batch 大量任務 | 不可（訂閱無 batch） | **新功能** |

### 7.4 Rollback 路徑

- API key 仍可保留、Settings 可隨時切回訂閱版
- 已 dispatch 的 batch 任務不受影響（continues to completion）
- Cost guard knob（`OMNISIGHT_AB_BATCH_ENABLED=false`）整套 disable

### 7.5 切換 SOP 步驟

1. **Day 0**：申請 API key、Workspace、設 budget；測試一次 real-time API call（curl 一個 hello world）
2. **Day 1**：OmniSight Settings 加 API key（保留訂閱版設定為 fallback）；切 default mode 為 API；跑一個小 task 驗證
3. **Day 2-3**：dogfood 一週、觀察 cost / latency / error rate；對比訂閱版體感
4. **Day 7**：accept、disable 訂閱版 fallback；正式啟用 batch dispatcher
5. **Week 2**：第一個 100-task batch 跑（HD parser 測試集）、驗 50% 折扣、驗 result 完整
6. **Week 3+**：把 TODO routine `[ ]` 任務批次化跑、加速 OmniSight 自身開發

---

## 8. OmniSight 整合計畫（Priority AB 對應）

| AB phase | 內容 | 預估 |
|----------|------|------|
| **AB.1** Tool schema canonical doc | 中央 registry + Pydantic validation + CI 鎖 | 3 day |
| **AB.2** Anthropic Messages API native client | `anthropic_native_client.py`（繞 LangChain、給 batch + 高效能 path） | 2 day |
| **AB.3** Anthropic Batch API integration | `batches.create / retrieve / results` 完整 wrapper | 3 day |
| **AB.4** Batch task queue + dispatcher | Redis / PG queue + worker + poll | 3 day |
| **AB.5** External MCP / subprocess tool registry | 接 KiCad-MCP / altium2kicad subprocess / OdbDesign sidecar / vision-parse / 等、按 task type dispatch | 4 day |
| **AB.6** Cost estimator + budget guard | per-task / per-batch / daily / monthly cap + 80%/100%/120% alert | 2 day |
| **AB.7** Rate limit + retry + backoff | 429 / 529 處理 + exponential backoff + dead-letter queue | 2 day |
| **AB.8** Subscription → API migration UI + runbook | Settings UI + operator runbook + rollback | 1 day |
| **AB.9** Batch eligible task identifier | per task type opt-in flag + 預設 routing 表 | 2 day |
| **AB.10** Test strategy + smoke + CI | mock + integration + cost regression | 2 day |

**Total**: ~24 day（~5 週、可平行壓 ~3 週）

### 8.1 與既有 priority 的整合

| 整合點 | 行為 |
|--------|------|
| **Z provider observability** | AB.6 cost estimator 接 Z spend anomaly detector |
| **AS Token Vault** | API key 走 AS encrypt（既有）|
| **N10 audit ledger** | 每 batch 進 audit、tamper-evident |
| **WP.1 Block model** | 每 batch task = Block、UI 統一渲染 |
| **WP.7 Feature flag** | `OMNISIGHT_AB_BATCH_ENABLED` 等 knob 進 registry |
| **BP.B Guild** | batch dispatcher 是 Guild 的另一種 worker mode（real-time + batch 雙模）|
| **HD.X 系列** | HD.1 / HD.4 / HD.5 / HD.18 等批次任務優先 batch 化 |
| **L4.3 Adversarial CI** | adversarial test set 走 batch 跑、節省 50% cost |

---

## 9. R-Series 風險（R76-R80）

- **R76 API key 洩漏 → 燒爆帳單**：API key 在 OmniSight backend memory / log 風險。**Mitigation**：走 AS Token Vault + KS.1 envelope encryption（既有）；log scrubber 防誤 log；Anthropic Workspace 設 spend limit（即便 leak 也 cap 住）。
- **R77 Batch 結果 24h 才回 → 排程錯亂**：dispatcher 預期分鐘級、batch 可能小時級。**Mitigation**：dispatcher 明確分 real-time vs batch lane；batch 任務帶 `expected_completion_within: 24h` SLA；UI 顯示 batch 進度條。
- **R78 Rate limit 撞牆 → 任務丟失**：429 / 529 處理不周。**Mitigation**：AB.7 exponential backoff + retry（max 5）+ dead-letter queue + alert；Tier 4 申請避免 default tier 限制。
- **R79 Tool schema drift**：Claude Code 升版、tool schema 改、OmniSight 自家 tool registry 跟不上。**Mitigation**：每月 schema diff vs Claude Code release notes；CI lock；deferred tool 走 ToolSearch 動態載入避免 hard-code。
- **R80 Batch 任務跨 tenant 隔離**：multi-tenant 後、batch 共享 dispatcher 但結果不該滲漏。**Mitigation**：每 task 帶 `tenant_id`、dispatcher 以 `(tenant_id, model, tools_signature)` 分 batch、callback 以 `(tenant_id, task_id)` 路由；KS.1 envelope 邊界繼承；tenant 退出時所有 in-flight batch task cancel + 加密 deletion。

Mitigation evidence is consolidated in
[`docs/ops/ab_r76_r80_mitigation_evidence.md`](../ops/ab_r76_r80_mitigation_evidence.md).

---

## 10. Migration / Schema

| Migration | 內容 |
|-----------|------|
| 0181 | `batch_tasks` / `batch_runs` / `batch_results` 表 |
| 0182 | `tool_schema_registry` / `tool_schema_versions` |
| 0183 | `cost_estimates` / `cost_alerts` 表 |
| 0184 | `external_tool_registry`（MCP / subprocess sidecar 紀錄） |
| 0185-0190 | 預留 |

合計 **AB 0181-0190（10 slots 預留）**。

### 10.1 Single Knob

- `OMNISIGHT_AB_BATCH_ENABLED=false` → 整套 batch dispatcher disable、退回 real-time only
- `OMNISIGHT_AB_API_MODE_ENABLED=true` → 切 API 模式；30 天觀察期結束後，`finalize_disable_subscription()` 會要求這個值已鎖成 true 才能 disable 訂閱版 fallback（false / unset 保留 rollback path）
- `OMNISIGHT_AB_COST_GUARD_ENABLED=true` → cost guard 強制（false debug only）

---

## 11. Open Questions

1. **訂閱版 fallback 保留多久**：切 API 後留訂閱版 30 天觀察期；觀察期過後只有在 `OMNISIGHT_AB_API_MODE_ENABLED=true` 已部署鎖定時，才允許完全 disable fallback。
2. **Anthropic Workspace 切分粒度**：dev / batch / production 三 workspace 是否夠？是否要 per-priority workspace（HD / WP / L4 各自）？傾向**先三 workspace、後續按 cost report 拆分**。
3. **Batch 任務的 prompt cache 策略**：batch 內每 task 有 70-80% 重複 system prompt + tool schema、是否走 cache？傾向**強制走 cache（90% 折扣）**、設計 task template 確保 cacheable。
4. **與 BP.B Guild 整合的優先級**：batch dispatcher 應該是 Guild 的 worker mode 還是獨立模組？傾向**獨立模組 + Guild 走 dispatcher API**、避免 Guild 升級時 batch 受影響。
5. **L4.9 Cost-per-Decision Meta-LLM 與 AB 整合時機**：AB 落地後、L4.9 自然有實作 substrate（已有 batch + cost estimator）；可同期或 AB 完工後接著做。

---

## 12. 參考文件

- `TODO.md` Priority AB / Priority Z 既有 / Priority CL（cost / billing 整合）
- `backend/llm_adapter.py`（既有 Anthropic provider integration）
- `backend/security/token_vault.py`（AS Token Vault — API key 加密存）
- `docs/integrations/llm-observability.md`（Z 既有 LLM observability doc）
- `docs/security/ks-multi-tenant-secret-management.md`（KS.1 envelope 邊界）
- Anthropic Messages API：https://docs.anthropic.com/en/api/messages
- Anthropic Batch API：https://docs.anthropic.com/en/api/messages-batches
- Anthropic Tool Use：https://docs.anthropic.com/en/docs/build-with-claude/tool-use

---

## 13. Sign-off

- **Owner**: Agent-software-beta + nanakusa sora
- **Date**: 2026-05-01
- **Status**: Accepted（Priority AB 排程依 OmniSight 實際 dev velocity 啟動、可立即開工不阻塞主路線）
- **Next review**: AB.1-AB.4 完工後、第一個 100-task batch 跑完後 review 實際 cost vs estimate
