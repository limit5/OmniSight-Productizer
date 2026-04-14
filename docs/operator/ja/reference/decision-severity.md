# Decision Severity — info / routine / risky / destructive

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

## TL;DR — PM 向け

AI が出す全ての決定にはリスクラベルが付きます。ラベルがアイコン・色・
カウントダウン・MODE による自動実行可否を決定します。
**特に destructive に注意してください。**

| Severity | アイコン | 色 | 復旧可能? | 典型例 |
|---|---|---|---|---|
| **info** | 情報円 | ニュートラル | はい | 「回答のために 12 ファイル読込」 |
| **routine** | 情報円 | ニュートラル | はい | このタスクで使うモデル選択 |
| **risky** | 警告三角 | アンバー | 復旧可能 | タスク途中での LLM provider 切替 |
| **destructive** | 警告八角 | 赤 | **不可** | production push、workspace 削除、release ship |

## AI はどう severity を選ぶか

決定が提案された瞬間に決まります。2 つのソース:

1. **ハードコードデフォルト** — engine は例えば `deploy/*` を
   `destructive`、`switch_model` を `risky` と認識しています。
2. **Decision Rules** — オペレーター定義の上書き。例えば「当チームの
   `deploy/staging` は `risky` 扱い」や「FULL AUTO では
   `git_push/experimental/*` を自動実行」といった宣言が可能。
   Decision Rules panel で設定します。

## UI 表示

**Decision Queue** panel と右上の **Toast** で:

- **Destructive** — 赤 AlertOctagon アイコン、赤枠、赤カウントダウンバー、
  APPROVE / REJECT クリック時にブラウザ `confirm()` ダイアログ表示
  (B10 セーフガード)。
- **Risky** — アンバー AlertTriangle、アンバー枠、カウントダウンあり confirm なし。
- **Routine / info** — 青 Info アイコン、`timeout_s` 設定がない限り
  カウントダウン非表示。

pending 決定の残り時間が 10 秒未満になると、panel と toast 両方で
カウントダウンが **赤く点滅** し、離れた場所からでも気づけます。

## タイムアウト時の挙動

pending 決定がタイムアウトした場合:

- `default_option_id` (通常は安全な選択肢)で自動解決
- `resolver` フィールドに `"timeout"` と記録
- `decision_resolved` SSE を発行し history へ移動
- 30 秒 sweep ループが処理。Decision Queue ヘッダーの **SWEEP**
  ボタンで手動トリガーも可能

間隔は `OMNISIGHT_DECISION_SWEEP_INTERVAL_S` で上書き(デフォルト 10)。

## Destructive 二重確認 — B10 保護

監査項目 B10 で追加。destructive 決定に APPROVE / REJECT すると、
タイトルと選択肢を示すブラウザ confirm ダイアログが出ます。意図:

- キーボードショートカット `A` の打ち間違いで「push prod」を
  通してしまう事故を防止。
- Reject も確認させる — destructive deploy の拒否は中途半端にマージ
  されたブランチを残す可能性があるため。

バイパスが必要な場合(E2E スクリプト等)は UI ではなく backend API を
直接呼んでください。

## レート制限

Decision mutator 端点(`/approve`、`/reject`、`/undo`、`/sweep`、
`/operation-mode`、`/budget-strategy`)はスライディングウィンドウ
レート制限あり — デフォルトはクライアント IP あたり 10 秒で 30 リクエスト。
`OMNISIGHT_DECISION_RL_WINDOW_S` と `OMNISIGHT_DECISION_RL_MAX` で調整。

## 内部実装

- Enum: `backend/decision_engine.py · DecisionSeverity`
- 自動実行マトリクス: `should_auto_execute(severity, mode)`
- Destructive confirm: `components/omnisight/decision-dashboard.tsx ·
  doApprove / doReject`
- レート制限: `backend/routers/decisions.py · _rate_limit()`

## 関連ドキュメント

- [Operation Modes](operation-modes.md) — severity × mode で
  自動 / キューが決まる仕組み
- [Panels Overview](panels-overview.md) — pending / history の表示場所
