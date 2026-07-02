#!/usr/bin/env python3
"""Regenerate the RPG visual docs (skill tree + roster) as SVG from live DB.

Reads the character roster / skills / talents from the same env DSN the runner
uses (OMNISIGHT_DATABASE_URL) and the canonical skill_matrix, then renders two
SVGs into docs/design/rpg/. Read-only. Re-run after roster changes:
    OMNISIGHT_DATABASE_URL=... python3 scripts/rpg_diagrams.py
"""
import os, sys, asyncio, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import matplotlib; matplotlib.use('Agg')
from matplotlib import font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

_FP=None
for c in ("/home/user/.local/share/fonts/NotoSansCJKtc-Regular.otf",
          "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"):
    if os.path.exists(c): _FP=c; break
if _FP:
    fm.fontManager.addfont(_FP); plt.rcParams['font.family']=fm.FontProperties(fname=_FP).get_name()

OUT=Path(__file__).resolve().parent.parent/"docs"/"design"/"rpg"
ICON_DIR=OUT/"characters"/"icon"
def _icon(slug):
    from matplotlib import image as mpimg
    fp=ICON_DIR/f"{slug}.png"
    return mpimg.imread(str(fp)) if fp.exists() else None
GC={'backend':'#3b82f6','frontend':'#10b981','sre':'#8b5cf6','isp':'#f59e0b',
    'auditor':'#ef4444','mobile':'#ec4899','red_team':'#b91c1c',
    'algo_cv':'#9ca3af','bsp':'#9ca3af','hal':'#9ca3af'}
GN={'backend':'後端','frontend':'前端','isp':'ISP影像','mobile':'行動','sre':'SRE維運',
    'auditor':'資安稽核','red_team':'紅隊','algo_cv':'演算法/CV','bsp':'BSP','hal':'HAL'}
BRAIN={'subscription-claude':'Claude','subscription-codex':'Codex','subscription-gemini':'Gemini','subscription-grok':'Grok'}
BRAINC={'Claude':'#d97706','Codex':'#10b981','Gemini':'#3b82f6','Grok':'#ec4899'}

