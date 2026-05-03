# 任務：執行標準流程

## 執行規則
- 嚴格按照順序執行，不可以跳步驟。

## 步驟清單
### Step 1: 深度分析及評估
- 執行深度分析，並評估該 Phase 的實作風險、衝擊、可能產生的副作用。
- **強制問題：Module-global state 稽核**（2026-04-21 納入，由 Phase-3-Runtime-v2 Epic 4 盲區討論確立）
  - 本次修改的函式 / 模組，是否讀 / 寫 module-level global、singleton、in-memory cache？
  - 如果有，**跨 worker 進程**的一致性怎麼保證？prod 是 ``uvicorn --workers N`` 多 OS 進程——module-global 不共享。
  - 三種合格答案：
    1. **"不共享，因為每 worker 從同樣來源推導出同樣的值"**（例：``_DUMMY_PASSWORD_HASH`` 每 worker 自己算一份常數，本來就該一致）。
    2. **"透過 PG / Redis 協調"**（例：audit chain 透過 ``pg_advisory_xact_lock`` 序列化；rate-limit 透過 Redis）。
    3. **"故意每 worker 獨立"**（例：rate-limit in-memory fallback 註解寫了 per-replica bucket，是刻意的）。
  - 任何**不在上述三種**的答案都要視為 **real bug**——要嘛補協調、要嘛開 follow-up task。
  - 撰寫時以一句話記錄在 commit message 或函式 docstring，讓下一個讀者知道此處的決定。
  - 背景：`backend.auth_baseline_mode` 的 cross-test pollution（task #90）、`secret_store._fernet` 的 first-boot 寫檔競爭（task #104）都是這個問題沒問就漏掉的實例。真正的 regression 保證請見 Step 4 的 multi-worker subprocess 測試（task #82）。
- **強制問題：Read-after-write timing-visible downstream tests**（2026-04-21 納入，由 Phase-3-Runtime-v2 SP-5.6a 的 [200,409]→[200,400] 行為變化確立）
  - 本次修改如果把「被串行執行」的操作改成「可能平行」（例：compat 單連線 → 連線池、file lock → row lock、module-global dict → Redis），是否有下游測試**假設了舊的 serialisation timing**？
  - 具體要問：「**A write → B read 立刻看到 A 的結果**」的測試——在舊 timing 下因為 single-writer-conn 所以總是成立；在新 timing 下可能 B 先 commit、A 後 commit，B 的 read 先跑，看不到對方的寫入。
  - 常見模式：`asyncio.gather(a, b)` 加 `HTTP POST` 加 optimistic-lock 409 → 新 timing 下 loser 的 GET 晚於 winner 的 commit，早一步看到新 state，走進另一個 error branch（例 SP-5.6a 的 router 早期 guard 400）。
  - **不是 bug，是 timing 可見的行為變化**。但 commit message 要明確記錄：「測試期望值從 X 放寬到 X or Y，因為 pool 的 asyncio scheduling 和 compat 單連線不同」，這樣後面讀者知道為什麼放寬。Epic 7 拆掉 compat 之後這個對比就沒了，相關 inline note 屆時一併清掉。

### Step 2: 拆分工作
- 將 Phase 的工作拆分成數個子階段。
- 同時評估每個子階段實作的困難度、影響範圍、可能產生的副作用。
- 列出所有子階段工作。
- **強制拆分規則：Test-fixture blast radius**（2026-04-21 納入，由 SP-5.5 / SP-5.6a 的 test-檔膨脹確立）
  - 如果一個 SP 要 port 的檔案**有超過 2 個 test 檔直接 import 它**，**預設拆成兩個 commit**：
    - `<SP>a`：prod 檔 port + 1 個主 test 檔（通常是最核心的 contract test）
    - `<SP>b`：剩下的 test 檔 fixture migration
  - 檢查方式：`grep -rln "from backend import <module>" backend/tests/ | wc -l`
  - **為什麼**：prod 檔 port + N 個 test fixture 擠在同一個 commit 時，blast radius 大、回滾困難、commit message 要塞兩件事、code review 變難。拆開後每個 commit 單一職責，異常時可精確定位。
  - **例外**：如果 N 個 test 檔全是「同一 fixture 模板 × N 份 copy/paste」，可以考慮一起改（但 commit message 要點出「N 個 fixture 同構遷移」）。
  - 歷史案例：SP-5.6a 應該拆成 a1(workflow.py + test_workflow.py) + a2(test_workflow_optimistic_lock*.py 兩個檔)。實際沒拆，commit 比預期臃腫。

