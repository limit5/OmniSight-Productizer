# RPG 視覺文件（活文件）

自動生成的技能樹 + 角色總覽圖，反映當下的 character/skill/talent DB 狀態。

- `skill-tree.svg` — 公會 → 技能 → Lv3 分支，掛上角色習得狀態
- `roster.svg` — 每個角色的等級/XP/腦/技能/待選天賦

## 重生成
資料變動（招募新角色、練級、鎖分支/天賦）後重跑：

```
OMNISIGHT_DATABASE_URL=<prod-or-any-env-dsn> python3 scripts/rpg_diagrams.py
```

唯讀（不改任何表）；用 skill_matrix + character_def/agent_character_card/
agent_skill_state/agent_talent_choice 當資料來源。中文字型走 Noto Sans CJK TC。
