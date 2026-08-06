# Pre-reboot handoff — 2026-08-06

Written immediately before a full host reboot. Everything below is state that either
would not survive, or that the next session needs in order to continue without re-deriving.

---

## 1. THE ONE THING THAT IS UNFINISHED

**The 3D-memory deep audit ran and its findings are preserved, but the report was never
properly synthesised.**

- 116 agents produced **165 findings** across 12 dimensions, with **158 adversarial
  verdicts**. All of it is saved (see §2).
- The first synthesis was written from **~16% of the corpus** — I passed its input through
  `.slice(0, 60000)` against ~375,000 chars. The report itself detected this and says so:
  *"I received material from three of the nine dimensions by name."* Treat
  `2026-08-01-3d-memory-system-deep-audit.md` as a partial report, not the audit.
- The corrected re-synthesis (hierarchical: per-dimension compaction → final) **failed on a
  session limit**, 61 of 125 agents erroring. `report: null`.

**To finish it after the reboot** — the expensive part does not need repeating:
```
Workflow({ scriptPath: "docs/audit/2026-08-01-3d-memory-audit-workflow.js",
           resumeFromRunId: "wf_1323d1d9-b6a" })
```
The 116 audit+verify agents replay from cache; only the compaction and final synthesis run.
If the cache is gone after the reboot, `2026-08-01-3d-memory-audit-RAW-FINDINGS.md` contains
every finding in full and a synthesis can be written straight from it.

---

## 2. ARTEFACTS PRESERVED (all under `docs/audit/`)

| file | what it is |
|---|---|
| `2026-08-01-3d-memory-audit-RAW-FINDINGS.md` | **all 165 findings**, per dimension, with evidence/expected-vs-actual/impact/silent flags. 368 KB. The durable record. |
| `2026-08-01-3d-memory-audit-journal.jsonl.gz` | raw agent-by-agent journal (116 results). Lets the synthesis be redone verbatim. |
| `2026-08-01-3d-memory-audit-workflow.js` | the workflow script, already corrected to the non-truncating hierarchical synthesis. |
| `2026-08-01-3d-memory-system-deep-audit.md` | the PARTIAL first report — useful, but written from 16% of the data. Do not cite as the audit. |

Runner logs: `/tmp/runner-*.log` (6 files, **4.4 GB**) are the only durable runner history
and are lost on reboot. Compressed archive started before the reboot at
`~/work/sora/logs/runner-archive/2026-08-06-pre-reboot/` — a `.complete` marker file appears
when the gzip loop finishes. **If that marker is absent after the reboot, the archive is
partial and the tail of the history is gone.**

---

## 3. AUDIT HEADLINES WORTH ACTING ON (from the partial report, independently re-verified)

These four I checked myself, not just accepted from an agent:

1. **No alert delivery path exists — every anti-hollow alert fires into a void.** Verified
   this pass: `activeAlertmanagers: 0`, zero Alertmanager containers, and the config
   Prometheus actually loaded is 346 bytes with **no `alerting:` block** while the host file
   is 607 bytes **with** one. Single-file bind mount, inode replaced after container start;
   Prometheus has been up since 2026-07-09 and OP-2563 wired alerting on 07-10. One rule
   group loaded, 4 rules, `ProjectStateNoTraffic` permanently firing on a metric no code
   produces.
2. **Leg-2's terminal arm has never executed anywhere.** `OMNISIGHT_RUNNER_LEARNED_ITEMS`
   is set in no env file, no container, no live process; `learned_item_citations`,
   `learned_item_versions`, `memory_publications`, `memory_transition_events` are all **0
   rows in prod**. Injection → citation → utility → revocation is a half-loop.
3. **Leg-3's drift gauge cannot move.** `omnisight_claude_memory_drift` has a definition and
   zero `.set()` calls, reads 0 on both replicas — while the mirror is 9 days stale and
   missing 4 memories including `MEMORY.md` itself.
4. **Leg-1 has written two rows, ever**, with its own predicted hollow signature (frozen
   count + growing age, now ~10 days) visible in Prometheus and no rule watching it.

The shape to notice: **the anti-hollow instrumentation built to prevent a recurrence of the
2026-07-07 "silent hollow" is itself the hollowest layer.** Same defect, one level up.

---

## 4. STATE OF THE FAILED-UNIT SWEEP (`scope:failed-units-2026-07-25`)

Closed: OP-2728, 2729, 2730, 2731, 2732, 2733, 2735, 2736(analysis), 2737, 2742, 2759, 2760.
The DSAR finding became its own epic: META **OP-2747** + 11 children (OP-2745/2746/2748/2749,
OP-2750–2758), all `tier:X`.