### Step 3: 執行每個子階段實作(每個子階段重複執行)
- 依照規劃逐一實作子階段。
- 完成每一個子階段後都確認一次可正常執行。
- 每個子階段完成後進行一次深度分析及檢查實作後造成的影響及產生的錯誤和副作用。
- 逐一修正實作後產生的每個問題。
- 確認該問題修正完成。
- 確認修正後可以正常執行。
- 清理工作路徑。
- 更新.gitignore。
- **強制檢查：Pre-commit compat-fingerprint grep**（2026-04-21 納入，由 SP-5.6a 的 `_reset_for_tests` 殘留 `await conn.commit()` 確立）
  - 在 `git commit` **之前**，對本次 port 的每個檔案跑一次 fingerprint grep：
    ```
    grep -nE "_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]" <file>
    ```
  - 4 個 fingerprint 代表 4 種最常見的 compat 舊碼殘留：
    1. `_conn()` — 舊的 compat-wrapper 入口
    2. `await conn.commit()` — asyncpg pool 不需要，殘留會在 pool conn 上引發 AttributeError
    3. `datetime('now')` — SQLite 語法，PG 需要 `to_char(clock_timestamp(), ...)`
    4. `VALUES (?, ?, ...)` — 舊的 SQLite `?` 占位符，PG 需要 `$1, $2, ...`
  - **任何一個 fingerprint 有命中都暫停 commit**、排查、修正、重跑 grep 直到都清。
  - 然後跑至少一個實際函式呼叫（不只是 `python -c "import ..."`，要真的 exercise ported code），確認沒有 runtime 爆 — 因為 import 不會抓到 function-body 內的 NoneType.commit() 這類殘留。
  - 為什麼不靠 lint：這些殘留在 Python 層面是合法語法（`conn.commit()` 只是方法呼叫），lint 看不到；型別也通不過因為 asyncpg 的 Connection 沒有 `commit` attr — 但這要到 runtime 才爆。
- 執行git commit。
- 確認新問題全數修正完成。
- 確認可以正常執行。

### Step 4: 實作單元測試工具
- 實作涵蓋所有新增功能的測試單元，包含前端、後端、整合測試、模擬工具的測試工具。
- 執行測試。
- 修正所有產生的問題。
- 列出測試總結。
- 確認問題修正完成。
- 確認修正後可以正常執行。
- 清理工作路徑。
- 更新.gitignore。
- 執行git commit。
- 確認新問題全數修正完成。
- 確認可以正常執行。

### Step 5: 重新評估實作後的問題
- 重新深度分析及檢查此 Phase 實作完成後所造成的問題、影響、錯誤、缺失、和副作用。
- 列出所有問題、影響、錯誤、缺失、和副作用。
- 並逐一進行修正。
- 每修正完一個問題就進行一次檢查。
- 確認該問題修復後可以正常執行。
- 清理工作路徑。
- 更新.gitignore。
- 執行git commit。

### Step 6: 狀態更新
- 進行常態更新。
- 重新進行工作項目評估，確認是否有新工作項目產生，如果有就加入未來工作項目中。
- 更新HANDOFF.md。
- 更新.README.md。
- 執行git commit。
- 列出該階段的執行總結。

### Cross-Agent Observation Protocol (B1 #209)

When an agent discovers information relevant to another agent:

1. The reporter agent calls `emit_debug_finding()` with `finding_type="cross_agent/observation"`.
2. The `context` dict must include `target_agent_id` (the intended recipient).
3. Set `context.blocking = true` if the reporter is blocked until the target acts.
4. The event bus automatically routes the finding to the Decision Engine as a proposal.
5. Blocking observations get `risky` severity (operator attention); non-blocking get `routine`.
6. The DE proposal offers two options: `relay` (forward to target) or `dismiss`.
7. On relay, an SSE `cross_agent_observation` event notifies the target agent's UI.

Example:
```python
from backend.events import emit_debug_finding
from backend.finding_types import FindingType

emit_debug_finding(
    task_id="task-42",
    agent_id="firmware-alpha",
    finding_type=FindingType.cross_agent_observation.value,
    severity="warn",
    message="ISP register map changed — SDK headers need update",
    context={
        "target_agent_id": "software-beta",
        "blocking": True,
    },
)
```

### Step 7: 實際啟動
- 啟動程式展示結果。
- 如果出現錯誤時，進行錯誤評估，然後修正。
- 確認問題修復後可以正常執行。
- 清理工作路徑。
- 更新.gitignore。
- 執行git commit。
- 重新啟動程式展示結果。

---

## Production Readiness Gate（2026-04-20 納入，由 Phase-3 預審發現的 G4 dev-green-but-not-prod-ready 漏洞確立）

### 背景：為什麼加這個 gate

G4（SQLite → PostgreSQL，#1532-1539 已全 `[x]`）shipped 時帶 712 顆 contract test 全綠、`pg-live-integration` CI job 硬 gate 過、`docs/ops/db_failover.md` 15 節 runbook 落地。從「code-level 完成度」看無可挑剔。但 Phase-3 預審時才發現兩個 production-breaking gap：

- **PG 驅動 (asyncpg + psycopg2) 不在 production image**——當初為省 11 MB wheel 刻意 CI-only，結果從未在 prod image 驗過 `import asyncpg`。
- **`migrate_sqlite_to_pg.py::TABLES_IN_ORDER` 落後 live schema 7 張表**——G4 landed 後兩週內 Alembic 加了 `bootstrap_state / dag_plans / iq_runs / mfa_backup_codes / password_history / prompt_versions / user_mfa`；migrator 無人跟進；直接 cutover 會靜默丟這 7 張表的資料。

兩個 gap 都不是 code bug——是「**dev-green 不等於 prod-ready**」的結構性縫隙。現行 `[x]` checkbox 一旦打勾就視為完工，但完工的是「code + tests + docs 合進 main」、不是「production stack 真的能跑這條 code path」。這中間有幾段 last-mile 工作持續被遺漏：

1. **Production image / runtime 依賴**：CI 跑的是一次性 ephemeral 環境、可以 install 額外 wheel；production Docker image 是 baked-in artifact、要重 build 才新增依賴。
2. **Schema / data migration drift**：任何以「列舉當前 schema」為前提的 offline tool（migrator / dump script / backup catalog / schema diff test），每次有新 Alembic migration 都必須同步。沒有自動偵測手段時必漂。
3. **Env knobs 實際套用**：code 支援 `OMNISIGHT_X` 不等於 operator 有 set 它；`.env` 是 gitignored 運維 artifact、commit 訊息不 cover。
4. **Docker network / volume 實際 wiring**：compose 宣告 `external: true` network 不等於該 network 真存在；宣告 volume 不等於 mount 給了對的 service。
5. **Runtime 實測**：有 unit test + CI integration test 不等於在 prod topology（雙 replica + Caddy LB + CF tunnel）下真跑過。

### 強制補丁：Step 3 / Step 4 / Step 6 的補充 acceptance criteria

以下檢查必須在 TODO row 打 `[x]` **之前**逐項過，任一未過即 row 不准打勾（或必須明確標註降級狀態、見下節 TODO 狀態分層）：

