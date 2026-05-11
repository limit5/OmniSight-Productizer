# Spike Report — Anthropic Dreams API vs proprietary B12+replay (OP-857)

**Date**: 2026-05-11
**Triggered by**: OP-857 (C7 — Sprint C Phase 4) operator pivot. Original master-plan §3.8 spec said "C1 memory tool + Dreaming after every completed ticket"; the OP-857 ticket reframed it as the head-to-head "Dreaming vs proprietary B12+replay; ship the better one." This report answers the reframed question.
**Disambiguation discipline**: per **L-OP-843** (*"vendor talks compress 3 layers; disambiguate first"*) we did NOT decide before reading the docs. The actual Dreams API is **not** the same shape as a "replay incidents through reflection" engine — see §1 / §3.
**Scope**: investigation only. No production code mutated. Deliverables: this report + `scripts/spike_c7_dreaming_compare.py` harness + `data/op-857-dreaming-comparison.json` sidecar. Per the OP-857 ticket (Tier M, area `backend|docs|tests`), out-of-area domains (db, devops, embedded, frontend, security, tooling) are **untouched**.
**Reference inputs**:
- `platform.claude.com/docs/en/managed-agents/dreams` (Dreams API reference, fetched 2026-05-11)
- `platform.claude.com/docs/en/managed-agents/overview` (Managed Agents parent surface)
- `docs/audit/2026-05-11-sprint-abc-master-plan.md` §3.8 (original C7 spec)
- `backend/agents/reflection_loop.py` (B12 production code, OP-850)
- `docs/sop/lessons/L-OP-827-twin-defects-runner-rebase-and-bridge-cursor.md`
- `docs/sop/lessons/L-OP-837-runner-main-repo-staleness.md`
- `docs/research/b3-outcomes-spike-2026-05.md` (the prior vendor-disambiguation spike — same shape, recommendation template)

---

## TL;DR — Recommendation

**Partial / DEFER. Do not file the `OMNISIGHT_DREAMING_ENABLED=1` runner-mode child today. Instead file two narrower follow-ups:**

1. **C7a — Managed Agents harness pilot** (Tier L, deferred). Decide whether OmniSight's runner moves off the bare Messages API onto Managed Agents *as an independent product question* — Dreams is one of several features (managed sandboxes, hosted memory stores, multi-agent) that all hinge on the same migration. Without that migration, Dreams is structurally inaccessible.
2. **C7b — JSONL→session translator spike** (Tier S, deferred until C7a decision). If Managed Agents pilot is approved, the *first* enabling change is converting OmniSight's existing JSONL transcripts into Managed Agents `sessions` so historical incidents become valid Dreams inputs. Without this, only *new* tickets running under Managed Agents would benefit — losing the entire historical-incident value.

