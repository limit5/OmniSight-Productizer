# What is OmniSight? — Strategic Snapshot 2026-05-14

> **這是一份凍結在 2026-05-14 的快照**，不是隨時更新的 pitch deck。
>
> 內容反映**今天我們對「這個 product 到底是什麼」的理解與決策**。6 個月後再讀，差異本身就是資訊 — 不要 in-place 改寫，而是建立新的 dated snapshot 並 reference 這份。
>
> **Audience**: 接手的工程夥伴、6 個月後的自己、潛在投資人、任何想理解這個 product 的人。

---

## 0. TL;DR

**OmniSight 是一個「user idea → physical product」的整合製造平台**。

End user 的體驗是「我想做一支智慧手機」→ 系統處理機械設計、熱流模擬、合規認證、BOM、供應商、量產 ramp，最後出來一個能量產的實體產品。

我們**不發明新的 AI primitive**（Claude / GPT / Gemini 是別人的事），而是**把既有 primitive 整合到極致可靠**，在「**特定客群 + 特定領域**」上提供更專精的能力 — 從 Mobile skill pack 開始驗證。

當前狀態：**Layer 1 平台地基 + dev-runner 工程紀律**還在加固中，**user-facing skill pack 還在規劃**。願景很遠，但每個維度都在補。

---

## 1. The Vision

### 1.1 The end-user experience

> User：「我想做一支智慧手機。」
>
> Product：（一段時間後）「這是你的手機 — 規格、設計、BOM、合規報告、量產夥伴、第一批 prototype 已下單。」

不是 ChatGPT 給你建議，不是 Figma 給你 mockup — **是真的把實體產品做出來**。

End user 看到的 AI runner、governance engine、orchestration layer 都不重要 — 那些是 product 的**內部建造機制**，不是 product 本身。

### 1.2 Why physical, not pure software

純軟體 SaaS 市場已經夠擠：Lovable / v0 / Bolt / Replit / Cursor / Devin 在 ship 純軟體上做得夠好。**真正還沒人解決的是「實體產品的 idea-to-shelf 整合」**：

- 機構設計 → 模具廠 → 量產 ramp
- 電路設計 → BOM → 供應商 → SMT
- 合規認證 → 各國法規 → 上架
- UX / 包裝 / 用戶手冊
- 售後 / 維修 / 回收

每一段都有專門公司、專門軟體、專門 know-how。**沒有人在做整合**。OmniSight 的押注是：「**整合**」這件事本身是巨大的 value，AI 是 enable 整合的 enabler。

### 1.3 Why now

3 件事在 2024-2026 同時發生：

1. **LLM 達到工程 useful 水準** — Claude 4.x / GPT-5 / Gemini 2 級別可作生產工具，不只是 demo
2. **Multi-agent orchestration 開始穩定** — subscription model 讓 token cost 可預測，agent 可以長時間自主執行
3. **Specialized vertical AI 開始有 PMF 證據** — Cursor (coding) / Cognition (engineering) / Sierra (CX) 證明通用 ChatGPT 不是最終形態

OmniSight 的賭注：**vertical + integration + physical** 三者交集，市場還空著。

---

## 2. 七個維度

Product 的功能不能用一個 buzzword 涵蓋。它是一個**多維度系統**，每個維度都必須補足才能完成 idea→product 的閉環。

### 2.1 Platform primitives
多租戶 SaaS runtime、身份、計費、observability、governance、安全、Cron / watchdog、daemon supervision、cost ledger。

> **「基礎掛了 = 直接出局」** — 這個維度不能商量、不能 trade-off、不能 polish-later。

### 2.2 Domain skill packs
**Mobile** = 第一個（軟體成熟、純數位、最易驗證）。後續可能：醫療設備 / 工業 IoT / 穿戴 / 機器人 / 家電。

每個 pack 是一組 specialized agent + 知識庫 + workflow，**可單獨對應一個垂直市場**。

### 2.3 UI / UX
User 怎麼**輸入 idea**、怎麼**追進度**、怎麼**做決策**（哪個 BOM / 哪家代工 / 哪個外觀方案）、怎麼**處理意外**（合規 fail / 元件缺貨 / 模具壞）。

### 2.4 Compliance
FCC / CE / KCC / VCCI / SRRC / NCC… 各國認證流程、產品責任、智財排除、出口管制、RoHS / REACH / Prop 65 環保法規、隱私法規（個資 / GDPR / 兒少保護）。

