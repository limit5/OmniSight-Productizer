# Runner Operations SOP — how to drive the AI runner fleet

**Audience**: operators + agents who need to make the AI runners do work, monitor them, and rescue them — without guessing. This is the **single entry point**; it links to the deep-dive docs for each topic. Established 2026-05-22.

> One-line mental model: **the runners are a pull-based fleet.** You don't "assign" work — you make a JIRA ticket *pickable*, and an idle runner of the matching class claims it, does it in a throwaway clone, and pushes a Gerrit change for a **human +2**. Almost everything below is about (a) making tickets pickable correctly and (b) unsticking them when they aren't.

---

## 1. The fleet

| Fact | Value |
|---|---|
| Instances | `runner-claude@1`, `runner-claude@2`, `runner-codex@1`, `runner-codex@2` (Family-F user-systemd; `Linger=yes` → survive reboot) |
| Class routing | claude runners pick **`class:subscription-claude`** tickets; codex runners pick **`class:subscription-codex`**. A ticket runs ONLY on its class's runners. |
| Max parallelism | 4 (2 claude + 2 codex), but bounded — see §9 |
| Entry point | `scripts/runner-wrapper/run-ephemeral.sh <instance> <class>` (each cycle = fresh clone of `develop` in `/tmp/runner-workspaces/<instance>/`) |
| Logs | `/tmp/runner-<class>-<n>.log` (e.g. `/tmp/runner-claude-1.log`) |
| Hard dependency | the **gerrit-jira-bridge heartbeat** — if stale, ALL runners silently abstain (`bridge_down`). See `[[project_bridge_heartbeat_tmpfs_trap]]` / `docs/sop/bridge-health-graded-contract.md`. |
| Sandbox | currently `sandbox=degraded` (bubblewrap not installed) — runs raw CLI. `docs/sop/runner-sandbox.md`. |

### Fleet control
```bash
systemctl --user status 'runner-claude@*' 'runner-codex@*'        # health
systemctl --user restart runner-codex@1.service                   # bounce one
systemctl --user stop 'runner-claude@*' 'runner-codex@*'          # pause the fleet (e.g. during a big batch-file)
```
A runner cycling every ~30s with `no candidate passed pre-pickup` = healthy + idle (nothing pickable for its class), NOT broken.

---

## 2. Ticket lifecycle (what a runner does, end to end)

```
JIRA ticket pickable ─▶ runner claims (fencing-token mutex) ─▶ fresh clone of develop
   ─▶ reads ticket (Goal/Files/Spec-ref/AC + area boundary) ─▶ CLI does the work
   ─▶ runs tests ─▶ commit ─▶ push HEAD:refs/for/<target> (Gerrit)  ─▶ ticket → Under Review
   ─▶ **HUMAN +2** ─▶ submit/merge ─▶ bridge moves ticket → 公開済み (Published/Done)
```
Two gates the operator owns: **(a) making it pickable** (§3–5) and **(b) the +2** (§7).

---

## 3. What makes a ticket PICKABLE (the gate)

Pickup JQL (`jira_dispatch.py:357`): `issuetype = Story AND status = "To Do" AND assignee is EMPTY AND labels = "class:subscription-<runner>" AND labels not in ("tier:X")`.

