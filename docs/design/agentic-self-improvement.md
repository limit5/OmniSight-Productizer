# OmniSight-Productizer: AI 智能體自我進化架構 (Agentic Self-Improvement)

本文件概述了在自動化嵌入式開發系統中，如何透過工程架構設計，讓 AI Agent 突破靜態模型的限制，達成「行為與系統層級的自我改善」。此進化架構分為四個遞進的層級，旨在讓 AI 系統隨著運行時間的增長，自動沉澱領域知識並提升解決問題的成功率。

---

## 🟢 Level 1: 知識繁衍 (Knowledge Generation)
**核心概念：讓 AI 自己寫自己的說明書與技能包。**

* **實作機制 (Mechanism)：**
  當 AI 開發者 Agent 經歷多輪除錯（例如超過 5 輪對話），並最終成功解決一個極度困難的特定晶片 Bug（如記憶體洩漏或 Linker Error）且通過實機驗證後，系統調度者將強制介入。
  系統會要求 Agent 總結剛才的除錯思路與最終解法，自動生成一份標準化的技能說明檔（例如 `rockchip-memory-leak-skill.md`），並將其存入專案的 `/.skills/` 技能庫資料夾中。
* **進化結果 (Evolutionary Result)：**
  系統的 SOP 技能庫會像滾雪球般自動擴充。未來的 Agent 接收到類似任務時，會自動掛載這個由「前輩」寫好的新技能，避免同一個坑踩兩次，達成團隊級的經驗傳承。

### BP.M 與 R3 Scratchpad 的邊界

BP.M 的 L1 skill distiller 與 R3 Scratchpad 都會把長任務壓縮成 markdown，但兩者的生命週期不同，不能互相替代：

| 面向 | R3 Scratchpad | BP.M Skill Distiller |
|---|---|---|
| 目的 | 單一任務內的 working memory offload 與 crash / max-token recovery | 成功任務後的 cross-task knowledge distillation |
| 內容 | `Current Task` / `Progress` / `Blockers` / `Next Steps` / `Context Summary` 等當下操作狀態 | 可泛化的解題程序、觸發條件、失敗徵兆、檢查步驟與後續任務可重用的 SOP |
| 儲存位置 | `data/agents/<agent_id>/scratchpad.md` 與 archive，at-rest encrypted，per-agent mutable | `auto_distilled_skills` draft row，review 後才 promote 到 `configs/skills/<skill_name>/SKILL.md` |
| 啟用時機 | 任務執行中；tool_done、turn interval、continuation、crash recovery 等事件觸發 | 任務成功後；`(tool_calls > 5 OR iterations > 3) AND success == true` 觸發 |
| 審核語義 | 無 promotion；操作員只拿它判斷 hot-resume / post-mortem | draft 必經 operator review / promote，才成為 production skill pack |
| 失敗語義 | best-effort；scratchpad IO 失敗不得阻斷 agent step | best-effort；distillation / audit 失敗不得改變 workflow completion |

因此：

1. Scratchpad 是 **in-task working memory**。它可以保留尚未完成、尚未驗證、甚至錯誤的推理線索，目標是讓同一個 agent 在同一個任務中續跑。
2. Skill distiller 是 **cross-task knowledge**。它只在任務成功後產生 draft，必須 scrub secrets、移除 task-specific 狀態，並經 human review gate 才能影響未來任務。
3. Distiller 不直接把 scratchpad archive 當成 skill pack，也不因 scratchpad 存在就自動 promotion。若 trajectory 裡已包含 scratchpad summary，distiller 只能把它當作輸入脈絡，輸出仍必須是可泛化的技能文件。
4. R3 與 BP.M 的共同 contract 是「壓縮狀態不可破壞主流程」：scratchpad 不能阻斷 tool execution；distiller 不能阻斷 workflow success；跨 worker 的真相分別由 disk snapshot 與 PG draft row 承載。

---

## 🔵 Level 2: 工具製造 (Toolmaking)
**核心概念：嫌現有工具不好用，就自己造一把。**

* **實作機制 (Mechanism)：**
  AI 目前常受限於基礎命令列工具的低效（例如直接讀取幾萬行的 Valgrind 原始日誌會導致 Token 溢出）。系統允許 Agent 識別出這些工作流的「瓶頸」，並主動撰寫客製化的輔助腳本（如一個專門用來過濾無用記憶體位址的 `parse_valgrind.py` 或 `analyze_gpio_timing.sh`）。
  寫好後，Agent 將此腳本 Commit 至 Git 儲存庫，並註冊為系統的全域指令。
* **進化結果 (Evolutionary Result)：**
  AI 實質上擴充了整個自動化系統的基礎設施。下一次遇到同樣的分析任務時，Agent 可以直接呼叫自己發明的新工具（如 `rtk python parse_valgrind.py`），大幅節省 Token 消耗並提高數據判讀的準確率。

---

## 🟣 Level 3: 提示詞自我優化 (Meta-Prompting / DSPy)
**核心概念：AI 自己修改自己的「潛意識規則」與系統提示詞。**

* **實作機制 (Mechanism)：**
  導入自動化的提示詞優化框架（如 DSPy）。系統在背景持續收集 Agent「做對的任務」與「做錯的任務」。
  定時啟動一個高智商的**評估 Agent (Evaluator Agent)** 來進行覆盤分析：「*為什麼編譯 Agent 剛才失敗了？因為 `CLAUDE.md` 裡面對指標對齊的描述太模糊。*」隨後，評估 Agent 會自動改寫並發布新版本的 `CLAUDE.md` 或 System Prompt。
* **進化結果 (Evolutionary Result)：**
  解決了人類難以手動調優龐大提示詞的痛點。AI 能夠針對失敗案例，動態調整自身的行為準則與思考框架，讓任務成功率在無人介入的情況下穩步爬升。

---

## 🟡 Level 4: 資料飛輪與模型微調 (Data Flywheel & Auto-Fine-Tuning)
**核心概念：從根本上改變 AI 的大腦結構（神經網路權重）。**

* **實作機制 (Mechanism)：**
  當系統平穩運行數月後，將累積數以萬計的高品質訓練對：包含「修改前的破爛 C++ 代碼」、「AI 的多輪除錯過程」以及「最終通過 HVT 實機測試的完美代碼」。
  系統的 CI/CD 流水線在離峰時間自動觸發 MLOps 軌道，將這些紀錄清洗並打包成 `JSONL` 訓練集，針對專屬的開源模型（如本地部署的 Llama 3 專項模型）或允許微調的 API 進行微調 (Fine-tuning)。
* **進化結果 (Evolutionary Result)：**
  系統將孕育出一個專屬於貴公司的「嵌入式領域除錯大模型 v2.0」。Agent 的基礎邏輯推理能力將產生質的飛躍，原本需要 10 輪對話才能解決的硬體 Bug，升級後的模型可能只需 1 輪就能直接給出正確解答。
