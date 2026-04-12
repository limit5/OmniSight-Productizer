# OmniSight-Productizer: 工單系統 (Issue Tracking) 與 AI Agent 整合架構指南

本文件旨在規範 AI Agent 與專案管理/工單系統（如 Jira, Linear, GitHub/GitLab Issues）的整合架構。透過此整合，AI Agent 將從被動的「腳本執行者」升級為具備任務管理能力的「虛擬協同開發者」，實現高度自動化的軟體工程閉環。

---

## 一、 整合後的 AI 理想工作流 (Day in the Life of an AI Agent)

當 Agent 具備操作工單系統的能力後，其工作流將與人類工程師高度一致，形成自動化的開發循環：

1. **主動認領任務 (Pulling Tasks)：**
   * 系統排程喚醒 AI Agent。
   * Agent 透過 API 查詢當前 Sprint Backlog 中，狀態為 `To Do` 且標記有特定標籤（如 `@AI-Assigned`）的工單。
2. **解析規格與驗收標準 (Understanding AC)：**
   * Agent 讀取工單的標題、描述 (Description) 與驗收標準 (Acceptance Criteria)。
   * Agent 呼叫 API 將工單狀態更新為 `In Progress`，並留言：「*AI Agent (ID: #042) 已開始處理此任務。*」
3. **執行開發與內部交接 (Execution & Handoff)：**
   * Agent 在獨立的 Docker/WSL2 沙盒中進行程式碼修改與編譯。
   * 產出 `HANDOFF.md` 以紀錄技術斷點與下一步計畫。
4. **回報進度與申請審查 (Updating Progress & Review)：**
   * Agent 將程式碼推送至 Gerrit 建立 Patch Set。
   * Agent **自動呼叫工單系統 API**，於工單底下留言：「*已完成基礎架構，Gerrit Review 連結：[Link]，請人類主管協助審查。*」
   * Agent 將工單狀態切換為 `In Review`（或自定義的 `Waiting for Human` 狀態）。

---

## 二、 平台技術實作與評估 (Platform Evaluation & Implementation)

不同的工單系統在 API 複雜度與權限控管上有極大差異。建議依據專案階段採取**漸進式導入**策略。

### 1. 平台整合難易度評估

| 平台類型 | 代表工具 | 整合難易度 | API 特性與限制 | 導入建議 |
| :--- | :--- | :--- | :--- | :--- |
| **輕量級/原生** | GitHub Issues, GitLab Issues | **低 (⭐)** | REST/GraphQL API 直覺，與程式碼儲存庫天然綁定。狀態切換簡單 (Open/Closed)。 | **強烈建議作為初期概念驗證 (POC) 首選。** |
| **現代化/敏捷** | Linear | **中 (⭐⭐)** | GraphQL API 現代且快速。狀態管理具備彈性但不過度死板。 | 適合中型團隊，系統擴展期的最佳選擇。 |
| **企業級/高定製** | Jira | **極高 (⭐⭐⭐⭐)** | 權限控管極度嚴格，狀態機 (State Machine) 複雜，包含多種必填的自定義欄位 (Custom Fields)。 | 僅在團隊已有成熟 Jira 工作流，且熟悉 Jira API 開發時才考慮整合。 |

### 2. 核心技術實作機制

* **大模型函數呼叫 (Tool Calling / Function Calling)：** 利用 Claude/Gemini 等模型的 Tool Calling 能力，將 API 封裝為 AI 可理解的工具。
* **中介層設計 (Middleware Wrapper)：**
  **絕對不要讓 AI 直接使用原始的 REST API。** 必須在系統後端撰寫一個 Wrapper 轉換層（如 Python 或 Node.js），提供簡化後的接口給 AI 呼叫：
  * `get_next_task()`: 取得下一個待辦事項的精簡資訊。
  * `update_task_status(task_id, status)`: 推進狀態。
  * `add_task_comment(task_id, text)`: 新增留言與連結。

---

## 三、 實務挑戰與緩解策略 (Practical Pitfalls & Mitigations)

將 AI 接入工單系統時，最常面臨以下三種災難性場景，必須在架構設計時予以防範：

### ⚠️ 挑戰 1：狀態機限制 (State Machine Constraints)
* **問題描述：** 尤其是 Jira，工單狀態轉換通常有嚴格的單向規則（例如不能從 `To Do` 直接跳轉到 `Done`，必須經過 `In Progress`）。若 AI 隨意呼叫狀態更新，會遭到 API 拒絕 (HTTP 400)，導致 AI 崩潰或陷入無限重試。
* **緩解策略：** 中介層在提供 `update_task_status` 工具時，必須先透過 API 查詢「當前允許的下一步狀態清單 (Available Transitions)」，並**僅將這些合法選項提供給 AI 選擇**。

### ⚠️ 挑戰 2：上下文視窗爆炸 (Context Window Overflow)
* **問題描述：** 工單中常包含長篇大論的人類討論串、無關的 Log 截圖或過時的規格。若將完整 API 回傳的 JSON 餵給 AI，會迅速消耗 Token，甚至導致 AI 產生幻覺、忘記核心開發任務。
* **緩解策略：** 實作 **Payload 輕量化與摘要機制**。中介層在回傳工單資訊給 AI 時，必須過濾掉無關欄位，**僅保留標題、最新描述，以及最近的 3 則留言**。

### ⚠️ 挑戰 3：幻覺承諾與提前結案 (Hallucinated Commitments)
* **問題描述：** AI 模型為了展現「樂於助人」，可能會在程式碼尚未編譯通過，或根本沒推送到 Gerrit 的情況下，就主動呼叫 API 將工單標記為 `Done`。
* **緩解策略：** **實行客觀事實閘道 (Fact-Based Gating)。** 系統後端必須攔截 AI 的狀態更新請求。例如，當 AI 嘗試將工單改為 `In Review` 時，系統後端必須先檢查「該 Agent 關聯的工作區是否真的有成功 push 記錄」。若無，則拒絕更新並回傳錯誤訊息要求 AI 重新檢視程式碼。