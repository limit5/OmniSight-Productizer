# AS.0.7 — Honeypot Field 設計細節

> **Created**: 2026-04-27
> **Owner**: Priority AS roadmap (`TODO.md` § AS — Auth & Security Shared Library)
> **Scope**: 釘住 OmniSight 自家 login / signup / password-reset / contact form 上線後 **honeypot field 的 5 個設計維度**——**rare field name + CSS hide + `tabindex=-1` + `autocomplete=off` + `aria-hidden`**——的精確 spec、跨 form context 的 field-name 命名空間、server-side validate helper 介面、與 AS.0.5 Phase 行為的互動、與 AS.0.6 三 axis bypass 的解耦語義、以及避免衝撞既有 form input name 的 drift guard。本 row 與 AS.0.5 共同 mitigate 設計 doc §3.5「JS 載入失敗 → 自動 fallback 到 honeypot + 慢速 rate limit」場景；AS.0.5 釘 phase / fallback chain timing、AS.0.6 釘 bypass mechanism、本 row 釘 honeypot 的 form-DOM-level invariant。
>
> **目標讀者**：(1) 寫 AS.4.1 `backend/security/honeypot.py`（實作 owner）+ `templates/_shared/honeypot/` TS twin 的人——本文件規範 server validate 接口、field name 取得 helper、shared-secret rotation cadence、audit row schema。(2) 寫 AS.6.3 OmniSight self login/signup/password-reset/contact form Turnstile + honeypot 後端 wire 的人——本文件規範 4 處 caller path 的 form-name namespace（login / signup / pwreset / contact 各自 prefix）+ bypass-flagged request short-circuit 順序。(3) 寫 AS.7.x frontend form 的人——本文件規範 hidden field 的 5 個 attribute 強制組合 + DOM render order + 為何不能改用「visible field 但 placeholder='leave blank'」這類退化方案。(4) 寫 AS.0.9 compat regression test #3 / #4 的人——本文件規範「bypass-flagged caller 不渲 honeypot field」的 form contract（test-token / api_key caller 走的 form-less endpoint vs full-form endpoint 的差別）。
>
> **不在本 row 範圍**：實際 `backend/security/honeypot.py` module（AS.4.1）+ TS twin、`templates/_shared/honeypot/` 落地、frontend form JSX / Tailwind class（AS.7.x）、honeypot 與 AS.4 helper 的 production lifespan validate（AS.4.1 落地時驗），以及 AS.6.4 admin Settings UI 的 `auth_features.honeypot_active` toggle (AS.0.2 已 land column / 本 row 不實作 toggle UI)。本 row 是 **plan-only design freeze**——下游 PR 必須遵守此處釘的 5 維 attribute 組合、namespace、validate semantic。

---

## 1. 為什麼必須有 honeypot 設計細節 design freeze

### 1.1 honeypot 是「次要層 + jsfail 終點」的雙身分

設計 doc §3.5 已釘「JS 載入失敗 → 自動 fallback 到 honeypot + 慢速 rate limit」；AS.0.5 §2.2 / §2.3 / §2.4 phase 行為矩陣三度引用 honeypot：

- **Phase 1/2 jsfail fallback chain 終點**（AS.3.5 fallback Turnstile → reCAPTCHA → hCaptcha → AS.4 honeypot）：widget 全 fail 時 honeypot + slow rate-limit 是 last-line defense，**過 → continue + audit `unverified_jsfail`**、**失敗 → block**。
- **Phase 3 jsfail 仍走 honeypot 而非直接 401**：fail-closed 嚴格只對「browser-context user 且 widget 成功 load 且 server verify 成功 return 而 score 低」這唯一情境 401；jsfail 仍走 honeypot 退場，避免 widget JS 故障鎖死 user。
- **任何 phase 的 form-renders-by-default 場景**：browser user 無論 widget pass / fail / jsfail，**form 渲染時 honeypot 永遠在 DOM 中**（與 widget 結果無關），server-side `_honeypot_validate()` 永遠先於 widget verify 跑——honeypot 是「主動 trap」而 widget 是「被動 challenge」。

「次要層」意味 honeypot 不阻擋人、只 trap bot 的低成本 layer；「jsfail 終點」意味 widget 全 fail 時 honeypot 是唯一還能跑的 anti-bot 機制。**這兩個身分對 honeypot 的設計約束不同**：次要層必須**對人零摩擦**（screen reader / keyboard / form-fill 都不能撞 honeypot），終點層必須**對 bot 高陷阱率**（field name / DOM 結構 / 行為 fingerprint 都要看起來像合法欄位）。AS.0.7 的 5 維 attribute 組合就是同時滿足這兩端的最小不變式。

### 1.2 為何不沿用「隨便加 hidden input」的 ad-hoc 寫法

OmniSight 既有 frontend 沒有 honeypot；若直接 ad-hoc 寫一條 `<input type="hidden" name="bot_check"/>`：

- **撞 form-fill / password manager**：Chrome / Safari autofill 看到 type=hidden 仍可能填入（取決於 heuristics），合法 user 變成「被 honeypot 抓到」假陽性；password manager 看到 `name=email` / `name=username` 類常見字會自動填，撞 honeypot 後 user 整 form 被拒登。
- **撞 screen reader**：純 `display:none` 不一定隱藏 ARIA tree，VoiceOver / NVDA 仍可能讀出「Email field」，視障 user 被引導去填 honeypot 觸發 ban。
- **撞 keyboard navigation**：tab 到 hidden field（取決於 browser）造成 focus trap、accessibility 退化。
- **撞既有 form name**：OmniSight 既有 form 控制 input 不用 `name` attribute（使用 React controlled-input），但 password manager 仍 fingerprint by `id` / `autoComplete` / `placeholder`；ad-hoc 取名 `email_confirm` / `phone` 等常見 word 會被 password manager 識為合法欄位。
- **沒有 server-side helper**：每個 form handler 各自 `if request.form.get("bot_check"): raise 400` → 5 處各自寫、5 處各自漂、其中一處忘了 = bypass；shared lib + 強制 helper interface 才能 enforce「所有 form path 都過 same gate」。
- **沒有 audit canonical 命名**：ad-hoc 寫法 audit row action 五花八門（`bot_caught` / `honeypot_hit` / `form_spam`），AS.5.1 dashboard 解析失敗、phase advance metric 算不準。

「rare field name + CSS hide + tabindex=-1 + autocomplete=off + aria-hidden」這 5 維是**經驗驗證可同時對人零摩擦 + 對 bot 高陷阱率**的最小組合，不是隨意取捨。本 row 釘住 5 維**全部強制**、缺一即視為 honeypot 失效——對應 §8 drift guard。

### 1.3 與 AS.0.6 三 axis bypass 的關係

AS.0.6 §2 釘三 axis bypass（A=API key / B=IP allowlist / C=test-token）「跳過 Turnstile + honeypot」的 invariant；§2 表格末「Skip honeypot: yes / yes / yes」對應到 honeypot 端必有「bypass-flagged request short-circuit return pass」的 helper hook。本 row §5 釘住 helper interface 與 short-circuit 順序。

**邊界 invariant**：

| Concern | AS.0.5 釘 | AS.0.6 釘 | AS.0.7 釘 |
|---|---|---|---|
| Phase 行為矩陣與 jsfail fallback timing | ✓ | — | — |
| Bypass mechanism storage / precedence | — | ✓ | — |
| Honeypot field 5 維 attribute spec | — | — | ✓ |
| Server-side helper interface | — | — | ✓ |
| 4 處 form path namespace（login/signup/pwreset/contact） | — | — | ✓ |
| 撞既有 form input name 的 drift guard | — | — | ✓ |
| Audit event `bot_challenge.jsfail_honeypot_*` schema | ✓（命名） | — | ✓（metadata schema 細化） |
| Bypass-flagged request → honeypot short-circuit | — | ✓（caller-side） | ✓（helper-side） |

