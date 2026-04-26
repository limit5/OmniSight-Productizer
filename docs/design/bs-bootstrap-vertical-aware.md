# BS — Bootstrap Vertical-Aware Setup & Platform Catalog（ADR）

> **Status**: Design ratified (2026-04-27) — pre-coding ADR for Priority BS（TODO.md `🅑🅢 Priority BS`，11 phases，~10 day）。本文件凍結 **BS.0.1 範圍內**的四項決策：(1) 四大設計核心、(2) 8 層動畫 spec、(3) sidecar protocol、(4) catalog 三層 source 模型，外加跨上述四項的 (5) reduce-motion 合規邏輯。任何 BS.1–BS.11 的實作若需偏離本文件，須先回頭改本文件並在 git log 留下 link。
>
> **Scope**: 本 ADR 是「**規格 + 為什麼**」層；具體實作（alembic 0051–0053、`backend/routers/catalog.py`、`Dockerfile.installer`、`installer/methods/*.py`、`hooks/use-zero-g.ts`、`lib/motion-preferences.ts`、`components/omnisight/*` 等）由 BS.1–BS.11 的 TODO row 落地。本文件**不**涵蓋：threat-model（在 `docs/security/bs-installer-threat-model.md`，BS.0.2）、R 系列風險登記更動（在 TODO.md，BS.0.3）。
>
> **Related**:
> - TODO.md `🅑🅢 Priority BS — Bootstrap Vertical-Aware Setup & Platform Catalog`（line 2520–2702）
> - `backend/bootstrap.py`（Priority L 既有 4-step `REQUIRED_STEPS`：admin password / LLM provider / CF tunnel / smoke）
> - `backend/pep_gateway.py`（R20 Part A inline coaching；BS.7 install pipeline 重用之）
> - `configs/embedded_catalog/*.yaml`（BS.1.2 seed source；目前作為 OmniSight upstream catalog 的地面真相）
> - `docs/design/edge-ai-npu-deploy.md`（vertical-aware SDK 安裝的相鄰 epic — Edge AI NPU 模型部署，與 BS 共用 sidecar pattern 的 lessons learned）
> - CLAUDE.md Compilation Rules / Safety Rules（cross-compile toolchain selection、`test_assets/` :ro 邊界、API key 不入 source）

---

## 1. TL;DR — 五個決策 + 可驗收後果

1. **Catalog 三層 source 模型**：每個 entry 在 PG `catalog_entries` 表中以 `(id, source)` 為複合主鍵，`source ∈ {shipped, operator, override}`；查詢時以 `override > operator > shipped` 優先序合併。Operator 走 UI CRUD 寫 `operator` / `override` 列；OmniSight upstream 經 alembic data migration 寫 `shipped` 列。**結果**：upstream re-seed 永遠不會覆蓋 operator 修改。
2. **Sidecar `omnisight-installer` 隔離 install 風暴**：所有 destructive install 動作（`docker pull` / `tar -xzf` / `bash vendor.sh`）執行在獨立容器內，long-poll backend `/installer/jobs/poll` 取得工作，progress 經 SSE 回流 backend bus。Backend 自身**不**跑 install。**結果**：install 失敗 / OOM / CPU 暴衝不影響 backend serving。
3. **零停機 additive bootstrap**：Priority L 的 4-step `REQUIRED_STEPS` 完全不動；BS.9 加入的 `STEP_VERTICAL_SETUP` 是 **optional intermediate step**——既有已 finalize 的 prod 直接跳過，新 prod 顯示但可 skip。**結果**：rollback 一個版本永遠可逆，已上線環境不受影響。
4. **8 層動畫 + 4 級 motion level + 電池感知，但 `prefers-reduced-motion` 是硬上限**：使用者預設 `dramatic`，按設定 4 級降階；行動裝置 `<50%` 自動降一級、`<15%` 強制 `off`；`prefers-reduced-motion: reduce` 永遠覆蓋一切回 `subtle` 或 `off`（見 §6.2 優先序表）。**結果**：a11y 永遠合規、低電量 UX 永遠不卡。
5. **Reduce-motion 合規是 **hook-level guard**，不是組件 opt-in**：每個 motion 觸發點都走 `useEffectiveMotionLevel()` 的回傳值決定要不要動，不允許 `<motion.div>` 直接寫死 keyframe；CI 檢查 keyframe class 必須來自 `lib/motion-classes.ts` 的常數匯出（防止隨手寫 `className="float-drift-a"` 繞過 hook）。**結果**：a11y violation 不可能漏接。

