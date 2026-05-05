---
audience: internal
risk_id: R10
risk_title: RLM library full-integration temptation
severity: medium impact x low likelihood
status: open - Option B remains active; Option A trigger not met
owners: architect / forensics / pm
landed: 2026-05-06 (BP.W3.9)
close_out: re-evaluate only when Appendix C trigger is met
---

# Risk R10 - RLM library full-integration feasibility

> TL;DR: BP.W3.9 re-ran the Appendix C research trigger check for the
> `rlms` library. The trigger is NOT met as of 2026-05-06. OmniSight
> should keep the current Option B implementation: borrow the
> partition-map-summarize pattern, keep `DEPTH_CAP = 1`, inherit the
> existing token budget, and do not install the `rlms` package.

This document is the BP.W3.9 feasibility record for full RLM library
integration. It does not replace the ADR source of truth:
`docs/design/blueprint-v2-implementation-plan.md` R10 and Appendix C.
It records the current evidence check so future agents do not repeat the
same "should we just pip install rlms?" discussion without new evidence.

---

## 1. Decision

**Decision:** Do not full-integrate the RLM library into OmniSight now.

**Reason:** The ADR R10 / Appendix C trigger requires both:

1. At least three independent reproduction papers.
2. At least one big-company production deployment report.

Current public evidence does not satisfy either bar. The evidence base
has grown since the original ADR, but it is mostly the original paper,
the authors' open-source library, follow-on research variants, startup /
vendor demos, and implementation guides. That is useful surveillance
signal, not enough to change the integration posture.

**Current posture remains:** Option B. OmniSight already ships the useful
piece in `backend/rlm_dispatch.py`: long `analysis` / `audit` /
`forensics` tasks above 100,000 tokens can route through
partition-map-summarize with a hard depth cap of 1 and fail-open
fallback to standard dispatch.

---

## 2. Evidence check

Research pass date: 2026-05-06.

| Evidence class | Found | Qualifies for Appendix C trigger? | Notes |
|---|---:|---|---|
| Original RLM paper | 1 | No | Primary claim source, not a reproduction. |
| Official `alexzhang13/rlm` library | 1 | No | Implementation source, not independent validation. |
| Independent follow-on papers | 3 | Partially | Useful critique / variants, but not three direct reproduction papers. |
| Vendor / startup production write-ups | 2 | No | Not big-co deployment reports; mostly architecture / marketing. |
| Big-company production deployment report | 0 | No | No public qualifying report found. |

Sources checked:

* Original paper: https://arxiv.org/abs/2512.24601
* Official library: https://github.com/alexzhang13/rlm
* SRLM follow-on paper page: https://huggingface.co/papers/2603.15653
* lambda-RLM follow-on paper page: https://huggingface.co/papers/2603.20105
* RLM-JB application paper: https://arxiv.org/abs/2602.16520
* ZenML production discussion: https://www.zenml.io/blog/rlms-in-production-what-happens-after-the-notebook
* CodeTether RLM product page: https://codetether.run/

### 2.1 What changed since ADR R10

The original paper now has a public library and several follow-on
projects. This confirms RLM as an active research direction, especially
for long-context tasks where the model needs programmatic exploration
rather than static retrieval.

The follow-on research also strengthens the original caution: open-ended
recursive code generation creates predictability, termination, cost, and
security questions. SRLM argues that uncertainty-guided program search
can outperform plain RLM under a fixed time budget and notes degradation
inside the model context window. lambda-RLM replaces the open REPL with
typed combinators specifically to obtain termination and cost bounds.
Those are not reasons to install `rlms`; they are reasons to preserve
OmniSight's depth cap and explicit dispatch heuristics.

### 2.2 What did not change

No qualifying big-company production report was found. The public
production material is from vendors / startups and describes prototype
or productized variants, not a large-company postmortem with workload,
latency, cost, safety, rollback, and incident data.

No set of three independent reproduction papers was found that directly
reproduces the original RLM claims across OmniSight-relevant workloads.
Follow-on papers may cite or compare against RLM, but the Appendix C bar
was intentionally stricter than "new papers exist".

---

## 3. OmniSight architecture fit

Candidate integration point: Forensics Guild Context Absorber
(`docs/design/enterprise-level-multi-agent-software-factory-architecture.md`
Context Absorber / `analyze_massive_crash_dump()`).

This remains the best future target because crash dumps and large log
bundles match the RLM problem shape: huge context, sparse evidence,
forensic summarization, and natural chunk-level map work.

Full library integration is still a poor fit today:

* **Sandbox boundary:** The official Python library's default quick-start
  path uses a REPL / `exec` style environment in the host process. That
  collides with OmniSight's PEP Gateway, sandbox-tier policy, audit
  chain, and future Phase D auxiliary-disclaimer posture.
* **Cost boundary:** Open recursion makes sub-LM call count hard to
  reason about. OmniSight already has BP.H.2.b recursive-subcall budget:
  yellow card at `count > 3`, red card at `count > 5`.
* **Determinism boundary:** Forensics output must be auditable. A model
  deciding arbitrary recursive code paths is harder to review than the
  existing explicit partition-map-summarize plan.
* **Regression boundary:** The current Option B dispatcher excludes
  `crud`, `retrieval`, and `simple_lookup`; full integration would need
  stronger negative tests to prove simple work does not regress.

---

## 4. Acceptance criteria if the trigger is met later

If Appendix C is satisfied in the future, do not replace
`backend/rlm_dispatch.py` directly. Start with a narrow Forensics Guild
experiment behind an explicit opt-in flag.

Minimum acceptance criteria:

1. `rlms` remains off by default.
2. Integration point is only Forensics Guild Context Absorber at first.
3. Recursion depth remains capped at 1 until production evidence supports
   a higher bound.
4. Recursive subcall budget events feed BP.H.2.b red-card evaluation.
5. PEP Gateway owns any code-execution / REPL boundary; no host-process
   `exec` path is allowed in production.
6. Every RLM run emits an audit trajectory with root task id, chunk ids,
   model ids, token cost, elapsed time, and final summarizer input.
7. Regression suite compares RLM mode against vanilla / current Option B
   for forensics tasks and includes negative tests for simple tasks.
8. Rollback is one env flag / config flip back to current Option B.

---

## 5. Current close-out

BP.W3.9 is closed as "evaluated, trigger not met". This is not a
runtime feature launch.

No code, dependency, Alembic migration, production image, env knob,
Docker network, or persistent schema changed. The operational next gate
is evidence-driven: re-run Appendix C only when the public evidence set
contains at least three qualifying reproduction papers and one qualifying
big-company production deployment report, or when another Appendix C
trigger fires.