任一 PR 動 honeypot 行為必同時滿足三文件條文。

---

## 2. 5 維 attribute 組合的精確 spec

### 2.1 維度 1：Rare field name（命名空間 + 衝突避免）

**核心設計**：每 form 的 honeypot field name 從固定的 **rare-word pool** 中選取、**per-form prefix 區隔**、**不撞既有 input name / autocomplete value / common form label**。

**Rare-word pool（候選列表，本 row 釘死）**：

```
# Pool A — 不在 password manager / autofill 常識字典中的英文罕用詞
# 篩選原則：
#   1. 不在 WHATWG autocomplete value spec
#   2. 不在 Chrome / Safari autofill heuristic 常用字
#   3. 不撞 OmniSight 既有 form input id / placeholder / autoComplete
#   4. 看起來像合法欄位（誘 bot 填）但人類不會誤填（CSS hide）
{
  "fax_office", "secondary_address", "company_role",
  "alt_contact", "referral_source", "marketing_pref",
  "newsletter_freq", "preferred_language", "fax_number",
  "secondary_email", "alt_phone", "office_extension",
}
```

**Per-form prefix 規則**：

| Form path | Prefix | 範例 field name |
|---|---|---|
| `/api/v1/auth/login` | `lg_` | `lg_fax_office`, `lg_alt_contact` |
| `/api/v1/auth/signup` | `sg_` | `sg_secondary_email`, `sg_referral_source` |
| `/api/v1/auth/password-reset` | `pr_` | `pr_alt_phone`, `pr_office_extension` |
| `/api/v1/auth/contact`（marketing 頁面 contact form） | `ct_` | `ct_fax_number`, `ct_marketing_pref` |

**為何要 prefix**：

1. **避免單一 field name 跨 form 重複**——bot scraper 看到 4 處 form 都有 `fax_office` 會 fingerprint 成 honeypot 全集，把該 field 加進「不要填」blacklist；prefix 讓四處 field name 對 bot 看起來各自獨立。
2. **server-side helper 容易由 path 推 expected name**——`_honeypot_field_name(path)` 從 path prefix 推 form prefix + 從 tenant_id seed 推 rare-word pool index、reproducible without state。
3. **audit 反向追蹤**——`bot_challenge.jsfail_honeypot_fail` audit row 帶 `metadata.honeypot_field_name=lg_fax_office` 直接看出來自哪 form。

**Field name selection algorithm**：

```python
# AS.4.1 backend/security/honeypot.py 必須匯出（plan §2.1 釘）
import hashlib

_FORM_PREFIXES = {
    "/api/v1/auth/login": "lg_",
    "/api/v1/auth/signup": "sg_",
    "/api/v1/auth/password-reset": "pr_",
    "/api/v1/auth/contact": "ct_",
}

_RARE_WORD_POOL = (
    "fax_office", "secondary_address", "company_role",
    "alt_contact", "referral_source", "marketing_pref",
    "newsletter_freq", "preferred_language", "fax_number",
    "secondary_email", "alt_phone", "office_extension",
)

def _honeypot_field_name(form_path: str, tenant_id: str, rotation_epoch: int) -> str:
    """Return the honeypot field name for a (form, tenant, epoch) triple.
    Pre-conditions:
      * form_path 在 _FORM_PREFIXES 中（4 處之一）
      * tenant_id 是 RLS context-bound
      * rotation_epoch 是 unix month index（int(time.time() // (30*86400))，每 30 天 rotate
    Returns:
      f"{prefix}{rare_word}" — e.g. "lg_fax_office"
    """
    if form_path not in _FORM_PREFIXES:
        raise ValueError(f"Unknown form path: {form_path}")
    seed = f"{tenant_id}:{rotation_epoch}".encode()
    idx = int(hashlib.sha256(seed).hexdigest(), 16) % len(_RARE_WORD_POOL)
    return _FORM_PREFIXES[form_path] + _RARE_WORD_POOL[idx]
```

**Rotation cadence**：每 30 天 epoch +1 → 重抽 rare word。理由：

- **不要太短**——bot 抓到 honeypot field name 後就把它加 blacklist 不再填；rotate 太勤（每天 / 每週）讓 bot 難以快取，但每次 rotate 必須 frontend + backend 同步刷，造成 deployment 複雜度。30 天是觀察窗 + 穩定窗的平衡。
- **不要太長**——bot 累積 N 個月可能用大數據統計推 form 結構；30 天 rotate 讓 fingerprint 失效週期 ≤ 30 天。
- **與 phase advance 解耦**——AS.0.5 phase advance 是 28 天觀察窗，honeypot rotation 是 30 天，兩個獨立 cadence；rotation 不影響 phase 計數（rotation 本身不寫 audit row）。
- **Epoch 是 deterministic 推導**——server / client 各自從 `int(time.time() // (30*86400))` 推同 epoch，不需協調 state；transition 邊界（month boundary）有 1-request grace（client 用 epoch N 算 field name、server 同時接受 epoch N 與 N-1），避免 clock skew 觸發 false-positive。

**為何 tenant_id 入 seed**：

- 多 tenant SaaS 場景：tenant A 與 tenant B 的 honeypot field name 各自獨立，bot 跨 tenant 攻擊時 fingerprint 失效；
- 但 tenant_id 是 PII-ish identifier，audit row metadata 必含 `honeypot_field_name`（如 `lg_fax_office`）但**不**含 raw tenant_id（per AS.5.1 隱私原則）；reverse lookup 走 audit context 的 `tenant_id` field（既有 schema），不靠 metadata。

**Drift guard**：rare-word pool 12 詞 immutable in plan；新增 / 刪除 word 必走新 plan PR + AS.4.1 lib 同步 + 7 天觀察窗（避免 frontend / backend 半同步狀態下 rotation 算錯）。

### 2.2 維度 2：CSS hide（visual hide 但保留可填）

**核心設計**：用 `position:absolute; left:-9999px; width:1px; height:1px; overflow:hidden;` 的「**off-screen positioning**」隱藏 honeypot field，**禁用** `display:none` / `visibility:hidden`。

**為何禁 `display:none`**：

- 部分自動化框架（Selenium / Playwright headless）會跳過 `display:none` element 不嘗試填寫——bot 看到 hidden 就跳 = honeypot 失效。
- 但反過來，較進階的 form-fill 工具會 detect `display:none` 並繞過——honeypot 反而變成「bot 不填 / 人不填」的死欄位。
- Off-screen positioning 對 bot 來說是「正常欄位、會填」、對人來說是「看不到、不會填」，**asymmetric trap**。

**為何禁 `visibility:hidden`**：

- Chrome autofill 不填 `visibility:hidden`，但 password manager（1Password / LastPass）heuristics 仍可能填——產生 false-positive。

**為何禁「色相同 background」這類「視覺隱形」trick**：

- 對視障 / 螢幕讀取器無效（仍會被讀出）。
- 對 high-contrast mode（Windows / macOS accessibility）失效，欄位會回到可見狀態。

**標準 spec（plan 釘死，frontend 必照搬）**：

```css
/* AS.0.7 §2.2 — honeypot field 必走 off-screen positioning */
.os-honeypot-field {
  position: absolute;
  left: -9999px;
  top: auto;
  width: 1px;
  height: 1px;
  overflow: hidden;
  /* 不要 display:none / visibility:hidden / opacity:0 */
}
```

**為何不直接 inline style**：方便 AS.7.x frontend Tailwind 同步、方便 dev mode preview（class toggle on/off）；但 production build 必走 critical-CSS inline，避免 CSS load fail 時 honeypot field 露出。

**Drift guard**：frontend template 必走 `class="os-honeypot-field"` + 不可改 inline style；CI 加 grep test 禁止 `display:none.*honeypot` / `visibility:hidden.*honeypot` / `opacity:0.*honeypot` 三組合。

