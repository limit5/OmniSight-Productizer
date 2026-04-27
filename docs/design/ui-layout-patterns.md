---
audience: internal
---

# OmniSight UI Layout Patterns — 解決反覆事故的 checklist

> **Status**: Living document, updated as new patterns emerge
> **Audit reference**: `docs/audit/2026-04-27-deep-audit.md` §3 P1.1
> **Background**: 30 天內 17 個 `fix(ui)` commit、5 個元件用同 pattern 各自踩坑（TokenUsageStats / ProviderRollup × 3 / SessionHeatmap header+tooltip / MERGER × 4 / LOCKS）。**根因是 CSS 慣用法沒成文、每個元件遇到都當新 bug 修**。本文檔是 single source of truth、寫給未來 component 設計 + PR review 用。

---

## 1. 為什麼會反覆事故 — 根因分析

### 1.1 CSS Grid 與 Flex 的 default 不直觀

**地雷 1 — CSS Grid items 預設 `min-width: auto`**

```css
.parent { display: grid; grid-template-columns: 1fr 1fr; }  /* ✗ 危險 */
.child { /* 沒設 min-width: 0 */ }
```

CSS Grid `1fr` 看起來是平均分配、實際上**等同 `minmax(auto, 1fr)`**。`auto` 是 child 的 min-content（最寬不可斷詞 token + padding + border），意思是「**子元素絕對不會 shrink 到比這個小**」。

Result：當子元素含長文字 / 寬 children，整個 column 會 push 過 `1fr` 配額、**overflow 到隔壁 column**。

**這是 R22.2 MERGER 漫進 LOCKS 的根因**。

**地雷 2 — Flexbox flex items 預設 `min-width: auto`**

跟 Grid 一樣的 default — `flex: 1` 無法 shrink 比 min-content 小。

**地雷 3 — `holo-glass` 父級的 `clip-path` + `backdrop-filter` 是 fixed-position 子元素的 containing block**

```css
.holo-glass {
  backdrop-filter: blur(10px);   /* ← 這個 */
  clip-path: polygon(...);        /* ← 跟這個 */
}

.modal-inside { position: fixed; inset: 0; }  /* ✗ 不是 viewport 大小 */
```

CSS spec：父級有 `transform` / `filter` / `backdrop-filter` / `will-change` / `perspective` / `clip-path` 任一，就會建立 `containing block for fixed descendants`。

Result：`fixed inset-0` 裡頭的 modal/drawer 不會貼齊 viewport，而會貼齊 holo-glass 父級的 bounding box。

**這是 PromptVersionDrawer 一開始排版崩壞的根因**（commit `0ed3a7af` 把 modal 改用 `createPortal` 貼到 `document.body` 才解，但又因為 syntax error 留下 c881bedf 那次事故）。

### 1.2 反覆事故的 5 個元件

| 元件 | 修 fix 次數 | 根因 |
|---|---|---|
| TokenUsageStats | 1 | header 內 metrics 沒 wrap、長 burn-rate 撞 token 數字 |
| ProviderRollup | 3 (v1 / v1a / v2) | 三組 metric stacking 反覆改、最終定為 2-row 不 truncate |
| SessionHeatmap header | 1 | 7d/30d tabs 沒 flex-wrap、push title 出界 |
| SessionHeatmap tooltip | 1 | 5 span 單行、orange cost 跑到 panel 外 |
| MERGER | 4 (R22 → R22.1 → R22.2 → R22.3) | grid-cols 固定 → 加 min-w-0 → 還是擠 → 改兩行佈局 |
| LOCKS | 1 | empty state 視覺真空、跟 MERGER 高度不對 |

5 個元件 / 4 個是 dashboard panel grid 的子元件、共用 holo-glass 容器、共用 metrics 排版需求。**沒有共用 BlockShell 抽象、各自寫 CSS 各自踩坑**。

---

## 2. 五大慣用 pattern — 每個 component 設計時的 checklist

### Pattern 1 — `min-w-0` 何時必要

**Rule**：**任何 CSS Grid item 或 Flex item 含可能 overflow 的子元素，wrapper 必加 `min-w-0`**。

```tsx
// ✗ 不安全
<div className="grid grid-cols-2 gap-2">
  <div>{longContentMaybeOverflow}</div>
</div>

// ✓ 安全
<div className="grid grid-cols-2 gap-2">
  <div className="min-w-0">{longContentMaybeOverflow}</div>
</div>
```

```tsx
// ✗ 不安全
<div className="flex gap-2">
  <div className="flex-1">{longContent}</div>
  <div className="shrink-0">{action}</div>
</div>

// ✓ 安全
<div className="flex gap-2">
  <div className="flex-1 min-w-0">{longContent}</div>
  <div className="shrink-0">{action}</div>
</div>
```

