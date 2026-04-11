# OmniSight-Productizer: RTK (Rust Token Killer) 導入與優化架構指南

本文件定義了在 OmniSight-Productizer 系統中導入 `rtk-ai/rtk` 工具的標準實作規範。RTK 負責在底層攔截並壓縮終端機輸出的高雜訊日誌，旨在解決 AI Agent 處理 C/C++ 等底層開發時常遇到的「上下文溢出 (Context Window Overflow)」問題，並巨幅降低 Token 消耗成本。

---

## 一、 整合後的 AI 理想工作流 (The RTK-Optimized Workflow)

導入 RTK 後，AI 開發者 (AI Coder Agent) 的日常工作流將變得更加專注與高效。系統會在背景默默過濾雜訊，確保 AI 只看到「有價值的技術線索」。

1. **環境啟動與靜默攔截 (Silent Interception)：**
   * 當系統派發任務並啟動獨立的 Docker/WSL2 沙盒時，RTK 已在全域 (Global) 載入。
   * AI Agent 像往常一樣開始工作，完全不需要意識到 RTK 的存在。
2. **執行高雜訊指令 (Executing Noisy Commands)：**
   * Agent 執行容易產生大量輸出的指令，例如：`make all`、`git diff`、或尋找特定字串的 `find / grep`。
   * **RTK 介入：** RTK 底層的 Bash Hook 會自動攔截這些輸出。它會剃除重複的 Warning、過長的進度條與無意義的空白行，將數千行的 Log 壓縮成數十行的「錯誤簽章 (Error Signatures)」。
3. **精準除錯與決策 (Focused Debugging)：**
   * Agent 收到壓縮後的乾淨輸出。由於上下文視窗非常充裕，AI 不會發生「失智」或「忘記任務目標」的幻覺。
   * Agent 能立刻針對關鍵的 Error Line 進行程式碼修改 (例如修復特定的 Memory Leak 或指標越界)。
4. **狀態交接與提交 (State Handoff)：**
   * 當 Agent 準備撰寫 `HANDOFF.md` 進行狀態交接時，它可以利用 RTK 壓縮過後的精簡版 Git Log 與編譯狀態，產出更具可讀性與重點的交接文件給下一任 Agent 或人類審查員。

---

## 二、 推薦技術實作方式評估 (Technical Implementation)

為確保系統的穩定性與 Agent 行為的一致性，推薦採用**「全域環境綁定」**搭配**「提示詞輔助」**的雙軌並行策略。

### 1. 基礎架構層：Docker 容器全域掛載 (強烈推薦 ⭐⭐⭐⭐⭐)
在系統為 AI 配置的基礎映像檔 (Base Image) 中，直接植入並初始化 RTK。這是最無縫的整合方式。

* **實作腳本 (Dockerfile 範例)：**
  ```dockerfile
  # 1. 安裝 RTK 單一執行檔
  RUN curl -fsSL [https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh](https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh) | sh
  
  # 2. 初始化全域 Bash Hook
  RUN rtk init --global
  ```
效益： Agent 依然使用標準的 git 或 make 指令，但輸出會被自動壓縮。無需修改 Agent 的核心邏輯。

### 2. 應用邏輯層：System Prompt 強制規範 (輔助手段 ⭐⭐⭐⭐)
部分基於 LLM 的代理工具 (如 Claude Code) 可能會透過其原生 API (而非透過系統 Bash 殼層) 來讀取檔案。為防止 RTK 被繞過，必須在 System Prompt 中加入規範。

實作方式 (加入 Agent Prompt)：

「當你需要搜尋專案內容、檢視大量程式碼差異，或執行編譯時，請務必使用標準的 Shell Command (如 cat, grep, make)。若輸出內容過大，你必須主動在指令前方加上 rtk 前綴 (例如：rtk cat src/main.c) 以確保你不會消耗過多 Token。」

## 三、 實務挑戰與緩解策略 (Practical Pitfalls & Mitigations)
導入資料壓縮機制無可避免會帶來一些副作用，以下是實務上最常遇到的問題及解法：

⚠️ 挑戰 1：過度壓縮導致「關鍵線索遺失」
問題描述： RTK 的去重 (Deduplication) 策略較為激進。在 C/C++ 複雜的 Segmentation Fault 或深度 Call Stack 追蹤中，有時看似重複的指標記憶體位址其實是破案關鍵。RTK 把它過濾掉會導致 AI 找不到 Bug 根源。

緩解策略 (Fallback Mechanism)： 在系統層設定 「重試降級機制」。如果 AI 針對同一個編譯錯誤連續修改兩次都失敗，系統應自動指示 AI 改用原始指令 (例如加上 --no-rtk 參數，或直接使用原生指令不經過 Hook) 重新抓取完整的 Log。

⚠️ 挑戰 2：與原生 LLM Tool Calling 的繞道衝突 (Bypass Issue)
問題描述： 如果您的 AI Agent 是透過其內建的 Read_File_Tool 直接用 Python 讀取檔案，這些操作完全不會經過 Bash 環境，RTK 的攔截機制將形同虛設。

緩解策略： 剝奪或限制 Agent 原生的檔案讀取權限，強迫它統一依賴 Execute_Shell_Command 這個 Tool 來與環境互動。這能確保所有資料流動必定經過 RTK 的守門。

⚠️ 挑戰 3：二進位與特殊編碼檔案的污染
問題描述： 如果 Agent 誤下指令去讀取 .o 檔、.so 函式庫或影像 Raw Data (NV12/YUV)，RTK 處理這些二進位亂碼可能會造成不可預期的錯誤或消耗多餘算力。

緩解策略： 建立嚴格的 .rtkignore 或全域設定檔，明確排除編譯過程產生的二進位目錄 (如 /build, /bin)，確保 RTK 只處理純文字的日誌與原始碼。