async def fetch():
    import asyncpg
    from backend.agents.talent_tree import MILESTONE_LEVELS
    dsn=os.environ.get("OMNISIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn: raise SystemExit("set OMNISIGHT_DATABASE_URL")
    c=await asyncpg.connect(dsn)
    defs={r['slug']:dict(r) for r in await c.fetch("SELECT slug,display_name,brain,guild,max_tier FROM character_def ORDER BY slug")}
    cards={r['agent_id']:dict(r) for r in await c.fetch("SELECT agent_id,level,xp FROM agent_character_card")}
    sk={}
    for r in await c.fetch("SELECT agent_id,skill_id,level,skill_xp,branch_choice FROM agent_skill_state"):
        sk.setdefault(r['agent_id'],[]).append(dict(r))
    tal={}
    for r in await c.fetch("SELECT agent_id,milestone_level FROM agent_talent_choice"):
        tal.setdefault(r['agent_id'],[]).append(r['milestone_level'])
    await c.close()
    from backend.agents.skill_matrix import load_skill_matrix
    m=load_skill_matrix()
    guilds={ (g.value if hasattr(g,'value') else str(g)):
        [{"skill_id":d.skill_id,"name":d.display_name,"branches":[(b.branch_id,b.display_name) for b in d.branches]} for d in defs2]
        for g,defs2 in m.items() }
    holders={}
    for slug,rows in sk.items():
        for r in rows: holders.setdefault(r['skill_id'],[]).append({**r,'agent_id':slug})
    roster=[]
    for slug,dd in defs.items():
        co=cards.get(slug,{}); lv=co.get('level',1); locked=tal.get(slug,[])
        pend=[m2 for m2 in MILESTONE_LEVELS if lv>=m2 and m2 not in locked]
        roster.append({"slug":slug,"name":dd['display_name'],"brain":BRAIN.get(dd['brain'],dd['brain']),
            "guild":dd['guild'],"tier":dd['max_tier'],"level":lv,"xp":co.get('xp',0),
            "skills":sk.get(slug,[]),"pending":pend})
    roster.sort(key=lambda z:-z['level'])
    chars_by_guild={}
    for r in roster: chars_by_guild.setdefault(r['guild'],[]).append((r['slug'],r['name']))
    return guilds,holders,chars_by_guild,roster

def draw_skilltree(guilds,holders,chars):
    order=['backend','frontend','isp','mobile','sre','auditor','red_team','algo_cv','bsp','hal']
    fig,ax=plt.subplots(figsize=(20,13.5)); ax.axis('off'); ax.set_xlim(0,20); ax.set_ylim(0,13.5)
    ax.text(10,13.05,"OmniSight 技能樹 — 公會 → 技能 → Lv3 分支",ha='center',fontsize=21,weight='bold')
    ax.text(10,12.65,"實心=有角色習得 · ★=已鎖分支 · 灰底=尚無角色的公會",ha='center',fontsize=11,color='#555')
    cw=9.5; xc=[0.35,10.15]; yc=[12.1,12.1]
    for gi,g in enumerate(order):
        col=gi%2; x=xc[col]; y=yc[col]; c=GC.get(g,'#9ca3af'); active=bool(chars.get(g))
        chs="  ".join(n for s,n in chars.get(g,[])) or "（尚無角色）"
        ax.add_patch(FancyBboxPatch((x,y-0.5),cw,0.5,boxstyle="round,pad=0.02",fc=c,ec='none',alpha=0.95 if active else 0.4))
        ax.text(x+0.15,y-0.25,f"{GN.get(g,g)} · {g}",fontsize=12.5,weight='bold',color='white',va='center')
        ax.text(x+cw-0.15,y-0.25,chs,fontsize=9.5,color='white',va='center',ha='right')
        yy=y-0.68
        for sk in guilds[g]:
            hs=holders.get(sk['skill_id'],[]); f=bool(hs)
            ax.add_patch(FancyBboxPatch((x+0.28,yy-0.32),cw-0.5,0.34,boxstyle="round,pad=0.01",fc=c if f else '#f3f4f6',ec=c,alpha=0.85 if f else 0.45,lw=1.2))
            ht="  ".join(f"{h['agent_id']} Lv{h['level']}" for h in hs)
            ax.text(x+0.42,yy-0.15,f"◆ {sk['name']}"+(f"    〔{ht}〕" if ht else "    · 空 ·"),fontsize=10,color='white' if f else '#777',va='center',weight='bold' if f else 'normal')
            yy-=0.42
            for bid,bn in sk['branches']:
                ch=[h['agent_id'] for h in hs if h['branch_choice']==bid]
                ax.text(x+0.85,yy-0.12,f"└ {bn}"+("  ★ "+",".join(ch) if ch else ""),fontsize=8.6,color=(c if ch else '#9a9a9a'),va='center',weight='bold' if ch else 'normal')
                yy-=0.30
            yy-=0.05
        yc[col]=yy-0.22
    fig.savefig(OUT/'skill-tree.svg',bbox_inches='tight',facecolor='white'); plt.close(fig)

def draw_roster(roster):
    n=len(roster); cols=2; rows=(n+1)//2
    fig,ax=plt.subplots(figsize=(18, 2.55*rows+1)); ax.axis('off'); ax.set_xlim(0,18); ax.set_ylim(0,2.55*rows+1)
    H=2.55*rows+1
    ax.text(9,H-0.4,"OmniSight 角色總覽 — 等級 · 技能 · 天賦",ha='center',fontsize=21,weight='bold')
    ax.text(9,H-0.78,"⚡=待選天賦(過里程碑未選) · ★=已鎖技能分支",ha='center',fontsize=11,color='#555')
    cw=8.7
    for i,r in enumerate(roster):
        col=i%2; row=i//2; x=0.3+col*9.1; y=H-1.5-row*2.5
        c=GC.get(r['guild'],'#9ca3af'); bc=BRAINC.get(r['brain'],'#666')
        ax.add_patch(FancyBboxPatch((x,y-2.2),cw,2.2,boxstyle="round,pad=0.03",fc='#ffffff',ec=c,lw=2.2))
        ax.add_patch(FancyBboxPatch((x,y-0.44),cw,0.44,boxstyle="round,pad=0.02",fc=c,ec='none'))
        _av=_icon(r['slug'])
        if _av is not None:
            ax.imshow(_av, extent=(x+0.15, x+1.65, y-2.05, y-0.55), zorder=5, aspect='auto')
            ax.add_patch(FancyBboxPatch((x+0.15,y-2.05),1.5,1.5,boxstyle="round,pad=0.0",fill=False,ec=c,lw=2,zorder=6))
        _nx = x+1.85 if _av is not None else x+0.2
        ax.text(_nx,y-0.22,f"{r['name']}  ·  {r['slug']}",fontsize=13,weight='bold',color='white',va='center')
        ax.text(x+cw-0.2,y-0.22,f"{GN.get(r['guild'],r['guild'])} / tier{r['tier']}",fontsize=9.5,color='white',va='center',ha='right')
        # brain chip
        ax.add_patch(FancyBboxPatch((x+1.85,y-0.86),1.5,0.32,boxstyle="round,pad=0.02",fc=bc,ec='none',alpha=0.9))
        ax.text(x+2.6,y-0.70,r['brain'],fontsize=9,color='white',ha='center',va='center',weight='bold')
        ax.text(x+3.55,y-0.70,f"Lv {r['level']}   ·   {r['xp']} xp",fontsize=11,va='center',weight='bold',color='#222')
        if r['pending']:
            ax.add_patch(FancyBboxPatch((x+cw-2.2,y-0.86),2.0,0.32,boxstyle="round,pad=0.02",fc='#fde047',ec='#ca8a04'))
            ax.text(x+cw-1.2,y-0.70,f"⚡ 待選天賦 Lv{','.join(map(str,r['pending']))}",fontsize=8.2,ha='center',va='center',color='#713f12',weight='bold')
        sy=y-1.12
        _sx = x+1.85 if _icon(r['slug']) is not None else x+0.3
        if r['skills']:
            for s in r['skills']:
                br=f"  ★{s['branch_choice']}" if s['branch_choice'] else ""
                ax.text(_sx,sy,f"◆ {s['skill_id']}  Lv{s['level']} ({s['skill_xp']}xp){br}",fontsize=9,va='center',color='#333')
                sy-=0.24
        else:
            ax.text(_sx,sy,"（尚無技能 — 交付 in-guild skill: 票即可開始練）",fontsize=9,va='center',color='#999',style='italic')
    fig.savefig(OUT/'roster.svg',bbox_inches='tight',facecolor='white'); plt.close(fig)

def main():
    g,h,cbg,roster=asyncio.run(fetch())
    draw_skilltree(g,h,cbg); draw_roster(roster)
    print(f"wrote {OUT}/skill-tree.svg + roster.svg ({len(roster)} chars)")
if __name__=="__main__": main()