---

## 2. 背景 — 為什麼有這個 epic

Priority L 解決了 OmniSight 系統本身的 4-step bootstrap（admin password / LLM provider / CF tunnel / smoke test），但**完全沒有 vertical-aware 概念**：使用者選擇要做哪種產品（Mobile / Embedded / Web / Software / Custom）後，對應的 SDK / toolchain / cross-compile 環境（Android SDK API levels / Xcode runtimes / Qt versions / NXP MCUXpresso / Yocto / Node + version manager / Python + uv / Rust + rustup 等等）目前全靠 operator 手動 `docker pull` + shell install + 編 yaml。P11 #351 的 Android CLI install row 標 `[O]` 就是承認這個 gap。

BS 把這條 gap 填上，**且**順便把 catalog 從 yaml 抬升到 PG（為將來可分發的 catalog feed、per-tenant 客製、operator 在 UI 改 entry 鋪路）。

---

## 3. 設計核心 1 — Catalog 三層 source 模型（forward-compat 重點）

### 3.1 三層 source 的角色分工

| source | 寫入者 | 寫入時機 | 對應 alembic | UI CRUD 權限 |
|---|---|---|---|---|
| `shipped` | OmniSight upstream | release 時 alembic data migration 走 `INSERT ... ON CONFLICT (id, source) DO UPDATE` | 0052（首批）+ 後續每個 catalog 變更 | read-only（admin 可 hide 但不可改值） |
| `operator` | tenant operator（admin role） | 透過 `POST /catalog/entries` UI form | — | full CRUD |
| `override` | tenant operator（admin role） | 透過 `PATCH /catalog/entries/{id}` 對 shipped entry 寫客製值 | — | upsert / delete |

`(id, source)` 做為複合主鍵（partial unique on `(id, source) WHERE NOT hidden`，hidden=true 列保留歷史足跡但不參與合併）；`metadata` 是 JSONB column 收 vendor-specific 欄位（Android SDK 的 API levels / Xcode runtimes 的 platform list / Yocto 的 BSP 名…），schema 由 `configs/embedded_catalog/_schema.yaml`（BS.1.3）做 JSONSchema 驗證。

### 3.2 合併優先序（讀取時）

對任意 catalog id **`X`**：

```
final(X) = override(X) ?? operator(X) ?? shipped(X) ?? null
```

實作：`backend/catalog.py::resolve_entry(entry_id, tenant_id)` 走一次 SQL（`WITH ranked AS (SELECT ..., ROW_NUMBER() OVER (PARTITION BY id ORDER BY CASE source WHEN 'override' THEN 1 WHEN 'operator' THEN 2 WHEN 'shipped' THEN 3 END) AS rn) SELECT * WHERE rn = 1 AND NOT hidden`）。`tenant_id` 過濾在 `operator` / `override` 兩層套用（per-tenant scope，依賴 Priority I Multi-tenancy Foundation）。

### 3.3 為什麼不直接「fork shipped → 改」一份

**反例方案**：operator 想改 `nxp-mcuxpresso-imxrt1170` 的下載 URL，UI 直接 deep-copy shipped row，在 `source='operator'` 寫一份完整副本。

**問題**：upstream 後來把同一個 id 的 install method 從 `shell_script` 改成 `docker_pull`（更安全、更快），operator 那份 deep-copy 沒收到 upgrade，因為 ID 已經獨立存在於 `operator` 層；operator 完全不知道 upstream 改了。Catalog drift 隨時間越積越多，三年後 operator UI 看到的 catalog 跟 OmniSight 維護的 catalog 完全不同。

**正解**：`override` 層只存 operator **修改了的欄位**（partial JSONB diff），其餘走 shipped。Upstream 改了 install method → shipped 自動更新 → override 沒覆蓋這個欄位 → operator 看到的是 shipped 的新值 + 自己改過的欄位。

實作層面：`override` row 的 `metadata` JSONB 只放 diff（`{"download_url": "https://my-mirror.example.com/..."}` 這種 sparse object），`resolve_entry` 在 PG 端用 `jsonb || jsonb` operator merge 出最終 entry。Schema validator 對 `override` row **放寬**必填欄位（因為 shipped 已經提供了 base）。

