---
audience: internal
---

# BS Installer — Threat Model（sidecar privilege / docker-socket-proxy / sha256 verify chain / air-gap mode）

> **Status**: Threat model frozen (2026-04-27) — pre-coding spec for Priority BS（BS.0.2 row）。本文件凍結 **`omnisight-installer` sidecar** 的安全邊界決策：(1) sidecar 容器的 privilege 最小化模型、(2) docker socket 不裸接而走 docker-socket-proxy 的具體 ACL 白名單、(3) install artifact 走「vendor URL → checksum file → tar payload」三層 sha256 verify 鏈、(4) air-gap mode（封閉網路）的 catalog feed / sidecar / sha256 三項規格。BS.4 / BS.7 任何 sidecar 實作偏離本文件須先回頭改本文件並在 git log 留下 link。
>
> **Scope**: 本文件是「**威脅模型 + 控制 + 為什麼**」層；具體實作在 `Dockerfile.installer`（BS.4.1）、`docker-compose.yml` 的 `omnisight-installer` service block（BS.4.6）、`installer/methods/*.py`（BS.4.3 / BS.7.8）、`backend/routers/installer.py`（BS.2.2）。本文件**不**涵蓋：catalog 三層 source 模型 / sidecar long-poll protocol semantics（在 `docs/design/bs-bootstrap-vertical-aware.md` §3 / §4，BS.0.1）、R 系列風險登記（在 TODO.md，BS.0.3）、PEP gateway 既有 R20-A 設計。
>
> **Related**:
> - `docs/design/bs-bootstrap-vertical-aware.md` §4「Sidecar `omnisight-installer` 隔離 install 風暴」（BS.0.1 ADR）
> - `docs/design/bs-bootstrap-vertical-aware.md` §7.1 PG schema（`install_jobs.sha256` / `install_jobs.error_reason` / `install_jobs.protocol_version`）
> - `docs/security/r20-phase0-chat-layer.md`（既有 chat-layer defense-in-depth 模板）
> - `docs/operator/secrets-config.md`（既有 secret_store 鍵管模式 — sidecar 共用）
> - `backend/pep_gateway.py`（R20-A install_intercept action category 在 BS.7.2 落地、與 sidecar job 排程綁）
> - CLAUDE.md Safety Rules（API key 不入 source、`test_assets/` :ro 邊界、Gerrit dual-sign gate）
> - `Dockerfile.backend`（既有 backend 容器的 non-root + cap-drop 範式 — sidecar 同款再緊）

---

## 1. TL;DR — 四項決策 + 可驗收後果

1. **Sidecar 跑 non-root + `--cap-drop=ALL` + `--security-opt=no-new-privileges` + read-only rootfs + `--storage-opt size=10G` + `--memory=2G --cpus=2` + `--pids-limit=512`**：sidecar 自身被 vendor installer 接管（command injection / supply-chain compromise）也只能在容器內以 uid `10001` 跑、不能 newuidmap、不能 mount 新檔案系統、不能寫除 `/var/lib/omnisight/toolchains/` 之外的任何路徑、不能 fork-bomb。**結果**：blast radius 嚴格限制在 sidecar container 內 + 兩個指定 bind-mount 內。
2. **Docker socket 走 `tecnativa/docker-socket-proxy:0.1.2` 並 envvar 白名單只開 `IMAGES=1` / `INFO=1`，全部 0 其他 verb**（含 CONTAINERS/EXEC/POST/...）：sidecar 即使被接管也只能 `docker pull` + `docker images` + `docker info` 三類唯讀動作，不能 `docker run` / `docker exec` / `docker rm` 主機任何容器、不能 mount host filesystem、不能 escape。**結果**：docker socket 從「root-equivalent」降到「image registry 唯讀 client」。
3. **三層 sha256 verify 鏈：vendor URL → 信任路徑取得 checksum file（HTTPS + GPG signature OR 訂閱簽章）→ tar payload 用 checksum file 驗 → tar 內每檔再驗（manifest 模式）**。Catalog entry 必填 `sha256`（top-level artifact）和可選 `sha256_manifest`（多檔 tarball）；缺 sha256 的 entry 預設 install method 退回 `noop` + UI hard-warning「無法驗證」。**結果**：vendor URL 被劫持、CDN 被注入、tarball 任一 byte 被改都會在 install 前 fail-closed。
4. **Air-gap mode（`OMNISIGHT_INSTALLER_AIRGAP=1`）**：sidecar 啟動時加 `--network=none`、catalog feed URL 強制 `file://` schema、checksum file 走 operator 預備的 `/var/lib/omnisight/airgap/checksums/*.sig`、`docker pull` 換成 `docker load -i tarball`、所有 vendor URL fetch 路徑被 hard-disabled。**結果**：主機可以完全沒有 outbound network、operator 自己負責把 install bundle 與 GPG 簽過的 checksum 帶進來。

---

## 2. 背景 — 為什麼 sidecar 需要獨立 threat model

