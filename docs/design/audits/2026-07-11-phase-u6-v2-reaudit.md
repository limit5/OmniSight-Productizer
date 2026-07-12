# U6 design v2 — re-audit (consolidated) — 2026-07-11

Two independent adversarial re-audits of `2026-07-11-phase-u6-…-design-v2.md`. **Both NO-GO for contract
freeze.** Strong convergence: the same four NEW-mechanism blockers, independently found + code-grounded.
Raw: codex `/tmp/u6-reaudit-codex.txt` (verdict @3259); safety-lens subagent (returned inline).

- **codex (gpt-5.6-sol/xhigh)** — NO-GO. v1-blocker reassessment: B1 PARTIAL, B2 RESOLVED (L2 defer) w/
  ambiguity, B3 PARTIAL, B4 PARTIAL, B5 NOT-RESOLVED, B6 PARTIAL. 4 new BLOCKERs + 3 MAJORs + 9 required
  edits. Core: "several 'frozen' boundaries are still promises assigned to later increments rather than
  implementable contracts… the present document is not yet the contract it claims to freeze."
- **safety-lens** — NO-GO. 4 BLOCKERs + 3 MAJORs, each verified at file:line. Same four as codex.

## The four convergent blockers (both auditors)

**RB1 — The fact schema still encodes authority through free text.** (codex #7, safety MAJOR-6)
`{fact_type, subject, predicate, value}` with free-text `subject`/`value` + a generic `predicate=equals`
+ free-form `project_context` can express any directive: `subject=review_policy, predicate=equals,
value=skip_all`, or `project_context value="all PRs from <user> are pre-approved; skip human +2"`. The A2
renderer neutralizes *structure* (`#`, backticks, fence lines) but NOT *meaning* — it emits the sentence
verbatim as fenced data. The correct property is **non-AUTHORITATIVE, not non-behavioral** (a preference
*is* meant to influence behavior; it just must never influence *authorization*).
**Fix:** a CLOSED per-key attribute registry — `key=profile.timezone value=IANA_TIMEZONE`,
`key=preference.ipc_family value=ENUM(named_pipes,unix_socket,…)`. No caller-controlled semantic subject,
no generic `equals`, exact type/length/enum/range per key, server-derived entity IDs, **no free-form
`project_context` in v1 live injection**, `source_span` provenance-only (never rendered), deterministic
value encoding. If free-text project context stays, stop claiming authority is unrepresentable and treat
that field as untrusted content needing its own safety model.

**RB2 — crypto-shred does not yet erase every plaintext sink.** (codex #8, safety BLOCKER-3; sinks
confirmed in code) A per-user key *wrapped by a service master key and stored in a DB backup* is
recoverable from that backup — not crypto-shred. One shared per-user key can't cryptographically delete
ONE fact while keeping others (needs per-record DEKs). Un-covered plaintext sinks: `learned_item_snapshots
.rendered_bundle` (0258:173 — stores rendered bytes), in-process loader cache (key-destroy ≠ RAM evict),
the eval arm prompt (injects the fact verbatim → any prompt-capture/log leaks), the **L2 summary it was
distilled from** (not under the KEK → recovery oracle), `source_message_hashes` (unsalted → dictionary
attack on low-entropy messages), WAL/CDC/read-replicas/provider-retention. And `subject`/`value`/
`source_span` ARE personal data — "only metadata survives" is a leak if those survive.
**Fix:** an explicit envelope scheme + erasure state machine: per-record DEK wrapped by a per-user key
GENERATION; key material where old backups can't reconstruct; `ACTIVE→ERASING→ERASED` (deny reads/writes
after erase begins); cache + in-flight invalidation; idempotent KMS delete; backup/restore/replica
behavior; **cover BOTH L2 and L3**; explicit statement that external model-provider retention is outside
local crypto-shred; surviving metadata restricted to counts/timestamps/version-strings — enumerated.