### 3.4 Forward-compat：catalog feed 規格凍結（R24 風險）

**外部風險**：未來會做 `catalog_subscriptions` 表（BS.1.1）— operator 訂閱第三方 catalog feed URL，cron 同步。如果 feed schema 與 OmniSight `shipped` schema 不一致，要嘛拒收要嘛 silent skip 欄位。

**Forward-compat 規則**：

1. **`metadata` JSONB 永遠允許 unknown 欄位**——validator 是 closed list 對 top-level columns、open list 對 `metadata` 子鍵。Catalog feed 用 OmniSight 沒見過的 `metadata.vendor_extra` key 寫東西，不會被 validator 退；但讀回來時 OmniSight UI 只渲染認得的欄位（unknown 欄位以「+ N more attributes」摺疊顯示，hover 看 raw JSON）。
2. **Schema version 是 entry-level field**，不是 catalog-level。`catalog_entries.schema_version SMALLINT NOT NULL DEFAULT 1`；validator 走 `SCHEMA_VALIDATORS[schema_version]`（dict of versioned JSONSchema）。新增 schema version → 新增一個 validator + 在 `_schema.yaml` 留 changelog。**舊 schema_version 的 entry 永遠可以讀**，但 admin UI 顯示 deprecation banner 提醒 operator 走 `PATCH` migrate to latest。
3. **`source='subscription'` 是 schema_version=1 之後可加的第四層**——本 ADR 不開這層，但 enum 預留 `subscription`、複合主鍵 `(id, source)` 結構不變、優先序定為 `override > operator > subscription > shipped`（subscription 比 shipped 新但不蓋 operator 客製）。

### 3.5 Drift guard test（強制）

按 SOP Step 4 的 drift-guard 規則：

- `backend/tests/test_catalog_schema.py`（BS.1.5）必含 `test_yaml_seed_matches_alembic_seed`——把 `configs/embedded_catalog/*.yaml` 全 load 一次、對比 alembic 0052 data migration 的 `INSERT` 列表，缺一個 / 多一個都 CI red。
- 每個 alembic 版本的 catalog data migration 都必須跑 schema validator（CI gate），確保新增的 entry 自身合法。

---

## 4. 設計核心 2 — Sidecar `omnisight-installer` 隔離 install 風暴

### 4.1 為什麼不在 backend 內裝

| 風險 | backend 內裝 | sidecar 模式 |
|---|---|---|
| `docker pull` 5 GB 大 image，網路 / 磁碟卡 | 卡到 backend HTTP serving，user-facing latency 飆升 | sidecar 自身 stuck，backend 完全無感 |
| vendor `bash install.sh` runaway（`while true: cp ...`） | 把 backend container 磁碟塞爆、OOM kill | sidecar 容器被 docker `--storage-opt size=10G` 限制，自身炸但不擴散 |
| install method 用 untrusted shell（vendor 提供） | command injection 直接打到 backend 帳號 | sidecar 跑 non-root user、socket 走 docker-socket-proxy `:ro`（threat model 在 BS.0.2） |
| Python 依賴衝突（vendor installer 要 `python3.10`，backend 要 `python3.12`） | 不能共存 | sidecar image 自選 base，backend image 不污染 |

**結論**：install 動作必須隔離。Sidecar 是最小開銷的隔離方法（vs. ephemeral k8s job：對 single-host docker-compose deployment 太重）。

### 4.2 Long-poll protocol（v1）

Backend 是**唯一**的 truth source；sidecar 是 stateless worker。

```
POST /installer/jobs                  # operator 從 UI 建 job（PEP gateway HOLD 之後）
GET  /installer/jobs/poll             # sidecar long-poll；timeout 30s 後回 204
POST /installer/jobs/{id}/progress    # sidecar 回報進度（SSE 用）
POST /installer/jobs/{id}/result      # sidecar 回報終局（completed / failed）
POST /installer/jobs/{id}/cancel      # operator 從 UI 按 cancel；sidecar 下一輪 poll 看到 cancel flag 中止
```

**Job state machine**：

```
queued → running → completed
                 ↘ failed
                 ↘ cancelled
```

State transition 全部在 backend 寫 PG（`install_jobs.state` enum），sidecar 不直接寫 PG（避免兩個 truth source）。Sidecar 透過 `POST .../progress` 帶 `state: running` 是用來「ack 拿到了」，backend 收到後寫 PG `state=running`、`started_at=now()`。