#### Step 3 完成條件加入：
- [ ] **Production image 實裝依賴驗收**：新增任何 Python / OS package 時，必須 (a) 加入 `backend/requirements.in`，(b) 重 run `pip-compile --generate-hashes`，(c) 重 build production image，(d) `docker run --rm <image> python3 -c "import <new_dep>"` 綠。
- [ ] **Schema migration 新增表時同步**：若 Alembic migration 新增 table，必須在同一 PR 內更新所有「以表列舉為前提」的 offline artefacts——至少包含 `scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER`、`TABLES_WITH_IDENTITY_ID`（若新表 INTEGER PK）、`scripts/scan_sqlite_isms.py` 的排除清單（若適用）。

#### Step 4 單元測試加入一條「drift guard」族：
- [ ] **任何「靜態列表 vs 實際資料源」的對齊關係必須有 test**。反例：migrator 的 `TABLES_IN_ORDER` vs live Alembic schema——Phase-3 F3 新增的 `backend/tests/test_migrator_schema_coverage.py` 就是該模式的範本。通用原則：「列表」+「資料源」配成對的地方，都加一個 diff test，差一個立刻 CI red。這類 test 可能以 auto-generate 形式出現（live DB introspect、manifest list scan、alembic metadata 讀取等），關鍵是「執行時動態」不是「commit 時寫死」。

#### Step 6 狀態更新加入：
- [ ] **Production status 必填**：HANDOFF entry 結尾新增 `**Production status:**` 一行，值為以下之一：
  - `dev-only` — code 在 main，只在本地/CI 跑過
  - `deployed-inactive` — production image + env knob 都準備好，但 feature flag / env var 關著（未啟用）
  - `deployed-active` — production 實跑中、至少一次 live smoke 綠
  - `deployed-observed` — production 實跑 ≥ 24h、無回歸 metric
- [ ] TODO row 的 checkbox 反映 deployed status（見下節分層約定）。

### TODO.md 狀態分層（2026-04-20 新約定）

既有 `[x]` checkbox 含糊、不區分「code merged」vs「prod 跑起來」。改成四層：

| 標記 | 含義 | 適用情境 |
|---|---|---|
| `[ ]` | Not started | 空盒子，未開始 |
| `[~]` | In progress | 工作正在做、未合進 main |
| `[x]` | Merged — **code 進 main + tests 綠 + CI 通過** | 典型「開發完」，**不保證 production 跑得起來** |
| `[D]` | Deployed — **production image 重 build + env 實套用 + 至少一次 live smoke 綠** | 真正「上線」 |
| `[V]` | Verified — **deployed ≥ 24h + 無 regression metric + 觀察窗過** | 真正「穩定」 |

舊 `[x]` row 不強制回填 `[D]` / `[V]`——太多歷史資料、工作量不合理。但**新 row 從今日起必須使用新分層**。特別：G4 / I10 / I9 類型的 "multi-artefact" milestone row，建議 sub-bullet 各自標：

```
### G4. SQLite → PostgreSQL migration
- [x] Runtime shim + dual-track CI  (code merged 2026-04-18)
- [x] Connection abstraction          (code merged 2026-04-18)
- [x] HA compose bundle               (code merged 2026-04-18)
- [x] Data migration script           (code merged 2026-04-18)
- [x] CI postgres matrix              (code merged 2026-04-18)
- [x] Failover runbook                (code merged 2026-04-18)
- [ ] → [D]: Production image ships asyncpg + psycopg2
- [ ] → [D]: ``OMNISIGHT_DATABASE_URL`` wired + cutover executed
- [ ] → [V]: 24h observation window after cutover clean
```

這讓 "**milestone is shipped**" 與 "**milestone is actually running in prod**" 兩個維度分開記錄、避免下一個 Phase-3-類型的驚喜。

### HANDOFF.md 格式補強

每次 entry 結尾新增固定欄位（放在「風險 / 已知問題」之後）：