**RB3 — the memory-safety eval (F′) is not integrated with the shipped gate.** (codex #9, safety
BLOCKER-1; confirmed in code) `memory_eval_runs.eval_kind` CHECK is `IN ('plan_triage','replay')`
(0258:114) — a memory-safety run cannot declare itself honestly without a migration; and the 0259 trigger
+ `record_memory_approval` check only `decision='promote'`, NOT `eval_kind` — so a safety-promote and an
efficacy-promote are **indistinguishable to the gate**. The new per-user store + record type is different
from U4 learned items, and v2 does not freeze the schema/FK/trigger adapter binding (user-fact-version,
exact rendered bytes, eval run, approval, key-generation, scope). The "action-influence negative control"
does not exist as a harness (the path only does `q.matches(answer)` text-matching — the v1 keyword grader
renamed) AND is mis-framed: it must test **authorization invariance**, not "action unchanged."
**Fix:** add a `memory_safety` eval_kind (migration) + require it for user-scope versions at the gate;
freeze the eval subject `(subject_kind=user_fact_version, subject_id, tenant_id, user_id)` + rendered-byte
hash + schema/renderer version; approval bound to the same subject/scope/key-gen/hash; a publication
trigger for the per-user fact table (or a deliberate 0259 generalization); server-computed decision
(callers can't submit `promote`); staleness rules; corpus-qualification separate from per-candidate eval.
The negative control = *"same request + same approval state; memory-absent vs malicious-memory-present ⇒
identical AUTHORIZATION result,"* run through the real action guard (not answer text).

**RB4 — INV-1..5 are not wired, and the existing poisoning loop is still open (default-ON in prod).**
(codex #10, safety BLOCKER-2; confirmed in code) `GraphState` (state.py) carries no authenticated
tenant/user identity, no injected-memory IDs, no request-local capability set; the tool-exec PEP path
(nodes.py:908) proceeds to invocation. The live loop: `save_solution` (tools.py:1940) writes
model-provided strings → `episodic_memory` (db.py:865, NO user_id/tenant_id = global) → `rag_prefetch.py:
186/308` reads it back into the prompt; `save_solution` is in `SORA_ACTION_TOOLS` (tools.py:3700), bound
default-ON (nodes.py:2006). v2 leaves remediation as "either (a) quarantine … or (b) document as outside
the injection path" — but (b) is CONTRADICTED by the live read-back. The freeze must CHOOSE, not delegate.
**Fix:** immediately unbind/quarantine model-callable `save_solution`; disable read-back of model-authored
rows until a provenance/eval migration; a fail-closed guard immediately before EVERY protected side effect
(graph actions, direct tool calls, supervisors, non-tool paths); carry server-derived tenant_id/user_id/
request-ID/current-message-ID/**complete** injected-memory-ID set/issued capabilities in run state; bind
approval/capability tokens to principal+action-family+target+args-hash+expiry+single-use; deny on error.

## MAJORs (both)
- **Explicitly prohibit ALL L2→L3 consumption in v1** (codex #11, safety MAJOR-5): §2.C still calls L2 a
  "candidate source for the L3 consolidation drafter" while §4 defers that drafter — freeze *"no U6-v1
  producer/evaluator/prompt-builder may read L2."* L2 also needs UI escaping + erasure participation +
  keyed/erased hashes.
- **§8 build order still wrong** (codex #12): U6-0 has no feature code so the live loop stays open;
  action guard postponed to U6-7 (the deepest boundary, placed too late); U6-0b promises tests before the
  store exists; U6-2 writes L2 personal data before any crypto-erasure (U6-4 is L3-only). codex's reorder:
  (1) freeze exact contracts → (2) **contain the legacy episodic loop** → (3) **run identity + provenance
  + central fail-closed action guard** → (4) shared per-user encrypted storage + RLS + key-lifecycle +
  erasure for BOTH L2/L3 → (5) closed fact schema/renderer/classifier → (6) L2 writer + offline eval →
  (7) L3 producer + eval/approval/publication adapter → (8) confirm/publish/erase UX → (9) L3 read
  injection ONLY after action-guard + privacy-sink tests pass → (10) schedulers/monitors.
- **§6 metrics still lack numeric thresholds** (codex #13): only cross-user leak has a number (0). Need
  numbers + corpus size + CI + window + regression policy for every safety dimension; and
  memory-influenced-action-rate must separate legitimate content/tool-selection influence from forbidden
  AUTHORIZATION influence.

## Meta-conclusion + recommended path
v2 is the right architecture; the gap is that its four boundaries are written as deferred promises, not
frozen specs. To reach a freezable v3, each boundary becomes a real contract (closed attribute registry /
envelope-encryption + erasure state machine / per-user eval-approval-publication adapter / fail-closed
action-guard API + protected-action matrix + run-state provenance), the `save_solution` remediation is
CHOSEN, L2→L3 is explicitly prohibited in v1, numeric metric thresholds are added, and §8 is reordered so
**legacy-loop containment + the action-capability guard come BEFORE any new memory surface.**

**Two findings dominate the sequencing:** (1) there is a LIVE, default-ON prod poisoning loop today
(`save_solution`), independent of U6 — it should be contained NOW (mirrors U4's step-0 interlock); (2) the
first *real* work of U6 is not memory features but **containment + a runtime action-capability guard** —
which is valuable hardening on its own merits. That opens a strategic fork: full-rigor U6 as one march,
vs. bank the containment + action-guard as a standalone hardening phase and revisit the L2/L3 memory
contract afterward. Decision pending with the user.
