# DAG 編寫 — 讓 planner 實際跑起來的 workflow 怎麼寫

> **source_en:** 2026-04-15 · Phase 56-DAG-E/F/G + Product #1-3 · authoritative

DAG 面板（`?panel=dag`）是你把「先編譯，再燒錄，最後並行跑三個
benchmark」這種 **有向無環 task 圖** 交給 OmniSight 的地方。Backend
負責排程、沙盒、守門。這份文件講你要填什麼、validator 檢查什麼、
三個編輯模式怎麼挑。

## 精要（TL;DR）

1. 從 header 右側的**範本 chip** 開一個起手式，**不要**從空白開始。
2. 不想手寫 JSON → 用 **Form** tab；要 diff review → **JSON** tab；要看依賴流 → **Canvas** tab。
3. Header 綠色 badge = `POST /dag/validate` 通過。Submit 在通過前是 disabled。
4. Canvas 節點**可點擊**，會跳到 Form 對應 row 並高亮。
5. Submit 成功的綠色 banner 會給一個 **View in Timeline** 鈕，一鍵跳 `?panel=timeline` 看執行。

## Schema（validator 看的東西）

每個 task 有這些欄位：

| 欄位 | 型別 | 規則 |
|---|---|---|
| `task_id` | 字串 ≤ 64 | 僅英數 / 底線 / 破折號。作為 Decision Engine key + workflow step idempotency key 使用，**一旦提交就是永久識別**，改名要小心。 |
| `description` | 字串 ≤ 4000 | 給人看。顯示在 Canvas tooltip 和 audit log。 |
| `required_tier` | `t1` / `networked` / `t3` | Tier 1 = airgapped 編譯沙盒。Tier 2（`networked`）= 出口受控。Tier 3 = 實機。如果要求的 toolchain 跟 tier 不合（例如 `flash_board` 放 t1），validator 會拒絕。 |
| `toolchain` | 字串 ≤ 128 | 自由文字，但必須是 agent 認得的名稱（`cmake`、`flash_board`、`simulate`、`git`、`checkpatch`、`finetune_export`、`http_download` …）。亂打會在 sandbox 階段才出錯。 |
| `inputs` | list[字串] | 這個 task 會讀的檔案路徑。通常是別的 task 的 `expected_output`。起點 task 空陣列就好。 |
| `expected_output` | 字串 ≤ 512 | 產出落腳處。允許三種格式：檔案路徑（`build/firmware.bin`）、git ref（`git:abc1234`）、issue ref（`issue:OMNI-42`）。 |
| `depends_on` | list[task_id] | 哪些 task 必須先完成。不可自我依賴、不可重複、不可形成循環。 |
| `output_overlap_ack` | bool | **MECE escape hatch**。預設不允許兩個 task 寫到同一個 `expected_output` — 除非**兩邊都**明示 true。用於並行 benchmark 寫到同一份合併報告的場景。其他情況留 false。 |

### 七條規則

編輯時每 500ms debounce 一次打 `POST /dag/validate`，會亮的錯誤：

| 規則 | 觸發條件 |
|---|---|
| `schema` | Pydantic 拒絕（缺欄位、型別錯） |
| `duplicate_id` | 兩個 task 的 `task_id` 撞名 |
| `unknown_dep` | `depends_on` 指向不存在的 id |
| `cycle` | 依賴圖出現循環 |
| `tier_violation` | Toolchain 在該 tier 不被允許 |
| `io_entity` | `expected_output` 不符合三種合法格式 |
| `dep_closure` | `inputs` 指向一個沒有上游 task 產出的路徑 |
| `mece` | 兩 task 產出同路徑但未雙邊 ack |

## 三個 tab

**JSON。** 純文字編輯。適合 diff review、跨實例 copy-paste。斷點：
JSON 壞掉時（括號不配等），Form tab 會拒絕渲染，要求你先回 JSON
修 — 不會無聲丟棄你的草稿。

**Form。** 一張卡片一個 task，`depends_on` 用 chip toggle，`inputs`
用 typeahead chip，`output_overlap_ack` 是 checkbox。**覆蓋 100 %
schema** — 常用情況下不必切回 JSON。

**Canvas。** 唯讀拓撲視圖。Tier 上色（紫 = T1、藍 = networked、
橘 = T3）。紅框 = 此節點牽涉驗證錯誤。**點擊節點可跳 Form 對應 row**。
1–20 task 規模 depth-based layout 夠看；未來升級 react-flow 加
pan/zoom/minimap 是另一個 phase。

## 範本

Header 的 chip 列可以一鍵載入：

| 範本 | 形狀 | 何時用 |
|---|---|---|
| `Minimal` | 1 個 T1 compile | Smoke-test 編輯器 / 最小可提交 |
| `Compile → Flash` | T1 → T3 | 典型 happy path |
| `Fan-out (1→3)` | 1 build → 3 parallel sims | 練習平行 / fan-out |
| `Tier Mix` | T1 + NET + T3 | 展示三 tier 交接 |
| `Cross-compile` | configure / compile / checkpatch | embedded SoC sysroot 模式 |
| `Fine-tune (Phase 65)` | export / submit / eval | 手動啟動一輪 self-improve |
| `Diff-Patch (Phase 67-B)` | propose / dry-run / apply | 經 DE 核准的 workspace 修改 |

挑最接近的改，不要從空白 textarea 起步。

## Submit

**Submit** 按鈕在 validate 回 `ok: true` 前是 disabled。點下去後
OmniSight 會：

1. `POST /dag` — 再跑一次完整 validator、入庫、開一個連結到這 DAG 的 `workflow_run`。
2. 驗證通過就立刻開始執行。
3. 驗證失敗且你勾了 `mutate=true`，OmniSight 會請 LLM 提 fix（最多 3 輪）。沒勾則回 422 + 規則錯誤列表。

成功 banner 上的 **View in Timeline** 按鈕一鍵跳 `?panel=timeline`
看 run 執行進度。

## `mutate=true` 什麼時候勾

只有在你**希望 OmniSight 自動修**驗證失敗的 plan 時才勾。LLM 看
錯誤、提編輯、我們重驗。最多 3 輪，超過會開 Decision Engine
提案讓你決定要不要接受自動修的版本。

手寫 plan 預設**不勾**：「失敗就失敗，我自己修」才是嚴格的預設行為。

## 常見錯誤

- **Toolchain 打錯字。** Validator 不抓，sandbox runtime 才炸。不確定就抄範本。
- **`expected_output` 跟實際不符。** 你寫 `build/foo.bin` 但 toolchain 放到 `out/foo.bin`，下游 task 的 `inputs` 在 runtime 會 `dep_closure`。要看 toolchain 契約。
- **Rename 造成 cycle。** Form 裡 rename `task_id` **不會**自動同步到其他 task 的 `depends_on`（只有 delete 會 scrub）。rename 後檢查下游 chip toggle。
- **第一版 DAG 太野心大。** 先寫 2–3 個 task、提交、看它跑，再長。Canvas 在 5 個 task 以上才真正有用。

## 相關資源

- [`panels-overview.md`](panels-overview.md) — 每個面板的一句話用途。
- `docs/design/dag-pre-fetching.md` — RAG 如何在 task 失敗時注入歷史解法。
- `backend/dag_validator.py` — 7 條規則的程式碼版。