### 4.3 Protocol versioning（R26 風險）

**問題**：BS.4 ship v1 protocol；半年後想改 progress payload 格式；舊版 sidecar 已 deploy 在 operator 機器上、運維沒同步升級。

**規則**：

1. **Sidecar 第一次 poll 必須先 handshake**：`GET /installer/jobs/poll?protocol_version=1`。Backend 收到時驗證 `protocol_version ∈ SUPPORTED_VERSIONS`（in-memory list），不在裡面回 426 Upgrade Required + JSON body 帶 `min_version` / `max_version` / `download_url` 提示 operator 怎麼升級 sidecar image。
2. **Backend 同時支援 N 與 N-1**（最多兩個版本並存，避免 protocol matrix 爆炸）。N-2 broken：sidecar `<= N-2` 連線拿到 426，operator 必須升才能繼續裝東西。
3. **Sidecar image tag 永遠用 `:bs-vN`，不用 `:latest`**——`docker-compose.yml` 釘版本，避免 silent drift。Operator 走 `docker-compose pull` 時看到 image SHA 變動才會升。
4. **Protocol breaking change 觸發**：(a) progress payload schema 移除 / 改型 / 必填欄位增加；(b) endpoint URL 變動；(c) auth 機制變動。**不**觸發：增加 optional 欄位、新增 endpoint、新增可選 query param。

### 4.4 Job idempotency（R27 風險）

**問題**：sidecar 跑到 90% 時 docker container restart（SIGKILL）；下次 poll 又拿到同一個 job？還是已經被別的 sidecar 拿走？

**規則**：

1. **Job claim 是一次寫死**：`POST .../poll` 由 backend 端走 `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` + `UPDATE ... SET state='running', sidecar_id=$1, claimed_at=now()` 一個 transaction 完成。一個 job 只可能被一個 sidecar 拿到。
2. **Sidecar 每次 startup 第一件事**：query `GET /installer/jobs?sidecar_id=self&state=running`，看到「我認領但沒回報結果」的 job → 回報 `failed` + reason `sidecar_restart_recovered`，operator 看到後手動決定要不要 retry。
3. **Install method 自身必須冪等**：`docker_pull` 重跑無害（image 已存在 → noop）；`shell_script` 由 vendor 寫不保證冪等，所以 sidecar **不**自動 retry shell_script 的 `failed` job——而是把 retry 決定權留給 operator（BS.7.6）。
4. **`install_jobs.idempotency_key` 欄位**（UUID，由 frontend 在 `POST /installer/jobs` 時帶上）：backend 走 `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`；同 key 重發回上次 ID，避免雙擊建兩個 job。

### 4.5 Health & observability

- Sidecar `/health` endpoint（BS.4.5）回 `{status: ok, last_poll_at: ..., last_job_id: ..., protocol_version: 1}`；backend 抓這個 endpoint 進 `system_health.sidecars` 表，operator UI 在 Settings → Platforms 頁右上角看到「Installer status: 🟢 healthy / 🟡 idle 5m / 🔴 unreachable」。
- 每個 progress emit 都帶 `stage / bytes_done / bytes_total / eta_seconds / log_tail`；log_tail 限制最後 4 KB（防 log 爆量）。
- **不**在 sidecar 寫長期 audit log——audit 寫 backend `audit_log` 表，sidecar 只是 transient worker。

---

## 5. 設計核心 3 — 8 層動畫規格（**動畫 spec ≠ CSS、是契約**）

### 5.1 8 層分類表

