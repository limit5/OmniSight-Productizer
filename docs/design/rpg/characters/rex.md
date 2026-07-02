# 角色規格 — Rex（雷克斯）· 值班小快手·SRE ✅定稿 2026-07-03

> 定稿立繪：`art/rex.png`（設定表：表情差分、SRE 裝備 details、三視圖、色票；motto「FIX IT. SHIP IT. SLEEP (LATER)」）。

> slug `rex`（不可改）· display_name「Rex」（內建角色，系統名暫維持 Rex；設計暱稱可用
> 「小霸」/「雷克斯」— rex=拉丁文「王」，一個 tier S 小不點卻守著整個基建的反差萌。
> 內建名要能改需另開「內建名 DB 覆蓋」小票。）
> 靈魂梗：SRE = on-call 值班救火 + 浣熊機靈拆修 + 黑眼罩紋＝值班黑眼圈。rex=王的反差。

## 系統屬性（既有，不動）
- 腦：Grok · 公會：sre（維運）· tier S · Lv1 / 尚無技能
- 定位：fast/cheap tooling/ops hand — 小而自足的維運/工具任務

## 設計軸套用
| 軸 | 選擇 |
|---|---|
| ① 年齡帶 | **正太 ~11歲**（3.3–3.7頭身，最小快手；元氣反差萌） |
| ② 腦色 | **桃紅/洋紅 `#ec4899`**（Grok）—— 全隊最跳、最元氣 |
| ③ 獸耳 | **浣熊耳**（灰+桃紅點綴），臉上**黑眼罩紋=值班黑眼圈** |
| ④ 公會裝備 | SRE：工具腰帶、on-call 呼叫器、小滅火器、監控儀表 |

## 形象細節（原創）
- **性格母題**：值班·救火·手腳快·隨時待命。永遠 on-call、睏但超拼。
- 髮：蓬亂灰+桃紅挑染短髮 + 翹呆毛（沒睡飽感）；瀏海下大眼。
- 眼：桃紅色大眼、機靈有神但**掛著淡淡黑眼圈**（值班熬夜）；小虎牙。
- 獸耳：灰底桃紅內耳的**浣熊耳**；**浣熊尾**（灰桃環紋、蓬大）。
- 臉：**浣熊黑眼罩紋**（横跨眼周）＝ on-call 黑眼圈雙關。
- 上身：短版**機能連帽背心/工作服**（袖捲起、隨性）+ 短T（印小小 uptime%）+ 頭上偶掛防塵護目鏡。
- 下身：機能工作短褲（多口袋）+ 短襪 + 耐磨工作短靴。
- **裝備（SRE）**：工具腰帶（迷你扳手/螺絲起子/束線帶/萬用表）、**on-call 呼叫器**（腰間閃紅光 PAGE!）、**小型滅火器**（救 incident）、**腕上監控儀表**（uptime / 綠紅健康燈）。
- 表情：元氣但帶睏意的值班小鬼,警報一響瞬間繃緊「！」。
- 氛圍母題：🔧扳手 · 🔔警報鈴 · 🔥小火苗 · 📟呼叫器 · 綠/紅健康燈。
- 姿勢（全身）：**待命衝刺姿**，一手抄工具/滅火器、一手按閃燈呼叫器，浣熊尾翹起隨時撲去救火。

## Prompt-ready 美術指導
```
masterpiece, best quality, cel shading, anime style, FULL BODY, full character
reference, standing on ground, feet fully visible, 1boy, solo, shota ~11 years old,
3.5 heads tall, energetic but slightly sleepy on-call expression, small fang,
bright MAGENTA-PINK eyes with faint dark under-eye circles, a RACCOON dark eye-mask
marking across his eyes,
grey RACCOON ears with magenta-pink inner-ear, messy grey hair with magenta-pink
highlights and a spiky ahoge, big fluffy grey-and-pink ringed raccoon tail,
dynamic on-call ready-to-dash pose, one hand grabbing a tool / small fire
extinguisher, other hand on a flashing red pager on his belt, raccoon tail up alert,
wearing a short hooded utility work-vest (rolled sleeves) over a short tee (tiny
uptime% print), dust goggles pushed up on his head, a tool belt (mini wrench,
screwdriver, zip-ties, multimeter), a flashing red on-call pager, a small fire
extinguisher, a wrist monitoring gauge (green/red health LEDs), functional cargo
shorts, worn work boots, fingerless gloves,
hot MAGENTA-PINK color scheme, floating wrench / alert-bell / small-flame / pager /
green-red-health-LED motifs around him, white sticker outline, soft grid-paper
background, scrappy energetic sleepy on-call SRE gremlin vibe
```
Negative: `(worst quality, low quality:1.4), bad anatomy, bad hands, extra fingers, deformed, cropped, cut off feet, text, watermark, multiple characters, female, realistic, 3d, cat ears, fox ears, dog ears`