`omnisight-installer` 是**第一個**主動執行 vendor-supplied 程式碼的 OmniSight 子系統：

- `docker_pull` install method 從第三方 registry（Docker Hub / NXP / Qualcomm / vendor private） pull 5 GB 的 SDK image。**信任邊界**：第三方 registry。
- `shell_script` install method 從 vendor URL 下 `install.sh` 並 `bash` 執行。**信任邊界**：vendor URL + 該腳本內部呼叫的所有命令（vendor 自己 fetch nested binaries、`apt-get install`、`pip install` 都常見）。
- `vendor_installer` install method（如 NXP MCUXpresso 的 `.run` self-extracting installer、Qt online installer GUI bypass 模式）執行 vendor binary。**信任邊界**：vendor binary。

對比已有的 OmniSight runtime：

| 子系統 | 主動執行外部程式碼？ | 攻擊面 |
|---|---|---|
| Backend HTTP serving | 否（FastAPI app code） | LLM injection、SQLi、XSS — 已由 R20-A + ORM + CSP 處理 |
| Workspace sandbox | 是（pytest / build commands） | 但 PEP gateway 已 HOLD + sandbox bind-mount 邊界 — `docs/operations/sandbox.md` |
| **`omnisight-installer`** | **是**（vendor `.sh` / `.run` / `docker pull`） | **本文件處理** |

這是新增攻擊面。即使 PEP gateway 已 HOLD（operator 必須 approve），approve 之後 vendor 程式碼仍會跑——所以 PEP 是**授權**邊界、不是**隔離**邊界。本 threat model 的工作是在 PEP 的下游再加一層**容器層**隔離。

### 2.1 與 BS.0.1 ADR §4 對照表（重複的內容不重寫）

ADR §4.1 已列出「為什麼不在 backend 內裝」的對照表（`docker pull` 5 GB 卡 backend / vendor `bash install.sh` runaway / command injection / Python 依賴衝突），結論是**必須**隔離到 sidecar。本文件接續討論「**隔離到 sidecar 之後 sidecar 自身要怎麼夠安全**」——前者是「為什麼要 sidecar」、後者是「sidecar 怎麼裝甲」。

---

## 3. 威脅清單（STRIDE 過濾 + 場景化）

| ID | 威脅類別（STRIDE） | 場景 | 影響 | 相關控制 |
|---|---|---|---|---|
| **T1** | Tampering / Elevation | Vendor `install.sh` 含 `curl | bash`、payload 被 CDN MitM 注入後門 | sidecar 跑了後門、若 sidecar 為 root → 寫 host filesystem、escape | §4 sidecar privilege、§5 sha256 verify chain |
| **T2** | Elevation | Sidecar 被接管後試圖 `docker run --privileged` 開新容器 | 開啟主機 root shell | §4.4 cap-drop、§4.6 docker-socket-proxy 白名單 |
| **T3** | Information Disclosure | Sidecar 容器內 `cat /etc/shadow` / `cat /run/secrets/*` | 偷 host 機密 | §4.2 read-only rootfs、§4.3 non-root user、§4.5 host bind 邊界 |
| **T4** | Denial of Service | Vendor installer 寫 `while true: cp ...` 把磁碟塞爆 / fork-bomb | 主機 disk full / pid exhaustion | §4.7 storage-opt + memory + pids-limit |
| **T5** | Tampering | Catalog entry 被 operator 自己改成「下載 attacker 控制的 URL」 | 引入惡意 toolchain | §5.1 sha256 必填 + §5.4 missing-sha256 退化 + Multi-tenancy admin role + audit |
| **T6** | Spoofing / Tampering | Vendor URL 被 DNS hijack / TLS strip → 假 SDK | 上一條的中間人變體 | §5.2 GPG-signed checksum file + §5.3 三層驗證 |
| **T7** | Information Disclosure | Catalog feed 訂閱第三方 → feed 自己被攻陷推 malicious entries | 整個 tenant 中招 | §5.5 feed 簽章驗證（subscription 第四層 forward-compat） |
| **T8** | Elevation | Sidecar 透過 docker socket 看到 backend 容器的環境變數 / secret 掛載 | 偷 OmniSight DB credential / LLM API key | §4.6 docker-socket-proxy 白名單 — 0 verb 給 CONTAINERS 類別 |
| **T9** | Denial of Service / Tampering | Air-gap 環境誤連外網（或 sidecar 還是會去 fetch） | 機密外洩 / 引入不該有的 binary | §6 air-gap mode `--network=none` |
| **T10** | Repudiation | Sidecar 行為無法回溯 | 不知道是哪個 vendor URL 失敗、不能 forensic | `install_jobs` 表的 `log_tail` + audit_log + sha256 result（§7） |
| **T11** | Elevation | Job 被 cancel 後 sidecar 還在背景跑 vendor binary | 資源未釋放 / 已下載 binary 滯留 | §4.8 job-scoped subprocess + reaping |
| **T12** | Denial of Service | sidecar 自己 OOM killed mid-install → 髒狀態（半解開的 tarball） | 下次 retry 從髒狀態開始、結果不可重現 | §4.9 install path scratch dir + atomic rename + idempotency_key |

