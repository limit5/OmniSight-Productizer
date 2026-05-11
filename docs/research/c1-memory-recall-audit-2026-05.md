# C1 Memory Tool — Recall Helpfulness Audit (OP-867)

**Date opened**: 2026-05-11
**Audit reference window**: 2026-05-11 → 2026-05-18 (7-day post-merge window)
**Author**: claude-bot (auto-runner-sdk, instance default), supervised by operator
**Source ticket**: OP-867 (X3 — C1 Memory Tool recall helpfulness audit, Tier M)
**Depends on**: OP-851 (C1 Anthropic Memory Tool standalone integration), merged into `develop` at 2026-05-11 (commit `ce8c8a55`).

---

## TL;DR — Verdict (current)

**DEFERRED.** The audit infrastructure is shipped (script + tests + this doc), but the verdict on whether C1's tier:S/M default auto-recall is *helpful* or *noise* is **not yet computable**: OP-851 merged on 2026-05-11, so as of this writing there is **0 days** of production audit data, far below the 7-day window the OP-867 acceptance criteria require (AC #1).

The script raises the documented error catalog codes when the data is missing:

* `C1NotYetMerged` — defensive guard against a partial-merge regression; the runner reverts the ticket via `transition_back_to_todo` if hit.
* `InsufficientSamplesForAudit` — fewer than 30 qualifying auto-recall events in the window. Operator waits for more data and re-runs the script.

**Re-run on / after 2026-05-18.** When the script can produce a real `mean_score`, append the *Verdict* section below (it is intentionally left as a placeholder so the empirical answer lands in one well-known spot the META/parent ticket can cite).

This is the same disambiguation discipline as L-OP-843 (*vendor-talk-compresses-layers*) and the C7 dreaming spike (`docs/research/c7-dreaming-comparison-2026-05.md` §1): we **do not pre-decide** before we have the data. The "obvious" verdict (auto-recall is fine; keep it) is also the cheapest one for the runner to claim, which is exactly the failure mode the audit exists to guard against.

---

## 1. Why this audit exists

C1 (OP-851) shipped Anthropic's Memory Tool (`memory_20260120`) wired into the SDK launcher as a per-fleet filesystem scratchpad. The merged design auto-recalls memory entries tagged `tier:S` and `tier:M` (per ADR-0005 tier authority and `backend.agents.memory_tool_handler.tier_is_recallable`); `tier:L` requires `OMNISIGHT_MEMORY_TIER_L_OPTIN=1`; `tier:X` is always refused.

The default-on choice for tier:M is the **costly direction to be wrong on**. If tier:M entries are genuinely helpful, default-on saves the operator a label-opt-in step per ticket; if they are noise, every runner ticket pays a context-token tax for nothing — and worse, the model gets *anchored* on stale or off-topic memory rows that the orchestrator never asked for. The same dynamic almost caused the L-OP-827 twin-defect (Memorized stale runner-rebase state contaminated unrelated tickets — generalisable lesson).

OP-867 is therefore a **falsification-style follow-up**, not a celebration audit. Its job is to look for the noise case; absent positive evidence, the default-on stance should tighten.

---

## 2. Methodology

### 2.1 Data source

JSONL audit rows in `progress.txt` files emitted by `MemoryToolHandler._audit` (see `backend/agents/memory_tool_handler.py:696-720`). Each tool operation produces one row of shape:

```json
{"type": "memory_tool", "tool": "memory", "op": "read",
 "key": "/memories/lesson:L-OP-827.md",
 "timestamp": "2026-05-12T03:14:00+00:00",
 "ticket_key": "OP-901", "tier": "S"}
```

Run-S1's runner writes these to `<WORKTREE>/.runner/progress-<TICKET>.txt` (see `scripts/run_s1_via_anthropic_sdk.py:965`). The audit script accepts one or more of these via repeated `--progress` arguments so all per-ticket files in a fleet can be aggregated.

### 2.2 Filter (AC #1)

The script keeps rows that match **all** of:

* `type == "memory_tool"` — excludes B3 ToMScratchpad rows that multiplex into the same file.
* `op` ∈ `{"read", "list"}` — the operations that actually push memory content back into the model's context. `write` / `delete` / `evict` / `tier_refuse` / `error:*` are operational rows and don't represent recall noise candidates.
* `op == "read"` rows must additionally carry a `tier` of `S` or `M`. `list` rows are not tier-tagged in C1 (a directory listing self-filters via `tier_is_recallable`); the audit currently focuses on `read` rows because those are the events whose content the operator can actually *score* for helpfulness.
* `timestamp` falls inside the 7-day window ending at `--now` (default: current UTC).