Still open and needing an operator decision, not more analysis:
- **OP-2736** — 9 text columns need anonymisation strategies (a data-protection judgement).
  `staging-pg-snapshot.timer` has `Persistent=true` + a stamp file, so **re-enabling fires
  the missed DROP DATABASE immediately.** Leave disabled.
- **OP-2739 AC1/AC4** — `/home/user/sora-bridge` is still dirty and 240 commits behind, so
  `sora-bridge-sync` is now correctly RED daily (that is the fix working). Fast-forwarding
  changes code under `pipeline-coordinator`, which executes from that tree — sequence it.
- **OP-2734** — ~68 GB genuinely reclaimable (images / build cache / stopped containers).
  **Do NOT bulk-prune:** ~64 GB of the "stale CI cache" is the `vendor-mirrors-nda` ATK
  RK3588 SDK mirror, with no second copy found on this host, owned by an active runner.
- **OP-2763** — the alert raised by sora-bridge-sync's new honest failure.
- **OP-2738** — merged; **OP-2734** untouched.

---

## 5. POST-REBOOT CHECKLIST

Ordered by "what silently stays broken if nobody looks".

1. **NAS first.** `sora.services` hosts Gerrit/GitLab/registry and needs a manual power-on
   after power loss. Signature of it being down: ping OK but all TCP blocked, runners
   reporting "No route to host". Check `:29418` (Gerrit), `:49154` (GitLab), `:49160`
   (registry) before concluding anything else is broken.
2. **The runner-log archive** — confirm `.complete` exists (§2).
3. **Coordinator pair.** `pipeline-coordinator` + `-watchdog`. Known trap: ≥3 crashes in
   5 min gives `failed (start-limit-hit)` with **no auto-recovery** — needs
   `systemctl --user reset-failed` then `start`. The watchdog now runs the tested Python
   module (OP-2735) and no longer starts the daemon it supervises.
4. **Backup lanes.** All three should self-recover on their timers: `omnisight-prod-backup`
   02:17, `omnisight-kek-escrow` 02:40, `omnisight-dr-drill` 04:30,
   `omnisight-pgdump-{local,s3}-daily` 11:00 local, `omnisight-backup-freshness` 06:10.
   The two new units now carry `After=network-online.target docker.service` precisely so a
   post-boot catch-up run does not fire before docker is up.
5. **Expect these to be red, and do not "fix" them:** `sora-bridge-sync` (by design until
   the tree is reconciled), `gitlab-cr-monitor` (expired GitLab token, pre-existing),
   `uvc-gitlab-sync` (chronic stopgap mirror).
6. **Staging** is hand-deployed now — `staging-sync` was retired (OP-2737), so nothing
   redeploys it automatically. Procedure: `docs/operations/staging-deploy-by-hand.md`.
   Tag format is `sha-<40hex>`, not the bare SHA.

---

## 6. THE TWO OPEN MEMORY QUESTIONS THE OPERATOR ASKED

Both were answered with evidence, both have an agreed action that is **not yet done**:

- **2(a) ingest** — the file store is 6 days ahead of the DB (`newest body in DB
  2026-07-23`, newest file change 07-29). Ingest is a **CLI, not the browser task** — I had
  this wrong initially and corrected it. Token present at
  `~/.config/omnisight/claude-memory-prod-key.txt`. Running it makes those 6 days durable,
  because DB rows are inside the encrypted off-site backup. It only adds *quarantined*
  versions; it publishes nothing.
- **2(b) file-store backup** — `~/.claude/projects/*/memory/` is covered by **no** backup
  lane. There are **5 real project stores** (Productizer 181 files, fate 40, capsule 6,
  uvc-uac-app 3, earthquake-system 3) and **54 throwaway runner-workspace dirs** that should
  be excluded from any backup.

Operator approved doing both; neither was started before the reboot.

---

## 7. A STANDING CAUTION FOR THE NEXT SESSION

This session's most repeated failure was not in the system — it was in the checks used to
*verify* the system. Eight separate verification harnesses produced confident, well-formatted
output that was simply wrong (`$?` from a pipeline, `grep -Ff` with an empty pattern file, a
bad SQL cast silenced by `2>/dev/null`, a test that died before creating anything, `setsid` +
`$!` killing the wrong process, an auth-denied registry listing read as "empty", a YAML list
parsed as a map, `awk` summing mixed units). Every one was caught by the same reflex —
**the number was implausible** — and never by the harness announcing itself.

Written up as `docs/sop/lessons/L-OP-2739-verification-harnesses-fail-silently.md`.
It applied again while writing this handoff: a `docker exec ... wc -c < path` reported "file
not found" because the redirection ran on the host.
