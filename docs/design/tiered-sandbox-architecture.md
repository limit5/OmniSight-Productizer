# OmniSight-Productizer: 階層式沙盒安全與執行架構設計書 (Tiered Sandbox Architecture)

本文件定義了多智能體 (Multi-Agent) 在自動化嵌入式開發系統中的執行環境架構。基於「能力與安全的終極博弈」，本系統揚棄傳統單一且死板的沙盒設計，改採 **「階層式沙盒架構」**。

**核心設計哲學：大腦活在雲端，手腳伸進客製化的沙盒。**
不同職責的 AI Agent 將被派發至對應安全等級的環境中執行，以確保系統穩定性、防止惡意代碼外溢，同時保證對實體硬體 (EVK) 的操作能力。

---

## 一、 架構總覽：四層隔離模型 (The 4-Tier Model)

系統環境由上至下分為四個層級，越底層對應的硬體操作能力越強，但其指令介面 (API) 的限制也越嚴格。

### 🛡️ Tier 0: 主控核心區 (Control Plane)
* **環境屬性：** 無沙盒 / 雲端伺服器 (AWS EC2 / Vercel Serverless)
* **進駐角色：** 調度者 Agent (Orchestrator)、狀態管理系統、OpenRouter API 閘道。
* **安全機制：** * 存放所有機密憑證 (API Keys、Gerrit SSH Keys)。
  * **絕對禁止** 在此層直接執行任何 AI 生成的腳本或編譯指令。
* **主要職責：** 思考、發派工單、分發憑證與監控底層沙盒狀態。

### 🔒 Tier 1: 嚴格隔離沙盒 (Strict Sandbox)
* **環境屬性：** 瞬態微虛擬機 (Ephemeral MicroVM，如 Firecracker / gVisor) 或嚴格限制的 Docker。
* **進駐角色：** C/C++ 開發 Agent、演算法重構 Agent、NPU 模型轉換 Agent。
* **安全機制：**
  * **實體斷網 (Air-gapped)：** 除特定白名單 (如 Git Server) 外，禁止對外連線，防範資料外洩。
  * **資源配額 (Cgroups)：** 限制最高 4 vCPU / 8GB RAM，防範 AI 寫出死迴圈或內存溢出 (OOM) 導致宿主機崩潰。
  * **用完即毀：** 每次任務結束立即銷毀 Container，確保下次編譯環境 100% 乾淨 (Reproducibility)。
* **主要職責：** `make` 交叉編譯、執行 Valgrind 檢測、跑 Python 數據分析腳本。

### 🌐 Tier 2: 網路穿透沙盒 (Networked Sandbox)
* **環境屬性：** 具備 Egress (對外) 網路權限的 Docker 容器。
* **進駐角色：** MLOps 資料爬蟲 Agent、第三方 API 測試 Agent。
* **安全機制：**
  * **內網阻斷 (VPC Isolation)：** 允許 AI 連接外部網際網路查資料，但在防火牆層級 (iptables/Security Groups) **封鎖所有企業內網 (LAN) IP 段**。
  * 防止 AI 被提示詞注入 (Prompt Injection) 後，掃描或攻擊公司內部的資料庫。
* **主要職責：** 從外部資料集下載訓練資料、比對開源社群的 Bug 解法。

### 🔌 Tier 3: 條件式實體橋接區 (Hardware Bridge)
* **環境屬性：** 連接實體開發板 (EVK) 的本機測試伺服器 (Bare Metal)。
* **進駐角色：** 硬體驗證 Agent (HVT)、NPU 實機測試 Agent。
* **安全機制 (最高級別防護)：**
  * **Agent 禁入本機：** AI Agent **不允許**透過 SSH 登入這台實體機或取得 Bash 權限。
  * **RPC / API 代理介面：** 在實體機上部署一個「守門員程式 (Hardware Daemon)」。Agent 只能傳送高階 JSON 請求 (如 `{"action": "flash_board", "firmware_url": "..."}`)，由守門員進行參數校驗後代為執行。
  * 禁止危險系統指令 (如 `rm`, `dd` 覆寫系統磁區)。
* **主要職責：** 燒錄韌體至 EVK、讀取 UART 串口日誌、擷取真實 I2C/SPI 訊號。

---

## 二、 標準任務流轉與沙盒生命週期 (Lifecycle)

以下展示一個包含「編譯 $\rightarrow$ 模擬 $\rightarrow$ 實機測試」的典型工單，在階層沙盒中是如何流轉的：

1. **[Tier 0] 任務解析：** 調度者 Agent 收到 Jira 工單，決定啟動編譯與實機驗證流程。
2. **[Tier 1] 啟動編譯沙盒：** 系統從 Image Registry 拉取包含 CMake 與 ARM 編譯器的 Docker Image，啟動為 Tier 1 沙盒。
3. **[Tier 1] 代碼注入與編譯：** AI 寫完程式碼後，在沙盒內執行 `./simulate.sh`。此時若發生 Segmentation Fault，只會導致該容器報錯，主系統毫髮無傷。
4. **[Tier 1] 產出提取：** 編譯成功後，系統將 `.bin` 或 `.rknn` 執行檔從沙盒中萃取出來，隨即**強制銷毀 (Kill)** 該 Tier 1 沙盒。
5. **[Tier 3] 橋接部署：** 系統將編譯好的執行檔透過 RPC 傳送給 Tier 3 的 Hardware Daemon。Daemon 負責透過 USB 燒錄至 EVK，並將 Serial Log 回傳給 Tier 0 的 AI 進行判讀。

---

## 三、 沙盒建置的技術選型建議 (Tech Stack Recommendations)

為了將此架構落地，建議工程團隊採用以下開源或企業級基礎建設技術：

| 需求模組 | 推薦技術棧 | 採用理由 |
| :--- | :--- | :--- |
| **輕量級沙盒引擎** | **Docker** 搭配 **gVisor** | 相比純 Docker，gVisor 提供 User-space kernel 隔離，防止 Agent 利用 Linux 核心漏洞逃逸。 |
| **沙盒編排與調度** | **Kubernetes (K8s)** 或 **Nomad** | 自動管理沙盒的生命週期，能輕鬆設定 Pod 的網路策略 (Network Policies) 與資源上限。 |
| **硬體橋接守門員** | 自研 **Python FastAPI** + **Celery** | 部署於連接著 EVK 的電腦上，提供安全的 RESTful API 供 AI 呼叫硬體操作。 |
| **日誌與狀態儲存** | **Redis** + **S3/MinIO** | 用於暫存沙盒內的編譯 Log 與最終生成的二進位檔，供不同 Agent 間傳遞。 |

## 四、 災難防禦機制 (Kill Switches)

1. **全局超時中斷 (Global Timeout Watchdog)：** 所有 Tier 1 與 Tier 2 的沙盒，預設生命週期不可超過 45 分鐘（可依編譯規模調整）。超時無條件 SIGKILL，防止 AI 無限期消耗算力。
2. **輸出長度截斷 (Output Truncation)：** 限制沙盒向 Tier 0 回傳日誌的最大字數 (如 10,000 Tokens)，若超過則透過 `rtk` (Rust Token Killer) 工具強制攔截並摘要，防止主控端 LLM 崩潰。