### 2.5 Supply chain
元件來源、BOM 結構化、供應商整合（JLCPCB / LCSC / Mouser / Digi-Key / 在地廠商）、價格 + 交期 + MOQ、二次來源、EOL 風險管理。

### 2.6 Manufacturing
代工廠對接、CM 選型、品控標準、量產 ramp、產線優化、yield management、不良品處理流程。

### 2.7 Simulation / Verification
機構強度 / 熱流 / 電磁 / 可靠度 / 可製造性（DFM） — **產品下線前的虛擬驗證**，省掉昂貴的實體 iteration。

---

## 3. What This Is NOT

清楚知道「不是什麼」跟知道「是什麼」一樣重要。常見誤解：

### ❌ 不是「ChatGPT for product makers」
ChatGPT 是 conversation。OmniSight 是 **execution pipeline**。GPT 給建議，OmniSight 真的去下單、跑模擬、生 BOM、聯絡代工廠。

### ❌ 不是「Figma for hardware」
Figma 是**設計**工具，停在 design file。OmniSight 是 **idea-to-shelf**，設計只是第一段，後面還有合規、供應鏈、量產。

### ❌ 不是「Lovable for physical products」
Lovable 直接 ship 純軟體（end-to-end 數位）。OmniSight 處理的維度遠超軟體 — 包含合規、供應鏈、製造這些**有物理世界惰性**的部分。

### ❌ 不是「另一個 AI agent framework」
LangChain / AutoGen / CrewAI / OpenAI Agents SDK 是**通用 agent infra**。OmniSight 內部用 agent infra，但 **agent infra 不是 product** — product 是「輸出一個實體商品」這個 outcome。

### ❌ 不是「AI 全棧自動化」
反過來說，我們**不宣稱完全自動化**。Human in the loop 在每個維度都存在（合規簽核、製造商選擇、最終 BOM 確認、出貨核可）。AI 加速整合，不取代決策。

### ❌ 不是試圖打敗 Apple / 三星 / Foxconn
我們不做手機品牌、不做代工。我們做的是「**幫想做產品的人完成整合**」— 客戶可能是中小品牌、新創、企業內部創新部門、學校實驗室、KOL 自有品牌。**長尾市場**，不是 mass market。

---

## 4. The Three-Layer Architecture

> 詳細版本見：[`../architecture/2026-05-14-runner-pivot-and-three-layer-architecture.md`](../architecture/2026-05-14-runner-pivot-and-three-layer-architecture.md)

### Layer 1 — Platform Primitives
Runtime、governance、multi-tenant isolation、identity、billing、observability、cost ledger、Gerrit / JIRA 整合、Cron、watchdog、daemon supervision、credential management。

**這層的特性**：對 user 不可見、對 user-agent 不可見，但兩者都依賴。**這層掛了所有東西都跟著掛**。

### Layer 2a — Dev-Runner
內部工程工具，**用來建造 Layer 1 + Layer 2b 本身**。包含 4 個 runner（claude-1 / claude-2 / codex-1 / codex-2）、capability matrix、scheduler、pickup mutex、auto-resolve、自動 commit / push / review-respond / merge。

**這層的特性**：是**內部建造機制**，不是 product。對 user 不可見。對未來的 customer 也不會 sell。

> Today's strategic clarity：「Runner-as-product」過去是策略誤判 — 應該回到 dev-tool 的角色。詳見 strategic doc。

### Layer 2b — User-Agent
**Customer-facing skill packs**。Mobile 是第一個（純數位、軟體成熟、最易驗證）。

**這層的特性**：直接被 user 使用、產生 customer value、是 product 對外的「臉」。

### 三層的關係

```
        ┌──────────────────────┐
        │ User                 │
        └──────────┬───────────┘
                   │ uses
                   ▼
        ┌──────────────────────┐
        │ Layer 2b: user-agent │  ← 客戶看到的「臉」
        │  (skill packs)       │
        └──────────┬───────────┘
                   │ depends on
                   ▼
        ┌──────────────────────┐
        │ Layer 1: platform    │  ← 地基，掛了全部一起掛
        │  primitives          │
        └──────────▲───────────┘
                   │ builds (during dev only)
                   │
        ┌──────────┴───────────┐
        │ Layer 2a: dev-runner │  ← 內部建造機制
        │  (claude/codex bots) │
        └──────────────────────┘
```

