# OmniSight-Productizer: RAG 語意預先載入 (Pre-fetching) 架構設計書

本文件定義了多智能體系統中「引擎 4：語意預先載入」的具體實作架構。透過在主系統端攔截錯誤並主動查詢 L3 向量記憶庫 (Vector DB)，我們能消除 Agent 主動呼叫檢索工具所產生的 API 往返延遲 (Round-trip Latency)，實現零等待的歷史經驗傳承。

---

## 一、 核心設計理念與解決痛點

* **傳統 RAG 流程 (慢)：** Agent 遇到編譯錯誤 $\rightarrow$ Agent 決定呼叫 `search_db` 工具 $\rightarrow$ 系統回傳結果 $\rightarrow$ Agent 重新思考解法。(多耗費 1 輪 API 請求，約 10~15 秒延遲)
* **Pre-fetching 流程 (快)：** 系統攔截到編譯錯誤 $\rightarrow$ 系統在背景秒查 Vector DB $\rightarrow$ 系統將「錯誤日誌 + 歷史解法」打包成 1 個 Prompt 一次性發送給 Agent。(0 額外延遲)

---

## 二、 核心運作流程 (Workflow Pipeline)

本機制部署於 Tier 1 (編譯沙盒) 與調度者 Agent 之間，分為四個自動化階段：

### Phase 1: 異常攔截 (Exception Interception)
* **監聽器：** 系統守護進程 (Daemon) 持續監聽 Docker 沙盒的執行狀態。
* **觸發條件：** 當沙盒執行 `./simulate.sh` 或 `make` 命令後，若回傳 `Exit Code != 0` (如：編譯失敗、Segmentation Fault、核心崩潰)。

### Phase 2: 特徵萃取 (Signature Extraction)
* **雜訊過濾：** 原始的 Error Log 通常包含大量無意義的記憶體位址或時間戳記，會干擾向量檢索的準確度。
* **正則萃取 (Regex)：** 系統使用 Python 腳本自動過濾日誌，僅提取核心特徵：
  * C++ Linker Error (`Undefined reference to...`)
  * CMake Error (`Target not found...`)
  * 特定 SoC 廠商的 SDK 報錯代碼 (`RKNN_ERR_FAIL...`)

### Phase 3: 向量預檢索 (Vector Pre-fetching)
* **語意比對：** 系統將萃取出的「錯誤特徵」與當前的「SoC 標籤 (如：`Vendor=Rockchip`)」轉為向量 (Embeddings)。
* **相似度閾值：** 向 L3 經驗記憶庫發起查詢。僅當相似度 (Cosine Similarity) **大於 0.85** 時，才判定為「高度相關的歷史經驗」。(避免塞入無關記憶導致 Agent 降智)

### Phase 4: 上下文無縫注入 (Context Injection)
* 系統將查詢到的歷史解決方案，格式化為 XML 標籤區塊，並**直接附加上下文**，與錯誤日誌一起發送給開發者 Agent。

---

## 三、 系統提示詞注入模板 (Injection Template)

當系統成功預先載入歷史記憶時，發送給 Agent 的 Prompt 結構必須嚴格遵循以下格式：

```xml
<task_update>
沙盒編譯失敗。請修復以下錯誤並重新提交。
</task_update>

<error_log>
[在此插入經過萃取的 Error Log 特徵]
/opt/vendor-sdk/lib/libmedia.so: undefined reference to `v4l2_open`
</error_log>

<system_auto_prefetch>
💡 系統在 L3 經驗記憶庫中發現了與此錯誤高度相似的歷史解法，請優先參考以下經驗進行除錯：

<past_solution>
  <bug_context>在 Fullhan SoC 環境下整合 V4L2 驅動時發生連結錯誤</bug_context>
  <working_fix>
    原廠 SDK v1.2 的 CMakeLists.txt 預設漏掉了系統底層庫。
    解決方案：必須在 target_link_libraries 中強制補上 `-lv4l2` 與 `-lrt`。
  </working_fix>
</past_solution>
</system_auto_prefetch>
```

---

## 四、 防呆機制與邊界條件 (Edge Cases & Fallbacks)

為確保 Pre-fetching 不會變成干擾 Agent 判斷的「毒藥」，系統必須實作以下防護網：

1. **版本嚴格匹配 (Version Hard-Match)：**
   若 L3 記憶庫回傳的解法標記為 `SDK_v1.0`，而當前系統環境為 `SDK_v2.0`，即使語意相似度高達 0.99，系統也**拒絕注入**，避免 Agent 使用已被原廠棄用的舊 API 解決問題。
2. **長度截斷 (Length Truncation)：**
   預檢索回傳的歷史解法，總長度不得超過 1000 Tokens。若歷史紀錄過長，系統僅注入 `<working_fix>` 的核心邏輯段落，放棄注入完整的歷史程式碼。
3. **無命中降級 (Cache Miss Fallback)：**
   若向量資料庫回傳的相似度全低於 0.85，系統將**隱藏** `<system_auto_prefetch>` 區塊，直接將純粹的 `<error_log>` 發送給 Agent，讓 Agent 自行發揮原本的推理能力。