---

## 4. 控制 1 — Sidecar privilege model（最小化容器權限）

### 4.1 為什麼不能跑 root

容器跑 root（uid=0）即使有 `--cap-drop=ALL`，仍會有以下 escape vector：

- **`/proc/sys/*`** 部分 sysctl 預設 namespace-shared（雖 unprivileged container 已被 docker default seccomp 擋大半，但 root 在容器內可寫某些 `/proc/<pid>/oom_score_adj` 影響 host 行為）。
- **Image layer 寫入**：root 可隨意改 `/etc/passwd` / 安裝後門 PATH binary，雖然 read-only rootfs（§4.2）後不可寫、但若有任何 misconfig（dev 階段 mount 出 `/etc` 為 rw）會立刻致命。
- **Future kernel CVE**：歷史上 runc / containerd CVE（CVE-2019-5736 / CVE-2022-0492 / CVE-2024-21626）對 root container 影響遠大於 non-root。
- **Bind-mount 寫入**：sidecar **必須** bind-mount `/var/lib/omnisight/toolchains/`（裝完 SDK 要存的位置）— 若以 root 身分寫、寫進來的檔案 owner 是 host root，後續 backend container（uid `10000`）讀取會 permission denied，需要 chmod。Non-root with explicit uid 一致才乾淨。

**規則**：sidecar Dockerfile 必須 `USER 10001:10001`（uid/gid 與 host `/var/lib/omnisight/toolchains/` 持有者一致；`Dockerfile.installer` 在 BS.4.1 落地）。`docker-compose.yml` 不允許 `user: root` override（CI lint 檢查）。

### 4.2 Read-only rootfs

```yaml
# docker-compose.yml omnisight-installer service block (BS.4.6)
services:
  omnisight-installer:
    read_only: true
    tmpfs:
      - /tmp:size=512M,mode=1777,noexec,nosuid,nodev
      - /run:size=64M,mode=755,noexec,nosuid,nodev
```

`read_only: true` 把 container rootfs 設為唯讀。`/tmp` 用 tmpfs（記憶體 backed、reboot 即清）、限 512 MB（防 vendor 在 `/tmp` 解 5 GB tarball 把記憶體吃爆）、`noexec` 額外阻止「在 /tmp 解出 binary 然後 exec」這條常見後門路徑。

**為什麼 `/tmp` 不能寫 noexec 也行**：vendor installer **常**把 self-extracting binary 解到 `/tmp` 然後執行（`make install`、`./install.sh`）— 這是合法行為。所以 `/tmp` 必須允許 exec？**不**，sidecar 把 install scratch path 顯式設在 `/var/lib/omnisight/toolchains/<entry-id>/scratch/`（bind-mount，noexec **不**設）；vendor installer 的 working-directory cwd 從 `/tmp` 改到 scratch path（`installer/methods/shell_script.py` 的 `subprocess.run(..., cwd=scratch_dir)`）。`/tmp` 完全 noexec 是刻意的——意思是「沒繞 scratch dir 的程式不准執行」。

### 4.3 Non-root + namespaced uid

Dockerfile：

```dockerfile
# Dockerfile.installer (BS.4.1)
FROM python:3.12-slim AS base
RUN groupadd -g 10001 installer && \
    useradd -u 10001 -g 10001 -m -d /home/installer -s /usr/sbin/nologin installer && \
    mkdir -p /var/lib/omnisight/toolchains && \
    chown -R installer:installer /var/lib/omnisight/toolchains
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/python3", "-m", "installer.main"]
```

Host bind-mount 必須 chown 給 `10001:10001`（compose `volumes:` block 加 `:Z` SELinux 標籤、`uid=10001,gid=10001` mount option for non-systemd hosts）。

### 4.4 Capability drop（完整白名單）

```yaml
services:
  omnisight-installer:
    cap_drop:
      - ALL
    cap_add: []  # 空白清單明示沒加任何 cap
    security_opt:
      - no-new-privileges:true
      - seccomp=default
      - apparmor=docker-default
```

**為什麼 `cap_add` 留空清單而不省略**：明示意圖。CI lint 對 `omnisight-installer` 強制 `cap_add: []`（不能省略），把「忘記加 cap_drop」與「明示不加 cap_add」兩種狀態分開。

vendor installer 偶有腳本想 `setcap` / `chown -R` host 路徑——這些動作會 fail（CAP_CHOWN / CAP_SETFCAP 沒給）。**這正是設計目的**——若 vendor 真的需要 setcap，operator 應該意識到並走 issue tracker review，而不是放手讓它跑。Sidecar log 把 EPERM 訊息原封不動寫進 `install_jobs.log_tail`，operator 從 UI 看到具體哪個 syscall 被擋。

### 4.5 Bind mount 邊界

