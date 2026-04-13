# OmniSight-Productizer: Edge AI 與 NPU 模型部署自動化整合指南

當目標硬體包含 NPU (神經網絡處理單元) 時，產品開發將從純粹的「軟體工程」升級為「AIoT 軟硬整合工程」。本指南定義了如何將不同 AI 應用（物件偵測、人臉辨識、瑕疵檢測等）的資料蒐集、模型訓練、廠商專屬格式轉換與邊緣端部署，無縫融入現有的多智能體自動化系統中。

---

## 一、 架構升級：NPU 統一推論介面 (Inference HAL)

為了避免上層應用程式被特定廠商的 NPU API 綁死（例如換了晶片，所有的推論程式碼都要重寫），系統必須建立一個與底層無關的 **NPU HAL (硬體抽象層)**。

* **抽象化輸入/輸出：** AI Agent 撰寫的應用邏輯，只負責將影像轉換為標準的 Tensor (張量)，並呼叫 `InferenceEngine::Run()`。
* **封裝廠商 API：** 系統底層根據 CMake Toolchain 設定，自動編譯對應的 Wrapper。例如：
  * 若目標平台為 Rockchip，底層自動呼叫 `rknn_inputs_set()` 與 `rknn_run()`。
  * 若目標平台為 NXP，底層自動呼叫 TensorFlow Lite for Microcontrollers API。
* **AI Agent 的優勢：** 負責開發「瑕疵偵測」或「條碼辨識」邏輯的 AI Agent，**完全不需要知道底層是哪一顆 NPU**。它只需要專注於前處理 (Pre-processing) 與後處理 (Post-processing，如 NMS 非極大值抑制)。

---

## 二、 流程升級：新增「第四軌道 - MLOps 模型工程」

在原有的「基礎設施、軟體架構、硬體實機」三軌並行之外，針對 AI 產品新增專屬的 **MLOps 軌道**。這個軌道由具備特定技能的 Agent 負責執行。

### Phase A: 資料準備與雲端訓練 (Data & Training)
* **執行環境：** 雲端 GPU 伺服器 (非 WSL2 嵌入式沙盒)。
* **自動化整合：** * 系統透過 Webhook 觸發 **「資料 Agent」**，自動驗證新蒐集的資料集（如人臉照片、瑕疵樣本）格式是否正確。
  * 觸發 **「訓練 Agent」**，使用 PyTorch 或 YOLOv8 框架進行模型訓練，並匯出標準的跨平台格式（如 **ONNX** 檔）。

### Phase B: 邊緣端量化與格式轉換 (Quantization & Conversion)
* **執行環境：** WSL2 或 Docker 封裝的廠商專屬轉換工具環境。
* **自動化整合 (最關鍵的一步)：**
  * DevOps 團隊事先將各廠商的轉換工具 (如 RKNN-Toolkit) 包裝進 Docker。
  * **「部署 Agent」** 接收 ONNX 檔後，準備**校正資料集 (Calibration Dataset)**。
  * 執行廠商工具，將浮點數模型 (FP32) 量化為整數模型 (INT8)，並轉換為專屬格式 (如 `.rknn` 或 `.om`)。

### Phase C: 模擬器與精度驗證 (Verification - 呼叫 Generator-Verifier 模式)
* **精度防線：** 量化過程必然會導致模型失真 (Accuracy Drop)。
* **自動化整合：**
  * 修改 `simulate.sh` 腳本，加入 NPU 驗證模式：`./simulate.sh --type=npu_verify --model=detect.rknn --test_images=dataset/`
  * 系統自動將轉換後的模型在 PC 端模擬器中跑過上百張測試圖，產出 mAP (平均精度) 報告。
  * **AI 審查員** 比對量化前後的精度。若精度掉出容許範圍 (>2%)，自動將工單退回給「訓練 Agent」，要求重新微調。

---

## 三、 多種 AI 產品情境的套件化管理 (Skill Kits)

面對多樣化的 AI 產品需求（如人臉辨識、手勢偵測），系統不應該寫死邏輯，而是應該利用 **「調度-子智能體 (Orchestrator-Subagent)」** 模式，動態載入不同的 **AI 技能包 (Skill Kits)**。

| 產品應用情境 | 載入之專屬 Agent 技能包 (Skill Kit) | 關注之驗證指標 (Metrics) |
| :--- | :--- | :--- |
| **物件/瑕疵偵測** | `yolo-detection-skill` (包含 NMS 解析邏輯) | 框選準確率 (mAP)、漏報率 (False Negative) |
| **人臉/身分辨識** | `face-recognition-skill` (包含特徵向量 Cosine Similarity 計算) | 錯誤接受率 (FAR)、錯誤拒絕率 (FRR) |
| **手勢姿態偵測** | `pose-estimation-skill` (包含人體關鍵點連線解析) | 關鍵點偏移誤差 (OKS) |
| **條碼/QR Code** | `barcode-hybrid-skill` (結合 NPU 定位 + 傳統 ZBar 解析) | 極端角度/模糊場景的解碼成功率 |

**運作方式：**
當 Jira 工單標記為 `@Face-Recognition` 時，調度者 Agent 會自動掛載 `face-recognition-skill`，此時負責開發的 Agent 就會瞬間具備人臉辨識特有的後處理邏輯知識，並知道要呼叫哪些特定的測試資料集。

---

## 四、 NPU 整合的防呆與風險控管

1. **鎖死轉換工具版本：** 晶片原廠的轉換工具 (Toolchain) 版本必須與 EVK 板子上的驅動 (NPU Driver) 版本**絕對一致**。DevOps 必須在 Dockerfile 與 CMake 中加上嚴格的版本校驗機制。
2. **硬體資源爭奪限制：** 若多個模型（如「場景辨識」與「手勢偵測」）要同時在同一顆 NPU 上運行，系統必須在 HVT 階段執行壓力測試，確保記憶體頻寬與 NPU 算力不會被單一模型吃光而導致系統死機。