- Layer 1 是**地基**。掛了，全部一起掛。
- Layer 2a 依賴 Layer 1，用來**建造 Layer 1 自己跟 Layer 2b**。
- Layer 2b 依賴 Layer 1，**不依賴 Layer 2a**（Layer 2a 只在開發階段在場）。
- Customer 只看到 Layer 2b。Investor 看到 Layer 2b 的 traction + Layer 1 的可靠度。

---

## 5. Survival Floor vs Growth Investment

7 個維度不是平均分配資源。**有些是地板（不過就出局）、有些是投資（多投多得）**。

### 5.1 Survival floor（硬地板）— 不能商量

- **Platform reliability** — Layer 1 掛了所有客戶一起掛，這是 game over
- **合規 baseline** — 法規違規可以一夕關門（特別是醫療、無線、跨境）
- **多租戶 isolation** — 一個客戶看到另一個的資料 = 全公司信任崩潰，產業 reputation 一輩子拉不回來
- **基本工程紀律** — code review / 不 force push main / secret 不進 repo / 災難恢復可演練 / 等等
- **核心安全姿態** — 不被勒索軟體癱瘓、不被竊取客戶資料、不被供應鏈攻擊

**這些不投資不會幫你贏，但不做就直接退賽。**

### 5.2 Growth investment（投資維度）— 可以選

7 個維度中除去地板部分，剩下的都是**可選 + 量級可調**的投資：

- 多投 manufacturing → 接更多代工廠 → 服務更大客戶
- 多投 simulation → 減少實體 iteration → 加速 ship
- 多投 skill packs → 開新垂直市場 → 擴大 TAM
- 多投 UX → 降低用戶門檻 → 攻長尾市場
- 多投 supply chain depth → BOM 優化 / 成本下降 → 毛利提升

每個維度的投資量級可以隨**市場 signal** 動態調整 — 哪邊有 traction 哪邊加碼，哪邊冷淡哪邊持平。

### 5.3 Good-enough threshold

**每個投資維度都需要一個「夠好門檻」**，否則 integration excellence 會變成 polish-forever，永遠覺得還沒準備好 ship：

| 維度 | Good-enough threshold（建議） |
|---|---|
| Runner reliability | runner-blocked rate < 5%（不需要 0%） |
| 多租戶 isolation | 通過 1 個外部 auditor + 0 leak incident（不需要 0 attack surface） |
| Mobile skill pack | 5 個 design partner 各自完成 1 個 app（不需要支援 10000 種 app） |
| Cost ledger | 連續 30 天 0 unexpected outflow（不需要密碼學級 audit） |
| Compliance | 在 1 個 vertical + 1 個地區可完整跑通（不需要全球全 vertical 全合規） |
| Simulation | 機構 + 熱流 + 電磁 三大項可跑（不需要 multiphysics 全到位） |

**達到 threshold → 停下 → 轉投下個維度**。這條紀律是抗 perfectionism 的核心。

---

## 6. Integration Excellence Over Invention

### 6.1 What we don't do

- **不發明新 model primitive** — Claude / GPT / Gemini 是 Anthropic / OpenAI / Google 的事。我們是消費者。
- **不追新模型 release** — 新模型出來不代表我們要立刻接。Stable > newest。我們的 model adapter 要設計成可換但不必常換。
- **不爭 benchmark** — MMLU / HumanEval / SWE-bench / GAIA 不是我們的指標。
- **不做通用 AI agent infra** — LangChain / AutoGen / CrewAI / OpenAI Agents SDK 已經做了，我們吃他們的飯。
- **不做通用 dev tool** — Cursor / Devin / Aider 已經做了。我們的 dev-runner 是內部使用，不對外賣。

### 6.2 What we do

- **把既有 primitive 整合到無聊地可靠** — 90% uptime 是 demo、99% 是產品、99.9% 是 platform、99.99% 是 moat
- **在 specialized domain 上做整合** — Mobile / 醫療 / 工業，不是「all things」
- **把 boring 做好** — error recovery / graceful degradation / observability / multi-tenancy / cost guard / 合規流程
- **把 unsexy 的整合工作做完** — 合規流程、供應鏈對接、製造商選型，這些沒人想做但人人需要
- **累積 domain knowledge** — 每個客戶教我們的 edge case 都進知識庫，下個客戶不必重學

