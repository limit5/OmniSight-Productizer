# Glossary 用語集

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

UI と log で使われる専門用語。アルファベット順。

**Agent** — 専門 LLM worker。デフォルト 8 種(firmware、software、
validator、reporter、reviewer、general、custom、devops)、各々に
`sub_type` があり `configs/roles/*.yaml` の役割ファイルを選択。
各 agent は独立 git workspace を持つ。

**Artifact** — pipeline が生成する保存価値のある任意ファイル:
コンパイル済 firmware、simulation report、release bundle 等。
`.artifacts/` 配下に保存、Vitals & Artifacts panel で表示。

**Budget Strategy** — 5 つの tuning knob (model tier、max retries、
downgrade threshold、freeze threshold、prefer parallel) を束ねた
名前付きセット。agent 呼出のコスト上限を規定。デフォルト 4 種:
`quality`、`balanced`、`cost_saver`、`sprint`。

**Decision** — AI が停止し、自身で動作するか・あなたに訊くか・
timeout で安全デフォルトに倒すかを決める地点。severity
(`info` / `routine` / `risky` / `destructive`) と選択肢リストを持つ。

**Decision Queue** — pending 決定リスト (panel 名と in-memory list
同名)。最新が先頭。

**Decision Rule** — オペレーター定義の上書き規則。`kind` glob
(例: `deploy/staging/*`) をマッチし、severity、デフォルト選択肢、
自動実行 mode を固定。SQLite で永続化 (Phase 50-Fix A1)。

**Emergency Stop** — 全稼働 agent と pending invocation を停止。
concurrency slot を解放し `pipeline_halted` を発行。Resume で復帰。

**Invoke** — 「全体同期」操作。orchestrator に現状棚卸しと次アクション
決定を指示。自由指令も可 (`/invoke fix the build`)。

**LangGraph** — 基盤となる agent graph フレームワーク。日常は意識
不要だが、log 中の「graph state」「reducer」は LangGraph の用語。

**L1 / L2 / L3 memory** — 階層 agent memory。L1 = `CLAUDE.md` の
不変コアルール。L2 = agent 別役割 + 近接対話。L3 = episodic
(過去事例を FTS5 で検索可能)。

**MODE** — 全体自律度。[operation-modes.md](operation-modes.md) 参照。

**NPI** — New Product Introduction、ハードウェア出荷ライフサイクル:
Concept → Sample → Pilot → Mass Production。各フェーズ独自の
pipeline を持つ。

**Operation Mode** — MODE の正式名。4 値: manual、supervised、
full_auto、turbo。

**Pipeline** — task を「idea」から「shipped」まで進める順序付き
ステップ群。ステップは phase を構成。Pipeline Timeline panel で
現在実行を可視化。

**REPORTER VORTEX** — 左側のスクロール log。システムの全動作を表示。
全 `emit_*()` イベントがここに書き込まれる。

**SSE** (Server-Sent Events) — backend が接続ブラウザに一方向で
リアルタイム更新を送るチャネル。端点 `/api/v1/events`。
Schema は `/api/v1/system/sse-schema`。

**Singularity Sync** — Invoke のマーケティング名、同義語。

**Slash command** — Orchestrator AI panel で `/` から始まる指令。
組込コマンド: `/invoke`、`/halt`、`/resume`、`/commit`、`/review-pr`、
加えて skill システム定義のもの。

**Stuck detector** — 同じエラーで agent が詰まっている時に補修決定
(switch model、spawn alternate、escalate) を提案する watchdog。
60 秒ごとに実行。

**Sweep** — deadline 経過した pending 決定を timeout 処理する
定期パス (デフォルト 10 秒)。Decision Queue ヘッダーから手動実行可。

**Task** — 作業単位。担当 agent、優先度、状態、親子ツリー、
オプションの外部 issue リンク (GitHub、GitLab、Gerrit) を持つ。

**Token warning** — 日次 LLM token 予算が 80 % / 90 % / 100 % に
達した時の SSE イベント。90 % で安価モデルへの自動ダウングレード。

**Workspace** — 各 agent の隔離 git clone。`OMNISIGHT_WORKSPACE`
(デフォルト一時ディレクトリ) 配下。状態: `none | active | finalized
| cleaned`。

## 関連

- [Operation Modes](operation-modes.md)
- [Panels Overview](panels-overview.md)
- `backend/models.py` — 正本の enum 定義
