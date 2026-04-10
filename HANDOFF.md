# HANDOFF.md — OmniSight Productizer 開發交接文件

> 撰寫時間：2026-04-10
> 最後 commit：`740ea54` (master)
> 工作目錄狀態：clean（所有變更已 commit）

---

## 0. 專案理解與未來開發藍圖

### 專案本質

OmniSight Productizer 是一套專為「嵌入式 AI 攝影機（UVC/RTSP）」設計的全自動化開發指揮中心。
系統以 `hardware_manifest.yaml` 和 `client_spec.json` 為唯一真實來源（SSOT），
透過多代理人（Multi-Agent）架構，實現從硬體規格解析、Linux 驅動編譯、演算法植入到上位機 UI 生成的全端自動化閉環。

### 目前系統能力

- **前端**：科幻風 FUI 儀表板，14 個組件，全部接真實後端資料，零假資料
- **後端**：FastAPI + LangGraph 多代理人管線，46 routes，17 sandboxed tools
- **LLM**：8 個 AI provider 可熱切換（預設 Anthropic），無 key 時降級規則引擎
- **隔離工作區**：git worktree（Layer 1）+ Docker 容器（Layer 2，含 aarch64 交叉編譯）
- **即時通訊**：EventBus → SSE 持久連線，所有事件推播到前端 + REPORTER VORTEX log（標籤上色）
- **INVOKE 全局指揮**：上下文感知，自動分析系統狀態 → 分派 task → provision workspace → 回報

### 未來開發藍圖

以下為尚未實作但設計上已預留接口的方向：

1. **Self-Healing Loop（自動修復閉環）**
   - Agent 執行編譯 → 失敗 → 自動分析 error log → 修改程式碼 → 重新編譯
   - 需要：LangGraph 的 loop edge（specialist → tool → error check → specialist）
   - 基礎已有：tool_executor 回傳 ToolResult.success=False 時可觸發

2. **PR 自動審核流程**
   - Agent 完成工作 → finalize workspace → 自動建立 PR
   - 需要：`git_push` tool 已就位，缺 GitHub API 整合（gh CLI 或 PyGithub）
   - 人工審核後 merge 到 main

3. **真實攝影機串流整合**
   - VitalsArtifactsPanel 已有 StreamSource 介面，目前空陣列
   - 需要：UVC/RTSP 串流後端（GStreamer 或 FFmpeg pipeline）
   - 可透過 WebRTC 或 MJPEG 推到前端 canvas

4. **Token Usage 真實追蹤**
   - `track_tokens()` 函數已定義在 `backend/routers/system.py`
   - 需要：在 `agents/llm.py` 的 LLM invoke 後呼叫 `track_tokens()`
   - LangChain callback handler 是最佳切入點

5. **Artifact 生成管線**
   - Reporter Agent 目前只回模板文字
   - 需要：markdown/PDF 生成工具（Jinja2 template → weasyprint/pandoc）
   - 產出物寫入 workspace → 前端 Artifact 面板顯示

6. **多專案管理**
   - 目前綁定單一 repo
   - 擴展方向：每個「專案」對應一組 SSOT 檔案 + 獨立的 agent pool

---

## 1. 本次對話完成的核心邏輯

### Phase 1-2: 前端基礎（繼承既有）
- Next.js 16.2 + React 19 + Tailwind 4.2 的科幻 FUI 儀表板已存在
- 本次新增 `useEngine` Hook + `lib/api.ts` 將所有組件接上後端 API
- 安裝 Vercel AI SDK + 8 個 provider 套件（@ai-sdk/anthropic 等）
- 建立 Next.js rewrite proxy（解決 WSL2 ↔ Windows 瀏覽器的 CORS 問題）

### Phase 3: 後端大腦
- **FastAPI 伺服器**：9 routers, 46 routes, CORS + Swagger
- **LangGraph 管線**：Orchestrator → 5 Specialist → Tool Executor → Summarizer
- **多 Provider LLM**：統一工廠 `get_llm()`，8 provider 可運行時切換
- **17 個 Tool**：檔案（6）+ Git（9）+ Bash（1）+ Search（1），全部 sandbox + workspace-aware
- **EventBus**：所有 emit 自動寫入 SSE + system log buffer

### Phase 4: 全端整合
- **SSE 即時推播**：`GET /events` 持久連線，前端自動更新 Agent/Task/Tool/Pipeline 狀態
- **INVOKE 全局指揮**：分析狀態 → 規劃行動 → 執行（assign/retry/report/health/command）→ SSE 回報
- **E2E 測試**：hardware_manifest.yaml + mock_compile.sh 完整閉環驗證通過

