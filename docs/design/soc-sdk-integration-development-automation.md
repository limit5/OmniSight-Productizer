# SoC SDK/EVK 併行整合開發與自動化流程指南 (含專屬工具鏈優化版)

本文件定義了將全新 SoC (System on Chip) 廠商提供的 SDK、專屬交叉編譯器與 EVK (開發評估板) 導入現有自動化開發系統的標準作業流程。為打破傳統「瀑布式」開發的等待時間，本流程採用**「三軌並行 (Concurrent Engineering)」**策略，透過高度解耦，讓基礎設施、軟體架構與硬體準備能同時啟動，並在最終的 HVT 階段完美匯集。

---

## 一、 核心並行策略與軌道定義 (The 3-Track Concurrent Workflow)

傳統流程中，軟體工程師往往需要等待硬體板子點亮、編譯環境架設完畢後才能動工。透過嚴格的介面定義與模擬機制，我們將整合工作拆解為以下三條可獨立推進的平行軌道：

| 軌道名稱 | 負責角色/系統 | 核心任務焦點 | 解耦關鍵 (為何能提前開工) |
| :--- | :--- | :--- | :--- |
| **軌道一：基礎設施與 AI 賦能** | DevOps / 系統管理員 | 專屬工具鏈配置、CMake Toolchain 建立、AI 技能設定 | 僅需 SDK 壓縮檔與原廠 PDF 文件，無需實體 EVK 版子即可建置。 |
| **軌道二：軟體架構與模擬** | C/C++ 應用開發者 | HAL 介面設計、Mock 假資料腳本、軟體演算法對接 | 透過原廠 API 規格書即可定義介面 (Interface)，用 PC 端模擬器即可驗證邏輯。 |
| **軌道三：硬體實機準備** | 硬體/底層韌體工程師 | EVK 開箱點亮、網路/串口設定、遠端燒錄通道建立 | 確保硬體本體無損壞，準備好實體連線通道即可，不依賴上層應用程式碼。 |

---

## 二、 軌道一：基礎設施與 AI 賦能 (DevOps & Infra Track)

本軌道旨在將 SoC 廠商提供的**專屬編譯環境**進行「隔離與固化」，並透過標準化配置防止 AI 產生編譯器路徑幻覺。

### Phase 1: 專屬工具鏈 (Vendor Toolchain) 部署與容器化
* **工具鏈絕對隔離：** 將原廠提供的交叉編譯器解壓至唯讀且固定的絕對路徑（例如 `/opt/vendor-name/gcc-arm-8.3/`）。**絕對禁止**將其加入系統全域的 `$PATH` 中，以免與 OS 預設的編譯器發生衝突。
* **Sysroot 與架構參數解析：** 爬梳原廠手冊，確認專屬的 `--sysroot` 路徑（包含原廠魔改過的標頭檔與底層庫），以及特定的架構優化參數（如 `-mcpu=cortex-a7`, `-mfloat-abi=hard`）。
* **建立 CMake Toolchain File (關鍵防呆)：** 這是系統核心。撰寫一份 `vendor_toolchain.cmake` 檔案，將原廠的 `C_COMPILER`、`CXX_COMPILER`、`SYSROOT` 與 `C_FLAGS` 釘死在裡面。

### Phase 4: AI 技能與全局規範注入 (Skill Up)
* **更新 `CLAUDE.md`：** 在專案根目錄寫入全局提示詞，明確告知 AI 目前的目標硬體，並**強制規定**編譯時必須掛載工具鏈檔：
  > *「編譯目標開發板程式時，嚴禁使用系統預設 GCC。必須使用 `rtk cmake -DCMAKE_TOOLCHAIN_FILE=build/vendor_toolchain.cmake ..` 進行配置。」*
* **建立廠商專屬 SOP：** 針對該 SoC 特殊的編譯報錯（例如缺少特定的原廠動態庫 `.so`），撰寫專屬的技能檔 (`vendor-sdk-guide.md`)，供 AI 在遇到 Linker Error 時呼叫參考。

---

## 三、 軌道二：軟體架構與模擬 (Software & Architecture Track)

本軌道旨在將底層硬體操作抽象化，確保核心演算法與上層應用不被特定廠商的 SDK 綁死。

### Phase 2: HAL 適配與介面定義 (HAL Adaptation)
* **API 封裝對接：** 閱讀廠商提供的驅動程式或多媒體 API 手冊，將其特殊寫法封裝進系統標準的硬體抽象層 (Hardware Abstraction Layer)。
* **隔離編譯：** 透過 C/C++ 的巨集 (如 `#ifdef VENDOR_SOC`) 將廠商特定的 Header Files 與實作隔離開來，確保 PC 端模擬編譯不會報錯。

### Phase 3: 模擬器與資料驅動同步 (Simulation Sync)
* **更新 Mock 驅動：** 根據新 EVK 的硬體腳位定義 (Pin Define) 或 I2C/SPI 位址，修改本地端的虛擬檔案系統腳本，確保 AI 對虛擬 GPIO 的讀寫能精準對應未來實機的操作。
* **測試用例更新：** 準備標準的測試資料集 (如特定格式的 NV12 影像或音訊 Raw Data)，讓軟體模組能在純 PC 狀態下完成功能性的單元測試。

---

## 四、 軌道三：硬體實機準備 (Hardware & Board Track)

本軌道專注於打通「虛擬開發環境」與「實體世界」的最後一哩路。

### EVK 開箱與基礎點亮 (Base Bring-up)
* **基礎系統燒錄：** 根據原廠手冊，將基礎的 Linux Image (通常由原廠提供，或自行由 Yocto/Buildroot 生成) 燒錄至 EVK 的 eMMC 或 SD 卡中，確保板子能順利開機。
* **連線通道驗證：** 測試 Serial Port (UART) 是否能正常輸出核心開機日誌 (Boot Log) 並具備終端機操作權限。

### 遠端部署環境建置 (Deployment Readiness)
* **網路與庫文件 (Shared Libraries) 配置：** 設定 EVK 靜態 IP。檢查 EVK 上的 `/lib` 或 `/usr/lib` 是否已放入原廠 SDK 編譯出的最新動態連結庫 (`.so`)，避免執行時發生庫版本不匹配。
* **撰寫自動化部署腳本：** 建立 `deploy_to_evk.sh` 腳本，讓 CI/CD 系統或 AI Agent 可以透過一行指令，將 Docker 中編譯好的執行檔推送到開發板並自動執行。

---

## 五、 最終匯集點：HVT 實機自動化驗證 (The Merge Point)

當上述三條軌道皆準備就緒，專案將進入 **Phase 5 (HVT - Hardware Verification Test)**，進行首次全系統的虛實整合。

### 自動化實機檢驗流程 (Hardware-in-the-Loop)
1. **精準編譯觸發：** AI Agent 在 Docker 環境中，精準指定 CMake Toolchain File 進行交叉編譯，產出完全符合目標 SoC 架構與 ABI 的執行檔。
2. **遠端部署：** 自動化腳本將執行檔推送到 EVK 上。
3. **實機驗證：** 執行檔在 EVK 上呼叫封裝好的 HAL，透過原廠驅動操作底層硬體暫存器或周邊。
4. **狀態回傳與交接：** EVK 透過網路將執行結果與 Log 傳回主系統。AI 判讀日誌無誤後，自動更新 `HANDOFF.md` 狀態檔，宣告 SoC SDK 整合成功。