| # | 層 | 觸發 | CSS 主要技法 | 預設啟用 motion level |
|---|---|---|---|---|
| 1 | **Idle drift** | 卡片 / hero 元件常駐 | `@keyframes float-drift-{a,b,c,d}`：4 變奏的 4-second-loop translate(±2px, ±3px) + rotate(±0.5deg)；不同元件用不同變奏避免同步 | `subtle` 起 |
| 2 | **Cursor magnetic tilt** | hover within 80px | `transform: rotateX(...) rotateY(...)`，CSS variable 由 JS 寫入；半徑外回歸 0；spring damping `cubic-bezier(0.34, 1.56, 0.64, 1)` | `normal` 起 |
| 3 | **Group breathe** | category strip / chip group | `@keyframes group-breathe`：6-second-loop scale(1.0 → 1.015 → 1.0) + opacity(0.95 → 1 → 0.95)，stagger 200ms 相鄰元件 | `subtle` 起 |
| 4 | **Cursor distance glow** | catalog card | mouse 距離卡片中心線性映射到 `--glow-intensity` CSS variable，box-shadow 強度 0–1 | `normal` 起 |
| 5 | **Hover lift + spring** | 任何 click target | hover: `translateY(-3px) + box-shadow 加大`；release: spring 回 baseline（`cubic-bezier(0.5, 1.5, 0.5, 1)`） | `subtle` 起 |
| 6 | **Scroll parallax** | hero panel + orbital diagram | scroll Y 對軌道 dot Y 1:0.4 ratio、對 hero glow 1:0.7 ratio；用 `transform: translateY(...)` 而非 `top` 避免 reflow | `normal` 起 |
| 7 | **Glass reflection** | hero panel + detail panel | radial gradient 隨 cursor 位置位移，模擬玻璃反光；`background-position` 跟 mouse 1:0.05 ratio | `dramatic` 限定 |
| 8 | **Click inertia spring** | install button / cancel button | mousedown: `scale(0.96)`；mouseup: spring overshoot to `scale(1.02)` → settle `scale(1)`，total 350ms | `subtle` 起 |

### 5.2 4 級 motion level 啟用矩陣

| Layer | `off` | `subtle` | `normal` | `dramatic` |
|---|---|---|---|---|
| 1. Idle drift | ✗ | ✓（amplitude × 0.5） | ✓（amplitude × 1.0） | ✓（amplitude × 1.5） |
| 2. Cursor magnetic tilt | ✗ | ✗ | ✓（max ±5deg） | ✓（max ±8deg） |
| 3. Group breathe | ✗ | ✓（scale × 0.5） | ✓ | ✓ |
| 4. Cursor distance glow | ✗ | ✗ | ✓ | ✓（intensity × 1.3） |
| 5. Hover lift + spring | ✗ | ✓（lift 1px） | ✓（lift 3px） | ✓（lift 5px） |
| 6. Scroll parallax | ✗ | ✗ | ✓ | ✓（ratio × 1.5） |
| 7. Glass reflection | ✗ | ✗ | ✗ | ✓ |
| 8. Click inertia spring | ✗ | ✓（overshoot 5%） | ✓（overshoot 10%） | ✓（overshoot 15%） |

**`off`**：所有 motion 全停，instant transition（duration=0）。
**預設值**：`dramatic`（per BS.3.3）；新使用者第一次進 Platforms 頁直接看到完整 8 層體驗。

### 5.3 Hook 邊界（BS.3.5 `useEffectiveMotionLevel()`）

```ts
function useEffectiveMotionLevel(): MotionLevel {
  if (mediaQuery('prefers-reduced-motion: reduce')) return 'off';   // OS 層硬上限
  const userPref = useUserPreference('motion_level', 'dramatic');   // BS.3.3
  if (userPref === 'off') return 'off';                             // 使用者顯式關
  const battery = useBatteryStatus();                                // BS.3.4
  return applyBatteryDowngrade(userPref, battery);                  // §6.3 規則
}
```

**所有 8 層動畫**的觸發 hook（`useFloatingCard` / `useCursorMagneticTilt` / ...）內部第一行都呼叫 `useEffectiveMotionLevel()` 並做對照表 lookup；`level === 'off'` 直接 `return null`（不掛 listener、不寫 CSS variable）。

### 5.4 為什麼不直接 `<motion.div>` 寫 framer-motion

1. **Bundle size**：framer-motion ~50 KB gzipped；Platforms 頁是 dashboard 內的 settings 子頁，沒有理由把整個動畫 runtime 拖進首屏。CSS `@keyframes` + `transform`/`opacity` 走 GPU compositor，不需要 JS runtime。
2. **a11y guard**：framer-motion 的 `useReducedMotion()` 是 component-by-component opt-in，漏接一個就違反 WCAG；本 ADR 走 hook-level guard（§5.3），保證每個 motion 都過 `useEffectiveMotionLevel()`。
3. **既有專案沒 framer-motion 依賴**——避免新增 dep（per CLAUDE.md「proper solution，不引入無端依賴」原則）。

### 5.5 性能 budget（BS.11.6 驗證）

