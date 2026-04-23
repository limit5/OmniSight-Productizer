# OmniSight 企業級多智能體軟體工廠架構藍圖 (Enterprise-level multi-agent factory architecture Blueprint)

## 🎯 核心願景 (Core Vision)
打造超越單一智能體限制的「全自動化軟體兵工廠」。核心精神為：**樣板驅動通訊、技能庫限制動作空間、動態尺度路由、碎形拓撲、認知負載拆解、GraphRAG、武裝合約與物理沙盒**。

---

## 壹、 結構化通訊與技能註冊中心 (Structured Communication & Skill Registry) 

在 OmniSight 架構中，**「人類看 Markdown，機器讀 JSON」** 是最高鐵律。為了徹底消滅 Agent 之間的溝通飄移 (Drift) 與工作幻覺，系統導入了基於 Pydantic/Zod 的強型別約束，並透過 Function Calling (函數呼叫) 限制 AI 的動作空間。本模組構成了軟體工廠中無懈可擊的「中樞神經系統」。

### 1.1 零自由度的結構化通訊協定 (Zero-DOF Template-Driven Protocol)

大語言模型是機率模型，給定空白畫布就會產生幻覺。因此，嚴禁 Agent 之間使用自由格式的純文字進行交接。所有狀態機傳遞，必須嚴格遵守預先定義的 JSON Schema。這種「填空題」機制能強制鎖定大語言模型的注意力機制 (Attention Mechanism)，防止任何邊界條件被遺漏。

系統定義了四大核心交接樣板，FastAPI 負責嚴格檢驗其欄位完整性：

* **A. 規格合約樣板 (Specification Template) [由 Opus 4.7 產出]**
  做為專案的《憲法》，規範系統邊界與硬約束。
  * `system_boundaries`: (陣列) 定義該模組不得越權操作的系統資源（如：禁止存取網路、禁止寫入 `/root`）。
  * `hardware_constraints`: (陣列) 極端環境限制（如：`"MAX_RAM_128MB"`, `"NO_MALLOC_IN_MAIN_LOOP"`, `"USE_I2S_DIRECT_NODE"`）。
  * `api_idl_schema`: (字串) 強型別的介面定義 (OpenAPI 3.0 / Protobuf / C++ Headers)。
  * `bdd_executable_specs`: (字串) Gherkin 格式的測試場景，作為最終驗收標準。
  * `edge_cases_handled`: (陣列) 強制要求列出至少 3 個極端狀況的處理邏輯（如：斷網、I2C Timeout）。

* **B. 任務執行樣板 (Task Dispatch Template) [由 PM Sonnet 4.6 產出]**
  做為發包給打工仔的《派工單》，控制粒度與防混淆。
  * `target_triple`: (字串) 跨平台編譯的硬性目標 (例如 `x86_64-pc-linux-gnu` 或 `arm-fhva12c-linux`)。
  * `allowed_dependencies`: (陣列) 嚴格限制 Coder 只能讀取這幾個合約檔，禁止越權搜尋全域 Repo。
  * `max_cognitive_load_tokens`: (整數) 預期輸出的代碼上限，超過此值代表切分粒度失敗，需退回 PM 重新拆解。

* **C. 實作交付樣板 (Implementation Template) [由 Coder Guild 產出]**
  做為打工仔回報的《完工單》，禁止任何廢話。
  * `source_code_payload`: (字串) 純代碼內容。
  * `compiled_exit_code`: (整數) 呼叫沙盒編譯技能後的回傳值（必須為 0）。
  * `time_complexity`: (字串) Big-O 時間複雜度宣告。

* **D. 審查稽核樣板 (Review Audit Template) [由 Auditor Opus 4.7 產出]**
  做為醫療級合規的《驗證報告》，擁有一票否決權。
  * `is_medically_compliant`: (布林值) 是否符合 IEC 62304 / MISRA C 規範。
  * `cyclomatic_complexity_score`: (整數) 循環複雜度分數，超過系統設定門檻即強制退件。
  * `critical_vulnerabilities`: (陣列) 記錄致命漏洞。若陣列非空，觸發最高級別警報阻擋 Gerrit 合併。

---

### 1.2 領域專屬技能註冊中心 (Domain-Specific Skill Registry)

剝奪 Agent 直接生成 `bash` 腳本或自主作業系統指令的權力（避免 rm -rf 災難）。系統依據「公會 (Guild)」賦予專屬的 Function Calling 技能庫（瑞士刀）。Agent 的任務不再是「寫出動作」，而是「決定呼叫哪個技能與傳入什麼參數」。

* **【指揮層：架構師與 PM 的技能庫】**
  * `query_graph_rag(entity, depth)`: 檢索企業知識圖譜，獲取精準的硬體限制與架構關聯。
  * `evaluate_rfc_impact(change_request)`: 計算中途變更對現有 DAG 依賴樹的衝擊範圍與 Token 成本估算。