### 2.3 維度 3：`tabindex="-1"`（keyboard navigation skip）

**核心設計**：所有 honeypot input 必含 `tabindex="-1"`，讓 keyboard tab 跳過此欄位。

**為何必加**：

- 視障 user / 鍵盤 user 用 Tab / Shift+Tab 在 form 間切換時，預設會 focus 所有 visible / focusable element；honeypot 雖然 off-screen positioned，**仍是 form input、仍 focusable by keyboard**——若沒有 `tabindex="-1"`，Tab 會跳到 honeypot field（看不到 focus 但 input 接收 keyboard event），下個 Tab 才到 password field；user 一臉茫然。
- `tabindex="-1"` 把 element 從 sequential keyboard navigation order 排除，但仍保留 programmatic focus（`element.focus()` 仍可），對 bot 而言「看起來像普通 disabled focusable field」、對人而言「Tab 直接跳過」。

**為何不用 `tabindex="0"` 或不寫**：

- `tabindex="0"`（default）= sequential focus，會 trap keyboard user。
- 不寫 = visible input 預設 sequential focus，同上問題。
- 不能用 `disabled` attribute 替代——`disabled` field 不會被 form submit、honeypot 變成「永遠 empty」、server 永遠 pass、形同虛設。

**Spec**：`tabindex="-1"` 必與 §2.2 CSS hide 同時存在，缺一視為 broken。

### 2.4 維度 4：`autocomplete="off"`（password manager / autofill skip）

**核心設計**：所有 honeypot input 必含 `autocomplete="off"` + 為避免 Chrome 忽略 `off`、額外加 fake autocomplete value `autocomplete="false"` （Chrome 對 unknown value 等同 off）。

**為何必加**：

- Chrome / Safari 有自動 autofill 機制——若 honeypot field name 不幸與某個 autofill heuristic 對齊（即使 §2.1 rare-word pool 已避免，但無法 100% 保證 future-proof），autofill 會自動填入 user 真實資料，造成 false-positive ban。
- `autocomplete="off"` 是 WHATWG spec 標準 hint，但部分 browser（特別是 Chrome）會忽略 `off`（基於 user-friendly 的 design choice）；改用 `autocomplete="false"` / `autocomplete="new-password"` 等 unknown / non-fillable value 是常見規避。
- Password manager（1Password、LastPass、Bitwarden）也會掃 form input、識別 password / username field 並提示 fill；honeypot 不可被識別。

**Spec**：`autocomplete="off"` + 同時 `data-1p-ignore="true"`（1Password）+ `data-lpignore="true"`（LastPass）+ `data-bwignore="true"`（Bitwarden）四 attribute 並用，覆蓋主流 password manager。

```html
<!-- AS.0.7 §2.4 — honeypot field 必走完整 ignore 組合 -->
<input
  type="text"
  name="lg_fax_office"
  class="os-honeypot-field"
  tabindex="-1"
  autocomplete="off"
  data-1p-ignore="true"
  data-lpignore="true"
  data-bwignore="true"
  aria-hidden="true"
  aria-label="Do not fill"
/>
```

**為何不用 `type="hidden"`**：

- `type="hidden"` 永不對 user 顯示、bot scraper 看到 hidden 會跳過——honeypot 失效。
- Honeypot 必須是 `type="text"`（看似合法 input），靠 §2.2 CSS hide 對人隱形、靠 §2.3 + §2.4 + §2.5 阻 form-fill / autofill / screen reader。

### 2.5 維度 5：`aria-hidden="true"`（screen reader skip）

**核心設計**：所有 honeypot input 必含 `aria-hidden="true"` + `aria-label="Do not fill"`（fallback context）。

**為何必加**：

- VoiceOver（macOS / iOS）/ NVDA（Windows）/ JAWS / TalkBack（Android）會讀出 form 中所有 input field 的 `<label>` / placeholder / aria-label——若沒 `aria-hidden`，screen reader user 會被引導去填 honeypot（即使 §2.2 CSS hide）；視障 user 觸發 ban、accessibility 違反 WCAG 2.1 AA。
- `aria-hidden="true"` 從 Accessibility Tree 完全移除此 element，screen reader 不讀。
- 額外加 `aria-label="Do not fill"` 是雙保險——若 future browser 改變 `aria-hidden` 解讀（已有 Chrome bug 報告），label 仍給 hint「不要填」。
- 注意：`aria-hidden="true"` 與 keyboard focus 不衝突（§2.3 tabindex=-1 才管 keyboard）；兩者各管各的。

**為何不用 `role="presentation"`**：

- `role="presentation"` 移除 semantic meaning 但保留 children——對 input 不適用（input 沒 children、role 也不適用）；正解是 `aria-hidden`。

**Spec**：`aria-hidden="true"` 必與 §2.3 + §2.4 同時存在；缺一視為 honeypot accessibility 退化、PR review 必拒。

### 2.6 5 維強制組合一覽表

| 維度 | Attribute | 缺 → 後果 | 對人影響 | 對 bot 影響 |
|---|---|---|---|---|
| 1 | rare field name + per-form prefix | 撞既有 input / autofill 命中 | autofill 撞、false-positive ban | bot 容易 fingerprint blacklist |
| 2 | `class="os-honeypot-field"` (off-screen CSS) | display/visibility hidden 被 Selenium / autofill 跳過 | 高 contrast mode 露出 | bot 看到 display:none 跳過 = 失效 |
| 3 | `tabindex="-1"` | 鍵盤 Tab 焦點 trap | keyboard user focus 卡 honeypot | n/a |
| 4 | `autocomplete="off"` + 三 ignore data-* | Chrome / password manager 自動填 | autofill 撞、false-positive | n/a |
| 5 | `aria-hidden="true"` + `aria-label` fallback | screen reader 讀出引導視障 user | 視障 user ban + WCAG 違反 | n/a |

**全 5 維必同時存在**——任一缺失 = honeypot 設計失效，§8 drift guard 必拒 PR。

---

## 3. Server-side validation helper interface（AS.4.1 spec）

### 3.1 Helper signature

```python
# AS.4.1 backend/security/honeypot.py 必須匯出（plan §3 釘）
from typing import Optional
from dataclasses import dataclass
from fastapi import Request

@dataclass(frozen=True)
class HoneypotResult:
    """Result of honeypot validation.
    Used by bot_challenge.verify() / 4 form caller paths.
    """
    pass_: bool                    # True = 過 honeypot（field 空 OR bypass）
    bypass_kind: Optional[str]     # 對應 AS.0.6 三 axis: "apikey" / "ip_allowlist" / "test_token" / None
    field_name_used: Optional[str] # form 路徑當下 epoch 的 expected field name (audit / metric)
    failure_reason: Optional[str]  # 若 pass_=False: "field_filled" / "field_missing_in_form" / "form_path_unknown"

def validate_honeypot(
    request: Request,
    form_path: str,
    tenant_id: str,
    submitted: dict[str, str],
) -> HoneypotResult:
    """Validate honeypot for a form submission.

    Pre-conditions:
      * request.state.api_key / request.state.bypass_test_token / request.state.bypass_ip
        already populated by AS.3.1 bot_challenge bypass short-circuit pre-step
      * form_path ∈ _FORM_PREFIXES (4 處)
      * submitted is the parsed form body (dict[str, str])

    Behavior（precedence high → low）:
      1. If request bypass-flagged (any of 3 axis) → return pass_=True with bypass_kind set
         * 不檢查 honeypot field — bypass-flagged caller 不 render form, submitted dict 可無此 key
      2. Compute expected_field_name = _honeypot_field_name(form_path, tenant_id, current_epoch)
         * 同時也算 prev_epoch_field_name（month boundary grace, AS.0.7 §2.1）
      3. If neither current_epoch_field_name NOR prev_epoch_field_name in submitted dict:
         → return pass_=False, failure_reason="field_missing_in_form"
         * 表示 frontend 沒 render honeypot — drift, server 拒絕（防 frontend 部署版本落後）
      4. If submitted[expected_field_name] (任一 epoch) is non-empty (after .strip()):
         → return pass_=False, failure_reason="field_filled"
         * Honeypot 中招
      5. Otherwise return pass_=True, bypass_kind=None, field_name_used=expected_field_name

    Side-effects: NONE (audit emit 由 caller 負責，per AS.0.6 §11)
    """
```