- 中端裝置（i5-1135G7 / Pixel 6）打開 Platforms 頁、catalog 100 entries，FPS ≥ 50。
- Lighthouse Performance ≥ 80（Platforms 頁，desktop preset）。
- **GPU layer 上限**：每張卡片最多 `transform` + `opacity` 兩個 compositor-only property；不允許 `box-shadow` 動畫（會觸 paint）— glow effect 改用 fixed `box-shadow` + 改 `opacity` of 一個額外的 glow `<div>`。

---

## 6. 設計核心 4 — Reduce-motion 合規邏輯

### 6.1 為什麼是 design core，不是 implementation detail

WCAG 2.3.3（Animation from Interactions, Level AAA）要求「使用者可以禁用非必要動畫」。`prefers-reduced-motion: reduce` 是 OS 層使用者表達「我會 motion sickness」的訊號。MDN：「This may be set due to vestibular disorders, migraines, or other conditions that make motion uncomfortable.」

**錯誤示範**（過去 commit 真實出過）：開發者寫了一個 `<motion.div whileHover={{ scale: 1.05 }} />`，沒檢查 `prefers-reduced-motion`，導致一位 vestibular 患者打開頁面立刻頭暈閉頁。**這是無條件 production bug，不是 enhancement**。

### 6.2 優先序（**硬規則**，違反 = CI red）

```
prefers-reduced-motion: reduce  →  motion = 'off'        # OS 層，永遠優先
                                            ↓ if not set
user explicit pref = 'off'      →  motion = 'off'        # UI Settings 顯式關
                                            ↓ if not 'off'
battery downgrade rule          →  apply rule (§6.3)     # 行動裝置電量規則
                                            ↓
user pref (dramatic / normal / subtle)                   # 預設 dramatic
```

### 6.3 電池降級規則（BS.3.4）

| 電量 | 充電中？ | 行為 |
|---|---|---|
| `> 50%` 或無 API（desktop） | — | 走使用者 pref |
| `30–50%` | 否 | 降一級（dramatic → normal、normal → subtle、subtle → subtle） |
| `15–30%` | 否 | 強制 `subtle` |
| `< 15%` | 否 | 強制 `off` |
| 任意 | 是 | 走使用者 pref（不降級） |

降級觸發時**一次性 toast**告知 operator：「Battery low (12%) — animations paused. [Override (full motion)]」。Override 寫到 `localStorage.battery_override_until`（unix timestamp，2 hours），到期失效。

**為什麼不在所有電量觸發 toast**：toast 太頻繁是另一種 UX 公害。`< 15%` 強制 off 才出 toast，30–50% 降一級是靜默降級（使用者通常不會察覺，畢竟只是 amplitude 從 1.5x 降到 1.0x）。

### 6.4 CI 檢查

`backend/tests/test_motion_a11y.py`（BS.3.7 / BS.11.1 sub-row）：

1. **AST scan `app/`** — `<motion.*>` 或 `motion(...)` framer-motion API 出現 → fail（強制走 css + hook 路線）。
2. **Class scan** — `className` 含 `float-drift-` / `group-breathe` / `spring-press` 等 keyframe class 但檔案 import `useEffectiveMotionLevel` 未發現 → warn，需 inline `// motion-class-allowed: <reason>` 才放行。
3. **Storybook story for each motion layer** — Storybook 跑 `prefers-reduced-motion: reduce` 模擬模式時所有 8 層 visually idle（screenshot diff 容忍 < 1% pixel difference）。

### 6.5 R25 風險登記摘要（細節在 BS.0.3）

- **R25.1**：`useEffectiveMotionLevel()` 漏接 `prefers-reduced-motion` listener（only 讀首次值）→ OS 設定切換後 stale。Mitigation：hook 內 attach `MediaQueryList.addEventListener('change', ...)`、unmount 時 cleanup。
- **R25.2**：CSS-only 動畫（`<style>` 直接內嵌）繞過 hook → a11y 漏。Mitigation：`@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; } }` 全域 fallback 寫進 `app/globals.css`，作為 hook miss 的 last line of defense。
- **R25.3**：第三方組件（如 `@tanstack/react-virtual`）內建動畫忽略偏好 → 走 component prop opt-out（disable virtual scroll smooth-transition）+ 在 `useEffectiveMotionLevel` === 'off' 時 prop 設定為 `disable`。

---

## 7. 跨 epic 介面摘要（給 BS.1–BS.11 作 reference）

### 7.1 PG schema（alembic 0051 + 0052 + 0053）