| Host path | Container mount | rw / ro | 用途 |
|---|---|---|---|
| `/var/lib/omnisight/toolchains/` | `/var/lib/omnisight/toolchains/` | rw | 唯一寫入路徑（裝好的 SDK 落這） |
| `/var/lib/omnisight/airgap/` | `/var/lib/omnisight/airgap/` | ro | air-gap mode 下 operator 預備的 install bundle + checksum |
| (docker socket via proxy) | `/var/run/docker.sock` | (see §4.6) | docker pull 用 |
| **不掛**：`/`, `/etc`, `/var/run` (除 docker socket 走 proxy)、`/proc/1/root`、host `~/.ssh`、host `/var/lib/secrets` | — | — | sidecar 不該看到 |

CI lint 對 `docker-compose.yml` 跑 yaml schema 檢查—— `omnisight-installer` 的 `volumes:` 只允許上述兩個 bind path（其餘 fail）。

### 4.6 Docker socket 走 proxy（核心 cap 控制）

裸接 `/var/run/docker.sock` 等同於 root（`docker run -v /var/run/docker.sock:/var/run/docker.sock -v /:/host alpine sh` 即可 escape）。BS 不接受裸接。

實作：採 `tecnativa/docker-socket-proxy:0.1.2`（pinned image，**不**用 `:latest`），envvar 白名單**精確**到只開 image 唯讀 verb：

```yaml
services:
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy:0.1.2
    environment:
      # 只開 IMAGES + INFO（讓 sidecar 能 pull 與 healthcheck）
      IMAGES: 1
      INFO: 1
      # 其他全部 0（明示）
      AUTH: 0
      BUILD: 0
      COMMIT: 0
      CONFIGS: 0
      CONTAINERS: 0
      DISTRIBUTION: 0
      EVENTS: 0
      EXEC: 0
      GRPC: 0
      NETWORKS: 0
      NODES: 0
      PING: 0
      PLUGINS: 0
      POST: 0
      SECRETS: 0
      SERVICES: 0
      SESSION: 0
      SWARM: 0
      SYSTEM: 0
      TASKS: 0
      VERSION: 0
      VOLUMES: 0
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    restart: unless-stopped
    networks:
      - omnisight-internal

  omnisight-installer:
    # 不直接 mount /var/run/docker.sock；改透過 proxy network 連
    environment:
      DOCKER_HOST: tcp://docker-socket-proxy:2375
    networks:
      - omnisight-internal
```

`POST: 0` 是核心——它把 `docker run` / `docker exec` / `docker rm` / `docker stop` / `docker volume create` 全擋掉（這些都是 POST verb）。`IMAGES: 1` 開 GET `/images/*` 跟 `POST /images/create`（pull 用，proxy 內部把這條 POST 視為 IMAGES 而非 generic POST）。

**為什麼把 `/var/run/docker.sock` 給 proxy 走 `:ro`**：proxy 自己只是流量過濾器，不需要寫 socket file（socket 是 unix-domain，`:ro` 在 proxy 邊只讓它讀 file inode、socket 操作仍由 daemon 處理）。**重點**：`:ro` 防 proxy 自己被入侵時改 socket file owner / 把它替換成自己的 server——此邊界也是 defense-in-depth。

ACL 矩陣（明示給 BS.4 / BS.7 reviewer 校對）：

| Docker API | Verb | Allowed via proxy? | 為什麼 |
|---|---|---|---|
| `GET /images/json` (list) | GET | ✅ | sidecar 需要看 image 已存在不 pull |
| `POST /images/create` (pull) | POST→IMAGES | ✅ | docker pull 主操作 |
| `GET /info` | GET | ✅ | health probe |
| `GET /version` | GET | ❌ | **不需要**——版本對 sidecar 行為無影響 |
| `POST /containers/create` | POST→CONTAINERS | ❌ | sidecar 不該起新容器 |
| `POST /containers/{id}/exec` | POST→EXEC | ❌ | sidecar 不該對任何容器 exec |
| `GET /containers/json` | GET→CONTAINERS | ❌ | sidecar 不該知道 host 上有哪些容器 |
| `POST /networks/create` | POST→NETWORKS | ❌ | sidecar 不該動 network |
| `POST /volumes/create` | POST→VOLUMES | ❌ | sidecar 不該動 volume（host bind 已經在 compose 給好） |
| `GET /events` | GET→EVENTS | ❌ | 太雜訊 |

**ACL drift guard**：CI 檢查 `docker-compose.yml` 的 `docker-socket-proxy.environment` block 與 `docs/security/bs-installer-threat-model.md` §4.6 的 envvar 白名單完全一致（key set + value 都 match）— 任一新增 `1` 必須有對應 PR 改本文件 + Gerrit dual-sign（含 security-bot + non-ai-reviewer +2，per CLAUDE.md Safety Rules）。

### 4.7 資源限制

```yaml
services:
  omnisight-installer:
    deploy:
      resources:
        limits:
          memory: 2G
          cpus: '2.0'
          pids: 512
    storage_opt:
      size: 10G
```