* **【系統層：*SA/SD/UX 的技能庫】**
  * `validate_database_normalization()`: 
  * `simulate_user_journey()`: 
  * `generate_openapi_swagger()`: 
* **【測試層：TDD QA 的技能庫】**
  * `generate_mock_server(idl_schema)`: 根據 API 合約，動態啟動帶有假資料的 Mock 伺服器供下游測試。
  * `run_pytest_sandbox(test_file)`: 在隔離環境中執行測試，並強制返回 JSON 格式的 Assert 結果。
* **【實作層：Coder 打工仔的技能庫】**
  * `compile_in_target_sandbox(source_code, target_triple)`: 將代碼丟入對應架構的 Docker (如 ARM Cortex-A7 環境) 進行編譯，獲取 Exit Code 與編譯日誌。
  * *(安全限制：Coder 無權呼叫任何修改全域環境或修改測試檔的技能。)*
* **【審計層：審查員與紅隊的技能庫】**
  * `scan_misra_c(source_code)`: 執行符合車載/軍規/醫療級的靜態語法與記憶體安全檢查。
  * `run_valgrind_analyze(executable)`: 執行記憶體洩漏與指標越界掃描。
  * `fuzz_api_endpoint(url, payload_schema)`: (Grok 4.2 專屬) 針對邊界值進行破壞性 Fuzzing 測試。

---

### 1.3 狀態機驗證與防呆攔截 (Validation & Intercept State Machine)

FastAPI 後端作為這套協定的「最高法院」，執行無情的機器驗證，構成不可突破的防禦網：

1. **Schema 校驗攔截 (Format Enforcer)：**
   當 Agent 試圖交接工作時，FastAPI 使用 Pydantic 驗證其輸出的 JSON。只要漏掉一個必填欄位（例如 `edge_cases_handled` 為空），FastAPI 拒絕接收，直接拋出 `ValidationError`。
2. **技能回傳驗證 (Execution Audit)：**
   Agent 不能單憑文字宣稱「我檢查過代碼沒有 Memory Leak」。FastAPI 會稽核該 Agent 的 Session Log，確認它是否真的成功呼叫了 `run_valgrind_analyze()` 技能，且提取了正確的回傳值。未經技能驗證的報告一律作廢。
3. **認知懲罰觸發 (Cognitive Penalty Loop)：**
   驗證失敗的 JSON 與編譯器報錯日誌不會進入下一關，而是被 FastAPI 攔截，轉化為帶有強烈系統語氣的 Prompt 退回給該 Agent（例如：`[FATAL] Compilation failed for arm64. See logs: ... Resolve immediately.`）。這強迫 Agent 進入自我修正 (Self-Correction) 迴圈，從根本上杜絕「假裝完成」的工作幻覺。

---

## 貳、 全尺度動態調度與碎形拓撲 (Scale-Adaptive & Fractal Topology)

為了兼顧「極致的開發效率」與「絕對的管理掌控力」，系統堅持 **「JIRA 絕對獨裁 (JIRA as SSOT)」**。無論任務大小，絕對禁止繞過 JIRA 進行任何「影子開發」。系統的彈性體現在：**透過動態調整 JIRA 的「任務層級 (Ticket Hierarchy)」、「依賴深度 (DAG Depth)」與「審核閘口 (Approval Gates)」，來匹配不同規模的管理節奏。**

### 2.1 S 級規模 (熱修復 / 微模組) —— 輕量化單軌管線 (Lightweight Single-Track)
當系統判定為 S 級任務（例如：修正 I2C Timeout Bug、微調 UI 按鈕顏色），流程追求「天下武功，唯快不破」，但絕對留下完整的稽核軌跡。
(熱修復)：Fast-Track 管線。** 建立單一 JIRA 票，`[Tech Lead]` 帶領 `[Coder]` 本地 TDD 迴圈。

* **JIRA 佈署策略 (扁平化)：**
  * PM Agent 不會建立龐大的 Epic，而是直接在 JIRA 建立單一張獨立的 `[Task]` 或 `[Bug]` 工單。
  * **節奏掌控：** 關閉複雜的 DAG 依賴檢查，跳過 CCB (變更控制委員會) 的前置審查。
* **拓撲編制：**
  * 喚醒極簡編制：`[PM Agent]` -> `[Tech Lead]` -> `[Coder Guild]`。
* **管線運作：**
  * PM Agent 建立 JIRA 票後，直接發包給 Coder Agent 於本地 Git 分支進行實作與測試。
  * 實作完成後推送 Gerrit，並將 JIRA 狀態自動推播為 `[In Review]`。人類主管只需看單一狀態的流轉，保持最輕快但清晰的管理心跳。

### 2.2 M 級規模 (獨立系統 / 標準模組) —— 標準 DAG 任務樹 (Standard Pipeline)
當開發一個完整但邊界清晰的系統（例如：醫療 Kiosk 終端應用程式），流程進入標準軟體兵工廠的節奏，確保每一個環節的品質與防呆。
(獨立系統)：標準管線。** 啟動完整兵工廠編隊 (架構師 -> SA/UX -> PM -> QA -> Coder)。