```
catalog_entries:
  id              TEXT NOT NULL          # e.g. "nxp-mcuxpresso-imxrt1170"
  source          TEXT NOT NULL          # enum shipped|operator|override (subscription 預留)
  schema_version  SMALLINT NOT NULL DEFAULT 1
  tenant_id       UUID NULL              # NULL for shipped; required for operator/override
  vendor          TEXT NOT NULL
  family          TEXT NOT NULL          # mobile|embedded|web|software|rtos|cross-toolchain|custom
  display_name    TEXT NOT NULL
  version         TEXT NOT NULL
  install_method  TEXT NOT NULL          # noop|docker_pull|shell_script|vendor_installer
  install_url     TEXT NULL
  sha256          TEXT NULL
  size_bytes      BIGINT NULL
  depends_on      TEXT[] NOT NULL DEFAULT '{}'
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
  hidden          BOOLEAN NOT NULL DEFAULT false
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
  PRIMARY KEY (id, source, COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid))
  UNIQUE (id, source, tenant_id) WHERE NOT hidden

install_jobs:
  id                 UUID PK
  tenant_id          UUID NOT NULL
  entry_id           TEXT NOT NULL          # FK refs catalog_entries.id (cross-source)
  state              TEXT NOT NULL          # queued|running|completed|failed|cancelled
  idempotency_key    UUID UNIQUE NOT NULL
  sidecar_id         TEXT NULL
  protocol_version   SMALLINT NOT NULL DEFAULT 1
  bytes_done         BIGINT NOT NULL DEFAULT 0
  bytes_total        BIGINT NULL
  eta_seconds        INTEGER NULL
  log_tail           TEXT NOT NULL DEFAULT ''     # 最後 4KB
  result_json        JSONB NULL
  error_reason       TEXT NULL
  pep_decision_id    UUID NULL                    # FK to PEP HOLD record (R20)
  queued_at          TIMESTAMPTZ NOT NULL DEFAULT now()
  claimed_at         TIMESTAMPTZ NULL
  started_at         TIMESTAMPTZ NULL
  completed_at       TIMESTAMPTZ NULL

catalog_subscriptions:
  id                 UUID PK
  tenant_id          UUID NOT NULL
  feed_url           TEXT NOT NULL
  auth_method        TEXT NOT NULL                # none|basic|bearer|signed_url
  auth_secret_ref    TEXT NULL                    # secret_store key, NEVER the secret itself
  refresh_interval_s INTEGER NOT NULL DEFAULT 86400
  last_synced_at     TIMESTAMPTZ NULL
  last_sync_status   TEXT NULL
  enabled            BOOLEAN NOT NULL DEFAULT true
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
```

### 7.2 Backend API 表

| Method | Path | Role | 用途 |
|---|---|---|---|
| GET | `/catalog/entries` | operator | filter / sort / paginate；自動套三層 source merge |
| GET | `/catalog/entries/{id}` | operator | 單筆完整 entry |
| POST | `/catalog/entries` | admin | 新增 `source='operator'` |
| PATCH | `/catalog/entries/{id}` | admin | 寫 `source='override'` partial diff |
| DELETE | `/catalog/entries/{id}` | admin | soft-delete（hidden=true）；shipped 只能 hide 不能真刪 |
| GET / POST / PATCH / DELETE | `/catalog/sources` | admin | catalog_subscriptions CRUD |
| POST | `/installer/jobs` | operator + PEP | 建立 install job（必走 PEP HOLD） |
| GET | `/installer/jobs` | operator | filter by state / tenant |
| GET | `/installer/jobs/{id}` | operator | 單筆完整 job |
| POST | `/installer/jobs/{id}/cancel` | operator | 取消 |
| POST | `/installer/jobs/{id}/retry` | operator | 重發（建新 job，保留歷史 row） |
| GET | `/installer/jobs/poll` | sidecar（special token） | long-poll claim |
| POST | `/installer/jobs/{id}/progress` | sidecar | progress emit |
| POST | `/installer/jobs/{id}/result` | sidecar | terminal state |

### 7.3 SSE event types（per `docs/design/sse-event-scope-policy.md` rubric）

| Event | Scope | 說明 |
|---|---|---|
| `install.progress` | `tenant` | 整個 tenant 都能看（多 admin 同時觀察） |
| `install.completed` | `tenant` | 同上 |
| `install.failed` | `tenant` | 同上 |
| `catalog.updated` | `tenant` | catalog_entries / subscriptions 有寫入 |
| `sidecar.health` | `global` | admin 級系統健康訊號（不含 PII） |