| Limit | 值 | 為什麼 |
|---|---|---|
| `memory: 2G` | 2 GB | 大多數 SDK install 200 MB-1 GB peak；2 GB 留 buffer。Vendor `bash` 寫不出記憶體洩漏要超過這個量 |
| `cpus: 2.0` | 2 cores | install 是 IO-bound（download + tar 解開），CPU 2 core 足夠；防 vendor 跑 `make -j$(nproc)` 把 host 全部核心吃爆 |
| `pids: 512` | 512 | fork-bomb 防護（`while true: { for i in {1..1000}: subprocess.spawn() }` 撞牆 512 直接 EAGAIN） |
| `storage_opt.size: 10G` | 10 GB | rootfs + `/tmp` tmpfs 上限；超過直接 ENOSPC、不會把 host disk 塞爆 |

**為什麼不用 cgroup v2 直接限制**：docker-compose 走的是 docker daemon 的 cgroup translation，背後仍是 cgroup v2（如果 host kernel 支援）。明示在 compose 寫的好處是**可審查、可 diff、可 lint**——operator 看 compose file 立刻知道 sidecar 上限是多少。

### 4.8 Job-scoped subprocess + reaping

`installer/methods/shell_script.py`（BS.4.3）：

```python
import subprocess, signal, os
def install(job, progress_cb):
    proc = subprocess.Popen(
        ["bash", "install.sh"],
        cwd=scratch_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # 重點 1: 開新 process group → 收到 cancel 時可以 killpg
        preexec_fn=os.setsid,
        # 重點 2: 強制 close-on-exec 所有不該繼承的 fd
        pass_fds=(),
    )
    job.subprocess_pgid = os.getpgid(proc.pid)
    # ... long poll loop with cancel check ...
    if cancelled:
        os.killpg(job.subprocess_pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(job.subprocess_pgid, signal.SIGKILL)
```

`os.setsid` 把 vendor `bash` + 它 spawn 的所有 child 放在同一個 process group；cancel 時 `killpg` 一起送 SIGTERM、10s 後升級 SIGKILL。**這是 T11（cancel 後仍跑）的對策**。

### 4.9 Atomic install path（T12 對策）

vendor installer 寫 `/var/lib/omnisight/toolchains/<entry-id>/` 必須走兩步：

1. 解到 scratch path：`/var/lib/omnisight/toolchains/<entry-id>/scratch-<job-uuid>/`
2. install method 終局成功時：`os.replace(scratch_path, final_path)`（atomic rename within same fs）

中途任何 cancel / OOM / kill → scratch dir 留著但不被 visible（catalog UI 只列 final path 存在的 entries）。Sidecar 啟動時 cleanup task 掃 `scratch-*` 老目錄（>24h）、確認無 alive job 引用後刪除——這條 cleanup 走 `installer/cleanup.py`（BS.4 follow-up，初版可省，留 task）。

`install_jobs.idempotency_key` 對應 frontend 帶上來的 UUID（BS.0.1 ADR §4.4），同 key 重發回上次 ID，避免雙擊建兩個 job 的時候 scratch dir 撞檔。

---

## 5. 控制 2 — sha256 verify 鏈（T1 / T5 / T6 對策）

### 5.1 PG 端必填欄位

`catalog_entries.sha256` 在 BS.0.1 ADR §7.1 是 `TEXT NULL`（為了 `install_method='noop'` 不需要 sha256）。本 threat model 補充規則：

```
IF install_method IN ('docker_pull', 'shell_script', 'vendor_installer')
THEN sha256 IS NOT NULL                # CHECK constraint at DB level (alembic 0051)
```

**Alembic 落地**：BS.1.1 的 `catalog_entries` migration 加：

```sql
ALTER TABLE catalog_entries
ADD CONSTRAINT catalog_entries_sha256_required_for_destructive
CHECK (
  install_method = 'noop'
  OR sha256 IS NOT NULL AND length(sha256) = 64 AND sha256 ~* '^[a-f0-9]{64}$'
);
```

UI 端（BS.8.6 `custom-entry-form.tsx`）對 admin 的「新增 entry」form 對 install_method 非 noop 時 sha256 欄位 required。

### 5.2 三層驗證 chain

對任意非 noop install：

```
Layer 1 — vendor URL TLS:        HTTPS only (sidecar 拒絕 http://); cert 驗證走 system CA bundle
Layer 2 — checksum file 取得:    在 catalog entry 內預存 sha256 (或 sha256_url + sha256_url_sig 三件組)
Layer 3 — payload 驗證:         sidecar 下載 payload 後計算 sha256(payload) == catalog.sha256?
                                  (若是 tarball 且有 manifest) 解開後對 manifest 內每檔再驗
```

**為什麼三層而不是一層**：