**判斷時機**：「**這個容器的子元素有可能變得比 column 寬嗎？**」如果可能、wrapper 必加 `min-w-0`。

### Pattern 2 — 三選一決策樹：truncate vs whitespace-nowrap vs shrink-0

當有「**重要文字**」（值 / 數字 / 必看的 metric）跟「**可省文字**」（label / 描述 / 二級資訊）並排時：

```
                      ┌─ Pattern decision tree ─┐
                      │                          │
有 column overflow 風險?
  │
  ├─ 否 → 不需特殊處理
  │
  └─ 是 → 哪個元素優先？
          │
          ├─ 重要文字（不可截斷）
          │  │
          │  └─ 加 `shrink-0 whitespace-nowrap tabular-nums`
          │     永遠保留完整顯示、不換行不縮
          │
          └─ 可省文字（可截斷）
             │
             ├─ 想省略尾巴 → 加 `truncate min-w-0`
             │   ("very-long-task-name-...")
             │
             └─ 想 wrap → 加 `min-w-0 flex-wrap`
                 多行顯示
```

**MERGER R22.3 的最終解**：label 用 `truncate min-w-0`、percentage 用 `shrink-0 whitespace-nowrap`，並排在同 row。當 panel 太窄、label 縮、percentage 完整保留 = 重要資訊永遠看得到。

```tsx
<div className="flex items-center justify-between gap-2 min-w-0">
  <span className="text-xs truncate min-w-0">{label}</span>
  <span className="font-bold tabular-nums shrink-0 whitespace-nowrap">
    {percentage}%
  </span>
</div>
```

### Pattern 3 — Two-line 佈局 vs Single-line shrink

```
                      ┌─ Layout decision ──────┐
                      │                          │
有 3+ 個並排元素？
  │
  ├─ 否（≤2 元素） → single-line + shrink rules（pattern 2）
  │
  └─ 是
      │
      ├─ 元素互相獨立（label / value / progress 三角關係）
      │   → **two-line layout**：
      │      line 1: label + value （flex justify-between）
      │      line 2: full-width progress bar
      │      → 讓 progress bar 永遠拿全寬
      │
      └─ 元素緊密耦合
          → 嘗試 single-line + 必要 shrink、如果不 work 升級 two-line
```

**MERGER R22.3 是這條 rule 的 case study**：label / percentage / bar 三件、single-line 永遠擠、改 two-line 給 bar 全寬。

### Pattern 4 — Container query for narrow panels

當 component 在 dashboard 多 panel 並列場景出現、無法保證 panel 寬度時：

```tsx
// ✓ Use container query if Tailwind v4+ 支援
<div className="@container">
  <div className="@sm:grid-cols-3 grid-cols-2">
    {/* narrow → 2 cols, wide → 3 cols */}
  </div>
</div>
```

OmniSight 用 Tailwind 4.x、支援 `@container` query。**新 component 設計時優先用 container query 而非 viewport query**（後者 不知道 component 在 dashboard 內被擠到多窄）。

### Pattern 5 — `holo-glass` 容器內的 fixed/absolute 子元素

**Rule**：**任何 modal / drawer / popover 子元素如果在 holo-glass 容器內，必走 `createPortal(..., document.body)`**。

```tsx
// ✗ 不安全 — fixed 會貼齊 holo-glass 父級
<div className="holo-glass-simple">
  <button onClick={() => setOpen(true)}>Open</button>
  {open && <div className="fixed inset-0 ...">Modal</div>}
</div>

// ✓ 安全 — fixed 貼 viewport
<div className="holo-glass-simple">
  <button onClick={() => setOpen(true)}>Open</button>
  {open && createPortal(
    <div className="fixed inset-0 ...">Modal</div>,
    document.body
  )}
</div>
```

**配套 SSR 防護**：
```tsx
if (typeof document === "undefined") return null
const node = (<div ...>...</div>)
return createPortal(node, document.body)
```

PromptVersionDrawer / TokenUsageStats / SessionHeatmap 都已修為這個 pattern。新 component 必須 follow。

---

## 3. PR Review checklist

任何修改 `components/omnisight/*.tsx` 或加新 component 的 PR、reviewer 對 5 個 pattern 各 check 一次：

- [ ] **Pattern 1**: 任何 grid/flex item 內含可能 overflow 子元素的 wrapper 加了 `min-w-0`？
- [ ] **Pattern 2**: 重要 vs 可省文字並排時、各自有對的 `shrink-0 whitespace-nowrap tabular-nums` 或 `truncate min-w-0`？
- [ ] **Pattern 3**: 3+ 元素並排時、考慮過 two-line vs single-line？選 two-line 的決策有寫進 component comment？
- [ ] **Pattern 4**: 當 component 在 dashboard 多 panel 並列場景、有用 container query (@container) 而非 viewport query 嗎？
- [ ] **Pattern 5**: 任何 modal/drawer/popover 子元素、有放進 createPortal 嗎？SSR 防護 (`typeof document === "undefined"`) 有嗎？