### 3.2 Caller responsibility

| Caller | 在 form path 流程哪個位置 |
|---|---|
| `backend/auth.py::login_handler` | 收到 form body 後、`_login_throttle()` 前先 call `validate_honeypot()`；fail → 401 + audit `bot_challenge.jsfail_honeypot_fail` + 5s response delay；pass + bypass → audit `bot_challenge.bypass_*` (per AS.0.6 §3) |
| `backend/routers/auth.py::signup_handler` | 同 login |
| `backend/routers/auth.py::password_reset_handler` | 同 login |
| `backend/routers/contact.py::contact_form_handler`（AS.6.3 future） | 同 login，但 fail → 200 + 假成功 response（不告訴 bot 失敗，對應 marketing form anti-spam 慣例）|

**4 處 caller 必走同 helper interface**——不可 inline rewrite；§8 drift guard 必 grep 4 處 caller 全 import `validate_honeypot`。

### 3.3 Bypass-flagged request → short-circuit 順序

per AS.0.6 §4 三 axis precedence A>C>B（最高 → 最低），bypass 判定**先於** `validate_honeypot()`：

```
flow:
  1. request enters handler
  2. AS.3.1 bot_challenge.verify() short-circuit:
     - axis A (api_key) hit → return BotChallengeResult.bypass(kind="apikey") + emit bypass_apikey audit
     - axis C (test_token) hit → return BotChallengeResult.bypass(kind="test_token") + emit bypass_test_token audit
     - axis B (ip_allowlist) hit → return BotChallengeResult.bypass(kind="ip_allowlist") + emit bypass_ip_allowlist audit
     - none hit → continue to widget verify path
  3. AS.4.1 validate_honeypot(request, ...):
     - request bypass-flagged (步驟 2 set request.state.bypass_kind) → return pass_=True, bypass_kind=<kind>
     - 不檢查 form 內 honeypot field （form-less caller 沒此 field）
  4. caller 繼續 password / MFA verify (per AS.0.6 §2 critical invariant: bypass 不繞 password)
```

**Short-circuit invariant**：bypass-flagged request 走 form-less endpoint（如 API key bearer caller 直接 POST JSON）時，submitted dict 不含 honeypot field name；validate_honeypot 必 step 1 `if bypass_kind: return pass` 之前不 check `field_missing_in_form`，否則 bypass caller 全 reject = AS.0.6 invariant 破。

### 3.4 audit row schema（refine AS.0.5 §3）

AS.0.5 §3 已釘 2 個 honeypot event：

```
EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_PASS = "bot_challenge.jsfail_honeypot_pass"
EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_FAIL = "bot_challenge.jsfail_honeypot_fail"
```

本 row **加** 1 個 event（保留 AS.0.5 已釘 2 個不動，補主動 path 對應）：

```python
# AS.4.1 backend/security/honeypot.py 必須額外匯出（plan §3.4 釘）
EVENT_BOT_CHALLENGE_HONEYPOT_PASS = "bot_challenge.honeypot_pass"        # 主動 form 走完 honeypot 過（非 jsfail 路徑也用）
EVENT_BOT_CHALLENGE_HONEYPOT_FAIL = "bot_challenge.honeypot_fail"        # 主動 form 走完 honeypot 失敗（非 jsfail 路徑也用）
EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT = "bot_challenge.honeypot_form_drift"  # field_missing_in_form 場景
```

**為何要加 form_drift event**：

- step 3 of helper 流程的 `field_missing_in_form` 失敗代表 frontend 部署版本落後（沒 render honeypot），不是 bot——把它與 `honeypot_fail` 分開命名，dashboard 可區分「真 bot」vs「frontend deploy drift」、ops 看 alert 才能正確診斷。
- form_drift event 必 audit row severity=`warn` + emit application log warning，作為 deploy gate 信號（連續 24h drift event ≥ N → block deploy 進階）。

**Schema 約束**（每個 event row 的 `metadata` JSONB 必含 fields）：

| Event | `metadata` 必含 |
|---|---|
| `honeypot_pass` / `honeypot_fail` | `form_path`（4 處之一）, `field_name_used`（current epoch field name e.g. `lg_fax_office`）, `epoch`（current 30-day epoch int）, `widget_action`（login / signup / pwreset / contact）|
| `jsfail_honeypot_pass` / `jsfail_honeypot_fail` | 同上 + `original_provider`（widget JS 故障時用的 provider）, `delay_ms`（5000 default per AS.0.5 §3）|
| `honeypot_form_drift` | `form_path`, `tenant_id`, `expected_field_names`（list of [current, prev_epoch] expected names）, `submitted_keys`（user 提交的 form keys，但 redacted password / sensitive fields）|

**注意**：metadata 不含 `field_filled_value` 即使 honeypot 中招——bot 填的 value 可能是 PII（autofill 填的真實 user email），audit row 不寫 raw value，只寫 length（`metadata.field_filled_length=int`），diagnostic 走 length pattern。

### 3.5 與 AS.0.5 phase metric denominator 的關係

per AS.0.5 §4 公式：

```
unverified_rate = COUNT(bot_challenge.unverified_*) / (
    COUNT(bot_challenge.pass) + COUNT(bot_challenge.unverified_*) + COUNT(bot_challenge.blocked_lowscore)
)
jsfail_rate = COUNT(bot_challenge.jsfail_*) / (...)
```

**本 row 釘**：

- `bot_challenge.honeypot_pass` / `honeypot_fail`（主動 path）→ **不**算 phase metric denominator（與 bypass 不同：bypass 是 by-design caller-kind 區分，honeypot 是 layer 區分；計入會雙重計數同一 request）；
- `bot_challenge.jsfail_honeypot_*`（jsfail terminal）→ **算** `jsfail_rate` 分子（per AS.0.5 §4 既有規範，jsfail_* 全 family 計）；
- `bot_challenge.honeypot_form_drift` → **不算**任何 metric denominator（drift 是 deploy-side 問題，非 user-side challenge outcome）；
- 月結報表（per AS.0.6 §6）的 honeypot 子表 totals 與 phase metric 分離計算。

---

## 4. 4 處 form path namespace 規範

### 4.1 Form path 與 honeypot field name 對應

| Form path | Form prefix | Frontend route | Backend handler | 備註 |
|---|---|---|---|---|
| `/api/v1/auth/login` | `lg_` | `app/login/page.tsx` (既有) | `backend/auth.py::login_handler` | 既有 controlled-input 不帶 `name`，新增 honeypot 時 `name=lg_<rare_word>` 是唯一帶 name 的 input |
| `/api/v1/auth/signup` | `sg_` | `app/signup/page.tsx`（AS.7.x 新建） | `backend/routers/auth.py::signup_handler`（AS.6.x 落地） | OmniSight 既有不開放 self-signup，AS.6 落地時釐清「signup 是否走 OAuth-only」決定本 form 是否落地 |
| `/api/v1/auth/password-reset` | `pr_` | `app/password-reset/page.tsx`（AS.7.x 新建） | `backend/routers/auth.py::password_reset_handler`（AS.6.x 落地） | 既有 password-reset email-token flow，form 落地時加 honeypot |
| `/api/v1/auth/contact` | `ct_` | `app/contact/page.tsx`（AS.7.x 新建 marketing 頁面） | `backend/routers/contact.py::contact_form_handler`（AS.6.3+ 新建） | OmniSight 自家 marketing site 的 contact form，bot 攻擊主流入口 |