So a runner-pickable ticket needs ALL of:
- **issuetype = Story** (not Task; `type:meta` routes to read-only safe-default → won't do code)
- **`class:subscription-claude`** or **`class:subscription-codex`** (which runner) + **`agent:auto`**
- **status `To Do`**, **assignee empty**, **tier ≠ X** (tier:X = human-only, excluded)
- the right **`area:*`** labels (9-value whitelist: backend/frontend/devops/tests/db/docs/security/embedded/tooling/ci/gerrit) — see §6
- **`capability:enable=gerrit_push`** if it needs to push (the safe-default is read-only)
- **all blockers resolved** — and "resolved" means status **公開済み / Published ONLY** (see §5 — Archived does NOT count)

If a runner says `no candidate passed pre-pickup` while you expect it to work, one of the above is failing.

---

## 4. Filing a ticket a runner can actually do

Use `scripts/file_jira_ticket.py` (it self-validates + aborts on area mismatch, so a bad ticket is never created). Full rules: **`docs/sop/runner-ticket-filing-checklist.md`** + `docs/sop/jira-ticket-conventions.md`.

Minimum recipe:
```bash
python3 scripts/file_jira_ticket.py \
  --summary "[BOT][<scope>] <imperative title with the guardrail in it>" \
  --description-file <md> --priority High --tier M \
  --class subscription-claude|subscription-codex --type feature|bug|docs \
  --areas <comma list spanning EVERY file the ACs touch> --scope <scope-label>
```
The description MUST give a **scope-anchor** so the runner doesn't drift (`docs/sop/runner-ticket-filing-checklist.md`):
```
## Goal           (one sentence; explicitly exclude sibling work)
## Files / Paths  (exact paths incl tests — drives the runner's file-mutex + boundary)
## Spec references(point at the design doc/section, committed to develop so the clone can read it)
## Out of scope   (name the sibling ticket that owns adjacent work)
## Acceptance     (4-AC: Code/Deploy/Integration/Exercised + Go-Live; each runtime AC has a machine `verify:` block)
```
Hard rules that bite:
- **`area:*` must span every directory the ACs touch** or the runner halts mid-ticket (`§11` revert). A `## Spec references` line pointing at a `docs/` file means you need `area:docs` too.
- **Spec-ref docs must be committed to develop** — the runner clones fresh; uncommitted local docs are invisible to it.
- **`type:meta` only for true roll-ups with children** (else it routes read-only).
- Tier:S backend pushes need explicit `capability:enable=gerrit_push`.

---

## 5. Dependencies (blockedBy) — two traps

**Trap A — direction.** A "Blocks" link is `inwardIssue = the BLOCKER, outwardIssue = the BLOCKED`. Use the intent-safe helper `file_coordinator.add_blocked_by(client, blocked_key=…, blocker_key=…)`, or if POSTing `/rest/api/3/issueLink` directly: `inwardIssue = blocker`, `outwardIssue = blocked`. **Always GET-verify after** (the blocked ticket should read `"is blocked by" <blocker>`).

**Trap B — wire blockedBy BEFORE the ticket is pickable, not after.** Pickup JQL does NOT check blockers, but the **pre-submit TOCTOU check does** (`has_unresolved_blockedby`). If you add a blockedBy link *after* a runner already grabbed the ticket, it does the work then aborts at submit (`abort_newly_blocked`) and stamps `runner-blocked:*` + `runner-stoploss:*` → stuck. So: **file the ticket with its blockers already wired**, or pause the fleet (`systemctl --user stop`) during batch filing.

**🚨 Blocker "resolved" = 公開済み / Published ONLY.** `has_unresolved_blockedby` uses `PUBLISHED_STATES = {"公開済み","Published"}` (`file_coordinator.py:21`). **`Archived` does NOT count** even though its statusCategory is "done". So:
- **Close [OP]/gate tickets at 公開済み, NEVER Archived** — an Archived blocker silently wedges every downstream ticket.
- If you already archived one, either remove the now-pointless blockedBy edges, or walk it Archived→To Do→…→公開済み.

---

## 6. Recognised areas + the filing-tool drift

Runner whitelist (`auto-runner-jira.py:~143`): `backend, frontend, devops, tests, db, docs, security, embedded, tooling` (+ `ci`, `gerrit` after OP-1559). An **unknown `area:*` label → silent infinite pre-pickup loop, no JIRA signal** — detect by tailing the runner log. Path→area mapping for decomposition: `docs/sop/sprint-s12-ticket-decomposition-rules.md`.

---

## 7. The human +2 gate (the real throughput limiter)

Runners push to `refs/for/<branch>` (a Gerrit review ref — allowed for bots; the O10 lockdown only blocks `pushMerge`/direct-to-branch). **AI bots can score at most +1; a human +2 is required to merge** (CLAUDE.md L1). So:
- Runners parallelise the *doing*, but every change waits on **your +2**. That's the bottleneck, by design.
- Find what's waiting: `gerrit query status:open project:omnisight/OmniSight-Productizer` (ssh `-i ~/.config/omnisight/gerrit-claude-bot-ed25519 -p 29418 claude-bot@sora.services`), or JIRA tickets in **Under Review**.
- **Operators pushing a doc/fix themselves** use the same bot path: `GIT_SSH_COMMAND="ssh -i ~/.config/omnisight/gerrit-claude-bot-ed25519 -o IdentitiesOnly=yes" git push gerrit HEAD:refs/for/develop` (don't need sora's identity; the human +2 is the gate regardless of who pushed).

---

## 8. Monitoring
```bash
# what each runner is doing right now
for f in /tmp/runner-claude-{1,2}.log /tmp/runner-codex-{1,2}.log; do echo "$f:"; tail -3 "$f"; done
# JIRA: in-progress / under-review / stuck for a scope
jql='labels = scope:<X> AND status in (進行中,"Under Review")'      # working
jql='labels = scope:<X> AND (labels in (runner-stoploss) OR labels = "runner-blocked:...")'  # stuck
```
Heartbeat (if ALL runners idle unexpectedly): `stat -c '%y' ~/.local/state/omnisight-bridge/heartbeat` should be < 60s old.

---

## 9. Parallelism — the 3 ceilings
1. **Class split** — claude tickets only on 2 claude runners, codex only on 2 codex. A lopsided unblocked set → only 2 busy. Rebalance `class:*` on queued tickets to feed both pairs.
2. **blockedBy** — only unblocked tickets are pickable; a wide fan-out (independent chains) parallelises, a join (one ticket blockedBy many) serialises.
3. **file-mutex** — siblings editing the same file can't run concurrently (the runner predicts overlap from `## Files / Paths`). Give each sibling distinct files + an `Out of scope` line.

---

## 10. Failure modes + rescue (symptom → cause → fix)

Detailed rescue tooling: **`docs/sop/runner-rescue-cli.md`**. Common cases:

| Symptom | Cause | Fix |
|---|---|---|
| All runners idle, `no candidate` | bridge heartbeat stale | restart bridge; verify `~/.local/state/omnisight-bridge/heartbeat` fresh |
| One ticket `no candidate`-skipped forever | stale `runner-blocked:*` / `runner-stoploss:*` / `claim:*` label (not auto-cleared) | strip the label(s) via JIRA labels PUT (verify no in-flight work first — `docs/sop/runner-rescue-cli.md`) |
| Ticket stuck after doing the work (`abort_newly_blocked`) | blockedBy link added after pickup (Trap B) | clear stoploss/blocked labels; ensure blockers are 公開済み |
| Downstream never unblocks though blocker "done" | blocker is **Archived**, not 公開済み (§5) | re-status blocker to 公開済み, or remove the edge |
| Unknown-area silent loop | `area:*` not in whitelist (§6) | re-label to a valid area, or add the area via tooling |
| Ticket claimed but no Gerrit change + stuck In Progress | push-setup fail / empty-tree rebase (OP-1400 class) | check runner log; strip `claim:*`; revert to To Do |
| Runner abstains on already-done work, loops to stoploss | "already-shipped abstain" (OP-1401) | strip stoploss + add `runner-blocked:already-shipped-<date>` |
| `claude CLI workspace-tampered` revert loop on a specific ticket | known claude-CLI sentinel bug | switch the ticket to codex class |

**Golden rescue rule**: a "stuck" ticket may have a runner mid-submit — before reverting status, check `git reflog` + sibling worktrees + all runner logs (`[[feedback_rescue_race_check_inflight_work]]`). Non-destructive rescue = strip `claim:*` only; **never unset assignee on a ticket a runner may be working** (TOCTOU storm).

---

## 11. See also (deep dives)
- Filing: `docs/sop/runner-ticket-filing-checklist.md`, `docs/sop/jira-ticket-conventions.md`, `docs/sop/jira-label-conventions.md`
- Fleet provisioning: `docs/operations/multi-instance-runner-runbook.md`, `docs/sop/multi-instance-runner-provisioning.md`
- Mutex/claims: `docs/sop/runner-pickup-mutex.md` · Rescue: `docs/sop/runner-rescue-cli.md`
- Capability: `docs/sop/capability-matrix-fallback-policy.md`, `config/capability_matrix.yaml`
- Substrate/state: `docs/sop/runner-substrate-contract.md`, `docs/sop/runner-state-authority-precedence.md`
- Sandbox: `docs/sop/runner-sandbox.md` · Artifacts: `docs/sop/runner-runtime-artifacts.md`