* **JIRA 佈署策略 (結構化)：**
  * 採用標準 WBS (工作分解結構)。PM Agent 嚴格建立 `[Epic]` -> `[Story]` -> `[Task]` 的三層架構。
  * **節奏掌控：** 強制引入 **DAG (有向無環圖) 依賴鎖定**。在 JIRA 中，下游任務會被自動標記為 `[Blocked by]` 上游任務。只有當上游任務（如：API 合約制定）在 Gerrit 獲得 `+2 Submit` 且 CI 亮綠燈後，下游的 Coder Agent 才會被 FastAPI 喚醒。
* **拓撲編制：**
  * 啟動完整兵工廠編隊：包含架構師、PM、TDD QA、Coder、Integration 與 Auditor。
* **管線運作：**
  * 嚴格執行 TDD 雙提交依賴鏈（測試 Patchset 先行，實作 Patchset 後上）。CCB Agent 會嚴格審核任何中途的 JIRA Scope Creep (範圍潛變)。

### 2.3 XL 級規模 (完整生態鏈) —— 碎形矩陣與聯動 (Fractal Matrix)
當面對橫跨多個領域的大型生態系（例如：Kiosk 硬體 + 雲端資料庫 + 醫院 HIS 系統對接），單一的 PM Agent 會因為認知超載 (Cognitive Overload) 而崩潰，單一的 JIRA Board 也會變成人類無法閱讀的災難。
(生態鏈)：碎形矩陣。** Opus 架構師實例化平行領域團隊 (Web/Firmware/Cloud)。透過 JIRA Cross-Project 連結同步節奏，團隊間僅能透過 API Mock 聯動。

* **JIRA 佈署策略 (聯動矩陣)：**
  * 採用 **System of Systems (系統的系統)** 級別的專案管理。
  * **全局看板 (Global Portfolio Board)：** 由 Opus (全局架構師) 掌控，只管理 `[Initiative]` 與跨子系統的 `[Epic]`。
  * **領域看板 (Domain Project Boards)：** 分裂出多個獨立的 JIRA Project (如 `WEB-xxx`, `FW-xxx`)，由各領域的 PM Agent 管理底層的 Story 與 Task。
  * **節奏掌控：** 透過 JIRA 的 **Cross-Project Issue Linking (跨專案依賴連結)** 進行節奏同步。如果 Cloud 團隊的 API `[Epic]` 發生延遲，Firmware 團隊對應的 `[Story]` 會自動亮起紅燈警報。
* **拓撲編制 (碎形展開)：**
  * `[全局架構師 Opus]` 實例化平行的子團隊：`[Firmware 領域架構師+PM+公會]`、`[Cloud 領域架構師+PM+公會]`。
* **管線運作 (微服務合約驅動)：**
  * 子團隊之間**嚴禁互相讀取代碼 (No Cross-Repo Read Access)**。
  * 節奏解耦的終極武器：FastAPI 根據全局架構師制定的 IDL，為每個子團隊啟動 **全天候運作的 Mock Server**。Firmware 團隊不需要等 Cloud 團隊寫完 Code，只要對著 Mock Server 開發，雙方的 JIRA 任務就能平行推進，將整體開發週期壓縮到極致。

---

## 參、 基礎設施與異質模型編隊 (Infrastructure & Heterogeneous Model Fleet) [全生態鏈矩陣版]

OmniSight 放棄了「單一模型打天下」與「全域工具包」的低效做法，採用 **FastAPI (控制面) + LangChain/Tool Calling (執行面)** 的混合基礎設施。系統首創 **「任務驅動的動態技能掛載引擎 (Task-Driven Dynamic Skill Injection)」**：FastAPI 會根據任務屬性，喚醒具備對應基因的大語言模型，並嚴格只注入該任務所需的專屬技能庫 (Skills/Tools)，實現極致的動作空間限制。

### 3.1 核心指揮與控制面 (Command & Control Plane)
大腦層級的模型不負責寫實作代碼，他們負責降維打擊、制定法規與分配資源。

* **【全局/領域架構師兼 CCB】 - `Claude Opus 4.7`**
  * **基因優勢：** 具備業界最強的複雜邏輯推理與長文本指令服從能力。
  * **動態技能包：** `query_graph_rag()`, `evaluate_rfc_impact()`, `generate_bdd_specs()`。
* **系統分析與設計師 (SA/SD Agent) - `Claude Sonnet 4.6`**
  * **職責：** 承接架構藍圖，將高階邏輯轉譯為具體的資料庫綱要 (Database Schema)、精確的 API Payload 結構與循序圖。確保資料庫的正規化與系統間的資料流正確。
* **使用者體驗設計師 (UX Agent) - `Gemini 3.1 Pro`**
  * **基因優勢：** 強大的多模態與空間邏輯理解能力。
  * **職責：** 規劃 User Journey (使用者旅程)。負責定義各個畫面的狀態機 (State Machine)，包含 Loading、Error、Offline 狀態，並制定符合醫療級無障礙 (WCAG) 的 UI 規範。
