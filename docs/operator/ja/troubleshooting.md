# Troubleshooting — dashboard が異常を通知した時

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

オペレーターが実際に見る症状別に整理。本ページで扱っていない場合は
Orchestrator AI panel の log 流れ (REPORTER VORTEX) と backend stderr を確認。

## Panel に赤いバナー

### `[AUTH] ...`
バックエンドが 401 / 403 で拒否。

- **原因**: バックエンドで `OMNISIGHT_DECISION_BEARER` が設定されているが
  フロントエンド保存の token が誤っているか欠落。
- **対処**: Settings → provider タブで bearer を再入力、または単機
  ローカル運用なら `.env` の `OMNISIGHT_DECISION_BEARER` を解除。

### `[RATE LIMITED] ...`
スライディングウィンドウ制限発動 (デフォルト クライアント IP あたり
10 秒で 30 リクエスト)。

- **原因**: スクリプトポーリングか UI のリトライループ暴走。
- **対処**: バナー自動消去を待つ (10 秒)、または
  `OMNISIGHT_DECISION_RL_MAX` / `_WINDOW_S` で緩和。`.env.example` 参照。

### `[NOT FOUND] ...`
端点が 404 を返却。

- **原因**: バックエンドが削除 / リネームした端点をフロントが呼んでいる。
  部分デプロイでの版ずれが典型。
- **対処**: ページをハードリロード。収まらなければフロントとバックエンド
  の版が不一致 — 両方再起動。

### `[BACKEND DOWN] ...`
バックエンドが 5xx を返却。

- **原因**: uvicorn 停止、または router の未処理例外。
  `/tmp/omni-backend.log` (dev) かサービス log (prod) を確認。
- **対処**: バックエンドを再起動。起動時に落ちる場合は
  `python3 -m uvicorn backend.main:app` をフォアグラウンドで走らせ stack を見る。

### `[NETWORK] ...`
fetch がバックエンドに到達する前に失敗。

- **原因**: バックエンドプロセス停止、port 違い、proxy / VPN 切断。
- **対処**: `curl http://127.0.0.1:8000/api/v1/health`。応答があれば
  フロントの `NEXT_PUBLIC_API_URL` か rewrite 設定ミス。なければバック起動。

## Decision Queue が止まって見える

### 承認 / 拒否を押しても pending が消えない
- **原因 1**: バックエンドが 409 を返却 — 別タブで既に解決済。UI は
  次の SSE イベントで整合、panel ヘッダーの **RETRY** で強制同期。
- **原因 2**: destructive severity の `window.confirm()` ダイアログが
  非表示タブに開いたまま。全 dashboard タブを確認。

### 毎回クリック前にタイムアウトする
- propose のデフォルト `timeout_s` は 60。producer が短い deadline を
  指定していて間に合わない場合、sweep loop がデフォルト安全選択肢で
  解決。これは意図した動作。
- 時間を確保したい: MANUAL モードへ切替 (deadline 非設定で決定は
  無期限保留 — decision payload の `deadline_at` で確認)。

### SWEEP を押しても反応なし
- deadline が **既に過ぎた** 決定のみ解決。全て時間内なら 0 件解決で
  一時メッセージ表示。

## Toast の問題

### "+N MORE PENDING" チップが消えない
- 表示中 toast を全て dismiss (各 Esc / ✕ クリック)。overflow カウンタは
  スタックが 0 の時だけリセット。
- それでも残る場合、バックエンドの `decision_pending` 発火速度が
  処理より速い。MODE を下げ (SUPERVISED / MANUAL) 通常決定の自動
  実行から新しい risky/destructive の派生を止める。

### カウントダウンが 100 % で固まる
- バックエンドとブラウザの時計ずれ。両端で `date -u` を比較。
- バック時計が先行している場合、実 deadline 経過まで満値で静止し
  その後 0 に瞬跳。

### カウントダウンが NaN / 奇妙な値
- バックエンドが不正な `deadline_at` を送信。監査 B2 で追加した
  バリデータが強制型変換する想定だが、それでも見る場合: ハードリロード
  (JS キャッシュ); 持続するなら raw SSE payload を添えて issue 起票。

## Agent の問題

### Agent が 30 分以上 "working" で停滞
- watchdog が 30 分後に発火し stuck 補修決定 (switch model /
  spawn alternate / escalate) を提案。Decision Queue を確認。
- 60 秒待って何も出ない場合、watchdog はその agent にアクティブな
  heartbeat があると判断している。**Emergency Stop** → Resume で強制リセット。

### Agent が同じエラーを繰り返す
- 各 agent の error ring buffer (10 件) は node graph が供給。
  ウィンドウ内で 3 回同一エラーが出ると stuck detector が FULL AUTO /
  TURBO では自動的に `switch_model` 補修を提案、下位モードでは
  キューイング。
- そこまで至らない場合、エラーが tool error として表面化していない
  可能性 — REPORTER VORTEX を確認。

### Provider health が赤だが key は正しい
- Provider health = 直近 3 回の probe ping。クォータ切れも失敗扱い。
  provider ダッシュボードを確認。
- key が有効な場合、keyring が古い版を読み込んでいる可能性。
  Settings → Provider Keys → 再保存。

## スマホ / タブレットの問題

### スマホで一部 panel にアクセスできない
- 下部 nav のドット列が全 12 panel 対応。ドット数が少ない場合、
  Phase 50D より古い build — ハードリロード。
- swipe prev / next ボタンで順番に巡回。

### ディープリンクが意図しない panel を開く
- `?panel=` が `?decision=` より優先。`?panel=` 成分を外すか、
  決定 id をディープリンクする場合は `?panel=decisions` を付与。

## 本当に行き詰まった時

- `curl http://localhost:8000/api/v1/system/sse-schema | jq` —
  バックエンドが応答しフロント期待のイベント型を発行しているか確認。
- `pytest backend/tests/test_decision_engine.py` — 決定エンジン
  27 テストが 1 秒未満で完走、バックエンド回帰の大半を捕捉。
- issue 起票時は: バックエンド commit hash (`git rev-parse HEAD`)、
  赤バナー文言、REPORTER VORTEX の最後 50 行。

## 関連

- [Operation Modes](reference/operation-modes.md)
- [Decision Severity](reference/decision-severity.md)
- [Glossary](reference/glossary.md)