### 2.3 Sample (AC #2)

`random.Random(seed).sample(events, 30)` — deterministic given a seed, so re-running the audit at the same data + seed reproduces the exact same draw. Sample without replacement so the operator scores 30 distinct events, not 30 dice rolls over the same key.

If the post-filter population is `< 30`, the script raises `InsufficientSamplesForAudit` per the OP-867 error catalog. The operator waits and re-runs — there is no fallback "score what you have" path because a `<30`-event mean is a useless signal and would only manufacture an unjustified verdict.

### 2.4 Score (AC #2)

For each sampled event, the operator enters a **1–5 helpfulness rating**:

| Score | Meaning |
|---|---|
| 1 | Harmful — the recall actively misled the model or burned context for off-topic content. |
| 2 | Wasted — content was unrelated to the ticket but not actively harmful. |
| 3 | Neutral — model could have done the ticket without it; no harm either way. |
| 4 | Helpful — content materially shortened the model's path to a correct answer. |
| 5 | Clearly useful — the ticket would have failed or required a reset without it. |

Two input modes:

* **Interactive** (`--interactive` or no `--scores` on a TTY): the script prompts per event, displays `tier`, `key`, `ticket_key`, `timestamp`, and reads the 1–5 score from stdin. Invalid input re-prompts.
* **Pre-filled JSON** (`--scores path.json`): a `{key: int}` mapping covering *every* sampled key. Partial files are rejected (CI cannot silently drop events). This mode is what lets us replay the same scoring session in CI and exists primarily for the test fixture in `backend/tests/test_audit_memory_recall_helpfulness.py`.

### 2.5 Aggregate + decide (AC #3)

Aggregation is intentionally minimal — mean, stdev, and a per-tier-mean split so the operator can see whether tier:M alone is dragging the average down. The script does **not** weight by tier or by ticket key; weighting would let one chatty ticket dominate the verdict, which is the opposite of what a falsification audit wants.

The decision rule is hard-coded per AC #3:

* `mean < 3.0` → `TIGHTEN_TIER_M` — file a follow-up to require label opt-in for tier:M (mirror tier:L).
* `mean >= 3.0` → `KEEP_AS_IS` — current default-on stance is net-positive.

The boundary case (`mean == 3.0`) intentionally maps to `KEEP_AS_IS` to avoid manufacturing a tightening when the population is truly neutral. The bias is to *only* tighten when the data clearly says noise. Tests pin the boundary (`backend/tests/test_audit_memory_recall_helpfulness.py::test_decision_threshold_below_3_recommends_tighten_above_3_keeps`).

---

## 3. Reproducibility

Run the audit exactly as below — paths, seed, and reference time fully reproduce a past audit window. The seed is purposely surfaced in the result so a contested verdict can be replayed:

```bash
python scripts/audit_memory_recall_helpfulness.py \
    --progress data/sdk-launcher/progress.txt \
    --progress data/sdk-launcher/<other-fleet>/progress.txt \
    --now 2026-05-18T00:00:00Z \
    --window-days 7 \
    --sample-size 30 \
    --seed 0 \
    --output data/op-867-audit-result.json
```

The emitted `AuditResult` JSON carries:

* `window_start`, `window_end`, `seed`, `sampled_event_count`, `total_events_in_window` — full reproducibility metadata.
* `mean_score`, `stdev_score`, `per_tier_mean`.
* `decision` ∈ `{TIGHTEN_TIER_M, KEEP_AS_IS}` and a human-readable `decision_reason`.
* `scores[]` — per-event `{key, tier, ticket_key, timestamp, score}` so the verdict's anchor data lives in the artefact, not in the operator's head.

---

## 4. Failure-mode pre-mortem (defensive)

Reasoning *before* the data lands, so the verdict cannot be retro-fitted to whichever finding is most flattering:

### 4.1 If `mean >= 3.0` (KEEP)

Plausible explanations to interrogate before accepting:

* **Selection effect**: did the runner pick mostly tickets that hit the same well-seeded lesson? Per-ticket distribution in `scores[]` reveals it; if one or two ticket keys dominate the sample, the verdict is fragile.
* **Anchoring bias**: was the operator scoring shortly after writing the lesson and remembering its content? Mitigated by the `--seed` + offline `--scores` mode — operator should batch-score from the JSON sample after a deliberate gap.

### 4.2 If `mean < 3.0` (TIGHTEN)

* **Surface vs depth**: a "wasted" score may reflect that the *current* ticket didn't need that memory, not that the entry itself is bad. Re-check: which tier dragged the mean? If tier:S dragged the mean too, this is a *seeding* problem (lessons are too generic), not a tier policy problem. The follow-up ticket the script recommends targets tier:M opt-in; if the data points at seeding, file a different follow-up.
* **Reactive overcorrection**: tightening tier:M to opt-in is reversible if it turns out the operator just hit a bad week. Re-audit cadence after tightening: 30 days (longer window dampens noise).

---

## 5. Follow-up ticket templates

These are pre-drafted so the operator can file them immediately on a real verdict — no second drafting pass during retro.

### 5.1 If verdict is `TIGHTEN_TIER_M`

* **Title**: "X4 — Tighten tier:M Memory Tool recall to require label opt-in"
* **Area**: `backend, docs, tests`
* **Tier**: M
* **AC sketch**:
  1. `tier_is_recallable("M", tier_l_optin=False)` returns `False` unless a new env var (`OMNISIGHT_MEMORY_TIER_M_OPTIN=1` or per-ticket label `memory:tier-m-optin`) is set.
  2. Audit log emits one `tier_refuse` row per blocked tier:M recall (same shape as the existing tier:L row).
  3. Update `docs/operations/memory-tool.md` § "Tier policy" to reflect the new default.
  4. Test cases: opt-out denial, opt-in success, refusal audit row shape.
* **Cite**: `docs/research/c1-memory-recall-audit-2026-05.md` § Verdict (this doc).

### 5.2 If verdict is `KEEP_AS_IS`

No new ticket — the doc itself plus the JSON sidecar are the audit trail. Note the verdict, the `mean_score`, and the sample seed in the OP-867 resolution comment so future audits can compare.

---

## 6. Verdict

> **STATUS — 2026-05-11**: deferred. 0 days of production audit data available; OP-867 audit infrastructure shipped, real audit run scheduled for 2026-05-18.
>
> When the script returns a real `AuditResult`, append below:
>
> * Run timestamp:
> * `--progress` paths fed:
> * `total_events_in_window`:
> * `sampled_event_count`:
> * `seed`:
> * `mean_score`, `stdev_score`:
> * `per_tier_mean`:
> * `decision`:
> * Operator confirmation (one line, signed):

---

## 7. Out-of-scope (deliberate omissions)

Per the OP-867 ticket boundary (Tier M, area `backend|docs|tests`) the following adjacent improvements were *not* made in this ticket and remain candidates for separate work:

* Modifying the runner's recall *prompt* to teach the model when to ignore retrieved memory — that is C5/C6 (capability matrix + tier-aware filter) territory and is governed by OP-855 / OP-856.
* Adding a Grafana panel for `tier_refuse` row counts — a `devops` / `tooling` change; not in this ticket's area set.
* Re-classifying existing lesson entries by tier (e.g. demoting a generic lesson from S → M) — a `docs` change but governed by `docs/sop/lessons/` SOP, not a recall audit.

---

## 8. References

* `backend/agents/memory_tool_handler.py` — the C1 handler. Key constants: `AUTO_RECALL_OPS` consumers, `tier_is_recallable`, `_audit`.
* `backend/tests/test_memory_tool.py` — C1 acceptance tests; `test_audit_log_jsonl_shape` pins the JSONL row schema this audit consumes.
* `scripts/audit_memory_recall_helpfulness.py` — this audit's script.
* `backend/tests/test_audit_memory_recall_helpfulness.py` — 3 required test cases (sample selection, scoring aggregation, decision threshold) plus two integration cases.
* `docs/sop/lessons/L-OP-827-twin-defects-runner-rebase-and-bridge-cursor.md` — the prior incident that motivates the "look for the noise case" stance.
* `docs/research/c7-dreaming-comparison-2026-05.md` § TL;DR — same disambiguation discipline (no pre-decision before the data).
* ADR-0005 — Tier S/M/L/X authority (escalation gates).