### 7.4 Frontend 模組

| 路徑 | 用途 |
|---|---|
| `app/settings/platforms/page.tsx` | 主入口、3 sub-tab routing |
| `app/settings/display/page.tsx` | Motion preferences UI |
| `app/bootstrap/page.tsx`（modify） | 加 `STEP_VERTICAL_SETUP` 渲染 |
| `components/omnisight/platform-hero.tsx` | Hero panel + orbital diagram |
| `components/omnisight/catalog-tab.tsx` + 5 子組件 | Catalog 列表 + 5-state card + detail panel |
| `components/omnisight/install-progress-drawer.tsx` | bottom-right drawer |
| `components/omnisight/installed-tab.tsx` | 已裝列表 + cleanup |
| `components/omnisight/sources-tab.tsx` | catalog feed CRUD（admin） |
| `components/omnisight/custom-entry-form.tsx` | operator 新增 catalog entry（admin） |
| `components/omnisight/bootstrap-vertical-step.tsx` | bootstrap wizard 新 step |
| `components/omnisight/motion-preview.tsx` | Display Settings 即時預覽 |
| `hooks/use-zero-g.ts` | 8 層動畫 hooks |
| `hooks/use-install-jobs.ts` | SSE subscribe + 本地狀態 |
| `lib/motion-preferences.ts` | MotionLevel enum + 持久化 |
| `lib/motion-classes.ts` | keyframe class 常數匯出（CI 檢查目標） |
| `lib/battery-aware-motion.ts` | battery API + 降級規則 |

---

## 8. 不在本 ADR 範圍 / 後續決策

| 議題 | 在哪解 |
|---|---|
| Sidecar threat model（privilege model / docker-socket-proxy ro 限制 / sha256 verify 鏈 / air-gap mode） | `docs/security/bs-installer-threat-model.md`（BS.0.2） |
| R 系列風險登記（R24/R25/R26/R27 條目） | TODO.md（BS.0.3） |
| Multi-tenancy scope 的具體 SQL row-level security | Priority I 既有 ADR；BS 只是 consumer |
| PEP coaching card 模板 | `backend/pep_gateway.py::_build_coaching` + R20-A 既有；BS.7.2 只新增 `install_intercept` action category |
| Bootstrap wizard 流程（既有 4 step） | Priority L 既有；BS.9 只插一個 optional step |
| Catalog feed 第三方協議（subscription 層） | 預留 enum，本 ADR 不規格化 feed schema；後續 BP 或 BS+1 epic |
| Mobile 4G 網路電量規則（無 wifi 額外降級） | 本 ADR 不處理；future enhancement |

---

## 9. Sign-off / 驗收

本 ADR 的接受標準：

- [ ] **§3 catalog 三層 source 模型**：`backend/catalog.py::resolve_entry` 對任意 (id, tenant_id) 回傳的 entry 等於 `override(X) ?? operator(X) ?? shipped(X)` 的 SQL 化身（test 在 `backend/tests/test_catalog_resolve.py`）。
- [ ] **§4 sidecar protocol**：sidecar v1 ↔ backend v1 long-poll 跑通；handshake 不符回 426；job state 五個 transition 都有 audit log。
- [ ] **§5 8 層動畫**：每層有對應 `hooks/use-zero-g.ts` 函式、Storybook story、performance budget 在中端裝置 ≥ 50 FPS。
- [ ] **§6 reduce-motion 合規**：BS.11.1 的 audit row 全綠；CI a11y 測試（`prefers-reduced-motion: reduce` 模擬）所有 motion-bearing 頁面 visually idle。
- [ ] **§3.5 + §5.5 drift guards**：`test_yaml_seed_matches_alembic_seed` + `test_motion_a11y_no_framer_motion` 兩個 CI gate red 即 row 不准打 `[x]`。

凍結時間：2026-04-27（BS.0.1）。任何改動需 update 本文件並在 BS 對應 row 的 commit message 標 `(ADR amended)`。

---

## 10. 變更紀錄

| 日期 | 改動 | 作者 |
|---|---|---|
| 2026-04-27 | 初版（BS.0.1） — 五個決策定稿、PG schema 定稿、protocol v1 定稿 | Agent-row7-self-agent（automated） |