**Form-less endpoint 不適用**：

- `/api/v1/livez` / `/api/v1/readyz` / `/api/v1/healthz` — health probe，沒 form。
- `/api/v1/webhooks/*` / `/api/v1/chatops/webhook/*` — signature-based，沒 form。
- `/api/v1/bootstrap/*` — server-rendered wizard，bot challenge 不適用（per AS.0.5 §4 bypass）。
- `/api/v1/admin/*` — RBAC-gated，已過 cookie session，不需 honeypot。
- API key bearer caller / OAuth callback — 不渲 form。

**4 處 form path 是 honeypot 唯一 wire 點**——AS.4.1 helper `_FORM_PREFIXES` 必恰 4 entries、§8 drift guard 必 assert `len(_FORM_PREFIXES) == 4` + key set 嚴格匹配。

### 4.2 既有 OmniSight form input name 衝突 grep

per AS.0.1 §7 表（AS.0.7 row）已標：「⚠️ honeypot 須避免衝撞既有 form field name；列 OmniSight 已有 form input names → grep 確認沒撞」。本 row §2.1 的 12 詞 rare-word pool 經以下 verification（2026-04-27 grep）：

```
$ grep -rn 'name="\([a-z_]\+\)"' app/ components/ 2>/dev/null | head -30
# 0 命中 — OmniSight 既有 frontend 全走 React controlled-input，不帶 name attribute

$ grep -rn 'autoComplete="\([a-z-]\+\)"' app/login/page.tsx
102:autoComplete="one-time-code"
236:autoComplete="email"
255:autoComplete="current-password"

$ grep -rn 'autoComplete="\([a-z-]\+\)"' app/bootstrap/page.tsx | sort -u
424,1408: autoComplete="email"
424: autoComplete="current-password"
439, 455, 1436, 1451: autoComplete="new-password"
1018, 1923, 2089, 2110, 2287, 2308, 2332: autoComplete="off"
```

**結論**：OmniSight 既有 form 用 React controlled-input、不帶 `name`，autoComplete value 全是 WHATWG spec 標準（`email` / `current-password` / `new-password` / `one-time-code` / `off`）；本 row §2.1 的 12 詞 rare-word pool（`fax_office` / `secondary_address` / ... / `office_extension`）**0 與既有 form 命中**——drift guard 通過。

未來新 form 若引入新 input name，必同 PR 過以下 grep：

```python
def test_as_0_7_honeypot_pool_no_collision_with_existing_forms():
    """AS.0.7 §4.2 invariant: rare-word pool 不撞 OmniSight 既有 form input name / autocomplete value / placeholder text."""
    import pathlib, re
    from backend.security.honeypot import _RARE_WORD_POOL
    frontend_files = pathlib.Path("app/").rglob("*.tsx")
    name_pat = re.compile(r'(?:name|id|autoComplete|placeholder)="([a-zA-Z_-]+)"')
    collisions = set()
    for f in frontend_files:
        text = f.read_text()
        for m in name_pat.finditer(text):
            v = m.group(1).lower().replace("-", "_")
            if v in _RARE_WORD_POOL:
                collisions.add((str(f), v))
    assert not collisions, f"AS.0.7 §4.2 honeypot pool collision: {collisions}"
```

### 4.3 與 `tenants.auth_features.honeypot_active` 的解耦

AS.0.2 alembic 0056 已 land `honeypot_active` boolean key 在 `tenants.auth_features` JSONB（既有 tenant 預設 false / 新 tenant 預設 true，per AS.0.2 §3）。本 row 不改 schema、不改 default；釘 runtime gate 行為：

| `auth_features.honeypot_active` | Honeypot 行為 |
|---|---|
| `false`（既有 tenant 預設） | 4 處 form path 不渲 honeypot field（frontend 略），server-side `validate_honeypot()` 直接 return `pass_=True, field_name_used=None`；不寫 honeypot audit row（與 AS.0.2 既有零行為變動 invariant 一致） |
| `true`（新 tenant 預設 OR existing tenant opt-in via Settings UI） | §2 5 維 attribute + §3 helper 全套 active |

**Per-tenant flip**：admin 在 Settings UI 切（AS.6.4 future row 落地）；flip 立即生效（next request），audit row `tenant.honeypot_active_flip` 記錄 actor + before/after。

**與 AS.0.5 phase 解耦**：phase advance 與 `honeypot_active` 兩個獨立 axis；phase 3 fail-closed tenant 仍可關 `honeypot_active`（雖然不建議——phase 3 + honeypot off 等同 widget verify 是唯一 layer，無 redundancy）；UI 顯示 warning。

**與 AS.0.8 single-knob 互動**：`OMNISIGHT_AS_ENABLED=false` 時，整 honeypot module noop（`validate_honeypot()` 直接 return passthrough），不查 `honeypot_active`，per AS.0.5 §7.2 解耦 invariant。

---

## 5. Frontend / TS twin 對齊（AS.4.1 TS twin spec）

### 5.1 Twin invariant

per 設計 doc §3 雙 twin pattern，`templates/_shared/honeypot/` 必與 `backend/security/honeypot.py` 行為一致：

| Symbol | Python (`backend/security/honeypot.py`) | TypeScript (`templates/_shared/honeypot/index.ts`) |
|---|---|---|
| Field name fn | `_honeypot_field_name(form_path, tenant_id, epoch)` | `honeypotFieldName(formPath, tenantId, epoch)` |
| Form prefixes | `_FORM_PREFIXES: dict[str, str]` 4 entries | `FORM_PREFIXES: Record<string, string>` 4 entries |
| Rare-word pool | `_RARE_WORD_POOL: tuple[str, ...]` 12 entries | `RARE_WORD_POOL: readonly string[]` 12 entries |
| CSS class | `OS_HONEYPOT_CLASS = "os-honeypot-field"` (constant) | `OS_HONEYPOT_CLASS = "os-honeypot-field"` (constant) |
| Validate fn | `validate_honeypot(...)` | n/a (server-only) |
| Render helper | n/a | `<HoneypotField formPath={...} tenantId={...} />` JSX component |

**Sync invariant**：12 rare words + 4 form prefixes 必雙邊一致，drift guard 必跨 lang 比對：

```python
def test_as_0_7_python_ts_twin_pool_aligned():
    """AS.0.7 §5.1 invariant: Python pool 與 TS twin pool 必一致."""
    import json, pathlib
    from backend.security.honeypot import _RARE_WORD_POOL, _FORM_PREFIXES
    ts_index = pathlib.Path("templates/_shared/honeypot/index.ts").read_text()
    # 從 TS 檔案 extract pool 與 prefixes
    py_pool = set(_RARE_WORD_POOL)
    ts_pool = _extract_array(ts_index, "RARE_WORD_POOL")
    assert py_pool == ts_pool, f"AS.0.7 §5.1 pool drift: py={py_pool} ts={ts_pool}"
    py_prefs = dict(_FORM_PREFIXES)
    ts_prefs = _extract_object(ts_index, "FORM_PREFIXES")
    assert py_prefs == ts_prefs, f"AS.0.7 §5.1 prefix drift: py={py_prefs} ts={ts_prefs}"
```

### 5.2 React component contract（AS.7.x frontend 落地時 wire）

```tsx
// templates/_shared/honeypot/HoneypotField.tsx — AS.4.1 TS twin spec
import { useEffect, useState } from "react";
import { honeypotFieldName, OS_HONEYPOT_CLASS } from "./index";

interface HoneypotFieldProps {
  formPath: keyof typeof FORM_PREFIXES;
  tenantId: string;
}

export function HoneypotField({ formPath, tenantId }: HoneypotFieldProps) {
  const [fieldName, setFieldName] = useState<string>("");
  useEffect(() => {
    // Compute epoch on mount (deterministic from clock)
    const epoch = Math.floor(Date.now() / (30 * 86400 * 1000));
    setFieldName(honeypotFieldName(formPath, tenantId, epoch));
  }, [formPath, tenantId]);
  if (!fieldName) return null;
  return (
    <input
      type="text"
      name={fieldName}
      className={OS_HONEYPOT_CLASS}
      tabIndex={-1}
      autoComplete="off"
      data-1p-ignore="true"
      data-lpignore="true"
      data-bwignore="true"
      aria-hidden="true"
      aria-label="Do not fill"
      defaultValue=""
    />
  );
}
```

