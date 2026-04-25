# R8. Idempotent Retry — Worktree-Based Reset（覆寫白皮書 §三.2）

> **Status**: Design decision ratified (2026-04-25) — 覆寫 `docs/design/enterprise_watchdog_and_disaster_recovery_architecture.md` §三.2「工作區清理」的 `git clean -fd` + `git checkout .` 指引。
>
> **Scope**: 本文件鎖定「任務重試時如何把 agent 工作區恢復到純淨錨點」這一單一決策；不涵蓋 anchor commit 寫入 CATC、`WorkspaceManager.discard_and_recreate()` 實作、audit trail 欄位、startup orphan scan、整合測試——那些為 R8 後續 rows（task #314 的 sub-bullet）的實作範疇，本文件提供其依據。
>
> **Related**: TODO.md `R8. Idempotent Retry 正規化`（#314）first row；`backend/workspace.py`（既有 WorkspaceManager）；`docs/design/enterprise_watchdog_and_disaster_recovery_architecture.md` §三.2（被覆寫段落）；TODO.md R1 第 `/omnisight rollback [ID]` bullet（任務 #307，已引用本設計）；CLAUDE.md Safety Rules (`NEVER modify files in test_assets/`)。

---

## 1. 決策摘要（TL;DR）

當一個 agent 任務失敗或逾時需要重試時，**不**對既有 worktree 執行 `git clean -fd` + `git checkout .`，而是：

1. 以 `git worktree remove --force` 銷毀當前 worktree，再以 `shutil.rmtree` fallback 確保目錄消失。
2. 從 **anchor commit SHA**（任務開始前寫入 CATC metadata 的純淨錨點）以 `git worktree add <new_path> -b <branch> <anchor_sha>` 建立全新 worktree。
3. 寫入 `audit_log`（`action=retry.worktree_recreated`，包含 `old_worktree_path`、`anchor_sha`、`reason`）形成可稽核紀錄。

從 operator / agent 的視角，retry 後的工作區**等同於**一個剛 provision 過的 worktree：檔案樹 = anchor commit 的內容，git HEAD = `anchor_sha`，分支 = 與 agent 綁定的 `agent/<safe_agent>/<safe_task>`，沒有任何前次嘗試殘留（包含 untracked files、submodule dirty state、`.gitignore` 隱藏的 build artifacts、index.lock 殘骸、chmod 過的檔案）。

---

## 2. 背景與被覆寫的原始設計

白皮書 `enterprise_watchdog_and_disaster_recovery_architecture.md` §三.2「冪等性重試與工作區回滾 (Idempotent Retry)」原句：

> * **工作區清理**：重試前，系統強制執行 `git clean -fd` 與 `git checkout .`。
> * **狀態回滾**：確保工作目錄恢復到任務開始前的「純淨錨點 (Clean Anchor)」，防止髒程式碼干擾下一次嘗試。

此設計在單一 shared checkout 的部署模式下成立，但 OmniSight 的 agent 工作區模型在 2025-Q4 已演進為：

* **每 agent 獨立 git worktree**（`backend/workspace.py::provision`，line 76–245）——每個 agent 走 `git worktree add` 從 main repo 的 HEAD 拉出專屬 branch + 專屬工作目錄 `${REPO}/.agent_workspaces/${safe_agent}/`。
* **Sandbox tier 可能掛 :ro bind-mount**（例如 `test_assets/` 按 CLAUDE.md Safety Rule 被強制 read-only）；`git clean -fd` 在這類目錄上會回傳非零並污染 retry 幾何。
* **Worktrees 共享同一 object store + 可能有 concurrent borrow**（`_shared_parallel` counter 同時追蹤多 agent session）——對 worktree 內 `checkout .` 與 sibling worktree 的 reflog / index 更新可能互相干擾。

在這個背景下沿用白皮書原句會產生下述問題。

---

## 3. 為何拒絕 `git clean -fd` + `git checkout .`

