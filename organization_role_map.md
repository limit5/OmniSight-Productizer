# 大型嵌入式 AI 企業研發與商業組織架構綱要

**發布日期：** 2026 年
**架構類型：** 矩陣式平台化架構 (Matrix Platform Organization)
**核心戰略：** 軟硬解耦、底層標準化、應用多樣化。以「前瞻探路、中台修路、產品跑路」為運作核心，支撐從軍車規至消費級之多產品線發展。

---

## 第一層：戰略與決策委員會 (Leadership & Strategy)

**部門定位：** 組織大腦，負責定調公司整體技術方向、SoC 供應鏈戰略與高階資源分配。

* **首席技術官 (Chief Technology Officer, CTO):** 制定公司中長期技術藍圖與硬體供應鏈戰略。
* **首席產品官 (Chief Product Officer, CPO):** 掌管全線產品之商業命脈與垂直市場進攻策略。
* **首席系統架構師 (Chief System Architect):** 定義軟硬體解耦邊界與跨平台通訊標準 (如 Protobuf/Schema)。
* **首席科學家 (Chief Scientist):** 引領前瞻實驗室，奠定公司未來三至五年之技術護城河。

---

## 第二層：前瞻技術實驗室 (Advanced Technology Group - ATG)

**部門定位：** 專注於技術成熟度 (TRL) 1-3 階段之顛覆性技術探索，脫離短期營收壓力。

* **AI 系統研究員 (AI System Researcher):** 探索下一代邊緣運算架構與極端神經網路輕量化理論。
* **前瞻通訊科學家 (Advanced Communication Scientist):** 預研 V2X 車間控制、6G 網路與高階抗干擾演算法。
* **感測/光學研究員 (Sensory / Optical Researcher):** 研究突破現有物理限制之感測技術與底層數學模型。

---

## 第三層：核心技術中台 (Core Technology Platform)

**部門定位：** 研發核心引擎 (TRL 4-6)，負責將技術模組化與標準化，屏蔽底層差異以利上層應用。

### 3.1 AI 與影像演算法中心 (AI & Vision Core)
* **應用科學家 (Applied Scientist):** 針對業務極限場景建立數學模型，產出 SOTA 演算法原型。
* **影像演算法工程師 (Imaging Algorithm Engineer):** 透過 C/C++ 與 NEON/SIMD 實作極致優化之影像預處理。
* **ISP / 3A 調優工程師 (IQ Engineer):** 負責感測器標定、色彩管理及自動曝光/對焦之底層邏輯。
* **AI 部署與優化工程師 (AI Optimization Engineer):** 負責模型量化、剪枝及各 NPU 平台之轉譯加速。

### 3.2 硬體與底層平台中心 (Hardware & OS Foundation)
* **硬體設計工程師 (Hardware Engineer, EE):** 負責電路設計、BOM 表選型與訊號完整性驗證。
* **機械與結構工程師 (Mechanical / Structural Engineer, ME):** 負責散熱結構設計及工業/軍車規之防護設計。
* **SoC / BSP 平台工程師 (BSP Engineer):** 負責 Linux/RTOS 核心移植及底層周邊驅動程式開發。
* **RF / 天線工程師 (RF/Antenna Engineer):** 負責無線通訊之天線佈局與電磁相容性 (EMC) 確保。

### 3.3 系統架構與中間件中心 (Framework & Middleware)
* **技術產品經理 (Technical Product Manager, TPM):** 擔任業務與中台之橋樑，將商業願景轉化為架構 API 規格。
* **HAL 硬體抽象層工程師 (HAL Engineer):** 實作軟硬解耦之 C++ 介面，確保程式碼具備高度跨晶片可移植性。
* **通訊中間件工程師 (Connectivity Middleware Engineer):** 封裝無線通訊底層邏輯，處理連線漫遊與狀態機管理。
* **協定與標準化工程師 (Protocol & Standardization Engineer):** 維護全公司之數據字典與 JSON Schema/Protobuf 定義。

---

## 第四層：垂直產品事業部 (Vertical Business Units - BUs)

**部門定位：** 產品落地單位 (TRL 7-9)，調用中台 SDK 開發終端應用，並直接對接市場與客戶。

### 4.1 對外商業鐵三角 (各 BU 皆配置)
* **商業產品經理 (Business Product Manager, BPM):** 定義終端產品規格、定價策略，對 BU 利潤負責。
* **現場應用工程師 (Field Application Engineer, FAE):** 擔任技術前鋒，負責 PoC 驗證、客戶端 API 整合指導與第一線除錯。
* **業務代表 / 商務經理 (Sales / Account Manager):** 負責商務談判、合約簽署及供應鏈客戶關係維護。

### 4.2 終端應用與整合團隊 (依業務屬性編制)
* **實時系統工程師 (RTOS Engineer):** [軍規/車規] 負責微秒級任務調度之實時系統應用開發。
* **功能安全專家 (Functional Safety Expert):** [軍規/車規] 設計符合 ISO 26262 等標準之軟硬體失效備援機制。
* **桌面應用工程師 (Desktop Application Engineer):** [工業/商業] 運用 C++/Qt 開發高效能本地監控與檢測軟體。
* **系統整合專家 (System Integration Expert, SI):** [工業/商業] 負責 Edge AI 設備與 PLC/MES 等工廠自動化系統之低延遲對接。
* **後端工程師 (Backend Engineer):** [雲端/SaaS] 開發設備管理平台，處理 API 閘道與 OTA 派發邏輯。
* **大數據架構師 (Big Data Architect):** [雲端/SaaS] 設計高併發資料庫架構，處理海量 AI 數據與設備日誌。
* **雲端維運專家 (Cloud Operations Expert):** [雲端/SaaS] 維護 Kubernetes 叢集，確保 SaaS 服務之高可用性。
* **行動端 App 開發工程師 (Mobile App Developer):** [消費級] 開發 iOS/Android 應用，優化配網與 P2P 串流體驗。
* **前端工程師 (Frontend Engineer):** [消費級/雲端] 開發直覺、響應式之 Web 儀表板與中控管理介面。
* **UI / UX 設計師 (UI/UX Designer):** [消費級/工業] 負責人機介面設計，優化資訊層級與操作體驗。

---

## 第五層：質量保障與營運基礎設施 (QA & Infrastructure)

**部門定位：** 守護研發品質與系統安全性，防範系統性風險並維護自動化開發流程。

* **DevOps 工程師 (DevOps Engineer):** 維護 CI/CD 自動化管線與跨平台編譯矩陣。
* **自動化測試工程師 (SDET):** 撰寫測試腳本，於設備農場執行 SDK 介面與演算法之自動化迴歸測試。
* **合規與認證專家 (Compliance & Certification Expert):** 專職處理各國射頻認證 (FCC/CE) 及軍車規審查文檔。
* **資安防護專家 (Cybersecurity Expert):** 整合硬體 TPM 晶片及 TLS 加密，防範模型竊取與網路攻擊。