* **【專案總管與領航員 (PM Agent)】 - `Claude Sonnet 4.6`**
  * **基因優勢：** 速度與智商的完美平衡，極擅長處理 JSON 結構與 DAG 拓撲排序。
  * **動態技能包：** `scan_cognitive_load()`, `dispatch_jira_ticket()`。
* **【翻譯與路由網關】 - `Claude Haiku 4.5` 或 本地 `Gemma 4`**
  * **動態技能包：** `route_pipeline_scale()`。

### 3.2 任務驅動的領域公會與執行面 (Domain Guilds & Task-Based Execution Plane) [全矩陣掛載引擎]
這是系統的「肌肉」。打工仔不再是泛用的 Coder。當 FastAPI 從 JIRA 抓取任務準備喚醒 Agent 時，會讀取票上的 `Domain_Tag`，並將 Agent 實例化為特定的「公會成員」，同時 **只注入該公會的專屬武器庫**。

#### 🔹 第一層：底層系統與驅動公會 (BSP, OS & Firmware)
負責讓矽晶片甦醒，直接與實體硬體交火。

* **【底層 BSP 與 OS 核心公會 (BSP & System Worker)】**
  * **觸發條件：** 任務標籤包含 `[BSP]`, `[Kernel]`, `[U-Boot]`, `[Device_Tree]`, `[System]`。
  * **專屬技能包 (BSP Loadout)：**
    * `validate_device_tree_syntax()`: 驗證 DTS/DTB 設備樹語法，避免硬體資源衝突。
    * `compile_custom_kernel()`: 編譯 Linux Kernel 或 RTOS 映像檔。
    * `analyze_memory_map()`: 分析實體記憶體映射 (Memory Map) 與中斷請求 (IRQ) 配置。
* **【嵌入式韌體與 HAL 公會 (Firmware & HAL Worker)】**
  * **觸發條件：** 任務標籤包含 `[FW]`, `[Driver]`, `[HAL]`, `[I2C/SPI]`。
  * **專屬技能包 (Firmware Loadout)：** `read_datasheet_rag()`, `compile_cross_platform()`, `check_memory_alignment()`。

#### 🔹 第二層：高階影像、聲學與演算法特種公會 (Multimedia & Math)
處理高度複雜的數學運算與多媒體訊號。

* **【演算法與幾何視覺公會 (Algorithm & CV Worker)】**
  * **專屬技能包：** `validate_matrix_operations()`, `simulate_parallax_triangulation()` (視差三角測量模擬)。
* **【光學工程公會 (Optical Engineering Worker)】**
  * **專屬技能包：** `calculate_lens_shading_profile()` (暗角補償), `simulate_led_gpio_pwm()` (補光燈時序)。
* **【影像畫質調校公會 (Image Quality / ISP Worker)】**
  * **專屬技能包：** `tune_ae_feedback_loop()` (自動曝光迴圈), `analyze_y_plane_brightness()` (亮度容錯)。
* **【聲學與音訊底層公會 (Audio & Acoustics Worker)】**
  * **專屬技能包：** `validate_direct_i2s_write()` (I2S 直寫測試), `analyze_audio_buffer_underrun()`。

#### 🔹 第三層：應用、雲端與基礎設施公會 (App, Backend & Infra)
負責使用者互動與無遠弗屆的雲端生態網路。

* **【前端與 GUI 公會 (Frontend & GUI Worker)】**
  * **觸發條件：** 任務標籤包含 `[UI]`, `[Web]`, `[Kiosk_Screen]`, `[MFC]`, `[WPF]`, `[React]`, `[Vue]`, `[Qt]`。
  * **專屬技能包 (UI Loadout)：** `build_frontend_assets()`, `mock_api_response()`。
* **【雲端後端與微服務公會 (Backend & Cloud Services Worker)】**
  * **觸發條件：** 任務標籤包含 `[Backend]`, `[API]`, `[Microservice]`, `[DB]`。
  * **專屬技能包 (Backend Loadout)：**
    * `validate_grpc_protobuf()`: 驗證 gRPC/REST 通訊協定與 Payload 結構。
    * `run_db_migration_sandbox()`: 在沙盒中執行資料庫 Schema 遷移 (Migration) 測試。
    * `simulate_high_concurrency()`: 驗證 API 在高併發狀況下的死鎖 (Deadlock) 與回應延遲。
* **【基礎設施與 SRE 公會 (Infrastructure & SRE Worker)】**
  * **觸發條件：** 任務標籤包含 `[Infra]`, `[K8s]`, `[Terraform]`, `[CI_CD]`。
  * **專屬技能包 (Infra Loadout)：**
    * `validate_terraform_plan()`: 執行 IaC (基礎設施即代碼) 的預覽規劃檢查。
    * `deploy_to_k8s_ephemeral_cluster()`: 部署至臨時 Kubernetes 叢集進行端到端連通性驗證。
    * `scan_iac_security_misconfig()`: 掃描 Dockerfile 與 YAML 設定檔中的權限過當或資安漏洞。

