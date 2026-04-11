# OmniSight-Productizer: 單一 Gerrit 叢集與單向外推架構設計

本文件定義了 OmniSight-Productizer 系統中，由 AI Agent 進行協同開發與自動化程式碼審查的標準架構。本架構採用「單一 Gerrit 作為審查閘道」搭配「單向 Replication 鏡像外推」的拓撲設計，確保底層程式碼的安全性、可追溯性與高品質。

---

## 一、 架構總覽 (Architecture Overview)

系統以 Gerrit Code Review 伺服器作為唯一的「真理來源 (Source of Truth)」與協作中樞。所有的 AI 開發任務都在獨立的沙盒中進行，且任何修改都必須先提交至 Gerrit 形成 Patch Set。經過 AI 審查員的初步檢驗與人類主管的最終核准後，Gerrit 才會透過 Replication Plugin 將乾淨且正確的程式碼，單向推送到最終的儲存庫。

### 核心拓撲結構
1. **開發端 (N 個 Agent)：** 多個運行於 WSL2 或 Docker 容器內的 AI Agent 獨立拉取程式碼。
2. **審查端 (1 個 Gerrit 叢集)：** 集中接收 `refs/for/main` 的提交請求，儲存 Review 記錄與分數。
3. **儲存端 (N 個遠端 Repo)：** Merge 成功後，自動單向同步至外部 GitHub 及私有 GitLab 伺服器（如 `sora.services` 等）。

---

## 二、 系統角色與權限配置 (Roles & Permissions)

為防止 AI 產生幻覺破壞核心業務或底層硬體通訊邏輯，系統內的角色與權限採嚴格的分級制度：

* **AI 開發者 (AI Coder Agent)**
    * **環境：** 每次領取任務時，於獨立的 Docker Container 中執行 `git clone`，確保工作區隔離。
    * **權限：** 僅具備 `Push` 至 `refs/for/*` (建立待審查 Patch Set) 的權限。絕對禁止直接 Push 至 `main`。
    * **職責：** 撰寫程式碼、處理 C/C++ 底層邏輯、編譯 Makefile，並在回合結束時產出 `HANDOFF.md` 或狀態 JSON 進行交接。
* **AI 審查員 (AI Reviewer Agent)**
    * **觸發：** 透過 Gerrit Event Stream 或 Webhook 自動喚醒。
    * **權限：** 具備讀取 Patch Set 差異 (Diff) 以及透過 REST API 留下行內註解 (Inline Comments) 的權限。
    * **評分上限：** 最高僅能給予 `Code-Review +1` (建議通過) 或 `Code-Review -1` (建議修改)。
    * **職責：** 執行靜態分析，檢查記憶體洩漏 (Memory Leak)、指標越界、多執行緒安全等常見底層錯誤。
* **人類主管 (Human Maintainer)**
    * **權限：** 擁有系統最高權限，可給予 `Code-Review +2` (絕對放行) 及執行 `Submit` (合併分支)。
    * **職責：** 審視高階業務架構、硬體整合正確性，並做最終的 Merge 決策。

---

## 三、 核心開發規則 (Core Rules)

1.  **隔離沙盒原則 (Sandboxed Development)：**
    多個 Agent 嚴禁共用同一個實體 Git 資料夾。每個任務啟動時，系統自動配置全新的虛擬環境與獨立 Token，任務結束後由系統自動清理磁碟 (Garbage Collection) 以釋放 I/O 資源。
2.  **明確狀態交接 (State Handoff)：**
    受限於上下文視窗限制，AI 開發者在結束單次對話或任務區塊時，必須將當前進度、修改檔案路徑、編譯狀態以及「下一步待執行清單」寫入專案的 `HANDOFF.md`，並附加於 Git Commit Message 中，確保接手的 Agent 狀態無縫接軌。
3.  **無限迴圈防護 (Infinite Loop Prevention)：**
    當 AI 審查員給予 `-1` 時，AI 開發者會自動讀取回饋並修正。為防止雙方因邏輯衝突陷入死迴圈，系統強制設定 **最大重試次數 (Retry Limit，建議值為 3 次)**。超過次數限制尚未獲得 `+1` 的 Patch Set，將凍結開發並標記 `@Human` 請求人類介入處理。

---

## 四、 標準工作流程 (Standard Workflow)

1.  **任務分派 (Task Provisioning)：**
    OmniSight-Productizer 派發任務（例如：實作某硬體的暫存器控制邏輯）。系統分配一個獨立工作區，AI 開發者就緒。
2.  **開發與提交 (Push for Review)：**
    AI 開發者完成代碼與自我測試後，執行 `git push origin HEAD:refs/for/main`。程式碼進入 Gerrit 伺服器，生成 **Patch Set 1**。
3.  **自動審查 (Automated Code Review)：**
    Gerrit 觸發 AI 審查員。審查員調用 API 分析程式碼，若發現潛在的記憶體管理錯誤，於特定行數留下 Comment 並給予 `Code-Review -1`。
4.  **修正迭代 (Evolution & Refinement)：**
    AI 開發者收到通知，根據 Comment 進行修改，再次提交產生 **Patch Set 2**。此時審查員確認無誤，給予 `Code-Review +1` 與 `Verified +1`。
5.  **人類決策 (Human Approval & Merge)：**
    人類主管登入 Gerrit 介面，檢視 Patch Set 2 的邏輯與 AI 的互動歷程。確認無誤後，點選 `Code-Review +2` 並按下 `Submit`。程式碼正式合併進入 Gerrit 的主分支。
6.  **單向外推同步 (One-Way Replication)：**
    Gerrit 內部觸發 Replication 外掛，將最新版本的代碼平行、即時推送到外部的 Git Repository (如內部 GitLab 鏡像伺服器及 GitHub)，觸發後續的 CI/CD 發布流程。

---

## 五、 Replication 外推拓撲配置 (Replication Topology)

系統透過 Gerrit 的 `replication.config` 集中控管代碼的分發，維持架構的單向資料流（Gerrit -> 遠端伺服器）：

```ini
# replication.config 範例
[remote "private-gitlab"]
  url = git@sora.services:project/omnisight-core.git
  push = +refs/heads/*:refs/heads/*
  push = +refs/tags/*:refs/tags/*
  mirror = true
  threads = 3

[remote "public-github"]
  url = git@github.com:organization/omnisight-open.git
  push = +refs/heads/main:refs/heads/main
  mirror = false
  ```
透過此配置，任何 Agent 產生的混亂或錯誤提交都將被阻擋在 Gerrit 的防火牆內，唯有經過嚴格審查、具備人類背書的程式碼，才會出現在外部的正式代碼庫中。