**5 維 attribute 必出現在 JSX 中**（grep able）；React.memo / 自動 prop spreading 等優化禁用——清楚顯式 attribute 才能讓 reviewer / drift guard 一眼看出 5 維是否齊。

---

## 6. Acceptance criteria

### 6.1 AS.4.1 落地時必過

- [ ] `backend/security/honeypot.py` export 5 個常數 + 1 個 helper：`_FORM_PREFIXES` (dict, 4 entries) / `_RARE_WORD_POOL` (tuple, 12 entries) / `OS_HONEYPOT_CLASS` ("os-honeypot-field") / `validate_honeypot(...)` / `_honeypot_field_name(...)` + 3 個 EVENT_* 常數（§3.4）
- [ ] `templates/_shared/honeypot/index.ts` + `HoneypotField.tsx` 雙 twin 對齊 §5.1 三條 invariant
- [ ] 4 處 caller path（login / signup / password-reset / contact）全 import `validate_honeypot`、無 inline rewrite（per §3.2）
- [ ] §8 drift guard 6 條 test 全綠
- [ ] HANDOFF.md AS.4.1 row `Production status: deployed-inactive`、`Next gate: deployed-active when 第一個 tenant 切 auth_features.honeypot_active=true 觸發 form-side wire 觀察`

### 6.2 AS.6.3 OmniSight self form 接 honeypot 落地時必過

- [ ] 4 處 caller 在 controller body 第一行（auth/throttle 之前）call `validate_honeypot()`
- [ ] honeypot fail → return 401 (login/signup/pwreset) / 200-faux-success (contact) + 5s response delay (per AS.0.5 §3 jsfail_honeypot_* metadata.delay_ms)
- [ ] frontend form 4 處全 mount `<HoneypotField>` JSX 元件（grep `<HoneypotField` 必 ≥ 4）
- [ ] 5 維 attribute 全在 rendered HTML 中（headless browser test 抓 DOM 驗 5 attr 全到位）
- [ ] critical CSS inline `.os-honeypot-field` rule（per §2.2 build invariant）

### 6.3 AS.0.9 compat regression test 第 6 顆（建議新增）

- [ ] 既有 password user 在 `honeypot_active=false` tenant：login form 不渲 honeypot field、submit 不帶相關 key、server pass（既有 zero-行為-變動 driver）
- [ ] 既有 password user 在 `honeypot_active=true` tenant：form 渲 honeypot、submit 不填、server pass + audit `bot_challenge.honeypot_pass`
- [ ] API key bearer caller (AS.0.6 axis A) → form-less endpoint，submit JSON 不含任一 honeypot key、server pass + audit `bot_challenge.bypass_apikey`
- [ ] test-token caller (AS.0.6 axis C) 在 form-rendering endpoint：渲 honeypot 但 server bypass-flagged short-circuit 不檢查 honeypot field、audit `bot_challenge.bypass_test_token`
- [ ] `OMNISIGHT_AS_ENABLED=false`：honeypot module noop、所有 form path 不渲 field、server pass + 不寫 honeypot audit row

---

## 7. 非目標 / 刻意不做的事

1. **不引入「visible field but placeholder='leave blank'」這類退化 honeypot** — 此 pattern 對 user 不友善（看見欄位疑惑）、對 bot 反而易識別（placeholder 內容是常見 honeypot 信號）；本 row §2 5 維強制全用，不退化。
2. **不引入 multi-field honeypot**（同 form 多 honeypot input） — 增加 frontend / backend 工作量、增加 false-positive 風險（autofill 撞 N 倍）、phase metric 計數複雜化；單 form 單 honeypot field 已足。
3. **不引入「動態變更 honeypot 位置」**（form DOM 內位置 randomize） — 增加 frontend rendering 不確定性 + screen reader 體驗變差；位置固定（form 末尾、submit 按鈕之前），靠 §2.1 30-day rotation 對抗 fingerprint。
4. **不引入「JS 計算 expected field value 並比對」** — 進階 bot 會 simulate JS、easy bypass；server 只 check 「field 為空 OR 不存在於 submitted dict」，不引入 client-side computed challenge。
5. **不引入「honeypot 失敗後 ban IP」** — IP-level ban 容易誤封 NAT 後企業 user / Cloudflare WARP user；honeypot fail 走 5s delay + 該 request 401 + audit warn，不對 IP 持久 ban；rate-limit 仍走 K6 既有 per-IP throttle。
6. **不引入 honeypot opt-out per user** — 用戶不能個別關自己的 honeypot；tenant admin flip `honeypot_active` 是唯一控制點。
7. **不規範 mobile app native client 的 honeypot** — OmniSight 沒 native mobile app；future iOS/Android client 必走 API key (AS.0.6 axis A bypass)，不需 honeypot；此 plan 不展開 native form scenario。
8. **不規範 SSR-rendered form 的 honeypot field name** — OmniSight 既有 form 全 client-rendered (Next.js client component / `"use client"`)；future SSR form 出現時必擴 plan、`epoch` 改 server-render-time 算（避免 SSR / hydration mismatch）。

---

## 8. Drift guards

### 8.1 5 維 attribute 全到位 guard

```python
def test_as_0_7_honeypot_jsx_5_attrs_present():
    """AS.0.7 §2 invariant: 4 處 caller form 渲 HoneypotField 必含 5 維 attribute."""
    import pathlib, re
    expected_attrs = (
        r'tabIndex=\{-1\}',
        r'autoComplete="off"',
        r'data-1p-ignore="true"',
        r'aria-hidden="true"',
        r'className=\{OS_HONEYPOT_CLASS\}|className="os-honeypot-field"',
    )
    twin = pathlib.Path("templates/_shared/honeypot/HoneypotField.tsx").read_text()
    for pat in expected_attrs:
        assert re.search(pat, twin), f"AS.0.7 §2 missing attr: {pat}"
```

### 8.2 4 處 caller import helper guard

```python
def test_as_0_7_callers_use_helper():
    """AS.0.7 §3.2 invariant: 4 處 form caller 必 import validate_honeypot."""
    import pathlib
    callers = (
        "backend/auth.py",
        "backend/routers/auth.py",
        "backend/routers/contact.py",
    )
    found = 0
    for c in callers:
        p = pathlib.Path(c)
        if p.exists() and "validate_honeypot" in p.read_text():
            found += 1
    assert found >= 3, "AS.0.7 §3.2 至少 3 處 caller 必 import validate_honeypot"
```

### 8.3 Rare-word pool 不撞既有 form input guard

見 §4.2 末 `test_as_0_7_honeypot_pool_no_collision_with_existing_forms()`。

### 8.4 Python ↔ TS twin 對齊 guard

見 §5.1 末 `test_as_0_7_python_ts_twin_pool_aligned()`。

### 8.5 Form-prefix 4 entries 不變 guard

```python
def test_as_0_7_form_prefixes_locked():
    """AS.0.7 §4 invariant: 4 處 form path 嚴格不變."""
    from backend.security.honeypot import _FORM_PREFIXES
    expected = {
        "/api/v1/auth/login": "lg_",
        "/api/v1/auth/signup": "sg_",
        "/api/v1/auth/password-reset": "pr_",
        "/api/v1/auth/contact": "ct_",
    }
    assert _FORM_PREFIXES == expected, f"AS.0.7 §4 prefix drift: {_FORM_PREFIXES}"
```

