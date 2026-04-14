# OmniSight-Productizer: AI 智力免疫與降智對應機制 (Intelligence Immune System)

本文件定義了全自動化系統中，針對大型語言模型 (LLM) 性能波動、降智或幻覺增加的自動化對應機制。系統將「智力穩定性」視為與「伺服器穩定性」同等重要的指標。

---

## 一、 智力監控指標 (Intelligence Metrics)

系統透過以下四個自動化維度，實時監測當前 Agent 的智力狀態：

1. **代碼驗證率 (Code Pass Rate)：** 沙盒編譯失敗率是否連續 3 次超過門檻？
2. **約束遵守度 (Constraint Compliance)：** AI 是否在回覆中遺漏了 `CLAUDE.md` 規定的必要欄位（如：漏寫 `HANDOFF.md`）？
3. **邏輯穩定性 (Logic Consistency)：** 針對同一個錯誤 Log，AI 給出的修復邏輯是否與歷史成功案例（L3 記憶）產生嚴重衝突？
4. **輸出長度異常 (Token Entropy)：** 回覆是否變得過度簡短（偷懶）或異常冗長（復讀機現象）？

---

## 二、 三級自動應變機制 (The 3-Tier Mitigation)

### 🟢 第一級：原地校準 (In-place Calibration)
* **觸發條件：** 單次任務失敗或輕微指令漂移。
* **自動行為：**
  * **上下文重置：** 清空當前對話歷史，僅保留 `HANDOFF.md` 摘要，排除「上下文中毒」。
  * **強效提示 (Prompt Boost)：** 在下一個請求中自動附加 L1 記憶中的「黃金範例 (Few-shot)」與「負面約束清單」。
  * **思考強制化 (COT Enforcement)：** 強制要求模型在輸出代碼前，先進行 500 字的邏輯推演。

### 🟡 第二級：動態路由備援 (Dynamic Routing Fallback)
* **觸發條件：** 第一級校準無效，或編譯錯誤無法在 3 輪內解決。
* **自動行為：**
  * **跨廠牌切換：** 透過 OpenRouter 將任務從當前模型 (如 Claude 3.5) 切換至備援模型 (如 GPT-4o 或 DeepSeek Coder V2)。
  * **架構異構化：** 利用不同廠商模型對同一問題的理解差異，繞過特定模型的「降智盲點」。

### 🔴 第三級：隔離與人工介入 (Containment & Human Intervention)
* **觸發條件：** 多個模型嘗試後皆無法通過沙盒驗證，或偵測到潛在的系統性安全風險。
* **自動行為：**
  * **任務掛起：** 停止該分支的所有自動化操作，防止 AI 瘋狂消耗 Token 並產生垃圾代碼污染 Git Repo。
  * **日誌封存：** 自動打包當前的所有 L2 記憶與沙盒 Log，傳送至 Jira 標記為 `Critical`，並呼叫人類主管進行「智力診斷」。

---

## 三、 預防性進化策略 (Proactive Evolution)

為減少降智發生的頻率，系統執行以下後台任務：

1. **每日智力回歸測試 (Daily IQ Benchmark)：**
   每天凌晨，系統自動從 L3 記憶庫中選取 10 個「經典難題」，讓當前模型重新回答。若得分低於 Baseline，系統自動發布預警並降低該模型的權重。
2. **提示詞版控與優化 (Prompt Versioning)：**
   所有 System Prompt (`CLAUDE.md`) 皆具備版本控制。當發現新版 Prompt 在新模型下表現不佳時，系統具備自動回滾 (Rollback) 至舊版提示詞的能力。
3. **資料淨化 (Memory Sterilization)：**
   定期清理 L3 經驗記憶，刪除已過時的 SDK 解法，防止 AI 被「老舊的知識」帶偏邏輯。

---

## 四、 執行指令範例 (CLI Reference)

系統調度器在調度時，可帶入 `intelligence_policy` 參數：

```bash
# 執行高難度 C++ 任務，並啟用嚴格智力監控與自動 Fallback
claude -p "執行 docs/sop_simulation.md" --policy="strict_monitor" --fallback="openai/gpt-4o"