| 風險面向 | `git clean -fd` + `git checkout .` 模式下的問題 | 「discard + fresh worktree」模式下的解法 |
|---|---|---|
| **Safety Rule 違反風險** | `git clean -fd` 會遞迴刪除 untracked 檔；若 `test_assets/` 的 :ro bind 因 sandbox escape 被當成 workspace subdir 看到，clean 會嘗試 `unlink` → EROFS → partial failure 留下半清狀態。CLAUDE.md Safety Rule: `NEVER modify files in test_assets/`。 | 整個 worktree 目錄被當成單位銷毀；`:ro` bind 是 mount point 不是檔案，`shutil.rmtree` 遇到 `:ro` 子目錄會跳過並報錯（可預期、可處理、不會「半刪」data）；mount table 由 container runtime 管、worktree 目錄銷毀不影響 bind source。 |
| **.gitignore 白名單 / build artifact 殘留** | `.gitignore` 列入的 `node_modules/`、`__pycache__/`、`build/` 被 `git clean -fd` 的預設白名單**保留**；要連 ignored 也清必須 `-x`（危險：會把 `.env` 一起清掉）或 `-X`（只清 ignored，但 untracked tracked 檔走不掉）。二選一都會在邊界案例留下髒狀態。 | 整個工作區目錄 `shutil.rmtree` 掉，`.gitignore` 狀態與否無關；fresh worktree 重新走 checkout → `.gitignore` 列舉的檔案根本沒產生過。 |
| **Git internal 髒狀態** | `git checkout .` 只復原 tracked 檔 working-tree；`.git/index.lock`（中斷的 commit）、`.git/MERGE_HEAD`（中斷的 merge）、`.git/rebase-merge/`（中斷的 rebase）、`.git/worktrees/<safe_agent>/locked` 這類中介狀態無法透過 `clean` 或 `checkout` 清掉；需額外 `rm` 呼叫分支去處理每種狀態。 | `git worktree remove --force` 會把 `.git/worktrees/<safe_agent>/` 整個 metadata block 移除；fresh `git worktree add` 產生的是全新 metadata + 全新 index + 全新 HEAD reflog，不可能帶入上述中介狀態。 |
| **Submodule / LFS / sparse-checkout** | `git clean -fd` 對 submodule 目錄預設不遞迴（需要 `-ff`）；LFS smudge cache 可能有 `.git/lfs/` 殘檔不被 clean 觸及；sparse-checkout 的 skip-worktree flag 對 `checkout .` 有特殊處理。這些 feature 互動規則隱晦且會隨 git 版本變化。 | 新 worktree 從 anchor SHA 重新 init：submodule 走 `git submodule update --init` path 乾淨、LFS 走 smudge-on-checkout 重抓、sparse-checkout 由 branch/worktree config 決定——一切回到 first-boot 狀態。 |
| **權限 / 檔案模式 / 時間戳** | Agent 跑過 `chmod +x build.sh` 或 `touch -d yesterday foo`，`checkout .` 只會把 foo 的**內容**拉回，但 mtime / mode 殘留，下游 make 之類的 timestamp-driven build 會誤判 stale。 | 全新 worktree 的所有 mode / mtime 都是 fresh checkout 當下的值，徹底消除跨重試的 side channel。 |
| **操作原子性與中斷安全** | `git clean -fd` + `git checkout .` 是兩步驟、可在中間被 SIGTERM 截斷；留下部分清、部分未清的狀態後，下一次 provision 看到「存在但髒」的 worktree，處理路徑膨脹（要嘛再 clean、要嘛 rmtree、要嘛放棄）。 | 「銷毀整個目錄 → 重建整個目錄」是兩個可分別冪等重試的操作：`git worktree remove --force` 失敗直接 `shutil.rmtree` 覆蓋；`git worktree add` 失敗本來就要重試 provision 整段。沒有「部分清」的中介狀態。 |
| **Audit 可讀性** | Audit log 只能記「曾經跑過 `clean + checkout`」——無法精確回答「retry 後的檔案樹與 retry 前哪些不同」「是否真的回到 anchor」。 | Audit log 記的是「舊 worktree path（含 branch tip SHA snapshot）銷毀 → 新 worktree path（HEAD = anchor_sha）建立」——明確、可驗證、可 diff 重現。 |
| **與既有 startup cleanup 路線一致性** | 啟動時的 orphan cleanup（R8 後續 row）預期的動作是「`git worktree list` 中不屬於任何 active agent 的 worktree 直接 remove」——與 retry path 語意若一致（「銷毀整個 worktree」），兩條路徑可共享 `WorkspaceManager` 同一 helper；若 retry 走 `clean + checkout`、startup 走 `remove`，就有兩套不同概念要分別測試與維護。 | 兩條路徑都是「remove → 可選 recreate」——retry = remove + recreate（anchor SHA）；startup orphan = remove + no recreate。同一 helper 的兩種模式。 |
| **`test_assets/` 這類 read-only bind 的保護** | 若 `.gitignore` 未列、`test_assets/` 在 worktree 內是符號連結到 `:ro` bind source，`git clean -fd` 會 follow symlink 嘗試刪除其內容（git 的預設行為 variant 隨版本）——違反 CLAUDE.md Safety Rule。 | `.agent_workspaces/<safe_agent>/` 整個子樹被 `shutil.rmtree`；目錄內若有 `:ro` bind，rmtree 遇到 EROFS 會對該子樹回報錯誤，但不會透過 symlink 穿出 workspace 邊界去動 bind source（`shutil.rmtree` 預設 `symlinks=False` 不跟 symlink 走）。可預期、可 instrument。 |