### 8.6 CSS hide 必走 off-screen positioning（禁 display:none）guard

```python
def test_as_0_7_css_hide_uses_offscreen_positioning():
    """AS.0.7 §2.2 invariant: .os-honeypot-field 必走 off-screen positioning，禁 display:none."""
    import pathlib, re
    css_files = list(pathlib.Path("app/").rglob("*.css")) + list(pathlib.Path("templates/_shared/honeypot/").rglob("*.css"))
    found_hide = False
    for f in css_files:
        text = f.read_text()
        if ".os-honeypot-field" in text:
            found_hide = True
            block = re.search(r"\.os-honeypot-field\s*\{[^}]*\}", text, re.DOTALL)
            assert block, f"{f}: .os-honeypot-field block not parseable"
            body = block.group()
            assert "position: absolute" in body or "position:absolute" in body, (
                f"{f}: 必含 position:absolute"
            )
            assert "left: -9999px" in body or "left:-9999px" in body, (
                f"{f}: 必含 left:-9999px"
            )
            assert "display: none" not in body and "display:none" not in body, (
                f"{f}: 禁 display:none"
            )
            assert "visibility: hidden" not in body and "visibility:hidden" not in body, (
                f"{f}: 禁 visibility:hidden"
            )
    assert found_hide, "AS.0.7 §2.2: 至少一個 CSS 檔必定義 .os-honeypot-field"
```

### 8.7 Audit event canonical 命名 guard

```python
def test_as_0_7_event_names_canonical():
    """AS.0.7 §3.4 invariant: honeypot 相關 audit event 必走常數匯出，禁 inline 字串."""
    import pathlib, re
    from backend.security.honeypot import (
        EVENT_BOT_CHALLENGE_HONEYPOT_PASS,
        EVENT_BOT_CHALLENGE_HONEYPOT_FAIL,
        EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT,
    )
    assert EVENT_BOT_CHALLENGE_HONEYPOT_PASS == "bot_challenge.honeypot_pass"
    assert EVENT_BOT_CHALLENGE_HONEYPOT_FAIL == "bot_challenge.honeypot_fail"
    assert EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT == "bot_challenge.honeypot_form_drift"
    inline_pat = re.compile(r'["\']bot_challenge\.honeypot[_a-z]*["\']')
    src = ["backend/security/honeypot.py", "backend/auth.py", "backend/routers/auth.py"]
    for path in src:
        p = pathlib.Path(path)
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if 'EVENT_BOT_CHALLENGE_HONEYPOT_' in line and '=' in line:
                continue
            assert not inline_pat.search(line), (
                f"{path}: bot_challenge.honeypot_* 必走常數 (line: {line.strip()})"
            )
```

---

## 9. 與 AS.0.x / AS.x 其他 row 的互動 / 邊界

| 互動對象 | 邊界 |
|---|---|
| **AS.0.1 §7 表 AS.0.7 row** | 本 row 是「⚠️ honeypot 須避免衝撞既有 form field name」的解決——§4.2 已做 grep verification |
| **AS.0.2** `tenants.auth_features.honeypot_active` | **強耦合**——本 plan §4.3 規範 runtime gate（false → noop / true → 5 維 attribute 全套）；schema 不改、default 不改（既有 false / 新 tenant true 沿用 AS.0.2 既定）|
| **AS.0.3** `users.auth_methods` + account-linking | 無交集——honeypot 是 form-DOM-level、與 user-level merge 無關 |
| **AS.0.4** credential refactor migration plan | 無交集 |
| **AS.0.5** Turnstile fail-open phased strategy | **強耦合**——§3.5 jsfail terminal layer / §3 audit event family（jsfail_honeypot_pass / jsfail_honeypot_fail 由 AS.0.5 釘 / honeypot_pass / honeypot_fail / honeypot_form_drift 由本 row 加）/ §3.5 phase metric denominator semantic 一致 |
| **AS.0.6** automation bypass list | **強耦合**——§3.3 short-circuit 順序 + bypass-flagged request 不檢查 honeypot field invariant；三 axis 都 skip honeypot 由本 row helper 端 enforce |
| **AS.0.8** `OMNISIGHT_AS_ENABLED` single-knob | **強耦合**——knob false 時 honeypot module noop（與 AS.0.5 §7.2 解耦語義一致，不查 `auth_features.honeypot_active`）|
| **AS.0.9** compat regression test suite | **強耦合**——建議新增第 6 顆 critical (§6.3)：honeypot off / on / bypass / single-knob 4 場景 |
| **AS.0.10** auto-gen password core lib | 無交集 |
| **AS.1** OAuth client core | 弱耦合——OAuth callback flow 不渲 form（callback 是 redirect-based），但 OAuth-init form（按「Sign in with Google」按鈕的頁面）若有 form 也走 §2 5 維（建議：OAuth-init 走 button-only、無 form、不需 honeypot）|
| **AS.2** token vault | 無交集 |
| **AS.3.1** `bot_challenge.py` module | **強耦合**——§3.3 short-circuit 走 `bot_challenge.verify()` 先於 `validate_honeypot()`；event 命名 family 與 AS.0.5 §3 / AS.0.6 §3 同一 namespace |
| **AS.3.4** server-side score verification | 無交集——honeypot 在 widget verify 之前 |
| **AS.3.5** provider fallback chain | **強耦合**——chain 終點走 honeypot（per AS.0.5 §3.5 + §5）；本 row 規範終點 honeypot 細節 |
| **AS.4.1** Honeypot helper（**本 row 是 AS.4.1 的 spec source**） | 雙 twin / helper interface / EVENT_* / drift guard 五項規範 |
| **AS.5.1** auth event format | **強耦合**——本 row §3.4 的 5 個 event（含 AS.0.5 已釘 2 個 + 本 row 加 3 個）是 AS.5.1 honeypot subset 的 canonical 命名 source |
| **AS.5.2** per-tenant dashboard | **強耦合**——dashboard 必顯示 form-drift 警告（§3.4 form_drift event 的 ops 訊號）+ honeypot fail rate trend（per-form path）|
| **AS.6.3** OmniSight self login/signup/pwreset/contact form Turnstile + honeypot wire | **強耦合**——caller-path 4 處必經本 row §3.2 helper interface + §6.2 acceptance criteria |
| **AS.6.4** admin Settings UI（建議新 row） | 弱耦合——`honeypot_active` toggle UI 由 AS.6.4 落地，本 row 規範 toggle flip 後行為 |
| **AS.7.x** UI redesign | **強耦合**——§5.2 React component contract + critical CSS inline 是 AS.7.x 必照搬 spec；form layout / 樣式 AS.7.x 自由設計 |
| **AS.8.3** ops runbook | 弱耦合——honeypot rotation 30-day cadence + form_drift alert 排查 SOP 從本 row 衍生 |

---

## 10. Module-global state audit (per docs/sop/implement_phase_step.md Step 1)

1. **本 row 全 doc / 零 code 變動** — 不引入任何 module-level singleton / cache / global；plan §3.4 釘的「3 個新 EVENT_* 常數」是規範未來 AS.4.1 module 必須匯出的常數，當下 `backend/audit_events.py` 既有 `EVENT_*` 常數模式（每 worker 從同 source file 推同 immutable string、Answer #1）保持不動。
2. **未引入新 module-level global / singleton / cache**。
3. **未來 AS.4.1 module-global state 預先標註**：
   - `_FORM_PREFIXES` (dict, 4 entries) / `_RARE_WORD_POOL` (tuple, 12 entries) / `OS_HONEYPOT_CLASS` (str) / `EVENT_BOT_CHALLENGE_HONEYPOT_*` (3 個 str) — 全 module-level immutable，每 worker 同推（Answer #1）。
   - **`_honeypot_field_name(form_path, tenant_id, epoch)` 純函數**——無 in-memory cache、每次調用從 `hashlib.sha256(seed).hexdigest()` 重算；epoch 從 `time.time()` 派生；不引入任何 stateful singleton（Answer #1）。
   - **`auth_features.honeypot_active` 讀取走 Y9 既有 60s `_TENANT_FEATURES_CACHE`** — 與 AS.0.6 §11 對齊規範（Answer #2）；不可額外 in-memory dict 重複 cache。
   - **Form-drift counter / honeypot fail rate** — 若 AS.4.1 加 in-memory 計數器，必走 per-replica bucket（Answer #3，與 `_login_throttle` 同 pattern），跨 worker 一致性走 PG 聚合（cron 月結 / dashboard query）。
   - AS.4.1 PR 落地時 Step 1 必再次驗證。

