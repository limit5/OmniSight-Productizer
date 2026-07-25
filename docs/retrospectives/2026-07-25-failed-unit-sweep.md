---
title: Failed-unit sweep retrospective — silence as the dominant failure mode
date: 2026-07-25
ticket: OP-2742
labels: [meta:retrospective, failed-units-2026-07-25, observability]
scope: the systemd failed-unit sweep of 2026-07-25 and what it revealed about
  alerting, activation and adversarial review; the individual defects live on
  OP-2728..OP-2739
---

# Failed-unit sweep retrospective — silence as the dominant failure mode

**Scope**: what the sweep as a whole showed. The individual defects are on the twelve child
tickets (`scope:failed-units-2026-07-25`); this document exists because none of them carries the
pattern that only appears when you look at all of them together.

**Out of scope**: executing the remaining findings, and any judgement about the memory-system
legs themselves. The leg-3 collision below is a fact about coupling, not a criticism of leg-3.

## 0. What triggered it

A routine session-start health check after a planned reboot (which had not in fact happened —
uptime was 39 days). Eleven user units were `failed`. The operator asked to confirm them before
anything else. Nothing was on fire, no user-visible symptom existed, and no alert had fired.

**That last clause is the whole retrospective.** Every defect below had been in place for days to
months, and the only reason any of it surfaced is that a human asked an open question.

## 1. The dominant theme: silence, not breakage

The four defects that cost the most were not complicated. They were invisible.

| what existed | what it had ever delivered |
|---|---|
| `deployment-audit-alert.service`, `staging-gate-alert.service` | nothing — both only `printf` a JSON line to the journal |
| `alertmanager`, defined in `docker-compose.prod.yml` | nothing — parked behind `profiles: ["observability"]`, never started |
| `alerts.yml`, `frontend-freshness-alerts.yml` in the mounted `obs-rules` dir | nothing — `rule_files` is an **explicit single-entry list, not a glob**; `/api/v1/rules` reports one loaded group |
| `pipeline-coordinator` + watchdog | nothing — **no `OnFailure=` at all**, and the watchdog runs `bash -lc` under `set -euo pipefail`, so it exits in ~21 ms when its `systemctl restart` returns non-zero under start-limit — it dies exactly when it is needed |

Each of these passes a config review by inspection. A file in the right directory that nothing
reads is byte-indistinguishable from a working one. The consequences: `pipeline-coordinator` was
down **5 days**, `omnisight-prod-backup` **3 nights**, and nobody knew.

The fix (OP-2728) was deliberately accepted only against a **deliberately induced failure**, not
against its own existence — see `docs/sop/lessons/L-OP-2728-alerting-artefact-proven-only-by-induced-failure.md`.

## 2. The second theme: "never once worked" is its own category

Two scheduled units were not *broken*. They had **never functioned at all**:

- `staging-sync` — **10,093 consecutive failures, 0 successes, since 2026-05-12**, firing every
  10 minutes and triggering a printf-only alert every time. Three independent breakages, each
  sufficient on its own: it passes a bare 40-hex tag while CI only ever publishes `sha-<40hex>`;
  CI only builds images for api/web pipelines carrying `CANDIDATE_SHA`, so most develop tips have
  no image at all; and `/var/lib/omnisight/staging` is root-owned, so the user-level unit could
  not commit state even if the first two were fixed.
- `staging-pg-snapshot` — **75/75 runs `prereq_failed`**. The obvious container-name drift is a
  trap: fixing it only moves the failure to the anonymiser, which refuses with **36 undeclared
  PII-shaped columns**. The destructive restore has therefore never been reachable, which also
  means **the prod→staging PII-anonymisation contract has never once executed**.

This is anti-pattern **#13 (shipped-but-not-deployed)** in a sharper form. #13 describes an
artefact that never reached its runtime. These reached their runtime, were enabled, ran on
schedule for months — and still never did their job. Every gate in the chain stops at "merged and
enabled". Nothing asks *"has this artefact ever produced its intended effect, once?"*

