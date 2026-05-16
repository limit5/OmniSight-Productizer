# OmniSight 操作指南 — 简体中文

这份指南写给实际使用 command center 的人，不是给开发它的人看的。
用白话回答"这个按钮是做什么的？""我的 task 怎么停了？"之类的问题，
只在您想深入时才指向源代码。

## 从哪一段开始看

| 如果您是…… | 从这里开始 |
|---|---|
| 决定是否 ship 的产品负责人 / release captain | [Operation Modes](reference/operation-modes.md) → [Decision Severity](reference/decision-severity.md) |
| 日常跑 pipeline 的嵌入式工程师 | [Panels Overview](reference/panels-overview.md) → [Glossary](reference/glossary.md) |
| 卡在某个画面 | 点该 panel header 上的 `?` 图标 |

## 快速地图

- **Reference** (`reference/`) — 精确、完整、随时更新
- **Glossary** — agent / task / pipeline / NPI 在本系统的具体定义
- **Troubleshooting**（即将推出）— 出红色 banner 了怎么办
- **Tutorials**（即将推出）— 从零开始跑一次 Invoke 的手把手教学

## Agent 身份与升级系统（Priority RPG）

Agent 不再只是无状态的 runner — 每个 agent 都有自己的角色卡
（Character Card），包含 Guild、level、XP、talent，以及每个 skill /
tool 的熟练度。日常操作 how-to（查看 roster、选 Guild、看 XP）请见
[Agent RPG 系统操作指南](../../operations/agent-rpg-system.md)；
设计理由与背后的 schema lock-in 请见
[ADR-0008 — Agent RPG Class & Skill Leveling System](../../adr/0008-agent-rpg-class-skill-leveling.md)
（权威规格 — 本指南与 ADR 不一致时以 ADR 为准）。

## 多语版本

本文档同步维护英文（`en/`，权威源）、繁中（`zh-TW/`）、
简中（`zh-CN/`，本档）、日文（`ja/`）。英文为权威源，其余为翻译版。
翻译若落后，每个文件顶部的 `source_en:` 会标示对应的英文修订日期。
