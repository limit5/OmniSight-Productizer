# B16 Part C — Accessibility Auditor 比對報告

> 撰寫時間：2026-04-18
> 對象 A：`configs/roles/web/a11y.skill.md`（OmniSight W5 a11y skill，135 行）
> 對象 B：agency-agents（MIT License）`testing/testing-accessibility-auditor.md`
> 目的：找出我方缺失 / 可吸收項，作為 B16 Part C 後續兩個 row
> （「補齊缺失項」+ 「同時適用於 W + V + P」）的實作依據。

---

## 1. 本 row 範圍

TODO B16 Part C 第一項：

- [ ] 比對 agency-agents 的 Accessibility Auditor 和現有 `web-a11y.md`

**只做比對**，不改動 `a11y.skill.md` 本體。後續補齊 / 跨 workspace 適用屬獨立 row。

---

## 2. 對象 A：`configs/roles/web/a11y.skill.md` 結構 & 既有覆蓋

| Section | 概述 | 行數 |
|---|---|---|
| frontmatter | role_id=a11y, category=web, priority_tools, trigger_condition | 1–11 |
| `## Personality` | 12 年資歷、Inclusive Design 三條信念（a11y is baseline / Semantic HTML > ARIA / Keyboard is the canary）、9 條「絕不做的事」 | 15–42 |
| `## 核心職責` | WCAG 2.2 AA / ARIA patterns / 鍵盤可達 / SR 相容 / 色彩對比 | 44–49 |
| `## WCAG 2.2 新增條款重點` | 2.4.11 Focus Not Obscured / 2.5.7 Dragging / 2.5.8 Target Size 24×24 / 3.2.6 Consistent Help / 3.3.7 Redundant Entry / 3.3.8 Accessible Auth | 51–58 |
| `## 作業流程` | 6 步：axe-core/pa11y → 手動鍵盤 → SR smoke → contrast → focus ring → simulate.sh | 60–66 |
| `## 品質標準` | Lighthouse ≥ 90 / axe 0 / target size / aria-live / label 關聯 | 68–75 |
| `## Success Metrics（驗收門檻）` | 13 條量化 checkbox（Lighthouse / axe / pa11y / WCAG 2.2 條款 / contrast / keyboard / focus / SR / img alt / form label / heading / landmark / L1） | 77–93 |
| `## Critical Rules` | 12 條 per-role 紅線（outline:none 替代 / div-as-button 三件套 / aria-label 分歧 / role=presentation 濫用 / 孤兒 input / tabindex>0 / modal 四件套 / 色彩單獨語意 / 自動輪播 / heading 跳級 / Lighthouse<90） | 95–108 |
| `## Anti-patterns` | 7 條禁用寫法 | 110–117 |
| `## 必備檢查清單` | 8 條 PR 自審 | 119–127 |
| `## Trigger Condition` | B15 lazy-loading hint | 129–135 |

**我方已覆蓋**：WCAG 2.2 AA 全條款 / POUR 意識（隱含於 Personality 三條信念）/ axe-core + pa11y + Lighthouse 三工具 / SR smoke（NVDA / JAWS / VoiceOver）/ contrast ratio / 鍵盤導覽 / modal focus trap / alt / label / heading / landmark / CLAUDE.md L1 合規 / B15 lazy-loading。

---

## 3. 對象 B：agency-agents Accessibility Auditor 結構 & 獨特資產

