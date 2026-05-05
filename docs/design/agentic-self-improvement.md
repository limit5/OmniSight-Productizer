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
  導入自動化的提示詞優化框架（如 DSPy）。系統在背景持續收集 Agent「做對的任務」與「做錯的任務」，並把實際 serving 過的 system prompt snapshot、canary outcome、workflow failure、audit trail 連回 `prompt_versions`。

  Phase O gamma 的落地邊界是 **Evaluator Agent 只能提出候選 prompt 版本，不能直接改 L1 規則檔、不能直接 promotion**。定時 job 掃描 `audit_log` 與 workflow failure trajectory，挑出同一 prompt path 下重複出現的失敗型態（例如 tool policy 誤用、patch protocol 違規、錯誤 fallback 決策）。Evaluator Agent 使用 Opus 4.7 產出最小 diff，寫成 `prompt_registry.register_canary(path, body)` 可接受的 candidate body，目標只限 `backend/agents/prompts/**.md`；`CLAUDE.md` / `AGENTS.md` / `coordination.md` 仍屬 L1 immutable，不在 L3 自動優化範圍。

  Candidate 必須進入 human review queue，reviewer 看 diff、失敗 trajectory 摘要、預期改善指標與 rollback 條件後，才可把候選註冊為 canary。上線後沿用 Phase 63-C prompt registry canary：5% deterministic agent bucket、`record_outcome()` 累積 success / failure、`evaluate_canary()` 自動 rollback regression；即使 `evaluate_canary()` 回傳 `promote_canary`，最終 `promote_canary()` 仍必須由 human reviewer 明確批准。
* **進化結果 (Evolutionary Result)：**
  解決了人類難以手動調優龐大提示詞的痛點，但保留 release discipline：AI 負責發現重複失敗、提出可 review 的 prompt diff；人類負責 approve canary / promote；prompt registry 負責版本、canary、rollback 與 outcome counters。系統能針對失敗案例持續收斂 agent 行為，同時避免「自動改規則、自動升級」造成 specification gaming 或 governance bypass。

### Phase O gamma 與既有 Phase 63-C 的邊界

Phase O gamma 不新增第二套 prompt storage，也不繞過 Phase 63-C canary。它只補上「從 fail trajectory 產生候選 prompt diff」這一段：

| 面向 | Phase 63-C Prompt Registry | Phase O gamma Evaluator Agent |
|---|---|---|
| 目的 | 管理 prompt version、active / canary / archive role、5% canary routing 與 rollback | 從 repeated failure trajectory 產生候選 prompt diff |
| 輸入 | on-disk `backend/agents/prompts/*.md`、operator-provided body、runtime outcome counters | `audit_log` fail rows、workflow failure metadata、`prompt_versions` snapshot / outcome history |
| 輸出 | `prompt_versions` row 與 canary evaluation decision | human-review proposal：path、diff、evidence、expected metric、rollback condition |
| Promotion 權限 | `register_canary()` / `promote_canary()` 只在 review 後被 operator 或 reviewer action 觸發 | 無 promotion 權限；不能直接寫 active row |
| Rollback | `evaluate_canary()` regression path 可自動 archive canary | 只能標記候選 rejected / stale，不能覆寫 active |
| 不可碰範圍 | `CLAUDE.md`、`AGENTS.md`、`coordination.md`、任意非 prompt tree 檔案 | 同左；Evaluator 的 output 必須通過 `prompt_registry._normalise_path()` whitelist |

因此 L3 的資料流固定為：

1. `prompt_registry.capture_prompt_snapshot()` / `record_outcome()` 累積 prompt version 與 outcome shadow。
2. 定時 evaluator job 讀 `audit_log` 中可歸因到 prompt path 的 failure trajectory，聚合同類失敗。
3. Evaluator Agent（Opus 4.7）對單一 prompt path 產生最小候選 diff，附上 evidence bundle 與 expected pass-rate improvement。
4. Human reviewer approve 後才呼叫 `register_canary(path, body)`；拒絕則候選歸檔並寫 audit。
5. Canary 期間繼續收集 outcome；regression 由 `evaluate_canary()` 自動 rollback，promotion 則仍需 human reviewer 明確呼叫 `promote_canary(path)`。

設計上的硬限制：

1. **前置資料不足時不產生候選**：同一 prompt path 需要已累積 enough trajectory（至少多筆同類 failure + 對應 prompt snapshot），否則 evaluator 只能輸出 `insufficient_evidence`。
2. **一個候選只改一個 prompt path**：避免把多個 agent 行為變更塞進同一 canary，讓 rollback 與 attribution 仍可讀。
3. **Audit chain 是 source of truth**：候選建立、review approve / reject、canary register、rollback、promotion 都必須有 audit row；post-mortem 不需要重讀 repo 就能看見哪個 prompt 改動造成行為變化。
4. **L3 不取代 L1 / L2 / L4**：prompt diff 只能改善指令與決策框架；可泛化知識仍走 L1 skill distillation，工具缺口仍走 L2 toolmaking，模型權重更新仍走 L4 fine-tune gate。

---

## 🟡 Level 4: 資料飛輪與模型微調 (Data Flywheel & Auto-Fine-Tuning)
**核心概念：從根本上改變 AI 的大腦結構（神經網路權重）。**

* **實作機制 (Mechanism)：**
  當系統平穩運行數月後，將累積數以萬計的高品質訓練對：包含「修改前的破爛 C++ 代碼」、「AI 的多輪除錯過程」以及「最終通過 HVT 實機測試的完美代碼」。
  系統的 CI/CD 流水線在離峰時間自動觸發 MLOps 軌道，將這些紀錄清洗並打包成 `JSONL` 訓練集，針對專屬的開源模型（如本地部署的 Llama 3 專項模型）或允許微調的 API 進行微調 (Fine-tuning)。
* **進化結果 (Evolutionary Result)：**
  系統將孕育出一個專屬於貴公司的「嵌入式領域除錯大模型 v2.0」。Agent 的基礎邏輯推理能力將產生質的飛躍，原本需要 10 輪對話才能解決的硬體 Bug，升級後的模型可能只需 1 輪就能直接給出正確解答。
