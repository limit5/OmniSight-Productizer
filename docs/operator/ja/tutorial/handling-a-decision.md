# Tutorial · 決定の扱い方 (8 分)

> **source_en:** 2026-04-14 · authoritative

[はじめての Invoke](first-invoke.md) の続編。決定の一生を最初から
最後まで追います: どこに現れ、どう判断し、間違えた時どう戻すか。

## 1 · 決定を強制発生させる

**Orchestrator AI** を開き入力:

```
/invoke workspace の変更を origin/main に push
```

SUPERVISED モードでは **destructive** 決定を提案します (`main` への
push は force-push + 祈り以外では不可逆)。

## 2 · 目に見える場所

3 つの同期サーフェスに同じ決定が表示されます:

- **Toast** (右上) — 赤枠、AlertOctagon アイコン、残 < 10 秒で赤く点滅するカウントダウン。
- **Decision Queue** panel — 項目が最上段に出現。Pending count バッジが
  +1、各行にカウントダウン列。
- **SSE log** (REPORTER VORTEX) — 1 行 `[DECISION] dec-… kind=push
  severity=destructive`。

デフォルトタイムアウトは 60 秒。propose 時に調整可、sweep loop が
監督 (`OMNISIGHT_DECISION_SWEEP_INTERVAL_S` 参照)。

## 3 · 決める

3 通り:

### Approve (承認)
APPROVE をクリック。severity が `destructive` なので
`window.confirm()` ダイアログが出ます ("Approve DESTRUCTIVE decision?")。
これが B10 セーフガード — キーボード `A` の誤打で prod push を通す
事故を防ぎます。

確認 → agent 続行、決定は HISTORY へ移動、toast が消える。

### Reject (拒否)
REJECT をクリック。destructive では同様に confirm。確認 → agent 中断。
決定は `resolver=user, chosen_option_id=__rejected__` で HISTORY へ。

### Timeout
何もしない。カウントダウン 0 で sweep loop が `default_option_id`
(destructive なら通常は安全選択肢) に自動解決。`resolver=timeout` を記録。

## 4 · Undo

Decision Queue を開き **HISTORY** タブへ (HISTORY クリックか、PENDING
から → 方向キー)。先程の決定を探して **UNDO** をクリック。

undo が **しないこと**: 現実世界の効果は元に戻しません (git push は
既に送信済)。決定状態を `undone` に反転し `decision_undone` SSE を
発行するだけ — あなたの記録系に「オペレーターは後悔した」と伝える。

`undone` は「監査ログ: オペレーターは後悔した」と解釈を。「システムが
自動で戻す」ではありません。真の巻き戻しは補償アクションを手動で実行
する必要あり (例: 直前 commit で `git push -f`)。

## 5 · SSE round-trip を観察

同じ dashboard を別タブで開く。全イベントがリアルタイム同期 —
Decision Queue、toast、mode pill — 全て SSE `/api/v1/events` 経由。

1 つのタブを閉じる。もう片方は継続動作。これが Phase 48-Fix で
入った共有 SSE manager: 1 ブラウザ 1 EventSource を全 panel が共有。

## 6 · Rule を定義して次回は問われないようにする

特定の branch パターンへの push を「常に」自動承認したい場合、
**Decision Rules** panel を開く:

```
kind_pattern: push/experimental/**
auto_in_modes: [supervised, full_auto, turbo]
severity: risky          # destructive から降格
default_option_id: go
```

保存。次回マッチする決定はリストされた mode で自動実行。ルールは
SQLite に永続化 (Phase 50-Fix A1)、再起動後も有効。

## 関連

- [Decision Severity](../reference/decision-severity.md) — destructive
  で confirm が出て risky で出ない理由。
- [Operation Modes](../reference/operation-modes.md) — severity × mode
  の自動実行マトリクス。
- [Troubleshooting](../troubleshooting.md) — `[AUTH]` / `[RATE LIMITED]`
  バナー、および「ボタンが反応しない」系の対処。