### 3.1 真實 workflow 中的具體案例

1. **Makefile + ccache 殘留**：Agent 第一輪跑 `make all`，產生 `.o` + `.d` 在 `build/`；`.gitignore` 已 ignore `build/`。任務失敗 retry。
   - **舊方案**：`clean -fd` 保留 `build/`（ignored），`checkout .` 不動 `build/`；第二輪 `make` 看到舊 `.d` depfile → 認為 headers 沒變 → skip 重編 → 重複踩同一個 bug。
   - **新方案**：`build/` 不存在了；第二輪 `make` 全量重編，真正重現問題或真正修復。

2. **Rust `target/` + Cargo lock 被 agent 誤改**：Agent 手抖 `cargo build --release` 後 commit 了 `Cargo.lock` 改動。
   - **舊方案**：`checkout .` 把 `Cargo.lock` 改回去；但 `target/` 是 ignored → 不清；第二輪 `cargo build` 使用 cached dep graph 與**新** `Cargo.lock` 不符 → 詭異的 linker error。
   - **新方案**：整個 fresh worktree，`target/` 根本沒產生過；Cargo lock 走 anchor SHA 版本。

3. **`test_assets/samples/large_video.bin` :ro bind（CLAUDE.md Safety Rule 物）** 被 agent 的 stray `cp --dereference` 當成 tracked 檔案 staged 了：
   - **舊方案**：`checkout .` 可能不動 `large_video.bin`（它在 HEAD 沒 tracked），但 `clean -fd` 看到這個未 tracked 的東西後會 `unlink` → `:ro` → EROFS → worktree 進入半清狀態。
   - **新方案**：整個 `.agent_workspaces/<safe_agent>/` rmtree → test_assets bind 是 mount point、rmtree 遇到 EROFS 對該子樹單獨報錯但不繼續深入；`git worktree remove` 清掉 `.git/worktrees/` 的 metadata；新 worktree 從 anchor SHA `git worktree add` 到**新路徑**時，若 `test_assets/` 由 provision 階段的 bind-mount 邏輯重新掛上，則 bind-source 本體從未被動過。

4. **Agent 跑過 `npm install -g` 在 workspace 內留下 `node_modules/.bin/` symlink 指向 `.gitignore` 白名單外路徑**：
   - **舊方案**：`clean -fd` 遞迴跟 symlink 可能穿出 workspace 邊界；`checkout .` 不清 symlink。
   - **新方案**：整個 workspace rmtree，其中 symlink 被當 symlink 刪除（不穿越）；新 worktree 不重建該 symlink。

---

## 4. Anchor Commit 機制概述（for R8 後續 rows）

本文件為**設計決策**層；具體實作由 R8 後續 sub-bullet 落地。摘要如下，作為實作時的契約：

* **Anchor 來源**：Agent provision 時，`WorkspaceManager.provision()` 於 `git worktree add` 後立刻呼叫 `git rev-parse HEAD` 取得該 worktree 當前 commit SHA，寫入 CATC metadata（`task_card.navigation.anchor_commit_sha`）與 `WorkspaceInfo.anchor_sha`（記憶體 + DB 欄位均有）。這個 SHA 在整個任務生命週期**不變**（即使 agent 之後 commit 了新的 work，anchor 依然指向 provision 那一刻的純淨起點）。
* **Retry 呼叫路徑**：`orchestrator.retry_agent_task(agent_id, reason)` → 查 CATC 取 `anchor_sha` → 呼叫 `WorkspaceManager.discard_and_recreate(agent_id, anchor_sha)` → 新 worktree path 回寫 `WorkspaceInfo.path` → audit log 寫一筆 `retry.worktree_recreated`。
* **冪等 / 中斷安全**：`discard_and_recreate` 內部兩步驟（destroy / create）皆可獨立重試：destroy 階段若 `git worktree remove --force` fail，fallback 到 `shutil.rmtree`；create 階段若 `git worktree add` fail（例如 branch 已存在、磁碟滿、anchor SHA 不在 object store），propagate exception 讓 orchestrator 走 retry 上限邏輯。全過程寫 audit 並 emit SSE `workspace.retried` event。
* **與 R1 `/omnisight rollback [ID]` 的介面**：ChatOps `rollback` 指令等同於 operator-triggered retry——走同一 `discard_and_recreate` 路徑、同一 audit action，差別在 audit `actor` 欄填 ChatOps user id 而非 `system`。

---

## 5. 取捨與已知成本

