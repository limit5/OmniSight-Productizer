# Tutorial · はじめての Invoke (10 分)

> **source_en:** 2026-04-14 · authoritative

起動直後の dashboard からはじめての **Singularity Sync / Invoke** まで
ご案内します。Invoke は "システムの状態を見て次にすべきことを決め、
実行する" orchestrator への全体指示です。本ツアー後、AI が光らせた
要素を全て識別でき、介入が必要な時にどこをクリックすればよいか
分かるようになります。

## 事前準備

- バックエンドが `http://localhost:8000` (または `BACKEND_URL` の示す
  先) で稼働。`curl http://localhost:8000/api/v1/health` で確認。
- フロントエンドが `http://localhost:3000`。
- `.env` に LLM provider key が最低 1 つあるか、なし (なしでも rule-based
  フォールバックは動作、agent はテンプレ応答のみ)。

## 1 · 環境を把握

`http://localhost:3000` を開くと、新規ブラウザでは **5 ステップの
初回ツアー** が自動起動 (各カード下部の Skip / Next)。完了後は
dashboard があなたのものに。最上部を見渡してください:

- **MODE** pill — デフォルト SUPERVISED。通常の AI アクションは自動、
  リスクのあるものはあなた待ち。
  [→ 詳細](../reference/operation-modes.md)
- **`?` ヘルプアイコン** (MODE の隣) — 忘れた時はいつでもクリック。
- **Decision Queue** (右側のタイル) — 今は空。AI が自動実行できない
  決定事項がここに集まります。

## 2 · 最もシンプルな task を選ぶ

**Orchestrator AI** panel を開き (デスクトップでは中央、スマホは
スワイプ)、入力欄に:

```
/invoke 接続中のハードウェアを一覧表示して
```

Enter。

## 3 · パイプラインが点灯する様子を見る

連鎖的にイベントが起こります (これが正常):

1. 左の **REPORTER VORTEX** ログに `[INVOKE] singularity_sync: ...`。
2. **Agent Matrix** panel で agent が `active` に。thought-chain が
   行ごとに更新。
3. 1 つ以上の **Tool progress** イベントがファイル読込 / シェル呼出を表示。
4. SUPERVISED モードで agent が `risky` / `destructive` を提案する
   場合、右上に **Toast** が出現し **Decision Queue** にも入る。

この "読取一覧" invocation では決定は出ないはず — AI は対話で直接回答。

## 4 · 答えを読む

Orchestrator が panel に応答メッセージを返します。接続デバイス一覧が
出るはずです (開発用ラップトップでカメラ未接続なら空で OK)。

## 5 · やや危険な invoke を試す

```
/invoke 現在の workspace に tutorial-sandbox という git branch を作成
```

今回 SUPERVISED モードだと **Decision Queue** に severity `risky` の
項目が出ます。Toast は A / R / Esc のキーヒントとカウントダウン。

- **A** (または APPROVE クリック) — AI が branch を作成。
- **R** — AI は手を引く。
- カウントダウン満了 — デフォルト安全選択肢 (通常 "手を引く") に解決。

もし決定が出なければ、規則か MODE を FULL_AUTO / TURBO に変えた結果
自動実行されている可能性。Decision Queue panel 内の `?` で severity
マトリクスを確認。

## 6 · MANUAL モードを試す

MODE pill → MANUAL。branch 作成 invoke を再実行。今度は *全ての*
ステップが Decision Queue に入ります。通常の読取も含めて。"AI が
何をしようとしているか見てから動かしたい" 時の正しいモード。

試し終わったら SUPERVISED に戻す。

## 次のステップ

- [決定の扱い方](handling-a-decision.md) — risky/destructive 決定の
  完全ライフサイクル (undo 含む)。
- [Operation Modes](../reference/operation-modes.md) — severity × mode
  マトリクスの詳細。
- [Budget Strategies](../reference/budget-strategies.md) — 本チュートリアル
  中に token 消費が気になった場合。
- [Troubleshooting](../troubleshooting.md) — 描写通りに光らなかった時。