```
**Production status:** [dev-only|deployed-inactive|deployed-active|deployed-observed]
**Next gate:** ....（下一個要 flip 的狀態、需要做什麼、誰做、何時）
```

`Next gate` 是為了讓閱讀者立刻知道「這 milestone 現在缺哪一步到 production」。例子：

> Production status: dev-only
> Next gate: deployed-inactive — 需要 (1) `pip-compile` 把 asyncpg + psycopg2 加進 production requirements.txt、(2) rebuild backend image。預估 30 min、不需 maintenance window。

`Production status: deployed-observed` 之後這兩欄可以省略（milestone 已封閉）。

#### Machine-readable status manifest（FX.7.4 新增 2026-05-04）

HANDOFF.md 的 ~230 條 entry 累積出 6+ 種 "Production status" 寫法（`**Production status: dev-only**` / `**Production status:** dev-only` / `### Production status` 等），prose 層查不出「哪些 row 還是 `dev-only`」。FX.7.4 把兩條欄位 mirror 進獨立的 machine-readable manifest：

- **Manifest**：`docs/status/handoff_status.yaml`（auto-generated；不要手改）
- **Generator**：`scripts/extract_handoff_status.py`（parses HANDOFF.md → emits manifest）
- **Drift guard**：`backend/tests/test_handoff_status_manifest_drift_guard.py`（CI 跑 `--check` 模式；HANDOFF.md 改了沒 regen → red）

**作者工作流**（每次新增 / 更新 HANDOFF entry 時）：

```bash
# 1. 編輯 HANDOFF.md 照原本的 prose 格式 ship
# 2. 重 generate 並 commit manifest
python3 scripts/extract_handoff_status.py --write
git add HANDOFF.md docs/status/handoff_status.yaml
```

**HANDOFF.md 仍是 source of truth** — manifest 只是 derivative view。Author 不需要碰 manifest，但 `--write` 步驟必跑、CI 會擋。Manifest 提供 grep / yq / SQL-like 查詢（"列出所有 deployed-active row" / "FX.7.x 系列還有幾個未上線" / "deployed-observed 但 next_gate 仍非空 = 漏清"）。

Schema （v1）：

```yaml
schema_version: 1
entries:
  - id: 2026-05-04--FX.7.4         # <date>--<task-id>，stable
    header_line: 12345              # 1-indexed line in HANDOFF.md
    date: '2026-05-04'
    author: Claude/Opus
    task_id: FX.7.4                 # 從 title 抽出，可能 null
    title: ...
    production_status: dev-only     # 必為 canonical_statuses 之一或 'unknown'
    next_gate: ...                  # one-line summary
```

非 canonical 值（例「planning + audit doc landed」）會被歸 `unknown` + 保留 `raw_status` 欄位，drift guard 不擋（escape hatch），但會在 generator stderr 印 `WARN`。新增穩定的非-canonical token（如 `planning-only`）時，請更新 generator 的 `EXTRA_STATUSES` 而非單條 escape。

### 給 AI 助手 / 未來自己的提醒

**實作 SOP Step 1 深度分析時，除了 code / test / docs，必須問**：

1. 這條 code path 現在 production image 真的跑得起來嗎？（`docker run --rm ... import X` 驗一下）
2. 這條 code path 依賴的「靜態列表 / catalog / schema」跟 live 狀態對齊嗎？（特別是 data migration、backup tools、schema scanners）
3. Env knob / feature flag 實際有 set 嗎？還是 code 支援但 `.env` 註解著？
4. Docker network / external volume / compose dependency 真存在 / 真 mount / 真連通嗎？
5. 有沒有 drift guard test 可以主動抓到 #2？沒有就開一個 task 補。

前一輪自己打 `[x]` 的 row，不代表下一個 feature 可以直接 build on top——在把它當 "done" 的基石之前，先跑這 5 個問題的 sanity check。這是 `implement_phase_step.md` 已有的 Step 1「深度分析及評估」的具體展開。