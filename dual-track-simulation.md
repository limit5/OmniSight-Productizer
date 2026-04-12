# 嵌入式 AI 自動化開發與雙軌模擬驗證通用架構

本文件定義了在缺乏實體開發板與感測器的環境下，AI Agent 如何透過「硬體抽象層 (HAL)」與「雙軌模擬驗證機制」，獨立完成 C/C++ 嵌入式軟體（涵蓋核心運算演算法與底層硬體周邊控制）的開發、測試與驗證工作流。

---

## 一、 核心架構：硬體抽象與統一測試介面 (HAL & Test Runner)

為確保 AI Agent 寫出的程式碼既能在 WSL2 / PC 端模擬，又能順利交叉編譯至目標硬體架構（如 ARM 平台），系統採用**硬體抽象層 (Hardware Abstraction Layer, HAL)** 架構。所有的感測器輸入與硬體輸出，皆提供「實體 (Physical)」與「模擬 (Mock)」兩種實作。

### 統一測試總控腳本 (`simulate.sh`)
這是 AI Agent 與模擬環境互動的唯一入口。Agent 不需要了解複雜的編譯參數，只需呼叫 CLI 指令：
* `Usage:` `./simulate.sh --type=[algo|hw] --module=[name] --input=[data]`
* `Output:` 系統將回傳標準化的測試結果、效能指標 (CPU/耗時) 與記憶體檢測 (Valgrind) 報告。

---

## 二、 雙軌模擬機制設計 (Dual-Track Simulation)

### 軌道一：純軟體與演算法模擬 (Data-Driven Algorithm Simulation)
針對邊緣運算、訊號處理或資料解析等純運算邏輯，採用**資料驅動回放 (Data-Driven Replay)** 進行驗證。

* **運作邏輯：** 將感測器數據擷取模組 (`SensorProvider`) 抽象化。在模擬模式下，系統不呼叫底層硬體驅動，而是從版本控制庫的 `test_assets/` 資料夾讀取預先錄製的測試集（如感測器 Raw Data、包含極端雜訊的資料集）。
* **AI 驗證標準：**
  1. **準確度：** 演算法輸出的運算數值或推論結果必須與標準答案 (Ground Truth) 一致。
  2. **效能與穩定度：** 透過 `TARGET=x86_64` 編譯成 PC 執行檔，搭配 Valgrind 檢測是否有 Memory Leak 或指標越界。

### 軌道二：硬體周邊控制模擬 (Peripheral Mocking & QEMU)
針對 GPIO/PWM 訊號輸出、通訊介面 (I2C/SPI) 等與底層暫存器或 Linux 核心高度綁定的控制邏輯，採用**虛擬檔案系統與指令集虛擬機**進行驗證。

* **運作邏輯：**
  * **Mock OS 層：** 對於 Linux `sysfs` 的操作（如 `echo 1 > /sys/class/gpio/gpio10/value`），在 WSL2 環境下透過 Mock 函式庫攔截寫入請求，並將狀態重定向輸出至 `mock_gpio_status.log` 供 AI 讀取比對。
  * **指令集模擬 (QEMU-ARM 等)：** 針對涉及特定硬體架構記憶體對齊或特定函式庫特性的底層代碼，在 PC 端完成交叉編譯後，由腳本自動丟入 QEMU 虛擬環境中試跑，捕捉架構專屬的 Runtime Crash。
* **AI 驗證標準：**
  硬體狀態日誌 (Log) 必須與預期的時序或電平變化一致（例如：檢測 PWM 佔空比的數值變化是否符合預期）。

---

## 三、 整合後的 AI Agent 模擬工作流 (The Simulation Workflow)

當 AI Agent 領取到包含硬體或演算法開發的工單時，將遵循以下「開發 $\rightarrow$ 模擬 $\rightarrow$ 修正」的自動化閉環：

**Step 1: 任務分析與編譯環境切換**
* Agent 分析任務性質，修改對應的 C/C++ 原始碼與 Makefile/CMake 腳本。
* 確保程式碼引用了正確的 HAL 介面（例如：`#ifdef MOCK_ENV`）。

**Step 2: 呼叫模擬器進行驗證 (RTK 攔截壓縮)**
* **情境 A (演算法優化)：** Agent 執行 `./simulate.sh --type=algo --module=core_algorithm --input=test_assets/extreme_case_01.dat`。
* **情境 B (硬體周邊控制)：** Agent 執行 `./simulate.sh --type=hw --module=gpio_pwm --mock=true`。
* *註：此過程產生的大量編譯 Warning 與測試輸出，將由底層的 **RTK (Rust Token Killer)** 自動攔截並壓縮，確保 AI 不會因上下文溢出而失智。*

**Step 3: 判讀報告與迭代修正**
* Agent 讀取 `simulate.sh` 回傳的精簡版報告：
  * 若出現 `[Valgrind] Error: Memory leak detected` $\rightarrow$ Agent 自動回頭檢視 `malloc`/`free` 的配對邏輯。
  * 若出現 `[Mock] GPIO Pin 12 expected HIGH, got LOW` $\rightarrow$ Agent 修改硬體初始化時序。
* Agent 獨立在沙盒內進行多次迭代，直至模擬腳本回傳 `[Status] All Tests Passed`。

**Step 4: 交接與提交流程**
* 模擬全數通過後，Agent 產出 `HANDOFF.md`，內容必須包含**「已通過的模擬測試模組與數據」**。
* Agent 呼叫 Git 指令推送至 Code Review 伺服器建立 Patch Set，並呼叫 API 將狀態切換為 `In Review`，等待人類主管 (Human Maintainer) 進行最終的實機驗證 (Hardware Bring-up) 與程式碼審查。

---

## 四、 實務開發的防呆機制

為防止 AI 在模擬環境中產生幻覺或「為通過測試而寫死代碼 (Hardcoding)」，系統強制實施以下限制：

1. **唯讀測試集 (Read-only Test Assets)：** AI Agent 所在的沙盒環境對 `test_assets/` 與測試驗證腳本僅具備讀取權限，嚴禁 AI 修改測試標準答案以騙過系統。
2. **覆蓋率底線 (Coverage Threshold)：** 核心演算法模組的模擬測試必須跑完資料夾內 100% 的極端資料集，不可僅針對單一測試案例優化。
3. **實機最終裁決權：** 模擬器通過僅代表「邏輯正確」，系統的代碼審查規則設定 AI 審查員最高只能給予建議通過分數。最終的合併權限，必須由人類工程師在真實開發板上完成 HVT/EVT 驗證後方可執行。