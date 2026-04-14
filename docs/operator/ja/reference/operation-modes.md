# Operation Modes — 画面最上部の MODE pill

> **source_en:** 2026-04-14 · Phase 50-Fix · authoritative

## TL;DR — PM 向け

MODE は **AI があなたに聞かずにどこまでやっていいか** を決めます。
4 段階、「全てを聞く」から「全部やる、間違ってたら止める」まで。
アイコンの色がリスクに対応します。

| Mode | アイコン色 | 一言で言うと |
|---|---|---|
| **MANUAL** (MAN) | シアン | 毎ステップ承認が必要 |
| **SUPERVISED** (SUP) | 青 | 通常作業は自動、リスクがあるものは停止 — **デフォルト** |
| **FULL AUTO** (AUT) | アンバー | 破壊的操作のみ停止 |
| **TURBO** (TRB) | 赤 | 全て自動(破壊的含む)、60 秒の取消猶予あり |

切り替えは即時に全接続ブラウザ(デスクトップ・スマホ・タブレット)に
反映されます。

## Decision Severity との相互作用

AI が行おうとする全ての事項には 4 種の severity タグが付きます
(詳細は [Decision Severity](decision-severity.md))。MODE はこの表
から該当する行を選ぶ役割です:

| Severity ↓ / Mode → | MANUAL | SUPERVISED | FULL AUTO | TURBO |
|---|---|---|---|---|
| `info`(読込・ログ) | キュー | 自動 | 自動 | 自動 |
| `routine`(通常書込) | キュー | 自動 | 自動 | 自動 |
| `risky`(復旧可能書込) | キュー | キュー | 自動 | 自動 |
| `destructive`(ship / deploy / 削除) | キュー | キュー | キュー | 自動(60 秒タイマー) |

「キュー」は **Decision Queue** panel に表示され、あなたの承認なしに
AI は先に進みません。

## 並列度バジェット

MODE は同時稼働 agent 数も制御します。pill の横に `in_flight / cap`
として表示されます。

| Mode | 並列上限 |
|---|---|
| MANUAL | 1 |
| SUPERVISED | 2 |
| FULL AUTO | 4 |
| TURBO | 8 |

並列度が高いほどスループット向上しますが、token 消費も増えます。
token 予算が厳しい場合は **Budget Strategy** を先に調整してから
MODE を上げてください。

## よくある場面

- **退社時・夜間放置** — MANUAL に切替。想定外の決定が走らず、
  未解決事項は朝一で処理できます。
- **日常開発** — SUPERVISED が最適。AI が通常作業(読込、ツール呼出、
  分析)を進め、不可逆操作前に停止します。
- **デモ前追込み** — FULL AUTO。破壊的 push のみ承認要求、他は止まらず。
- **週末バッチリファクタ** — TURBO + スマホ toast で 60 秒カウントダウン
  監視。異変を感じたら Emergency Stop。

## MODE を変更できる人

バックエンド `.env` に `OMNISIGHT_DECISION_BEARER` が設定されている場合、
その token を API で提示した呼出元のみが MODE を切替可能です
(UI は localStorage から token を読取)。未設定なら、バックエンドに
到達可能な全員が操作可能 — ローカル単独利用なら OK、共有環境では非推奨。

## 内部実装

- フロント: `components/omnisight/mode-selector.tsx` — セグメント pill +
  SSE 購読で全 tab を同期
- バックエンド: `backend/decision_engine.py` · `set_mode()` /
  `get_mode()` · `should_auto_execute(severity)` が上記マトリクス
- イベント: 切替時に `mode_changed` を SSE bus に発行。
  schema は `GET /api/v1/system/sse-schema` で取得可
- 永続化: **再起動をまたいで保持されません** — 再起動後は
  SUPERVISED にリセットされます。将来 phase で対応予定。

## 関連ドキュメント

- [Decision Severity](decision-severity.md) — `risky` と
  `destructive` の違い
- [Budget Strategies](budget-strategies.md) — MODE の隣の
  token / コスト調整器
- [Panels Overview](panels-overview.md) — キューに溜まった決定を
  どこで見るか
