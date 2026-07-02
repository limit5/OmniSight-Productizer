# RPG 角色視覺設計 — 方向預留（operator 指定，2026-07-02）

**Status: RESERVED — 待與 operator 共同設計。本文件先錨定方向，避免任何人
（含 AI agent）在共同設計session之前擅自生成/決定角色外觀。**

## Operator 已拍板的方向（不可自行偏離）
- 全員 **男性** 角色。
- 年齡帶 **12–50 歲**，與技能/經驗值/等級 **完全無關**（純角色設計軸）。
- 風格分佈：**大部分 = 可愛正太形象**（cute young-boy）；**少部分 = 年紀稍長的
  青年/壯年**。
- 具體到每個角色（nova/vega/pixel/sage/rex/iris/argus/…）的年齡、外觀、配色、
  服裝、個性表現 → **之後與 operator 一起討論設計**，本文件屆時擴充為每角色
  規格表。

## 系統預留位（實作時機 = 共同設計完成後）
- `character_def` 未來擴充欄位（migration 屆時開票）：`appearance`（JSONB：
  age / style / palette / avatar_ref …）；招募 API/UI 的對應欄位。
- 頭像資產管線（生成/存放/AGENT MATRIX 與公會廳的顯示）屆時一併設計。
- 現有 `blurb` 僅承載職能描述，不放外觀設定。

## Out of scope（明確排除）
- 在共同設計前生成任何頭像/立繪。
- 把年齡/外觀接到任何能力、tier、路由邏輯（設計軸與能力軸嚴格分離）。
