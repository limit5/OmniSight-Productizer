# Priority Y — Multi-user × Multi-project 情境盤點

> 文件起點：2026-04-25  
> 對應 TODO：`Y0. Multi-user × Multi-project 情境盤點 + 架構文件 (#276)`  
> 撰寫策略：每個 TODO 子勾選對應一個 `S-x` 情境章節。本提交完成 **S-6（多分支同專案 — Doorbell V1 客戶 A 量產期下 main / staging / v2.1-hotfix / customer-x-fork 四 branch 並行開發、workspace 要能同時保有）**，承接 S-1 / S-2 / S-3 / S-4 / S-5 已落地章節；其餘 S-7～S-9 章節仍留「Skeleton — TBD by future row」標記，等該勾選排到時再展開。共用區段（ER diagram / 權限矩陣 / migration 策略）在所有情境章節成型後彙整。

---

## 文件結構導航

| 章節 | TODO 對應 | 狀態 |
|---|---|---|
| [S-1 單租戶多用戶](#s-1-單租戶多用戶) | `[x]` 第 1 勾選 | 完成（2026-04-25） |
| [S-2 多租戶單用戶](#s-2-多租戶單用戶) | `[x]` 第 2 勾選 | 完成（2026-04-25） |
| [S-3 跨租戶協作](#s-3-跨租戶協作) | `[x]` 第 3 勾選 | 完成（2026-04-25） |
| [S-4 多產品線](#s-4-多產品線) | `[x]` 第 4 勾選 | 完成（2026-04-25） |
| [S-5 多專案同產品線](#s-5-多專案同產品線) | `[x]` 第 5 勾選 | 完成（2026-04-25） |
| [S-6 多分支同專案](#s-6-多分支同專案) | `[x]` 第 6 勾選（本 row） | **本次完成** |
| [S-7 消失用戶回收](#s-7-消失用戶回收) | `[ ]` 第 7 勾選 | Skeleton |
| [S-8 熱點撞牆](#s-8-熱點撞牆) | `[ ]` 第 8 勾選 | Skeleton |
| [S-9 遺留相容](#s-9-遺留相容) | `[ ]` 第 9 勾選 | Skeleton |
| [共用區段：ER / 權限矩陣 / migration](#共用區段) | 全九勾選成型後彙整 | Stub |

---

## S-1 單租戶多用戶

> 一家公司多個工程師共享一個 tenant、同一個 LLM quota pool；RBAC 控制誰能改 secrets / 誰能開 project / 誰只能讀。

### S-1.1 角色 Persona — 真實人物對應

以一家做相機 / 門鈴的硬體新創 `Acme Cameras` 為樣本（tenant_id = `t-acme`）。整家公司用同一個 OmniSight tenant、同一份 LLM 預算、同一個 git 整合配置。

| Persona | 公司角色 | OmniSight RBAC 角色 | 該 do | 該 not do |
|---|---|---|---|---|
| **Alice** | 工程主管 / 平台 owner | `owner` (tenant) | 改 plan、改 LLM secret、邀請 / 撤離成員、看所有人的 audit、設 quota | （無上限。但任何危險動作都記 audit） |
| **Bob** | DevOps lead | `admin` (tenant) | 開 / 關 project、設 git 憑證、調整 quota；管 invite | 不能改 plan / 不能改 super-admin role；不能刪 tenant |
| **Carol** | 韌體工程師 | `member` (tenant) → `contributor` (project: `firmware-ipcam`) | 在 project 內跑 workflow、push branch、看自己 project 的 audit | 不能讀其他 project 的 artifact / audit；不能改任何 secret；不能邀請新人 |
| **Dave** | QA / 測試工程師 | `member` (tenant) → `viewer` (project: `firmware-ipcam`) | 看 dashboards、看 workflow_run、下載 artifact、看 chatops mirror | 不能 trigger workflow_run / 不能 inject hint / 不能 modify SOP / 不能讀 secrets |
| **Eve** | 實習生 | `viewer` (tenant) | 只能進預設 project 看 dashboard、看公開 artifact | 不能進其他 project（除非被 explicit add）；不能看 audit；不能跑任何 mutator |
| **MachineKey** | CI service token | `service` (tenant，role flag 同 `member`) | 用 API key 跑 workflow_run、寫 artifact、不能用 web UI | 不能改 RBAC、不能讀其他 project、token rotate 後立刻失效 |

**S-1.1 設計斷言**：
1. **6 層 RBAC（super-admin / tenant-owner / tenant-admin / project-owner / project-member / project-viewer）能涵蓋這 6 個 persona** — `super-admin` 是平台方（OmniSight 廠商）跨 tenant 維運用，不算 Acme 內部角色；其餘 5 層 + `service` flag 一一對應。
2. **role 是 (user, scope) 的二維矩陣，不是 user 的單一欄位** — Bob 是 tenant-admin 但若 Acme 之後養出第二個產品線，Bob 在「IPCam 線」是 admin、在「Doorbell 線」可能只是 viewer。`users.role` 在 Y1 schema 將降級為「主 tenant 的快取欄位」、權威來源走 `user_tenant_memberships.role` + `project_members.role`（見 S-1.6）。
3. **service token 不是新角色** — 它是 `users.role='member'` + `enabled=true` + 不能登入 web UI 的 user row（透過 `api_keys` 表持有 token）。共用 RBAC 矩陣，不另起 capability 集合。

### S-1.2 LLM Quota Pool 共用模型

整家 Acme 用同一個 `tenant_quota_llm_tokens_30d` 預算（假設 enterprise plan = 100M tokens / 30d）。

**情境**：
- Carol 跑 `firmware-ipcam` 的 cross-compile workflow，10 分鐘耗 200K tokens（Claude Opus 評估 SDK 變更）。
- Bob 同時跑 `data-pipeline` 的 schema-migration 預演，5 分鐘耗 150K tokens。
- Eve 在 `t-acme` 預設 project 點開了 dashboard 但沒跑 workflow，0 tokens。
- MachineKey CI 觸發 30 個 nightly run，總計 8M tokens。

**所有用量寫進同一個 `tenant_quota` row**，T 系列 billing 端再用 `(tenant_id, project_id, user_id)` 三元 tuple 拆分歸因。

**S-1.2 設計斷言**：
1. **預算共用是 hard constraint** — quota 用盡後所有 user / 所有 project 一起被 throttle。Y8 dashboard 必須讓 Alice / Bob 一眼看到「目前 quota 70%、再 3 天滿」、避免被個別 project 的 burn-rate spike 突襲。
2. **歸因不是 enforcement** — `(tenant_id, project_id, user_id)` 用於計帳與 dashboard 切片；real-time gating 仍走 tenant 級的 atomic counter（見 `backend/llm_secrets.py` + 未來 Y6 的 `llm_token_meter`）。否則跨 project / 跨 user 的競賽寫法（每筆 LLM call 要看 N 個 row 才能放行）會成為熱點。
3. **個別 user 級 daily 上限是 nice-to-have** — Bob 可在 Y4 設 `Carol.daily_token_cap=2M`（透過 `project_members.metadata->>daily_token_cap`），但 tenant-level 是權威 ceiling。Eve 的角色預設 `viewer` 自然 quota=0（讀不觸發 LLM）。

### S-1.3 RBAC 控制 — 誰能改 secrets

**最高敏感操作**：寫入 / 讀取 / 撤銷 `tenant_secrets`（LLM API key、git PAT、Jira token、PagerDuty integration key、SMTP credentials）。

**權限矩陣**（**只列 secret 相關 endpoint**，完整矩陣見 [共用區段 §權限矩陣](#共用區段)）：

| 操作 | super-admin | owner | admin | member | viewer | service |
|---|---|---|---|---|---|---|
| `POST /api/v1/tenants/{tid}/secrets` | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `GET /api/v1/tenants/{tid}/secrets` (list w/ fingerprint) | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `GET /api/v1/tenants/{tid}/secrets/{id}/decrypt` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| `DELETE /api/v1/tenants/{tid}/secrets/{id}` | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `POST /api/v1/tenants/{tid}/secrets/{id}/rotate` | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| 在 workflow_run 內讀 secret（透過 `secret_store.decrypt`） | N/A | N/A | N/A | ✅ | ❌ | ✅ |
| 看 secret fingerprint（hash prefix 用於確認 rotate 對到正確 key） | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |

**S-1.3 設計斷言**：
1. **「改 secret」 vs 「讀 secret 明文」分權** — `admin` 能寫入 / 撤銷 / rotate，但只有 `owner` 能 `GET .../decrypt` 看明文。理由：rotate 不需要先看舊值（直接覆寫新值即可），admin 看明文沒有 legitimate use case；audit 鏈上「明文外洩」事件必然能定位到單一 owner。
2. **member 只能在 workflow_run 內讀 secret** — 不是直接 `GET /secrets/{id}/decrypt`，而是讓 workflow runner 走 `tenant_secrets.read(...)` 服務層注入到 sandbox 環境變數。Carol 寫的 build script 拿得到 `OMNISIGHT_LLM_API_KEY`（透過 sandbox env）但她在 web UI 看不到該 key 的明文 — UI 端看到 `sk-***...***fp=8a3c`。
3. **viewer 完全沒有 secret 讀寫能力** — Eve / Dave 連 fingerprint 都看不到（避免 fingerprint 反推或社交工程）。
4. **service token 不能改 secret** — MachineKey 即使 role=member，secret-mutation endpoints 仍 403。理由：service token 比 user 容易洩漏（CI logs / fork 同步），把 secret 改寫權限留給人類提供雙人核帳基礎。
5. **所有 mutation 必走 audit chain** — `tenant.secret_created` / `tenant.secret_rotated` / `tenant.secret_revoked` / `tenant.secret_decrypted_for_human`（最後一條尤其重要，回放時可以看「Alice 在 2026-05-03 14:22 看了 LLM key 明文」）。
6. **MFA + step-up auth** — `secrets/{id}/decrypt` 強制 MFA 重驗（沿用 K MFA 系列的 `mfa_challenges`）。即使 cookie session 還在，看明文要重打 OTP / TOTP。

### S-1.4 RBAC 控制 — 誰能開 project

| 操作 | super-admin | owner | admin | member | viewer | service |
|---|---|---|---|---|---|---|
| `POST /api/v1/tenants/{tid}/projects` | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `PATCH /api/v1/tenants/{tid}/projects/{pid}` (rename / budget) | ✅ | ✅ | ✅ (with caps) | project-owner only | ❌ | ❌ |
| `POST /.../projects/{pid}/archive` | ✅ | ✅ | ✅ | project-owner only | ❌ | ❌ |
| `DELETE /.../projects/{pid}` (硬刪) | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| `POST /.../projects/{pid}/members` (邀人入 project) | ✅ | ✅ | ✅ | project-owner only | ❌ | ❌ |
| `PATCH /.../project_members/{uid}` (改 project 內 role) | ✅ | ✅ | ✅ | project-owner only | ❌ | ❌ |

**S-1.4 設計斷言**：
1. **「admin 能改 budget but 有 caps」** — Bob 能把 `firmware-ipcam` 的 disk_budget 從 50GB 升到 100GB，但要在「tenant 總 budget」之內；想超 tenant 額度必須走 owner（plan upgrade）。
2. **「project-owner 是 project-scoped 的 owner」** — Carol 是 `firmware-ipcam` 的 project-owner 後，可以邀請 Dave 進來 / 改 Dave 的 role / 改 project budget（在 Bob 設的 cap 之內）— 但她**不能**對其他 project（如 `data-pipeline`）做任何事。這是 Y4 的核心 invariant。
3. **硬刪只 owner 能做** — 軟封存（`archive`）admin 就能做，硬刪要 owner（避免 admin 失誤 / 被入侵後一鍵清資料）。配 `?confirm={pid}` query param 雙重確認。
4. **service token 不能碰 project 結構** — 即使被攻破也只能對既存 project 做事，不能新開 project 規避 quota gating。

### S-1.5 RBAC 控制 — 誰只能讀

`viewer` 是「最大可能的訪問範圍 = 純讀取」。在 Acme 場景：

- **Eve（tenant-viewer）** — 看到 `t-acme` 預設 project 的 dashboard / workflow list / non-secret artifact / public audit summary。看不到：secret fingerprint、私有 project（除非 explicit `project_members` add）、Carol 跑 workflow 的 prompt 內文（包含 SDK key 等敏感資訊時 redact）、Bob / Alice 的 audit log。
- **Dave（project-viewer at firmware-ipcam）** — 在 `firmware-ipcam` 內，看 workflow_run 詳情、artifact 下載、chatops mirror、SOP / playbook；不能 trigger workflow_run、不能 inject agent hint、不能改 SOP / skill pack 內容；audit 只看到自己參與過的事件。

**S-1.5 設計斷言**：
1. **viewer 不是「沒 sidebar」** — viewer 仍進得去 dashboard，UI 要把 mutator 按鈕（trigger / inject / archive / settings）灰掉而非隱藏（避免 viewer 以為功能不存在）。Y8 frontend 的 RBAC gate 用 `<RequireRole min="operator">` HOC + 灰按鈕 fallback。
2. **viewer 對 audit 是「自我中心」過濾** — Dave 看 audit 只看到 `actor_user_id=Dave` 的事件 + `target.project_id=firmware-ipcam` 且 `actor in {Dave 自己, machinekey}`。理由：viewer 的職責是觀察自己 / 觀察 service token，不是稽核同事 — 同事稽核是 admin / owner 的事。
3. **secret_decrypted_for_human 對 viewer 全部 redact** — 即使 actor=Eve 也不該存在（viewer 不可能 decrypt）。但 audit 中提到的其他 actor 對 Eve 全 hide。

### S-1.6 schema 衝擊（與 Y1 對齊）

S-1 在 Y1 落地時對應的 schema 子集：

```
users
  id            uuid pk
  email         citext unique
  name          text
  password_hash text                          -- argon2id
  role          text default 'member'         -- 主 tenant 的快取，**非權威**
  tenant_id     text default 't-default'      -- 主 tenant 的快取，**非權威**
  enabled       bool default true
  ...

user_tenant_memberships    -- Y1 新表（S-1 權威來源）
  user_id     uuid fk users(id)
  tenant_id   text fk tenants(id)
  role        text                            -- owner / admin / member / viewer
  status      text                            -- active / suspended
  created_at  timestamptz
  last_active_at timestamptz
  PRIMARY KEY (user_id, tenant_id)

projects                   -- Y1 新表（S-1 用單一 default project）
  id            uuid pk
  tenant_id     text fk tenants(id)
  product_line  text default 'default'
  slug          text default 'default'
  ...

project_members            -- Y1 新表（S-1 內部 RBAC 細化）
  user_id       uuid fk users(id)
  project_id    uuid fk projects(id)
  role          text                          -- owner / contributor / viewer
  created_at    timestamptz
  PRIMARY KEY (user_id, project_id)
```

**S-1 對 Y1 / Y2 / Y3 的最小可行子集**：
1. **Y1 的 5 表中本 S-1 只強相依 3 表** — `user_tenant_memberships`、`projects`、`project_members`；`tenant_invites` 是 S-1.7 預留（見下）；`project_shares` 不在 S-1 scope（屬 S-3 跨租戶）。
2. **`users.role` 與 `users.tenant_id` 仍保留** — 為相容 I 系列已落地的 RLS / sandbox / audit chain，且 `OMNISIGHT_AUTH_MODE=open` 開發路徑要靠它。Y1 migration 規定：以 `user_tenant_memberships.role` 為**權威**、`users.role` 為**主 tenant 快取**；read-after-write 同步在 Y2/Y3 endpoint 套 transaction。
3. **不允許 `users` 沒對應 `user_tenant_memberships` row** — Y1 migration 後置 CHECK：`SELECT count(*) FROM users u WHERE NOT EXISTS (SELECT 1 FROM user_tenant_memberships m WHERE m.user_id = u.id AND m.tenant_id = u.tenant_id) = 0`。Y2 / Y3 寫 user 必同步寫 membership。

### S-1.7 Operator 工作流 — Acme 落地步驟

從 Acme 全新部署到 5 名工程師上線，operator（Alice）看到的時間軸：

1. **Day 0 — Bootstrap wizard**（沿用 Y7）  
   Alice 跑 `docker compose up -d` + 訪 `/bootstrap` → 設 tenant 名稱 `Acme Cameras`、tenant id 自動 slug 化 = `t-acme`、設 plan = enterprise、設首任 super-admin = `alice@acme.com` + 密碼。  
   wizard 結束後 DB 狀態：`tenants(id=t-acme, plan=enterprise)`、`users(alice@acme.com, role=admin, tenant_id=t-acme)`、`user_tenant_memberships(alice, t-acme, owner, active)`、`projects(t-acme, default, default)`、Alice 的 `project_members(alice, default-pid, owner)`。

2. **Day 0+5min — 設 LLM secret**  
   Alice 走 `/admin/tenants/t-acme/secrets` → 上傳 Anthropic API key + Jira token + PagerDuty key。  
   每筆 mutation 寫 audit：`tenant.secret_created` × 3。

3. **Day 1 — 邀請 Bob 為 admin**  
   Alice 用 Y3 endpoint `POST /api/v1/tenants/t-acme/invites {email: bob@acme.com, role: admin}`。  
   token 寄到 Bob 信箱（`notification_*` 走 email），Bob 點 `/invite/{token}` → 註冊密碼 → 自動 `user_tenant_memberships(bob, t-acme, admin, active)`。

4. **Day 1+10min — Bob 開 project `firmware-ipcam`**  
   Bob `POST /api/v1/tenants/t-acme/projects {product_line: embedded, name: "Firmware IPCam", slug: "firmware-ipcam"}`。  
   Bob 自動成為該 project 的 owner（`project_members(bob, fw-ipcam-pid, owner)`）。

5. **Day 2 — Bob 邀 Carol 為 contributor / Dave 為 viewer**  
   Bob 走 Y3 邀 Carol + Dave（兩者 tenant role 都是 member），再用 Y4 `POST /.../projects/firmware-ipcam/members` 把 Carol 設 contributor、Dave 設 viewer。Carol 觸發第一個 cross-compile workflow_run。

6. **Day 3 — Eve 加入為 tenant-viewer**  
   Bob 邀 Eve（tenant role=viewer），Eve 自動繼承 `t-acme` 預設 project 的 viewer 權限。Eve 看 dashboards 但 sidebar 列出的所有 mutator 按鈕都灰掉。

7. **Day 5 — Alice 設 daily LLM cap on Carol**  
   實習生 Eve 報告 Carol 一個 prompt 卡 30 次 retry 一晚燒 500K tokens（單人最大日量）。Alice 走 Y4 `PATCH /.../project_members/carol {metadata: {daily_token_cap: 250000}}`。下次 Carol 觸發 LLM call 時 `llm_secrets.guard(...)` 看到 cap、超量 throttle 回 429。

8. **Day 7 — service token rotation**  
   Bob 跑月度 secret 輪替 `POST /api/v1/tenants/t-acme/secrets/{id}/rotate`。舊 token 24h grace、之後 reject。CI runner 在 grace window 內 pull 新 token。

**S-1.7 設計斷言**：
1. **Bootstrap → invite → project → member 是線性 happy path** — 每步都有 idempotent 重做能力（重跑 invite 不重複建 user / 重跑 add member 不複製 row），support engineer 接手時可以從任何步驟繼續。
2. **viewer 加入無需 invite-by-default 也能看 default project** — Eve 加入後不必 Bob 再去 default project 加 viewer row（tenant role=viewer + `project_members` 缺 row 時 fallback 為 tenant role 對 default project 的 viewer 預設）。其他 project 仍要 explicit add（避免 Eve 進到 firmware-ipcam）。
3. **per-user daily cap 是 metadata jsonb** — 不另起表，避免 schema 提前優化；Y4 的 `project_members.metadata` 預留就能放（含 `daily_token_cap` / `weekly_token_cap` / `block_outside_hours` 等未來擴充）。

### S-1.8 邊界 / 退化情境

| 邊界場景 | 預期行為 | 驗收條件 |
|---|---|---|
| 唯一 owner 想離職 | UI 強制要求先指定第二個 owner、否則禁止 self-remove | Y3 endpoint `DELETE /.../members/{owner_id}` 在 owner_count==1 時 409 + message |
| 全 tenant 5 個 user、4 個都是 admin | 允許但 audit 會 surface「admin 集中度過高」warning（合規 / SOC2 提示） | Y9 audit observability 加 metric `tenant_admin_ratio` |
| owner 帳號 password reset 時 LLM secret 不可丟失 | reset 走 K MFA backup code 路徑、secret_store 的 fernet master key 不依賴 owner password | `secret_store.py` 已沿 KMS / env-derived key（Y 不退化此設計） |
| 邀請 email collision（Carol 已是其他 tenant 的 member） | 一律允許（user 可跨 tenant），新 invite 創 `user_tenant_memberships` row 而非 user row | Y3 `POST /invites/{id}/accept` logged-in 路徑覆蓋 |
| Bob 把 Alice 的 role 從 owner 降為 member | 拒絕 — admin 不能改 owner 的 role；只有 owner 能改 owner | Y3 `PATCH /.../members/{alice_id}` 在 actor=admin && target.role=owner 時 403 |
| 某 user 同時刪 LLM secret + 同一秒 Carol 跑需要該 secret 的 workflow | workflow runner 在 secret_store.decrypt 撈不到 → workflow_run 標 `secret_unavailable` 失敗、Carol 看到清楚錯誤訊息 | `secret_store.decrypt` raise 後 runner 掛 specific failure type、UI surface |
| viewer Eve 嘗試直接打 mutation API（繞 UI） | API server 端 RBAC dependency 仍 403 | Y5 `require_project_member(min_role="contributor")` test |
| 整 tenant disabled（plan 過期） | 所有 user 看到 banner「tenant disabled、聯絡 owner」、唯讀降級 | Y2 `PATCH /admin/tenants/{id} {enabled:false}` + frontend banner |

### S-1.9 Open Questions（標記給 Y1～Y10 後續勾選）

1. **「同 LLM quota pool 內、project 之間是否需要 weighted fair share？」** — S-1 假設 Acme 5 個工程師都是同公司、信任彼此；但若實際看到 `firmware-ipcam` 一直餓死 `data-pipeline`，需 DRF 機制。屬 [S-8 熱點撞牆](#s-8-熱點撞牆) scope。
2. **「viewer 能否被 self-promote 為 contributor？」** — 不允許（避免特權升級攻擊）；但 contributor 能否 self-demote 為 viewer？目前傾向允許（自我審慎），等 Y9 audit 模型化後決定。
3. **「per-user daily token cap 超量時的失敗模式」** — throttle to 0 還是降級到 `claude-haiku`？S-1.7 假設 throttle，但實作端要先決定（throttle 較簡單、降級需 `model_router.fallback_chain` 支援）。Y4 / Y6 需明確。
4. **「service token 是否需要獨立 RBAC 維度（如 ‘CI-only’ flag）？」** — S-1 假設 service token 用 user role 套用（共用矩陣）；但若 Acme 之後想限制 service token 只能 read project A 而 user 同時可 read A + B，需獨立 capability 集合。等 T 系列計費觀察 service vs user 用量比例後再決定。
5. **「viewer 對 chatops mirror 的能見度」** — Dave 是 project-viewer，看 chatops mirror 算是「閱讀」（OK）；但若 chatops 內含 PEP HOLD 對話（Bob 在批准 Carol 的 prod-deploy），Dave 看不看得到？目前傾向「看得到 mirror 但不能參與 button click」。屬 R9 / S-3 跨域問題，未定。

### S-1.10 既有實作的對照表

S-1 設計與目前 codebase（截至 2026-04-25）的對齊狀況：

| S-1 invariant | 目前狀況 | 缺口 |
|---|---|---|
| `users.tenant_id` 默認 `t-default` | ✅ `backend/auth.py:167` | 只支援 1:1，需 Y1 的 `user_tenant_memberships` |
| 3 階層 `viewer / operator / admin` | ✅ `backend/auth.py:47` (`ROLES`) | 需擴成 4 階層 `viewer / member / admin / owner`（Y1 重命名 + 加 owner）|
| `tenant_secrets` encrypt + audit | ✅ `backend/tenant_secrets.py` + `backend/secret_store.py` | 缺 `secret_decrypted_for_human` audit 事件（Y9）+ `decrypt` 強制 MFA（Y3） |
| `tenant_quota` 共用 disk pool | ✅ `backend/tenant_quota.py` | LLM token pool 尚未集中（散在 `llm_secrets.py` 計量、無單一 atomic counter）|
| `projects` 表 | ❌ | Y1 新建 |
| `project_members` 表 | ❌ | Y1 新建 |
| `user_tenant_memberships` | ❌ | Y1 新建 |
| 邀請流 | ❌ | Y3 新建 |
| RBAC dependency `require_project_member` | ❌ | Y5 新建 |
| Frontend tenant + project switcher | ⚠️ I7 已加 `X-Tenant-Id` header & localStorage prefix，但 UI 缺真實切換器 | Y8 |

**S-1.10 對 Y1 的關鍵 deliverable**：4 個新表（user_tenant_memberships / projects / project_members / tenant_invites）+ 1 個遷移 script（把 `(users.tenant_id, users.role)` 回填成 `user_tenant_memberships` row + 為每個 tenant 建一個 default project + 把現有 user 加為 default project 的 contributor）。回填策略對 `t-default` 也適用 — 屬 [S-9 遺留相容](#s-9-遺留相容) 範圍。

---

## S-2 多租戶單用戶

> 一名 MSP 顧問 / 共享 PM / 平台 SRE 同時隸屬多個 tenant；UI 內切 tenant、API 隨之自動 scope；每 tenant 內角色獨立。

> **與 S-1 的差異邊界**：S-1 假設 `(user, tenant)` 是 1:1，整套 RBAC 用 `users.tenant_id` + `users.role` 就能撐住；S-2 把 `(user, tenant)` 升為 N:N，使 `users.role / users.tenant_id` 從「權威欄位」降為「主 tenant 快取」、權威來源改走 `user_tenant_memberships`。S-2 不引入跨租戶資料分享（那是 [S-3 跨租戶協作](#s-3-跨租戶協作) 的責任）— 顧問同時看 3 個 tenant，仍是 3 條獨立的視窗，沒有「tenant A 的 artifact 在 tenant B 的 sidebar 出現」這種事。

### S-2.1 角色 Persona — 真實人物對應

以一家 MSP（managed service provider）`Bridge MSP` 為樣本，他們同時服務 3 個客戶 tenant：`t-acme`（S-1 的 Acme Cameras）/ `t-blossom`（Blossom Robotics）/ `t-cobalt`（Cobalt Drones）。Bridge MSP 自身是不是 tenant 看作可選 — 顧問的「主 tenant」可以是 `t-bridge-msp`（內部用作 SOP / playbook 集中地），也可以乾脆沒主 tenant、每次登入後從 `currentTenantId=null` 進入「請先選 tenant」狀態。

| Persona | 跨幾個 tenant | 在每個 tenant 的角色 | 該 do | 該 not do |
|---|---|---|---|---|
| **Maya** | 3 個（acme / blossom / cobalt）+ 主 tenant `t-bridge-msp` | `t-acme`: admin、`t-blossom`: member（contributor 於 `firmware-amr` project）、`t-cobalt`: viewer、`t-bridge-msp`: owner | 用 UI 一鍵切 tenant、API 自動換 scope；audit 看「Maya 在 t-acme 做的」與「Maya 在 t-cobalt 做的」是兩條獨立 chain | 任何單一 request 內不能同時對兩個 tenant 寫入；不能用 t-acme 的 LLM secret 跑 t-blossom 的 workflow_run |
| **Owen** | 2 個（acme / blossom）— 自由接案者，無主 tenant 或主 tenant=`t-default` | `t-acme`: member、`t-blossom`: viewer | 切 tenant 後 sidebar 重整；舊 SSE / WS 訂閱在切換時關閉再重開 | 不能把 t-acme 的 chatops mirror 文字 paste 到 t-blossom 的 SOP（雖然技術上做得到、屬人為失誤、audit 必然 surface） |
| **Pat** | 全部 tenant（platform-side super-admin） | super-admin | 跨 tenant 維運（plan 升降、tenant disable、forensic audit）；每次跨 tenant 動作都打 step-up MFA | 不直接以 super-admin 身分跑 workflow / 改 SOP（避免汙染 actor 軌跡）— 維運時建立短期「tenant-impersonation token」走代理身分 |
| **Carol（S-1 的 Acme 工程師）** | 1 個（acme） | `t-acme`: contributor（`firmware-ipcam` project） | 仍是 S-1 的單租戶用法；UI 不顯 switcher 或顯示「你只在 1 個 tenant」的灰色 badge | 不會被 S-2 改寫 — 1:N 是 1:1 的超集，S-1 持續成立 |
| **Eve（S-1 的 Acme 實習生）** | 1 個（acme），但實習結束後加入第 2 個（`t-blossom` 暑期計畫） | `t-acme`: viewer → `t-blossom`: viewer | 同 Eve 在 S-1 的行為，多一個 tenant switcher entry | 不能因為兩 tenant 都 viewer 就把兩 tenant 的 dashboard 混合（每次只看一個 active tenant）|

**S-2.1 設計斷言**：
1. **`(user, tenant)` 是 N:N、不再是 N:1** — `users.tenant_id` 仍存在但僅作為「最常用 / 預設 tenant」的快取（Maya 登入後預設進 `t-bridge-msp`）；新登入路徑必須立刻讀 `user_tenant_memberships` 拼出可用 tenant 清單，而非信任 `users.tenant_id` 單值。
2. **「主 tenant」是 UX 概念、不是 RBAC 概念** — 主 tenant 只決定登入後預設進哪個 scope；不影響權限。Maya 的主 tenant=`t-bridge-msp`，但她的 t-acme admin 權限不因此降階。
3. **super-admin 是平台方角色、不依賴 N:N** — Pat 的 super-admin 不要用 `user_tenant_memberships(pat, *, owner)` 表達（會掃 N rows），而是 `users.is_super_admin = true` 的單欄旗標 + 跨 tenant 必走 step-up MFA + audit。S-2 不弱化 S-1.1 對 super-admin 的設計斷言。
4. **service token 不適用 N:N** — Bridge MSP 的 CI runner 即使要碰 3 個客戶 tenant，也應為**每個 tenant 各發一把 service token**（避免單 token 洩漏導致跨客戶資料外洩、且 token rotation 可獨立進行）。
5. **「跨 tenant 即時切換」不是「跨 tenant 同時操作」** — Maya 在切 tenant 那一瞬間，前端應 abort 舊 tenant 的 in-flight requests / SSE / WS，避免 race（見 S-2.2）；後端在同一個 request 看到的 `X-Tenant-Id` 是切換後的、不會混淆。

### S-2.2 Tenant Switcher UX

從現有 I7 落地（`components/omnisight/tenant-switcher.tsx` + `lib/tenant-context.tsx` + `GET /api/v1/auth/tenants`）出發；S-2 要把它從「驗證 header 合法」升級為「真實 N:N 切換」。

**現況觀察**（截至 2026-04-25）：
- `GET /api/v1/auth/tenants` 已實作（`backend/routers/auth.py:283`）— 但 admin 看全部、非 admin 只看 `users.tenant_id`，**沒有 N:N 概念**（看不到 Maya 屬於 3 個 tenant）。
- `<TenantSwitcher>` 已實作（`components/omnisight/tenant-switcher.tsx`）— 切 tenant 只更新 localStorage 的 `_currentTenantId`、下次 fetch 帶上 `X-Tenant-Id`，但**不重整現有 SSE / WS 訂閱**、且**不告知後端這次切換是 user-driven**（無切 tenant 的 audit）。
- `_tenant_header_gate` middleware（`backend/main.py:625-661`）— 收到 `X-Tenant-Id` 後比對 `user.tenant_id`、不同則需 `user.role == 'admin'`。**這個邏輯在 S-2 必須改寫**（見 S-2.3）。

**目標 UX**（S-2 落地後）：

| 步驟 | 用戶動作 | 前端反應 | 後端反應 |
|---|---|---|---|
| 1 | Maya 登入（無 `X-Tenant-Id`） | TenantContext 拉 `GET /auth/tenants` → 拿到 4 個 tenant、預設選主 tenant `t-bridge-msp`、頁面渲染為 bridge-msp scope | session cookie 寫入；`db_context.set_tenant_id('t-bridge-msp')`；audit 加 `auth.login` |
| 2 | Maya 點開 switcher、選 `t-acme` | (a) abort 所有 in-flight `/api/*` requests；(b) close SSE `/api/v1/events/sse`、WS `/api/v1/chatops/ws`；(c) 清空 React-query / SWR cache；(d) localStorage 寫 `t-acme`；(e) 觸發頁面 soft-reload 或 router.refresh()；(f) 重 mount sidebar / dashboard 元件 | 前端發 `POST /api/v1/auth/active-tenant {tenant_id: "t-acme"}` → 後端寫 audit `auth.tenant_switched` + 更新 session 的 `last_active_tenant_id`（用於下次登入預設） |
| 3 | Maya 對 t-acme 的 project_x 發 fetch | `X-Tenant-Id: t-acme` 自動帶 | middleware 比對 `user_tenant_memberships(maya, t-acme)` 存在且 `status=active`、注入 RLS scope |
| 4 | Maya 切回 `t-bridge-msp` | 同步驟 2 但目標 tenant 不同；UI 不該卡住任何「from t-acme」的 stale state | 同 audit |

**S-2.2 設計斷言**：
1. **切 tenant 必須是「軟重整」(soft reload) 而非「無感切換」** — 切 tenant 後保留現有 React state 是 footgun；Maya 在 t-acme 開了「artifact 預覽 modal」、切到 t-blossom 時 modal 必須關閉，否則 modal 內的 image src 會帶錯 tenant scope（artifact_id 對不上 t-blossom 的儲存）。實作策略：tenant context 用 `key={currentTenantId}` 包住整個 `<AppLayout>`，切 tenant 觸發 React 整 sub-tree 重 mount。
2. **SSE / WS 必須在切 tenant 時關閉並重連** — 否則舊連線繼續推 t-acme 的事件、但前端已渲染為 t-blossom 的視角，UI 會錯亂。連線 close 由 React-effect cleanup 處理：`useEffect(() => { const es = new EventSource(...); return () => es.close() }, [currentTenantId])`。
3. **`POST /auth/active-tenant` 是 best-effort 的書籤 / audit 寫入** — 不是 RBAC gate（gate 由 middleware + memberships 表做）。後端就算 5xx 失敗、前端切 tenant 仍生效（headers 已換）；只是失去 audit 與「下次登入預設」的便利。S-2 endpoint 設計強調 idempotent、可重試、不阻塞 UI。
4. **「切到自己沒 membership 的 tenant」必須在前端就擋掉** — switcher 的選項本來就只列 `GET /auth/tenants` 回傳的合法 tenant；但 URL deep-link（如 `/?tenant=t-stranger`）需要 frontend reject + 後端 middleware 第二道防線（403 + 引導回主 tenant）。
5. **「無 membership 切 tenant」用 403 而非 404** — 避免 tenant id enumeration；403 回應只說 "Tenant not accessible"、不說 "Tenant t-stranger does not exist vs you don't have access"。S-1.3 設計的「fingerprint 不洩漏」原則延伸到這。
6. **switcher 必須區分 `enabled=false` tenant** — Plan 過期 / 退訂的 tenant 在 switcher 灰掉但仍可選（讓 owner 進去做 last-resort 動作如 export data），但選中後 banner 提示「tenant disabled、唯讀」（呼應 S-1.8 邊界場景）。

### S-2.3 API 自動 Scope — middleware 升級

現況 `_tenant_header_gate`（`backend/main.py:626`）的判斷：

```python
if header_tid != user.tenant_id and user.role != "admin":
    return JSONResponse(403, ...)
```

這個邏輯在 S-1（1:1）下成立，但 S-2 必須改寫為：

```python
# 偽碼，Y2 / Y5 落地時實作
async def _tenant_header_gate(request, call_next):
    header_tid = request.headers.get("x-tenant-id")
    if not header_tid:
        # 無 header：fallback 用 user.tenant_id（向下相容 I 系列既有 path）
        return await call_next(request)
    user = ... # 從 session 解析
    if user is None:
        return await call_next(request)  # auth_mode=open or anonymous

    if user.is_super_admin:
        # super-admin 走任意 tenant，但必須是經過 step-up MFA 的「impersonation token」session
        require_impersonation_token(request, header_tid)
    else:
        # Y2: 查 user_tenant_memberships 而非 user.tenant_id
        membership = await fetch_membership(user.id, header_tid)
        if membership is None or membership.status != "active":
            return JSONResponse(403, {"detail": f"Tenant {header_tid} not accessible"})
    db_context.set_tenant_id(header_tid)
    return await call_next(request)
```

**S-2.3 設計斷言**：
1. **權威來源從 `users.tenant_id` 切換到 `user_tenant_memberships`** — middleware 每 request 查表會有效能負擔；Y2 必須加 in-memory LRU cache（key = `(user_id, tenant_id)`、TTL 60s、invalidate on memberships mutation）。Cache 失效寫成 explicit hook（add / remove membership 時 publish 一個 internal event 觸發 cache.purge），不要靠 TTL 撐 — 撤銷 membership 必須 ≤ 1s 全 worker 生效（避免「剛被踢出 tenant 的 user 還能用 60s」）。
2. **`X-Tenant-Id` 缺失時 fallback 行為要明確** — S-1 / I 系列 既有路徑大量假設「不帶 header → 用 user 自己的 tenant」、Y2 不能突然要求所有 endpoint 都帶 header（會炸所有舊 client）。Fallback 規則：無 header 且 `user.last_active_tenant_id` 存在 → 用它；否則用 `users.tenant_id`；否則用 `t-default`。三層 fallback。
3. **`db_context.set_tenant_id` 是 contextvar、worker 進程內 request-scoped** — 不依賴 module-global state，沒有 multi-worker 一致性問題（各 worker 各自設）。SOP Step 1 強制問題此處答案是「故意每 worker 獨立、本來就該如此」。
4. **POST `/auth/active-tenant` 是 audit + 更新 session 欄位、不是 RBAC gate** — 看到「Maya 在 14:22 切到 t-acme」純粹用於 forensic 與 UX bookmark；middleware 不依賴它做任何判斷（避免 audit 寫入失敗導致 user 被卡住）。
5. **`is_super_admin` 旗標 + step-up MFA 是雙保險** — `users.is_super_admin=true` + 想對非自己主 tenant 做事必須先打 `POST /auth/impersonation-token { tenant_id, mfa_code }`，回傳一張 5 分鐘短期 token 寫進 cookie；該 cookie 期間任意 tenant 切換不需重打 MFA。Y3 / Y9 落地。

### S-2.4 RBAC 跨租戶獨立性

最核心的 invariant：**Maya 在 t-acme 是 admin，不代表 Maya 在 t-blossom 也是 admin**。

權限解析路徑（pseudo code）：

```python
def resolve_role(user, tenant_id, project_id=None):
    # super-admin 跨 tenant
    if user.is_super_admin:
        return "owner" if has_impersonation_token(tenant_id) else None

    # tenant 級
    tm = fetch_membership(user.id, tenant_id)
    if tm is None or tm.status != "active":
        return None  # 403
    tenant_role = tm.role

    # project 級（若指定）
    if project_id:
        pm = fetch_project_member(user.id, project_id)
        if pm:
            # project role 凌駕 tenant role（更精細）
            return pm.role
        # 沒 project_members row → 用 tenant role 推導預設
        if tenant_role == "viewer":
            return "viewer"  # tenant-viewer 預設能 viewer 看 default project（S-1.7）
        return None  # tenant-member 沒 explicit add 不能進非 default project
    return tenant_role
```

**S-2.4 設計斷言**：
1. **role 解析永遠是 (user, scope) 二維、不查 user.role** — `users.role` 在 S-2 後等同「主 tenant 內的快取」、所有 RBAC 邏輯改走 `resolve_role(user, tenant_id, [project_id])`。Y2 / Y5 強制把這個 helper 提出單獨函式，現有 `auth.require_role(min_role)` dependency 會被 deprecated（讀者透過 `require_tenant_role / require_project_role` 兩個更明確的 dependency 替換）。
2. **「tenant role」與「project role」名稱不同 vocabulary** — S-1.6 的 schema 已經設好：tenant 級用 `owner / admin / member / viewer`，project 級用 `owner / contributor / viewer`。S-2 嚴守此區分；否則 Maya 在 `t-acme` 的 admin（tenant）和 `firmware-ipcam` 的 contributor（project）混淆會引發 RBAC bug。
3. **跨 tenant 操作必須是不同 session-state 維度** — 後端不能記住「Maya 是 admin」這件事；只能記「Maya in t-acme 是 admin」。所有 capability 檢查在 dependency 注入時帶 `tenant_id`（從 X-Tenant-Id middleware 注入到 request.state），絕不從 `user.role` 推。
4. **既有 `auth.current_admin` / `current_admin_user` 等 dependency 必須改名為 `current_tenant_admin`** — Y2 內 codemod；同時保留舊名為 alias 1 release 緩衝、加 DeprecationWarning。
5. **「跨 tenant 升級」要寫 audit** — Maya 在 t-acme 是 admin、被 Alice 升為 owner，這條 audit 寫進 t-acme 的 chain（不是寫進 t-bridge-msp 也不是寫進 Maya 的「個人 audit」）。

### S-2.5 Audit 防混淆 — Cross-tenant Audit Hygiene

當同一個 user 跨 N tenant 操作時，audit 鏈最容易出 cross-contamination bug。

**S-2 落地後 audit 模型**：
- `audit_events.tenant_id` 永遠是「事件發生在哪個 tenant 的 scope」、不是 actor 的主 tenant。
- `audit_events.actor_user_id` 是 user.id（跨 tenant 唯一）。
- 同一個 user.id 出現在 N 條 audit chain 是合理的、且這 N 條 chain 各自獨立 hash（chain head 不混 — 詳見 R5 audit chain 模型）。

**Maya 視角的 audit 查詢**：

| 查詢 | 預期回傳 |
|---|---|
| `GET /api/v1/audit?actor_user_id=maya` (Maya 自己) | 跨 4 個 tenant 的所有事件，但 UI 用 tab 切 tenant、不混合時間軸 |
| `GET /api/v1/audit?tenant_id=t-acme&actor_user_id=maya`（t-acme owner Alice） | t-acme 內 Maya 的事件 — Alice 看得到（她是 owner） |
| `GET /api/v1/audit?tenant_id=t-cobalt&actor_user_id=maya`（t-acme owner Alice） | 403 — Alice 不是 t-cobalt 的 owner，看不到別人的 audit |
| `GET /api/v1/audit?actor_user_id=maya`（t-cobalt 某 viewer） | 只看到 `tenant_id=t-cobalt AND actor_user_id=maya` 的事件（即 viewer 自我中心過濾不允許跨 tenant） |

**S-2.5 設計斷言**：
1. **Audit chain 嚴格 per-tenant 隔離** — t-acme 的 chain head hash 與 t-cobalt 的 chain head hash 互不相關；Maya 在 t-acme 跑了 100 個事件、不影響 t-cobalt chain。R5（audit chain）落地時的 PG advisory lock 已 per-tenant 切，S-2 不弱化。
2. **「Maya 個人時間軸」是 UI 端 fan-out 查詢、不是新表** — 不要為「跨 tenant 個人 audit」單獨建 `user_audit_global` 之類的表；UI 並列 N 個 `tenant_id=X` 的查詢結果即可（避免雙寫 / 一致性 bug）。權限上：每個 query 各自走 RBAC 過濾，所以「我看得到的」自然是合法子集。
3. **Audit row 的 `subject` payload 不能順手把 `actor_tenant_id` 寫成 actor 的主 tenant** — 一個常見 footgun 是 `audit.write({ actor: user.id, actor_tenant: user.tenant_id, ... })`，但 user.tenant_id 是主 tenant 快取、不是當下 active tenant。Y9 落地時把 `actor_tenant` 欄位整個移除（事件 `tenant_id` 已存）、並在 audit row schema 標記 actor 是 user.id（純 user 維度，不混 tenant 維度）。
4. **「跨 tenant」action 在 audit 上要 surface 為兩條相關事件** — 例如 Pat（super-admin）對 t-acme 啟用 plan 升級：(a) 寫一條 `tenant.plan_changed` 進 t-acme 的 chain，(b) 同時寫一條 `platform.tenant_admin_action` 進 platform-internal audit（以 `tenant_id=NULL` 或 `tenant_id="_platform_"` 表達）。這條鏡像由 Y9 處理。

### S-2.6 schema 衝擊（與 Y1 對齊）

S-2 對 Y1 schema 的特別關注點（S-1.6 已列大部分欄位、此處只列 S-2 增量）：

```
user_tenant_memberships    -- Y1 新表（S-2 中真正成為「N:N 權威來源」）
  user_id     uuid fk users(id)
  tenant_id   text fk tenants(id)
  role        text                            -- owner / admin / member / viewer
  status      text                            -- active / suspended / pending_invite
  is_primary  bool default false              -- S-2 新增：主 tenant 旗標（每 user 至多 1 row 為 true）
  created_at  timestamptz
  last_active_at timestamptz                  -- S-2 用：tenant switcher 排序提示
  PRIMARY KEY (user_id, tenant_id)

users
  ...
  is_super_admin bool default false           -- S-2 新增：取代 role='admin' 的跨 tenant 含義
  last_active_tenant_id text                  -- S-2 新增：login 時的預設 tenant，可被前端 POST /auth/active-tenant 更新
  ...

sessions
  ...
  active_tenant_id text                       -- S-2 新增：當前 session 在用的 tenant
  impersonation_expires_at timestamptz        -- S-2 新增：super-admin step-up token 有效期（NULL = 一般 session）
  impersonation_target_tenant text            -- S-2 新增：step-up 的目標 tenant（NULL = 未 impersonate）
```

**S-2.6 設計斷言**：
1. **`is_primary` 約束是 partial unique index、不是 trigger** — `CREATE UNIQUE INDEX ON user_tenant_memberships (user_id) WHERE is_primary = true`，PG 原生語法、寫入時 atomic、不需 row-level trigger 維護。Maya 換主 tenant 時走「先 set false、再 set true」的兩步交易（同 transaction 內）。
2. **`is_super_admin` 與 `users.role='admin'` 不重複** — `is_super_admin` 是平台級（跨 tenant 維運）；`users.role='admin'` / `user_tenant_memberships.role='admin'` 是 tenant 內角色（admin within scope）。Y1 migration 把現有 `users.role='admin'` 不自動升為 `is_super_admin`、需要 operator 在 bootstrap wizard 顯式勾選「平台 super-admin = ✅」。
3. **`sessions.active_tenant_id` 與 `users.last_active_tenant_id` 不同** — 前者是當前 session 的 active scope（每次 `POST /auth/active-tenant` 更新）；後者是「下次新登入時的預設」（只在 logout / new login 時讀）。兩欄分開避免「Maya 登一個 session 切到 t-acme、開另一 browser 登第二 session 預設仍是 t-bridge-msp」這種反直覺行為。
4. **既有 `users.tenant_id` 不可移除（兩階段）** — Y1 階段並存（雙寫 + 雙讀、`user_tenant_memberships` 是權威）；Y10 階段才能移除欄位（要等所有 read sites 改完）。S-2 不負責收尾（屬 [S-9 遺留相容](#s-9-遺留相容)）。
5. **`pending_invite` 狀態 row** — Maya 接到 t-cobalt 的邀請但尚未接受時，先寫 `user_tenant_memberships(maya, t-cobalt, viewer, status='pending_invite')`，accept 後改 `active`。狀態為 `pending_invite` 的 row 不出現在 `GET /auth/tenants`（S-2.2 switcher 只列 `status='active'`）；屬 Y3 範圍。

### S-2.7 Operator 工作流 — Maya Onboarding 三家客戶

Maya 加入 Bridge MSP 後從 0 ＜→＞ 同時服務 3 家客戶的時間軸：

1. **Day 0 — Bridge MSP 內部 onboarding**  
   Bridge MSP 的 owner Mark 在 `t-bridge-msp` tenant 走 Y3 邀 Maya（role=owner，因為 Maya 是 partner-level 顧問）。  
   Maya 接邀請 → 自動建立 `users(maya@bridge.com)` + `user_tenant_memberships(maya, t-bridge-msp, owner, active, is_primary=true)`。Maya 主 tenant = t-bridge-msp。

2. **Day 3 — Acme 簽顧問合約**  
   Acme owner Alice 走 Y3 `POST /tenants/t-acme/invites { email: maya@bridge.com, role: admin }`。  
   Maya 收到邀請信、點 link 進入 `/invite/{token}` → 偵測她已登入 → `POST /invites/{id}/accept`（不需要重新註冊密碼，沿用既有 user）。  
   寫入 `user_tenant_memberships(maya, t-acme, admin, active, is_primary=false)`。  
   Maya 的 switcher 從 1 個 entry 變 2 個。

3. **Day 3+5min — Maya 第一次切 tenant**  
   Maya 點 switcher 選 `t-acme` → 前端 abort + reload → `POST /auth/active-tenant {t-acme}` → audit 寫 `auth.tenant_switched(maya, from=t-bridge-msp, to=t-acme)`。  
   Maya 開始在 t-acme 跑工作。

4. **Day 7 — Blossom 簽顧問合約（限時 POC）**  
   Blossom owner Bryce 邀 Maya 為 member（不是 admin，因為這次只是 POC 顧問），Maya 也加入 Blossom 的 `firmware-amr` project 為 contributor（Blossom 的 admin 走 Y4 add member 流）。  
   `user_tenant_memberships(maya, t-blossom, member, active)` + `project_members(maya, amr-pid, contributor)`。

5. **Day 14 — Cobalt 緊急 audit 顧問**  
   Cobalt owner Cher 邀 Maya 為 viewer（純 audit forensic），不開 project member。Maya 進去 Cobalt 後 sidebar 全是 viewer 灰按鈕、僅能看 audit 與 dashboard。

6. **Day 30 — Acme 合約結束、撤銷 Maya 權限**  
   Alice 走 Y3 `DELETE /tenants/t-acme/members/maya`；Maya 在 t-acme 的 active session 立刻被踢（cache invalidate）；Maya switcher 自動移除 t-acme entry；audit 寫 `tenant.member_removed`。

7. **Day 60 — Bridge MSP 總部變更**  
   Mark 想把 Maya 從主 tenant 改為 Owen；Mark 走 `PATCH /memberships/{maya@bridge}/{t-bridge-msp} { is_primary: false }` + `PATCH /memberships/{owen@bridge}/{t-bridge-msp} { is_primary: true }` 兩步交易。  
   Maya 下次登入預設 tenant 變成 `t-blossom`（依 last_active_at 排序遞補）。

**S-2.7 設計斷言**：
1. **「邀請已存在的 user」不應該另建 user row** — Y3 邀請流必須先 lookup `users.email`、命中則改寫 `user_tenant_memberships`；不命中才建新 user。否則 Maya 會在系統有 4 個 user row（每加入一個 tenant 就一個 user），密碼分裂。
2. **撤銷 membership 不刪 user row** — Maya 的 user row 永久存在（為了 audit 完整性），只是該 tenant 的 membership row 變 deleted（軟刪）或 row-level RLS 隱藏。Audit 中 `actor_user_id=maya` 的事件繼續可被 owner 查到。
3. **「主 tenant 唯一」由 partial unique index 保證** — 同 S-2.6 設計斷言 1。
4. **每次 invite-by-email 必走 step-up MFA**（避免社交工程） — Alice 邀 Maya 進 admin 等於把 Acme 的「改 secret」權限交給 Maya，這比 Alice 自己改 secret 還危險（attacker 控制 Alice → 邀請自己進 admin → 跳過 owner-only 的 decrypt）。Y3 invite endpoint 必須沿 K MFA 系列強制 step-up。
5. **撤銷 membership ≤ 1s 全 worker 生效** — middleware cache invalidation 要主動 publish event、不能等 60s TTL（同 S-2.3 設計斷言 1）。

### S-2.8 邊界 / 退化情境

| 邊界場景 | 預期行為 | 驗收條件 |
|---|---|---|
| Maya 在切 tenant 中途、舊 tenant 的 SSE event 抵達 | 前端 EventSource 已 close、event 被 GC、不影響 UI | Y8 frontend test：`switchTenant()` 後立刻 fire 一個假 SSE event、不該觸發任何 React update |
| Maya 同時開 4 個 browser tab、每個切到不同 tenant | 每 tab 各自 localStorage `_currentTenantId` 隔離（per-tab state via tabId-prefixed storage） — 否則 tab A 切 tenant 影響 tab B | Y8 frontend：driver-level test 開 2 tab 切不同 tenant、各自 fetch 帶不同 X-Tenant-Id |
| Maya 切到 tenant、tenant 在 5 秒前才被 disabled | switcher entry 顯示 disabled（灰色 + tooltip "Tenant disabled, contact owner"）；點仍可進入但 banner 提示「唯讀」、所有 mutator 禁用 | Y2 `enabled=false` tenant 的 RBAC 降級（讀允許、寫拒絕） |
| Maya 在 t-acme 跑 workflow_run、跑到一半切到 t-blossom | t-acme 的 workflow_run 繼續跑（背景任務、與 frontend session 解耦）；UI 切回 t-acme 時還能看到完成狀態 | R 系列 workflow_run 不依賴 frontend session、E2E 測：Maya 切 tenant 不中斷 backend job |
| Maya 切到沒 membership 的 tenant id（URL 直接打） | middleware 403 + frontend 收到後重導回主 tenant | Y2 middleware test：偽造 `X-Tenant-Id: t-stranger` 應 403 |
| Maya 在 t-acme 的 session、session age 30min 後 revalidate 時 t-acme 已撤銷她的 admin | revalidate 時 fetch_membership 回傳 status=suspended → 403 → 前端踢回 login（或主 tenant） | Y3 session refresh 路徑必查 memberships |
| Pat（super-admin）忘記用 step-up token、直接打 `X-Tenant-Id: t-acme` | middleware 看到 `is_super_admin=true` 但無 impersonation token → 403 + 引導打 `POST /auth/impersonation-token` | Y3 step-up MFA endpoint + middleware 嚴格檢查 |
| Maya 的 sessions row 出現 active_tenant_id=t-acme 但 memberships 同步被刪 | 下次 request middleware fetch_membership=None → 403 → session 內 `active_tenant_id` 自動清空 / 視為 stale | Y2 middleware：缺 membership 時清 `sessions.active_tenant_id` |

### S-2.9 Open Questions（標記給 Y1～Y10 後續勾選）

1. **「Maya 切 tenant 時的 in-flight HTTP request 怎麼處理？」** — abort 是激進、有些 idempotent GET 可以放它跑完；但對 mutation 必須 abort。落地時是否 case-by-case 還是一律 abort？目前傾向「全 abort + UI 顯示 loading 重新拉」、Y8 落實。
2. **「per-tab `_currentTenantId` vs per-window 共享」** — S-2.8 列「per-tab 隔離」是預設，但 UX 上「同視窗開新 tab 預期繼承當前 tenant」的人也很多。等 Y8 做使用者訪談決定。
3. **「super-admin 的 `is_super_admin` 跨 tenant 操作要不要寫進 target tenant 的 audit chain？」** — S-2.5 設計斷言 4 說「兩鏡像」（target tenant 鏈 + platform 鏈），但 Pat 一天可能跨 10 個 tenant、寫進 10 條 chain 會放大 audit row size — 還是改寫到 platform-internal 但給 target tenant 的 owner 一個「鏡像可見」flag？等 Y9 audit observability 落地時決定。
4. **「主 tenant 為空（is_primary 全 false）的 user 怎麼辦？」** — Maya 主 tenant 移除後又沒有別的 active tenant，登入後該預設進什麼？目前傾向「要求她在登入後第一個動作就指定主 tenant」（強制 modal），但若是 Pat 這種 super-admin 沒任何 tenant membership 也合理（純維運），需要例外路徑。Y2 / Y3 落地時釐清。
5. **「impersonation token 期間切換多 tenant」** — Pat 拿到 5 分鐘 step-up token、能不能在這 5 分鐘內切多個 tenant、每個都用此 token？目前傾向 token 綁 `impersonation_target_tenant` 單一 tenant、切到第二個要重新 step-up；但 Y9 forensic 場景可能需要 Pat 連跳 5 個 tenant — 等使用情境驗證後決定。

### S-2.10 既有實作的對照表

S-2 設計與目前 codebase（截至 2026-04-25）的對齊狀況：

| S-2 invariant | 目前狀況 | 缺口 |
|---|---|---|
| `(user, tenant)` N:N 權威表 | ❌ | Y1 新建 `user_tenant_memberships`（同 S-1.10）|
| Frontend tenant switcher UI | ✅ `components/omnisight/tenant-switcher.tsx` | 切 tenant 不重整 SSE / WS、無 audit 通知（Y8 升級） |
| Frontend tenant context | ✅ `lib/tenant-context.tsx` + `setCurrentTenantId(tid)` 自動寫 X-Tenant-Id | 切 tenant 沒 abort 舊 in-flight requests（Y8 升級） |
| `GET /api/v1/auth/tenants` | ✅ `backend/routers/auth.py:283` | admin 看全部、非 admin 只看單一 — 需改為查 `user_tenant_memberships`（Y2 修） |
| `_tenant_header_gate` middleware | ✅ `backend/main.py:625-661` | 用 `users.tenant_id` 比對、需改為查 `user_tenant_memberships`（Y2 修） |
| `POST /api/v1/auth/active-tenant` | ❌ | Y3 新建（更新 `sessions.active_tenant_id` + audit） |
| `POST /api/v1/auth/impersonation-token` | ❌ | Y3 新建（super-admin step-up MFA） |
| `users.is_super_admin` 旗標 | ❌ — 目前 `users.role='admin'` 雙重含義（既是「t-default tenant 的 admin」也用於 middleware bypass） | Y1 加欄位 + bootstrap wizard 顯式勾選 |
| `users.last_active_tenant_id` | ❌ | Y1 新增欄位 |
| `sessions.active_tenant_id` | ❌ | Y1 新增欄位 |
| `sessions.impersonation_expires_at` / `impersonation_target_tenant` | ❌ | Y1 新增欄位 |
| Membership cache invalidation | ❌ | Y2 新建（middleware-level LRU + invalidate hook） |
| Per-tab `_currentTenantId` 隔離 | ⚠️ 目前 localStorage 全 window 共享 | Y8 評估後決定（見 S-2.9 Q2） |
| 切 tenant 重整 SSE / WS | ❌ — `useEventSource` hook 不依賴 `currentTenantId` | Y8 升級 |
| 切 tenant abort in-flight fetch | ❌ | Y8 升級（AbortController 綁 tenantId） |
| 切 tenant 寫 audit | ❌ | Y3 endpoint + Y9 audit |
| Tenant switcher 顯示 `enabled=false` | ⚠️ tenants 列表已含 `enabled` 欄位、UI 渲染為 `opacity-50` 但仍可選 | Y8 加 banner / tooltip 提示降級行為（呼應 S-1.8） |

**S-2.10 對 Y2 / Y3 的關鍵 deliverable**：
1. **Y2 middleware 升級** — `_tenant_header_gate` 改查 `user_tenant_memberships`，加 LRU cache + invalidation hook；`GET /auth/tenants` 改查 N:N 表。
2. **Y3 新增 endpoint** — `POST /auth/active-tenant`、`POST /auth/impersonation-token`、邀請流接受路徑要支援「已登入 user 增 membership」（不另建 user row）。
3. **Y8 frontend 升級** — switcher 切 tenant 觸發 abort + reload（用 `key={currentTenantId}` 包 `<AppLayout>`）、SSE / WS 連線在 `currentTenantId` change 時 close & reconnect、加 audit POST 通知後端。

---

## S-3 跨租戶協作

> tenant A（**host**）想把自己某個 project 開放給 tenant B 的 user（**guest**）看（唯讀）或讓他評論（read-comment），但 guest 不應該看到 host 的其他 project / 其他 secret / 其他 audit；guest 在 host 的視角是「**在自己的 tenant 裡看到一個被 mount 進來的外部 project**」、不是「臨時切到 host tenant」。

> **與 S-1 / S-2 的差異邊界**：S-1 是「同 tenant 多 user」、S-2 是「同 user 多 tenant 但 tenant 之間互不交集」、S-3 是首次出現「**資料跨 tenant 邊界流動**」的情境 — host 的某個 project row 在 guest tenant 的 sidebar 出現、host 的 audit 事件在 guest 視角部分可見、host 的 LLM 預算可能因 guest 的瀏覽 / 評論而消耗。S-1 / S-2 的所有 invariant 仍持續成立（guest 看 host 的 project ≠ guest 變成 host tenant 的 member）；S-3 在這之上加一條新的權威表 `project_shares` 與 guest 視角的 fence 規則。

### S-3.1 角色 Persona — Host / Guest 雙視角

接續 S-1 的 Acme Cameras（`t-acme`）+ S-2 的 Bridge MSP（`t-bridge-msp`）。新增第三家 `Cobalt Drones`（`t-cobalt`，無人機公司，與 Acme 合作 ISP 韌體 ）作為 host / guest 互補樣本。

| Persona | 主 tenant | guest 身份 | 該 do | 該 not do |
|---|---|---|---|---|
| **Alice**（Acme owner，host 端決策者） | `t-acme` (owner) | — | 對 `firmware-ipcam` project 發 share invite 給 cobalt 的 Cher、設 role=`commenter`、設 expires_at=90d、撤銷 share | 不能對外分享自己沒 owner 權限的 project（即使 tenant-owner 也要 project-owner 雙簽）；不能把 share role 拉高到 `contributor`（最高 commenter）；不能無限期 share（必設 expires） |
| **Cher**（Cobalt owner，guest 端決策者 / 接收者） | `t-cobalt` (owner) | `t-acme` 的 `firmware-ipcam` project guest viewer | 在 cobalt tenant 的 sidebar 看到「**Shared with us**」分組，內含 `firmware-ipcam`（標 host=acme + 紅 guest badge）；接受 / 拒絕 share；設 cobalt 內部誰可以代表 cobalt 進去看 | 不能把 host 的 project 在 cobalt 內 fork 為自己的 project；不能把 host project 內的 artifact 下載後再上傳到 cobalt 自己的 project（DRM-style 無法強制，但 audit 會 surface） |
| **Cody**（Cobalt 韌體工程師，guest 實際使用者） | `t-cobalt` (member, contributor of `t-cobalt`'s `drone-isp` project) | `t-acme` 的 `firmware-ipcam` project guest commenter | 在 cobalt UI 內進入「Shared with us → firmware-ipcam」、看 workflow_run / artifact / SOP；可在 artifact 上加 comment（注 `actor=cody@cobalt.com tenant=t-cobalt`）給 acme 的 Carol 看 | 不能 trigger workflow_run（即使 commenter）；不能 inject agent hint；不能讀任何 acme 的 secret（即使 fingerprint）；不能看 acme 的 `data-pipeline`（acme 內 Cody 沒有任何 visibility） |
| **Carol**（Acme 韌體工程師，host 端 project owner） | `t-acme` (member, contributor → S-3 後升 owner of `firmware-ipcam`) | — | 在 share 操作中是「project 端授權者」（與 tenant-owner Alice 雙簽授權）；在 firmware-ipcam 的 comment thread 看到 cody 的留言、可回覆 | 不能單獨授權跨 tenant share（必須同時有 project-owner + tenant-owner / admin 簽）；不能撤銷其他 project 的 share |
| **Eve**（Acme 實習生 viewer） | `t-acme` (viewer) | — | 看 firmware-ipcam dashboard 時可看到 sidebar 提示「此 project 已分享給 1 個外部 tenant（cobalt）」、但看不到 share 細節（who can comment / when expires） | 不能看 share 詳情（屬 admin / project-owner-only）；不能撤銷 share |
| **Mark**（Bridge MSP owner） | `t-bridge-msp` (owner) | （MSP 不在 S-3 直接情境裡，但作為「N:N user 同時是 guest 在多 host」的對照） | 若 bridge MSP 之後同時被 acme / blossom 各 share 1 個 project 進來 → 在 bridge tenant sidebar 看到 2 個 shared-with-us project、各自獨立 fence | 不能用 acme share 進來的 project 的 artifact 在 blossom share 進來的 project 裡引用（跨 host 互不可見） |

**S-3.1 設計斷言**：
1. **「Guest」不是 tenant 內角色、是 share 邊界 metadata** — guest 角色的 user 仍然完全屬於 guest tenant（`user_tenant_memberships(cody, t-cobalt, member, active)`）、不在 host tenant 建任何 row；host 視角看到的「guest」是透過 `project_shares` 反查得來。這保證撤銷 share 時 guest user 在 host 端立刻消失（不需要刪 user / membership / role row 三處）。
2. **Share 授權需「雙簽」** — host 端的 project share 必須同時有：(a) **project-level owner 同意**（Carol 是 firmware-ipcam owner）+ (b) **tenant-level owner / admin 同意**（Alice / Bob）。理由：project owner 知道內容該不該給外部看（業務判斷）、tenant owner 知道對方 tenant 是否可信（合規 / 法務判斷）；單一一方都不夠安全。Y4 endpoint `POST /.../projects/{pid}/shares` 必須在 backend 強制這個 quorum（不能只信任 frontend）。
3. **Guest role 的最大值是 `commenter`（不能是 contributor / owner）** — 跨 tenant 的 RBAC ceiling 設在 `commenter`、即使 host 想開放更多也不允許。理由：contributor 能 trigger workflow_run（要燒 host LLM quota）、能改 SOP（影響 host 內部其他 user），這兩個都不該由跨 tenant 帳號控制；想真正讓對方 contribute、就應該邀請對方為 host tenant 的 member（走 S-2 invite 流），而非 guest。
4. **Share 必須有 `expires_at`（hard cap 1 年）** — 永久 share 是合規大忌（離職員工、退休合作夥伴一直保有 access）；UI 預設 90d、最大 365d、續期需要重新雙簽。Y1 schema 在 `project_shares.expires_at` 上設 `NOT NULL` + CHECK constraint 強制。
5. **Guest 在 host 端 audit 是 first-class actor** — `audit_log.actor` 一直記成 `cody@cobalt.com`（不是匿名化、不是 `guest@*`），但**多加一欄** `actor_external_tenant_id='t-cobalt'`，讓 host 端的 audit 篩選時能 surface 「是哪個外部 tenant 的人做的」、避免事後追責失準。

### S-3.2 Share 模型 — 三維度權限合成

S-3 的權限是「(guest_user, host_project, share_role)」三維度合成、與 S-1 / S-2 的「(user, scope) 二維」並列。完整解析路徑：

```python
# 偽碼，Y4 / Y5 落地時實作
def resolve_role(user, tenant_id, project_id=None):
    # super-admin 跨 tenant — 同 S-2.4
    if user.is_super_admin:
        return "owner" if has_impersonation_token(tenant_id) else None

    # 1) S-2 既有路徑：user 是 tenant_id 的 member？
    tm = fetch_membership(user.id, tenant_id)
    if tm and tm.status == "active":
        # 走 S-1 / S-2 的「member 二維解析」(已實作於 S-2.4)
        return _resolve_member_role(tm, user.id, project_id)

    # 2) S-3 新路徑：user 是 tenant_id 內某 project 的 guest？
    if project_id is None:
        return None  # 沒有 tenant membership 又沒指定 project_id → 不能進 tenant scope

    share = fetch_active_share(project_id, guest_tenant_id=user.tenant_id)
    if share is None or share.expires_at < now():
        return None  # 403

    # guest fence：guest user 必須在自己的 tenant 內也是該 share 的 in-scope user
    # （見 S-3.4 設計斷言 2 — share 是 tenant-level grant，不是 user-level）
    guest_membership = fetch_membership(user.id, user.tenant_id)
    if guest_membership is None or guest_membership.status != "active":
        return None  # guest user 連自己 tenant 都不在 → 403

    # share role 可被 guest tenant 端再往下收緊（不能往上）
    capped = _cap_role(share.role, guest_membership.role)
    return capped  # 可能是 "viewer" / "commenter" / None
```

**S-3.2 設計斷言**：
1. **Guest 路徑與 member 路徑完全不交叉** — `resolve_role` 先判斷「user 是不是這個 tenant 的 member」、是就走 S-1 / S-2；否則才看 share。一個 user **不可能同時是 host tenant 的 member 又是 guest**（若 Cody 哪天加入 acme 為 member，share 路徑就被 short-circuit、走 member 路徑，避免雙重路徑導致 RBAC 推導歧義）。Y2 / Y5 dependency 強制這個排他性。
2. **Share 是 tenant-to-project 的 grant、不是 user-to-project 的 grant** — `project_shares.guest_tenant_id` 是 `t-cobalt`、不是 `cody@cobalt.com`。理由：(a) host 端不需要知道 guest tenant 內哪些 user 該 access，由 guest tenant 自己 RBAC 決定；(b) guest user 異動（離職 / 加入）不需要 host 端配合改 share；(c) audit 上仍能透過 `actor_user_id` 知道是 guest tenant 內哪個 user 真正動作。
3. **Guest 端可「再下調」role、不能「再上調」** — host 設 share role=`commenter`，guest tenant 內：(a) 預設所有 active member 都繼承為 commenter；(b) guest tenant 的 admin 可在 cobalt UI 設「只有 cody / cher 能進這個 share」（fence 縮小）；(c) 但 guest tenant 不能把 share role 升為 contributor（即使 host 同意也不行 — 因為 guest 的角色是跨 tenant ceiling）。Y4 endpoint 用 `_cap_role(share_role, guest_member_role_for_this_share)` 取 min。
4. **`fetch_active_share` 必查 `expires_at`** — 不能依賴 cron 撤銷過期 share；middleware 每 request 檢查（避免 cron 延遲導致過期 share 仍被使用）。配 LRU cache（key=(project_id, guest_tenant_id)，TTL 30s + invalidation hook on share mutation）。
5. **`resolve_role` 對 share 路徑回傳 `None` 時必須 403、不能 fallthrough 到 viewer** — 這個 fence 是安全邊界，不存在「降級為 viewer 是友善 default」的設計空間；任何 fallthrough 都是 cross-tenant leak。

### S-3.3 Audit 雙鏈寫入（host + guest）

S-3 是 audit chain 設計最複雜的一段：guest user 在 host project 內的每個動作必須**同時寫入兩條 chain**（host 的 + guest 的），且兩條鏈的內容**部分對稱、部分非對稱**。

**非對稱性的根源**：host 端要看到「Cody 來看了我的 artifact」（forensic / 客戶服務），guest tenant 端要看到「我的 user Cody 上週外出存取了 acme 的 4 個 artifact」（離職前審查 / 計費對帳）。但兩端不能完整看到對方鏈的全部 row（否則就是 leak）。

| 事件 | host (`t-acme`) chain 寫什麼 | guest (`t-cobalt`) chain 寫什麼 |
|---|---|---|
| Cody 第一次 access shared project | `audit.guest_session_started(guest=cody@cobalt.com, guest_tenant=t-cobalt, project=fw-ipcam, share_id=sh-123)` | `audit.cross_tenant_access(actor=cody, target_tenant=t-acme, target_project=fw-ipcam, share_id=sh-123)` |
| Cody 看 artifact `art-456` | `audit.artifact_viewed(actor=cody@cobalt.com, actor_external_tenant=t-cobalt, target=art-456)` | `audit.cross_tenant_artifact_view(actor=cody, target_tenant=t-acme, target_artifact_kind=image)` ← 注意：不寫 host artifact id（避免 host 內部 artifact 命名洩漏到 guest tenant audit） |
| Cody 對 artifact 加 comment | `audit.comment_added(actor=cody@cobalt.com, content=<full text>, target=art-456)` | `audit.cross_tenant_comment_posted(actor=cody, target_tenant=t-acme, target_kind=artifact)` ← 注意：不寫 comment 內文（避免 host 內部討論進到 guest tenant audit） |
| Alice 撤銷 share | `audit.share_revoked(actor=alice@acme.com, share_id=sh-123, guest_tenant=t-cobalt, reason="contract_ended")` | `audit.cross_tenant_share_revoked(target_tenant=t-acme, target_project=fw-ipcam, share_id=sh-123, reason="contract_ended")` |
| Cody 在 cobalt 內部被 Cher 撤銷 access（cobalt 自己縮小 fence） | （**不寫**）— 這是 guest tenant 內部的權限變動、host 不該知道 cobalt 內部 RBAC 細節 | `audit.guest_fence_narrowed(actor=cher@cobalt.com, removed_user=cody, share_id=sh-123)` |

**S-3.3 設計斷言**：
1. **「雙寫」必須在同一 transaction 內** — 否則 host 寫成功但 guest 寫失敗會出現「acme 看到 Cody 來過、cobalt 卻說沒這事」的審計分歧。Y9 落地時用 PG 2-phase commit 還是 advisory lock + try/catch + retry — Y9 reviewer 抉擇；本 row 規範「**雙寫必須要嘛同成功要嘛同失敗**」+「失敗模式要求 client 收到 5xx 而非 silent partial write」。
2. **兩條鏈的 row 內容**「**對應但非鏡像**」 — host 鏈內容詳細（包含 artifact id、comment 內文）、guest 鏈內容是「sanitized 摘要」（只到 kind 層、不到 instance 層）。理由：guest tenant 的 audit 不該成為「窺探 host 內部命名空間」的後門。
3. **`actor` 在兩條鏈都是 `cody@cobalt.com`** — 不在 host 鏈匿名化為 `guest_001@t-cobalt`（會讓 forensic 查無此人 / 客訴對接時對不到人）；但 host 鏈額外加 `actor_external_tenant_id` 欄位（S-3.1 設計斷言 5）讓 host 端 audit 篩選器能「我只想看 cobalt 的人做了什麼」一鍵分組。
4. **`share_id` 是兩條鏈的 join key** — forensic 場景 (e.g. cobalt 與 acme 對帳「上個月 Cody 看了多少次」) 由 reviewer 用 `share_id` 在兩 tenant audit 各自查、比對 row count 一致 — 不靠任何「跨 tenant audit join API」（那會打破 audit chain 的 per-tenant 隔離 invariant）。
5. **撤銷 share 的 `reason` 是必填欄位** — 強制 enum {`contract_ended` / `security_incident` / `lifecycle_expiry` / `requested_by_guest` / `other`}+ 自由 text 補充。理由：撤銷 share 是高敏感事件、半年後回頭看 audit 必須能立刻知道「為什麼當初撤銷」、純空白 reason 會讓 forensic 失去脈絡。
6. **`audit.guest_fence_narrowed` 不外寫到 host** — guest tenant 內部的 RBAC 細節（cobalt 把 cody 從 share 移除、改派 daria）是 cobalt 的內部紀律、host 不該觸及；host 視角只看到「依然有人來自 cobalt」即可。

### S-3.4 Cross-Tenant Secret 隔離

S-3 引入「跨 tenant 邊界資料流動」、最危險的 leak 路徑是 secret。完整 fence 設計：

| 場景 | 預期行為 | 為什麼 |
|---|---|---|
| Cody（guest commenter）想 `GET /api/v1/tenants/t-acme/secrets` | 403 — guest role 從 `commenter` 推導對 secret 端的 capability=`null` | secret 端從來只開給 host tenant 內部 user（S-1.3 設計斷言 1-3）；guest role ceiling 在 `commenter`、與 secret read 完全不交集 |
| Cody 透過 share 內 commentable workflow_run 觸發某個 inline command (e.g. `/regen-artifact`) | 403 + 提示「此操作需 contributor 權限、guest 無法執行」 | guest ceiling 在 `commenter`，所有 mutator (含 trigger workflow_run、即使「只是 regen 既有 artifact」) 走 contributor capability check、guest 全 403 |
| Cody 在 comment 內 paste `sk-xxxx`（手動社交工程） | host 端 audit row `actor=cody, content="sk-xxxx"` 完整保留（自動 redact 反而失證據鏈完整性）；但 host 端 UI 顯示 comment 時自動 mask（`sk-***xxxx`）+ 顯示 banner「Comment contains potential secret pattern」+ host 端 owner 可手動申請刪除 row | 跨 tenant 來的 comment 是 untrusted input、host 端要假設可能含惡意 / 釣魚內容；但 audit 完整性高於 UI 美觀 |
| Acme 的 `firmware-ipcam` 跑的 workflow_run 內呼叫 Anthropic API 燒了 200K tokens、Cody 在 share 內看到該 workflow_run 的 cost panel | Cody 看不到 cost / token 用量（S-1.5 設計斷言 1 viewer 限制延伸：guest commenter ≤ host viewer 對 secret-derived field 的可見性） | LLM token 量是 secret-derived（揭露 host 內部成本結構、competitive 敏感）；guest 看 artifact 內容 OK、看 cost 不 OK |
| Cody 透過 cobalt 自己的 LLM secret 在 share 範圍內加 comment（cobalt 內部走 Claude API 提煉留言）| 走 cobalt secret、計費走 cobalt quota、cobalt audit 寫`llm.call_in_cross_tenant_share`；host 端 audit 寫 `comment_added`、不知道 cobalt 用了什麼 LLM | 跨 tenant share 的「LLM 用量歸屬」永遠記在 caller 的 tenant — 否則 host 會被惡意 guest 用 LLM 量打爆 quota（DoS） |
| host project 的 SOP markdown 內含 `${SECRET_LLM_KEY}` 模板變數、Cody 在 commentable view 看到的應該是什麼 | 看到 `${SECRET_LLM_KEY}`（**不展開**）— SOP 模板對 guest 永遠是 raw 形式；只有 host 內部 contributor 在 sandbox 內 trigger workflow_run 時才走 secret_store.decrypt 注入環境變數 | 模板變數展開（template substitution）只發生在 sandbox runtime；UI render 端永遠不展開 — guest 看到的就是 SOP 作者寫的原文 |

**S-3.4 設計斷言**：
1. **「跨 tenant 看到的」 ≤「host viewer 看到的」** — 任何 host 內部 viewer（如 Eve）看不到的資訊（secret fingerprint、LLM cost、internal audit、其他 project 名）guest 也看不到。這是 RBAC ceiling 的延伸：guest commenter 對「secret-derived」欄位的能見度 ≤ host viewer = `null`。Y5 implementation 要把 `is_guest_actor` flag 帶進每個 RBAC 檢查 dependency、預設黑名單模式。
2. **LLM 計費歸屬「caller pays」** — 跨 tenant share 內 guest 觸發的任何 LLM 呼叫（即使是 cobalt 自己想用 Claude 提煉留言） 走 caller (cobalt) 的 secret + quota；host (acme) 不為此扣費。Y6 / Y9 quota 計量必須在 LLM call site 帶 `caller_tenant_id` (而非 `resource_tenant_id`)。
3. **comment 內容是 untrusted input、必須在 UI render 端走 secret pattern detector** — frontend 對 `sk-...` / `ghp_...` / `xoxp-...` 等已知 secret prefix 自動 mask + 顯示 banner；audit 端保留原文（forensic 完整性）。Y8 frontend 落地時整合既有 `lib/secrets-detect.ts` (若不存在則新建)。
4. **SOP / template 對 guest 永遠 raw、不展開** — 這條 invariant 預防最隱蔽的一類 leak（SOP 內模板變數的展開時機若搞錯、guest 在 view-time 看到展開後的明文 secret）。Y4 / Y6 落地時 template engine 只在 workflow_run sandbox 內執行；UI render path 走 raw 模板。
5. **撤銷 share ≠ 撤銷既有 audit row 內的 secret 痕跡** — 即使 acme 撤銷對 cobalt 的 share，過去 90d 內 Cody 加的 comment 仍在 audit 內留存（含他在 comment 內 paste 過的 `sk-xxxx`）。host 端 owner 可走「申請 audit row 刪除」工單流（觸發 R5 audit chain re-verify、寫 `audit.row_redacted_post_review` meta event）。Y9 落地時要支援這條 redaction path、不能讓 audit 變成永久 secret 累積桶。

### S-3.5 schema 衝擊（與 Y1 對齊）

S-3 在 Y1 落地時對 schema 的增量（在 S-1.6 + S-2.6 既有設計上加）：

```
project_shares             -- Y1 新表（S-3 權威來源）
  id              uuid pk
  project_id      uuid fk projects(id)             -- host 端 project
  host_tenant_id  text fk tenants(id)              -- 冗餘（projects.tenant_id 可推），但讓 RLS index 容易
  guest_tenant_id text fk tenants(id)              -- 被 share 的對象 tenant
  role            text                             -- 'viewer' / 'commenter'（ceiling，S-3.1 設計斷言 3）
  granted_by      uuid fk users(id)                -- host 端發起授權的 user（雙簽中的 project-owner 端）
  approved_by     uuid fk users(id)                -- host 端 tenant-owner / admin 簽核 user（雙簽中的 tenant 端）
  expires_at      timestamptz NOT NULL             -- hard cap，CHECK (expires_at <= created_at + interval '365 days')
  status          text                             -- 'pending_guest_accept' / 'active' / 'revoked' / 'expired'
  revoked_at      timestamptz
  revoked_by      uuid fk users(id)
  revoked_reason  text                             -- enum check: contract_ended / security_incident / ...
  created_at      timestamptz
  CONSTRAINT no_self_share CHECK (host_tenant_id <> guest_tenant_id)
  UNIQUE (project_id, guest_tenant_id) WHERE status IN ('active', 'pending_guest_accept')

project_share_members      -- Y1 新表（S-3 guest tenant 內部 fence）
  share_id        uuid fk project_shares(id)
  user_id         uuid fk users(id)                -- 在 guest tenant 內被授權的 user（subset of guest_tenant 全員）
  role_override   text                             -- NULL = 繼承 share.role；非 NULL = 在 ceiling 內再下調
  added_by        uuid fk users(id)                -- guest tenant 內加入此 user 的 admin
  added_at        timestamptz
  PRIMARY KEY (share_id, user_id)

audit_log                  -- 既有表（S-3 加欄位）
  ...
  actor_external_tenant_id text NULL               -- S-3 新增：當 actor 是 guest 時的 origin tenant
  share_id                 uuid NULL fk project_shares(id) -- S-3 新增：跨 tenant 操作的 join key
  ...
```

**S-3.5 設計斷言**：
1. **`UNIQUE (project_id, guest_tenant_id) WHERE status IN ('active', 'pending')` partial index** — 同一 (project, guest tenant) 對至多有一個 active share（避免重複 share 撤銷時誤判）；舊的 revoked / expired row 留歷史證據、不參與唯一性。
2. **`CHECK (host_tenant_id <> guest_tenant_id)`** — 拒絕「自己 share 給自己」（S-2 範圍、不該走 S-3 路徑）。Y4 endpoint 在 backend 也要檢、不能只靠 DB constraint。
3. **`granted_by` + `approved_by` 不能是同一 user** — schema 不強制（不容易寫出簡潔 CHECK），但 Y4 endpoint 強制 `actor_doing_grant != actor_doing_approve`；理由 = S-3.1 設計斷言 2 雙簽要兩個人。
4. **`project_share_members.role_override` 走「下調 only」** — Y4 endpoint 在寫入時做 `_cap_role(share.role, role_override)` 的方向性檢查；DB 層留純 text 不做 CHECK（避免 enum 演進時要改 constraint）。
5. **`audit_log.share_id` NULL 是常態** — 大多數 audit row 是 host 內部動作、與 share 無關；只在 (a) guest 觸發的事件、(b) host 端對 share 本身的 mutation（grant / revoke）兩類 row 上非 NULL。Y9 加 partial index `WHERE share_id IS NOT NULL` 加速跨 tenant audit 篩選。
6. **既有 `users.tenant_id` 在 S-3 路徑下絕不參與 RBAC 推導** — `resolve_role` 路徑（S-3.2）只查 `user_tenant_memberships` + `project_shares`、永不讀 `users.tenant_id`。`users.tenant_id` 仍持續為「主 tenant 快取」（S-1.6 / S-2.6），在 S-3 路徑下完全是 noise — 防 reviewer 寫出「if user.tenant_id == project_shares.host_tenant_id」這類 nonsense check。

### S-3.6 Operator 工作流 — Acme 與 Cobalt 的 Joint Firmware 計畫

從 acme 與 cobalt 簽 NDA 到 cody 第一次 comment 的時間軸：

1. **Day 0 — 法務簽 NDA**（OmniSight 之外）  
   acme 與 cobalt 簽署「ISP 韌體聯合開發」NDA，明確「acme 將開放 firmware-ipcam project 給 cobalt 工程團隊唯讀 + 評論、為期 90 天、cobalt 工程師不能下載 source code 商用化」。

2. **Day 1 — Carol（project owner）發起 share request**  
   Carol 在 acme UI 走 `POST /api/v1/tenants/t-acme/projects/firmware-ipcam/shares { guest_tenant_id: "t-cobalt", role: "commenter", expires_in_days: 90, reason: "Joint ISP firmware POC" }`。  
   backend 寫 `project_shares(status='pending_tenant_approve', granted_by=carol, ...)`、發通知給 acme tenant admin/owner。

3. **Day 1+10min — Alice（tenant owner）批准雙簽**  
   Alice 在 admin notification 點 `Approve`、走 `POST /api/v1/tenants/t-acme/shares/{sh-123}/approve { mfa_code: 654321 }`（強制 MFA step-up，呼應 S-2.7 設計斷言 4）。  
   backend 改 `project_shares.status='pending_guest_accept'` + 寫 audit `share.granted_by_host(actor=alice, granted_by=carol, share_id=sh-123)`、發通知到 cobalt 的 owner Cher 信箱（含 OmniSight 內 deep-link）。

4. **Day 2 — Cher（guest tenant owner）審 + 接受**  
   Cher 點通知 link、登入 cobalt UI、看 `/cross-tenant-shares/incoming` 頁面，看到 acme firmware-ipcam 的 share invite（含 host 名稱、project 名稱、role=commenter、expires=Day 91）。Cher 點 `Accept`、走 `POST /api/v1/tenants/t-cobalt/incoming-shares/{sh-123}/accept`。  
   backend 改 `project_shares.status='active'` + 寫雙鏈 audit（`share.accepted_by_guest(actor=cher, ...)` 進 cobalt 鏈 + 鏡像 `share.guest_accepted(...)` 進 acme 鏈）。

5. **Day 2+5min — Cher 設 fence（只授權 cody + cher 自己進這個 share）**  
   `POST /api/v1/tenants/t-cobalt/incoming-shares/{sh-123}/members { user_ids: [cody, cher], role_override: null }`。  
   backend 寫 `project_share_members` 兩 row、寫 audit `guest_fence_set(actor=cher, share=sh-123, members=[cody, cher])`（**只進 cobalt 鏈、不外洩到 acme** — 呼應 S-3.3 設計斷言 6）。

6. **Day 3 — Cody 第一次進 share**  
   Cody 在 cobalt UI sidebar 看到「**Shared with us**」分組、點 `firmware-ipcam (acme)`、進入 share 視角。  
   middleware 在第一次 `GET /api/v1/projects/firmware-ipcam` 時 resolve_role 走 share 路徑 → 推出 commenter → 允許。寫 audit `audit.guest_session_started`（雙鏈）。  
   Cody 看 artifact 列表、點 view 一個 ISP 校準 image、加 comment「校準對 IR-cut 切換時偏色 2°、可調 GAMMA 曲線改善」。

7. **Day 5 — Carol 看到 Cody 的 comment 並回覆**  
   Carol 在 acme UI 看 firmware-ipcam project 的 comment thread、看到 Cody 的留言（標 cobalt guest 紅 badge）、回覆「明天 push commit `cobalt-isp-tune-v1.2` 修這條」。  
   寫 audit `comment_added(actor=carol)` 進 acme 鏈 + 鏡像 `comment_added_by_host_in_share` 進 cobalt 鏈。

8. **Day 60 — Eve（acme 實習生 viewer）進 firmware-ipcam dashboard 看到「此 project 已分享給 1 個外部 tenant」提示**  
   Eve 看 dashboard 發現「Cross-tenant access: 1 active share」灰色 badge、但點不開細節（屬 admin / project-owner-only）— 呼應 S-3.1 Eve persona。

9. **Day 91 — Share 自動 expire**  
   背景 cron task 每 1h 跑一次 `UPDATE project_shares SET status='expired' WHERE expires_at < now() AND status='active'`、同時對每個 expired share 寫雙鏈 audit `share.lifecycle_expired`。  
   middleware cache 自動 invalidate（呼應 S-2.3 設計斷言 1）；Cody 下次 fetch 收 403。  
   想續 share 必須走完整雙簽流程（不能只 PATCH expires_at）。

**S-3.6 設計斷言**：
1. **「pending_tenant_approve → pending_guest_accept → active」三段式狀態機** — host 內部雙簽 + guest 接受是兩個獨立 gate，缺一不可；host approve 後 guest 不一定要接受（cobalt Cher 也可拒絕）— 拒絕走 `POST /.../incoming-shares/{id}/reject` + status='rejected' + 雙鏈 audit。
2. **Cron 自動 expire 是 best-effort、不取代 middleware 即時檢查** — 呼應 S-3.2 設計斷言 4；cron 每 1h 跑只是讓 status field 視覺一致 + 觸發通知，但 RBAC gate 不依賴 cron（middleware 每 request 查 expires_at）。
3. **續 share = 開新 share** — 不允許 PATCH 既有 share 的 expires_at（會繞過雙簽）；想續期就 create 新 share row + revoked 舊 row（兩條 row 都留 audit、forensic 完整）。
4. **MFA step-up 對 host approve 強制、對 guest accept 也強制** — 雙方都是高敏感邊界動作（host 開門 + guest 接門）、都走 MFA；但 guest 端日常進入 share 不再 MFA（只第一次 accept 時）。

### S-3.7 邊界 / 退化情境

| 邊界場景 | 預期行為 | 驗收條件 |
|---|---|---|
| Cody（guest）的 cobalt membership 在 share 期間被撤銷（中途離職） | middleware 下次 request 走 `fetch_membership(cody, t-cobalt)` 看到 status≠active → 403；cobalt 端的 `project_share_members(cody)` row 不需 cascade 刪（保留 forensic） | Y2 middleware test：guest user 的 home tenant membership inactive 時、share 路徑也 403（不能用 share 路徑繞過 home tenant 的撤銷）|
| Acme 自己被 disabled（plan 過期） | 既有 share 自動進入 read-only 降級（呼應 S-1.8 邊界場景）— guest 仍可看但不能 comment；解 disable 後 share 自動恢復；不需手動 revoke 再 grant | Y2 middleware：tenant.enabled=false 時所有 mutator 拒絕、含 share 內的 comment endpoint |
| Cobalt 自己被 disabled | 既有 share 進入 read-only 降級 — cobalt 端 user 仍能看 host project 但不能 comment（出於對等原則） | Y2 middleware：guest_tenant.enabled=false 時對該 tenant 全 read-only、含跨 tenant share |
| Acme 想撤銷 share 但 Cody 正在加 comment 那一秒 | comment endpoint 已通過 middleware 檢查、寫入完成、回 200；下一個 request 走 fetch_active_share → revoked → 403。已寫入的 comment 在 audit 內保留 | Y4 endpoint：share revoke 不 retroactively 撤銷已存事件、只影響後續 request |
| Cobalt 接受 share 但 1 小時後改主意拒絕 | accept 後 1h 內可走 `POST /.../incoming-shares/{id}/reject` 撤銷接受（24h 寬限期 grace window）；超過 24h 想退出走 `POST /.../incoming-shares/{id}/leave` | Y4 endpoint：accept → reject 在 24h 內允許；之後改走 leave 路徑（語義不同：reject = 一開始就不要、leave = 用過了想撤）|
| Acme 把 firmware-ipcam project archive 了（軟封存） | 既有 share 自動降為「viewer-only」+ banner「Project archived, comments disabled」；guest 仍可看 read-only | Y4 archive endpoint：cascade 設 share 內所有 commenter 降為 viewer fence |
| Acme 把 firmware-ipcam project 硬刪 | 所有對該 project 的 share `status='cascade_deleted'` + 雙鏈 audit；guest 端 sidebar 自動移除該 entry | Y4 delete endpoint：cascade 處理 project_shares + project_share_members + 雙鏈 audit |
| Cody 在 comment 內貼了 `ghp_xxx` 這類 GitHub PAT pattern | host UI 自動 mask 顯示為 `ghp_***xxx` + banner「Comment contains potential secret pattern」+ host owner 可走 redaction 流；audit row 保留原文 | Y8 frontend：lib/secrets-detect.ts pattern matcher；Y9 audit redaction endpoint |
| Cobalt 想看「自己 user 在 acme 那邊看了什麼」（管理層審核） | 走 cobalt 內 audit 查 `cross_tenant_artifact_view`（kind 級摘要）— 看不到 acme 內部 artifact id；想看明細需要直接去問 acme（NDA + 法務） | S-3.3 設計斷言 2 對應 — guest 鏈是 sanitized，Y9 不違背 |
| acme 想對單一 cobalt user 限制 access（e.g. cody 但不要 daria） | 不允許 — share 是 tenant-to-project grant；想限制 user 由 cobalt 端設 fence (`project_share_members`)；acme 想 vetoe 某 user 必須整個 revoke share 重新發 | S-3.2 設計斷言 2 — host 不該知道 guest tenant 內細節、不該寫 user-level grant；UI 在 acme 端不顯示 cobalt user 列表 |

### S-3.8 Open Questions（標記給 Y1～Y10 後續勾選）

1. **「跨 tenant 的 LLM call attribution」深層細節** — S-3.4 設計斷言 2 寫「caller pays」，但若 host 端的 SOP 內某個 step 自動觸發 LLM call（即使是 guest 看 page 觸發的 lazy-load）— 該算 caller 還是 host？目前傾向「host 內的自動觸發 LLM 算 host 帳、guest 主動點 button 觸發算 guest 帳」、Y6 / Y9 落地時定。
2. **「Share 模型是否支援 chained share」** — Acme share 給 Cobalt、Cobalt 能不能再 share 給 Bridge MSP？目前傾向**禁止**（chain share 是 audit 與 RBAC 推導惡夢、且 NDA 通常不支援）— DB 層面用 `CHECK (NOT EXISTS subquery)` 還是 endpoint 層面拒絕？等 Y4 落地時確認可行性。
3. **「Comment thread 的 read receipt」** — Carol 想知道「Cody 看到我的回覆了沒」 — 這需要寫額外的 `comment_views` 表 + 雙鏈 audit；MVP 先不做、等實際使用反饋後決定。
4. **「Guest tenant 端的 LLM token cap on cross-tenant share」** — Cobalt 內部能不能對「對 acme share 的 LLM 用量」設專門 cap（避免 cobalt 員工狂在 acme 那邊用 LLM 燒 cobalt quota）？S-1.7 / S-1.8 的 per-user daily cap 模型可延伸、但 fence 維度是 (cobalt_user, acme_share, tokens_per_day) 三維、Y6 落地時設計。
5. **「Share metadata 的可見性對等」** — Cobalt 的 owner Cher 看到 acme 是「Acme Cameras（owner: alice@acme.com）」的程度有多細？目前傾向只露出 tenant display name + 邀請者 email、不露出其他內部 user / project / spend。但若 cobalt 法務要求「對方公司基本資訊揭露」可能需更多欄位 — Y4 落地時定。

### S-3.9 既有實作的對照表

S-3 設計與目前 codebase（截至 2026-04-25）的對齊狀況：

| S-3 invariant | 目前狀況 | 缺口 |
|---|---|---|
| `project_shares` 表 | ❌ | Y1 新建（同 S-1.10 / S-2.10 提到的 5 表之一）|
| `project_share_members` 表 | ❌ | Y1 新建（S-3 增量、S-1.6 / S-2.6 未列）|
| `audit_log.actor_external_tenant_id` 欄位 | ❌ — 現有 `actor` 是 single text email（`backend/audit.py:95,187`）| Y1 加欄位 + Y9 audit log path 同步寫 |
| `audit_log.share_id` 欄位 | ❌ | Y1 加欄位 + Y4 share 操作時寫 |
| 既有 `POST /report/share` endpoint | ✅ `backend/routers/report.py:97` — 只支援 signed read-only URL（無 user-scope、無 commenter、無 tenant 對 tenant grant）| **不衝突 / 不取代** — 兩條完全不同的 path：report-share 是「公開連結 + HMAC 簽名 + 24h 過期」對 PDF report；S-3 是「user × tenant × project × commenter」對 live project。Y4 文件要釐清「signed URL 適合分享 snapshot、project_shares 適合 live 協作」 |
| 既有 `task_comments` 表 | ✅ `backend/alembic/versions/0001_baseline.py:64-70` — `(id, task_id, author, content, timestamp)` task-scoped、無 tenant 隔離、無 RBAC 區分 read vs read-comment | **部分可重用 / 必須延伸** — 既有 schema 可作為 comment payload 模板、但需加 `actor_external_tenant_id` 欄位 + 改 author 從 plain text 升為 user_id reference + 加 `share_id` join key（Y1 / Y4）|
| `_tenant_header_gate` middleware (`backend/main.py:625-661`) | ✅ S-2 已規劃升級為查 `user_tenant_memberships`（S-2.3）| Y2 升級時要再加第二查路：(a) member 查 ✓ → 走 S-1/S-2 path、(b) member 查 ✗ → 接 share 查路（fetch_active_share + project_share_members）→ 走 S-3 path；兩條路順序不可逆（S-3.2 設計斷言 1）|
| `resolve_role` helper | ❌ — 既有 `backend/auth.py:47` `ROLES = ("viewer", "operator", "admin")` 是 global 階層、不是 (user, scope) 二維 | Y2 / Y5 新建 `resolve_role(user, tenant, project)`（S-2.4）、Y5 再延伸支援 share 路徑（S-3.2）|
| Frontend 「Shared with us」sidebar 分組 | ❌ — 既有 `lib/tenant-context.tsx` 只列 user 自己 membership 的 tenants、沒「跨 tenant guest project」 概念 | Y8 新增：tenant context 加第二個 list `incoming_shares: ProjectShare[]`、sidebar 分兩組（My projects / Shared with us）|
| Frontend `/cross-tenant-shares/incoming` page (guest 端 inbox) | ❌ | Y8 新建（settings 頁底下的 Shares tab）|
| Frontend `/projects/{pid}/settings` 內 Shares tab（host 端管理） | ❌ — 既有 `app/projects` 整個資料夾不存在（S-1.10 既已標）| Y8 新建（呼應 TODO 1773）|
| Comment redaction endpoint（host owner 申請刪除 audit row 內 comment 文字） | ❌ — 既有 audit row 是 immutable（呼應 R5 audit chain 設計）| Y9 新建：redaction 是「append meta event、不真改舊 row」式設計（保留 chain 完整性）|
| Secret pattern detector (frontend) | ❌ — 既有 `lib/` 目錄沒 `secrets-detect.ts`（grep `lib/*.ts*` 無命中）| Y8 新建：純前端 regex matcher、覆蓋 `sk-` / `ghp_` / `xoxp-` / `AIza` 等常見 prefix |
| Cron task：share 自動 expire | ❌ | Y9 新建（lifespan async task、每 1h 跑一次、寫雙鏈 audit）|
| MFA step-up for share approve / accept | ⚠️ K MFA 系列 ✅ session-level step-up、但無 endpoint-level enforcement | Y3 / Y5 沿用 S-2.7 規劃的 `require_mfa_step_up` dependency |

**S-3.9 對 Y1 / Y4 / Y5 / Y9 的關鍵 deliverable**：
1. **Y1 新增 2 表 + 2 欄位** — `project_shares` (15 欄) + `project_share_members` (5 欄) + `audit_log.actor_external_tenant_id` + `audit_log.share_id`；外加 partial unique index `(project_id, guest_tenant_id) WHERE status IN ('active', 'pending_guest_accept')` + `CHECK (host_tenant_id <> guest_tenant_id)` + `CHECK (expires_at <= created_at + interval '365 days')`。
2. **Y2 middleware 第二查路** — `_tenant_header_gate` 升級時加 share path（S-2 升級的 member path 之後）；確保 member-then-share 順序不可逆（S-3.2 設計斷言 1）。
3. **Y4 4 endpoint set** — `POST /.../projects/{pid}/shares` (host 端發起) + `POST /.../shares/{id}/approve` (host 端雙簽核) + `POST /.../incoming-shares/{id}/accept|reject|leave` (guest 端) + `PATCH /.../incoming-shares/{id}/members` (guest 端 fence)。
4. **Y5 `resolve_role` 延伸** — 在 S-2 規劃的 member 路徑後加 share 路徑（cap_role + guest_membership 雙檢查）。
5. **Y8 frontend** — sidebar 「Shared with us」分組 + `/cross-tenant-shares/incoming` 頁 + project settings 內 Shares tab + comment 內 secret pattern detector + guest badge 視覺。
6. **Y9 audit + cron + redaction** — 雙鏈 audit 寫入 transaction-safe / cron task / comment redaction endpoint（append meta event）。

---

## S-4 多產品線

> 一家硬體公司在同一個 tenant 下同時養多條產品線（IPCam / Doorbell / Intercom）。產品線之間**業務上獨立但共用公司資源**（同一份 LLM 計費合約、同一個法人 git org 名下、同一個合規邊界）— 但**運營細節必須隔離**：每條線各有 LLM 月預算上限、各自接到不同 git 倉庫 / 不同 default platform、各自 on-call rotation 不互通。

> **與 S-1 / S-2 / S-3 的差異邊界**：S-1 / S-2 / S-3 處理的是「user × tenant」的 RBAC 邊界；S-4 是首次出現「**tenant 內部資源垂直切分**」的情境 — 同一 tenant 內的所有 user 都還是同一個 RBAC 對象（沿用 S-1 / S-2 的 membership 模型不變），但「project / artifact / quota / secret / on-call」這層**資源繼承 / override 階層**多了一個中介層 `product_line`（`tenants → product_lines → projects → workflow_runs / artifacts`）。S-4 不引入新的 user 角色、不修改 share 模型；它純粹是把「tenant」一層的「LLM 預算 / git 整合 / on-call routing / SOP / skill pack」四類設定**再切片一次**，讓單一 tenant 也能 model 真實多產品線運營的隔離度。

### S-4.1 角色 Persona — Acme Cameras 三產品線

接續 S-1 的 Acme Cameras（`t-acme`）。Acme 從單純做 IPCam 起家、後來新增了 Doorbell（智能門鈴）與 Intercom（對講機）兩條產品線；公司不打算為這三線各開一個 OmniSight tenant（HR / 合約 / 合規邊界都是同一家公司、開三 tenant 過度切割）— 但運營上每條線必須有獨立的 LLM 月預算（避免某條線狂燒預算把另外兩條線餓死）、獨立的 git 整合目標（IPCam 走 GitHub Enterprise `acme/ipcam-*`、Doorbell 走 GitHub Cloud `acme-doorbell/*`、Intercom 走 內部 Gerrit `gerrit.acme.local/intercom/*`）、獨立的 on-call rotation（IPCam 線 PagerDuty schedule_id 不同於 Doorbell 線）。

| Persona | 主 tenant | tenant role | product line scope | 該 do | 該 not do |
|---|---|---|---|---|---|
| **Alice** | `t-acme` | owner | 全 3 條線（無 product_line scope） | 開新 product line、設 product line 預算、改 product line on-call key、跨 line 看用量 dashboard | （無上限。但 cross-line LLM 預算合計受 tenant plan ceiling 約束） |
| **Bob** | `t-acme` | admin | 全 3 條線（owner 預設賦權） | 開 / 關 product line 內 project、調 line-level git account、看 line audit | 不能 promote 自己為 product-line-owner（owner-only 動作）；不能改 tenant plan |
| **Pam** | `t-acme` | member | `pl-ipcam` (product-line owner) | 在 IPCam line 內開 project、改 IPCam line 的 LLM 預算（cap by tenant ceiling）、IPCam 的 git account default 切換、IPCam 的 on-call 排班 | 不能進 Doorbell / Intercom line；不能跨 line 看用量；不能改 tenant 級設定 |
| **Doris** | `t-acme` | member | `pl-doorbell` (product-line owner) | 在 Doorbell line 內開 project、改 Doorbell line 的 LLM 預算 / git account default / on-call schedule | 不能進 IPCam / Intercom line；不能跨 line 看用量 |
| **Ian** | `t-acme` | member | `pl-intercom` (product-line owner) | 在 Intercom line 內所有對應動作 | 不能進 IPCam / Doorbell line |
| **Carol**（S-1 韌體工程師） | `t-acme` | member | `pl-ipcam` (line member) → IPCam 內 `firmware-ipcam` project contributor | 在 IPCam line 內跑 workflow_run、push branch、看 IPCam line 的 LLM 用量 dashboard（read-only） | 不能讀 Doorbell / Intercom line 的 artifact / SOP / audit；不能改 IPCam line 的預算 / on-call |

**S-4.1 設計斷言**：
1. **`product_line` 不是 user 的角色屬性、是 user 在某 tenant 內 membership 的「scope filter」** — Pam 在 `t-acme` 仍然只有一個 `user_tenant_memberships` row（role=member），`product_line` 訪問權限走另一張表 `product_line_members(product_line_id, user_id, role)`。理由：(a) 同一 user 加入新 product line 不需要動 tenant membership row；(b) tenant role（owner/admin）天然繼承所有 product line 權限、無需在 product_line_members 重複寫 row；(c) S-1 既有 RBAC 階層不破壞。
2. **Tenant owner / admin 預設訪問所有 product line** — Alice / Bob 不需要在 `product_line_members` 各 line 寫 row；middleware fallback「tenant role ≥ admin → 任意 product_line OK」。`product_line_members` row 的存在意義是「member 級 user 被授權進入特定 line」。
3. **Product-line owner 不是新 RBAC 階層、是 product_line_members.role 的一個值** — `product_line_members.role ∈ ('owner','contributor','viewer')`、與 `project_members.role` 同 vocabulary；想真正在 IPCam line 內動 budget / on-call，必須是 `product_line_members(pam, pl-ipcam, role='owner')` + tenant role ≥ member。Y4 endpoint 在改 line-level 設定時 require `(tenant_role ≥ admin) OR (product_line_role == 'owner')`。
4. **Project 必屬於恰好一條 product_line** — 不允許「null product_line」/「跨 line project」（會破壞預算 / git / on-call 繼承的 deterministic 性）；遺留 `t-default` project 在 Y4 migration 時會被指派到 `pl-default` (見 S-9 範圍)。Y1 `projects.product_line_id` 必設 `NOT NULL`（Y4 落地時兩階段：先 NULL 允許 + backfill + 再 NOT NULL）。
5. **「product_line 等於 frontend `WORKSPACE_TYPES`（web/mobile/software）」是錯誤類比** — 既有 frontend `app/workspace/[type]/types.ts` 的 `WORKSPACE_TYPES = ('web','mobile','software')` 是**UX 視角的 workspace 變體**（不同產品線的 chrome 不同），與 S-4 的 product_line 是**business 維度的資源切分**完全不同層；S-4 不替換 / 不延伸 WORKSPACE_TYPES；同一個 product_line `pl-ipcam` 可能對應 `software` workspace、`pl-doorbell` 也可能、與 S-4 schema 完全 orthogonal。

### S-4.2 LLM 預算階層 — Tenant Ceiling × Product-Line Override

S-4 引入「**雙層 LLM 預算模型**」：tenant 級 ceiling 仍是 hard constraint（同 S-1.2 設計斷言 1），但每條 product_line 可在 ceiling 內**獨立配額**，避免單一 line 把全 tenant 預算燒光。

**配額模型**（Acme enterprise plan = 100M tokens / 30d 為例）：

```
tenant t-acme:           ceiling = 100M tokens / 30d
├── pl-ipcam:            budget  =  50M tokens / 30d   (override of tenant)
├── pl-doorbell:         budget  =  35M tokens / 30d   (override of tenant)
├── pl-intercom:         budget  =  10M tokens / 30d   (override of tenant)
└── 未分配 (pl-default): budget  =   5M tokens / 30d   (fallback / cross-line tooling)
                                  ─────────
                                   100M tokens   ← Σ(all line budgets) ≤ tenant ceiling
```

**配額檢查偽碼**（Y6 落地時實作，依靠 PG 原生 atomic counter）：

```python
# 偽碼，Y6 / Y10 落地時實作
async def check_llm_budget(tenant_id, product_line_id, tokens_to_consume):
    # 1) 先查 product_line 級配額（atomic counter）
    pl_remaining = await fetch_atomic("llm_meter:pl:" + product_line_id, "tokens_30d")
    if pl_remaining < tokens_to_consume:
        raise LLMQuotaExceeded(scope="product_line", id=product_line_id)

    # 2) 再查 tenant 級 ceiling（防止 Σ(line budgets) > ceiling 的 race；見設計斷言 4）
    tenant_remaining = await fetch_atomic("llm_meter:t:" + tenant_id, "tokens_30d")
    if tenant_remaining < tokens_to_consume:
        raise LLMQuotaExceeded(scope="tenant", id=tenant_id)

    # 3) 雙層 atomic decrement（同 transaction、要嘛同成功要嘛同失敗）
    await atomic_decrement_both(
        ("llm_meter:pl:" + product_line_id, tokens_to_consume),
        ("llm_meter:t:" + tenant_id, tokens_to_consume),
    )
```

**S-4.2 設計斷言**：
1. **Σ(product_line.budget) ≤ tenant.ceiling 是 backend invariant、不是 UI 約束** — Pam 想把 IPCam budget 從 50M 升到 60M，若三線合計超 100M 必須 reject + 提示「需先降 doorbell / intercom 或升 plan」。Y4 `PATCH /product_lines/{id}` endpoint 在 backend 走 `SELECT SUM(budget) FROM product_lines WHERE tenant_id=?` + 比對 ceiling、超則 409。
2. **Product-line 預算超用優先 throttle 該 line、不影響其他 line** — IPCam 燒到 50M tokens 觸發 throttle，Doorbell / Intercom 仍可正常用各自配額。理由：避免單一 line 把全公司預算燒光是 S-4 的核心 raison d'être；若降為「全 tenant 軟降級」，S-4 就退化為 S-1.2 的 cosmetic 版本。
3. **tenant 級 ceiling 仍是 hard gate**（呼應 S-1.2 設計斷言 1） — 即使 Σ(line budget) < ceiling，跨 line 的 audit 工具 / chatops bot / shared LLM call（屬 `pl-default`）也計入 tenant 級 counter。任何 line 的 atomic decrement 都會同步寫 tenant 級 row、防止「未分配 line」變成 quota 漏洞。
4. **雙層 atomic decrement 必同 transaction** — Race scenario：IPCam 與 Doorbell 同時各觸發 5M tokens call、tenant ceiling 剩 8M。若雙寫不同 transaction，可能兩邊 line counter 各 -5M 成功（line 各還剩 45M / 30M）但 tenant counter 變成 -2M（超 ceiling）。`atomic_decrement_both` 必走 PG `SELECT ... FOR UPDATE` 兩 row 一次鎖 + 任一不足則整批 rollback。Y6 落地時 SOP Step 1 必寫「合格答案 #2 — 透過 PG 序列化」釋因。
5. **Per-line budget 改動立即生效、不等下次 reset** — 與 cron / monthly reset 解耦：Pam 從 50M 改為 30M、若該 line 已用 35M 立刻進入 throttle 狀態（current_used 不歸零、只是 budget 降）；Y4 endpoint 寫 audit + 推 SSE notification 給該 line 的 owner。
6. **`pl-default` 不可被刪、為 fallback bucket** — 跨 line 工具（cross-line dashboard、tenant 級 audit observer、bootstrap wizard 自動觸發的 LLM call）走 `pl-default` 帳；Y1 schema enforce `pl-default` `is_system=true` + `DELETE` reject。

### S-4.3 Git 整合目標階層 — Per-Product-Line Default

S-4 的第二個切片維度：每條 product_line 各自的「**預設 git platform / org / 認證**」。

**現況**（既有 `git_accounts` 表，`backend/alembic/versions/0027_git_accounts.py:88-110`）：
- 唯一 unique index 是 `(tenant_id, platform)`、`(tenant_id, platform) WHERE is_default=true`；**沒有 product_line 維度**。
- Resolver 走 `WHERE tenant_id = ? AND platform = ? ORDER BY is_default DESC`、取第一筆。
- 結果：Acme 的 IPCam 工程師 push 到 `git@github.com:acme/ipcam-*`、Doorbell 工程師 push 到 `git@github.com:acme-doorbell/*` — **無法分開 default**，只能讓某一條線手動每次選 git account。

**S-4 要求的階層解析路徑**：

```python
# 偽碼，Y4 / Y6 落地時實作
def resolve_git_account(tenant_id, product_line_id, platform, *, prefer_label=None):
    # 1) 若 caller 顯式指名 label、先 try product_line scope
    if prefer_label:
        a = fetch_one(
            "SELECT * FROM git_accounts "
            "WHERE tenant_id=? AND product_line_id=? AND platform=? AND label=? AND enabled",
            tenant_id, product_line_id, platform, prefer_label,
        )
        if a: return a
        # 2) 否則 fallthrough 到 tenant scope（product_line_id IS NULL = tenant-wide）
        a = fetch_one(
            "SELECT * FROM git_accounts "
            "WHERE tenant_id=? AND product_line_id IS NULL AND platform=? AND label=? AND enabled",
            tenant_id, platform, prefer_label,
        )
        if a: return a

    # 3) 取 product_line 內預設
    a = fetch_one(
        "SELECT * FROM git_accounts "
        "WHERE tenant_id=? AND product_line_id=? AND platform=? AND is_default AND enabled",
        tenant_id, product_line_id, platform,
    )
    if a: return a

    # 4) 否則 fallback 到 tenant 級預設（既有路徑、不破壞 backward compat）
    a = fetch_one(
        "SELECT * FROM git_accounts "
        "WHERE tenant_id=? AND product_line_id IS NULL AND platform=? AND is_default AND enabled",
        tenant_id, platform,
    )
    if a: return a

    return None  # 由 caller 處理（提示「請設 git account」）
```

**S-4.3 設計斷言**：
1. **`git_accounts.product_line_id` 為 nullable、NULL 意義 = tenant-wide** — 既有所有 row 在 Y4 migration 時保持 `product_line_id IS NULL`、不破壞既有 resolver 行為。新 row 可選擇 (a) per-product-line（指 specific id）或 (b) tenant-wide（NULL）；resolver 走 line-scoped → tenant-wide fallback。
2. **同一 (tenant, product_line, platform) 至多一個 default** — partial unique index `WHERE is_default=true AND product_line_id IS NOT NULL`；既有 partial unique `WHERE is_default=true AND product_line_id IS NULL` 保持不變（共用同 column 不同 partial filter）。Y4 落地時兩個 partial index 並存。
3. **Resolver 順序: line-default → tenant-default**（不是 line-default → line-any → tenant-default → tenant-any）— 簡化推導：若 Pam 在 IPCam line 設了 GitHub default 帳 `acme-bot`、所有 IPCam workflow 一致用 `acme-bot`；line 內若沒 default、fallthrough 到 tenant-wide default；不允許「line 內任意非-default 帳被自動選用」（會引發無法預期的 push 目標）。
4. **Cross-line git account 不允許「leak」** — Doorbell line 的 git account 不能從 IPCam workflow 內被 resolve 到（即使 prefer_label 命中）；resolver 必檢 `product_line_id` 與 caller 的 `product_line_id` 相符 OR 為 NULL。Y5 落地時把 `(tenant_id, product_line_id)` tuple 帶進 git resolver context。
5. **`product_line.metadata->>git_org_hint` 是 UI 提示、不是 RBAC enforcement** — 例：IPCam line 在 metadata 寫 `git_org_hint='acme/ipcam-*'`、UI 在新建 git account 時 pre-fill；但 backend 不驗證 url_pattern 與 hint 一致（人類可能臨時用其他 org 做 POC）— hint 是 UX、enforcement 走 url_patterns 既有欄位（`git_accounts.url_patterns`）。
6. **Git account 是 secret 容器** — `git_accounts.encrypted_token / encrypted_ssh_key` 沿用 S-1.3 設計斷言 1-6 的 secret RBAC（read 明文 owner-only + step-up MFA + audit decrypt），不因為「per-line」而降級；line owner 想 rotate token 仍走 owner 雙簽路徑（line owner 是 product_line scope 但 secret rotate 跨進 tenant secret RBAC 邊界）。

### S-4.4 On-Call Routing — Per-Product-Line PagerDuty Schedule

S-4 的第三個切片維度：每條 product_line 各自的 on-call schedule。

**現況**（既有 `backend/notifications.py:1207-1246` `_send_pagerduty()` + `backend/routers/integration.py:117-134` settings）：
- `settings.notification_pagerduty_key` 是 **system-wide global** integration key（單一 PagerDuty integration、單一 routing key）。
- 所有 L4 critical event 都打到同一個 PagerDuty service / 同一個 schedule。
- Acme 的痛點：IPCam line 半夜出事、目前 page 到「整 acme 平均 on-call」、不一定是會看 IPCam codebase 的人；Doorbell on-call 早起被 IPCam P1 吵醒卻不能解。

**S-4 要求的 routing 階層**：

```
Notification.fire(severity=L4, tenant=t-acme, product_line=pl-ipcam, ...)
  ↓
fetch_oncall_routing(t-acme, pl-ipcam)
  → 1) product_line_oncall(pl-ipcam) → integration_key = "abc123" + schedule_id "PD-IPCam-NoC"
  → 2) 若 None: fallthrough tenant_oncall(t-acme) → 既有 settings.notification_pagerduty_key
  → 3) 若 None: fallthrough system default（既有 settings 全域 key、為了 t-default 既有用例）
  ↓
PagerDuty Events API V2 routing_key = "abc123"
  ↓
PagerDuty 內部 schedule "PD-IPCam-NoC" 派遣到 IPCam 線 on-call
```

**S-4.4 設計斷言**：
1. **On-call routing 解析走「最具體 → 最 generic」階層** — `product_line.routing → tenant.routing → system default`、與 git resolver 同模型；NULL 邊界明確（找不到時 fallthrough、不報錯）。理由：t-default 既有 user 不該因為 S-4 落地而需要強制設定 product_line 級 PagerDuty key。
2. **Routing key 是 secret、走 tenant_secrets / product_line_secrets**（**不**直接存 `product_line_oncall.encrypted_key` column） — 統一 secret 管理：所有 PagerDuty / Slack webhook / SMTP credentials 都走 `tenant_secrets` 既有表 + 新加 `product_line_id` nullable 欄位（同 S-4.3 git_accounts 模式）；`product_line_oncall_routing` 表只存「指向哪個 secret」+ schedule_id 等 non-secret metadata。理由：(a) 避免兩個 secret 倉庫；(b) 既有 secret RBAC + audit + MFA step-up 自動套用。
3. **Severity-tag → product_line tag 注入 PagerDuty payload** — 既有 `_send_pagerduty()` 的 `custom_details` 裡多加 `product_line: pl-ipcam`、`product_line_label: IPCam`、PagerDuty incident title prefix `[IPCam P1]` 取代既有 `[Acme P1]`；on-call 看 incident 一眼知道是哪條線出事。Y9 落地時改 notification_pagerduty 的 payload composer。
4. **Severity escalation 不跨 line** — IPCam P1 升級 P0、自動 escalate 到 IPCam line owner（Pam）+ tenant owner（Alice）；**不會** escalate 到 Doris / Ian。Y9 escalation graph 必帶 product_line scope；fallthrough 到 tenant owner 的條件嚴格（line owner 30min 沒 ack + tenant owner ack 後才能解開 incident）。
5. **on-call schedule_id 可空、payload 仍能送達** — 若 line 沒設 schedule_id（小公司只配 routing_key、靠 PagerDuty 內部固定 service routing），系統不該 reject；schedule_id 純 metadata、用於 audit 與 UI 顯示「誰是這條 line 當班」。Y9 落地時 schedule_id 是 nullable text。
6. **routing 變更走 audit + 通知舊 on-call** — Pam 把 IPCam line 的 on-call key 從 PagerDuty 換到 Opsgenie，audit 寫 `product_line.oncall_routing_changed(actor=pam, old_provider=pagerduty, new_provider=opsgenie)` + 立即 page 舊 / 新 on-call 一條測試 alert（避免 silent breakage：改了 key 卻沒測、下次真出事才發現新 key 配錯）。

### S-4.5 SOP / Skill Pack 共享範圍 — Tenant 全 vs Product-Line scoped

S-4 不直接動 SOP / skill pack 的 schema（屬 R 系列範圍），但 S-5（多專案同產品線）要求「Doorbell 下三 project 共用 Doorbell 的 SOP」、所以 S-4 需要先把「**SOP 在哪一層**」釐清。

**設計選擇**：SOP / skill pack 的 owning scope 是**可選的層** — 既可以 attach 在 tenant 層（全 acme 通用、跨 line）、也可以 attach 在 product_line 層（Doorbell 特化）、也可以在 project 層（單一 project 特化）。**繼承走 specific → generic**（與 LLM budget 相反方向）：

```
project firmware-doorbell-v2 想用「壓力測試 SOP」？
  → 先查 sop_resolver(project_id=fw-db-v2)         → null
  → 再查 sop_resolver(product_line_id=pl-doorbell) → 找到「Doorbell 標準壓測 SOP」 → 用之
  → 否則 fallthrough sop_resolver(tenant=t-acme)   → 找到 acme 全公司「壓測通用 SOP」
  → 否則 system default (R 系列既有 ROM SOP 庫)
```

**S-4.5 設計斷言**：
1. **SOP / skill_pack 表加 nullable `product_line_id` 欄位**（與 `git_accounts` 同模式） — 既有 R 系列 SOP 表 `sop_definitions(tenant_id, ...)` Y4 加 `product_line_id NULLABLE`；NULL 表 tenant-wide。Y4 落地時不 force migrate 既有 SOP 進入 line scope（保 backward compat）。
2. **Resolver 走 specific-first**（與 git account / on-call 路徑相反） — 因為 SOP 是「行為標準」，越具體的越精準（project SOP > line SOP > tenant SOP）；on-call / git 是「資源指派」，越 generic 越 fallback safety。本斷言預防 reviewer 誤把所有 resolver 設成同方向。
3. **Skill pack 同模型** — `skill_packs.product_line_id` nullable、預設 tenant-wide；但「LLM token 用量計帳」必跟著 caller 的 product_line（呼應 S-4.2 設計斷言 3）— skill pack 是被誰呼叫就計誰帳，不是 skill pack owner 的 line 計帳。
4. **跨 line copy SOP 是顯式動作、不是 inheritance** — Doorbell line owner 想用 IPCam line 的「韌體燒錄前流程」SOP — 必走 `POST /sops/{id}/clone {target_product_line: pl-doorbell}`；audit 寫 `sop.cloned_cross_line` + 新 row 在 doorbell line 內生成獨立版本（避免「同一份 SOP 跨 line 共享、IPCam 改一改 doorbell 跟著爆」的耦合）。
5. **Tenant-wide SOP 改動需 tenant admin / owner 簽** — line owner 不能改 tenant-wide SOP（會影響其他 line）；Y4 endpoint 強制 RBAC：(a) 改 tenant-wide SOP 需 tenant role ≥ admin、(b) 改 line-scoped SOP 需 product_line_role == owner OR tenant role ≥ admin。

### S-4.6 schema 衝擊（與 Y1 對齊）

S-4 在 Y1 / Y4 / Y6 落地時對 schema 的增量（在 S-1.6 + S-2.6 + S-3.5 既有設計上加）：

```
product_lines               -- Y1 新表（S-4 權威來源）
  id                  uuid pk
  tenant_id           text fk tenants(id) NOT NULL
  slug                text NOT NULL                       -- 'ipcam' / 'doorbell' / 'intercom' / 'default'
  display_name        text NOT NULL                       -- 'IPCam' / 'Doorbell' / 'Intercom' / 'Default'
  description         text
  llm_budget_tokens   bigint                              -- 30d budget; NULL = 不獨立 cap，依 tenant ceiling
  is_system           boolean NOT NULL DEFAULT false      -- 'pl-default' = true (S-4.2 設計斷言 6 防刪)
  archived_at         timestamptz
  created_at          timestamptz NOT NULL
  metadata            jsonb NOT NULL DEFAULT '{}'         -- e.g. {"git_org_hint":"acme/ipcam-*","color":"#0EA5E9"}
  CONSTRAINT no_default_archive CHECK (NOT (is_system AND archived_at IS NOT NULL))
  UNIQUE (tenant_id, slug)
  -- partial index: 一個 tenant 至多一個 is_system=true 的 line（pl-default）
  -- CREATE UNIQUE INDEX uq_product_line_default_per_tenant ON product_lines(tenant_id) WHERE is_system

product_line_members        -- Y1 新表（S-4 RBAC 補充表）
  product_line_id    uuid fk product_lines(id)
  user_id            uuid fk users(id)
  role               text NOT NULL                        -- 'owner' / 'contributor' / 'viewer'
  added_by           uuid fk users(id)
  added_at           timestamptz NOT NULL
  PRIMARY KEY (product_line_id, user_id)

product_line_oncall_routing -- Y1 新表（S-4.4 routing 階層）
  product_line_id    uuid fk product_lines(id) PRIMARY KEY
  provider           text NOT NULL                        -- 'pagerduty' / 'opsgenie' / 'slack' / 'none'
  secret_id          uuid fk tenant_secrets(id)           -- routing_key 存在 tenant_secrets，本表只指
  schedule_id        text                                 -- nullable; e.g. 'PD-IPCam-NoC'
  escalation_minutes integer NOT NULL DEFAULT 30          -- line owner 多久沒 ack 升級到 tenant owner
  metadata           jsonb NOT NULL DEFAULT '{}'
  updated_at         timestamptz NOT NULL
  updated_by         uuid fk users(id)

projects                    -- 既有 Y1 草圖（S-1.6 / Y1 row 1669）加欄位
  ...
  product_line_id    uuid fk product_lines(id) NOT NULL    -- S-4 加：每 project 必屬一 line
  ...
  -- partial unique index 既有: UNIQUE (tenant_id, product_line, slug)（Y1 row 1669 既已寫 `product_line` 為 string column）
  -- S-4 落地時把 Y1 row 1669 的 `product_line text` 改為 `product_line_id uuid fk`、Y1 row 1669 的 UNIQUE 也改為 (tenant_id, product_line_id, slug)

git_accounts                -- 既有 Alembic 0027 表加欄位
  ...
  product_line_id    uuid fk product_lines(id) NULL        -- S-4 加：NULL = tenant-wide（保 backward compat）
  ...
  -- partial unique 增量：
  -- CREATE UNIQUE INDEX uq_git_accounts_default_per_line_platform
  --   ON git_accounts(tenant_id, product_line_id, platform)
  --   WHERE is_default AND product_line_id IS NOT NULL
  -- 既有 uq_git_accounts_default_per_platform partial index 修改為:
  --   ... WHERE is_default AND product_line_id IS NULL（tenant-wide default）

llm_credentials             -- 既有 Alembic 0029 表加欄位（同 git_accounts 模式）
  ...
  product_line_id    uuid fk product_lines(id) NULL        -- NULL = tenant-wide
  ...

tenant_secrets              -- 既有 Alembic 0013 表加欄位（同 git_accounts 模式）
  ...
  product_line_id    uuid fk product_lines(id) NULL        -- NULL = tenant-wide
  ...
  -- 既有 UNIQUE (tenant_id, secret_type, key_name) 改為:
  -- UNIQUE (tenant_id, product_line_id, secret_type, key_name)
  -- 注意：UNIQUE 包含 NULL 列在 PG 預設視為「NULL ≠ NULL」、需用 partial unique 兩條:
  --   UNIQUE WHERE product_line_id IS NULL
  --   UNIQUE WHERE product_line_id IS NOT NULL

audit_log                   -- 既有表（S-3 已加欄位、S-4 再加）
  ...
  product_line_id    uuid NULL fk product_lines(id)       -- S-4 新增：line scope filter（Y9 partial index）
  ...
```

**S-4.6 設計斷言**：
1. **新表 3 張**（`product_lines` + `product_line_members` + `product_line_oncall_routing`）+ **既有表加欄位 5 張**（`projects` / `git_accounts` / `llm_credentials` / `tenant_secrets` / `audit_log`）— 用 nullable column + partial unique index 而非「另起平行表」（如 `product_line_secrets`），維持 secret RBAC 路徑只有一條（S-1.3 既有 audit / MFA step-up 不需重複實作）。
2. **`product_lines.is_system` partial unique 保證每 tenant 恰一個 `pl-default`** — `CREATE UNIQUE INDEX ... ON product_lines(tenant_id) WHERE is_system`；遺留 `t-default` migration 時建立 `pl-default(t-default, slug='default', is_system=true)`，所有既有 row 對應到此 line（S-9 範圍）。
3. **`projects.product_line_id NOT NULL`，但 Y4 兩階段落地** — 第一階段 nullable + backfill（既有 project → `pl-default`）+ 第二階段加 NOT NULL；同 Y1 既有 `tenant_id` 兩階段策略（TODO row 1674）。
4. **`tenant_secrets.product_line_id` 加欄位 = 跨 line secret 沿用同表** — 不再為 line-scoped secret 另建表；secret RBAC（S-1.3）+ audit 路徑（既有 `tenant.secret_*` 事件）+ MFA step-up（K MFA 系列）一律繼承；只在 `_check_secret_rbac()` dependency 加 product_line scope 比對。
5. **`audit_log.product_line_id` partial index 加速 line-scoped 查詢** — `CREATE INDEX ... WHERE product_line_id IS NOT NULL`；Pam 在 IPCam line dashboard 看 audit 時 backend 走此 index、不 scan 全 tenant audit row。
6. **既有 `users.tenant_id` 與 `product_line` 完全 orthogonal** — `users.tenant_id` 是 S-1 / S-2 設計的「主 tenant 快取」、與 product_line 無關（user 在某 line 的角色查 `product_line_members`）；防 Y4 reviewer 誤把 product_line 寫成 user 屬性。

### S-4.7 Operator 工作流 — Acme 從 1 線變 3 線的 7 步演進

從 acme 只有 IPCam 一條線（既有狀況）演進到 IPCam + Doorbell + Intercom 三線並行的時間軸：

1. **Day 0 — Acme 既有狀況（S-1.7 落地後）**  
   `t-acme` 內所有 project 都隸屬於唯一 line `pl-default`（is_system=true，Y4 migration 自動建立）；單一 LLM 預算 100M / 30d 全給 default line（無 override）；單一 GitHub default git account；單一 PagerDuty key（system-wide）。

2. **Day 1 — Alice 開新 product line `pl-ipcam`、把既有 firmware project 移過去**  
   Alice 走 `POST /api/v1/tenants/t-acme/product-lines { slug: "ipcam", display_name: "IPCam", llm_budget_tokens: 50000000 }` + `PATCH /api/v1/tenants/t-acme/projects/firmware-ipcam { product_line_id: "<pl-ipcam-id>" }`。  
   backend 寫 `product_lines` row + 寫 audit `tenant.product_line_created` + 寫 audit `project.moved_to_product_line`；既有 LLM atomic counter 從 `pl-default` 切過 50M 額度到 `pl-ipcam`。

3. **Day 1+15min — Pam 升任 IPCam line owner**  
   Alice 走 `POST /api/v1/tenants/t-acme/product-lines/pl-ipcam/members { user_id: pam, role: "owner" }`。Pam 在 sidebar 看到 `IPCam` line entry、點進去看到 firmware-ipcam project；audit 寫 `product_line.member_added`。

4. **Day 3 — Pam 設 IPCam line 的 git default**  
   Pam 走 `POST /api/v1/tenants/t-acme/git-accounts { product_line_id: pl-ipcam, platform: github, label: "acme-ipcam-bot", encrypted_token: ..., url_patterns: ["acme/ipcam-*"], is_default: true }`。  
   Resolver 之後對 IPCam line 的 push 自動用 `acme-ipcam-bot`（既有 tenant-wide default 仍用於非 IPCam workflow）。

5. **Day 5 — Pam 設 IPCam on-call routing**  
   Pam 先存 PagerDuty integration key 為 secret：`POST /api/v1/tenants/t-acme/secrets { product_line_id: pl-ipcam, secret_type: "pagerduty_key", key_name: "main", encrypted_value: ... }` (走 owner-only step-up MFA 沿 S-1.3 設計斷言 6)。  
   再走 `POST /api/v1/tenants/t-acme/product-lines/pl-ipcam/oncall-routing { provider: "pagerduty", secret_id: "<sec-id>", schedule_id: "PD-IPCam-NoC", escalation_minutes: 30 }`。  
   backend 立即 send 一條 test alert 到 IPCam on-call（S-4.4 設計斷言 6 silent-breakage 預防）；Pam 確認後該 routing 進入 active。

6. **Day 14 — Doris 開 Doorbell line**  
   Alice 走同樣流程開 `pl-doorbell` (35M budget)、加 Doris 為 owner、Doris 設 GitHub Cloud `acme-doorbell` git default、設 Doorbell PagerDuty schedule_id。  
   `tenant_quota` ceiling check 觸發：50M (ipcam) + 35M (doorbell) + 5M (default) = 90M ≤ 100M ceiling、通過。

7. **Day 30 — Ian 開 Intercom line + 突發預算超用**  
   Alice 走流程開 `pl-intercom` (10M budget)。三線總和 = 50 + 35 + 10 + 5 = 100M、剛好觸 ceiling、Y4 endpoint 預算驗算通過。  
   IPCam 線當週密集驗證新 ISP，Day 33 月中已用 47M tokens；Pam 想升 IPCam budget 到 60M、走 `PATCH /product_lines/pl-ipcam { llm_budget_tokens: 60000000 }`，但 backend 算總和 = 60+35+10+5=110M > 100M、return 409 + 「請先降 doorbell 或升 plan」。Pam 與 Doris 協調暫時 doorbell 降到 25M、ipcam 升 60M、總和 100M、通過。
   audit 雙寫 `product_line.budget_changed(actor=pam,old=50M,new=60M)` + `product_line.budget_changed(actor=doris,old=35M,new=25M)`。

**S-4.7 設計斷言**：
1. **加新 line 是 owner-only 動作** — 開 line 是公司治理層級的決策（影響 budget allocation + 法務責任邊界）、不該下放給 admin 級。Y4 endpoint 走 `require_role("owner")` dependency。
2. **遺留 project 自動進 `pl-default`、不 force migration**（呼應 S-9 範圍） — 既有 acme 在 Y4 migration 時所有 project 進 `pl-default`、Pam 想搬到 `pl-ipcam` 是顯式 PATCH 動作、不是被動發生；migration 不破壞既有 LLM counter / git account / on-call 路由（既有路徑 product_line_id IS NULL、走 fallback path 仍工作）。
3. **Per-line budget 改動立即生效**（呼應 S-4.2 設計斷言 5） — Pam 改 budget 從 50M 到 30M 不等下次 30d reset、立即進入新 cap；若已超用、立即進入 throttle、SSE 推 IPCam line owner notification。
4. **Routing 變更 send test alert** （呼應 S-4.4 設計斷言 6） — 改 on-call key / schedule_id 後 backend send 一條測試 PagerDuty incident 到新路徑；Pam 收到後 ack、舊路徑收到 cleanup ping。

### S-4.8 邊界 / 退化情境

| 邊界場景 | 預期行為 | 驗收條件 |
|---|---|---|
| Pam（IPCam line owner，非 tenant admin）想刪 `pl-ipcam` line | 403 — 刪 line 是 tenant owner 動作（可能影響其他 line 的 budget 重分配）；line owner 只能 archive 該 line（settings 隱藏 + 維持 budget cap=0、project 仍存在） | Y4 `DELETE /product-lines/{id}` require tenant role=owner |
| Alice 想刪 `pl-default` system line | 409 + 「Cannot delete system default line; archive other lines instead」 | Y1 schema CHECK + Y4 endpoint 雙重 reject |
| 三 line 預算總和恰好等於 tenant ceiling，pam 想再升 1M | 409 + 提示「合計超過 100M ceiling」 + UI 顯示「您可從 Doorbell / Intercom / Default line 各降 X / Y / Z M」 | Y4 PATCH endpoint 算 SUM + 比對 + reject 帶上下文 |
| Alice 把 tenant plan 從 enterprise 降到 pro（ceiling 從 100M 降到 30M），但既有 line 預算總和 = 100M | Y2 endpoint 在 plan 降級時驗算當前 Σ(line budget)、超過新 ceiling 時走「**強制按比例縮減**」（每 line 按既有比例壓縮、寫雙鏈 audit）+ banner 警告 owner 1 個 30d 週期內回審 | Y2 PATCH plan endpoint 帶 `auto_rebalance_lines` flag、預設 true（呼應 S-1.8 plan 過期降級） |
| IPCam line 的 PagerDuty key 失效（rotated 但 secret 未更新） | `_send_pagerduty()` retry 3 次失敗、fallthrough 到 tenant-level routing；同時寫 audit `oncall_routing.delivery_failed(scope=line, line_id=pl-ipcam, fallback=tenant)` + SSE 推 line owner Pam | Y9 notification fallback：line key 失敗 → tenant key → system default、層層 fallback；不直接 drop alert |
| Carol（IPCam line member，非 line owner）想看 Doorbell line 的 audit | 403 — 跨 line 看 audit 走 tenant-admin 級權限；line member 看到的 audit 自然按 product_line_id 過濾 | Y9 audit observable：require `product_line_role >= viewer` OR `tenant_role >= admin` per row |
| 想把 firmware-ipcam project 從 IPCam line 搬到 Doorbell line | 允許（線間 project 移動），但同 transaction 重新計算累積 LLM 用量歸屬（30d 滾動統計按搬遷時刻 cutoff、舊 line 計到 cutoff、新 line 從 cutoff 起算）；audit 寫 `project.moved_between_product_lines` | Y4 PATCH endpoint：require tenant admin OR (source line owner AND target line owner)；30d counter 雙寫 |
| 同 user 同時是 IPCam owner 與 Doorbell viewer | 完全允許（N:N relation） — Pam 在 IPCam sidebar 看到 owner 視角、切到 Doorbell sidebar 看 viewer 視角；UI 自動切換 capability | Y8 frontend：sidebar 內 product_line picker、依 active product_line 切 capability set |
| `pl-default` 的 budget 設成 0（不允許未分類 LLM call） | Y4 endpoint 允許設 0；但若 Σ(其他 line) < tenant ceiling、剩餘額度進入 `pl-default`（避免 ceiling lower 邊界 throttle）— Y6 atomic counter 對 `pl-default` 做 dynamic credit | Y6 token meter：default line counter 動態算 = ceiling - Σ(其他 line current_used) |

### S-4.9 Open Questions（標記給 Y1～Y10 後續勾選）

1. **「Product-line 拆 tenant 的退路」** — Acme 之後決定把 IPCam 完全獨立成子公司、要把 `pl-ipcam` 切出來成 `t-acme-ipcam` 新 tenant — schema migration 工具需要？目前傾向「PATCH project_line 不能跨 tenant、必走 export → 新 tenant import 流（M-export 系列範圍）」；但 audit 鏈拆分是個複雜問題。等 M-export 落地時定。
2. **「Cross-line LLM call attribution」邊界** — 跨 line 的工具（如 chatops bot 在 IPCam channel 觸發但執行邏輯涉及 doorbell 的 SOP）—  caller_product_line 算 IPCam 還是 doorbell？目前傾向「caller 是觸發 user 當下 active 的 line（IPCam）」、callee resource 不影響 attribution；但若 chatops 是 system actor、無 user context、走 `pl-default`。Y6 落地時實做需在 SOP / skill_pack call site 帶 product_line context。
3. **「Per-line on-call rotation 內部成員」是否該寫進 OmniSight schema** — 目前 S-4.4 只存 PagerDuty schedule_id；rotation 細節（誰 primary / 誰 secondary）由 PagerDuty 自管。但若想在 OmniSight 內 dashboard 顯示「IPCam 此刻 on-call: Pam」、需要從 PagerDuty API pull schedule。等 Y9 dashboard 落地時決定。
4. **「Line archive 的 cascade 行為」** — Alice archive `pl-doorbell`、line 內 project 怎麼辦？目前傾向「archive line ≠ archive project；line archive 後 budget 凍結、project 仍可看 / 不可改、新 workflow_run 拒絕」；想徹底清理就先把 project 搬到其他 line 再 archive。Y4 落地時定 archive cascade 範圍。
5. **「Product-line 是否該支援 nested hierarchy（line 內再分 sub-line）」** — IPCam 線之下「室內 IPCam」+「室外 IPCam」是否該獨立切？目前傾向**不支援 nested**（複雜度爆炸、用 metadata.tags 即可），但若 enterprise 客戶剛性需求、Y10 再考慮。S-4.6 的 schema 不預留 parent_id 欄位（YAGNI）。

### S-4.10 既有實作的對照表

S-4 設計與目前 codebase（截至 2026-04-25）的對齊狀況：

| S-4 invariant | 目前狀況 | 缺口 |
|---|---|---|
| `product_lines` 表 | ❌ — 完全不存在；frontend `app/workspace/[type]/types.ts:11` 有 `WORKSPACE_TYPES` 但屬 UX 變體不是 RBAC scope | Y1 新建（S-4.6 第 1 表） |
| `product_line_members` 表 | ❌ | Y1 新建（S-4.6 第 2 表） |
| `product_line_oncall_routing` 表 | ❌ | Y1 新建（S-4.6 第 3 表） |
| `projects.product_line_id` NOT NULL fk | ⚠️ Y1 既有草圖（TODO row 1669）已寫 `product_line` 為 string column（`UNIQUE (tenant_id, product_line, slug)`）、但是 string 而非 fk | Y1 修：把 string `product_line` 換成 `product_line_id uuid fk`、UNIQUE 改用 fk |
| `git_accounts.product_line_id` NULL fk | ❌ — `backend/alembic/versions/0027_git_accounts.py:88-110` 既有 unique index 是 `(tenant_id, platform)`、無 product_line 維度 | Y4 加欄位 + 加 partial unique `WHERE is_default AND product_line_id IS NOT NULL` + 改既有 partial unique 加 `WHERE product_line_id IS NULL` 約束 |
| `llm_credentials.product_line_id` NULL fk | ❌ — `backend/alembic/versions/0029_llm_credentials.py:97-123` 既有 unique 是 `(tenant_id, provider)`、無 product_line 維度 | Y4 加欄位 + 加 partial unique（同 git_accounts 模式） |
| `tenant_secrets.product_line_id` NULL fk | ❌ — `backend/alembic/versions/0013_tenant_secrets.py` 既有 UNIQUE `(tenant_id, secret_type, key_name)` | Y4 加欄位 + UNIQUE 拆兩條 partial（含 NULL / 不含 NULL） |
| `audit_log.product_line_id` NULL fk | ❌ — S-3 已加 `actor_external_tenant_id` + `share_id`、本 row 加第三個 nullable scope filter | Y4 加欄位 + partial index `WHERE product_line_id IS NOT NULL` |
| Per-line LLM atomic counter | ❌ — 既有 `backend/llm_secrets.py:106-216` 是全域 in-memory cache、`backend/tenant_quota.py` 是 per-tenant disk quota（與 LLM 無關）、`backend/adaptive_budget.py` 是 adaptive token budget 但不分 line | Y6 新建：`llm_token_meter.py` 雙層 atomic decrement（PG `SELECT FOR UPDATE` 鎖 (tenant_row, product_line_row) 兩 row）|
| Per-line PagerDuty routing | ❌ — `backend/notifications.py:1207` `_send_pagerduty()` 用 `settings.notification_pagerduty_key`（system-wide）；`backend/routers/integration.py:128` 既有 `notification_pagerduty_key` 是 SharedKV 全域欄位 | Y9 改 `_send_pagerduty()`：先查 `product_line_oncall_routing` → fallback tenant → fallback system；payload composer 加 `product_line` custom_details |
| Frontend product_line picker | ❌ — `lib/tenant-context.tsx` 只有 `currentTenantId`；`components/omnisight/tenant-switcher.tsx` 切 tenant，不切 line | Y8 新增 `lib/product-line-context.tsx`：`useProductLine()` + sidebar 內 line picker（subordinate 於 tenant switcher） |
| `git_resolve_account()` 含 product_line scope | ❌ — `backend/git_credentials.py` / `backend/routers/git_accounts.py` 走 `WHERE tenant_id=? AND platform=?` | Y6 改 resolver：`WHERE tenant_id=? AND product_line_id IN (caller_line, NULL) AND platform=? ORDER BY product_line_id NULLS LAST, is_default DESC` |
| LLM provider resolver 含 product_line scope | ❌ — `backend/llm_secrets.py:186` `get_provider_credentials()` 走全域 in-memory（不分 tenant、更不分 line） | Y6 改：query `llm_credentials WHERE tenant_id=? AND product_line_id IN (caller_line, NULL) AND provider=?` 取 line-first |
| SOP / skill_pack `product_line_id` 欄位 | ❌ — R 系列 SOP 表既有 schema 屬 R 系列範圍、Y0 不直接動 | Y4 / R 系列 落地時加（S-4.5 設計斷言 1）|
| Per-line dashboard usage breakdown | ❌ | Y8 新增：`/dashboard/product-lines` 頁、顯示三 line 的 LLM tokens / git account count / on-call status |
| Bootstrap wizard 創建 `pl-default` | ❌ | Y4 / Y10：每個新 tenant bootstrap 時自動建 `pl-default(tenant_id, slug='default', is_system=true)`；既有 t-default + 5 enterprise tenant 在 Y4 migration 時 backfill |

**S-4.10 對 Y1 / Y4 / Y6 / Y8 / Y9 的關鍵 deliverable**：
1. **Y1 新增 3 表 + 5 欄位** — `product_lines`(11 欄) + `product_line_members`(5 欄) + `product_line_oncall_routing`(7 欄) + `projects.product_line_id` 兩階段 NOT NULL + `git_accounts.product_line_id` nullable + `llm_credentials.product_line_id` nullable + `tenant_secrets.product_line_id` nullable + `audit_log.product_line_id` nullable；外加 partial unique index 4 條（pl-default per tenant、git default per line+platform、llm default per line+provider、tenant_secrets per line+type+name）。
2. **Y4 endpoint 集合** — `POST/PATCH/DELETE/archive /product-lines` + `POST/DELETE /product-lines/{id}/members` + `PATCH /product-lines/{id}/oncall-routing` + `PATCH /projects/{id} { product_line_id }` 跨 line 搬遷 + tenant plan 降級 auto_rebalance；budget 變更時的 `Σ(line budget) ≤ tenant.ceiling` invariant 檢查在 endpoint 強制。
3. **Y6 resolver 重寫** — `git_resolve_account()` / `get_provider_credentials()` / `secret_store.read()` 三條都要支援 `product_line_id IN (caller_line, NULL)` ordered fallback；新建 `llm_token_meter.check_budget()` 雙層 atomic decrement (PG SELECT FOR UPDATE)。
4. **Y8 frontend** — `lib/product-line-context.tsx` + sidebar product_line picker subordinate to tenant switcher + `/dashboard/product-lines` 三 line 用量 breakdown 頁 + 新建 line 時 bootstrap wizard step 寫 git default + on-call routing。
5. **Y9 notification 路徑改寫** — `_send_pagerduty()` / `_send_slack()` 階層 fallback (line → tenant → system)、payload 內加 product_line custom_details + fallback delivery 失敗時自動寫 audit + SSE 推 line owner。

---

## S-5 多專案同產品線

> 一個 product_line（Doorbell）內部同時養多個 project — `firmware-doorbell-v1-customer-a`（量產出貨給 Customer A 的智能門鈴 BSP，2 年合約）/ `firmware-doorbell-v2-customer-b`（替 Customer B 客製化 ISP tuning 的 POC 階段，3 個月合約）/ `firmware-doorbell-v3-internal-rnd`（內部探索新一代 SoC 的研發、無外部客戶）。三 project 在**業務上完全分開**（外部客戶 / 計費 / 合規邊界都獨立）—  但**共用同一條產品線的工程資源**（同一份 Doorbell 標準 SOP / 同一 skill pack 庫 / 同一個 git org / 同一個 PagerDuty schedule）。

> **與 S-4 的差異邊界**：S-4 是「**tenant 內部資源垂直切分**」的第一層（tenants → product_lines），S-5 是同一階層**再下一層**（product_lines → projects）；兩層的本質差異 ≠ 都是一樣的階層化。S-4 的 product_line 是「**運營邊界**」（誰負責這條線、用哪個 git org、on-call 是誰）— 線間設計上「彼此獨立、互不影響」是 raison d'être；S-5 的 project 是「**計費 / 客戶 / 生命週期邊界**」（這個 project 的 token 用量算誰錢、客戶交付節點、archive 不影響其他 project）— project 間刻意「**共用 SOP / skill_pack / git org / on-call**」是 raison d'être（同一條產品線不能各 project 各自為政、Doorbell 標準燒錄流程必須三 project 一致）。本章節要把這個「**對稱表面下的非對稱本質**」釐清，避免 reviewer 機械式套用 S-4 的「per-line 隔離」模板到 project 層、過度切割反而讓「同產品線多 project 共享資源」這個目的退化。

> **S-5 引入的三類新 invariant**（S-1 / S-2 / S-3 / S-4 都沒有）：
> 1. **Customer attribution** — project 必綁定 `customer_account_id`（外部客戶交付）或 `is_internal=true`（無外部客戶 / R&D）— 這是計費 export / 合約對帳的 first-class field，**不是 metadata.tags** 自由欄位。
> 2. **Lifecycle stage 顯式狀態機** — `lifecycle_stage ∈ ('rnd', 'poc', 'production', 'graduated', 'archived')` 的 typed enum + 狀態轉移規則（不允許 `rnd → production` 直接跳階、必須走 `rnd → poc → production`）— Y4 endpoint 在轉換時驗算前置條件。
> 3. **三層 LLM 預算階層 + caller-pays skill_pack** — 在 S-4.2 雙層（tenant ceiling / product_line budget）之上再加 project budget 第三層；skill_pack 跨 project 共用時 token 用量計入 caller project（不是 skill_pack owner project）— 與 S-3.4 跨 tenant「caller pays」對稱。

### S-5.1 角色 Persona — Doorbell 三專案

接續 S-4 的 Acme Cameras / `t-acme` / `pl-doorbell`（Doris 為 Doorbell line owner）。Doorbell 線此時已運營半年、累積三個 project：

- `firmware-doorbell-v1-customer-a` — 已量產出貨給 **Customer A**（連鎖物流商，部署 5,000 台）2 年合約、月度交付 firmware patch；lifecycle_stage = `production`；Doorbell 線 LLM 預算 35M / 30d 中、本 project 拿 20M（commercial workload heavy）。
- `firmware-doorbell-v2-customer-b` — 替 **Customer B**（Tier-1 安防經銷）做客製化 ISP tuning POC、3 個月合約（剩 2 個月）、若通過驗證合約轉量產；lifecycle_stage = `poc`；本 project 拿 10M / 30d。
- `firmware-doorbell-v3-internal-rnd` — 內部研發新一代 ISP 演算法（無外部客戶、為下一代 BSP 鋪路）；lifecycle_stage = `rnd`；本 project 拿 5M / 30d。

| Persona | 主 tenant | tenant role | product_line scope | project scope | 該 do | 該 not do |
|---|---|---|---|---|---|---|
| **Doris**（S-4 Doorbell line owner） | `t-acme` | member | `pl-doorbell` (line owner) | 全 3 project（line owner 預設賦權） | 開新 project、改 project 預算 cap（合計受 line budget 約束）、改 project lifecycle_stage（含 archive）、設 project-level git default override、跨 project 看用量 dashboard | 不能改 tenant ceiling；不能跨 line 動 project（要把 V1 搬到 IPCam line 必須兩 line owner 雙簽 + tenant admin 簽，呼應 S-4.8）|
| **Quinn**（V1 客戶 A 工程主管） | `t-acme` | member | `pl-doorbell` (line member) | `firmware-doorbell-v1-customer-a` (project owner) | 在 V1 內 push branch、跑 workflow_run、看 V1 的 LLM 用量 / token cost、改 V1 SOP override、看 V1 client A audit、出 V1 的月度計費 export | 不能進 V2 / V3；不能改 Doorbell line budget；不能改 V1 的 customer_account_id（屬於合約變更、tenant admin 動作）|
| **Rita**（V2 客戶 B POC 工程師） | `t-acme` | member | `pl-doorbell` (line member) | `firmware-doorbell-v2-customer-b` (project owner) | 在 V2 內所有對應動作、若 POC 通過走 `lifecycle_stage` poc → graduated 流程、出 customer B 試用報告 | 不能進 V1 / V3；不能直接把 V2 設成 `production`（必走 graduated 中介狀態 + tenant admin 簽）|
| **Sam**（V3 內部 R&D 工程師） | `t-acme` | member | `pl-doorbell` (line member) | `firmware-doorbell-v3-internal-rnd` (project owner) | 在 V3 內探索新演算法、跑大量 LLM call、不需出計費 export（無外部客戶）、可 clone V1 / V2 的 SOP 為 V3 內部變體（呼應 S-4.5 設計斷言 4 的 cross-line 也適用 cross-project）| 不能把 V3 升 `production`（lifecycle_stage 轉換需 `customer_account_id` non-NULL OR tenant owner override）|
| **Carol**（S-1 韌體工程師） | `t-acme` | member | `pl-doorbell` (line member) | V1 contributor（被 Quinn 加入幫忙 BSP review） | push branch、看 V1 的 SOP / skill_pack（從 line / tenant 繼承）、跑 V1 的 workflow_run、看 V1 用量 dashboard（read-only）| 不能改 V1 預算 / customer / lifecycle；不能進 V2 / V3；不能讀 V1 的 customer A NDA secret（owner-only step-up） |
| **Bob**（S-1 tenant admin） | `t-acme` | admin | 全 3 條 line（admin 預設賦權） | 全 9 project（admin 預設賦權所有 line × 所有 project） | 跨 project 看用量、改 customer_account_id 綁定（合約變更）、強制 archive 違規 project、轉移 V1 ownership 給其他 user（離職 offboarding，呼應 S-7） | 不能升 R&D 直接到 production（lifecycle 狀態機強制不論 role）|

**S-5.1 設計斷言**：
1. **Project owner 不是新 RBAC 階層、是 `project_members.role` 的一個值**（沿用 S-4.1 設計斷言 3 的設計哲學） — `project_members.role ∈ ('owner', 'contributor', 'viewer')`、與 `product_line_members.role` / `project_share_members.role` 同 vocabulary；想真正在 project 內動 budget / customer / lifecycle，必須是 `project_members(quinn, v1, role='owner')` + Doorbell line member（line member 自動含 project viewer 預設、想 push 必須 explicit `contributor`）。Y4 endpoint 在改 project-level 設定時 require `(tenant_role ≥ admin) OR (product_line_role ≥ owner) OR (project_role == 'owner')` 三選一。
2. **Line owner 預設訪問所有 line 內 project**（沿用 S-4.1 設計斷言 2 的階層繼承） — Doris 不需要在 `project_members` 各 project 寫 row、middleware fallback「line role ≥ owner → 任意該 line 內 project OK」；`project_members` row 的存在意義是「line member 級 user 被授權進入特定 project 的 contributor / owner」。
3. **`customer_account_id` 是 project 的 first-class 計費欄位、不是 `metadata.tags`** — 既有 SaaS 業界慣例：客戶歸屬一旦走 metadata 自由欄位（`tags: ["customer:acme"]`），就無法寫 type-safe export、無法強制 audit、無法在 schema 層做 unique constraint（同一 customer 名下多 project 的 cross-check）；S-5 強制 `projects.customer_account_id uuid fk customer_accounts(id) NULL` + `projects.is_internal boolean NOT NULL` + 兩者互斥（CHECK constraint）。
4. **Lifecycle stage 是強型別 enum、不是 status string**（與 S-3 的 share status 三段式狀態機同模型） — `lifecycle_stage ∈ ('rnd', 'poc', 'production', 'graduated', 'archived')` + 狀態轉移規則（見 S-5.5）；不接受 `'experimental'` / `'beta'` / `'deprecated'` 等自由命名 — 限制 5 值是為了 Y8 dashboard / Y4 endpoint / Y9 audit 都能基於同一 typed vocabulary 寫 type-safe code。
5. **「三 project 共用 Doorbell SOP / skill_pack」是 invariant、不是 default** — Quinn 不能在 V1 內 fork 一份「Doorbell 燒錄前 SOP」並私改 — 那是違反「同產品線必有一致燒錄流程」的工程治理原則。Quinn 想要 project-specific 的細節，必須是 SOP `inheritance` mode（V1 SOP override `parameters` jsonb + 繼承 line SOP body）— 不是 SOP `clone` mode（複製整個 SOP body）。Y4 落地時 `POST /projects/{id}/sops` 兩種 mode 並存、UI 預設 `inheritance` mode；clone mode 只在跨 line / 跨 tenant 時允許（S-4.5 設計斷言 4 已限制）。

### S-5.2 LLM 預算階層 — 三層擴充（Tenant Ceiling × Line Budget × Project Cap）

S-5 在 S-4.2 雙層（tenant ceiling × product_line budget）之上再加第三層 project cap：

**配額模型**（Acme enterprise plan = 100M tokens / 30d、Doorbell line = 35M / 30d 為例）：

```
tenant t-acme:                      ceiling = 100M tokens / 30d  (S-4.2)
└── pl-doorbell:                    budget  =  35M tokens / 30d  (S-4.2)
    ├── firmware-doorbell-v1-A:     cap     =  20M tokens / 30d  (S-5 新增 third-tier)
    ├── firmware-doorbell-v2-B:     cap     =  10M tokens / 30d  (S-5 新增)
    ├── firmware-doorbell-v3-rnd:   cap     =   5M tokens / 30d  (S-5 新增)
    └── line-default (unallocated): cap     =   0M tokens / 30d  (Σ 已 = line budget; 0 fallback)
                                              ─────────
                                               35M tokens   ← Σ(project cap) ≤ line budget
```

**配額檢查偽碼**（Y6 落地時實作，三層 atomic decrement，呼應 S-4.2 偽碼三層延伸）：

```python
# 偽碼，Y6 落地時實作
async def check_llm_budget(tenant_id, product_line_id, project_id, tokens_to_consume):
    # 1) project cap (若 project_id 非 NULL)
    if project_id is not None:
        proj_remaining = await fetch_atomic("llm_meter:proj:" + project_id, "tokens_30d")
        if proj_remaining < tokens_to_consume:
            raise LLMQuotaExceeded(scope="project", id=project_id)

    # 2) product_line budget (S-4.2 既有)
    pl_remaining = await fetch_atomic("llm_meter:pl:" + product_line_id, "tokens_30d")
    if pl_remaining < tokens_to_consume:
        raise LLMQuotaExceeded(scope="product_line", id=product_line_id)

    # 3) tenant ceiling (S-1.2 / S-4.2 既有)
    tenant_remaining = await fetch_atomic("llm_meter:t:" + tenant_id, "tokens_30d")
    if tenant_remaining < tokens_to_consume:
        raise LLMQuotaExceeded(scope="tenant", id=tenant_id)

    # 4) 三層 atomic decrement（同 transaction、要嘛同成功要嘛同失敗）
    keys = [
        ("llm_meter:t:" + tenant_id, tokens_to_consume),
        ("llm_meter:pl:" + product_line_id, tokens_to_consume),
    ]
    if project_id is not None:
        keys.append(("llm_meter:proj:" + project_id, tokens_to_consume))
    await atomic_decrement_n(keys)  # PG SELECT FOR UPDATE 鎖 N row 一次
```

**S-5.2 設計斷言**：
1. **Σ(project cap) ≤ line budget 是 backend invariant**（與 S-4.2 設計斷言 1 同模型、再下一層） — Doris 想把 V1 cap 從 20M 升到 25M、若 Σ(V1 25M + V2 10M + V3 5M) = 40M > 35M line budget 必須 reject + 提示「需先降 V2 / V3 或升 line budget」。Y4 `PATCH /projects/{id}` endpoint 在 backend 走 `SELECT SUM(cap) FROM projects WHERE product_line_id=?` + 比對 line budget、超則 409。
2. **Project cap 超用優先 throttle 該 project、不影響其他 project**（S-4.2 設計斷言 2 的 project 層延伸） — V1 燒到 20M 觸發 throttle、V2 / V3 仍可正常用各自 cap。理由：V1 客戶 A 的 token 大量消費可能源自合約交付期密集驗證、不該因此餓死 V2 / V3 — 這是 S-5「分開計費」的核心訴求。
3. **Skill_pack 跨 project 共用時 caller pays**（呼應 S-3.4 跨 tenant caller pays + S-4.5 設計斷言 3） — Doorbell line 的「ISP 自動 tuning skill_pack」被 V2 callsite 觸發時、token 用量計入 V2 project cap，**不是** skill_pack owning line 或 owning project；理由：caller pays 讓「誰用誰負責」清晰、避免「fork skill_pack 變成繞過自己 cap 的後門」。
4. **三層 atomic decrement 必同 transaction**（S-4.2 設計斷言 4 的三層延伸） — Race scenario：V1 與 V2 同時各觸發 5M tokens call、line budget 剩 8M。若 project counter 各 -5M 成功 但 line counter 變 -2M（超 line budget）= 違反 invariant。`atomic_decrement_n` 必走 PG `SELECT ... FOR UPDATE` 鎖 N row（tenant + line + project）一次、任一不足整批 rollback。Y6 落地時 SOP Step 1 必寫「合格答案 #2 — 透過 PG 序列化」釋因。
5. **無 `project_id` context 的 LLM call 走「line-default」桶**（呼應 S-4.2 設計斷言 6 的 `pl-default` 設計、向下擴散） — line-level 工具（如「Doorbell 線總用量 dashboard」呼叫 LLM 做 trend 摘要）、無明確 project 主體 — 這類 call 計入 line counter（不需設 project counter），不破壞 Σ(project cap) ≤ line budget invariant（line budget 預留差值即是 line-default 桶）。
6. **Project cap = NULL 表示「不獨立 cap、共用 line budget 剩餘額度」** — 小 project / 短期實驗不需要設 cap、`projects.llm_cap_tokens` NULL 時自動套用「line budget - Σ(已設 cap)」剩餘額度；Y6 atomic decrement 對 NULL cap project 只做兩層（line + tenant）、不做第三層；簡化新 project bootstrap UX。
7. **Lifecycle stage 影響 budget 預設值** — `production` project 預設 cap 較大（quinn 量產期需要穩定額度）、`poc` 預設較小（短期實驗）、`rnd` 預設最小（探索性）、`graduated` 維持 poc 期間值不重設、`archived` cap 自動歸 0；新 project bootstrap wizard 依 lifecycle 選 cap default。

### S-5.3 Customer Attribution — Project 計費客戶綁定

S-5 引入 OmniSight 第一個「**外部客戶**」概念。先區分三層：

```
tenant t-acme              ← OmniSight 內部「公司主體」(S-1)
└── pl-doorbell            ← Acme 內部產品線 (S-4)
    └── firmware-...-v1-A  ← project (S-5)
                                │
                                └── customer_account_id → cust-customer-a (Customer A 連鎖物流商)
                                                         (外部客戶帳號、與 S-3 跨 tenant 不同層)
```

**`customer_accounts` 表**（per-tenant 內部客戶清單，與 S-3 跨 tenant share 完全不同維度）：

```
customer_accounts            -- Y1 新表（S-5 計費歸屬權威來源）
  id                  uuid pk
  tenant_id           text fk tenants(id) NOT NULL
  display_name        text NOT NULL                       -- 'Customer A 連鎖物流商' / 'Customer B Tier-1 安防經銷'
  external_ref        text                                -- 客戶 ERP / 合約系統 ID（自由格式、acme 自填）
  billing_email       text                                -- 月度計費 export 寄送地址（不等於 OmniSight user）
  contact_email       text                                -- 業務 / 工程 PoC 通訊（NDA 範圍內）
  status              text NOT NULL DEFAULT 'active'      -- 'active' / 'paused' / 'churned'
  metadata            jsonb NOT NULL DEFAULT '{}'         -- 客戶等級、合約類別等（自由欄位）
  created_at          timestamptz NOT NULL
  archived_at         timestamptz
  UNIQUE (tenant_id, display_name)
```

**`projects.customer_account_id` 與 `is_internal` 的互斥約束**：

```
projects                    -- 既有 Y1 / S-4 草圖（再加 S-5 欄位）
  ...
  customer_account_id   uuid fk customer_accounts(id) NULL    -- S-5 加：外部客戶綁定
  is_internal           boolean NOT NULL DEFAULT false        -- S-5 加：true 表無外部客戶（R&D / 內部工具）
  CONSTRAINT customer_or_internal CHECK (
    (is_internal AND customer_account_id IS NULL) OR
    (NOT is_internal AND customer_account_id IS NOT NULL)
  )
  ...
```

**S-5.3 設計斷言**：
1. **`customer_accounts` 表是 OmniSight tenant 內部 view**（不是 OmniSight 平台層的 entity） — Customer A 不會自己登入 OmniSight；customer_accounts row 是 acme 內部對「我的客戶 A」的記錄、用於 (a) 計費 export 對帳 (b) NDA / 合約 metadata 集中存放 (c) cross-project 看「Customer A 名下所有 project」。Customer A 自己的 OmniSight tenant（如果存在）是另一條 reality（透過 S-3 cross-tenant share 連接）。
2. **`is_internal` 與 `customer_account_id` 互斥但不可雙 NULL** — V3 R&D project 必走 `is_internal=true`、不能 customer_account_id 也 NULL；理由：強迫每個 project 顯式宣告「對外計費 vs 內部自燒」、避免「忘記填客戶 → 計費 export 漏單」的 silent bug。CHECK constraint 在 schema 層強制；UI 在新建 project 時必選一個（two-choice radio button）。
3. **Customer attribution 變更走 audit + 雙簽**（呼應 S-3.6 雙簽精神） — Bob 想把 V1 從 Customer A 改綁 Customer C（合約轉手），這是 financial-impact 動作；Y4 endpoint require `tenant_role ≥ admin` + step-up MFA + audit 雙寫（`project.customer_changed(old=A, new=C)` 寫進 acme tenant chain + customer-level audit chain）。Doris（line owner）不能單獨改、避免操作風險。
4. **Customer churn 不級聯 archive project**（呼應 S-2.10 + S-1.8 的 graceful degradation） — Customer B POC 失敗、acme 把 customer_accounts(B).status 設 'churned'；V2 project **不自動 archive**（V2 內可能有寶貴 firmware artifact / IP）— 改成 banner 警告 + lifecycle_stage 強制不能升 production；Doris 顯式決定 archive / 重歸內部 R&D / 重綁其他 customer。
5. **Customer-level audit chain**（額外 chain、不取代 tenant chain） — V1 的所有 audit 自動寫**雙鏈**：(a) acme tenant chain（既有）+ (b) customer-A chain（per-customer 鏈，按 customer_account_id 切）— 讓 Customer A 出 audit export 給合規方時、acme 可只給「Customer A 名下所有 project 的事件」、不用先過濾 acme 全 tenant chain（呼應 S-3.3 雙鏈設計、再下一層）。Y9 落地時 `audit_log.customer_account_id` nullable + partial index。
6. **Customer 跨 line 的 project 列表是 first-class view** — Customer A 在 acme 內可能同時有 V1（Doorbell line）+ 另一個 IPCam line 內的 project（同一 customer 跨 line）；Y8 dashboard 必有 `/customers/{id}` 頁、橫跨 line 列出該 customer 所有 project；filter 不靠 metadata.tags、靠 schema-level fk。

### S-5.4 SOP / Skill Pack 三層繼承解析

S-4.5 已建立「SOP / skill_pack 兩層繼承（tenant → product_line）+ specific-first 解析方向」；S-5 把它擴成三層（tenant → product_line → project）：

```python
# 偽碼，Y4 / R 系列落地時實作（specific-first，與 git/on-call 方向相反）
def resolve_sop(project_id, sop_slug):
    # 1) project SOP override（最具體）
    sop = fetch_one(
        "SELECT * FROM sop_definitions "
        "WHERE project_id = ? AND slug = ? AND archived_at IS NULL",
        project_id, sop_slug,
    )
    if sop: return sop

    # 2) product_line SOP（S-4.5 既有層）
    project = fetch_project(project_id)
    sop = fetch_one(
        "SELECT * FROM sop_definitions "
        "WHERE product_line_id = ? AND project_id IS NULL "
        "AND slug = ? AND archived_at IS NULL",
        project.product_line_id, sop_slug,
    )
    if sop: return sop

    # 3) tenant-wide SOP（S-4.5 既有層）
    sop = fetch_one(
        "SELECT * FROM sop_definitions "
        "WHERE tenant_id = ? AND product_line_id IS NULL AND project_id IS NULL "
        "AND slug = ? AND archived_at IS NULL",
        project.tenant_id, sop_slug,
    )
    if sop: return sop

    # 4) system default（R 系列既有 ROM SOP 庫）
    return fetch_system_sop(sop_slug)
```

**Inheritance vs Clone 模式對照**（呼應 S-5.1 設計斷言 5）：

| 模式 | schema 表現 | 行為 | 適用場景 |
|---|---|---|---|
| **Inheritance**（V1 用 line SOP 但改部分參數） | `sop_overrides(project_id, parent_sop_id, parameters_jsonb)` 一張薄表、不複製 SOP body；`resolve_sop` 走 line SOP body + project override parameters merge | line SOP body 升版時 V1 自動受惠（升版者顯式評估後可選 propagate yes/no） | 量產 project 微調參數（如 V1 客戶 A 想把「燒錄超時」從 60s 改 120s 但其他步驟不動）|
| **Clone**（V3 想自定整個流程、與 line 解耦） | `sop_definitions(project_id, ...full body...)` 完整複製 row + `metadata.cloned_from_sop_id` 記錄祖先 | 父 SOP 升版不影響 clone；clone 之後 V3 內獨立演化 | R&D project 探索新流程、與標準完全脫鉤 |

**S-5.4 設計斷言**：
1. **SOP / skill_pack 表加 nullable `project_id` 欄位**（與 S-4.5 設計斷言 1 同模式、再下一層） — Y4 加 `sop_definitions.project_id NULL` + `skill_packs.project_id NULL`；NULL 表示 line-wide（S-4.5）或 tenant-wide（既有）；resolver 走 project → line → tenant → system。
2. **Resolver 走 specific-first**（沿用 S-4.5 設計斷言 2、再下一層） — project SOP 永遠 override line SOP；line SOP 永遠 override tenant SOP；理由：越具體的 scope 越精準（V1 量產期的「BSP 燒錄前流程」一定比 Doorbell 線標準更貼近 V1 客戶 A 場景）。
3. **Inheritance 是預設、Clone 是顯式選擇**（呼應 S-5.1 設計斷言 5） — Quinn 在 V1 內想要 SOP override：UI 預設「我要 inherit Doorbell SOP 並 override 部分參數」(inheritance mode、寫 sop_overrides row、保留升版 propagation)；Clone mode 必須顯式點按「我要完全 fork」按鈕、UI 警告「fork 後 line SOP 升版不會自動帶入」。預設 inheritance 是為了避免「Quinn 隨手 fork → 半年後 Doorbell 線 SOP 升版 V1 沒跟到 → 不一致」。
4. **Skill_pack 計費 caller pays**（呼應 S-3.4 + S-5.2 設計斷言 3） — Doorbell line 的「自動 ISP tuning skill_pack」被 V2 callsite 觸發、token 用量計 V2 project counter；skill_pack 自身不計帳（無「skill_pack owner project」概念）。Y6 落地時 `skill_pack_invoke()` 必帶 `caller_project_id` context、傳給 `check_llm_budget()`。
5. **跨 project clone SOP 是顯式動作 + 強制斷代**（呼應 S-4.5 設計斷言 4 cross-line 也適用 cross-project） — Sam 想把 V1 客戶 A 的「客製化燒錄 SOP」clone 到 V3 內部試驗；走 `POST /sops/{id}/clone {target_project: v3}`、新 SOP 在 V3 內生成獨立 row + `metadata.cloned_from_sop_id` 記祖先 + audit 寫 `sop.cloned_cross_project`；新 row 與原 row 完全斷代（V1 SOP 升版不影響 V3 clone）— 避免「Quinn 改 V1 客戶 A 的 SOP 結果 Sam 的 V3 內部試驗也跟著動」。
6. **Tenant-wide SOP 改動需 tenant admin / owner 簽**（沿用 S-4.5 設計斷言 5） — 呼叫範圍越廣、權限階層越高；line-wide SOP 改動 line owner（Doris）即可；project-scoped SOP 改動 project owner（Quinn）即可。

### S-5.5 Project Lifecycle 狀態機

S-5 引入 typed lifecycle stage、嚴格狀態機（不允許任意跳階）：

```
                        ┌─── archived ───┐  (任何階段都可進、不可逆)
                        │                │
                        ▼                │
       ┌──── rnd ──→ poc ──→ graduated ──→ production
       │              │                       │
       │              └────  rejected ────────┘ (poc 失敗 / graduated 失敗)
       └────────────  (rejected → archived 自動 90d) ──────────────
```

**狀態說明**：

| stage | 含義 | 進入條件 | 退出條件 | 預設 LLM cap |
|---|---|---|---|---|
| `rnd` | 內部研發、無外部客戶 | 新建時 `is_internal=true` 預設 | → `poc` 需綁 customer_account_id + tenant admin 簽 | 5M / 30d |
| `poc` | 外部客戶 POC、合約有期限 | 新建時帶 customer_account_id 預設 / 從 `rnd` 升 | → `graduated` 需 owner + admin 雙簽 + POC 通過驗證 / → `rejected` 失敗 | 10M / 30d |
| `graduated` | POC 通過、過渡到量產（合約轉長期） | 從 `poc` 升、必走中介狀態 | → `production` 需 tenant admin 簽 + 30d 觀察期 / → `archived` 客戶取消 | 15M / 30d (繼承 poc cap 不重設) |
| `production` | 量產交付、長期合約 | 從 `graduated` 升、不能跳階 | → `archived` 合約結束 | 20M / 30d |
| `rejected` | POC 失敗、無轉量產（保留 audit + artifact） | 從 `poc` / `graduated` 失敗 | 90d 後自動 archive cron | 1M / 30d (僅查歷史用) |
| `archived` | 不可逆終態（保留 audit + 可 export） | 任何 stage 進入 | 不可退出（除非 tenant admin un-archive、僅 90d 內可逆） | 0 |

**S-5.5 設計斷言**：
1. **狀態轉移走嚴格白名單、不允許任意跳階** — Y4 `PATCH /projects/{id} {lifecycle_stage}` endpoint 在 backend 維護 transition table、不在白名單的轉換 reject 422；理由：避免 R&D project 被誤升 production（合規 / 計費風險）、避免 production 直接退 rnd（影響合約）。
2. **`rnd → poc` 必綁 customer_account_id + 強制 tenant admin 簽** — R&D project 升為外部客戶 POC 是合約 + NDA 邊界跨入點、不能只由 project owner 決定；Y4 endpoint require `tenant_role ≥ admin` + step-up MFA + audit 寫 `project.lifecycle_promoted_to_poc(customer=...)`。
3. **`graduated` 是強制中介狀態、不可跳過**（與 S-3.6 三段式 share 狀態機同設計哲學） — POC 通過驗證後不直接升 `production`、必先進 `graduated`（30d 觀察期）；理由：production cap 較大（20M）+ 合約已轉長期 = 客戶開始扣月費；middle state 留時間驗證「POC 通過 ≠ 量產穩定」 + 留客戶法務簽合約緩衝。`graduated` 階段 cap 維持 poc 值（10M）不立即升 20M，避免「升階就燒爆預算」。
4. **`archived` 不可逆但 90d 緩衝期可 un-archive** — archive 是 graceful 終態（保留 audit + artifact + branch 留 `customer-x-fork` 可下載 export）；un-archive 在 90d 內由 tenant admin 單方解（合約延長 / 客戶回來談）；> 90d 後 token 重置 + workspace path 進 GC（呼應 S-7 範圍）。
5. **Lifecycle stage 變更必雙鏈 audit**（呼應 S-5.3 設計斷言 5） — 寫 acme tenant chain + customer-level chain 雙鏡像；customer 對帳時可看「我的 V1 何時從 poc 升到 production」、acme 內部 forensic 可看跨 line lifecycle 演進。
6. **`rejected` 是顯式失敗終態、不混用 `archived`** — POC 失敗（客戶不續約 / 驗證未通過）走 `rejected`、保留「為什麼不續」context（audit `lifecycle_rejected(reason=...)`）；archived 是 happy ending（量產合約結束）；分兩個 enum value 讓計費 export / 商業 dashboard 區分「失敗」vs「正常退場」。
7. **狀態轉移引發 LLM cap 自動調整 + 通知**（呼應 S-4.7 設計斷言 3 + S-5.2 設計斷言 7） — `poc → graduated` 時 cap 維持 10M 不動（避免突跳 20M）、`graduated → production` 時 cap 自動升 20M（line budget 容許下）、`*  → archived` 時 cap 歸 0；同 transaction 內 SSE 推 project owner notification。

### S-5.6 schema 衝擊（與 Y1 / Y4 對齊）

S-5 在 Y1 / Y4 / Y6 / Y9 落地時對 schema 的增量（在 S-1.6 + S-2.6 + S-3.5 + S-4.6 既有設計上加）：

```
customer_accounts            -- Y1 新表（S-5.3 計費歸屬權威）
  id                  uuid pk
  tenant_id           text fk tenants(id) NOT NULL
  display_name        text NOT NULL
  external_ref        text                                -- ERP / 合約系統 ID
  billing_email       text                                -- 月度 export 寄送
  contact_email       text                                -- PoC 通訊
  status              text NOT NULL DEFAULT 'active'      -- 'active' / 'paused' / 'churned'
  metadata            jsonb NOT NULL DEFAULT '{}'
  created_at          timestamptz NOT NULL
  archived_at         timestamptz
  UNIQUE (tenant_id, display_name)

projects                     -- 既有 Y1 / S-4 草圖（再加 S-5 欄位）
  ...
  customer_account_id   uuid fk customer_accounts(id) NULL    -- S-5 加
  is_internal           boolean NOT NULL DEFAULT false        -- S-5 加
  lifecycle_stage       text NOT NULL DEFAULT 'rnd'           -- S-5 加 enum: rnd/poc/graduated/production/rejected/archived
  llm_cap_tokens        bigint                                -- S-5 加：30d cap; NULL = 共用 line budget 剩餘
  CONSTRAINT customer_or_internal CHECK (
    (is_internal AND customer_account_id IS NULL) OR
    (NOT is_internal AND customer_account_id IS NOT NULL)
  )
  CONSTRAINT lifecycle_stage_valid CHECK (
    lifecycle_stage IN ('rnd','poc','graduated','production','rejected','archived')
  )
  ...
  -- partial index: archived project 排除常用查詢
  -- CREATE INDEX idx_projects_active_per_line ON projects(product_line_id) WHERE lifecycle_stage <> 'archived'

project_lifecycle_history    -- Y4 新表（S-5.5 狀態轉移 audit join 表）
  id                  uuid pk
  project_id          uuid fk projects(id) NOT NULL
  from_stage          text                                 -- NULL = 新建
  to_stage            text NOT NULL
  changed_by          uuid fk users(id) NOT NULL
  approved_by         uuid fk users(id)                   -- promote 雙簽（rnd→poc / graduated→production）
  reason              text                                 -- 'poc_passed' / 'churned' / 'spec_change' 等
  metadata            jsonb NOT NULL DEFAULT '{}'
  changed_at          timestamptz NOT NULL
  -- 給 customer 出歷史時走此表

sop_definitions              -- 既有 R 系列表（S-4.5 已加 product_line_id、S-5 再加 project_id）
  ...
  project_id          uuid fk projects(id) NULL            -- S-5 加：NULL = line/tenant scope
  ...
  -- partial unique 增量：
  -- UNIQUE (project_id, slug) WHERE project_id IS NOT NULL  AND archived_at IS NULL
  -- 既有 line / tenant partial unique 保持

sop_overrides                -- Y4 新表（S-5.4 inheritance mode 薄 override 表）
  project_id          uuid fk projects(id) NOT NULL
  parent_sop_id       uuid fk sop_definitions(id) NOT NULL  -- 指向 line / tenant SOP body
  parameters          jsonb NOT NULL DEFAULT '{}'           -- override 部分參數
  created_at          timestamptz NOT NULL
  created_by          uuid fk users(id) NOT NULL
  PRIMARY KEY (project_id, parent_sop_id)

skill_packs                  -- 既有 R 系列表（S-4.5 已加 product_line_id、S-5 再加 project_id）
  ...
  project_id          uuid fk projects(id) NULL            -- S-5 加
  ...

audit_log                    -- 既有表（S-3 已加 actor_external_tenant_id + share_id；S-4 加 product_line_id；S-5 再加）
  ...
  project_id          uuid NULL fk projects(id)            -- S-5 新增：project scope filter
  customer_account_id uuid NULL fk customer_accounts(id)   -- S-5 新增：customer-level chain join key
  ...
  -- partial index 加速 project / customer scoped 查詢
```

**S-5.6 設計斷言**：
1. **新表 3 張**（`customer_accounts` + `project_lifecycle_history` + `sop_overrides`） + **既有表加欄位 4 張**（`projects` + `sop_definitions` + `skill_packs` + `audit_log`）— 維持「擴充既有 schema、不另起平行表」的 Y 系列共識（S-3.5 / S-4.6 已建立模式）。理由：sop_overrides 是必要新表（schema 結構與 sop_definitions 完全不同 — 一個是 thin override、一個是 full body）、不適合塞進 sop_definitions 用 nullable 欄位。
2. **`projects.lifecycle_stage` 用 text + CHECK 而非 PG enum type** — 避免 enum migration 痛點（PG enum value add 是 ALTER TYPE 但 remove / rename 困難）；CHECK constraint 給同樣強型別保證 + migration 可走標準 ALTER TABLE；Y1 / Y4 一律用此模式（與 S-4.6 / S-3.5 既有 CHECK 設計一致）。
3. **`sop_overrides` 走「parameters jsonb merge」而非「整個 SOP body deep merge」** — 結構簡單、reviewer 可一眼看出 override 改了什麼；deep merge 容易出 bug（哪些 list 該 replace 哪些該 append）。Y4 落地時 SOP runtime 在 invoke 時做：line SOP body + project override parameters → final config（簡單 dict update、不遞迴）。
4. **`project_lifecycle_history` 與 `audit_log` 並存、不取代** — audit_log 是 cross-cutting / append-only / chain hash 的事件流；lifecycle_history 是 typed 領域表（with `approved_by` 雙簽欄位、可寫複合 query「列出 acme 過去 12 個月 poc → production 的 project」），兩者是 first-class 副本（呼應 S-3.5 share 與 audit 並存設計）。
5. **`audit_log.customer_account_id` partial index** — `WHERE customer_account_id IS NOT NULL`；Customer A 出 audit export 時 backend 走此 index 不 scan 全 tenant audit row（呼應 S-4.6 設計斷言 5）。
6. **`projects.llm_cap_tokens` 為 nullable bigint** — NULL 表「共用 line budget 剩餘額度」（S-5.2 設計斷言 6）；非 NULL 是顯式設定值；Y6 atomic decrement 對 NULL cap project 自動 skip 第三層。
7. **既有 `projects.product_line_id NOT NULL`** + **新加 `customer_account_id` nullable**（互斥約束）— S-4.6 設計斷言 3 已要求 `product_line_id NOT NULL`、不放鬆；S-5 加的 customer_account_id 是 project 額外屬性、與 product_line_id orthogonal（同 customer 的 project 可跨 line：例 customer A 在 Doorbell 與 IPCam 各有 project）。

### S-5.7 Operator 工作流 — Doorbell 從 1 project 變 3 project 的 7 步演進

從 Doorbell 線只有 V1 一 project（S-4.7 落地後）演進到 V1 / V2 / V3 三 project 並行：

1. **Day 0 — Doorbell 線既有狀況（S-4.7 落地後）**  
   `pl-doorbell` 內只有 `firmware-doorbell` 一個 project（從 S-4.7 Day 14 移過來）；無 customer_account 概念（既有 project `is_internal=true` + `lifecycle_stage='production'`）；line budget 35M 全給此 project。

2. **Day 1 — Bob 建立 Customer A 帳號 + 把既有 firmware-doorbell project 綁定 Customer A**  
   Bob 走 `POST /api/v1/tenants/t-acme/customer-accounts { display_name: "Customer A 連鎖物流商", external_ref: "acme-erp-7841", billing_email: "billing-cust-a@cust.com" }`。  
   再走 `PATCH /api/v1/projects/firmware-doorbell { customer_account_id: "<cust-a-id>", is_internal: false }`（**強制 tenant admin step-up MFA**，S-5.3 設計斷言 3）；audit 雙寫 acme tenant chain + customer-A chain。  
   project 從 internal R&D（`rnd` 時期遺留）走特例升 `production` 路徑（`is_internal=false` 後第一次 promote、tenant admin override 跳階規則 + audit 寫 `lifecycle_promoted_with_admin_override`）。

3. **Day 1+10min — Doris rename project 為 V1**  
   為了與後續 V2 / V3 區分、Doris 走 `PATCH /projects/firmware-doorbell { slug: "firmware-doorbell-v1-customer-a" }`；寫 audit `project.slug_renamed`；URL 自動 redirect（30d 過渡期保留舊 slug → 新 slug 的 308）。

4. **Day 7 — Doris 開 V2（Customer B POC）**  
   先建 customer B：`POST /customer-accounts { display_name: "Customer B Tier-1 安防經銷", ... }`。  
   再開 project：`POST /api/v1/tenants/t-acme/projects { product_line_id: pl-doorbell, slug: "firmware-doorbell-v2-customer-b", customer_account_id: <cust-b-id>, is_internal: false, lifecycle_stage: "poc", llm_cap_tokens: 10000000 }`。  
   backend 算 Σ(project cap)：V1 20M + V2 10M = 30M ≤ line budget 35M、通過；audit 寫 `project.created` + `project.lifecycle_promoted_to_poc`。  
   Doris 加 Rita 為 V2 owner：`POST /projects/firmware-doorbell-v2-customer-b/members { user_id: rita, role: owner }`。

5. **Day 30 — Doris 開 V3（內部 R&D）**  
   `POST /projects { product_line_id: pl-doorbell, slug: "firmware-doorbell-v3-internal-rnd", is_internal: true, customer_account_id: NULL, lifecycle_stage: "rnd", llm_cap_tokens: 5000000 }`。  
   Σ(project cap)：V1 20M + V2 10M + V3 5M = 35M = line budget、剛好 fit；通過。  
   加 Sam 為 V3 owner；audit 雙鏈寫 acme tenant chain（無 customer chain — `is_internal=true` 不寫 customer chain，呼應 S-5.3 設計斷言 5 partial index）。

6. **Day 60 — V2 POC 通過、Rita 走 graduated 流程**  
   Rita 申請 promote：`POST /projects/firmware-doorbell-v2-customer-b/lifecycle-transition { to_stage: "graduated", reason: "poc_passed" }`。  
   backend 檢查白名單：`poc → graduated` 允許、且需 project owner（Rita）+ tenant admin（Bob）雙簽 + step-up MFA。  
   Bob 在 admin notification 點 Approve（呼應 S-3.6 設計斷言 4 雙簽 MFA）；audit 寫 `project_lifecycle_history(from='poc', to='graduated', changed_by=rita, approved_by=bob, reason='poc_passed')` + 雙鏈 audit。  
   cap 維持 10M 不動（呼應 S-5.5 設計斷言 3 + S-5.2 設計斷言 7）；30d 觀察窗開始。

7. **Day 90 — V2 觀察窗過、Bob 升 production + 自動調 cap**  
   Bob 走 `POST /projects/firmware-doorbell-v2-customer-b/lifecycle-transition { to_stage: "production" }`。  
   backend 檢查 30d 觀察窗（看 `project_lifecycle_history` 最新 graduated row 的 changed_at）+ tenant admin role；通過。  
   cap 自動升 10M → 20M（前提：line budget 容許 — 此時 V1 20M + V2 20M + V3 5M = 45M > 35M line budget 必須先擴 line budget）。  
   workflow：Doris 先升 line budget 從 35M 升到 45M（需 tenant ceiling 容許 — 100M 仍 OK，呼應 S-4.7 設計斷言 4）；audit 寫 `product_line.budget_changed` + `project.lifecycle_promoted_to_production` + `project.cap_auto_adjusted`（三條同 transaction）。

**S-5.7 設計斷言**：
1. **既有 project 升外部客戶綁定走特例 admin override**（呼應 S-5.5 狀態機嚴格） — 既有 `firmware-doorbell` 是 production 但 `is_internal=true`、要綁 customer A 等於從「無客戶 production」進入「有客戶 production」、跨越 customer attribution 邊界；走 tenant admin override 走「特例升階」+ 加倍 audit 加 `with_admin_override` flag 是 forensic 對帳時必要。
2. **3 project 同時 fit line budget = 緊邊界**（呼應 S-5.2 設計斷言 1） — Day 30 Σ(cap) = 35M 剛好 = line budget；後續任何升 cap 必觸發「先升 line budget OR 先降其他 project」決策；UI 在 PATCH 時帶上下文提示。
3. **`graduated` 30d 觀察窗 + 自動 cap 調整**（呼應 S-5.5 設計斷言 7） — `graduated → production` 在 backend 自動算 30d 觀察窗（不夠 reject 422 + 提示「再等 X 天」）；cap 升級走「先 PATCH line budget → 再 PATCH project cap」兩段、避免 atomic 違反 invariant。
4. **Rename slug 走 308 redirect 保護**（呼應 S-2 / S-3 已建立的 graceful migration） — 改 slug 是 URL-visible 變更、舊 URL 30d 過渡期 308 redirect、避免外部書籤 / chatops 連結爆。

### S-5.8 邊界 / 退化情境

| 邊界場景 | 預期行為 | 驗收條件 |
|---|---|---|
| Quinn（V1 owner、非 tenant admin）想改 V1 的 customer_account_id 到其他 customer | 403 — 改 customer 是合約 / 計費邊界動作（S-5.3 設計斷言 3）、tenant admin only | Y4 PATCH `/projects/{id}` 對 customer_account_id 欄位 require tenant admin + step-up MFA |
| Doris 把 V1 的 cap 從 20M 升到 30M、但 Σ(project cap) > line budget | 409 + 提示「合計 35M+ 超過 35M line budget；請先降 V2 / V3 cap、或先升 line budget（需 tenant ceiling 容許）」+ 顯示可降空間 | Y4 PATCH endpoint 算 SUM(cap) + 比對 line budget；backend invariant，UI 預先提示 |
| Sam 想把 V3（rnd / is_internal=true）直接升 production 跳過 poc 中介 | 422 — 狀態機白名單只允許 `rnd → poc` 不允許 `rnd → production`；UI 引導「先綁 customer + tenant admin 簽 + 升 poc」 | Y4 lifecycle-transition endpoint 嚴格白名單檢查 |
| Customer B churn 把 customer_accounts(B).status 設 'churned' | V2 不自動 archive；改成 banner 警告 + 阻擋 lifecycle 升階；Doris / Bob 顯式決定 archive / 重綁 / 重歸 internal R&D（必走 PATCH customer_account_id 流程）| Y4 customer status 變更觸發 SSE 推 project owner + 在 dashboard 顯紅色警告 banner |
| Quinn fork SOP（用 Clone mode）後 Doorbell line SOP 升版 | V1 clone SOP 不受影響（顯式 fork 已斷代、S-5.4 設計斷言 5）；UI 在 `/projects/{id}/sops` 頁顯示「此 SOP 為 clone、line 升版不會自動帶入；若想同步請手動 reapply」 | Y4 SOP detail page banner + audit `sop.cloned_cross_project` 留下祖先指標 |
| V2 graduated 觀察窗未滿 30d、Bob 想升 production | 422 + 提示「再等 X 天」；強制等滿（避免「升階就燒爆預算」+ 客戶法務簽合約緩衝）| Y4 lifecycle-transition endpoint 算 from `project_lifecycle_history` 最新 graduated row 的 changed_at |
| Sam 想跑 V3 內部試驗 LLM call、但 V3 cap 已用完 | Throttle 該 project 但不影響 V1 / V2（呼應 S-5.2 設計斷言 2）；SSE 推 V3 owner Sam；audit 寫 `llm_quota_exceeded(scope=project)` | Y6 token meter 三層 atomic decrement 第一層 fail → 立即拒絕該 LLM call |
| 同一 user 同時是 V1 owner + V3 viewer + V2 contributor | 完全允許（N:N relation、S-4.8 同模型）；UI 在 project picker 顯示三 project + 各自 capability badge | Y8 frontend：sidebar 內 project picker subordinate 於 line picker、依 active project 切 capability set |
| V1 量產 Customer A 期間需要與 Customer C 共用一份 firmware artifact | 走 S-3 cross-tenant share（不用 cross-project 機制 — 兩個 customer 是不同 cobalt-tenant 級、不是 acme tenant 內部 project 切分）；S-5 不涵蓋 cross-project artifact share | S-3 既有路徑、S-5 不延伸 |
| V1 archive 後 5d，Customer A 回來談合約延長 | tenant admin 走 `POST /projects/{id}/un-archive`；90d 內可逆（S-5.5 設計斷言 4）；workspace path 還在（GC 在 90d 後）；recover lifecycle 為 archive 前狀態（production）| Y4 endpoint require tenant admin + step-up MFA + audit 寫 `project.unarchived(reason=...)` |
| V2 archive 之後 91d cron 跑、自動清 workspace | Cron 跑 `archive_after_90d_cleanup()`：從 disk GC `.agent_workspaces/` 對應 workspace + 寫 audit `project.workspace_gc_completed`；DB row 仍保留（forensic + 計費 export 需要）| Y4 cron job + Y6 workspace path GC（呼應 S-7） |

### S-5.9 Open Questions（標記給 Y1～Y10 後續勾選）

1. **「Customer 跨 tenant 結合 — Customer A 自己是 OmniSight tenant」如何模擬** — Customer A 若也是 OmniSight tenant、acme 的 V1 project 想直接給 Customer A 看 — 走 S-3 cross-tenant share 還是另起 `customer_tenant_link` 表？目前傾向「兩條獨立路徑共存」（內部 customer_accounts 是 acme 私有對帳簿；S-3 share 是雙向 grant），但兩者 status 同步是個複雜問題。等 M-export / 合規系列落地時定。
2. **「V1 / V2 / V3 共用同一份 Doorbell git repo 還是各自 fork」** — 既有 S-4.3 git resolver 走 line-default、所有 Doorbell project push 到同一個 `acme-doorbell` git org；但 V1 量產 / V2 POC / V3 R&D 可能想用不同 branch 策略（V1 主線 + V2 customer-b-fork branch + V3 internal/* branch）— S-6（多分支同專案）會處理 branch 切分、S-5 暫不延伸。
3. **「Project lifecycle 自動觸發 cap 縮減 vs 維持」** — V2 從 graduated 升 production cap 自動升（S-5.5 設計斷言 7）；但 production → archived 是否該立刻 cap 歸 0？目前傾向「立刻 0」但要 SSE 通知 + 30d 內可 un-archive 自動恢復。等 Y4 / Y6 落地時測試 production-to-archive 邊界。
4. **「Skill_pack 跨 project caller pays 的細粒度」** — line-level skill_pack 被 V2 觸發、token 計 V2；但若 skill_pack 內部分步驟用 LLM 各自做不同事（步驟 A 算 BSP code、步驟 B 算 customer-specific tuning）— 兩步是否該分開計？目前傾向「skill_pack invoke 整體計到 caller」、子步驟不細分（避免太細粒度的 attribution 引發 dashboard 噪音）。等 R 系列 skill_pack runtime 重寫時定。
5. **「Customer churn 後保留期 vs 立即 archive」** — Customer B churn 後 V2 banner 警告但不自動 archive — 但若 acme 90d 都沒動 V2 是否該 cron 自動 archive？目前傾向「不自動 archive、強迫人類決策」（archive 影響 customer audit chain finalization、不該在無人 review 下發生）；但 dashboard 應有「churned 客戶遺留 project」名單 nag UI。等 Y8 dashboard 落地時實。

### S-5.10 既有實作的對照表

S-5 設計與目前 codebase（截至 2026-04-25）的對齊狀況：

| S-5 invariant | 目前狀況 | 缺口 |
|---|---|---|
| `projects` 表 | ❌ — 完全不存在；只有 `backend/alembic/versions/0006_project_runs.py:21-27` 既有 `project_runs` 表（groups workflow_runs）以 `project_id` text 字串作為弱 FK；`backend/project_runs.py:25-30` 的 `ProjectRun` dataclass 把 `project_id: str` 視作 logical label | Y1 新建（S-1.6 / S-4.6 已規格化、S-5.6 再加 customer_account_id / is_internal / lifecycle_stage / llm_cap_tokens 4 欄）|
| `customer_accounts` 表 | ❌ — 不存在 | Y1 新建（S-5.6 第 1 表）|
| `project_lifecycle_history` 表 | ❌ | Y4 新建（S-5.6 第 2 表）|
| `sop_overrides` 表 | ❌ | Y4 新建（S-5.6 第 3 表，inheritance mode 薄表）|
| `projects.lifecycle_stage` typed enum | ❌ — 既有 `project_runs` 並無 lifecycle 概念 | Y1 加 column + CHECK constraint 5 值 |
| `projects.customer_account_id` + `is_internal` 互斥 | ❌ | Y1 加 column + CHECK constraint |
| `projects.llm_cap_tokens` per-project cap | ❌ — `backend/alembic/versions/0024_token_usage_cache.py:22-32` 既有 `token_usage` 表 only indexed on `model`、無 project 維度；`backend/tenant_quota.py` 是 disk quota；`backend/llm_secrets.py:106-216` 是全域 in-memory cache | Y1 加 column；Y6 `llm_token_meter.py` 雙層 atomic decrement（S-4.10 row 1120）擴成三層（tenant + line + project）|
| Three-tier LLM atomic decrement | ❌ — 既有 atomic decrement 不存在（將由 Y6 實作雙層、S-5 擴三層）| Y6：`atomic_decrement_n` PG `SELECT FOR UPDATE` 鎖 N row（tenant + line + project）一次 |
| SOP / skill_pack `project_id` 欄位 + project-level resolver | ❌ — `backend/skill_registry.py` + `backend/skill_manifest.py` 只走 flat manifest walker、無階層 resolver | Y4 / R 系列：sop_definitions / skill_packs 加 nullable `project_id` + resolver 從 S-4.5 兩層擴成三層（project → line → tenant → system，呼應 S-5.4 偽碼）|
| SOP inheritance vs clone 兩 mode | ❌ — 完全不存在 | Y4：sop_overrides 表 + `POST /projects/{id}/sops` 兩 mode endpoint + UI 預設 inheritance |
| Per-project audit filter | ❌ — `backend/alembic/versions/0003_audit_log.py:23-34` 既有 audit_log 無 `project_id` 欄位（也無 product_line_id、share_id）| Y4 加 `project_id` + `customer_account_id` 兩欄（S-3 已加 actor_external_tenant_id + share_id、S-4 已加 product_line_id、S-5 再加兩欄）|
| Customer-level audit chain | ❌ | Y9 加 partial index `WHERE customer_account_id IS NOT NULL` + customer audit export endpoint |
| Lifecycle 狀態機 transition table | ❌ | Y4：`POST /projects/{id}/lifecycle-transition` endpoint + transition 白名單 + double-sign for `rnd→poc` 與 `graduated→production` |
| Frontend project picker | ❌ — `lib/tenant-context.tsx` 只有 `useTenant()`；無 `useProject()` / `ProjectContext`；`components/omnisight/tenant-switcher.tsx` 切 tenant、不切 project；`app/workspace/[type]/types.ts:11-18` `WORKSPACE_TYPES` 是 UX 變體、不是 project | Y8 新增 `lib/project-context.tsx`：`useProject()` + sidebar project picker（subordinate to line picker subordinate to tenant switcher，呼應 S-4.10 row 1122）|
| Per-customer dashboard | ❌ | Y8 新增 `/customers/{id}` 頁、跨 line 列出 customer 名下所有 project（呼應 S-5.3 設計斷言 6） |
| Customer churn banner / lifecycle nag | ❌ | Y8 dashboard：customer status='churned' 觸發紅色 banner；「churned 客戶遺留 project」名單 nag UI（呼應 S-5.9 Q5）|
| Workspace path layout | `backend/workspace.py:29, 98` 既有 `.agent_workspaces/{safe_agent_id}/agent/{agent}/{task}` — agent-scoped、無 tenant / line / project 巢狀 | S-5 不直接動（屬 S-6 / Y6 範圍）；S-5 假設 V1 / V2 / V3 在 workspace path 上的隔離由 S-6 解決 |
| `git_resolve_account()` 含 project scope | ❌ — 既有 backend/git_credentials.py 走 (tenant_id, platform)、S-4 加 product_line scope、S-5 不要求再加 project scope（git resolver 對 project 透明、project 共用 line default git account）| Y6 不擴；S-5 default 是「project 共用 line git account」（與 SOP 三層繼承反方向、與 git resolver 兩層一致 — git account 是運營資源、不該每 project 各設）|

**S-5.10 對 Y1 / Y4 / Y6 / Y8 / Y9 的關鍵 deliverable**：
1. **Y1 新增 1 表 + 4 欄位** — `customer_accounts`(10 欄) + `projects.customer_account_id` nullable + `projects.is_internal` not null default false + `projects.lifecycle_stage` not null default 'rnd' + `projects.llm_cap_tokens` nullable bigint；外加 2 條 CHECK（互斥 / lifecycle enum）+ 1 條 partial index（active per line）。
2. **Y4 新增 2 表 + endpoint 集合** — `project_lifecycle_history`(8 欄) + `sop_overrides`(5 欄) + `POST /customer-accounts` + `PATCH /projects/{id} { customer_account_id }` 走 admin step-up + `POST /projects/{id}/lifecycle-transition` 嚴格白名單 + `POST /sops/{id}/clone` cross-project + `POST /projects/{id}/un-archive` 90d 內可逆 + `GET /customers/{id}` 跨 line project 列表 endpoint。
3. **Y6 token meter 擴三層** — `llm_token_meter.check_budget()` 從 S-4.2 雙層 (tenant + line) atomic decrement 擴成三層（tenant + line + project，project_id 為 NULL 時 skip 第三層）；`atomic_decrement_n` PG `SELECT FOR UPDATE` 鎖 N row 一次。
4. **Y4 / R 系列 SOP / skill_pack resolver 擴三層** — `resolve_sop()` 從 S-4.5 兩層（line → tenant → system）擴成三層（project → line → tenant → system）；inheritance mode（sop_overrides 薄表）vs clone mode（sop_definitions 完整 row）兩種共存；UI 預設 inheritance。
5. **Y8 frontend** — `lib/project-context.tsx` + sidebar project picker subordinate to line picker + `/customers/{id}` 跨 line 列表頁 + churn banner + lifecycle stage badge + project-level cap usage breakdown 頁 + `/projects/{id}/sops` SOP 模式選擇 UI。
6. **Y9 audit 路徑改寫** — `audit_log.project_id` + `audit_log.customer_account_id` 雙 partial index；customer-level audit export endpoint（出 customer A 名下所有 project 的 audit）；project lifecycle 變更必雙鏈寫入 acme tenant chain + customer chain（呼應 S-3.3 + S-5.5 設計斷言 5）。

## S-6 多分支同專案

> 一個專案下 `main / staging / v2.1-hotfix / customer-x-fork` 四 branch 並行開發，workspace 要能同時保有；branch 是 first-class scope（介於 project 與 task 之間）、有獨立 lifecycle / 獨立 workspace path / 獨立並行 lock，但不另起 quota / RBAC 階層。

### S-6.1 角色 Persona — Doorbell V1 客戶 A 量產期下的 4 branch

承接 S-5.1 Doorbell `firmware-doorbell-v1-customer-a` project（Quinn = owner、production stage、customer A 綁定）；S-6 把該 project 內部 branch 模型展開：

| Branch | 用途 | 預設 push policy | 預設 reviewer | 工程角色對應 |
|---|---|---|---|---|
| **`main`** | trunk、客戶 A 量產追溯來源 | protected — 走 PR + 兩位 admin / line owner approve；禁直推 | Doris (line owner) + Bob (tenant admin) 必至少一人 | Carol（contributor）平日 PR 標的、不能直推 |
| **`staging`** | 整合 / pre-prod、跑完整 nightly 測試套組 | protected — fast-forward only from feature branches；禁強制覆蓋 | Quinn (project owner) | Carol / Pam 各自 feature merge 進來測 |
| **`v2.1-hotfix`** | 量產出貨後的緊急修補（customer A 已部署 v2.1，hotfix 不能等下一次 staging release）| 允許 cherry-pick 自 main、不允許 staging 雜訊；merge 必走 fast-forward | Quinn + Doris 雙簽 | 緊急時 oncall 工程師（依 S-4.4 routing）直接開 worktree |
| **`customer-x-fork`** | customer A 客製分支（含客戶私有 secrets / 客戶限定 telemetry）、不回流 main | private — 不公開 PR；diff 紀錄寫進 customer A audit chain（呼應 S-5.3 雙鏈）| Bob (tenant admin) — 客戶分支的合規責任歸屬 | Pam（IPCam line owner、跨支援）受邀 reviewer；Carol 不能直接 push 客戶分支需 Bob grant |

**S-6.1 設計斷言**：
1. **branch 不是新 RBAC 階層** — 上表的「reviewer」「push policy」是 project-scoped policy（存在 `projects.metadata.branch_policies` 或 `project_branches` 表的 column）、不是 user role 屬性；Quinn 仍是 V1 project owner、Carol 仍是 V1 contributor、僅各 branch 的「能否 push / 能否 merge」由 policy 評估。維持 S-1.6 RBAC 二維 (user × scope) 模型不擴成三維。
2. **branch 是 first-class scope，介於 project 與 task** — workspace path / workflow_run attribution / git worktree 都依 `(project, branch)` 為 key；但 LLM cap / git_account / SOP / on-call routing 仍走 project / line / tenant 既有三層繼承（S-5.4），不為 branch 另設第四層 — 否則 resolver 爆炸（project × branch × line × tenant 四層、預設值組合過多）。
3. **`customer-x-fork` 不是 cross-tenant share** — 客戶 A 是 acme tenant 內部 `customer_accounts` row（S-5.3）、不是獨立 OmniSight tenant；customer-x-fork branch 的 commit / artifact 仍存 acme tenant 內、僅 RBAC + audit chain 加倍嚴格（雙鏈寫 acme + customer-A、push policy 從 line owner 升 tenant admin）。若 customer A 自己也是 OmniSight tenant 想直接看 fork → 走 S-3 cross-tenant share 路徑（S-5.9 Q1 已標）。
4. **branch 名稱有保留字 + 強制白名單** — `main / master / staging / production / hotfix*/release-*/customer-*-fork` 是 first-class 名稱（policy 預設套用嚴格 reviewer 規則）；`agent/*` / `task/*` / `retry/*` 是系統保留命名（worktree 內部使用，user 不能直接建）— 防 user 開 `agent/anything` branch 干擾系統 retry path（呼應 backend/workspace.py:50-56 anchor commit + agent branch 約定）。
5. **branch policy 可繼承 line / tenant default、可 per-branch override** — Doorbell 線預設「`main`/`staging` protected、`customer-*-fork` private」由 line owner Doris 設一次；V1 project 內若 Quinn 想為 `staging` 額外加「強制 nightly 過綠」就走 project-scoped override（與 S-5.4 SOP override 同模式、預設 inherit、顯式 override）。
6. **同 user 同 project 不同 branch 不同 capability** — Carol 在 V1 `main` 是「能開 PR 不能直推」、在 `staging` 是「能直推 feature/* 但 main merge 要 reviewer」、在 `customer-x-fork` 是「需 Bob grant 才能 push」；UI 在 branch picker 切換時動態展示 capability badge（不切 sidebar 結構、僅切 inline button enable 狀態）。

### S-6.2 Workspace 路徑模型 — 從 agent-scoped 升至 (project, branch, agent, task) 巢狀

**現況**（S-6.10 row 1: `backend/workspace.py:29 + 96-104`）：
```
.agent_workspaces/
└── {safe_agent_id}/                      # ← 唯一鍵
    └── agent/{safe_agent}/{safe_task}    # ← git branch 名
```

**S-6 要求**：四 branch 並行存在 + 每 branch 內可有多 agent task workspace + 不破壞既有 agent retry anchor 機制 → 升至：

```
.agent_workspaces/
├── {tenant_id}/                          # S-2 多租戶單用戶（在 multi-tenant 啟用後 nest）
│   └── {product_line_id}/                # S-4 多產品線（pl-default fallback）
│       └── {project_id}/                 # S-5 project（含 default project）
│           ├── _branches/
│           │   ├── main/                 # ← S-6 新增：long-lived branch worktree
│           │   ├── staging/
│           │   ├── v2.1-hotfix/
│           │   └── customer-x-fork/
│           └── _tasks/
│               └── {agent_id}/{task_id}/ # ← 既有 agent task workspace、改路徑但結構不變
└── _legacy/                              # 既有 .agent_workspaces/{agent_id} 透過 symlink 兼容
    └── {agent_id} → ../<full_path>       # 1 release 過渡期保留
```

**Path sanitization invariant**（沿用既有 `re.sub(r'[^a-zA-Z0-9_-]', '_', x)` 模式）：每段（tenant_id / product_line_id / project_id / branch / agent_id / task_id）獨立 sanitize；branch 額外處理 `/` → `__`（branch `feature/foo` 落地為 `feature__foo`，UI 反映原值）。

**S-6.2 設計斷言**：
1. **`_branches/` 與 `_tasks/` 分兩個子目錄、不混合** — long-lived branch worktree 是「永久 checkout、agent 進去做事」、agent task workspace 是「臨時 worktree、用完 finalize / cleanup」；兩者 lifecycle 完全不同（branch 跟 git ref、task 跟 task lifecycle）— 同層混放會讓 GC policy 爆炸（cleanup 既存 agent task 又要排除 long-lived branch）。
2. **底線前綴 `_branches/` / `_tasks/` 是保留字** — 防 branch 取名 `branches` 或 project 取名 `tasks` 撞路徑（OS 層面 hard collision）；底線前綴是 OmniSight 內部 namespace marker（與 git refs `refs/heads` / `refs/tags` 設計同哲學）。
3. **既有 `.agent_workspaces/{agent_id}` 透過 `_legacy/` symlink 過渡** — 1 release 過渡期保留 symlink、避免既有 agent / 既有 audit log path reference / 既有 finalized workspace 全部立刻失效；遷移時對既有 row 跑 backfill `migrate_workspace_paths()` script（不複製檔案、僅建 symlink + 更新 `agent_workspaces` table 的 `path` 欄位）。
4. **每段 sanitize 獨立、不允許跨段 escape** — 嚴格白名單 `^[a-zA-Z0-9_-]+$` per segment；branch 的 `/` 是合法 git 規約但落地必轉 `__`（雙 underscore 因為單 underscore 會與 sanitize 後的非法字元混淆）；防 `../` path traversal + 防 user 取 branch `..` 或 `.git` 等保留名（reject 422）。
5. **路徑不嵌入 customer_account_id 或 lifecycle_stage** — project_id 已唯一識別、customer_account_id 是計費 attribution（S-5.3）、lifecycle_stage 是狀態機（S-5.5），都是 column-level metadata 不該影響 disk path（避免 lifecycle promote 時整個 workspace 大搬家、避免 customer rebind 時 workspace 路徑漂）。
6. **disk quota 仍走 tenant-scope（既有 `backend/tenant_quota.py`）+ 新增 per-branch breakdown view** — Y9 dashboard 加「V1 內 4 branch 各佔多少 disk」切片頁；硬上限仍是 tenant 級（plan-based），per-branch 是觀察用 not enforcement。
7. **完全相容既有 `agent/{safe_agent}/{safe_task}` git branch 命名** — workspace path 改了但**不**改 git branch 命名規約（`agent/*` / `task/*` 仍由 backend/workspace.py:139-143 生成）；branch 名只是 worktree 內部的 ref，不出現在 disk path（disk path 用 `task_id` segment 而非 git branch 名）。

### S-6.3 Git Worktree 策略 — 1 bare clone × N worktree

**現況**（S-6.10 row 3: `backend/workspace.py:161-198`）：每次 `ws_provision` 對 `_MAIN_REPO`（即 OmniSight repo 自身）跑 `git worktree add`，agent task 結束後 `git worktree remove`；單個 long-lived 主倉、N 個短命 worktree。

**S-6 要求**：對「**外部**客戶 git repo」（如 customer A 的 firmware repo）支援 4 個 long-lived branch worktree + N 個臨時 agent task worktree → 升至：

```
{workspaces_root}/{tenant}/{line}/{project}/
├── _bare/                                # ← S-6 新增：每 project 一個 bare clone
│   ├── HEAD
│   ├── objects/
│   ├── refs/
│   └── packed-refs                       # 共享 object store、所有 worktree 從這裡 fork
├── _branches/
│   ├── main/                             # git worktree add ../../_bare main
│   ├── staging/
│   ├── v2.1-hotfix/
│   └── customer-x-fork/
└── _tasks/{agent_id}/{task_id}/          # git worktree add ../../_bare agent/{agent_id}/{task_id}
```

**啟動偽碼**（S-6 落地時 Y6 / R 系列實作）：

```python
# backend/workspace.py 新增（取代既有 _MAIN_REPO 單根模型）
async def ensure_project_bare(project_id: str, repo_source: str) -> Path:
    """確保 project bare clone 存在；不存在時 git clone --bare。
    
    Module-global state 稽核：bare path 由 (workspaces_root, project_id) 決定、
    每 worker 推導出同樣值（合格答案 #1）；bare clone 本身的並發走 PG advisory
    lock（合格答案 #2）。"""
    bare = workspaces_root / project.tenant_id / project.product_line_id / project_id / "_bare"
    if bare.exists():
        return bare
    async with pg_advisory_lock(("project_bare", project_id)):
        if bare.exists():  # 雙重檢查（lock 等到時可能已被別 worker 建好）
            return bare
        await _run(f'git clone --bare "{repo_source}" "{bare}"')
        return bare

async def provision_branch_worktree(project_id: str, branch: str) -> Path:
    """確保 long-lived branch worktree 存在（idempotent）。"""
    bare = await ensure_project_bare(project_id, repo_source)
    safe_branch = branch.replace("/", "__")
    wt = bare.parent / "_branches" / safe_branch
    async with pg_advisory_lock(("branch_worktree", project_id, branch)):
        if wt.exists() and (wt / ".git").exists():
            # 確保 ref 仍指對；可能 upstream 被 force-push、本地 stale
            await _run(f'git fetch origin "{branch}:{branch}"', cwd=wt)
            return wt
        await _run(f'git worktree add "{wt}" "{branch}"', cwd=bare)
        return wt

async def provision_agent_task_worktree(project_id: str, agent_id: str, task_id: str,
                                         base_branch: str) -> Path:
    """既有 ws_provision 流程；現在多了 base_branch 參數（取代隱式 HEAD）。"""
    bare = await ensure_project_bare(project_id, repo_source)
    agent_branch = f"agent/{sanitize(agent_id)}/{sanitize(task_id)}"
    wt = bare.parent / "_tasks" / sanitize(agent_id) / sanitize(task_id)
    async with pg_advisory_lock(("agent_worktree", project_id, agent_id, task_id)):
        # 從指定 base_branch（main / staging / v2.1-hotfix）fork agent branch
        await _run(f'git branch "{agent_branch}" "{base_branch}"', cwd=bare)
        await _run(f'git worktree add "{wt}" "{agent_branch}"', cwd=bare)
        # ... 既有 anchor_sha 邏輯（backend/workspace.py:200-214）保持不變
        return wt
```

**S-6.3 設計斷言**：
1. **Bare clone per project，所有 worktree 共用 object store** — 4 個 long-lived branch + N 個 agent task worktree 共用同一 `_bare/objects/`，磁碟省 4×~ + git fetch 只下載一次；bare clone 本身是 idempotent（雙重檢查 + advisory lock）每 worker 從相同 source 推導出相同路徑（合格答案 #1）。
2. **PG advisory lock keyed (project_id, branch)** — 取代既有「靠 agent_id 唯一性 + 無鎖」模型；同 project 同 branch 兩個 in-flight 操作（如 worktree add 與 branch fetch）必序列化；不同 branch / 不同 project 完全 parallel。Lock key namespace 用 hash(("branch_worktree", project_id, branch)) 對應 PG 64-bit advisory lock id；txn level 鎖（自動釋放）。
3. **Long-lived branch worktree 與 agent task worktree 兩條獨立 path** — `provision_branch_worktree` / `provision_agent_task_worktree` 分兩 function、不共用 `_workspaces` registry（branch worktree 不註冊到 `_workspaces` dict、後者只追蹤 ephemeral agent task）；理由：lifecycle 不同（前者跟 branch、後者跟 task）、cleanup policy 不同、registry 共用會混淆 GC 範圍。
4. **`agent/{agent}/{task}` 仍從指定 `base_branch` fork 而非從隱式 HEAD** — 既有 `backend/workspace.py:164` 的 `git branch "{branch}" HEAD` 升為 `git branch "{agent_branch}" "{base_branch}"`；workflow_run 觸發時必帶 `branch` 參數（S-6.5 endpoint 規格化）；缺 branch 預設 `main`（與既有 HEAD 行為對齊但顯式化）。
5. **External clone path（既有 `git clone "{source}" "{ws_path}"`）退役**（`backend/workspace.py:193-197`） — 改一律走「先 bare clone 一份 → worktree add」；理由：既有路徑每 task fresh clone 浪費頻寬 + 每 task 各自 push 認證、改 bare clone 後 4 branch + N task 共用一次 fetch 的 object pack。external repo 的 auth env（`backend/git_auth.py`）只在 bare clone 時用一次。
6. **Force-push detection + recovery** — `provision_branch_worktree` 的 fetch 步驟若回 `non-fast-forward` 警告（即 upstream 被 force-push）、worktree 內 working tree 標 stale、SSE 推 project owner notification + audit 寫 `branch.upstream_force_pushed(branch=...)` 不自動 reset；強制 reset 走人類動作 `POST /projects/{id}/branches/{branch}/reset-to-upstream` + step-up MFA（防意外 lose 本地 commit）。
7. **保留 anchor_sha 不變**（呼應既有 R8 #314 design） — `provision_agent_task_worktree` 仍 capture `anchor_sha`、retry path 仍從 anchor 重建；S-6 把 anchor 概念明確 anchor 在「branch fork 時 base_branch 的當下 HEAD」（既有「HEAD」隱式版本對齊）。

### S-6.4 Branch Lifecycle 狀態機 — long-lived vs ephemeral

S-6 引入 typed branch type + lifecycle stage：

```
                                           ┌── deleted (可選；ref 真的刪掉)
                                           ▼
   ┌── active ──→ frozen ──→ archived (可選 un-archive 30d) ──→ purged
   │   │            │
   │   │            └── (frozen → active 復活、僅 long-lived)
   │   │
   │   └── (long-lived 與 ephemeral 在 active 階段同 schema、其他階段歧異)
   │
   └── (新建)
```

**Branch type × lifecycle 表**：

| Type | active 行為 | frozen 行為 | archived 行為 |
|---|---|---|---|
| **long-lived**（main / staging / v2.1-hotfix / customer-x-fork） | worktree 永久存在；可被多 agent task fork；可手動 delete-ref 但有 protected guard | worktree 仍在但 push policy 升「禁所有 push」；通常 release 過後 freeze 防誤改 | worktree 拆掉、git ref 保留 + tag `archived/<branch>/<date>`；un-archive 30d 內可逆復活 worktree |
| **ephemeral**（`agent/*` / `retry/*` / `task/*`） | worktree 在 task lifecycle 內存活；task finalize 後 worktree cleanup、ref 保留 | 不適用（ephemeral 無 freeze 概念） | task 結束後 90d ref 進 GC 候選、purge cron 跑時刪 ref + objects（無 reachable parent 時）|

**S-6.4 設計斷言**：
1. **long-lived 與 ephemeral branch 在 schema 用 enum 顯式區分**（`project_branches.kind text CHECK IN ('long_lived', 'ephemeral')`） — 不靠 branch 名前綴推斷（`agent/*` 是約定 / 名前綴可被 user 繞過）；schema 級 enum 才是 invariant 來源。
2. **long-lived branch 升 frozen 是 release 流程的 first-class 動作** — 不靠 git protected branch hook（外部設定不可審計）；走 `POST /projects/{id}/branches/{branch}/freeze`、寫 audit `branch.frozen(reason='v2.1.0_released')`、push policy 自動切「reject all」、UI banner 顯示 freeze 狀態。
3. **archived 是 worktree 拆 + ref 保留 + tag mark**（與 S-5.5 project archived 同設計哲學） — disk 釋放（worktree 拆）但歷史保留（ref + tag），便於後續 forensic / 客戶資料可回溯；un-archive 在 30d 內由 project owner 單方解（不需要 admin）— 因為 branch archive 比 project archive 影響範圍小（只該 branch、不影響其他 branch），緩衝期可短於 S-5.5 的 90d。
4. **purged 是不可逆終態 — 只對 ephemeral + 30d 後** — long-lived branch 不會 auto-purge（即使 archived 後 30d 也保留 ref + tag）；ephemeral branch 在 archived 後 90d 自動 purge（git gc reachable from no ref）；purge 寫 audit `branch.purged(name=..., last_commit=...)` 包含最後 commit SHA forensic 用。
5. **agent task workspace 不直接寫 `branch_lifecycle_history`** — task workspace 本身有 finalize / cleanup（既有 backend/workspace.py:862）、不需要 branch-level history；只 long-lived branch 與 ephemeral branch（kind=ephemeral but persisted as ref）兩類進 lifecycle 表。
6. **branch lifecycle 變更必走 audit + project chain 雙寫**（呼應 S-3 / S-5 雙鏈設計） — `customer-x-fork` 的 lifecycle 變更必雙寫 acme tenant chain + customer-A chain（因 customer-x-fork 與 customer A 直接綁）；其他 branch 只寫 acme tenant chain。
7. **`v2.1-hotfix` 是 release 後的 typed branch 模式** — 自動命名 `<release-tag>-hotfix`、自動繼承 release tag 對應 commit 為 base、自動套 push policy「from main cherry-pick only」；UI 提供「create hotfix from release」一鍵動作；release 結束 30d 自動 freeze（避免長期野生）。

### S-6.5 workflow_run × branch attribution

S-6 要求每個 workflow_run 必綁 branch：

**現況**（S-6.10 row 5: `backend/alembic/versions/0002_workflow_runs.py`）：`workflow_runs` 無 `branch` 欄位、無 `project_id` FK；`backend/alembic/versions/0006_project_runs.py` 有 join 表 `project_runs(project_id, workflow_run_ids)` 但 project_id 是 string label。

**S-6 schema 增量**（與 Y4 對齊）：

```
workflow_runs                  -- 既有表 + S-6 新增欄位
  ...
  project_id          uuid fk projects(id) NULL          -- S-5 / Y4 加（先 nullable）
  branch              text                               -- S-6 加：'main' / 'staging' / 'v2.1-hotfix' / 'customer-x-fork' / 'agent/foo/task-42'
  branch_kind         text                               -- S-6 加：'long_lived' / 'ephemeral'，冗餘但加速 query
  base_branch         text                               -- S-6 加：fork 自哪 branch（agent/* 必填，long_lived 為 NULL）
  ...
  -- index: (project_id, branch, started_at DESC) for "show V1 main 最近 100 runs"
```

**S-6.5 設計斷言**：
1. **`workflow_runs.branch` NOT NULL（兩階段 migration）** — Y4 落地時先 nullable + backfill `'main'`、1 release 後加 NOT NULL；理由：S-6.5 attribution 是核心 invariant、不能 NULL 否則 LLM cost / artifact / audit 都無法歸因到正確 branch；但 NOT NULL 變更需與既有資料 backfill 雙寫（與 S-5.6 partial CHECK 同模式）。
2. **branch 不引入新 quota 維度** — LLM cap 仍走 project（S-5.2 三層 atomic decrement）、disk quota 仍走 tenant；branch 只是 attribution + dashboard 切片用。理由：S-6.1 設計斷言 2「branch 不擴成第四層繼承」；多 branch 同時跑爆同 project cap 是預期行為（呼應 S-5.7 緊邊界 line budget）。
3. **`base_branch` 用於 anchor 推導 + audit forensic** — agent/* ephemeral branch 的 `base_branch='main'` / `'staging'` / `'v2.1-hotfix'` 必填、否則 retry path 不知該從哪 fork；long-lived branch 自身 `base_branch=NULL`（自己就是 base）；audit 寫 `workflow_run.started(branch=..., base=...)` 雙欄。
4. **`branch_kind` 是冗餘欄位但加速 query** — 從 `branch` 名前綴或 `project_branches.kind` 都可推、但 dashboard query「列出 V1 過去 30d ephemeral run 數」不想 join；冗餘欄位寫入時走 trigger 或 application-level 約束（兩處保持 sync）。
5. **同一 workflow_run 不能跨 branch** — 一個 run 對應恰好一個 (project, branch) tuple；想 cross-branch 比對（如「main vs customer-x-fork 在同一 workflow 跑出來的 artifact diff」）走外部 dashboard 比對 N 個 run、不在 single run 內混 branch。
6. **artifact / audit_log 也加 `branch` 欄位**（與 workflow_run 同模式） — `artifacts.branch` + `audit_log.branch` 兩處加 nullable text；UI 切片頁可依 branch 過濾 artifact / audit（partial index `WHERE branch IS NOT NULL`）。

### S-6.6 schema 增量（與 Y1 / Y4 對齊）

S-6 在 Y1 / Y4 / Y6 / Y9 落地時對 schema 的增量（在 S-1.6 + S-2.6 + S-3.5 + S-4.6 + S-5.6 既有設計上加）：

```
project_branches             -- Y4 新表（S-6.4 long-lived + ephemeral lifecycle 權威源）
  id                  uuid pk
  project_id          uuid fk projects(id) NOT NULL
  name                text NOT NULL                       -- 'main' / 'agent/carol/task-42' 等原值（含 '/'）
  kind                text NOT NULL                       -- CHECK IN ('long_lived', 'ephemeral')
  lifecycle_stage     text NOT NULL DEFAULT 'active'      -- CHECK IN ('active','frozen','archived','purged')
  base_branch_name    text                                -- ephemeral 必填、long_lived 為 NULL
  push_policy         text NOT NULL DEFAULT 'protected'   -- CHECK IN ('protected','fast_forward_only','open','private','reject_all')
  metadata            jsonb NOT NULL DEFAULT '{}'         -- 含 reviewer 設定 / hotfix release tag / customer fork audit chain pointer
  created_at          timestamptz NOT NULL
  archived_at         timestamptz
  purged_at           timestamptz
  UNIQUE (project_id, name)
  CONSTRAINT base_branch_required CHECK (
    (kind = 'ephemeral' AND base_branch_name IS NOT NULL) OR
    (kind = 'long_lived' AND base_branch_name IS NULL)
  )
  -- partial unique index: 一 project 內 long_lived branch 同 name 不能並存 active & archived
  -- CREATE UNIQUE INDEX idx_project_branches_active ON project_branches(project_id, name)
  --   WHERE lifecycle_stage = 'active'

branch_lifecycle_history     -- Y4 新表（S-6.4 狀態機轉移 audit 副本）
  id                  uuid pk
  branch_id           uuid fk project_branches(id) NOT NULL
  from_stage          text                                -- NULL = 新建
  to_stage            text NOT NULL
  changed_by          uuid fk users(id)                   -- NULL = system cron（archived → purged）
  reason              text                                -- 'release_tagged' / 'force_push_recovery' / 'agent_task_finalized' 等
  metadata            jsonb NOT NULL DEFAULT '{}'         -- 含 last_commit_sha / release_tag 等 forensic 欄
  changed_at          timestamptz NOT NULL

agent_workspaces             -- 既有 in-memory `_workspaces` dict 升 durable 表
  agent_id            text PRIMARY KEY
  task_id             text NOT NULL
  project_id          uuid fk projects(id) NOT NULL       -- S-5 加
  branch_id           uuid fk project_branches(id)        -- S-6 加：agent task fork 自哪 branch
  agent_branch_name   text NOT NULL                       -- 'agent/{agent}/{task}'
  workspace_path      text NOT NULL                       -- S-6.2 新版 nested path
  status              text NOT NULL DEFAULT 'active'      -- 'active' / 'finalized' / 'cleaned'
  anchor_sha          text                                -- R8 既有
  created_at          timestamptz NOT NULL
  finalized_at        timestamptz
  cleaned_at          timestamptz
  -- index: (project_id, branch_id, status) for "show V1 main 上仍 active 的 agent task"

workflow_runs                -- 既有表 + S-6 新增欄位（呼應 S-6.5）
  ...
  project_id          uuid fk projects(id) NULL           -- S-5 / Y4 加
  branch              text                                -- S-6 加（兩階段 NULL → NOT NULL）
  branch_kind         text                                -- S-6 加（冗餘加速）
  base_branch         text                                -- S-6 加
  ...
  -- index: (project_id, branch, started_at DESC)

artifacts                    -- 既有表
  ...
  branch              text                                -- S-6 加 nullable + partial index

audit_log                    -- 既有表（S-3 / S-4 / S-5 已加多欄、S-6 再加）
  ...
  branch              text                                -- S-6 加 nullable + partial index `WHERE branch IS NOT NULL`
```

**S-6.6 設計斷言**：
1. **新表 2 張**（`project_branches` + `branch_lifecycle_history`） + **既有表加欄位 3 張**（`workflow_runs` + `artifacts` + `audit_log`） + **`agent_workspaces` 從 in-memory 升 durable 表**（既有 `backend/workspace.py:60 _workspaces dict` 是 module-global，多 worker 不共享 → 必須升 PG row）— 維持「擴充既有 schema、不另起平行表」的 Y 系列共識（S-3.5 / S-4.6 / S-5.6 已建立模式）。
2. **`project_branches.kind` 用 text + CHECK 而非 PG enum**（沿用 S-3.5 / S-4.6 / S-5.6 既有 CHECK 設計）— 避免 enum migration 痛點；`lifecycle_stage` 同模式。
3. **`agent_workspaces` 升 durable PG 表是必要 module-global state 修復**（呼應 SOP Step 1 module-global 稽核） — 既有 `_workspaces: dict[str, WorkspaceInfo]` 是 module-level dict、`uvicorn --workers 4` 下 4 worker 各持一份不同步、registry 一致性靠各 worker 自己 startup `cleanup_orphan_worktrees()` 補（startup 才一致；runtime 期間 worker 互相不知對方創了哪些 workspace）— S-6 多 branch 情境下這 race 變嚴重（worker A 給 branch=`main` 開 task workspace、worker B 同時給 branch=`staging` 開另一 task、registry 各持半份）；改 PG 表 + 既有 advisory lock 序列化是合格答案 #2「透過 PG 協調」。
4. **`project_branches` partial unique index 處理 active 與 archived 並存** — 同 project 同 name long-lived branch 可能被 archived 後又重建（如 `v2.1-hotfix` 第一輪 archived、第二輪 release v2.2 又開新的）；partial unique 只 enforce active row、archived row 可多份（`WHERE lifecycle_stage = 'active'`、與 S-5.6 `idx_projects_active_per_line` 同模式）。
5. **`base_branch_name` CHECK constraint 強制 ephemeral 必填、long_lived 必空** — 防 schema 層 silent bug（`agent/foo/task-42` 沒填 base 的話 retry path 找不到 fork 點）；CHECK 相容兩階段（`kind='long_lived' AND base_branch_name IS NULL` 與 `kind='ephemeral' AND base_branch_name IS NOT NULL` 互斥組合）。
6. **`audit_log.branch` partial index** — `WHERE branch IS NOT NULL`；既有 audit query 不依 branch（已加 product_line_id / project_id / share_id partial index），new partial index 加速「列出 V1 customer-x-fork 過去 30d 所有 audit」forensic 用；非必查欄不加 mandatory index、避免拖慢 audit_log insert hot path。
7. **`workflow_runs.branch` 兩階段 NOT NULL migration** — Y4 落地時 nullable、Y4+1 release 把既有 row backfill `'main'` 後加 NOT NULL；同時 backfill 也補 `branch_kind='long_lived'` + `base_branch=NULL`（既有 run 預設視為 main 上的 long-lived run）。

### S-6.7 Operator 工作流 — Doorbell V1 從 1 branch 變 4 branch 的 7 步演進

從 V1 既有「只有 main」（S-5.7 落地後）演進到 4 branch 並行：

1. **Day 0 — V1 既有狀況（S-5.7 落地後）**  
   `firmware-doorbell-v1-customer-a` project 在 acme tenant `pl-doorbell` 線下、lifecycle=`production`、customer A 綁定；git remote origin=`acme-doorbell-bot` 帳號（S-4.3 git resolver 走 line-default）；只有 `main` long-lived branch、`agent/*` ephemeral 隨 task 開合；workspace path 仍是 S-5 新版 `_branches/main/` + `_tasks/{agent}/{task}/`。

2. **Day 1 — Quinn 開 `staging` long-lived branch**  
   Quinn 走 `POST /api/v1/projects/firmware-doorbell-v1-customer-a/branches { name: 'staging', kind: 'long_lived', base_ref: 'main', push_policy: 'fast_forward_only' }`。  
   backend：(a) `provision_branch_worktree('staging')` 建 worktree `_branches/staging/`、(b) PG 寫 `project_branches(name='staging', kind='long_lived', lifecycle_stage='active')`、(c) audit 寫 `branch.created(name='staging', from='main')`、(d) SSE 推 V1 project members banner「staging branch 已上線」。

3. **Day 7 — release v2.0 + Carol 觸發第一個 staging-targeted workflow_run**  
   Carol 走 `POST /workflows/run { workflow_id: 'nightly-firmware-test', project_id: V1, branch: 'staging' }`。  
   backend：(a) `provision_agent_task_worktree(project, agent, task, base_branch='staging')` 建 `_tasks/{agent}/{task}/`、(b) git branch `agent/{agent}/{task}` fork 自 staging HEAD、(c) `workflow_runs` row 寫 `branch='staging' base_branch='staging' branch_kind='ephemeral'`、(d) anchor_sha 取自 staging HEAD（呼應 S-6.3 設計斷言 7）。

4. **Day 14 — Customer A 反饋 v2.0 出貨後緊急 bug、Quinn 開 `v2.1-hotfix`**  
   Quinn 走 `POST /branches { name: 'v2.1-hotfix', kind: 'long_lived', base_ref: 'tag/v2.0.0', push_policy: 'protected', metadata: { release_tag: 'v2.0.0', auto_freeze_after_days: 30 } }`。  
   backend：(a) 建 worktree `_branches/v2.1-hotfix/` 自 release tag `v2.0.0` fork（不是從 main HEAD、避免帶入 v2.0 之後的 main 變更）、(b) push_policy='protected'+雙簽 reviewer（Quinn+Doris）、(c) audit 寫 `branch.created(name='v2.1-hotfix', from_tag='v2.0.0', kind='hotfix')`。

5. **Day 14+1h — Bob 開 `customer-x-fork` 客製分支**  
   Bob（tenant admin、因 customer-x-fork 是 customer A 私有需 admin）走 `POST /branches { name: 'customer-x-fork', kind: 'long_lived', base_ref: 'main', push_policy: 'private', metadata: { customer_account_id: '<cust-a-id>' } }`。  
   backend：(a) 建 worktree、(b) audit 雙鏈寫 acme tenant chain + customer-A chain（branch.metadata.customer_account_id 觸發 S-3 / S-5 雙鏈規則）、(c) push policy 升「private + audit 全 push 雙鏈」、(d) Pam（IPCam line owner）獲邀 reviewer（cross-line support）。

6. **Day 30 — release v2.1.0 + auto-freeze cron 跑**  
   release tag `v2.1.0` 自 v2.1-hotfix push 出去後 30d、auto_freeze_after_days cron 觸發：`PATCH /branches/v2.1-hotfix { lifecycle_stage: 'frozen', reason: 'release_v2.1.0_completed' }`。  
   backend：(a) push_policy 自動切 'reject_all'、(b) UI banner「branch frozen」、(c) audit 寫 `branch.frozen` + `branch_lifecycle_history(from='active', to='frozen', changed_by=NULL, reason='auto_freeze_after_30d')`、(d) worktree 仍在（archive 才拆）。

7. **Day 60 — 4 branch 並行運行、SSE dashboard 顯示用量切片**  
   V1 project 內：`main`（穩定 trunk、Carol 平日 PR 標的） / `staging`（每晚 nightly 跑）/ `v2.1-hotfix`（frozen、僅查歷史） / `customer-x-fork`（Pam 主動維護、客戶反饋走這條）。  
   Y8 dashboard：(a) 「V1 LLM 30d 用量 18M / 20M cap」總計（呼應 S-5.2 project cap）、(b) 各 branch 切片：main 8M / staging 6M / v2.1-hotfix 0M / customer-x-fork 4M、(c) 每 branch 最近 5 個 workflow_run 列表、(d) 每 branch 對應 customer audit chain（僅 customer-x-fork 雙鏈、其他單鏈）。

**S-6.7 設計斷言**：
1. **建 long-lived branch 由 project owner（Quinn）發起、customer-x-fork 升 tenant admin（Bob）** — 一般 long-lived branch 是工程治理範疇 project owner 即可；客戶私有 fork 是合規範疇升 tenant admin（呼應 S-6.1 customer-x-fork reviewer 規則）。
2. **`v2.1-hotfix` fork 自 release tag 而非 main HEAD** — 與 S-3.6 三段式狀態機 / S-5.5 graduated 中介態同設計哲學：跨重大邊界（release）必有 anchor、避免帶入 release 之後 main 上的不想要變更；`from_tag` 是 first-class 欄位（不是 metadata）的考量留 Y4 落地時定。
3. **`auto_freeze_after_days` 是 release 後 hygiene 機制** — 防 v2.1-hotfix 一直開著被當 long-term feature branch 用；30d 是合理 default（一般 hotfix release 28d 觀察期 + 緩衝、與 S-5.5 graduated 30d 觀察期級別一致）；user 可在 metadata 顯式覆寫（30/60/90 三選）。
4. **4 branch 同時跑爆 project cap 是預期行為** — V1 project cap 20M、4 branch 各跑 nightly + on-demand 加總若超 20M 是 throttle 該 project（S-5.2 設計斷言 2）；UI 預先警告「某 branch 用量月增 X% 接近 project cap」、不為 branch 自動分 cap（呼應 S-6.5 設計斷言 2「branch 不引入 quota 維度」）。

### S-6.8 邊界 / 退化情境

| 邊界場景 | 預期行為 | 驗收條件 |
|---|---|---|
| Carol（contributor）想直推 V1 `main` | 403 — main push_policy='protected' 走 PR + 雙簽；UI 在 PR 創建時 require min 2 reviewer（Doris+Bob 至少一） | Y4 git push hook（pre-receive）+ PR endpoint check `project_branches.push_policy` |
| 兩個 workflow_run 同時 target V1 `staging`（不同 task_id） | 完全允許（兩 ephemeral worktree 各自 fork staging HEAD、staging worktree 本身不變動）；advisory lock 只 serialize 「同 project 同 branch 的 worktree 元操作」、不 serialize agent task workspace 內部活動 | Y6 PG advisory lock keyed `(project_id, agent_id, task_id)`；多 task 並行 OK |
| Quinn force-push V1 `main`（rebase 重寫歷史） | (a) `provision_branch_worktree` 下次 fetch 偵測 `non-fast-forward`、worktree 標 stale；(b) 仍 active 的 `agent/*` ephemeral 若 base_branch=main 會看到 anchor_sha 已不在 remote main、retry path 走 anchor 自身（仍指向被 rebase 前的 commit、forensic 可重建）；(c) audit 寫 `branch.upstream_force_pushed` + SSE 警告 + 不自動 reset | Y6 force-push detection；Y8 stale banner |
| `customer-x-fork` 與 main 嚴重 diverge（半年沒同步）想 cherry-pick 一批 main 上的 commit | 完全合法；走 git merge / git cherry-pick 之外無 OmniSight 額外限制；audit 寫每筆 cherry-pick commit 對應的 source / target branch | Y6 不擴；走標準 git 操作 |
| Bob 想刪 V1 `main` ref | 422 — `main` 是 project default branch（`projects.metadata.default_branch='main'`）+ `kind='long_lived'`、刪 default branch 必先改 default、且需 tenant owner step-up MFA；UI 引導「先設新 default 再刪舊」 | Y4 DELETE branch endpoint check default_branch 條件 |
| `agent/carol/task-42` ephemeral branch 跑到一半 V1 project 被 archive（S-5 lifecycle） | (a) 立即 reject 新 workflow_run、(b) 既有 in-flight task 走 graceful shutdown（與 S-5.7 cron archive 同流）、(c) workspace 進 archived 狀態、agent_workspaces.status='cleaned'；ephemeral branch ref 在 90d 後 cron purge | Y4 project archive 觸發 cascade；Y6 workspace cleanup hook |
| User 取 branch 名 `..` 或 `_branches` 或 `_tasks` 或 `agent/x` | 422 — schema 級 CHECK + application 級 reject；保留命名 `_branches/_tasks/agent/*/retry/*/task/*` 全 reject；防 path traversal + 命名衝突 | Y4 POST /branches input validation；CHECK constraint on `project_branches.name` regex |
| 同 project 同 branch name `staging` 已 archived、想再開新的 `staging` | 完全允許；partial unique 只 enforce active row、archived row 可多份；新 row 是 fresh `kind='long_lived' lifecycle_stage='active'`、舊 row 留 `archived` | partial unique index `WHERE lifecycle_stage='active'` |
| `customer-x-fork` push 時 push policy='private'、Carol 試圖 review 該分支 PR | UI 隱藏 PR（partial filter；Carol 不在 reviewer list）；endpoint /api/v1/projects/V1/branches/customer-x-fork/prs 對 Carol 回 403；audit 寫 `branch.private_pr_access_denied` | Y4 endpoint check `push_policy='private'` + reviewer list whitelist |
| customer A churn（customer_accounts.status='churned'）、customer-x-fork 該怎麼辦 | (a) customer-x-fork 不自動 archive（呼應 S-5.9 Q5 churn 不自動 archive）、(b) UI banner 警告「客戶已 churn、此 fork 是 dangling」、(c) Bob 顯式決定 archive / 重歸 main / 留作參考；archive 時 worktree 拆但 ref 留 + tag `archived/customer-x-fork/2026-...`（forensic 用）| Y4 customer churn 觸發 SSE + Y8 banner；branch archive 走人類動作 |
| 既有 `.agent_workspaces/{agent_id}` legacy path 在升級後 worker 找不到 | `migrate_workspace_paths()` script 在 alembic upgrade 時跑、為每筆 in-flight workspace 建 symlink；worker 啟動時 `cleanup_orphan_worktrees()` 對 legacy + new path 都 scan；過渡期 1 release 後移除 legacy path 支援 | Y6 migration script + symlink；Y6+1 release 移除 legacy code path |
| Doorbell 線上線後 Carol 看到 V1 project 內 4 個 branch、想知道每 branch 的最近 commit / 是誰改的 | Y8 frontend 在 project detail page 加 branch list 表格：每 row 顯示 name / kind / lifecycle / 最後 commit（SHA + author + timestamp） / 30d push 次數 / 30d workflow_run 次數 / 30d LLM token 用量切片 | Y8 GET /projects/{id}/branches/summary endpoint + 表格 UI |
| Bob 想對 V1 project 設「所有 branch 都 require Doris approve」（line-level branch policy） | line owner Doris 在 line-level 設 default branch policy `{ all_branches_require_approver: 'line_owner' }`、project 自動繼承（與 S-5.4 SOP 三層繼承同模式）；project 想 override 走顯式按鈕 | Y4 product_line.metadata.branch_policy_default + project-level override UI |

### S-6.9 Open Questions（標記給 Y1～Y10 後續勾選）

1. **「主倉 vs 外部 repo 的 bare clone 邊界」** — S-6.3 規格化「每 project 一個 bare clone」假設了 `repo_source` 是 git URL；但 OmniSight 自身也是「project」之一（`backend/workspace.py:33 _MAIN_REPO`）— OmniSight repo 自己是否也走 bare clone 模型？目前傾向「OmniSight repo 是特殊 project，不走 bare clone（OmniSight 本身不在 `.agent_workspaces/` 內），保持既有 `_MAIN_REPO` 直接 worktree from」；但這個非對稱性需在 Y6 落地時定。
2. **「customer-x-fork 的 secrets 注入路徑」** — customer-x-fork 內可能有 customer A 私有的 LLM key（A 想用自己的 Anthropic 帳號跑、不燒 acme tenant cap）、或 customer A 私有的 firmware signing key — 是繼承 acme tenant_secrets（S-1.3）還是另起 customer_secrets（per-customer secret store）？目前傾向「customer 私有 secret 走 customer_secrets per-customer key 表（與 S-3 cross-tenant secret 隔離設計同哲學再下一層）」、但 schema + RBAC 留 Y3 / Y9 落地時定。
3. **「agent/* ephemeral branch 的 retry semantics 是否跨 base_branch 變更」** — Carol 在 main 開 task → fork agent/carol/task-42 from main HEAD → main 被 force-push → retry 時 anchor_sha 仍指原 main commit、但 base_branch=main 已不同 — retry 是該回 anchor（既有 R8 #314 行為）還是走「重 fork 自新 main HEAD」？目前傾向「retry 仍走 anchor（保持 R8 invariant），警告 user main 已動」；但這需 Y6 retry path 落地時驗證 + 寫測試。
4. **「branch 是否該支援 nested hierarchy / merge graph 視覺化」** — 部分團隊想看「customer-x-fork 從 main fork 出去多少 commit、main 之後又長出多少 commit、diverge graph」— 是否該在 OmniSight 內建 git graph 渲染？目前傾向「不內建（Gerrit / GitLab / GitHub 已做得好），OmniSight 只做 branch metadata + workflow_run 切片」；但 Y8 dashboard 落地時可能仍要做最簡 ascii graph。
5. **「branch 對 chatops mirror 的能見度」** — Carol 在 `customer-x-fork` push commit 時 chatops mirror（既有 R 系列）是該 mirror 到 acme 全 tenant Slack channel 還是僅 customer A 私有 channel？呼應 S-3.4 設計斷言 1「跨 tenant 看到的 ≤ host viewer 看到的」、傾向「customer-x-fork 預設只 mirror 到 customer-A scoped channel + acme admin channel」；但 channel 路由邏輯留 R 系列 chatops 重寫時定。

### S-6.10 既有實作對照表

S-6 設計與目前 codebase（截至 2026-04-25）的對齊狀況：

| S-6 invariant | 目前狀況 | 缺口 |
|---|---|---|
| Workspace path layout（per-tenant / per-line / per-project / per-branch nested） | ❌ — `backend/workspace.py:29` `_WORKSPACES_ROOT = .agent_workspaces`；`backend/workspace.py:96-104` path 由 `safe_agent_id` 單一 segment 構成、不嵌入 project_id / line / tenant；`safe_agent_id = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_id)` 既有 sanitize 邏輯可重用 | Y6 改寫 path computation：`{tenant}/{line}/{project}/_branches/{branch}/` 與 `{tenant}/{line}/{project}/_tasks/{agent}/{task}/` 兩條；`migrate_workspace_paths()` script + 1 release symlink 過渡 |
| Branch 作為 first-class scope | ❌ — `backend/models.py:61-75` `AgentWorkspace.branch` 是欄位但僅作 「agent task git branch ref」字串、未代表 long-lived branch；`workflow_runs` 表（`backend/alembic/versions/0002_workflow_runs.py`）無 branch / project_id；`agent_workspaces` 完全不存在 PG 表（in-memory dict） | Y4 新建 `project_branches` 表（11 欄）+ `branch_lifecycle_history` 表（7 欄）；`agent_workspaces` in-memory 升 PG 表（11 欄）；`workflow_runs` + `artifacts` + `audit_log` 各加 `branch` text + partial index |
| Bare clone per project + N worktree | ❌ — `backend/workspace.py:161-198` 對 `_MAIN_REPO` 直接 `git worktree add` 或對 external 直接 `git clone`；無 bare clone 中介層 | Y6 新建 `ensure_project_bare(project_id, repo_source)` + `provision_branch_worktree(...)` + `provision_agent_task_worktree(...)` 三 fn；`_MAIN_REPO` 是 OmniSight 自身、留特例（S-6.9 Q1） |
| PG advisory lock per (project, branch) | ❌ — `backend/workspace.py:622-859` 既有 cleanup 路徑用 in-memory `_workspaces` dict、無 PG lock；`.git/index.lock` 60s 容忍是 fs 層 + 啟動掃；runtime 並發無防護 | Y6 加 `pg_advisory_lock(("branch_worktree", project_id, branch))` + `pg_advisory_lock(("agent_worktree", ...))` 兩條；txn-level lock 自動釋放 |
| Long-lived branch lifecycle 狀態機 | ❌ — 完全不存在；branch 僅為 git ref、無 typed status / freeze / archive 概念 | Y4 加 `project_branches.lifecycle_stage` 4 enum（active / frozen / archived / purged）+ branch_lifecycle_history join 表 + `POST /branches/{id}/freeze` + `POST /branches/{id}/archive` + `POST /branches/{id}/un-archive`（30d 內可逆） |
| `workflow_runs.branch` 欄位 | ❌ — `backend/alembic/versions/0002_workflow_runs.py` 僅 (id, kind, started_at, completed_at, status, last_step_id, metadata)；branch 不在 first-class 欄位、metadata jsonb 也未約定 | Y4 加 `branch text` + `branch_kind text` + `base_branch text`、兩階段 NULL → backfill 'main' → NOT NULL；index `(project_id, branch, started_at DESC)` |
| `workflow_runs` 接受 branch 參數 | ❌ — `backend/routers/invoke.py:1039-1050` `ws_provision()` 不傳 branch；branch 由 `agent_id / task_id` 推導 | Y4 `POST /workflows/run` schema 加 `branch?: string default 'main'`；invoke router 傳給 `provision_agent_task_worktree(... base_branch=branch)` |
| Branch push policy enforcement | ❌ — 無 push hook / pre-receive hook；既有 git push 走 git_credentials.py / S-4.3 git resolver、不檢 push_policy | Y4 加 `project_branches.push_policy` 5 enum + 在 PR endpoint / git push hook 檢；'protected' 走雙簽 reviewer、'fast_forward_only' 走 git fast-forward 檢、'private' 走 reviewer whitelist、'reject_all' frozen 用 |
| Per-branch LLM cost / artifact / audit attribution | ❌ — 既有 `backend/llm_secrets.py:106-216` LLM gating 不分 branch（亦不分 project，待 S-5 落地）；artifacts 表（`backend/alembic/versions/...`）無 branch 欄；audit_log 已加 product_line_id / project_id / share_id（S-3 / S-4 / S-5）但無 branch | Y6 `llm_token_meter.check_budget()` 加 `branch` context（不擴 cap 維度只記 attribution）；artifacts.branch 加 nullable + partial index；audit_log.branch 加 nullable + partial index `WHERE branch IS NOT NULL` |
| Frontend branch picker | ❌ — `components/omnisight/workspace-context.tsx:35-79` `WorkspaceState` 只 (type / project / agentSession / preview)、無 branch；`app/workspace/[type]/{software,web,mobile}/page.tsx` 無 branch picker；project switcher 走 lib/tenant-context.tsx 但 project 維度本身亦待 S-5 落地 | Y8 新增 `lib/branch-context.tsx` + `useBranch()` + sidebar branch picker subordinate to project picker（呼應 S-5.10）；project detail page 加 branch list 表格（S-6.8 row 12） |
| Per-branch dashboard | ❌ — Y8 dashboard 仍待 S-5 落地 project-level、無 branch 切片 | Y8 加 `/projects/{id}/branches` 頁、跨 branch 列出近期 workflow_run / 30d LLM 用量 / push 頻率 / lifecycle stage badge |
| `agent_workspaces` durable PG 表（取代 in-memory dict） | ❌ — `backend/workspace.py:60` `_workspaces: dict[str, WorkspaceInfo]` 是 module-global、`uvicorn --workers N` 多 worker 各持一份；既有 `cleanup_orphan_worktrees()` startup 對齊但 runtime 期間 worker 互不知 | Y4 新表 + 寫入時序：`provision_*` 一律先寫 PG row 再做 disk op、advisory lock 保護；既有 `_workspaces` dict 退役 / 改為 read-through cache（合格答案 #2「透過 PG 協調」） |
| Force-push detection + recovery flow | ❌ — 既有 fetch / pull 路徑無 detection；branch ref stale 時靜默 | Y6 `provision_branch_worktree` fetch step parse `non-fast-forward` warning；audit `branch.upstream_force_pushed`；UI banner；`POST /branches/{name}/reset-to-upstream` 強制 reset 走 step-up MFA |
| Branch GC / purge cron | ❌ — 既有 `cleanup_orphan_worktrees()` 是 startup 一次性掃；無周期性 GC；ephemeral branch ref 在 task 結束後留下無 GC | Y6 cron `branch_purge_after_90d`：對 `lifecycle_stage='archived' AND archived_at < now() - 90d` 的 ephemeral branch 拆 ref + git gc；不對 long-lived auto-purge |

**S-6.10 對 Y1 / Y4 / Y6 / Y8 / Y9 的關鍵 deliverable**：
1. **Y4 新增 2 表 + 1 表升 durable + 3 表加欄位** — `project_branches`(11 欄)、`branch_lifecycle_history`(7 欄)、`agent_workspaces` in-memory dict 升 PG 表(11 欄)、`workflow_runs.branch` + `branch_kind` + `base_branch`、`artifacts.branch`、`audit_log.branch`；外加 1 條 partial unique index（active branch per project）+ 1 條 CHECK（base_branch_required）。
2. **Y4 endpoint set** — `POST /projects/{id}/branches` + `PATCH /branches/{id}` + `DELETE /branches/{id}`（含 default_branch guard）+ `POST /branches/{id}/freeze` + `POST /branches/{id}/archive` + `POST /branches/{id}/un-archive`（30d 內可逆）+ `POST /branches/{id}/reset-to-upstream`（force-push recovery、step-up MFA）+ `POST /workflows/run` schema 加 `branch` 參數 + `GET /projects/{id}/branches/summary`（每 branch 30d push / run / token 切片）。
3. **Y6 workspace 重寫** — `ensure_project_bare()` + `provision_branch_worktree()` + `provision_agent_task_worktree()` 三 fn；既有 `ws_provision` 退役 / 包裝；`_workspaces` in-memory dict 改為 PG-backed read-through cache；PG advisory lock 鎖 (project_id, branch) 與 (project_id, agent_id, task_id) 兩 namespace；force-push detection；`migrate_workspace_paths()` 一次性 script + 1 release symlink 過渡。
4. **Y6 LLM token meter 加 branch context** — `llm_token_meter.check_budget(tenant, line, project, branch=...)` 把 branch 寫進 `token_usage` row（attribution、不 enforcement）；不擴 cap 維度（呼應 S-6.5 設計斷言 2）。
5. **Y8 frontend** — `lib/branch-context.tsx` + `useBranch()` + sidebar branch picker subordinate to project picker subordinate to line picker subordinate to tenant switcher（4 層）+ project detail page branch list 表格 + `/projects/{id}/branches` 切片頁 + force-push stale banner + customer-x-fork churn banner（呼應 S-6.8 row 9 / row 10）。
6. **Y9 audit + cron** — `audit_log.branch` partial index + `branch_lifecycle_history` join；cron `branch_purge_after_90d` 對 archived ephemeral branch 拆 ref；customer-x-fork lifecycle 變更必雙鏈寫入 acme tenant chain + customer-A chain（呼應 S-3 / S-5 雙鏈、S-6.4 設計斷言 6）。

## S-7 消失用戶回收

> **Skeleton — TBD by future row** (TODO 第 7 勾選)。
> user 離職 / tenant 退訂 / project 封存時的 graceful offboarding：migrate ownership、保留 audit、釋放 quota。
> 預定章節：S-7.1 ownership migration、S-7.2 audit 保留期、S-7.3 quota 釋放。

## S-8 熱點撞牆

> **Skeleton — TBD by future row** (TODO 第 8 勾選)。
> 單 project 打爆 tenant quota → 其他 project 被餓死還是 project 間 DRF 公平分配？
> 預定章節：S-8.1 fairness 模型選擇、S-8.2 throttle vs reject、S-8.3 emergency burst 機制。

## S-9 遺留相容

> **Skeleton — TBD by future row** (TODO 第 9 勾選)。
> 所有 `t-default` 現存資料怎麼對應到新階層（預設 product_line="default" / project="default"）。
> 預定章節：S-9.1 migration 步驟、S-9.2 雙寫期、S-9.3 fallback 行為驗收。

---

## 共用區段

> **Stub — 待 S-2 ～ S-9 完成後彙整**（TODO 末段「ER diagram、權限矩陣、migration 策略」要求）。
> 本段只在 9 個情境章節都成型後落筆，避免在情境未盡時提早收斂出錯誤抽象。

### ER Diagram（占位）

```
                          (待 S-2 ～ S-9 完成後繪製)

  Users ───────┐
               │ N : N
               ▼
        UserTenantMemberships ──── Tenants
                                    │
                                    │ 1 : N
                                    ▼
                                 Projects ─── ProjectMembers ─── Users
                                    │
                                    │ 1 : N
                                    ▼
                                 ProjectShares ──── (guest tenant)
```

完整 ER 含欄位、外鍵、約束、index — 留 Y1 落地時繪製成 mermaid。

### 權限矩陣（占位）

S-1.3 / S-1.4 已給出 secret + project 部分。完整矩陣（涵蓋 audit / quota / workflow_run / artifact / SOP / skill pack / chatops / decision / git_account / ...）— 留 Y2 / Y3 / Y4 落地時逐 endpoint 補。

### Migration 策略（占位）

- **Y1 加 4 表 + 回填 script**（為 `t-default` + 既有 5 tenant 各自建 default project + 把 user 加為 contributor）
- **Y4 加 `project_id` 欄位到所有業務表 + NULL 暫時允許 + 1 release 後加 NOT NULL**
- **Y6 workspace 路徑搬遷 + symlink 過渡 + 1 release 後移除**

詳細步驟 + 回滾策略 — 留 Y10 落地時定稿。

---

## 變更歷史

| 日期 | 對應勾選 | 變更摘要 |
|---|---|---|
| 2026-04-25 | TODO 第 1 勾選（單租戶多用戶） | 初次落地。完整 S-1 章節（10 子節 + 6-persona 矩陣 + secret/project RBAC 表 + Acme 7 步落地時間軸 + 8 邊界 + 5 open questions + 對照表）；S-2 ～ S-9 留 skeleton；共用區段（ER / 權限矩陣 / migration）留 stub。 |
| 2026-04-25 | TODO 第 2 勾選（多租戶單用戶） | S-2 章節展開（10 子節 + 5-persona Bridge MSP + Maya 7 步 onboarding + middleware 升級偽碼 + resolve_role 二維解析 + audit hygiene 4 種查詢 + schema 增量 5 欄 + 8 邊界 + 5 open questions + 16 行對照表）；S-1 row 標完成（2026-04-25）；S-3 ～ S-9 維持 skeleton；共用區段不收尾。 |
| 2026-04-25 | TODO 第 3 勾選（跨租戶協作） | S-3 章節展開（9 子節 + 6-persona host/guest 雙視角 Acme/Cobalt + Joint Firmware 9 步 onboarding + resolve_role 三維合成偽碼 + audit 雙鏈寫入對照表 + cross-tenant secret 隔離 6 場景 + schema 增量 2 表 2 欄 + 9 邊界 + 5 open questions + 16 行對照表）；S-2 row 標完成（2026-04-25）；S-4 ～ S-9 維持 skeleton；共用區段仍 stub。|
| 2026-04-25 | TODO 第 4 勾選（多產品線） | S-4 章節展開（10 子節 + 6-persona Acme 三線（IPCam Pam / Doorbell Doris / Intercom Ian + Alice/Bob/Carol 對照組）+ LLM 雙層預算階層偽碼 + git resolver 階層偽碼 + on-call routing 階層偽碼 + SOP/skill_pack 共享範圍 + schema 增量 3 表 5 欄 + Acme 1→3 線 7 步演進時間軸 + 9 邊界 + 5 open questions + 16 行對照表）；S-3 row 標完成（2026-04-25）；S-5 ～ S-9 維持 skeleton；共用區段仍 stub。|
| 2026-04-25 | TODO 第 5 勾選（多專案同產品線） | S-5 章節展開（10 子節 + 6-persona Doorbell 三 project（V1 客戶 A 量產 Quinn / V2 客戶 B POC Rita / V3 內部 R&D Sam + Doris/Carol/Bob 對照組）+ 三層 LLM 預算階層偽碼（tenant ceiling × line budget × project cap）+ customer attribution 模型（customer_accounts 表 + is_internal 互斥 CHECK）+ SOP/skill_pack 三層繼承解析 + inheritance vs clone 雙 mode + lifecycle 狀態機 6 stage 嚴格白名單轉移 + schema 增量 3 表 4 欄（customer_accounts + project_lifecycle_history + sop_overrides + projects 4 欄）+ Doorbell 1→3 project 7 步演進時間軸 + 11 邊界 + 5 open questions + 17 行對照表）；S-4 row 標完成（2026-04-25）；S-6 ～ S-9 維持 skeleton；共用區段仍 stub。|
| 2026-04-25 | TODO 第 6 勾選（多分支同專案） | S-6 章節展開（10 子節 + 4-branch persona 矩陣（main / staging / v2.1-hotfix / customer-x-fork × push policy × reviewer × 工程角色）+ workspace 路徑 nested 模型（tenant/line/project/_branches/_tasks 4 段 + sanitize 規約 + legacy symlink 過渡）+ git worktree 策略偽碼（per-project bare clone × N worktree 共享 object store + ensure_project_bare / provision_branch_worktree / provision_agent_task_worktree 三 fn + PG advisory lock keyed (project, branch)）+ branch lifecycle 狀態機 4 stage（active / frozen / archived / purged）+ long-lived vs ephemeral typed enum + workflow_run × branch attribution（branch / branch_kind / base_branch 3 欄兩階段 NOT NULL）+ schema 增量 2 新表 + 1 表升 durable + 3 表加欄位（project_branches + branch_lifecycle_history + agent_workspaces 升 PG + workflow_runs/artifacts/audit_log 各加 branch）+ Doorbell V1 1→4 branch 7 步演進時間軸（含 release v2.0 / v2.1-hotfix from tag / customer-x-fork 雙鏈 audit / auto-freeze 30d）+ 13 邊界 + 5 open questions + 14 行對照表（含既有 backend/workspace.py module-global state 升 PG 表合格答案 #2 的具體實作路徑））；S-5 row 標完成（2026-04-25）；S-7 ～ S-9 維持 skeleton；共用區段仍 stub。|
| 2026-04-25 | TODO 第 2 勾選（多租戶單用戶） | 完整 S-2 章節（10 子節 + Bridge MSP × Acme/Blossom/Cobalt 5-persona 矩陣 + tenant switcher UX 4 步流程 + middleware 升級偽碼 + RBAC `resolve_role(user, tenant, project)` 二維解析 + audit cross-contamination 4 條 invariant + Y1 新增欄位（`is_super_admin` / `last_active_tenant_id` / `sessions.active_tenant_id` / `impersonation_*` / `is_primary` partial unique index）+ Maya 7 步 onboarding 時間軸 + 8 邊界場景 + 5 open questions + 16 行對照表盤點 Y2/Y3/Y8 缺口）；S-3 ～ S-9 仍留 skeleton；共用區段不動。 |
