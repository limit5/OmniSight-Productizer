---
audience: operator
---

# PR Review Checklist — OmniSight

> **Audit reference**: `docs/audit/2026-04-27-deep-audit.md` §3 P1.4
> **Background**: 本 session 多數 bug 是「operator 在 production 用了才回報」（R16 / R18 / R19 / R22 系列 / R20-A / R20-B 等）。**測試 coverage 對窄面板 layout / 暫時網路 / SSE 邊界完全空白**。本 checklist 寫進 PR review SOP、把 4 類常見 bug 移到 PR 階段就抓出來。

---

## 4 大類 PR Review checklist

### 1. UI Layout（最常 ship 後 operator 報）

對應審計 §3 P1.1 的 5 個 pattern。**任何修改 `components/omnisight/*.tsx` 或加新 component 的 PR、reviewer 5 條都要 check**：

- [ ] **Pattern 1 (`min-w-0`)**：任何 grid/flex item wrapper 含可能 overflow 子元素時加了 `min-w-0`？
- [ ] **Pattern 2 (truncate / shrink-0 / whitespace-nowrap)**：重要 vs 可省文字並排時、各自有對的 class？
- [ ] **Pattern 3 (two-line vs single-line)**：3+ 元素並排時考慮過 two-line？選 two-line 的決策有寫進 component comment？
- [ ] **Pattern 4 (container query)**：當 component 在 dashboard 多 panel 並列場景、有用 `@container` 而非 viewport query 嗎？
- [ ] **Pattern 5 (createPortal)**：任何 modal/drawer/popover 子元素在 holo-glass 容器內、有放進 createPortal 嗎？SSR 防護有嗎？

詳細解釋見 `docs/design/ui-layout-patterns.md`。

### 2. SSE / 暫時網路（R16 / R19 系列根因）

對應審計 §3 P1.2。SSE / WebSocket / long-poll 任何修改：

- [ ] 連線 init 失敗（401 / 503 / network blip / CF tunnel buffering）後仍能 retry 嗎？
- [ ] init 多 phase 設計是否每 phase 各自 try/catch（R16 教訓 — 不要把 critical phase nested 在 optional phase 同 catch）？
- [ ] 連線 disconnect / cancel 路徑會不會 mutate 共用 state（R19 教訓 — finally block 不要做 side effect 給其他 client 看到）？
- [ ] Background tab / browser throttle 的場景驗過了嗎？
- [ ] 重 connect cycle（disconnect → reconnect within window）會不會卡 stale state？

### 3. Empty State / 第一次使用體驗（R20-A / R20-B / LOCKS R23 系列）

對應審計 §3 P1.4 第 4 類。任何使用者面板 / 對話 / 通知顯示 list / metric：

- [ ] 0 筆資料的 empty state 設計過了嗎？（避免「panel 看起來像 broken」）
- [ ] 第一次按按鈕（INVOKE / submit / install）的 reaction 路徑寫過 spec？
- [ ] Error state 跟 empty state 視覺有區別（red 邊 vs 灰 / 綠 chill）？
- [ ] Coach prompt / next-step suggestion 有設計給 stuck operator？

### 4. 既有 production user 兼容性（A2 / R15 / Phase 5b 教訓）

對應審計 §3 P0/P1。任何後端 schema / auth / API surface 變動：

- [ ] alembic migration 是 add-only（不 drop / rename existing column）？
- [ ] Per-tenant feature flag（既有 tenant 預設 off）保留？
- [ ] Single-knob env rollback（`OMNISIGHT_FOO_ENABLED=false`）保留？
- [ ] Compat regression test（既有 password user / API key client / test token 等）pass？
- [ ] Production runtime 真的驗過 — 不只看 TODO.md 的 `[D]` 標記就信？

---

## 5 大強制 build / deploy gate

獨立於 4 大 checklist、每次 PR merge / production deploy 都要過：

- [ ] **TS gate**: `npx tsc --noEmit` 0 error（P0.1, 2026-04-27 落地，next.config.mjs::ignoreBuildErrors=false）
- [ ] **Test pass**: 既有 backend pytest + frontend vitest 全綠
- [ ] **Lint pass**: ESLint + ruff（如果 backend 有改 Python）
- [ ] **Manual 視覺檢查**：UI 改動在 narrow viewport (375 / 768) 看一次
- [ ] **Operator deploy SOP**：對 backend 改 → rebuild backend-a/b、對 frontend 改 → rebuild frontend、對兩邊改 → 全 rebuild + 4 步 rolling restart（詳細見 `docs/design/blueprint-v2-implementation-plan.md` §7.3）

---

## 為何 PR Review Checklist 這條重要

審計報告 §3 P1.4 點出：「**Operator-driven 而非 CI-driven 的 bug 發現模式**」。本 session 4 種 finding 都是「先用、後修」：

1. 窄面板 overflow（5+ 元件、17 fix(ui) commit / 30 天）
2. SSE 短暫斷線下的狀態不一致（R16 / R19）
3. Class composition 邊界 bug（createPortal / clip-path / backdrop-filter）
4. UI empty state 沒設計（LOCKS / 通知 / 帳號鎖）

每個 bug 都讓 operator 在 production 撞到、回報、AI 修。這個循環的單次成本（context 切換 + 修 + redeploy + verify）大約是「PR 階段就 catch 」的 5-10 倍。

**把 review 階段做扎實、線上事故率會下降一個 order**。本 checklist 是工具、不是儀式 — 每條都對應一次過去踩過的真實坑。

---

## 維護紀律

- 本 doc 每次有新類型的 production bug 浮現、加新一條 checklist row
- 累積 N 條 row 但 review 太冗長時、合併成更高層的 pattern
- 半年 review 一次：哪些 row 從沒抓到 bug → 移除（避免 ritual review）

---

## References

- 審計報告：`docs/audit/2026-04-27-deep-audit.md`
- UI layout patterns 詳解：`docs/design/ui-layout-patterns.md`
- Deploy SOP：`docs/design/blueprint-v2-implementation-plan.md` §7.3
- 既有工程紀律：`docs/sop/implement_phase_step.md`

---

## Sign-off

- **Owner**: Agent-software-beta + nanakusa sora
- **Date**: 2026-04-27
- **Next review**: BS 完工後 + 累積 ~10 PR 後 review 哪幾條真的 catch 過 bug
