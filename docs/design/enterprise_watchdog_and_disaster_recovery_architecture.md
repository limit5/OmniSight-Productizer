# OmniSight-Productizer: 企業級多智能體全維度守護與部署架構 (Enterprise Production Blueprint)

本文件定義了 OmniSight 系統的商用級架構規範。系統採用事件驅動模型，結合「語意級監控」、「工具執行網關」與「階梯式部署策略」，旨在確保高壓開發場景下的系統安全性、自癒能力與物理穩定性。

---

## 一、 核心架構：事件驅動任務鏈 (Event-Driven Task Chain)

系統全面解耦，確保在無人值守狀態下仍能透過訊息佇列 (Message Queue) 進行狀態同步與斷點續傳。

1.  **意圖與看板層 (JIRA / Scrum)**：
    * **意圖解析**：JIRA Story 移至 `In Progress` 時發送 Webhook，觸發 Orchestrator。
    * **任務追蹤**：Agent 自動在 JIRA 建立 Sub-tasks，完成後回傳 Git Hash 與測試結果。
2.  **調度中心 (Orchestrator Gateway)**：
    * **有向無環圖 (DAG)**：將任務拆解為具備依賴關係的最小執行單元。
    * **分散式鎖 (Redis Lock)**：依據 CATC 的 `impact_scope`，在派發前對特定路徑進行鎖定，防止併發修改衝突。
3.  **執行叢集 (Worker Pool)**：
    * **無狀態容器**：從隊列領取 CATC 任務卡，執行 ReAct 微迴圈。
    * **心智卸載**：每 10 輪對話自動將進度寫入 `scratchpad.md`，實現記憶續傳。

---

## 二、 任務與交接標準：CATC 格式 (Context-Anchored Task Card)

放入訊息佇列的每一個任務 Payload，必須嚴格遵守 CATC 格式：

```json
{
  "id": "TKT-891",
  "priority": "High",
  "context": {
    "entry_point": "backend/services/inventory_sync.py",
    "impact_scope": ["backend/services/inventory*", "backend/models/stock.py"],
    "domain_knowledge": "Redis 緩存穿透防護：在查詢資料庫前，必須透過 Bloom Filter 驗證 ID 有效性。"
  },
  "acceptance_criteria": [
    "通過 pytest 單元測試且覆蓋率達 80% 以上",
    "模擬 10,000 筆併發請求無 Deadlock",
    "符合 OpenAPI 3.0 規格"
  ],
  "state_snapshot": "BASE64_ENCODED_SNAPSHOT_DATA" 
}
```

---

## 三、 全維度守護與自動化修復機制 (Guardianship & Recovery)

本章節整合了最新的 **PEP 網關** 與 **冪等性重試** 機制，構建五道安全防線。

### 1. 工具執行網關 (PEP: Policy Enforcement Point)
為了解決 Agent 幻覺導致的高危操作，所有工具呼叫 (Tool Calling) 必須經過 PEP 網關：
* **指令審核**：攔截所有 Shell 指令。嚴格禁止 `rm -rf /`、`chown` 等毀滅性操作。
* **權限提升攔截**：當指令涉及生產環境配置 (如 `deploy.sh prod`) 時，PEP 自動掛起任務。
* **人類審核 (Human-in-the-loop)**：高危指令將觸發 ChatOps 推播，必須由人類在通訊軟體點擊「核准」後，網關才會放行至沙盒。

### 2. 冪等性重試與工作區回滾 (Idempotent Retry)
當任務失敗或超時需要重試時，必須消除前一個 Agent 留下的副作用：
* **工作區清理**：重試前，系統強制執行 `git clean -fd` 與 `git checkout .`。
* **狀態回滾**：確保工作目錄恢復到任務開始前的「純淨錨點 (Clean Anchor)」，防止髒程式碼干擾下一次嘗試。

### 3. 語意與假死監控 (Semantic UX Monitor)
* **語意熵值檢測 (Entropy Check)**：偵測輸出內容。若重複率過高或連續幾輪無實質產出，判定為「認知死結」。
* **物理資源隔離 (Cgroups)**：限制 Agent 的 CPU/RAM 上限，確保宿主機不被單一錯誤容器拖垮。

### 4. 自動續寫與狀態恢復 (Auto-Continuation)
* **Token 溢出處理**：自動偵測 `max_tokens` 截斷，自動發送「請繼續輸出」指令。
* **記憶快照還原**：任務重啟後自動加載 `scratchpad.md`，實現「記憶續接」。