## 3. The self-inflicted collision

`omnisight-prod-backup` — the only backup lane that is DLP-scanned **and** gpg-encrypted **and**
uploaded with S3 Object Lock `COMPLIANCE` — began failing on 2026-07-23, the first run after the
leg-3 memory corpus was ingested into prod. The DLP scanner correctly flagged
`claude_memory_versions.body`, because that column now contains operator engineering notes that
*discuss* this stack's own topology (`pg-primary`, `ai_cache`, throwaway loopback DSNs).

Two things are worth extracting.

**A subsystem's success became another subsystem's poison.** Nothing was wrong with either. leg-3
did exactly what it was built to do; the DLP gate did exactly what it was built to do. The
coupling was invisible because the gate's allowlist was written 2026-05-23 and the table was
created 2026-07-22 — nobody could have listed it, and no test spans the two.

**It is anti-pattern #11 (self-referential text-match false positive) at the data layer.** The
documented Generalisation already says documentation surfaces should rarely be subject to
content-pattern scans. What was missing was any mechanism to notice that a *new* documentation
surface had appeared inside the scanned set.

Also surfaced, and worse than the block itself: **two other prod-DB backup lanes run green daily
and call no DLP scanner at all** — one writing plaintext `.sql` to `$HOME` (found at mode 0664,
world-readable, containing `git_accounts`, `llm_credentials`, `tenant_deks`, `sessions`), one
shipping to S3 with SSE only and no Object Lock. So the material the gate refused to encrypt was
leaving the host nightly, unscanned, the whole time. The gate's *relative* security value was
negative: its existence is what stopped anyone looking at the lanes beside it.

## 4. Three designs, nine audit passes, one constant error shape

The fix for §3 went through three rounds of adversarial review by three independent reviewers
(a safety lens, a correctness lens, and an external model). **All three rejected v1. All three
rejected v2. All three rejected v3.** The verdicts converged every time, from different routes.

The error was the same shape in all three versions: a gate that says *"release this cell unless my
patterns detect a credential"* is only as sound as its detector is **complete** — and no regex set
is complete over free-form prose. Each round the reviewers simply found the next uncovered shape:

- v1 assumed a loopback-host predicate was safe → the classifier's own trailing character class
  lets one match swallow a second connection string.
- v2 assumed the internal-hostname labels were harmless to release → `postgresql+asyncpg://` and
  `redis://:pw@` fire *only* those labels, because the `database_url` pattern needs a bare scheme
  and a non-empty user. `postgresql+asyncpg://` is this repo's own documented DSN form.
- v3 added a broader URI regex and a password-assignment rule → reviewers produced 17 more
  released shapes (`.pgpass`, JSON, YAML folded scalars, `curl -u`, `-p<pw>`, line-split URIs…),
  and a correctness pass showed the design did not even achieve its goal: it took the block count
  from 15 to **1**, not 0.

The accepted design came **from a reviewer, not from the author**: allowlist the reviewed *content*
by digest, using the `body_sha256` column the table already had. Completeness stops being a
requirement — any edit is a new digest, hence a new review.

**The process point**: the value of the review rounds was not catching mistakes in an otherwise
sound design. It was demonstrating that the entire design *category* was unsound, which no amount
of iteration inside that category would have revealed. Three rounds was the right number: rounds
1 and 2 each killed a version, round 3 killed the version *and* supplied the replacement.

## 5. Audit accuracy — what the initial diagnosis got wrong

Recorded plainly, because this is the part that generalises.