#### 🔹 第四層：獨立品管與系統集成公會 (QA & Integration)
* **【獨立品管與集成公會 (QA & Integration)】**
  * **專屬技能包：** `generate_mock_server()`, `execute_sandbox_pytest()`, `generate_glue_code()`。

### 3.3 零容忍武裝審計與守護層 (Armed Audit & Guardian Plane)

這層是確保系統不會產出「災難性代碼」的最高法院。系統會依據專案 `Spec Template` 中宣告的產業標準，為審計員動態掛載對應的「合規矩陣 (Compliance Matrix)」，擁有一票否決權 (`Verified -1`)。

* **【安全與跨域合規審計員 (Security & Compliance Auditor)】 - `Claude Opus 4.7`**
  * **職責：** 獨立於開發團隊之外，負責審閱 PR、覆蓋率報告與架構依賴。絕不妥協，任何違反該領域合規標準的代碼將被直接物理阻擋。
  * **動態合規矩陣與技能掛載 (Dynamic Compliance Loadouts)：**
    FastAPI 會根據專案的 `[Industry_Tag]`，為 Auditor 載入截然不同的法則與技能：

    * **🏥 醫療規 (Medical Device Loadout)**
      * **遵循標準：** FDA Class I/II/III、IEC 62304 (醫療軟體生命週期)、ISO 13485、HIPAA (資料隱私)。
      * **審查重點：** 絕對的可追溯性 (Traceability)。每一行代碼都必須能追溯回 BDD 規格；嚴格審查 PII (個人身分資訊) 的加密狀態；禁止「Dead Code (未使用的死代碼)」存在。
      * **專屬技能：** `audit_iec62304_traceability()`, `scan_phi_data_leakage()`。

    * **🚗 車載規 (Automotive Loadout)**
      * **遵循標準：** ISO 26262 (道路車輛功能安全 - ASIL 等級)、MISRA C/C++ 2012、AUTOSAR 規範。
      * **審查重點：** 記憶體與指標的絕對安全。嚴格執行 MISRA 規則（如：禁止使用動態記憶體分配 `malloc`/`free`、禁止使用 `goto`、強制限制指標的轉型與層級）。
      * **專屬技能：** `scan_misra_c_strict()`, `verify_asil_d_redundancy()` (驗證關鍵路徑的硬體/軟體冗餘設計)。

    * **🏭 工控規 (Industrial / ICS Loadout)**
      * **遵循標準：** IEC 61508 (電氣/電子/可程式化電子安全系統)、SIL (安全完整性等級)。
      * **審查重點：** 系統高可用性與容錯復原力 (Fault Tolerance)。審查硬體 Watchdog (看門狗) 的觸發機制、狀態機的非法狀態攔截、以及 I/O 介面的電氣隔離邏輯防呆。
      * **專屬技能：** `analyze_state_machine_deadlocks()`, `verify_watchdog_pet_timing()`。

    * **🪖 軍規與航太 (Military & Aerospace Loadout)**
      * **遵循標準：** DO-178C (航空載具軟體認證)、MIL-STD-882E。
      * **審查重點：** 追求數學級別的極致確定性。要求 MC/DC (修改條件/判定覆蓋率) 達到 100%；審查抗輻射翻轉 (Single Event Upset) 的記憶體 ECC 校驗邏輯；要求全靜態分配與極端的循環複雜度限制。
      * **專屬技能：** `verify_mcdc_100_percent()`, `run_formal_verification_proof()` (執行形式化驗證數學證明)。

* **【紅隊駭客與底層突破者 (Red Team)】 - 🚀 `Grok 4.2`**
  * **動態技能包：** `fuzz_api_endpoint(url, schema)`, `inject_fault_to_kernel(module)`。負責以攻擊者視角驗證上述合規防線是否能被突破。

* **【海量日誌分析員 (Context Absorber)】 - `Gemini 3.1 Pro`**
  * **動態技能包：** `analyze_massive_crash_dump()`。

* **【SecOps 威脅情報官 (Threat Intelligence Agent)】 - `Gemini 3.1 Pro` / `Claude Sonnet 4.6`**
  * **基因優勢：** 具備強大的聯網搜尋 (Web Search) 與即時資料庫爬取能力。
  * **職責：** 專職的「雷達」。它不寫代碼，只負責在每次專案建構或依賴更新時，即時爬取 CVE 資料庫、GitHub Advisories 與 NVD (國家弱點資料庫)。
  * **動態技能包 (Intel Loadout)：**
    * `search_latest_cve(package_name, version)`: 查詢特定套件是否有昨天剛爆發的漏洞。
    * `query_zero_day_feeds()`: 掃描資安社群即時情報。
    * `fetch_latest_best_practices(framework)`: 查詢官方文件最新版本的 API 棄用 (Deprecation) 警告與最佳做法。
  * **聯動機制：** 獲取情報後，立刻將最新的 Payload 或漏洞特徵整理成 JSON 樣板，**動態餵給【安全與合規審計員】與【紅隊駭客】**，讓防禦團隊擁有最新的「疫苗」來審查代碼。

