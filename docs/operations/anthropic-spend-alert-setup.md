---
audience: operator
---

# Anthropic Console Spend Alert Setup（P0.2 第 1 層）

> **Audit reference**: `docs/audit/2026-04-27-deep-audit.md` §3 P0.2
> **Estimated time**: 10 分鐘 ops，純 console 操作、零 code 改動
> **Why this is operator-only**: Anthropic 沒提供 API 設 spend alert，必須手動進 console
> **Why this matters**: Finding #3 (Anthropic 額度耗盡 → silent gemma4 fallback) 三層防線之第 1 層，提早警告，不等到耗盡才知道

---

## 為何要做這件事

2026-04-25 production 已發生過一次 Anthropic 額度耗盡事件 — backend silent 切到 ollama gemma4 fallback、但因為當時 fallback chain `.env` knob 還沒 active，整個 LLM 路徑直接 break。第三方 audit 報告（2026-04-27）標為 P0.2 第 1 層必處理。

雖然現在 Phase 2 ollama fallback 已 active，但仍有兩個 risk：
1. **使用者體驗劇烈下降**（Opus 4.7 → gemma4:e4b 等於從 200B parameter 降到 8B、品質明顯不同）
2. **operator 不知何時發生**（沒 alert 直到使用者抱怨）

加 spend alert = 提早幾天知道、有時間 top-up 或調整 budget。

---

## 操作步驟

### 1. 登入 Anthropic console

前往 https://console.anthropic.com/settings/billing

用 OmniSight production 綁定的 Anthropic 帳號登入（如果不確定是哪個帳號、看 backend 用的 API key 對應的）。

### 2. 找到 Spend Alerts / Usage Limits 區塊

可能在：
- `Settings` → `Billing` → `Spend alerts`
- 或 `Settings` → `Usage` → `Set spending limit`

不同時期 Anthropic UI 可能 redesign。找含「spend」或「limit」的 section。

### 3. 設定兩個 threshold

| 等級 | 金額 | 行為 |
|---|---|---|
| **Warning** | $50 USD | Email 通知 operator（不 throttle、不 block） |
| **Hard cap** | $80 USD | 達到後 Anthropic API 自動 reject 後續請求（或 throttle，看 Anthropic 政策） |

理由：
- $50 警告線給 operator 一週左右反應時間（依日均消費推算）
- $80 硬上限避免月底突然超支
- 可依實際 production 月費調整（建議 = 月均 × 1.5 / 月均 × 2.4）

### 4. 設定 email 通知對象

```
operator-on-call@your-domain.com
ops-team@your-domain.com
```

至少 2 個（避免單一 email 過濾掉漏看）。

### 5. 確認 + 寫 audit log

完成後：
1. Anthropic console 應顯示「Spend alert active at $50 / Hard cap at $80」
2. **手動觸發測試 email**（Anthropic 通常有 "send test alert" 按鈕）— 確認 email 真的送得到
3. 在 OmniSight 對話中或 chat 中跟 AI agent 說「**已完成 Anthropic spend alert 設定，請 commit audit row**」，AI 會幫忙寫 commit 紀錄

---

## 驗證步驟

完成後：
- [ ] Console 顯示 spend alert active
- [ ] Test email 收到（檢查 spam folder）
- [ ] 兩個 email 對象都收到
- [ ] OmniSight repo 寫一個 `chore(audit/P0.2-1)` commit 標記完成

---

## Rollback / 調整

如果發現警告線設太低（每個月一直收信）或太高（月底才來不及）：
- 直接進 console 同位置調整數字
- Anthropic 通常允許隨時改
- 不需 redeploy / restart anything

---

## 跟其他防線的關係

P0.2 三層防線完整 picture：

| 層 | 內容 | 狀態 |
|---|---|---|
| **1** | **Anthropic console spend alert**（本文檔） | ❌ **operator 待做** |
| **2** | Phase 2 ollama fallback chain | ✅ active in production（2026-04-27 audit verified） |
| **3** | BP.F.8-F.10 hard-error classifier（區分 hard-error vs soft-fallback） | ❌ Phase F 未開工，需 Phase B 先做（預期 1-2 月） |

**完成第 1 層後**，本文檔狀態改 `audience: internal`、移到 `docs/audit/post-fix-records/`，作為歷史紀錄。

---

## References

- Audit report: `docs/audit/2026-04-27-deep-audit.md` §3 P0.2
- Action plan: `docs/audit/2026-04-27-deep-audit.md` §4.1 row 1
- Background: TODO.md row 107 標 `[O]` operator-blocked