- 只信 catalog.sha256（單層）：catalog 自己被改（operator 帳號被偷、PG 被攻陷、subscription feed 被劫）→ sha256 換成 attacker 提供的值 → 任何 payload 都會 verify pass。
- 加 vendor checksum file 簽章驗證（雙層）：catalog.sha256_url 指向 vendor 的 SHA256SUMS 檔（如 `https://vendor.example.com/SHA256SUMS`）+ catalog.sha256_url_sig 指向 vendor GPG signature；sidecar 走 `gpg --verify` 對 SHA256SUMS 驗簽，再從 SHA256SUMS 取出對應 file 的 sha256，再對 payload 驗——攻擊者要同時改 catalog **與** 攻陷 vendor 簽章 key 才能繞。
- Tarball 內 manifest（第三層）：對 multi-file artifact（NXP MCUXpresso 的 `.tar.gz` 內含 100 個 `.h` / `.so`），抽出 `manifest.sha256` 列出每檔 hash，sidecar 解 tar 後逐檔驗——對「攻擊者重新打包 tarball、外層 sha256 重算」的進階場景仍能擋（除非 attacker 連 manifest 也重簽，但 manifest 在 tar 裡已被外層 sha256 cover）。

### 5.3 簽章信任鏈（forward-compat）

第二層的 GPG 驗證走的 trust store：

```
/var/lib/omnisight/airgap/keyring/
├── omnisight-upstream.asc       # 必有，shipped catalog 簽章 key
├── nxp-vendor.asc               # 可選，vendor 提供的 release key
├── qualcomm-vendor.asc          # 可選
└── ...
```

Sidecar 啟動時 `gpg --import` 這些 key 進 isolated keyring（**不**用 host 的 `~/.gnupg`，避免污染）；catalog entry 的 `sha256_url_sig_key` 欄位指出這個 entry 的簽章該由哪一把 key 驗（fingerprint match）。

**為什麼 GPG 而不是其他簽章機制**：

- vendor 業界普遍提供 GPG-signed checksum file（apt repos / npm release / PyPI / Docker Content Trust 都基於 GPG 或類似格式的簽章）。
- minisign / signify 較新但 vendor 普及度低，本 ADR 不導入（forward-compat：未來新增 `sig_format` enum 即可擴充，但 v1 只支援 GPG）。
- 不走 X.509 / PKI 是因為 PKI 對「個別 vendor 一把 key」這種場景過重。

### 5.4 缺 sha256 的 entry 退化（防 catalog drift）

若 admin 透過 PATCH 把已有 entry 的 sha256 砍掉（極端情況、不該發生但 defense-in-depth），install_jobs 排程時 sidecar 第一件事檢查 sha256：

```python
# installer/main.py poll loop
def claim_job(job):
    entry = backend.get_entry(job.entry_id)
    if entry.install_method != 'noop' and not entry.sha256:
        backend.report_result(job.id, state='failed',
            error_reason='catalog_entry_missing_sha256')
        return  # 不執行 install
```

UI（catalog-tab）對 missing-sha256 + non-noop entry 顯示紅色 hard-warning「verification disabled — install blocked」，install button 永遠 disabled（即使 admin）。

### 5.5 catalog feed subscription 簽章（forward-compat hooks）

BS.0.1 ADR §3.4 預留 `source='subscription'` 第四層；本 threat model 對 subscription feed 額外要求：

- feed payload 必須帶 detached GPG signature（`SHA256SUMS.sig`）。
- subscription 表（`catalog_subscriptions`）的 `auth_secret_ref` 欄位指向 `secret_store` 的 key、value 是 GPG public key 的 fingerprint。
- 同步任務每次 fetch 都 verify；fail 不寫 PG、log audit + admin SSE 通知。

具體實作 in BS+1 epic（subscription 第四層落地時）；本 ADR 規格只凍結「**訂閱 feed 必有簽章驗證**」這條原則。

---

## 6. 控制 3 — Air-gap mode（T9 對策）

### 6.1 觸發條件

operator 透過環境變數啟動 air-gap mode：

```yaml
# docker-compose.yml override (operator-managed)
services:
  omnisight-installer:
    environment:
      OMNISIGHT_INSTALLER_AIRGAP: "1"
    network_mode: none      # docker-compose 把 sidecar 從所有 network 拔掉
    volumes:
      - /var/lib/omnisight/airgap:/var/lib/omnisight/airgap:ro
```

### 6.2 對 sidecar 行為的硬約束

| 路徑 | 非 air-gap | air-gap mode |
|---|---|---|
| Catalog feed URL schema | `https://` 或 `file://` | **僅** `file://` |
| Vendor URL fetch | HTTPS GET | hard-disabled（程式碼層面 raise `AirgapViolation`） |
| `docker pull` | proxy 走 `IMAGES=1` | 程式碼層面 raise `AirgapViolation`，改走 `docker load -i /var/lib/omnisight/airgap/<entry-id>.tar` |
| GPG key fetch | optional from vendor | **僅** 從 `/var/lib/omnisight/airgap/keyring/` |
| sha256 checksum file 取得 | HTTPS or file | **僅** `/var/lib/omnisight/airgap/checksums/<entry-id>.sha256.sig` |
| Sidecar `--network=none`？ | no（需要 docker-socket-proxy 內網） | **yes** |