### 隔離工作區
- **Layer 1（git worktree）**：INVOKE assign 自動 provision → 每 Agent 獨立 branch + 目錄
- **Layer 2（Docker）**：`omnisight-agent:latest` 映像（Ubuntu + aarch64-linux-gnu-gcc）
- **安全機制**：路徑沙箱、危險指令封鎖、push 限 agent/* 分支、容器 --network none

### 假資料清除
- 全部 18 處假資料替換為真實後端數據（/proc, lsusb, lsblk, git, hardware_manifest.yaml）
- REPORTER VORTEX 日誌標籤上色（8 種標籤 × 3 等級色彩）

### SSOT 結構
- `configs/hardware_manifest.yaml`：真實硬體規格（系統讀取此檔）
- `configs/client_spec.json`：客戶需求規格
- `test_fixtures/`：E2E 測試用假資料（不影響正式環境）

---

## 2. 修改的檔案清單（精確路徑）

### 新增檔案（33 個）

```
# Backend 核心
backend/main.py
backend/config.py
backend/models.py
backend/events.py
backend/workspace.py
backend/container.py
backend/requirements.txt
backend/docker/Dockerfile.agent

# LangGraph Agents
backend/agents/__init__.py
backend/agents/graph.py
backend/agents/nodes.py
backend/agents/llm.py
backend/agents/tools.py
backend/agents/state.py

# API Routers
backend/routers/__init__.py
backend/routers/health.py
backend/routers/agents.py
backend/routers/tasks.py
backend/routers/chat.py
backend/routers/invoke.py
backend/routers/tools.py
backend/routers/providers.py
backend/routers/events.py
backend/routers/workspaces.py
backend/routers/system.py

# Frontend
lib/api.ts
lib/providers.ts
hooks/use-engine.ts
app/api/chat/route.ts

# Config / Test
configs/hardware_manifest.yaml
configs/client_spec.json
test_fixtures/hardware_manifest.yaml
test_fixtures/mock_compile.sh
```

### 修改檔案（15 個）

```
.gitignore                                       # +venv/pycache/build/workspaces
next.config.mjs                                  # +rewrites proxy to backend
package.json                                     # +ai sdk + 8 provider packages
app/page.tsx                                     # useEngine hook + real data props
components/omnisight/agent-matrix-wall.tsx        # defaultAgents → []
components/omnisight/global-status-header.tsx     # defaults → 0/Detecting
components/omnisight/host-device-panel.tsx        # remove fake simulation
components/omnisight/invoke-core.tsx              # +onCommandChange callback
components/omnisight/orchestrator-ai.tsx          # +tokenUsage prop
components/omnisight/source-control-matrix.tsx    # remove fake repos + setTimeout
components/omnisight/spec-node.tsx                # remove sampleSpec
components/omnisight/task-backlog.tsx             # +tasks prop, remove sampleTasks
components/omnisight/token-usage-stats.tsx        # remove fake simulation
components/omnisight/vitals-artifacts-panel.tsx   # remove all fake data + tag coloring
README.md                                        # complete rewrite + badges
```

---

## 3. 編譯與測試狀態

### Frontend Build
```
Status: PASS
Route (app)
  ○ /              (Static)
  ○ /_not-found    (Static)
  ƒ /api/chat      (Dynamic)
```
- `npm run build` 通過，零錯誤
- `ignoreBuildErrors: true` 在 next.config.mjs（部分 TS 型別警告被忽略）

### Backend
```
Status: PASS
FastAPI: 46 routes loaded
All 24 endpoints: HTTP 200
```
- `backend/.venv/bin/python -m uvicorn backend.main:app` 正常啟動
- LangGraph pipeline 測試通過（routing + tool execution + summarize）
- Workspace provision/finalize/cleanup 測試通過
- Docker container start/exec/stop 測試通過
- E2E 閉環測試通過（parse manifest → compile → error → read → search → report）

### Docker
```
Image: omnisight-agent:latest (995MB)
Status: Built and tested
Cross-compile: aarch64-linux-gnu-gcc verified
```

### 已知問題
1. TypeScript 有若干非阻塞型別警告（SpecValue 遞迴型別、AIModel union type）
2. `package-lock.json` 和 `repomix-output.xml` 在 git untracked（可考慮 gitignore）
3. Token usage tracking 的 `track_tokens()` 已定義但尚未接入 LLM invoke callback

---

## 4. 下一個對話接手後，立刻要執行的前五個步驟

### Step 1: 啟動開發環境並驗證

```bash
# Terminal 1: Backend
cd /home/user/work/sora/OmniSight-Productizer
backend/.venv/bin/python -m uvicorn backend.main:app --reload --port 8000

# Terminal 2: Frontend
npm run dev

# Terminal 3: Verify
curl http://localhost:3000/api/v1/health
# Expected: {"status":"online","engine":"OmniSight Engine","version":"0.1.0","phase":"3.2"}
```

打開瀏覽器 `http://localhost:3000`，確認：
- GlobalStatusHeader 顯示真實 WSL OK / task 數量
- HostDevicePanel 顯示真實 CPU (AMD Ryzen 9 9950X3D) / RAM (96GB)
- REPORTER VORTEX 有彩色標籤日誌

### Step 2: 設定 LLM API Key（啟用智慧代理）

```bash
cp .env.example .env
# Edit .env, add at minimum:
echo 'OMNISIGHT_ANTHROPIC_API_KEY=sk-ant-your-key-here' >> .env
```

重啟 backend 後驗證：
```bash
curl http://localhost:8000/api/v1/providers/test
# Expected: {"status":"ok","provider":"anthropic","model":"claude-sonnet-4-20250514","response":"OMNISIGHT_OK"}
```

### Step 3: 填入真實 SSOT 並觸發首次 INVOKE

編輯 `configs/hardware_manifest.yaml`，填入真實的硬體規格（sensor 型號、I2C 地址、ISP pipeline 等）。

然後在前端按下 INVOKE ⚡ 按鈕，或：
```bash
curl -X POST http://localhost:8000/api/v1/invoke
```

觀察：
- 3 個 Task 自動分派到對應 Agent
- 每個 Agent 自動 provision isolated workspace（git worktree）
- REPORTER VORTEX 即時顯示所有 [AGENT] [WORKSPACE] [TASK] 日誌
- Agent 卡片狀態從 idle → running

### Step 4: 接入 Token Usage 追蹤（完成已知問題 #3）

`track_tokens()` 已定義但尚未接入 LLM invoke。在 `backend/agents/llm.py` 加入 LangChain callback：

```python
# 在 _create_llm() 回傳的 LLM 上掛 callback
from langchain_core.callbacks import BaseCallbackHandler
from backend.routers.system import track_tokens
import time

class TokenTracker(BaseCallbackHandler):
    def __init__(self, model: str):
        self.model = model
        self.start = 0
    def on_llm_start(self, *a, **kw):
        self.start = time.time()
    def on_llm_end(self, response, **kw):
        usage = response.llm_output.get("token_usage", {}) if response.llm_output else {}
        track_tokens(
            self.model,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            int((time.time() - self.start) * 1000),
        )
```

完成後，在前端 Orchestrator 面板的 Token Usage 區塊會即時顯示真實消耗數據。

### Step 5: 實作 Self-Healing Loop（自動修復閉環）

這是 README 規格書中最核心的能力。在 `backend/agents/graph.py` 加入 loop edge：

```
Specialist → Tool Executor → Error Check
                                |
                         有錯誤? → 回到 Specialist（帶 error context）
                         無錯誤? → Summarizer → END
```

具體步驟：
1. 在 `backend/agents/state.py` 加 `retry_count: int = 0` 和 `max_retries: int = 3`
2. 在 `backend/agents/graph.py` 加一個 `error_check_node`，檢查 `tool_results` 中是否有 `success=False`
3. 如果有錯誤且 `retry_count < max_retries`：回到 specialist node（帶上 error message）
4. 如果無錯誤或已到重試上限：走到 summarizer
5. 在 `backend/agents/nodes.py` 的 specialist prompt 中加入 "Previous attempt failed with: {error}" 上下文

這樣當交叉編譯失敗時，Agent 會自動分析 error log、修改程式碼、重新編譯，直到成功或達到重試上限。

---

## 附錄：關鍵檔案快速參考

| 需求 | 檔案 |
|------|------|
| 加新的 API endpoint | `backend/routers/` 下新增 .py，在 `backend/main.py` 掛載 |
| 加新的 Agent tool | `backend/agents/tools.py` 加 `@tool` 函數 |
| 加新的 LLM provider | `backend/agents/llm.py` 的 `_create_llm()` + `lib/providers.ts` |
| 改 Agent 路由邏輯 | `backend/agents/nodes.py` 的 `_ROUTE_KEYWORDS` 或 orchestrator_node |
| 改 LangGraph 拓樸 | `backend/agents/graph.py` 的 `build_graph()` |
| 改前端狀態管理 | `hooks/use-engine.ts` |
| 改前端 API 呼叫 | `lib/api.ts` |
| 改 SSOT 規格 | `configs/hardware_manifest.yaml` |
| 改 INVOKE 行為 | `backend/routers/invoke.py` 的 `_plan_actions()` |
| 改 REPORTER VORTEX 色彩 | `components/omnisight/vitals-artifacts-panel.tsx` 搜尋 `tagColor` |
| 改 Docker 編譯環境 | `backend/docker/Dockerfile.agent` |
| 改 workspace 隔離邏輯 | `backend/workspace.py` |