### 6.3 Why this strategy is defensible

「整合精緻度 + 領域 know-how」是**累積型 moat**：

- 第 1 個客戶教我們 80% 的 case，第 100 個教我們剩下 20% 的 edge case
- 每多一個 skill pack，剩下的客戶 onboarding 變便宜（共享 Layer 1）
- 合規 / 供應鏈關係**不可移植** — 大廠也要重頭建
- 既有客戶的 switching cost 高（整合到他們的供應鏈、品管流程、合規檔案）

**對抗大廠的方式**：當 GPT-9 / Claude 7 / Gemini 4 出來，我們**不重寫產品**，只是**換掉 Layer 1 的 model adapter**。Layer 2b 不變、客戶不感、合規不變、供應鏈不變。

### 6.4 為何「無聊」是 feature

**創造性的失敗對 SaaS 比創造性的成功更貴**：

- 1 次 24 小時 downtime → 客戶信任崩 → 流失 30%
- 1 次 data leak incident → 法律訴訟 + 名譽損失 → 5 年補不回
- 1 次合規違規 → 罰款 + 禁售 → 整個 vertical 退出

「**不出事**」這件事在 SaaS 經濟學裡價值極高 — 因為**負面事件的成本是不對稱的**。所以「無聊地可靠」不是 boring，是**最理性的 strategy**。

---

## 7. The Bootstrap Self-Reference

### 7.1 Product builds itself

**Layer 2a（dev-runner）在 build Layer 1 + Layer 2b**。也就是 **product 在 build 自己**。

具體例子：
- Sprint Atlas（Family ⑩）有 22 個 ticket，目的是讓 Layer 2a runner 自己更可靠
- 這 22 個 ticket **被 runner 自己** pick up + 實作 + push 到 Gerrit
- 一個 runner 在改另一個 runner 的 code

### 7.2 為何這很危險

如果 Layer 1 不穩，那 Layer 2a 不穩，那 Layer 2a 改 Layer 1 的工作就有風險：

- runner 寫的 commit 把 platform 弄掛了 → runner 自己也跟著掛
- runner 自己掛了 → 沒法 rollback
- 需要人類緊急介入

→ **Layer 1 必須先穩定，才能放心讓 Layer 2a 自己改自己**。這就是為什麼今天的 Sprint Atlas 是當前最重要的工作。

### 7.3 為何這也是 strategic advantage

如果 bootstrap 成功，整個系統會進入**自我加速循環**：

- 每個 ticket 都教 runner 變更好
- 每次 runner 變好，下個 ticket 推得更快
- 6 個月後 runner 可能比 6 個月前快 5-10×
- 而且**這個能力直接 transfer 到 Layer 2b** — Mobile skill pack 用同一套 runner infra ship

但前提是：**Layer 1 沒掛**。

---

## 8. Where We Are Today (2026-05-14)

### 8.1 Working ✅
- 4 個 runner 並行 ship Gerrit Change（claude-bot ×2 + codex-bot ×2）
- JIRA → runner → Gerrit → main 自動化 pipeline 運作中
- ~70 個 Atlas ticket 規劃完成，等 Keystone（G.A-v1）做完就 unblock
- Anthropic prompt cache（90% off after turn 1）
- Cost ledger（80/100/120% tier alert）
- Gerrit submit-rule 6 區塊（dual-sign + release-cut-promote 等）
- 11 個 Foundation Rebuild phase spec（Sprint S12 規劃完成）

### 8.2 Fragile ⚠️
- Multi-runner contention（同 `.git` 不同 worktree 偶爾撞）— Atlas Family ⑩ v2-Ⅹ-2bc 修
- Watchdog → bridge heartbeat 機制（今天剛從 stale 狀態恢復）
- Prod 跟 dev 共用同一台 host（隔離不完整）
- Alembic 版本對齊跟 image 版本對齊（今天的 reboot 暴露的 drift）
- WSL2 強制重啟時 systemd graceful shutdown 不夠時間 → Atlas v2-⑧ 修

