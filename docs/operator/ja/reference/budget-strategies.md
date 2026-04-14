# Budget Strategies — Budget Strategy panel の 4 枚のカード

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

## TL;DR — PM 向け

Budget Strategy は **agent 呼び出し 1 回あたりにかけていいコスト** を
決めます。4 つのプリセットで目的別に用意されており、カスタムは現時点で
未サポートです(必要なら issue を立ててください)。

| 戦略 | 使いどころ | 一行で見るコスト / 品質トレードオフ |
|---|---|---|
| **QUALITY** | 重要リリース、安全認証ファームウェア | 最上位モデル、3 回リトライ、自動ダウングレード無 — 最精度、最コスト |
| **BALANCED** | デフォルトの日常開発 | 標準モデル、2 回リトライ、90 % で降格 |
| **COST_SAVER** | 試行錯誤、サイドプロジェクト、実験 | 廉価モデル、1 回リトライ、70 % で降格 |
| **SPRINT** | デモ前追込み、納期プッシュ | 標準、2 回リトライ、並列実行優先 |

切替は即時。`budget_strategy_changed` SSE で全接続ブラウザに反映されます。

## 5 つの tuning knob

各戦略は 5 knob の固定組合せ。Budget Strategy panel 下部で現在値をリアルタイム表示。

| Knob | 範囲 | 意味 |
|---|---|---|
| **TIER** | `premium` / `default` / `budget` | provider チェーンが最初に使うモデル段。`premium` = provider の最強、`budget` = 最安。provider 設定で各段の具体モデルが対応付け。 |
| **RETRIES** | 0 – 5 | 一時的 LLM エラー (rate limit / 5xx) 後、諦める前に何回再試行するか。 |
| **DOWNGRADE** | 0 – 100 % | 日次 token 予算の何 % で自動的に廉価段へ切替えるか。 |
| **FREEZE** | 0 – 100 % | 非重要 LLM 呼出を全て凍結し、以降の agent 作業に明示承認を要求する閾値。 |
| **PARALLEL** | YES / NO | orchestrator が独立 agent を積極的に並列化するか(SPRINT は YES)。 |

`DOWNGRADE < FREEZE` — FREEZE は厳しい停止。両方 100 % なら発動せず。

## 4 戦略の詳細

### QUALITY
- TIER=premium · RETRIES=3 · DOWNGRADE=100 % · FREEZE=100 % · PARALLEL=NO
- **向いている**: 有料顧客への出荷、安全レビュー、最終 firmware build。
- **向かない**: 高速イテレーション — タスク単価が最高、premium モデルは
  概して遅い。

### BALANCED (デフォルト)
- TIER=default · RETRIES=2 · DOWNGRADE=90 % · FREEZE=100 % · PARALLEL=NO
- **向いている**: 日常作業。品質とコストのバランス点。日予算 90 % を
  超えると静かに budget 段に落ちて当日を乗り切る。
- **向かない**: リリース直前で 10 % 降格ゾーンが品質リグレッションの
  リスクになる時。

### COST_SAVER
- TIER=budget · RETRIES=1 · DOWNGRADE=70 % · FREEZE=95 % · PARALLEL=NO
- **向いている**: 試験的コーディング、サイドプロジェクト、手動 QA スクリプト。
- **向かない**: 顧客向けのどれか。budget 段は premium が捕捉する境界
  ケースを取りこぼし、リトライ 1 回だと一時障害がハードエラーとして表面化。

### SPRINT
- TIER=default · RETRIES=2 · DOWNGRADE=95 % · FREEZE=100 % · PARALLEL=YES
- **向いている**: 納期追込み、デモ準備、並列リファクタ一斉実行。
  `prefer_parallel=YES` によりスケジューラが MODE 並列上限を飽和
  (FULL AUTO = 4 agent 同時、TURBO = 8)。
- **向かない**: 並列度が低く厳密な実行順序が必要なタスク — 依存未宣言
  なら子 task が親より先に走る可能性あり。

## MODE との相互作用

Budget Strategy と Operation Mode は直交概念:

- MODE は **誰が承認** (あなた vs AI) を決定
- Budget Strategy は AI の決定の **コスト** を決定

よく使う組合せ:

| MODE × 戦略 | 意義 |
|---|---|
| SUPERVISED × BALANCED | 日常デフォルト — AI が通常作業、リスクはあなたが承認、標準モデル |
| TURBO × SPRINT | 週末バッチリファクタ — 最大並列度、最大自律度 |
| MANUAL × QUALITY | 最終リリースレビュー — 各ループで人、premium モデル |
| FULL AUTO × COST_SAVER | 試験的プロトタイプ — AI 推進、廉価モデル |

## Token 予算との相互作用

DOWNGRADE と FREEZE の閾値は日次 LLM token 予算
(`OMNISIGHT_LLM_TOKEN_BUDGET_DAILY` で設定) に対して評価。
`token_warning` SSE は 80 / 90 / 100 % で発火、Budget Strategy tuning が
自動降格トリガーを決定。

## 戦略を変更できる人

mode と同様、PUT `/api/v1/budget-strategy` は `OMNISIGHT_DECISION_BEARER`
設定時に bearer token 必須、レート制限はクライアント IP あたり 10 秒で
30 リクエスト。

## 内部実装

- バックエンド: `backend/budget_strategy.py` · `_TUNINGS` が上記 4 行の
  凍結 dict。`set_strategy()` で `budget_strategy_changed` を発行。
- フロント: `components/omnisight/budget-strategy-panel.tsx` · 4 枚
  カード + 5 knob cell (TuningCell) + SSE 同期。
- イベント: `backend/sse_schemas.py` の `SSEBudgetStrategyChanged`。

## 関連ドキュメント

- [Operation Modes](operation-modes.md)
- [Decision Severity](decision-severity.md) — severity タグは budget と独立
- [Troubleshooting](../troubleshooting.md) — panel が赤いエラーバナーを表示した時
