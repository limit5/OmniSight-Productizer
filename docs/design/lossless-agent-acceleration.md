# OmniSight-Productizer: AI 智能體無損加速與效能最佳化架構

本文件定義了多智能體 (Multi-Agent) 系統的效能優化策略。旨在突破「速度與準確度」的物理限制，透過工程架構的升級，在**不犧牲任何輸出精度與邏輯能力**的前提下，將每一輪 Agent 的 API 延遲 (Latency) 與 Token 消耗降至最低，實現工業級的自動化流水線速度。

---

## 一、 四大無損加速引擎 (The 4 Acceleration Engines)

### 🚀 引擎 1：提示詞快取 (Prompt Caching) —— 讀取加速
**核心痛點：** Agent 每次啟動皆需重新讀取龐大的 SoC 手冊、`CLAUDE.md` 規範與歷史對話，導致極高的首字延遲 (TTFT) 與 API 成本。
* **實作規範：**
  * 在呼叫 LLM API（如 Claude 3.5 或支援快取的開源模型）時，將靜態不變的系統層級知識庫（System Prompts、工具定義檔、長篇 API Spec）加上 `ephemeral` 或 `cacheable` 標籤。
  * **動態數據隔離：** 將變動頻繁的日誌 (Log) 放在訊息的最尾端，確保前段的 Cache Hit Rate（命中率）達到 90% 以上。
* **效能提升：** 上下文讀取時間從 10 秒以上驟降至 **100 毫秒**以內，大幅降低 Input Token 成本。

### 🚀 引擎 2：差異化輸出 (Diff-Patching) —— 生成加速
**核心痛點：** 模型最耗時的操作是「生成 Token」。AI 若僅修改 3 行程式碼，卻重寫整份 2000 行的檔案，將浪費數十秒並增加幻覺風險。
* **實作規範：**
  * 剝奪 Agent 直接覆寫全檔的權限。
  * 強制 Agent 使用標準的 `Search-and-Replace` (搜尋與替換) 或 `Unified Diff` 格式來提交修改。
  * 由沙盒外的宿主機腳本 (Patch Applier) 負責將 Diff 應用到真實檔案上。
* **效能提升：** 輸出 Token 數量呈指數級下降，生成時間從 30 秒縮短至 **2~3 秒**，且修改精度大幅提升。

### 🚀 引擎 3：推測性平行處理 (Speculative Execution) —— 執行加速
**核心痛點：** 序列化工作流導致死板的等待時間（如：等待 Agent 寫完 Code $\rightarrow$ 才啟動 Docker $\rightarrow$ 才開始編譯）。
* **實作規範：**
  * **環境預熱：** 當調度者 Agent 決定將任務分派給 Tier 1 沙盒時，系統立即在背景非同步 (Async) 拉取 Docker Image 並啟動 Container。
  * **任務重疊：** 當代碼正在編譯時，調度者無需閒置，可同步啟動另一個 Agent 撰寫該次 PR (Pull Request) 的 Release Note 或更新 `HANDOFF.md`。
* **效能提升：** 隱藏基礎設施的啟動延遲，系統總體感等待時間減半。

### 🚀 引擎 4：語意預先載入 (RAG Pre-fetching) —— 檢索加速
**核心痛點：** 遇到錯誤時，Agent 呼叫搜尋工具查閱 L3 記憶庫，需要額外消耗一輪完整的 API 網路通訊時間。
* **實作規範：**
  * **主動注入：** 當沙盒回傳 `Exit Code != 0`（如 Compilation Error 或 Segfault）時，主系統的錯誤攔截器會**自動**擷取 Error Log 的特徵，查詢 L3 向量資料庫。
  * 將關聯的「歷史解法」作為附加 Context (如 `<related_past_solutions>`)，連同錯誤日誌一次性發送給 Agent。
* **效能提升：** 完全省去一次 Agent 主動呼叫 Tool 的等待時間（節省約 10~15 秒）。

---

## 二、 開發者 Agent 系統提示詞模板 (Diff-Patching 強制規範)

為了落實「引擎 2：生成加速」，請將以下規則加入所有負責撰寫程式碼的 Agent 的 `CLAUDE.md` 或 System Prompt 中：

```markdown
## ⚡ 代碼修改與輸出規範 (Code Modification Protocol)

為了極大化執行速度並防止程式碼毀損，**絕對禁止**在回覆中輸出完整的原始碼檔案。

當你需要修改現有檔案時，必須嚴格使用以下 `SEARCH/REPLACE` 區塊格式。
1. `SEARCH` 區塊必須包含足夠的上下文（通常是修改處的上下各 2-3 行），以確保能唯一匹配原始檔案中的該段落。
2. `REPLACE` 區塊則包含修改後的新程式碼。

**輸出範例：**
```python
<<<<<<< SEARCH
    def init_gpio(pin_number):
        # Initialize the hardware pin
        setup_pin(pin_number, MODE_IN)
=======
    def init_gpio(pin_number):
        # Initialize the hardware pin with Pull-Up resistor
        setup_pin(pin_number, MODE_IN, PULL_UP)
        verify_pin_state(pin_number)
>>>>>>> REPLACE
```
違規懲罰： 若你直接輸出超過 50 行未經修改的原始碼，系統攔截器將會判定任務失敗並強制重啟。