### 8.3 Not yet built ⛔
- Skill packs — 只有 Mobile 在規劃，code 還沒寫
- Customer onboarding — 還沒設計
- 合規 workflow — 還沒設計
- 供應鏈 integration — 還沒對接
- 製造 handoff — 還沒設計
- UI / UX — 還沒設計
- 計費 / 訂閱模型 — 還沒設計
- API runner（API 版 vs 訂閱版）— 在 R1/R2/R3 research phase 之前不啟用

### 8.4 Current sprint focus
**Sprint S12 + Atlas**：consolidate Layer 1 reliability + 工程紀律。預期 12-15 週完成 v2 contract（G.A-v2 + Family ⑩）。

之後才會進入 Mobile skill pack MVP（user-facing 第一個 deliverable）。

### 8.5 今天（2026-05-14）的關鍵事件

- 05:29 WSL2 host reboot → prod services 全掛 → 救火 + retrospective
- 策略反思：「Runner-as-product 是策略誤判」→ 寫成 strategic doc + 3-layer 架構正式化
- G.A-v2 Runtime Defense Contract spec v1.0 → v1.4（2 輪 codex 獨立 review）
- Sprint Atlas 建立 + 23 個 ticket 填入（22 + 1 meta）
- 27 條 blockedBy chain 串好 + 5 個 dangerous ticket 改成 class:operator-window
- 本文件本身（OP-1121）— 用來 canonicalize 上述策略對話

---

## 9. Common Questions

8 個會反覆被問的問題的 canonical 答案。下次有人問同樣的問題，**直接送這個 section**。

### 9.1 「這不就是另一個 AI agent framework 嗎？」

**A**：不是。

AI agent infra 是我們的**內部建造機制**，不是賣給客戶的東西。客戶看到的是「我想做手機 → 手機被做出來」這個 outcome，他不在乎背後是 LangChain 還是 OpenAI Agents SDK 還是 OmniSight 自製。

**類比**：LangChain 跟 OmniSight 的關係，類似 React 跟 Netflix 的關係。一個是工具，一個是用工具做出來的服務。問「Netflix 不就是個 React app 嗎」聽起來很奇怪 — 同樣，問「OmniSight 不就是個 agent framework 嗎」也是混淆抽象層級。

OmniSight 內部當然用 agent infra（就像 Netflix 內部用 React），但**這不是 product 的賣點**。

### 9.2 「跟 Lovable / v0 / Bolt 差在哪？」

**A**：他們 ship **純軟體**（end-to-end 數位、JS bundle 上 CDN）。我們 ship **實體產品**（要打模具、買元件、過合規、上量產線）。

差距是「**物理世界惰性**」 — Lovable 寫好 code 就 done，OmniSight 寫好 code 後才開始：

- 模具會壞、會磨損、會公差漂移
- 元件會缺、會 EOL、會被廠商 discontinue
- 供應商會 disappear、價格會漲、交期會延
- 認證會 fail、法規會改、檢測標準會升級
- 代工廠會跳票、yield 會差、品控會掉

**整合「會壞的物理世界」是核心難題**。純軟體領域沒有這些挑戰。所以 Lovable 的核心能力（code generation）只是 OmniSight 整個 pipeline 中的 1/20，剩下 19/20 是處理物理世界的 messiness。

### 9.3 「真的能做出智慧手機嗎？」

**A**：**今天還不能。** 願景跟當前能力有大差距。**我們不假裝可以**。

目標 path：

| 時間 | 能力 |
|---|---|
| 近期（6 個月） | Layer 1 穩定 + Mobile skill pack 能做出**第一個** app（純軟體 MVP） |
| 中期（12-18 個月） | 能做出**簡單 IoT 設備**（電路 + 機構 + 韌體 + 量產 50 件） |
| 長期（3-5 年） | 複雜消費電子（智慧手機級別） |

「智慧手機」是 north star，**不是 12 個月 deliverable**。中間有 100+ 個 milestone 要過。

但**北極星很重要** — 它告訴我們每個 milestone 該往哪個方向走、什麼能力是 nice-to-have、什麼是 must-have。

### 9.4 「為什麼不直接用 Claude / ChatGPT 就好？」

**A**：因為 Claude / ChatGPT **不會幫你下單 PCB、不會處理 FCC 認證、不會對接 Foxconn**。

他們是 primitive，OmniSight 是 **integration layer** — 把 primitive 跟物理世界 + 合規 + 供應鏈接起來，讓 primitive 的輸出真的變成一個能 ship 的產品。