---

## 11. Read-after-write timing audit

- **本 row 改動**：純 doc 落地，無 schema / 無 caller / 無 transaction 行為變化；不適用 timing 分析。
- **plan 文件本身對未來 PR 的 timing 約束**：
  - **Honeypot rotation 30-day epoch boundary timing**：epoch transition（每 30 天）邊界 1-request grace（server 同時接受 epoch N 與 epoch N-1，per §2.1 / §3.1 step 2）；frontend 用 client clock 算 epoch、server 用 server clock 算 — 兩者偏差 ≤ 60s（NTP clock skew 上限），不會跨 epoch 邊界，但邊界附近 ±30 秒 window 必 grace。**不**支援 ±N hours / days grace（攻擊者可預測 rotation，過大 grace 無意義）。
  - **`auth_features.honeypot_active` flip → 下次 form render 變化**：admin flip → audit row 寫 → JSONB UPDATE → Y9 60s cache invalidate；下次 GET form 走 cache miss 重 query → 新 `honeypot_active` 值生效；最壞 60s eventual consistency window（per AS.0.6 §12 同 timing pattern）。
  - **form 渲染與 server validate 的 epoch 一致性**：client 在 mount 時 compute epoch、submit 時帶入 form data；server validate 時 compute current epoch + epoch-1 兩個 expected name，submitted dict 含其一即 OK。**邊界場景**：client 在 epoch=N 時 mount form、user 拖延填 form、submit 時已 epoch=N+1——server 仍接受（grace），不視為 drift。
  - **`OMNISIGHT_AS_ENABLED` env flip → restart-only**：AS.0.5 §7.2 既定 — env 改 必 restart backend (`docker compose restart backend-a backend-b`)，重新讀 env；不存在 read-after-write 問題。

---

## 12. Pre-commit fingerprint grep（per SOP Step 3）

- 對 `docs/security/as_0_7_honeypot_field_design.md`：`_conn()` / `await conn.commit()` / `datetime('now')` / `VALUES (?,...)` 全 0 命中（doc 本身只有規範描述 + Python / TypeScript test pattern 範例，無可執行 SQL 殘留；Python pattern 為 documentation only、不會被 import 執行）。
- 對 `TODO.md` 改動 hunk：1 行單 row 狀態翻 `[ ]` → `[x]` + reference 條目，無 fingerprint。
- 對 `HANDOFF.md` 改動 hunk：plan-only entry header + 範圍 + contract + 設計決策，無 fingerprint。
- Runtime smoke：本 row 不適用 — 純 plan doc，無 code path 可 smoke。drift guard tests（§8.1 - §8.7）在 AS.4.1 / AS.6.3 PR 落地、屆時各自有自己的 smoke。

---

## 13. Production status

* 文件本身：plan-only design freeze。
* 影響的程式碼：本 row 不改 code；AS.4.1 / AS.6.3 / AS.6.4 / AS.7.x follow-up rows 才動。
* Rollback 影響：plan 無 runtime impact、無 rollback。

**Production status: dev-only**
**Next gate**: 不適用 — 本 row 是 design doc。Schedule 由 AS.4.1 (`backend/security/honeypot.py` + `templates/_shared/honeypot/`) PR 觸發 helper module 落地；AS.6.3 (OmniSight self login/signup/pwreset/contact form Turnstile + honeypot wire) PR 觸發 4 處 caller 接 helper；AS.7.x（frontend redesign）PR 觸發 React `<HoneypotField>` 元件 render；AS.6.4 admin Settings UI PR 觸發 `auth_features.honeypot_active` toggle UI。四 PR 完成 = honeypot 全 wire = 與 AS.0.5 §6.1 Phase 0 → Phase 1 deploy gate 對齊。

---

## 14. Cross-references

- **AS.0.1 inventory**：`docs/security/as_0_1_auth_surface_inventory.md` §7 表 AS.0.7 row（「⚠️ honeypot 須避免衝撞既有 form field name」是本 row §4.2 grep verification 動機）+ §1.1 既有 password user 主流程（4 處 form path 對齊 source）。
- **AS.0.2 alembic 0056**：`backend/alembic/versions/0056_tenants_auth_features.py` — `honeypot_active` boolean key 是 §4.3 runtime gate 的 schema source；既有 tenant 預設 false / 新 tenant 預設 true 是 §4.3 表 default 一欄的 source。
- **AS.0.5 Turnstile fail-open**：`docs/security/as_0_5_turnstile_fail_open_phased_strategy.md` §2.2 / §2.3 / §2.4 / §3 / §3.5 — 本 row §1.1 / §3.4 / §3.5 對齊 source；jsfail_honeypot_* event 命名 + delay_ms 5000 + phase metric denominator 三條 invariant 與 AS.0.5 同 set。
- **AS.0.6 automation bypass list**：`docs/security/as_0_6_automation_bypass_list.md` §2 / §4 / §11 — 本 row §3.3 short-circuit 順序 + bypass-flagged 不檢查 honeypot field 對齊 source；三 axis precedence A>C>B 與 AS.0.6 同 set。
- **設計 doc § 3.5 / § 5（雙 twin pattern）**：`docs/design/as-auth-security-shared-library.md` — fail-open jsfail honeypot fallback / Python+TS twin 兩處原始 design source；本 row §1.1 / §5 對齊。
- **G4 production-readiness gate**：`docs/sop/implement_phase_step.md` lines 136-216；AS.4.1 / AS.6.3 落地 PR 必過此 gate（image rebuild + env knob wired + at least one live smoke + 24h observation）。
- **Audit event canonical 命名範本**：`backend/audit_events.py` — `domain.verb` 命名規範與 `EVENT_*` 常數匯出 pattern 是 §3.4 的 SoT。
- **既有 `app/login/page.tsx`**：lines 102 / 236 / 255 — `autoComplete="one-time-code"` / `"email"` / `"current-password"` 是 §4.2 grep 既有 form input attribute 的 source；本 row §2.1 rare-word pool 0 與此撞。
- **既有 `app/bootstrap/page.tsx`**：lines 424 / 439 / 455 / 1018 / 1408 / 1436 / 1451 / 1923 / 2089 / 2110 / 2287 / 2308 / 2332 — `autoComplete` value 全是 WHATWG spec 標準（`current-password` / `new-password` / `email` / `off`），與本 row §2.4 規範並行不衝突。
- **R34 risk**：design doc §10 — Turnstile lock 既有自動化 client → AS.0.7 honeypot fallback（jsfail terminal）+ AS.0.6 bypass list 共同 mitigate。
- **WCAG 2.1 AA**：`aria-hidden="true"` + `tabindex="-1"` + screen-reader skip 對齊基準（external accessibility standard）。

---

**End of AS.0.7 plan**. 下一步 → AS.0.8 Single-knob rollback：`OMNISIGHT_AS_ENABLED=true|false` env，false 時 AS 全套 disabled — 把本 row §10 「knob false 時 honeypot module noop」、AS.0.5 §7.2 「knob false 時 phase 行為退 pre-AS」、AS.0.6 §11 「knob false 時三 axis bypass 全 noop」三條子 invariant 整合成一張完整的 AS-wide kill switch 行為表 + env wiring location + lifespan validate spec + drift guard。