| Section | 概述 | 本地對應 |
|---|---|---|
| `## 🧠 Your Identity & Memory` | Role / Personality / Memory / Experience（含「passed Lighthouse 仍不可用」警語） | ≈ 我方 `## Personality` |
| `## 🎯 Your Core Mission` | 四大主軸：Audit Against WCAG / Test with AT / Catch What Automation Misses / Remediation Guidance；明列「automation 只抓 ~30%，其餘 70% 靠手動」 | 部分對應我方`## 核心職責` + `## 作業流程`，但**我方缺少「automation 30% ceiling」明確警語** |
| `## 🚨 Critical Rules You Must Follow` | 三大塊：Standards-Based Assessment / Honest Assessment / Inclusive Design Advocacy；含「'Works with a mouse' is not a test」/「custom widgets guilty until proven innocent」 | ≈ 我方 `## Critical Rules` 但角度不同：我方偏「程式碼紅線」，agency-agents 偏「審查態度紅線」 |
| `## 📋 Your Audit Deliverables` | 三份**可直接輸出的 markdown 模板**：① Accessibility Audit Report / ② Screen Reader Testing Protocol / ③ Keyboard Navigation Audit | **我方完全沒有**成品模板；Personality 雖說「我的輸出永遠長這樣」但未附可複製的 markdown skeleton |
| `## 🔄 Your Workflow Process` | 四步 workflow：Automated Baseline Scan（附 bash 指令） / Manual AT Testing / Component-Level Deep Dive / Report and Remediation | 我方 `## 作業流程` 是 6 條文字描述，**缺可直接執行的 CLI 片段** |
| `## 💭 Your Communication Style` | 五條固定句式範例（「The search button has no accessible name...」附 WCAG 條款 + 影響 + fix） | **我方沒有**溝通風格規範 — 可能造成 agent 輸出 PR comment 風格飄移 |
| `## 🔄 Learning & Memory` | 四類記憶主題（common failure / framework pitfalls / ARIA anti-patterns / what actually helps） | **我方沒有**「agent memory 主題指引」 |
| `## 🎯 Your Success Metrics` | 六條質性成功定義（「SR users complete critical journeys independently」） | 我方 `## Success Metrics` **量化上更嚴**（Lighthouse / axe / contrast 數值門檻），但**缺「critical journey 獨立完成」行為性 metric** |
| `## 🚀 Advanced Capabilities` | Legal/Regulatory（ADA / EAA / Section 508）/ Design System a11y / Testing Integration / Cross-Agent Collaboration | **我方沒有**法規清單；W5 Cross-Agent 協作點未明列 |

---

## 4. Gap Analysis — 我方缺失項（對照 TODO B16 Part C 其餘兩個 row）

### 4.1 TODO 明列的四個必補項目

| TODO 指定項 | 我方現況 | agency-agents 對應 | Gap 等級 |
|---|---|---|---|
| **focus order 驗證流程** | `## 作業流程` 第 2 步「手動鍵盤測試：Tab / Shift+Tab / Enter / Space / Escape / Arrow keys 覆蓋所有互動」— **有覆蓋面，無「流程化驗證步驟 + 結果記錄格式」** | `## 📋 Keyboard Navigation Audit` 模板：Global Navigation 7 條 checkbox + Component-Specific（Tabs / Menus / Carousels / Data Tables）分四類 pattern checkbox + 結果統計表（Total / Keyboard Accessible / Traps / Missing Focus Indicators） | **中** — 我方有原則、缺結構化產出 |
| **screen reader 測試腳本** | Personality 一句「VoiceOver / NVDA smoke test — 每個互動元件至少在一個 screen reader 上跑一次」+ 作業流程第 3 步 — **純描述、無腳本化測試步驟 & 結果紀錄** | `## 📋 Screen Reader Testing Protocol` 模板：Setup / Navigation Testing（heading / landmark / skip link / tab order / focus visibility）/ Interactive Component Testing（buttons / links / forms / modals / custom widgets）/ Dynamic Content Testing（live regions / loading / errors / toast）/ Findings 表格 | **高** — 我方完全缺測試腳本 |
| **色彩對比自動計算** | `## 作業流程` 第 4 步「Figma a11y 插件 + browser devtools contrast checker」+ Success Metrics 4.5:1 / 3:1 阈值 — **只有工具名 + 閾值，無自動化流程 / CI 整合 / 色票級 pre-commit check** | Advanced Capabilities → Design System a11y：「Establish accessible color palettes with sufficient contrast ratios across all combinations」（方向正確但無具體腳本） | **中** — 需自行補「CI 階段 tokens × text-size 笛卡兒積掃描腳本」 |
| **動態內容 ARIA live region 檢查** | `## 品質標準` 一句「動態內容變更有 aria-live region 或 focus management」 — **有提及、無檢查清單** | `## Screen Reader Testing Protocol → Dynamic Content Testing`：live regions / loading states / error messages / toast notifications 共 4 條具體檢查點 | **高** — 我方只有「該做」、缺「如何驗」 |