**具體差別**：

- Claude 給你 BOM 建議是 5 分鐘的事。
- 但「**這個 BOM 真的能下單、價格合理、交期可接受、不違反出口管制、能被代工廠接受、品控有 SPEC、不良率有預期、二次來源已備**」是 5 個月的事。
- 那 5 個月才是 OmniSight 在做的工作。

問「為什麼不直接用 Claude」，類似於問「為什麼不直接用 Postgres，要用 Stripe？」— Postgres 是資料庫 primitive，Stripe 是**處理付款這個複雜業務的整合服務**。差距不是 capability 大小，是「**通用 vs 特定問題**」。

### 9.5 「大廠 copy 你怎麼辦？」

**A**：3 層防線：

1. **整合精緻度** — 大廠擅長發明 primitive，**不擅長 boring-but-reliable 的整合**（看看 Google Cloud vs AWS 的歷史 — Google 技術強多，但 AWS 的「無聊地可靠」贏了）
2. **領域 know-how** — 合規 / 供應鏈 / 製造的 expertise 不能 copy，要時間累積
3. **客戶的 switching cost** — 一旦客戶把他們的 BOM / CM 關係 / 合規檔案放進 OmniSight，搬家成本高

具體拆解：

- **Apple / Google / Microsoft 不會做這個** — 他們做自有產品、不做 enable-others（Apple 不會幫你做 non-Apple 手機，Google 不會幫你做 non-Google 服務）
- **OpenAI / Anthropic 不會做這個** — 他們做 model primitive，不做 vertical 整合（會 disrupt 自家 ecosystem 上下游）
- **會做的是另一個新創** — 到時候比的是**誰先建立 customer base + 累積 domain depth**

→ **race 是跟另一家新創比 execution speed**，不是跟大廠比 capability。

### 9.6 「12 個月的真實目標是什麼？」

**A**：

**承諾的目標**：

- Layer 1 reach 99.5% uptime（survival floor 達標）
- Mobile skill pack MVP（5 個 design partner 各 ship 1 個 app）
- 多租戶 isolation 通過 1 個外部 audit
- Pricing / 訂閱模型上線
- 一個合規 baseline 文件 + 1 個 vertical 的法規對接（建議 FCC for IoT）
- Operator team 從 1 人擴到 3 人

**不在 12 個月內承諾**：

- ❌ 智慧手機
- ❌ 醫療設備
- ❌ 量產線實際運作
- ❌ 自動化合規認證
- ❌ 跨境出貨
- ❌ 跨 vertical 多 skill pack

**為什麼這麼保守**：因為今天 Layer 1 還在加固（survival floor 還沒達標）。在 floor 之上加 ceiling 之前，**先把 floor 補牢**。一旦 Layer 1 穩了，後面每一步都能加速。

### 9.7 「為什麼選 physical product，不只做純軟體？」

**A**：3 個理由：

1. **市場空白** — 純軟體 SaaS 太擠，physical product 整合幾乎沒人做
2. **AI 增益最大** — 純軟體的 AI 助力主要在「寫程式快一點」（10× 加速）。Physical product 的 AI 助力在「**讓不可能變可能**」 — 一個人配合 AI 做出實體產品，本來需要一整隊工程師（100× 槓桿，不是 10×）
3. **moat 更深** — 純軟體 SaaS 容易被 GPT-N copy。Physical 整合需要 domain depth + 關係網路 + 合規檔案，這些是真的 moat

**反過來說 — 為什麼不只做純軟體**：

- 純軟體市場有 Lovable / v0 / Bolt / Replit / Cursor 等，competition 過於激烈
- AI 對「寫 code」這件事的加速，已經不是 differentiating advantage（人人都有）
- 進入 physical 領域，是用更困難的問題換更深的 moat

### 9.8 「為什麼特化，不通用？」

**A**：通用 = 所有人的 tool = **沒有任何一個客戶覺得「這是專為我做的」**。特化 = 某個 vertical 的 deep solution = **客戶離不開**。

**具體例子**：

> 「**AI 幫你做產品**」（通用） vs 「**AI 幫你做合規的醫療穿戴設備**」（特化）

後者：