---

## 4. 既有 component 對齊狀況

audit 完每個元件對 5 pattern 的對齊：

| Component | Pattern 1 | Pattern 2 | Pattern 3 | Pattern 4 | Pattern 5 | 總體 |
|---|---|---|---|---|---|---|
| MERGER（R22.3 後） | ✅ | ✅ | ✅ two-line | N/A | N/A | ✅ |
| LOCKS（R23 後） | ✅ | ✅ | ✅ | N/A | N/A | ✅ |
| ProviderRollup（v2 後） | ✅ | ✅ | ✅ | ⚠ 用 viewport | N/A | 🟡 |
| TokenUsageStats（426d88b6 後） | ✅ | ✅ | ⚠ single-line | N/A | N/A | 🟡 |
| SessionHeatmap header / tooltip（dfa99158 後） | ✅ | ✅ | ✅ | N/A | N/A | ✅ |
| PromptVersionDrawer（c881bedf 後） | ✅ | ✅ | ✅ | N/A | ✅ portal | ✅ |
| **未 audit 的元件**（風險） | ? | ? | ? | ? | ? | 🔴 |

### 未 audit 的高風險元件（待補）

依高 churn 跟複雜度排序、應該下次 PR 時順手 audit：

1. `agent-matrix-wall.tsx` — agent list 的 status badges 高機率有 overflow risk
2. `task-backlog.tsx` — 多 metric column 可能 narrow panel 擠
3. `decision-dashboard.tsx` — 決策列表可能 long task title overflow
4. `pep-live-feed.tsx` — PEP toast 排版可能受 narrow panel 影響
5. `notification-center.tsx` — drawer 是 portal-based 但其他 nested 元件可能有問題
6. `orchestrator-ai.tsx` — chat 訊息有長 content / 嵌入 preview 等元素

---

## 5. 推薦的 BlockShell 共用 component（Future work）

過去 5+ 個元件都是 dashboard panel `holo-glass-simple` + corner-brackets-full + 內部 grid/flex 結構。可以抽出共用 `<BlockShell>`：

```tsx
// components/omnisight/_shared/block-shell.tsx (proposal)
interface BlockShellProps {
  title: string
  icon?: LucideIcon
  density?: "compact" | "comfortable" | "spacious"
  children: ReactNode
  empty?: boolean         // 空狀態 fallback ui
  emptyMessage?: string
  emptyIcon?: LucideIcon
}

export function BlockShell({ ... }: BlockShellProps) {
  // 統一 holo-glass-simple + corner-brackets-full 容器
  // 統一 title row（icon + label + 右側 actions slot）
  // 統一 empty state pattern（依 LOCKS 收尾的設計）
  // 統一 min-w-0 + overflow handling
}
```

**Benefit**：
- 5 個元件 collapse 進共用 component、CSS rule 一處改全套受惠
- 新元件繼承 default safe pattern、不需 reviewer 逐項 check 5 pattern
- empty state 視覺一致（reuse LOCKS R23 那套）

**Tradeoff**：
- 一次性 refactor 工作量 ~1.5d（5 元件對齊 BlockShell）
- abstraction 過早風險（如果各 component 需求差太多、強塞反而讓彈性差）

**建議**：BS 完工後做（每個 component 已穩定）、再來抽共用。現在強做反而 break 中。

---

## 6. References

- 審計報告 §3 P1.1：`docs/audit/2026-04-27-deep-audit.md`
- MDN CSS Grid `minmax(auto, 1fr)`: https://developer.mozilla.org/en-US/docs/Web/CSS/minmax
- CSS containing block spec: https://www.w3.org/TR/css-position-3/#containing-block
- Tailwind v4 container query docs
- 既有 commit history（5 個 component 的 fix 歷程）：
  - MERGER: `bba15721 → b66aa44f → 4a5a38d1 → 91563aec`
  - LOCKS: `f02d307d`
  - ProviderRollup: `2ee53fb0 → 14a09d85 → ...`
  - TokenUsageStats: `426d88b6`
  - SessionHeatmap: `0ed3a7af → dfa99158`
  - PromptVersionDrawer: `0ed3a7af → c881bedf`

---

## Sign-off

- **Owner**: Agent-software-beta + nanakusa sora
- **Date**: 2026-04-27
- **Next review**: BS 完工後、做 BlockShell refactor 時