`installer/main.py`（BS.4.2）開頭 hard-check：

```python
AIRGAP = os.environ.get("OMNISIGHT_INSTALLER_AIRGAP") == "1"

def fetch_url(url: str):
    if AIRGAP and not url.startswith("file://"):
        raise AirgapViolation(f"non-file URL in air-gap mode: {url}")
    if not AIRGAP and not url.startswith("https://"):
        raise InsecureURL(f"http URL not allowed: {url}")
    # ... fetch ...
```

backend `/installer/jobs` POST handler 也檢查 entry 的 `install_url` schema、reject 不合 air-gap 的 entry——這條 fail-fast 在 backend 而非 sidecar，避免 sidecar 拿到 job 後才 fail。

### 6.3 Air-gap 對 docker-socket-proxy 的影響

air-gap mode 下 sidecar `--network=none`、無法連 `docker-socket-proxy` 的內網 `tcp://docker-socket-proxy:2375`。所以 air-gap 模式下：

- `docker_pull` install method 自動 redirect 到 `docker_load`（內部用 unix-socket 直接 `subprocess.run(["docker", "load", "-i", tar_path])`）。
- 仍 mount `/var/run/docker.sock`？**不**——air-gap mode 下 sidecar 完全不接 docker daemon socket（`docker load` 需要 socket，所以 air-gap mode **不**支援 `docker_pull` / `docker_load` 路線；改 `vendor_installer` 路線）。
- 等價說法：**air-gap 模式 = 沒有 docker pull 也沒有 docker load**；想要 vendor docker image 在 air-gap 環境跑，operator 需自己預先 `docker import < <(cat tarball.tar)` 進 host 然後 catalog entry 走 `noop` install method、`metadata.expected_image_present=true`（sidecar `docker images | grep` 確認 image 已在 host、不 pull）。

**這條限制是刻意的**——air-gap 環境的安全前提是「sidecar 不接任何 daemon socket」；docker daemon 自己有 root 權限、即使走 unix-socket 也是 cap escalation 路徑。BS.0.1 ADR `metadata` JSONB 預留 `expected_image_present` 自由欄位、足以標記 noop 模式的 image 預存需求。

### 6.4 Air-gap mode 的 operator 工作流（FYI 非規格）

operator 在 internet-facing 機器上：

1. 從 vendor 下載 SDK tarball + GPG signature。
2. `gpg --verify SHA256SUMS.sig SHA256SUMS` 確認簽章合法。
3. 把 tarball + SHA256SUMS + SHA256SUMS.sig + vendor public key 拷進 USB / 內網 file share。

operator 在 air-gap 機器上：

1. `cp` 到 `/var/lib/omnisight/airgap/<entry-id>/`、`/var/lib/omnisight/airgap/checksums/<entry-id>.sha256.sig`、`/var/lib/omnisight/airgap/keyring/<vendor>.asc`。
2. 在 OmniSight UI 用 admin 帳號新增 catalog entry，`install_url=file:///var/lib/omnisight/airgap/<entry-id>/payload.tar.gz`、`sha256_url=file:///var/lib/omnisight/airgap/checksums/<entry-id>.sha256.sig`。
3. install job 排程 → sidecar 走 layer 2 / 3 sha256 chain 驗 → 寫進 `/var/lib/omnisight/toolchains/`。

整條 flow 主機可以**完全沒有 outbound network**——這是 air-gap mode 的真正價值。

---

## 7. Audit / Observability（T10 對策）

| 事件 | 寫入位置 | 欄位 |
|---|---|---|
| `install_job.created` | backend `audit_log` | tenant_id / actor_id / entry_id / install_method / sha256_expected |
| `install_job.claimed` | `audit_log` | sidecar_id / job_id / protocol_version |
| `install_job.sha256_verified` | `audit_log` | sha256_actual / sha256_expected / matched: bool / layer (1/2/3) |
| `install_job.failed` | `audit_log` | error_reason (string enum) / log_tail (last 4KB) |
| `sidecar.airgap_violation` | `audit_log` (severity=alert) | url_attempted / job_id |
| `sidecar.docker_socket_denied` | `audit_log` (severity=warn) | api_path / verb |
| `catalog.entry_sha256_changed` | `audit_log` | entry_id / old_sha256 / new_sha256 / changed_by |

`install_jobs.error_reason` 是 string enum（**不**是自由文字）：

```
'sha256_layer1_mismatch' | 'sha256_layer2_signature_invalid' | 'sha256_layer3_manifest_mismatch'
| 'airgap_violation' | 'docker_socket_denied' | 'cap_denied' |
| 'oom_killed' | 'pids_limit' | 'storage_full' |
| 'sidecar_restart_recovered' | 'cancelled_by_operator' |
| 'vendor_installer_exit_code_<N>' | 'catalog_entry_missing_sha256'
| 'protocol_version_unsupported' | 'unknown'
```

固定 enum 讓 backend 可以聚合 metrics（`prom-client install_jobs_total{error_reason="..."}`）、operator 從 dashboard 直接看出哪類 fail 最多。

