# Priority Y — Multi-user × Multi-project 情境盤點

> 文件起點：2026-04-25  
> 對應 TODO：`Y0. Multi-user × Multi-project 情境盤點 + 架構文件 (#276)`  
> 撰寫策略：每個 TODO 子勾選對應一個 `S-x` 情境章節。本提交完成 **S-1（單租戶多用戶）**，其餘 S-2～S-9 章節僅留「Skeleton — TBD by future row」標記，等該勾選排到時再展開。共用區段（ER diagram / 權限矩陣 / migration 策略）在所有情境章節成型後彙整。

---

## 文件結構導航

| 章節 | TODO 對應 | 狀態 |
|---|---|---|
| [S-1 單租戶多用戶](#s-1-單租戶多用戶) | `[x]` 第 1 勾選 | 完成（2026-04-25） |
| [S-2 多租戶單用戶](#s-2-多租戶單用戶) | `[x]` 第 2 勾選（本 row） | **本次完成** |
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
| 2026-04-25 | TODO 第 2 勾選（多租戶單用戶） | 完整 S-2 章節（10 子節 + Bridge MSP × Acme/Blossom/Cobalt 5-persona 矩陣 + tenant switcher UX 4 步流程 + middleware 升級偽碼 + RBAC `resolve_role(user, tenant, project)` 二維解析 + audit cross-contamination 4 條 invariant + Y1 新增欄位（`is_super_admin` / `last_active_tenant_id` / `sessions.active_tenant_id` / `impersonation_*` / `is_primary` partial unique index）+ Maya 7 步 onboarding 時間軸 + 8 邊界場景 + 5 open questions + 16 行對照表盤點 Y2/Y3/Y8 缺口）；S-3 ～ S-9 仍留 skeleton；共用區段不動。 |
