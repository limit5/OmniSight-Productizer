# RPG 老兵敘階（grandfather XP backfill）— 2026-07-02

**Why:** the character/XP ledger went live 2026-07-01, but the org's domain
experience (9 customer cases: cameras/UVC/BSP/security/mobile) predates it.
New domain-veteran hires get a one-time XP grant so the roster reflects real
accumulated experience instead of starting blind at Lv1.

**Rule (operator-approved):**
- Source of truth: JIRA `project=OP AND labels="area:<domain>" AND
  statusCategory=Done` counts at 2026-07-02.
- **Character XP grant = 10 xp per historical delivered ticket** ("documented
  legacy work counts at 1/10 of ledgered work" — keeps veterans senior without
  dwarfing honestly-ledgered characters).
- **Skill XP grant = 10 xp per keyword-matched historical ticket** (exact
  upsert, NO outcome/first-time multipliers — grants are not deliveries).
- One-time only; never re-run (this doc is the ledger). Future XP accrues
  exclusively through the live delivery path.

**Grants applied 2026-07-02:**
| character | guild | source count | char XP | skills |
|---|---|---|---|---|
| iris (new hire) | isp | area:embedded = 328 | 3280 | uvc: 39×10=390 (Lv4) · ipcam: 31×10=310 (Lv4) — branches left PENDING for the operator |
| argus (new hire) | auditor | area:security = 32 | 320 | security_audit: 32×10=320 (Lv4) — branch PENDING |
| (mobile hire) | mobile (pending guild, OP ticket filed) | mobile-keyword = 71 | 710 (applied after the guild lands) | mobile skill TBD |

Existing characters (nova/vega/pixel/sage/rex) receive NO grant — their ledgers
are honestly accrued and stay that way.