---

## 8. Drift guard tests（強制 CI gate）

按 SOP Step 4 的 drift-guard 規則 + BS.0.1 ADR §3.5 模板：

| Test 檔 | 檢查內容 | 對應 row |
|---|---|---|
| `backend/tests/test_threat_model_compose_lint.py` | parse `docker-compose.yml`，check `omnisight-installer` user / cap_drop / read_only / storage_opt / 兩個 bind path / 不裸 mount docker.sock | BS.4.6 |
| `backend/tests/test_threat_model_proxy_acl.py` | parse `docker-compose.yml` 的 `docker-socket-proxy.environment`，與本文件 §4.6 envvar 白名單比對（key set + value diff，任一不對 fail） | BS.4.6 |
| `backend/tests/test_threat_model_sha256_required.py` | alembic 0051 升級後，PG schema 內 `catalog_entries_sha256_required_for_destructive` CHECK constraint 存在；嘗試 INSERT non-noop entry 無 sha256 → IntegrityError | BS.1.1 |
| `installer/tests/test_airgap_violation.py` | 設 `OMNISIGHT_INSTALLER_AIRGAP=1`、call `fetch_url('https://example.com')` → raise `AirgapViolation` | BS.4.2 |
| `installer/tests/test_sha256_chain.py` | mock 三層 chain（vendor URL / checksum file / payload）；任一層 mismatch / signature invalid / manifest 缺 → 對應 error_reason | BS.4.7 / BS.7.8 |
| `backend/tests/test_audit_install_events.py` | `POST /installer/jobs` 走完一輪後 `audit_log` 有對應 4 條 entry（created / claimed / sha256_verified / completed） | BS.2.4 |

任一 fail → CI red → BS row 不准打 `[x]`。

---

## 9. 不在本文件範圍 / 後續決策

| 議題 | 在哪解 |
|---|---|
| catalog 三層 source 模型內部細節 | `docs/design/bs-bootstrap-vertical-aware.md` §3 |
| sidecar long-poll protocol semantics（state machine / handshake） | 同上 §4 |
| 8 層動畫 / reduce-motion / battery rule | 同上 §5 / §6 |
| R 系列風險登記新增（R24/R25/R26/R27）| TODO.md（BS.0.3） |
| Multi-tenancy SQL row-level security | Priority I 既有 ADR |
| catalog feed subscription 第四層完整 schema | 預留、本文件 §5.5 只凍結「必有簽章驗證」原則；具體 in BS+1 |
| Sidecar 自身的 supply-chain（`Dockerfile.installer` base image 怎麼信） | 走 `docker scan` + `Snyk` + 既有 `dependency_upgrade_runbook.md`；本文件不重複 |
| backend container 的安全邊界（已 ship） | `docs/operations/sandbox.md` |
| Network policy（Calico / Cilium）對 omnisight-internal network 的隔離 | 不在 single-host docker-compose 範圍；future k8s migration 時再處理 |
| HSM / TPM 簽章 key 保護 | 未來 enterprise edition feature；v1 GPG keyring 在 host filesystem 已可接受（host root 信任邊界） |

---

## 10. Sign-off / 驗收

本 threat model 的接受標準：

- [ ] **§4 sidecar privilege**：`Dockerfile.installer` USER `10001:10001` + `cap_drop: ALL` + `read_only: true` + `storage_opt.size=10G` + `pids: 512`；CI lint pass。
- [ ] **§4.6 docker-socket-proxy 白名單**：`docker-compose.yml` envvar 與本文件 ACL 矩陣一致；`test_threat_model_proxy_acl.py` green。
- [ ] **§5 sha256 chain**：`alembic 0051` CHECK constraint 落地；三層驗證在 `installer/methods/*` 走通；`test_sha256_chain.py` 對 6 個失敗 case green。
- [ ] **§6 air-gap**：`OMNISIGHT_INSTALLER_AIRGAP=1` 啟動時 sidecar `network_mode: none` + 所有 outbound URL hard-disabled；`test_airgap_violation.py` green。
- [ ] **§7 audit**：所有 §7 表內事件實際寫進 `audit_log`；`test_audit_install_events.py` green。
- [ ] **§8 drift guards**：6 個 CI gate 全綠才允許 BS.4 / BS.7 row 打 `[x]`。

凍結時間：2026-04-27（BS.0.2）。任何改動需 update 本文件並在 BS 對應 row 的 commit message 標 `(threat-model amended)`，且因屬安全範疇、Gerrit dual-sign（security-bot +1 + non-ai-reviewer +2）強制 per CLAUDE.md Safety Rules。

---

## 11. 變更紀錄

| 日期 | 改動 | 作者 |
|---|---|---|
| 2026-04-27 | 初版（BS.0.2） — 四項決策定稿（sidecar privilege / docker-socket-proxy ACL / sha256 三層 chain / air-gap mode）+ STRIDE 12 條威脅清單 + 6 個 drift guard CI gate | Agent-row7-self-agent（automated） |