- **客戶很清楚我們是不是適合他**（自我篩選降低 sales cost）
- **我們的 workflow / 知識庫可以深度為這個 vertical 量身做**
- **競爭對手**如果是通用 tool，輸給我們的 vertical depth；如果是同 vertical 特化 tool，要比 execution speed

Mobile 是 first vertical，**不是 only vertical**。但**先做透一個**，再開第二個 — 不要 7 個 vertical 一起做、每個都半成品。

**Sequencing**：
1. Mobile（純數位、軟體成熟、最易驗證）→ MVP 6-12 個月
2. IoT 設備（簡單電路 + 機構）→ 12-24 個月
3. 醫療穿戴（合規難度高、單價高）→ 24-36 個月
4. 工業 IoT / 機器人 → 36+ 個月

每個 vertical 的 launch 都複用 Layer 1 + 加一個 skill pack。

---

## 10. Glossary

- **Layer 1 / 2a / 2b** — 三層架構（Platform primitives / dev-runner / user-agent）
- **Skill pack** — 一組 specialized agent + 知識庫 + workflow，對應一個 vertical 市場
- **Survival floor** — 硬地板需求（不做就出局）
- **Growth investment** — 可選的投資維度（量級可調）
- **Good-enough threshold** — 每個維度的「夠好門檻」，達標就停（抗 perfectionism）
- **HDIR** — Hardware Design Intermediate Representation，未來的硬體設計中介表示法
- **Runner** — Layer 2a 的執行單位（claude-bot / codex-bot），自動 pick JIRA ticket → ship Gerrit Change
- **Atlas** — 當前 Sprint，consolidate Layer 1 reliability（Family ⑩ + others）
- **G.A-v1 / G.A-v2** — Foundation Rebuild 的兩個階段（v1 = keystone primitives；v2 = runtime defense contract）
- **dev-runner vs user-agent** — 內部工程工具 vs 對外客戶介面，**架構性區隔，不可混淆**
- **Foundation Rebuild** — Sprint S12 + Atlas 的合稱，consolidate Layer 1 + 工程紀律
- **subscription-claude / subscription-codex** — Runner 採用 LLM provider 的訂閱版（vs API 版），cost 可預測

---

## 11. Related Documents

- **3-Layer Architecture deep-dive**：[`../architecture/2026-05-14-runner-pivot-and-three-layer-architecture.md`](../architecture/2026-05-14-runner-pivot-and-three-layer-architecture.md)
- **G.A-v2 Runtime Defense Contract spec (v1.4)**：[`../sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md`](../sprint-s12/sprint-s12g-A-v2-runtime-defense-contract-spec.md)
- **Host reboot retrospective (今天的事件)**：[`../retrospectives/2026-05-14-host-reboot-image-db-drift.md`](../retrospectives/2026-05-14-host-reboot-image-db-drift.md)
- **Project memory (auto-memory, persists across Claude conversations)**：
  - `project_runner_pivot_strategic_reframe.md`
  - `project_s12g_A_v2_runtime_defense.md`
  - `project_overview.md`

---

## 12. How This Document Evolves

**Don't in-place edit**. 這份是 2026-05-14 的快照，未來理解變動時：

1. **建立新的 dated snapshot**：`docs/product/YYYY-MM-DD-what-is-omnisight.md`
2. **在新檔頂部 reference 舊檔**：「supersedes 2026-05-14 snapshot. Key changes: ...」
3. **保留舊檔** — 它是歷史證據，不是錯誤
4. **MEMORY.md 只指向最新一份**（其他份在 git history 中查得到）

未來可以從**最新一份 snapshot** derive 一份持續更新的 `docs/PRODUCT.md` 作為投資人 / 新人首頁 — 但 snapshot 本身永遠 frozen。

### 觸發新 snapshot 的條件

下列任一發生時，應該建立新 dated snapshot：

- 市場 positioning 變動（vertical 選擇、客戶 segment）
- 7 個維度的範圍變動（增 / 減 / 重新定義）
- 3-layer 架構重大調整
- Survival floor 定義變動
- Strategic stance（如「integration excellence over invention」）翻轉
- 重大策略誤判被識別並修正
- 12 個月目標重大調整

---

*Document version: snapshot-2026-05-14*
*Author: 2026-05-14 strategic discussion (operator + Claude pair, JIRA OP-1121)*
*Status: frozen — supersession via new dated snapshot only*
*Co-authored-by: claude-bot (sora.services) and global operator account*
