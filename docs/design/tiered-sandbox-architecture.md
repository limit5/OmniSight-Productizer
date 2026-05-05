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
* **環境屬性：** Docker + gVisor (`runsc`) user-space kernel；非 production / dev fallback 才允許 Docker default `runc`。
* **進駐角色：** C/C++ 開發 Agent、演算法重構 Agent、NPU 模型轉換 Agent。
* **安全機制：**
  * **實體斷網 (Air-gapped)：** 除特定白名單 (如 Git Server) 外，禁止對外連線，防範資料外洩。
  * **資源配額 (Cgroups)：** 限制最高 4 vCPU / 8GB RAM，防範 AI 寫出死迴圈或內存溢出 (OOM) 導致宿主機崩潰。
  * **用完即毀：** 每次任務結束立即銷毀 Container，確保下次編譯環境 100% 乾淨 (Reproducibility)。
* **主要職責：** `make` 交叉編譯、執行 Valgrind 檢測、跑 Python 數據分析腳本。

#### Phase U 落地狀態 (BP.W3.13, 2026-05-06)

Tier 1 的 production runtime 現在是 load-bearing gVisor，而不是
`SandboxCostWeight.gvisor_lightweight` 的 cost label。runtime selection 仍集中在
`backend/container.py`：

| Gate | Production 行為 | Dev / CI 行為 |
| :--- | :--- | :--- |
| Runtime preference | `ENV=production` 時必須解析為 `OMNISIGHT_DOCKER_RUNTIME=runsc`；若 operator 設成 `runc` 或未知值，sandbox launch hard-fail | 預設偏好 `runsc`，但可顯式設 `runc` 供 macOS / WSL2 / local Docker 相容 |
| Host capability | `docker info --format '{{json .Runtimes}}'` 必須列出 `runsc`；缺少 gVisor 時 hard-fail，不再 silent fallback | 缺少 `runsc` 時 fallback 到 `runc`，並 emit `sandbox_runtime_fallback` 方便 dev diagnosis |
| Request-time assertion | container 啟動後以 `docker inspect --format '{{.HostConfig.Runtime}}' <container>` 驗證實際 runtime 是 `runsc`；若不符立即 `docker rm -f` 並記 `omnisight_sandbox_launch_total{result="runtime_mismatch"}` | 不做 request-time hard assertion，避免 local Docker implementation 差異阻塞測試 |
| Observability | `omnisight_sandbox_launch_total{tier="t1",runtime="runsc",result="success"}` 是 production smoke 的必查 metric；audit row `sandbox_launched.after.runtime` 必須為 `runsc` | 相同 metric / audit label 仍存在，可看出 dev host 是否跑 `runc` |

Operator adoption 順序：

1. 每台 Tier 1 / Tier 2 sandbox host 安裝 gVisor：`runsc` 與
   `containerd-shim-runsc-v1` 必須出現在 `docker info` runtime list。
2. production `.env` 設 `ENV=production`，並保留預設
   `OMNISIGHT_DOCKER_RUNTIME=runsc`。
3. rolling restart backend；第一個 sandbox launch 需要同時通過
   `docker info` capability gate 與 `docker inspect` request-time assertion。
4. 跑 `scripts/benchmark_gvisor_runtime.sh` 產出 `runc` vs `runsc` CSV；
   若同一 workload 的 `runsc` 中位數超過 `runc` 2 倍，先回到 staging 調整
   workload / image / seccomp profile，不把 production row 翻
   `deployed-observed`。

此段關閉 Risk R12 的 runtime gap：production claim 可引用的是「每次 Tier 1
sandbox launch 的 Docker HostConfig.Runtime 已驗證為 `runsc`」，不是 cost
weight enum 名稱。

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

#### Phase T 落地狀態 (BP.W3.12, 2026-05-06)

Tier 3 的實作入口是 `tools/hardware_daemon/app.py`，作為部署在
EVK 連接機上的獨立 FastAPI daemon。它不是 OmniSight backend 的一般
router，也不給 Agent shell / SSH / arbitrary command surface；所有可執行
能力都被固定在三個 JSON-only endpoint：

| Endpoint | Action | 參數邊界 | 執行方式 |
| :--- | :--- | :--- | :--- |
| `POST /flash_board` | `flash_board` | `board_id`、`firmware_url` 或 artifact-root 內的 `artifact_path` | `OMNISIGHT_HW_BRIDGE_FLASH_CMD` argv |
| `POST /read_uart` | `read_uart` | `/dev/tty*` 或 `/dev/serial/by-id/*`、baud、duration、max bytes | `OMNISIGHT_HW_BRIDGE_UART_CMD` argv |
| `POST /capture_signal` | `capture_signal` | `bus ∈ {i2c, spi, gpio}`、channel、duration、sample rate | `OMNISIGHT_HW_BRIDGE_SIGNAL_CMD` argv |

共同約束：

* POST 必須是 `Content-Type: application/json`；非 JSON request 在 body
  parsing 前回 `415`。
* Pydantic model 使用 `extra="forbid"`，未知欄位不會被悄悄傳進硬體層。
* 子程序一律走 `asyncio.create_subprocess_exec(*argv)`，不走 shell
  interpolation；daemon 只把高階參數轉成白名單命令 argv。
* `OMNISIGHT_HW_BRIDGE_TOKEN` 若有設定，caller 必須帶
  `X-Omnisight-Bridge-Token`；mTLS / LAN ACL 仍由部署層負責。
* systemd 範本在 `deploy/systemd/omnisight-hardware-bridge.service`，預設
  綁定 `127.0.0.1:8765`，由反向 proxy / VPN / mTLS sidecar 決定是否暴露
  給 Tier 0 控制面。

這補上 Risk R13 的最小 runtime surface：自動化燒錄 / UART / 訊號擷取可走
Tier 3 RPC，不再需要把 Agent 放進 EVK host。

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
