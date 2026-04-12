# OmniSight-Productizer: AI Agent 分層記憶架構 (Tiered Memory Architecture) 設計書

本文件定義了 OmniSight-Productizer 系統中 AI Agent 的記憶管理機制。在嚴謹的 C/C++ 嵌入式開發與軟硬整合環境中，AI 的記憶不能是「無差別吸收的黑洞」，而必須是「階層化、具備時效性且可控的知識庫」。

透過導入 **L1 核心記憶**、**L2 工作記憶** 與 **L3 經驗記憶** 的三層架構，系統能確保 AI 既遵守嚴格的開發紀律，又能具備跨專案的長期學習能力。

---

## 一、 架構總覽與記憶分層定義

### 🔴 L1: 核心記憶 (Core Memory - The DNA)
* **系統定位：** 絕對的權威與法則 (Immutable Rules)。
* **技術實作：** `CLAUDE.md`、System Prompts、標準化 SOP (如 `vendor-sdk-guide.md`)。
* **記憶內容：**
  * Agent 角色定義 (如：C/C++ 底層開發者、Gerrit 審查員)。
  * 絕對不可違背的指令規則 (例如：強制使用 `rtk` 前綴執行高雜訊指令)。
  * 專案的 HAL 介面規範與目標架構編譯參數 (CMake Toolchain 設定)。
* **生命週期與讀寫權限：**
  * **唯讀 (Read-only)：** AI Agent 無權修改 L1 記憶。
  * **全域載入：** 在每次對話或任務啟動時，由主系統強制注入 Context Window 的最頂層。

### 🔵 L2: 工作記憶 (Working Memory - The Desk)
* **系統定位：** 當前任務的上下文 (Current Context)。
* **技術實作：** 大語言模型的上下文視窗 (LLM Context Window)、`HANDOFF.md` 狀態檔、當前 Jira 工單內容、暫存的 RTK 日誌。
* **記憶內容：**
  * 我現在正在解哪一個 Bug？規格書 (AC) 是什麼？
  * 過去 10 分鐘內編譯失敗的 3 次 Valgrind 報錯內容。
  * `HANDOFF.md` 中記錄的上一手交接進度。
* **生命週期與讀寫權限：**
  * **讀寫 (Read/Write)：** Agent 可隨時更新 `HANDOFF.md` 與本地暫存檔。
  * **任務拋棄 (Task-scoped)：** 當 Jira 工單狀態切換為 `Done` 或 `In Review` 時，L2 記憶將被清空 (或總結後轉移至 L3)，以釋放 Token 空間並防止干擾下一個任務。

### 🟣 L3: 經驗記憶 (Episodic / Long-term Memory - The Library)
* **系統定位：** 跨專案的歷史經驗與避坑指南 (Historical Knowledge Base)。
* **技術實作：** 向量資料庫 (Vector DB，如 Chroma / Pinecone) 搭配 AI 工具呼叫 (Tool Calling)。
* **記憶內容：**
  * 過去成功 Merge 的 Gerrit Patch Set 解決方案。
  * 特定 SoC SDK 版本的隱藏 Bug 與 Workaround (例如：「Fullhan SDK v1.2 必須外掛 `-l_vendor_media` 才能編譯通過」)。
* **生命週期與讀寫權限：**
  * **主動檢索 (Query-based)：** 絕對**不可**自動注入 prompt。Agent 必須透過明確呼叫工具 (如 `search_past_solutions(error_signature)`) 才能獲取。
  * **受控寫入：** Agent 無法直接寫入 L3。只有當 PR 成功被人類 Merge 後，CI/CD 系統才會自動將「Bug 描述 + 最終解法」向量化並存入 L3。

---

## 二、 標準除錯工作流 (Memory-Driven Debugging Workflow)

當 Agent 遇到一個未知的編譯或系統崩潰錯誤時，其記憶調用順序如下：

1. **[依賴 L2] 快速重試：** Agent 讀取 L2 中的 RTK 壓縮日誌，嘗試根據當前 Context 進行 1~2 次的程式碼修正。
2. **[查閱 L1] 檢視規範：** 若修復失敗，Agent 檢視 L1 (`CLAUDE.md`)，確認是否違反了該 SoC 架構的特定編譯限制。
3. **[呼叫 L3] 歷史檢索：** 若錯誤極度冷門 (如 Linker Error)，Agent 主動呼叫工具 `query_vector_db(query="Undefined reference to v4l2_open in Vendor SDK")`。
4. **[記憶過濾] 時效驗證：** Agent 取得 L3 回傳的歷史解法後，比對該解法的 `SDK_Version` 標籤與當前環境是否相符，避免套用過期的舊解法。
5. **[狀態更新] 寫入 L2：** 成功解決問題後，Agent 將解決思路寫入 L2 的 `HANDOFF.md` 中，準備交接。

---

## 三、 記憶系統的風險緩解機制 (Mitigations)

在嵌入式系統中，「錯誤的記憶比沒有記憶更可怕」。系統強制實施以下防護網：

### 1. 拒絕記憶污染 (No Garbage Collection)
* 失敗的嘗試、無限迴圈的 Log、被退回的程式碼，**一律禁止**寫入 L3 經驗記憶庫。只有通過人類審查 (Gerrit +2) 的最終解答，才具備進入 L3 的資格。

### 2. 強制元數據標籤 (Mandatory Metadata Tagging)
* 所有進入 L3 的記憶碎片，必須附帶嚴格的 Metadata 標籤：
  * `timestamp`: 寫入時間
  * `soc_vendor`: 晶片廠商 (例如: Rockchip / Fullhan)
  * `sdk_version`: 開發包版本
  * `hardware_rev`: EVK 硬體版號
* **目的：** 避免 Agent 拿 Rockchip 的歷史解法，去套用在 Fullhan 的晶片上產生嚴重幻覺。

### 3. 上下文截斷 (Context Truncation)
* L2 工作記憶必須設定嚴格的 Token 上限。當對話輪次過長導致 Context 接近爆滿時，系統會強制觸發 `summarize_state` 工具，將過去 20 輪的對話壓縮成一段 300 字的摘要，覆蓋掉過度冗長的對話歷史。