| initial claim | reality |
|---|---|
| "prod backups are down 3 days" | Only the gpg/DLP lane. Two other lanes were green daily. The urgency premise for hot-patching a release-pinned checkout was false. |
| "the crash reason is lost to journal rotation" | It was on disk the whole time. The unit writes `StandardOutput=append:` to a **file**; only the journal was checked. 18 × `OSError: [Errno 28]`. |
| "log retention is the root-cause fix for the ENOSPC" | The named logs total 2–3 GB on a 1007 GB filesystem. The disk had been at 99%+ for three days; the actual ~212 GB consumer is **still unidentified**, and a docker prune is positively excluded (dangling images survived). |
| "fixing staging-pg-snapshot would destroy the leg-2 soak" | Unreachable. The anonymiser refuses two steps earlier; the destructive path has never executed. |
| "exactly 15 memory files = 15 prod findings" | Two offsetting errors. Prod has 14 distinct slugs (one file contributes two revisions) and the local corpus contains one file prod rejected. |
| "the staging gate is currently false-GREEN" | Currently false-**RED**: the canary stamps the patchset revision while the checker looks up by the merge-commit tip. False-green is the fast-forward case. Both are defects. |

Five of six were caught by reviewers, not by the author. The one structural lesson: **every one of
these errors was a claim about a system's state made from a partial read** — one log source, one
directory listing, one plausible causal story that fit the timing. The timing fit in every case.

## 6. Checkout topology is a risk surface

Three checkouts of the same repo exist on this host and **all three are compromised, differently**:
`/home/user/omnisight-prod` is release-pinned (correct, but means a fix reaches prod only at a
release cut); `/home/user/sora-bridge` is 202 commits behind with a dirty tree **while its sync
reports `Result=success` daily** and is `expected=yes` in the deployment audit; the canonical work
tree has ~735 dirty paths. Two daemons execute from the second one, four live systemd unit files
are symlinks into it, and one prod backup lane runs from the third.

That topology is why the alert channel was deliberately installed as standalone files under
`~/.local/bin`: an alerting path must not inherit the fragility of the thing it watches. The repo
copy is for review and backup; the filenames are identical so drift is a `sha256sum` compare.

## 7. What changed, what did not

**Changed** — 11 failed units → 6; `deployment-audit` FAIL → PASS (`fatal_red` 2 → 0);
coordinator restored in shadow mode (`ACTING` commented out pending review of one observation
cycle) after 5 days down, 75 clean ticks; two never-working timers disabled; 29 plaintext prod
dumps (351 MB) taken from 0664/0644 in a 0755 dir to 0600 in a 0700 dir, with `UMask=0077` on both
producing units; and a working alert channel with a proven delivery path, wired to the four units
whose silence would matter.

**Not changed, deliberately** — the DLP gate itself (OP-2729, reaches prod only at the next release
cut, and blocked on the alert channel by design); the unscanned backup lanes (a policy decision,
with the exposure window explicitly accepted and dated by the operator); the disk consumer (still
unidentified — the floor now alerts with `df`, `docker system df` and top-consumer evidence
attached, so the next occurrence arrives self-diagnosing); `sora-bridge` (fast-forwarding it
mid-remediation would change the code under a running daemon and rewrite live unit files).

## 8. What would have caught this earlier

1. **An activation gate that requires one observed success.** Both never-worked timers would have
   been caught on day one by a check that asks whether the unit has ever produced its intended
   effect, rather than whether it is enabled.
2. **Induced-failure acceptance for anything that alerts.** Four mechanisms, zero deliveries, all
   config-review-clean.
3. **A free-space floor.** The disk climbed 87.5% → 100% over nine days. An 85% floor would have
   alerted roughly nine days before the outage. It now exists.
4. **Coupling awareness when a new data surface enters a scanned set.** The DLP collision was
   predictable the moment migration 0279 created a prose-bearing table, and unpredictable to
   anyone who was not looking at both subsystems at once.
5. **Treating a green from an unproven monitor as unknown, not as green.** `sora-bridge-sync`
   reporting success while 202 commits behind is the purest instance: a monitored gate that
   reports OK while not doing its job is worse than a red one, because it consumes the attention
   that would otherwise have found the problem.

## 9. Open items

Tracked on OP-2729 … OP-2739. The two that block the most: **OP-2728's follow-ups** (a scheduled
drift check between repo and installed copies; whether the two printf-only alert units should be
re-pointed) and **OP-2734** (the unidentified ~212 GB consumer — the only finding in this sweep
whose root cause is still genuinely unknown).
