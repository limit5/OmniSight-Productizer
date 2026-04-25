# Priority Y — Multi-user × Multi-project 情境盤點

> 文件起點：2026-04-25  
> 對應 TODO：`Y0. Multi-user × Multi-project 情境盤點 + 架構文件 (#276)`  
> 撰寫策略：每個 TODO 子勾選對應一個 `S-x` 情境章節。本提交完成 **S-1（單租戶多用戶）**，其餘 S-2～S-9 章節僅留「Skeleton — TBD by future row」標記，等該勾選排到時再展開。共用區段（ER diagram / 權限矩陣 / migration 策略）在所有情境章節成型後彙整。

---

## 文件結構導航

| 章節 | TODO 對應 | 狀態 |
|---|---|---|
| [S-1 單租戶多用戶](#s-1-單租戶多用戶) | `[x]` 第 1 勾選（本 row） | **本次完成** |
| [S-2 多租戶單用戶](#s-2-多租戶單用戶) | `[ ]` 第 2 勾選 | Skeleton |
| [S-3 跨租戶協作](#s-3-跨租戶協作) | `[ ]` 第 3 勾選 | Skeleton |
| [S-4 多產品線](#s-4-多產品線) | `[ ]` 第 4 勾選 | Skeleton |
| [S-5 多專案同產品線](#s-5-多專案同產品線) | `[ ]` 第 5 勾選 | Skeleton |
| [S-6 多分支同專案](#s-6-多分支同專案) | `[ ]` 第 6 勾選 | Skeleton |
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

> **Skeleton — TBD by future row** (TODO 第 2 勾選)。
> MSP / 顧問同時服務三家客戶，需要在 UI 切換 tenant，API 自動 scope 當前 tenant。
> I7 已有 `X-Tenant-Id` middleware，但沒 membership 後端支撐。
> 預定章節：S-2.1 persona、S-2.2 tenant switcher UX、S-2.3 audit 防混淆、S-2.4 schema 衝擊。

## S-3 跨租戶協作

> **Skeleton — TBD by future row** (TODO 第 3 勾選)。
> tenant A 邀請 tenant B 的 user 作為 guest 看他們某個 project（唯讀 / 可評論），但不能看 tenant A 其他 project。
> 預定章節：S-3.1 share 模型、S-3.2 guest 的 RBAC fence、S-3.3 audit 在 host + guest 雙鏈寫入、S-3.4 cross-tenant secret 隔離。

## S-4 多產品線

> **Skeleton — TBD by future row** (TODO 第 4 勾選)。
> 一個 tenant 下「相機 IPCam / 門鈴 Doorbell / 對講機 Intercom」三條獨立產品線，各自有 LLM 預算 / 各自有 git 整合目標 / 各自有 on-call。
> 預定章節：S-4.1 product_line 列舉與擴充、S-4.2 per-product-line budget override、S-4.3 on-call routing。

## S-5 多專案同產品線

> **Skeleton — TBD by future row** (TODO 第 5 勾選)。
> Doorbell 下「V1 客戶 A 量產」「V2 客戶 B POC」「V3 內部 R&D」三專案，分開計費但共用 Doorbell 的 SOP / skill pack。
> 預定章節：S-5.1 SOP 繼承層級、S-5.2 計費分流、S-5.3 skill pack 共用 vs override。

## S-6 多分支同專案

> **Skeleton — TBD by future row** (TODO 第 6 勾選)。
> 一個專案下 `main / staging / v2.1-hotfix / customer-x-fork` 四 branch 並行開發，workspace 要能同時保有。
> 預定章節：S-6.1 workspace 路徑模型、S-6.2 git worktree 策略、S-6.3 branch-level GC。

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