### 4.2 其餘 agency-agents 獨特資產（可選擇吸收）

| 獨特資產 | 建議 | 影響範圍 |
|---|---|---|
| **「Automation 30% ceiling」警語** | 建議吸收，放 `## Personality` 或 `## 核心職責` 開頭作為 agent self-calibration anchor | 純文字、低風險 |
| **Audit Report 模板** | 建議新增 `## Audit Deliverables` section，含三份 markdown skeleton（可直接作 agent 產出） | 新增 section，不動既有 |
| **Communication Style 五條句式** | 建議新增 `## Communication Style` section — 明確 PR comment / Gerrit review 回覆格式（WCAG 條款 + 影響 + fix） | 新增 section |
| **Cross-Agent Collaboration 清單** | 建議吸收為 `## Cross-Agent Observation`（呼應 CLAUDE.md Protocol B1 #209）— 指定 a11y 發現要 relay 給 `frontend-react` / `ui-designer` / `reporter/compliance` / `validator/security` 等 target_agent_id | 新增 section |
| **法規面向（ADA / EAA / Section 508）** | 建議點到為止加入 `## Regulatory Alignment` 一段 — 不展開，但讓 a11y skill 與 `reporter/compliance.skill.md` 交叉引用 | 新增 2-3 行 |
| **POUR 四原則明列** | 建議在 `## WCAG 2.2 新增條款重點` 前加一段「POUR（Perceivable / Operable / Understandable / Robust）」總覽，作 WCAG mental model | 新增 4 行 |
| **行為性 Success Metric（「SR user 獨立完成 critical journey」）** | 建議在 `## Success Metrics` 尾端追加一條 | 單行追加 |
| **Quick Wins vs. Architectural Changes 二分法** | 建議在 `## Communication Style` 裡併入「PR 對 reviewer 必要時建議二分 remediation priority」 | 隨 Communication Style |

### 4.3 agency-agents 有但**不建議吸收**的項目

| 項目 | 不吸收理由 |
|---|---|
| `Legal Compliance Checker` / `Cultural Intelligence Strategist` Cross-Agent | OmniSight 無對應 role skill；硬塞會造成 B1 #209 relay target 對不上 |
| `## 🔄 Learning & Memory` section 講「agent memory 主題」 | 我方用 auto-memory + HANDOFF.md 管實務記憶，不需要 role skill 自敘 |
| Emoji-heavy section titles（🧠 / 🎯 / 🚨 / 📋 / 🔄 / 💭 / 🚀） | 違反 OmniSight CLAUDE.md 預設「Only use emojis if the user explicitly requests it」+ 既有 49 檔 role skill 一致 plain heading；若吸收需剝除 emoji |
| `Your Identity & Memory` 的 Role / Personality / Memory / Experience 四欄表格式 | 我方 Personality section 已用散文式（背景故事 + 三信念 + 9 紅線），重寫成表格反而失去 narrative 力度 |

---

## 5. 對後續兩個 row 的具體建議（不在本 row 執行）

### Row 2（TODO 291）：補齊缺失項

建議插入位置與順序（不動既有 section）：

1. **`## 核心職責` 之後、`## WCAG 2.2 新增條款重點` 之前**
   新增 `## POUR Principles` 一小節（4 條）+「Automation 30% ceiling」警語一段
2. **`## 作業流程` 之後、`## 品質標準` 之前**
   新增 `## Keyboard Navigation Audit`（focus order 驗證結構化 checklist，含 Global / Tabs / Menus / Carousels / Data Tables 分組 + 結果統計表）
3. **接上新增 `## Screen Reader Testing Protocol`**
   含 Setup / Navigation / Interactive Component / Dynamic Content 四階段 + Findings 表格