---

## 四、 全渠道通報與 ChatOps 遠端介入

### 1. 智慧事件路由 (Intelligent Routing)
當 Watchdog 偵測到 P2 (邏輯卡死/PEP 攔截) 或 P1 (系統崩潰) 異常時：
* **富文本告警**：推播至 Discord/Teams/Line。訊息包含 Agent 思考路徑、報錯截圖及【核准/阻斷】按鈕。
* **P1 (系統停機)**：**SMS** + **緊急電話** (SRE Team) + **Teams/Discord @everyone**。
* **P2 (任務卡死)**：**JIRA 自動 Blocked 標籤** + **Discord/Line/Slack** 群組推播（包含日誌截圖）。
* **P3 (自動修復中)**：**E-mail 匯總報告**。

#### 情境：Agent 在 PROJ-402 任務中因「邏輯鬼打牆」被 Watchdog 暫停 (Suspended)。
**1. 接收告警 (The Alert)**
使用者在 Discord / Teams / Line 收到 Watchdog 推播的 Interactive Message：
> 🚨 **[P2 異常介入請求] PROJ-402 發生邏輯死結**
> * 狀態：Agent 已連續 4 輪無法修復 CMake 連結錯誤。任務已暫停。
> * 現場日誌：`undefined reference to 'v4l2_open'`
> * 請選擇操作： `[🔴 強制斬殺任務]` `[🟢 注入提示並繼續]` `[🟡 重置沙盒重試]`
**2. 遠端修復 (The Intervention)**
使用者直接在通訊軟體中回覆機器人指令（ChatOps）：
* **指令範例**：`/omnisight inject PROJ-402 "請在 target_link_libraries 中手動加入 -lv4l2"`
* **API 替代方案**：使用者也可登入 Web Dashboard，在該任務卡片點擊「Provide Hint」。
**3. 狀態機熱啟動 (Hot Resume)**
Watchdog 接收到使用者的 Hint 後，**不會**重啟整個 Docker 沙盒，而是：
1. 將人類的 Hint 包裝為最高權限的 `<system_override>` 標籤。
2. 將其強行塞入 Agent 暫停前的 Context Window 尾端。
3. 解除任務暫停 (Resume)，讓 Agent 帶著人類的「神諭」繼續執行下一輪 ReAct 推理。

### 2. ChatOps 互動指令
* **`/omnisight inspect [ID]`**：查看 Agent 的最後三輪 ReAct 日誌。
* **`/omnisight inject [ID] "提示"`**：將人類智慧（如：修正參數建議）注入 Context 尾端以打破死結。
* **`/omnisight rollback [ID]`**：強制手動觸發工作區回滾。

---

## 五、 階梯式部署與高可用策略 (Deployment & HA Hierarchy)

本架構支援靈活的部署方式，請依據硬體條件與專案規模按以下優先級進行佈署：

### 【優先級 1】方案一：單機極限防禦 (Single Node + Cgroups) - **首選推薦**
* **適用場景**：底層系統開發、需實體硬體掛載、資源有限的本地開發。
* **關鍵實作**：使用 `Docker Compose` 搭配實體資源硬上限 (`mem_limit`, `cpus`)。
* **防護機制**：實作 `Auto-Janitor` 定時清理腳本，防止磁碟爆滿導致宿主機假死。

### 【優先級 2】方案二：主備容災架構 (Active-Standby VRRP)
* **適用場景**：具備兩台實體機或 VM，要求在主機燒毀時能自動接手。
* **關鍵實作**：使用 `Keepalived` 管理虛擬 IP (VIP)，搭配 `Redis Master-Slave` 進行狀態實時抄寫。
* **切換邏輯**：當主機 A 斷電，主機 B 取得 VIP 並自動重啟 Worker 叢集，接續訊息佇列中的任務。

### 【優先級 3】K8s 叢集部署 (Kubernetes Managed)
* **適用場景**：專案進入商用爆發期，需要大量 Agent 同時跑 Sprint，且有專職運維。
* **關鍵實作**：利用 K8s 的 `ReplicaSet` 與 `Pod Priority` 進行故障轉移與任務再分配。

### 【優先級 4】方案三：雲端全代管 (Serverless Container PaaS)
* **適用場景**：不考慮硬體成本，純軟體開發，追求極速橫向擴展。
* **關鍵實作**：利用 `AWS Fargate` 或 `GCP Cloud Run` 徹底消滅「宿主機」概念，任務隨跑隨開。