### 3.4 基礎設施底層支撐 (Underlying Infrastructure Mechanics)
支撐上述 Agent 及其技能運作的實體環境：
1. **沙盒即服務 (Sandbox-as-a-Service)：** Agent 呼叫的 `compile` 技能，實際上是透過 FastAPI 與 Kubernetes 聯動，即時拉起帶有指定架構 (x64/ARM/K8s Cluster) 的 Ephemeral Docker (臨時容器)，確保技能執行的絕對隔離與無狀態 (Stateless)。
2. **圖譜與記憶體庫 (Graph & State Storage)：** 使用 Neo4j 支撐 `query_graph_rag` 技能；使用 Redis 儲存 Agent 執行技能的上下文狀態，確保中斷可恢復。

---

## 肆、 核心嚴謹度、防混淆與武裝防線 (Core Rigor & Armed Defenses)

在企業級與醫療級軟體工程中，「防禦性設計」比「生成代碼」更重要。本章節定義了 OmniSight 如何透過物理沙盒、量化指標與圖譜檢索，徹底封殺大語言模型的「幻覺 (Hallucination)」、「目標染色 (Target Bleed)」與「架構飄移 (Architecture Drift)」。

### 4.1 GraphRAG 知識中樞與決定論約束 (GraphRAG & Deterministic Constraints)
傳統的向量檢索 (Vector RAG) 會將規格書切碎，導致 AI 拿到的是「缺乏系統邊界」的破碎脈絡。OmniSight 導入 GraphRAG 與約束引擎，確保架構的嚴謹性。

* **圖譜化關聯 (Ontology Mapping)：** 系統將公司的硬體限制、過往架構慣例建立為實體 (Entities) 與關係 (Relationships) 的網路。例如：`[I2C 驅動]` -> *(Requires)* -> `[獨立硬體 Timer]` -> *(Conflicts With)* -> `[Qt UI 事件迴圈]`。當 Agent 企圖在 Qt 迴圈中呼叫 I2C 時，檢索會沿著關係鏈觸發「衝突警報」，直接在 Prompt 階段攔截錯誤。
* **決定論約束引擎 (OPA / Linter Rules)：** AI 是機率模型，不能依賴 AI 的「自律」來遵守規則。系統將醫療級硬約束（如：`NO_MALLOC_IN_MAIN_LOOP`）寫成 CI 管線中的靜態檢查腳本。規則是 100% 決定論的，AI 只要踩線就必定退件。

### 4.2 認知負載驅動拆解 (Cognitive Load-Driven Decomposition)
人類習慣按「功能」切分任務，但這會導致 AI 大腦當機（給太少 Context 會亂猜，給太多 Context 會遺忘）。PM Agent 採用「量化數據」動態調節任務粒度，追求品質與預算的雙重極致：

* **防護 1：依賴邊界法則 (Fan-in / Fan-out Limit)** 若評估單一任務需要讀取超過 **3 個外部介面合約**，判定為「過載危險區」。PM Agent 會強制將任務往下分裂 (Recursive Splitting)，例如拆分為「資料存取層」與「業務邏輯層」。
* **防護 2：Mock 斷點極限 (Mock Limits)**
  若 TDD 階段發現需要建立超過 **2 個 Mock 外部對象** 才能完成單元測試，代表耦合度過高。觸發架構重構警告，要求進一步解耦。
* **防護 3：經濟效益聚合 (Upward Aggregation)**
  若預期輸出小於 100 Tokens（例如單一 Helper Function），卻要消耗 5000 Tokens 的系統 Prompt 成本，判定為「經濟效益極差」。PM Agent 會自動將多個微小任務打包為 `[Batched Task]` 批次交接給單一 Coder。
* **黃金輸出區間：** 透過上述計算，系統將每張 JIRA 票的實作產出，穩定控制在 **300~800 Tokens** 的大語言模型最佳認知區間內。

### 4.3 跨平台防混淆隔離 (Target Isolation & Anti-Bleed)
當系統同時包含 `x64` PC 應用程式與 `arm64` 韌體時，Agent 極易發生「目標染色 (Target Bleed)」——看著 ARM 的 SDK，卻寫出含有 ARM 專屬指令的 x64 代碼。系統透過三道物理防線徹底阻絕此災難：

* **第一道：建構矩陣定義 (Build Matrix)**
  總架構師在專案初始，以 JSON 陣列精確定義每個子系統的 `Target_Triple`（編譯目標三元組，如 `x86_64-pc-linux-gnu`）。