4. **接上新增 `## Contrast Automation`**
   含（a）CI 階段自動掃描腳本規格 — 讀 `configs/web/*.tokens.json`（若存在）做 foreground × background × text-size 笛卡兒積 → WCAG AA 比對 → fail 時列出違規 pair；（b）build-time assertion 呼叫 `LIGHTHOUSE_MIN_A11Y` + axe-core ≥ 0；（c）Figma 稿階段的 Stark/axe plugin mandatory 記錄
5. **接上新增 `## Dynamic Content ARIA Live Region Checklist`**
   含（a）`aria-live="polite"` / `"assertive"` 選擇決策樹；（b）toast / loading / error / route change 四類場景各 1 份「正確實作 + 反例」snippet；（c）SR 手動驗證步驟
6. **`## Anti-patterns` 之後、`## 必備檢查清單` 之前**
   新增 `## Audit Deliverables`（三份 markdown 模板 — Report / SR Protocol / Keyboard Audit；agent 可直接複製產出）
7. **檔尾新增 `## Cross-Agent Observation`**（對齊 CLAUDE.md B1 #209，列出 target_agent_id 優先對象）

### Row 3（TODO 292）：跨 workspace 適用（W + V + P）

| Workspace | 吸收策略 |
|---|---|
| **W（Web）** | 主宿主，此檔本體即 W5 |
| **V（Visual workspace，前端設計稿審查）** | 建議在 `configs/roles/ui-designer.md` + `configs/roles/mobile-ui-designer.md` 的 Cross-Agent 區加一條：「設計稿 a11y 預審 → relay finding 給 a11y skill，target_agent_id=a11y」；或於 a11y.skill.md 新增 `## Visual Workspace Scope` 小節明確「Figma / 設計稿階段的 a11y pre-flight 走 `ui-designer` + `a11y` 雙角色」 |
| **P（Mobile a11y）** | 已有 `configs/roles/mobile/mobile-a11y.skill.md`（獨立 skill），不應把 web-a11y 擴成 mobile 超集（違反單一職責），改為雙向交叉引用：`web/a11y.skill.md` 的 `## Cross-Workspace Scope` 列「Mobile a11y 走 mobile/mobile-a11y.skill.md，本檔不跨足 Android/iOS 專屬 pattern（TouchTarget / TalkBack / Switch Control）」，並建議在 mobile-a11y.skill.md 對稱補一條反向指向 |

---

## 6. 結論

**我方 `a11y.skill.md` 的 WCAG 條款覆蓋、量化 Success Metrics、Critical Rules per-role 紅線密度，比 agency-agents 版本**更嚴謹**。

**缺口集中在「可複製的產出模板」與「結構化驗證腳本」三類**：
1. Keyboard / Screen Reader 測試 protocol（無 markdown template）
2. 色彩對比自動化掃描（無 CI 階段笛卡兒積 script spec）
3. 動態內容 ARIA live region 檢查決策樹（無 polite / assertive 場景對照表）

**吸收原則**：
- **只增不減** — 保留既有 Personality / Critical Rules / Success Metrics / Anti-patterns / Trigger Condition 全數 byte-identical
- **剝除 emoji** — agency-agents 的 🧠 / 🎯 / 🚨 等 emoji heading 違反 CLAUDE.md 預設，吸收時全部改 plain heading
- **OmniSight 上下文化** — 所有 CLI 腳本範例改指向 `scripts/simulate.sh --type=web` / `LIGHTHOUSE_MIN_A11Y` / `run_a11y_audit()` / `configs/web/*.tokens.json` 等本地 infra 名詞
- **Cross-Agent 連結點** — 指向既存 role skill（`frontend-react` / `ui-designer` / `mobile-ui-designer` / `mobile/mobile-a11y` / `reporter/compliance`），不發明不存在的 agent
- **不跨 mobile 職責** — P（Mobile a11y）由 `mobile/mobile-a11y.skill.md` 專職；本檔交叉引用而非吞併

**本 row 僅產出此比對報告，作為後續兩個 row 的實作依據。**
