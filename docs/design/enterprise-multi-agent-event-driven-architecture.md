# OmniSight-Productizer: 企業級多智能體事件驅動架構白皮書 (Enterprise Multi-Agent Event-Driven Architecture)

本文件定義了 OmniSight 系統如何捨棄單機腳本，採用「事件驅動 (Event-Driven)」與「分散式佇列 (Distributed Queue)」構建企業級的多智能體協作網路。本架構無縫整合 JIRA (Scrum) 與 Git Forge (GitHub/GitLab)，並透過分散式鎖與專職智能體徹底解決平行處理下的競爭危害 (Race Condition) 與合併衝突 (Merge Conflict)。

---

## 一、 核心架構：事件驅動與任務解耦

系統不再使用本機檔案 (如 `TODO.md`) 進行輪詢排程，而是轉型為標準的微服務 (Microservices) 架構。

### 1. 意圖層 (Intent Layer)：JIRA Scrum 看板
* **人類介面**：PM 與技術主管僅在 JIRA 進行 User Story 的建立、驗收標準定義與 Sprint 規劃。
* **事件觸發**：當 JIRA Story 狀態變更為 `In Progress` 時，JIRA 自動觸發 Webhook 打向中控網關 (Orchestrator Gateway)。

### 2. 規劃層 (Planning Layer)：Orchestrator Gateway
* **角色**：作為系統的大腦（可部署為 FastAPI 或 Node.js 服務）。
* **行為**：
  1. 接收 JIRA Webhook，解析業務意圖。
  2. 生成系統的 DAG（有向無環圖）任務依賴。
  3. 為每個子任務生成 **CATC 高精細任務卡**。
  4. 透過 API 將 Sub-tasks 寫回 JIRA。
  5. 將任務推入 **分散式訊息佇列 (如 Redis / RabbitMQ / AWS SQS)**。

### 3. 執行層 (Execution Layer)：Agent Worker Pool
* **角色**：無狀態 (Stateless) 的容器化 Agent 節點。
* **行為**：Worker 節點訂閱訊息佇列。一旦有處於 `Ready` 狀態的任務，閒置的 Agent Worker 便會主動領取任務（Pull Model），配置獨立的 Docker 沙盒環境並開始開發。

---

## 二、 CATC 任務卡標準格式 (Context-Anchored Task Card)

放入訊息佇列的每一個任務 Payload，必須嚴格遵守 CATC 格式，確保任何 Worker 領到任務都能「秒入狀況」。

```json
{
  "jira_ticket": "PROJ-402",
  "acceptance_criteria": "系統能根據設定檔動態切換 UVC 或 RTSP 來源，且記憶體無洩漏。",
  "navigation": {
    "entry_point": "./src/camera/stream_manager.cpp",
    "impact_scope": {
      "allowed": ["src/camera/*", "include/camera_api.h"],
      "forbidden": ["src/core/npu_pipeline.cpp"]
    }
  },
  "domain_context": "ARM32 架構下切換影像串流時，必須先確保 V4L2 緩衝區已完全 munmap，否則會引發 Kernel Panic。",
  "handoff_protocol": [
    "執行 make test 通過沙盒驗證",
    "Commit 代碼並發起 Pull Request",
    "透過 JIRA API 將狀態轉為 Reviewing"
  ]
}
```

---

## 三、 平行處理的防護與衝突解析 (Concurrency & Conflict Resolution)

在 Worker Pool 模型下，多個 Agent 會同時全速開發。為了解決 Race Condition 與 Merge Conflict，系統實作「三層防禦架構」：

### 🛡️ 第一層：事前防堵 —— Redis 分散式互斥鎖 (Distributed Pessimistic Lock)
* **機制**：當 Orchestrator 準備將任務推入佇列前，會掃描 CATC 的 `impact_scope`。
* **鎖定**：系統向 Redis 申請檔案路徑的鎖（例如：`LOCK:include/camera_api.h`）。
* **決策**：若 Task B 需要修改的檔案已被 Task A 鎖定，Task B 的狀態將被標記為 `Blocked_by_Mutex` 並留在滯留區，直到 Task A 完成 PR 並釋放鎖後，才進入 `Ready` 佇列。
* **優點**：從根源防止核心系統檔案發生覆寫災難。

### 🛡️ 第二層：事中隔離 —— 樂觀分支模型 (Optimistic Branching)
* **機制**：對於沒有鎖定衝突的任務，Agent Worker 領取任務後，第一步是執行 `git checkout -b feature/PROJ-402` 建立獨立分支。
* **開發**：Agent 在自己專屬的沙盒與分支中開發，完全不受其他平行 Agent 的干擾。
* **交付**：任務完成後，不直接推上 `main`，而是透過 GitHub/GitLab API 建立一個 Pull Request (PR)。

### 🛡️ 第三層：事後仲裁 —— PR 攔截與 Merger Agent
當多個 PR 嘗試合併入 `main` 發生 Git 衝突 (`CONFLICT`) 時，將觸發 CI/CD Pipeline 中的應急機制：

1. **Webhook 觸發**：Git Forge 發送 `Merge Conflict` 事件至 Orchestrator。
2. **喚醒 Merger Agent**：Orchestrator 不親自處理，而是將衝突區塊（包含 `<<<<<<< HEAD` 等標記）送入一個特定的高智商模型實體（The Merger Agent）。
3. **專職仲裁**：Merger Agent 的唯一 Prompt 約束為：「你是一個合併衝突解決專家。請閱讀雙方代碼，保留兩者的邏輯意圖（如 Agent A 的日誌與 Agent B 的錯誤處理），刪除 Git 標記，並輸出乾淨的代碼。」
4. **自動化解**：Merger Agent 產出結果後，CI/CD 腳本自動執行 `git commit`，化解衝突並完成合併。

---

## 四、 企業級 Sprint 運作全景圖 (The Enterprise Workflow)

1. **[Human]** PM 在 JIRA 將 Sprint Backlog 中的 Story 移至 `In Progress`。
2. **[System]** JIRA Webhook 喚醒 Orchestrator Gateway。
3. **[AI-Orch]** Orchestrator 解析需求，產出 DAG 與多張 CATC，寫回 JIRA Sub-tasks。
4. **[System]** Orchestrator 進行 Redis 鎖定檢查，將無衝突的任務推入 Message Queue。
5. **[AI-Dev]** 叢集中的 Agent Workers 抓取任務，建立 Git 分支平行開發。
6. **[AI-Dev]** Worker 完成沙盒驗證，發起 PR，並透過 API 更新 JIRA 子任務狀態。
7. **[System]** CI/CD 執行合併。若遇衝突，自動喚醒 Merger Agent 進行邏輯融合。
8. **[System]** 所有 PR 合併完成，Story 自動推進至 `Done`，人類主管進行最後驗收。