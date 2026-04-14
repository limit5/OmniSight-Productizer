# Panels Overview — 画面上の各タイルの役割

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

Dashboard は計 12 個の panel で構成されます。デスクトップではタイル
表示、スマホ / タブレットでは下部 nav bar でスワイプ切替。各 panel の
一言役割と、必要に応じた詳細文書へのリンクを示します。

## 上部バー(常時表示)

| 要素 | 役割 | 詳細文書 |
|---|---|---|
| **MODE** pill | 全体自律度 — AI が承認なしにどこまで動けるか | [operation-modes.md](operation-modes.md) |
| **Sync count** | 本セッションの Singularity Sync 発行回数 | — |
| **Provider health** | 現在到達可能な LLM provider | — |
| **Emergency Stop** | 全ての稼働 agent と pending invocation を即停止 | — |
| **Notifications** ベル | 未読 L1-L4 通知(Slack / Jira / PagerDuty / 画面内) | — |
| **Settings** 歯車 | Provider key、連携、agent 別モデル上書き | — |
| **Language** 地球儀 | UI 言語切替(文書リンクも連動) | — |

## 主要 panel

| Panel | URL パラメータ | 対象 | 一言役割 |
|---|---|---|---|
| **Host & Device** | `?panel=host` | エンジニア | 駆動中の WSL2/Linux ホストと接続カメラ / 開発ボード |
| **Spec** | `?panel=spec` | PM + エンジニア | agent の参照仕様 `hardware_manifest.yaml` |
| **Agent Matrix** | `?panel=agents` | 両者 | 8 agent × 現状 / thought chain / 進捗 |
| **Orchestrator AI** | `?panel=orchestrator` | 両者 | supervisor agent との対話。slash コマンドもここ |
| **Task Backlog** | `?panel=tasks` | PM | sprint 風 task リスト。ドラッグ再割当、優先度ソート |
| **Source Control** | `?panel=source` | エンジニア | agent 別隔離 workspace、branch、commit 数、repo URL |
| **NPI Lifecycle** | `?panel=npi` | PM | Concept → Sample → Pilot → MP 各フェーズと日程 |
| **Vitals & Artifacts** | `?panel=vitals` | 両者 | Build log、simulation 結果、firmware artifact ダウンロード |
| **Decision Queue** | `?panel=decisions` | 両者 | pending 決定待機 + history | ⭐ |
| **Budget Strategy** | `?panel=budget` | PM | 4 戦略カード × 5 tuning knob で token / コスト制御 |
| **Pipeline Timeline** | `?panel=timeline` | 両者 | 水平タイムライン、現在進捗マーカー、ETA |
| **Decision Rules** | `?panel=rules` | 両者 | severity/mode デフォルトを上書きするオペレーター定義ルール |

## ディープリンク早見表

URL パラメータはリロード後も保持され、チームメイトとの共有可能です。

```
/?panel=decisions                     ← Decision Queue を開く
/?decision=dec-abc123                 ← Queue を開き該当決定へスクロール
/?panel=timeline&decision=dec-abc123  ← Timeline 表示、決定はキューのまま
```

不正な `?panel=` 値は Orchestrator panel にフォールバック(クラッシュしません)。

## スマホ / タブレットナビゲーション

画面幅が `lg` breakpoint (1024 px) 未満の場合:

- 12 panel が 1 カラムスクロールに折り畳まれる
- **下部 nav bar**: ← 前の panel、中央 pill (タップで全メニュー)、
  → 次の panel、各 panel に対応するドット列
- ドットのタップ領域は 44 × 44 px (視覚は 8 px)、WCAG 2.5.5 準拠

## キーボードショートカット (Decision Queue / Toast 内)

- **A** — focus 中 / 最新決定をデフォルト選択肢で承認
- **R** — focus 中の決定を拒否
- **Esc** — 現在の toast を閉じる(何も実行せず)
- **← / →** もしくは **Home / End** — Decision Queue の
  PENDING / HISTORY tab 切替

## 内部実装

- Panel 登録: `app/page.tsx · VALID_PANELS` と `readPanelFromUrl()`
- URL 同期: `app/page.tsx` の `useEffect` が `history.replaceState`
  で `activePanel` を `?panel=` にバインド
- スマホナビ: `components/omnisight/mobile-nav.tsx`

## 関連ドキュメント

- [Operation Modes](operation-modes.md)
- [Decision Severity](decision-severity.md)
- [Glossary](glossary.md) — NPI / Singularity Sync の意味が不明な時
