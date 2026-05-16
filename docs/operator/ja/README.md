# OmniSight オペレーターガイド — 日本語

このガイドは、コマンドセンターを実際に使う人向けです(作った人向けでは
ありません)。「このボタンは何をするのか」「タスクがなぜ止まったのか」
といった疑問に平易な言葉で答え、ソースコードへのリンクは深掘りが
必要な場合のみ提供します。

## どこから読むべきか

| あなたの立場 | ここから |
|---|---|
| リリース可否を判断する PM / release captain | [Operation Modes](reference/operation-modes.md) → [Decision Severity](reference/decision-severity.md) |
| パイプラインを日常的に走らせる組込みエンジニア | [Panels Overview](reference/panels-overview.md) → [Glossary](reference/glossary.md) |
| 特定画面で詰まっている | その panel ヘッダーの `?` アイコンをクリック |

## クイックマップ

- **Reference** (`reference/`) — 正確・網羅的・常に最新
- **Glossary** — agent / task / pipeline / NPI の本システムでの定義
- **Troubleshooting**(近日公開)— 赤いバナーが出た時の対処
- **Tutorials**(近日公開)— Invoke をゼロから一度走らせるハンズオン

## Agent アイデンティティと育成システム(Priority RPG)

agent はもはやステートレスな runner ではなく、それぞれが Guild ・
レベル・ XP ・タレント・スキル / ツール熟練度を持つキャラクター
カード(Character Card)を備えています。日常運用の how-to(ロスター閲覧・
Guild 選択・XP の読み方)は
[Agent RPG システム運用ガイド](../../operations/agent-rpg-system.md)を、
設計理由と背後のスキーマ固定は
[ADR-0008 — Agent RPG Class & Skill Leveling System](../../adr/0008-agent-rpg-class-skill-leveling.md)
を参照してください(正本仕様 — 本ガイドと ADR が食い違う場合は ADR が
優先)。

## 多言語版

本ドキュメントは英語(`en/`、正本)、繁体字中国語(`zh-TW/`)、
簡体字中国語(`zh-CN/`)、日本語(`ja/`、本ファイル)で同期維持されています。
英語が正本で他言語は翻訳です。翻訳が遅れている場合、各ファイル冒頭の
`source_en:` ヘッダーが対応する英語版リビジョン日を示します。