1. **Fresh worktree 的 disk / IO 成本比 `clean + checkout` 略高**——因為要銷毀目錄並重新 checkout。評估：平均 OmniSight-productizer 目前 ~180MB 的 worktree，本機 NVMe 下重建 <3s、經 SSHFS 環境下約 20–40s；相較 retry 本身觸發的 LLM 推理成本（$0.10-$1/retry），此 IO 差值可忽略。
2. **Git object store 不會被清理**——worktree remove 只動 `.git/worktrees/` metadata 與 working-tree 檔案，pack files / loose objects 不動。長期來看 packed objects 會累積，靠既有 `git gc --auto` 路徑回收，本決策不改此行為。
3. **Anchor 期間 base branch 若前進，retry 後的 fresh worktree 仍從 anchor SHA 走**——刻意設計，確保 retry 語意是「從 task 開始的狀態重跑」而非「從最新 base rebase 重跑」；若 operator 需要 rebase 到最新 base，走另一條 `orchestrator.rebase_then_retry()` 路徑（非本 decision 範圍）。
4. **CATC 多一個強制欄位 `anchor_commit_sha`**——遷移策略：既有 CATC rows `anchor_commit_sha=NULL` 時 retry fallback 回舊 `clean + checkout`（過渡期相容），新 provision 一律寫入 anchor；遷移窗口鎖 30 天後移除 fallback。此遷移為 R8 後續 row 的責任。

---

## 6. 採用的判準（為何這是正確的覆寫）

白皮書的目的是「**retry 從純淨錨點重跑**」。`git clean -fd` + `git checkout .` 是在單一 checkout 模式下對這個目的的一種實作；在 per-agent worktree 模式下，對同一目的的更好實作是 discard + fresh worktree。兩者目的一致，本決策**不**違背白皮書精神，只是在不同架構基底上採用更 idempotent / auditable / safety-rule-compliant 的機制。

Blast radius：白皮書 §三.2 影響 R8 的 implementation rows（#314）以及 R1 的 `/omnisight rollback` bullet（#307，已合進 master）——R1 早已採用 worktree-based rollback 但未有對應 design doc；本文件追認該決策並綁到 R8 的未完成 rows 上，R8 後續實作直接援引本文件即可。

---

## 7. 驗收契約（當 R8 後續 rows 落地時須滿足）

以下項目**不**屬於本決策 row 的範疇（它們是 R8 下方的 `[ ]` rows），但作為契約列出讓實作 row 可引用：

1. **Anchor commit 寫入 CATC**：`TaskCard.navigation.anchor_commit_sha` 欄位新增、`workspace.provision()` 寫入、CATC JSON schema 更新、既有 rows NULL-fallback 過渡期策略。
2. **`WorkspaceManager.discard_and_recreate(agent_id, anchor_sha)` 實作**：signature、同一 path 複用 vs 新 path、`git worktree remove --force` + `shutil.rmtree` fallback、`git worktree add -b` 從 anchor SHA 建新 branch、回傳新 `WorkspaceInfo`、emit `workspace.retried` SSE event。
3. **Audit trail**：`audit.log(action="retry.worktree_recreated", entity_kind="workspace", entity_id=agent_id, before={old_path, old_branch_tip_sha}, after={new_path, new_branch_tip_sha=anchor_sha}, actor=..., session_id=...)`——對齊 `backend/audit.py` 既有 hash-chain 契約。
4. **Startup orphan worktree scan**：`main.py` lifespan 啟動時呼叫 `WorkspaceManager.cleanup_orphan_worktrees()`——`git worktree list` 與 `_workspaces` 記憶體表 / DB 表交集，不在 active 集合的 worktree 走 `git worktree remove --force`，emit `workspace.orphan_cleanup` audit event。
5. **整合測試**：`backend/tests/test_workspace_discard_recreate.py`（新檔）鎖 happy-path + anchor SHA 不存在 fail-retry + 同一 agent 連續 retry 3 次的冪等性 + audit log chain 延續性 + startup orphan scan 命中率。

本文件不實作上述 5 項；實作為 R8 下方 5 個 `[ ]` rows 的範圍。本文件只鎖定「採用 worktree-based discard 而非 `git clean` 為 retry 機制基礎」這一決策本身，作為上游依據。

---

## 8. 覆寫宣告（索引）

本決策覆寫 `docs/design/enterprise_watchdog_and_disaster_recovery_architecture.md` §三.2「冪等性重試與工作區回滾」中「工作區清理：重試前，系統強制執行 `git clean -fd` 與 `git checkout .`」一句；其餘 §三.2 條目（狀態回滾的**目的**、5 道安全防線的總體架構）**不變**。白皮書本身已在該段落標記 override 指標（指向本檔）。

未來若架構回到 single shared checkout（不再使用 per-agent worktree），本決策可重新評估；在使用 worktree 模式的任何部署下，本決策持續生效。