* **第二道：硬性標籤注入 (Hard Tag Injection)**
  FastAPI 攔截發給 Agent 的任務，強制在 System Prompt 最頂端注入最高優先級的硬限制：`[CRITICAL: 此模組運行於 x64 PC，絕對禁止使用任何 ARM NEON 指令集，違者嚴懲]`。
* **第三道：物理沙盒阻斷 (Ephemeral Docker Sandbox) [終極防線]**
  不信任 Agent 的任何宣稱。FastAPI 根據 Target 動態啟動「純 x64」或「純 ARM 交叉編譯」的 Docker 容器。如果 Agent 發生幻覺混用了架構指令，底層 GCC/Clang 編譯器會瞬間拋出 Fatal Error 並將其物理阻擋。

### 4.4 武裝合約與零容忍懲罰 (Armed Contracts & Zero-Tolerance Penalty)
沒有懲罰機制的合約只是一張廢紙。系統對無視合約或企圖蒙混過關的 Agent 實施「三級懲罰階梯」：

* **第一級：物理斬殺 (CI Hard Rejection)**
  只要未通過 TDD 測試、合約 Mock 驗證失敗、或 Schema 格式錯誤，Git Hook / Jenkins 會直接返回 `Exit Code 1`。在 Gerrit 上自動標記 `Verified -1`，退回 PR，禁止任何人為合併。
* **第二級：認知懲罰與強迫反思 (Cognitive Penalty & Forced Reflection)**
  FastAPI 會將 CI 的報錯日誌（如 `TypeError: Expected Int, got String`）攔截，轉化為帶有強烈警告語氣的 Prompt 重新餵給 Agent。強迫 Agent 在下一次輸出代碼前，先輸出一段 JSON 解釋：「*我錯在哪裡？我將如何修正？*」以此打破 AI 盲目重試的迴圈。
* **第三級：紅牌熔斷 (Circuit Breaker / Red Card)**
  若某個 Agent 在同一張 JIRA 票上連續 3 次獲得 `Verified -1`，系統判定該 Agent 陷入「幻覺死結」。FastAPI 將立即切斷該 Agent 的 API 呼叫權限以止損，並將票券標記為 `[BLOCKED]`，升級交由 Opus 4.7 重新檢視合約，或由 Grok 4.2 進行紅隊除錯。

### 4.5 零日威脅情報與動態知識注入 (Zero-Day Intel & Dynamic Knowledge Injection)
大語言模型的靜態權重無法防禦未來的漏洞。系統摒棄了低效的「定期模型微調」，改採「即時檢索增強 (Real-Time RAG & Web Tooling)」機制：

* **依賴套件即時阻斷 (Just-In-Time Dependency Blocking)：**
  在 Integration Engineer 決定引入任何第三方開源套件 (如 `npm install` 或 `apt-get`) 之前，必須先觸發 **SecOps 情報官** 進行聯網掃描。若發現該套件存在 72 小時內爆發的未修補 CVE 漏洞，情報官將強制覆決引入，並要求架構師尋找替代方案。
* **研發與最佳實踐雷達 (R&D Technology Radar)：**
  當 Opus 總架構師在規劃藍圖時，若偵測到關鍵字（如：最新的 Qt 6.8 或即將發布的 Linux Kernel 模組），系統會自動呼叫情報官執行 `fetch_latest_best_practices()`。情報官會爬取官方最新文檔，將「剛出爐的寫法」轉換為硬性規則寫入 `Task Template`，確保底層 Coder Agent 不會寫出已經被官方宣告棄用的舊世代代碼。

---

## 伍、 系統運作標準生命週期 (OmniSight SDLC SOP - 決定論狀態機版)

OmniSight 的 SDLC 是一套由 FastAPI 全程監控的決定論狀態機。系統的推進不依賴 Agent 的自由意志，而是嚴格依賴「結構化樣板 (JSON Schema)」的交接與「閘口 (Gates)」的驗證。全程在 Gerrit Server 留下符合醫療/車載稽核標準的絕對追溯軌跡。

### 第 0 階段：需求攔截與動態路由 (Intercept & Routing)
系統的起點，負責將人類的混沌意圖轉化為機器的秩序。

1. **意圖降維與 T-Shirt 規模評估：** 人類客戶輸入模糊的 CJK (繁中) 需求。最前端的 Gateway Agent (Haiku/Gemma) 毫秒級將其翻譯為精確的英文意圖，並執行規模評估，決定專案應走 S 級 (Fast-Track)、M 級 (標準) 或 XL 級 (碎形矩陣) 管線。
2. **AI 驅動骨架準備 (Scaffolding)：** 路由確定後，FastAPI 不會讓 AI 從零開始。它自動呼叫環境技能，拉取公司標準的 CI/CD 樣板、目錄結構與 Dockerfile Boilerplate，為接下來的 Agent 備妥無菌的開發手術室。

### 第 1 階段：需求除偏與架構藍圖 (De-biasing & Architecture Blueprint)
確立系統的《憲法》，消滅大語言模型最致命的方向性幻覺。