Reason in one paragraph: the Dreams API is real, well-designed, and would deliver curation quality the proprietary B12 path cannot match (the harness's deterministic mock shows ~0.92 insight-overlap vs ~0.19, and even adjusting for mock bias the qualitative shape is clear — Dreams operates on whole sessions with cross-session pattern-matching, B12 reacts to one failure at a time within the 3-attempt cap). But Dreams **only accepts Managed Agents `sessions` and `memory_store` inputs** — not bare Messages-API JSONL. OmniSight's runner is built around the bare Messages API + custom JSONL transcripts (`anthropic_native_client.py`), so wiring Dreams today is *structurally* not a code change — it's a runtime re-platforming. That's a Tier L decision, not a Sprint C Phase 4 spike outcome. Per L-OP-843 rule 3 (independent-audit veto), keep the proprietary path **as-is** while the migration question is decided separately. **Do NOT delete or downgrade B12 / OP-850.**

**Sprint children to file IF accepted**: C7a (Managed Agents harness pilot, Tier L, area `backend|devops`) and conditionally C7b (translator spike, Tier S) — see §6.

---

## 1. Dreams API surface (verified 2026-05-11)

Unlike the b3-outcomes spike where the wire shape was reconstructed from a talk demo, the Dreams API has formal documentation. Verified directly against `platform.claude.com/docs/en/managed-agents/dreams`.

```python
# Per docs §"Create a dream" — Python SDK example.
dream = client.beta.dreams.create(
    inputs=[
        {"type": "memory_store", "memory_store_id": store_id},
        {"type": "sessions", "session_ids": [session_a, session_b]},
    ],
    model="claude-opus-4-7",
    instructions="Focus on coding-style preferences; ignore one-off debugging notes.",
)
print(dream.id)  # drm_01...
```

### Surface summary

| Field | Type | Notes |
|---|---|---|
| `inputs[]` | `list[Input]` | At least one `memory_store` + up to 100 `sessions` |
| `inputs[].type` | `"memory_store" \| "sessions"` | Tagged union |
| `inputs[].memory_store_id` | `str` | Pre-existing store id (`memstore_01...`) |
| `inputs[].session_ids` | `list[str]` | Managed Agents session ids (`sesn_01...`) — **not** bare transcripts |
| `model` | `str` | `claude-opus-4-7` or `claude-sonnet-4-6` only (research preview) |
| `instructions` | `str` | ≤ 4,096 chars; designer-owned guidance |
| Response: `id` | `str` | `drm_01...` |
| Response: `status` | `pending\|running\|completed\|failed\|canceled` | Async; poll by id |
| Response: `outputs[]` | `list[{type:"memory_store", memory_store_id}]` | The new curated store; populated when status reaches `completed` |
| Response: `usage` | `{input_tokens, output_tokens, cache_*}` | Standard token counters |
| Beta header | `managed-agents-2026-04-01,dreaming-2026-04-21` | Both required |

### Three structural facts that matter for OmniSight

1. **Inputs are Managed Agents resources, not Messages-API artifacts.** A `session_id` references a session that was *created* via `client.beta.sessions.create(agent=..., environment_id=...)` — i.e. the agent *ran* inside Managed Agents infrastructure (managed sandbox, server-side event log, etc.). OmniSight's runner does not produce these. JSONL transcripts in `~/.local/state/omnisight-runner/transcripts/*.jsonl` are not directly convertible without re-creating the session shell server-side.
2. **Async pipeline; minutes to tens of minutes per dream.** Per docs: "Dreams run asynchronously and typically take minutes to tens of minutes depending on input size." Polling pattern is documented (`while status in ('pending','running'): sleep(10); retrieve`). This is fundamentally **post-hoc curation**, not a per-failure reflection loop. It overlaps with the master-plan §3.3 "C2 random-Friday replay" cadence (nightly / weekly), NOT with B12's per-attempt synchronous reflection.
3. **Output is a *new* memory store; input is never modified.** Dreams produces a *separate* store you can inspect, A/B against the input, and discard. This is genuinely better than overwriting; mirrors L-OP-827 generalisation #4 (recovery primitives for state mutations). But the value only materialises if you have a memory-store consumer downstream — i.e. the Memory Tool / C1 must be wired first.

---

## 2. Methodology

The spike harness `scripts/spike_c7_dreaming_compare.py`:

- **Wires the real B12 primitive.** Path B uses the production `ReflectionInput` + `ReflectionCounter` from `backend/agents/reflection_loop.py` (OP-850). Each scenario's failures are converted to `ReflectionInput`, the cap is exercised at the production `REFLECTION_LIMIT = 3`, and the `to_user_turn()` payload is rendered exactly as the live runner would inject it. This is the same fidelity discipline as `spike_b3_outcomes_compare.py` (which wires the real `LoopDetector`).
- **Uses the real Dreams wire shape.** Path A's `MockDreamingClient` returns the documented response body verbatim (top-level fields, `usage` shape, `outputs[]` tagged-union). The harness's `validate_dream_response()` enforces those required fields, raising `DreamingAPISchemaUnexpected` on drift — exercised as a sanity test.
- **Two error classes per ticket §"Error catalog"**:
  - `DreamingAPIUnavailable` → harness falls back to the proprietary path for that scenario (records the unavailability in `errors[]`).
  - `DreamingAPISchemaUnexpected` → harness pauses + escalates (does NOT fall back). This matches the spec wording "pause spike + escalate".
- **Constants pinned for reproducibility** (see header): pricing from `config/llm_pricing.yaml`, dream wall-clock anchored on the docs's "minutes to tens of minutes" range (median = 360s for 3-session inputs), B12 reflection turn anchored on OP-850 pilot medians.

**Why no live Dreams calls during the spike**:

- Dreams is research-preview, access-gated (form at `claude.com/form/claude-managed-agents`). The OmniSight org has not been admitted at the time of writing.
- Even with access, dream creation requires *prior* Managed Agents sessions to point at. OmniSight has none. The translator (JSONL → server-side session) is the actual blocker, not the access form.
- Per the b3-outcomes spike's discipline (L-OP-843 rule 1: vendor demos compress capability layers): the deliverable is a structural recommendation, not a benchmark. The harness gives the dimensions; this report draws the line.

The harness was exercised once for the comparison run + 3 sanity tests (`--tests`) — all pass. JSON sidecar at `data/op-857-dreaming-comparison.json`. By design the mock outputs are deterministic (no RNG; `dream_id` derived from a SHA-256 over the payload) so the sidecar is bit-identical across re-runs.

---

## 3. Scenario matrix + per-cell results

The ticket cites OP-829 / OP-832 / OP-835 as scenario sources, but those tickets do not have standalone post-mortem files — they appear as *components* in the L-OP-827 / L-OP-837 retrospectives. Three scenarios drawn from the available evidence:

| ID | Source | Description | Path A (Dreaming, mock) | Path B (B12 replay) | What it probes |
|---|---|---|---|---|---|
| **S1** | `L-OP-827` | Twin-defect post-mortem: runner rebase precondition + bridge cursor | overlap=1.00, $0.39, 360s | overlap=0.25, $0.082, 6s | Cross-incident pattern matching (the two defects share a structural shape) |
| **S2** | `L-OP-837` | Long-lived runner checkout staleness, 5 commits behind develop | overlap=1.00, $0.20, 360s | overlap=0.33, $0.077, 3s | Single-incident reflection (one failure → one fix) |
| **S3** | `L-OP-837` §Situation | OP-829 5x revert loop (no per-key dampening) | overlap=0.75, $0.59, 360s | overlap=0.00, $0.077, 3s | Repeated-failure pattern (5 cycles of identical revert) |

Aggregate (n=3, deterministic mock):

- **Mean insight overlap**: A = **0.917** · B = **0.194**.
- **Mean cost / scenario**: A = **$0.39** · B = **$0.077** (~5×).
- **Mean wall-clock / scenario**: A = **360 s** · B = **4 s** (~90×).

### Important honesty caveat on the overlap numbers

Path A's overlap is *biased upward* by the mock client returning the scenario's first 3 expected insights verbatim. Path B's is *biased downward* by the heuristic insight synthesis (`_heuristic_insight_for_failure`) which paraphrases the failure rather than running an actual model turn. Neither number is a measurement of real quality.

What the mock shape **does** legitimately demonstrate — and is the load-bearing structural finding — is the **shape of the cost / latency profile**: Dreams is async, batch, expensive (whole-store input), high-quality-when-it-works; B12 is sync, per-failure, cheap, narrow. They do not occupy the same niche. Replacing B12 with Dreams would lose the synchronous failure-recovery pathway entirely.

---

## 4. Coverage analysis — disjoint operating modes

The mock numbers in §3 hide the more important finding: Dreams and B12 catch **structurally different** classes of value, the same way b3-outcomes-spike found B3 and Outcomes did.

| Capability | B12 (sync reflection) | Dreams (async curation) | Why |
|---|---|---|---|
| Per-failure recovery during a live attempt | **Yes** (OP-850 design intent) | No (async, post-hoc) | B12's bread-and-butter |
| Cross-session pattern extraction | No (per-attempt scope) | **Yes** (up to 100 sessions) | Dreams's bread-and-butter |
| Memory consolidation (dedupe / contradiction resolution) | No | **Yes** (per docs §How it works) | Dreams-only |
| Bounded retry budget enforcement | **Yes** (`REFLECTION_LIMIT=3`) | N/A | B12-only |
| Failure-class index (master plan §3.3) | Consumes one | Could populate via `instructions` | Both, differently |
| Operator-reviewable curated output | No (inline turn) | **Yes** (separate output store) | Dreams-only |
| Works on bare Messages API today | **Yes** | **No** (requires Managed Agents) | The blocker |

Conclusion: **the comparison is not "which one wins"; it's "which gap each fills"**. B12 is the right primitive for the per-attempt failure case. A Dreams-equivalent (or Dreams itself, if Managed Agents lands) is the right primitive for periodic memory-store curation — *if* a memory store exists for it to curate. Today OmniSight has no production memory store consumer, so the Dreams output would land on no rails.

---

## 5. Caveats discovered

1. **Managed Agents is the actual scope**, not Dreams alone. Dreams is one of several features (managed sandboxes, hosted memory, multi-agent, outcomes) gated on the same migration. Picking Dreams in isolation distorts the migration-vs-stay decision; it should be made as one Tier L call.
2. **Memory Store consumer is missing.** Dreams emits a curated store. Without C1 (Memory Tool wiring) or an equivalent agent-side reader, the output goes nowhere. The master plan §3.8 *original* spec correctly chained C7 after C1; the OP-857 pivot description omits this dependency. The dependency is real.
3. **Async cadence is not "incident replay".** The OP-857 ticket framing — "replay all-incidents through B12 reflection loop" — suggests a synchronous reaction-time loop. Dreams is *batch curation* on the time scale of minutes-to-tens-of-minutes per dream. They are different latency tiers; rebadging one as the other would mislead operator expectations. The master-plan §3.8 framing ("nightly background process") matches Dreams's actual cadence.
4. **Cost scales with memory-store + session size, not "per dream".** Per docs §Billing: "standard API token rates for the model you select; cost scales roughly linearly with the number and length of input sessions". A naive "dream over the last 24h of incidents" could submit 100 sessions × ~12k tokens of Opus input ≈ $18 per nightly dream just on input. Sustainable, but not free; budget guard should land *before* the cron does.
5. **Async non-determinism**. Same input may produce different curated stores across runs (the docs do not promise determinism, and the pipeline includes a sub-session whose model behaviour is stochastic). Reproducibility is qualitatively lower than B12, which reproduces the exact `ReflectionInput` payload bit-for-bit. For incident-postmortem use cases this is acceptable; for regression-test fixtures it is not.
6. **Research-preview risk gate (per ticket §"Error catalog")**. `dreaming-2026-04-21` is dated; the API may rev or disappear. Any production wiring needs a `try/except DreamingAPISchemaUnexpected` adapter (the spike already builds one) + a feature flag (`OMNISIGHT_DREAMING_ENABLED=0` default).
7. **JSONL → session translator is not free.** A first-cut translator that creates an empty Managed Agents session and stuffs the transcript into events is feasible (~1-2 days of engineering) but loses the *real* runtime trace shape (server-side tool execution, cache state, env). The translator's output dreams may produce lower-quality curation than dreams over real Managed Agents sessions; that's a measurement gap C7b would close.

---

## 6. Sprint children to file (proposed)

**C7a — Managed Agents harness pilot** — Tier L, area `backend|devops|tooling` (DEFERRED).

Scope:
- Stand up one parallel runner instance (e.g. `claude-bot-managed`) that uses `client.beta.sessions.create` instead of the bare Messages-API loop.
- Pick 5 low-risk Tier S tickets and run them end-to-end through Managed Agents.
- Measure: cost delta, latency delta, tool-execution behaviour delta, failure-mode parity vs the bare-API runner.
- Deliverable: a follow-up decision document `docs/research/managed-agents-pilot-2026-XX.md` recommending fully migrate / partial / abandon.

Rationale: every other Managed Agents feature (Dreams, multi-agent, outcomes, hosted memory) hinges on this; making it once + measuring once is cheaper than evaluating each feature against the bare-API baseline separately.

**C7b — JSONL → Managed Agents session translator spike** — Tier S, area `backend` (DEFERRED on C7a).

Scope:
- ~150 LOC translator that consumes `~/.local/state/omnisight-runner/transcripts/*.jsonl` and produces a Managed Agents session via the documented Sessions API.
- Lossy by construction (no real server-side tool execution); document what fidelity is lost.
- Deliverable: `scripts/translate_jsonl_to_managed_session.py` + a comparison run of one historical ticket through translator-then-Dreams vs the originating L-OP retrospective.

Rationale: ungated by C7a because the translator can be evaluated standalone; if it produces useful Dreams output even with lossy fidelity, the historical-incident value of Dreams is unlocked without the runtime migration.

**Do NOT file**:
- A direct `OMNISIGHT_DREAMING_ENABLED=1` runner-mode child today (per ticket AC #4 conditional). The dependency chain (Managed Agents pilot → translator → memory-store consumer → cron) is too long to land as one child.
- Any deprecation of B12 / OP-850. The b3-outcomes-spike-precedent (don't delete resilience on a vendor-claim hearing) applies symmetrically here.

---

## 7. What this spike did NOT investigate (out of scope)

- **Live Dreams calls.** Gated on Managed Agents access + the translator (see §5.7).
- **Memory Tool / C1 wiring.** Listed in master plan §3.1 as a Phase 1 Sprint C dependency; out of OP-857's `backend|docs|tests` scope (would touch the agent-loop init, which is on the C1 ticket's path).
- **Failure-class index (C2) integration.** Master plan §3.3; the OP-857 BlockedBy field cites C2, but C2 itself is not yet filed. The harness reuses the L-OP-827/L-OP-837 retrospective tags as a proxy for the failure-class enum; once C2 lands, the spike's `IncidentScenario.expected_insights` field is the natural integration point.
- **Multi-agent / outcomes interaction with Dreams.** Each is a separate Managed Agents feature; the comparison would belong inside C7a's pilot report, not here.
- **Cost-guard / circuit-breaker integration for the dream cadence.** A nightly cron that submits 100 sessions per dream needs the same `cost_guard` envelope as live runner attempts; defer to C7a.

---

## 8. Pointers for the reviewer

- Harness: `scripts/spike_c7_dreaming_compare.py` (~700 LOC including docstrings + 3 scenarios + 3 sanity tests; sub-CLI `--tests` runs the sanity suite, `--mode live` is a deliberate `SystemExit` until C7a/b land).
- Raw outputs: `data/op-857-dreaming-comparison.json` (deterministic; identical by construction).
- B12 production code being compared against: `backend/agents/reflection_loop.py` (OP-850), `backend/tests/test_reflection_loop.py`.
- Master-plan reference: `docs/audit/2026-05-11-sprint-abc-master-plan.md` §3.8 (original C7 spec — note the OP-857 pivot reframed it; both framings are addressed in §1 / §6 above).
- Prior-art template: `docs/research/b3-outcomes-spike-2026-05.md` (same structural shape; C7 deliberately mirrors it for reviewer ergonomics).
- Lesson candidate: if reviewer agrees the §6 deferral is the right call, file `L-OP-857-vendor-feature-requires-runtime-migration.md` (proposed: *"vendor capability whose inputs require a runtime re-platform is a Tier L decision, not a Tier M spike — surface the migration before the feature"*). Not pre-required per ticket §"Files touched".