3. **GraphRAG 檢索與隱含假設驗證：** Opus 4.7 (總架構師) 呼叫 `query_graph_rag()` 技能，檢索企業內部的硬體限制與過往災難紀錄。系統強制表列出所有「隱含假設 (Hidden Assumptions)」，交由人類在畫面上點擊核取/修正。
4. **高階合約生成：** 假設收斂後，Opus 4.7 產出不可變的 `[Spec Template]`。內含系統邊界、API IDL (OpenAPI/Protobuf)、跨平台建構矩陣 (Target Triple)，以及 Gherkin 格式的 BDD 驗收規格。

### 第 2 階段：系統分析與體驗設計 (System Analysis & UX Design)
將高階骨架填入血肉，確保商業邏輯與人機互動的精確性。

5. **資料庫與 API 展開 (SA/SD)：** SA Agent 承接高階藍圖，進行精確的資料建模。產出 `[Data Schema Template]`，包含符合正規化的關聯庫結構、API Payload 細節與循序圖。
6. **狀態機與無障礙設計 (UX/UI)：** UX Agent 針對前端需求，產出 `[UX/UI State Template]`。嚴格定義每一個畫面的 Loading / Error / Offline 狀態機，並確保符合 WCAG 無障礙醫療標準。
7. **🔒 人類一鍵核准閘口 (Sign-off Gate)：** 系統將生硬的 JSON 轉化為可視化的 Mermaid 流程圖與 ER Diagram。人類主管確認業務邏輯無誤後點擊放行，藍圖正式凍結。

### 第 3 階段：認知負載拆解與 TDD 雙軌開發 (Cognitive Load & TDD Execution)
進入兵工廠核心，以數學最佳化驅動的分工實作。

8. **最佳化拆解與 JIRA 佈署：** PM Agent 執行「認知負載掃描」。若單一任務依賴 > 3 檔或 Mock > 2 個，觸發「遞迴拆解」；若產出 < 100 Tokens 則「向上聚合」。最終生成最佳粒度的 DAG 任務樹，並為每張票注入 `Target_Triple` 標籤與發包給對應公會。
9. **TDD 測試先行 (Test-First Push)：** QA Agent 啟動，讀取 BDD 規格與 API 合約，呼叫技能自動生成 Mock Server 並撰寫嚴酷的單元測試。推送到 **Gerrit (Patchset A: 純測試)**。
10. **依賴實作與沙盒編譯 (Implementation)：** 特種公會打工仔 (如光學、韌體、Web) 載入專屬武器庫，下載 Patchset A 進行局部實作。完成後推送 **Gerrit (Patchset B: 純實作)**，並在 Commit 中強制宣告 `Depends-On: <Patchset A>`，形成物理鎖定的信任鏈。交接報告必須符合 `[Implementation Template]`。

### 第 4 階段：零容忍驗證與跨域審計 (Zero-Tolerance Verification & Audit)
機器對機器的無情審查，確保每一行代碼都能上戰場。

11. **物理沙盒 CI 驗證：** CI 系統讀取目標標籤，動態拉起專屬架構（如純 ARM 或純 x64）的 Docker 容器。執行測試與合約校驗。若觸發 Fatal Error，直接打回 `Verified -1`，並將錯誤日誌轉為「認知懲罰」逼迫 Agent 修正。
12. **情報掃描與合規審查：**
    * **SecOps 情報官：** 即時聯網掃描依賴套件是否存在 72 小時內的 Zero-day CVE 漏洞。
    * **Auditor 審計員：** 依據專案屬性載入醫療 (IEC 62304) 或車載 (MISRA) 合規矩陣，執行靜態分析。
    * **Red Team 紅隊：** 發動 Fuzzing 邊界攻擊。
    審查結果強制寫入 `[Review Template]`。
13. **系統整合黏合 (System Integration)：** 各模組皆亮綠燈後，Integration Engineer 撰寫 Glue Code (黏合代碼)，進行系統級的符號鏈接，提交最終整合 Patchset。

### 第 5 階段：人類決策、變更控制與文件自癒 (Finalization & Self-Healing)
人類收攏最終控制權，確保系統資產永不腐化。

14. **CCB 變更控制機制 (防禦 Scope Creep)：** 若專案中途人類插單，CCB Agent 立即凍結 DAG。執行影響力評估 (RFC) 後，作廢舊票並無損重構新票，避免系統死結。
15. **🔒 人類最終決策 (+2 Approval)：** 人類主管絕不陷入茫茫的代碼海。只審閱 Gerrit 上高度結構化的 AI 報告（覆蓋率、合規掃描、測試綠燈）。負責最終責任背書，手動給予 `Code-Review +2` 與 `Submit`。
16. **知識庫反向更新 (Self-Healing Docs)：** 代碼成功合併入 Master 後，Watchdog Agent 被喚醒。自動反向更新 Git 庫中的 Markdown 技術文件、架構圖與 API Swagger，維持 SSOT (單一真實來源) 的絕對一致。