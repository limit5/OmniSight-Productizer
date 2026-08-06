export const meta = {
  name: 'audit-3d-memory-system',
  description: 'Adversarial multi-dimension audit of the U6 / 3D memory system: liveness, anti-hollow instrumentation, kernel safety invariant, poisoning surface, the three legs, durability, and design coherence',
  phases: [
    { title: 'Audit', detail: 'one agent per dimension, evidence-first' },
    { title: 'Verify', detail: 'adversarial refutation of every finding' },
    { title: 'Critic', detail: 'what did the sweep miss' },
    { title: 'Synthesis', detail: 'ranked report' },
  ],
}

const GROUND = `
CONTEXT — read this before anything.

TARGET: the "3D memory system" of the OmniSight project = the runner-learning /
project-state memory with THREE AXES (structural, temporal, causal), plus the wider
U6 "three principals, one governed kernel" memory architecture:
  leg-1 Sora 3-tier (L1 working / L2 episodic / L3 semantic)
  leg-2 worker experience loop (curator/distiller/eval/publication/injection/citations)
  leg-3 Claude cross-session memory (file store -> claude_memory_* tables)
  shared: an action-capability authorization kernel whose INVARIANT is
  "persistent memory can NEVER authorize a side effect; enforced in code, not prompt".

CRITICAL HISTORY — this system was audited 2026-07-07 and found DEPLOYED-BUT-HOLLOW:
flags on, endpoints called, blocks injected, but 3 of 4 data sources delivered nothing
(a permanent 'if False' JIRA stub, cognee dying on sqlite, a graphiti dispatcher never
assigned, and a causal axis whose only writer never existed). Every failure layer was
degrade-silent, so ZERO alarms ever fired. The team named this the "silent-hollow
compound" anti-pattern. Phase R (13 tickets, OP-2535..OP-2551) then claimed to repair it.

YOUR JOB IS TO FIND WHAT IS STILL WRONG. Assume the repair is incomplete until proven
otherwise. The single most valuable thing you can produce is evidence that something
reports success while not doing its job.

WHERE THINGS LIVE:
  /home/user/work/sora/OmniSight-betaF          repo at develop tip (READ THIS as current code)
  /home/user/omnisight-prod                     release-pinned checkout prod actually runs
  /home/user/sora-bridge                        STALE tree (240 commits behind) that
                                                pipeline-coordinator executes from
  Runner code SELF-DEPLOYS per cycle: run-ephemeral.sh git-clones fresh each cycle and
  runs auto-runner-jira.py from the clone, so merged-to-develop == live for RUNNER code.
  Backend code runs from the prod IMAGE, so merged != live for BACKEND code.
  This distinction matters enormously for "is the fix actually live".

HARD CONSTRAINTS — this host runs many unrelated production projects (omnisight-tcm,
omnisight-fate, omnisight-ai-core, cognee, eq-*, pos-kiosk) and a shared postgres.
STRICTLY READ-ONLY. No systemctl start/stop/restart/enable, no writes outside /tmp,
no mutating docker exec, no INSERT/UPDATE/DELETE/DDL, no API POST/PUT/DELETE, no git
mutation, no AWS calls. SELECT queries and read-only inspection are fine. NEVER print a
credential value.

EVIDENCE RULES:
 - cite file:line or the exact command and its output
 - mark every claim EVIDENCE (you ran/read it) or INFERENCE (you reasoned it)
 - a query returning zero rows is NOT proof of absence until you have proven the query
   works (run it against something you know matches). This project has been bitten
   repeatedly by broken verification harnesses producing confident wrong answers.
 - if you cannot check something, say so explicitly rather than assuming
`;

const FINDINGS_SCHEMA = {
  type: 'object',
  required: ['dimension', 'summary', 'findings'],
  properties: {
    dimension: { type: 'string' },
    summary: { type: 'string', description: 'two sentences: overall state of this dimension' },
    checks_performed: { type: 'array', items: { type: 'string' }, description: 'what you actually ran/read, so coverage is auditable' },
    could_not_check: { type: 'array', items: { type: 'string' } },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'severity', 'evidence', 'evidence_type', 'expected_vs_actual', 'impact'],
        properties: {
          title: { type: 'string' },
          severity: { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'] },
          category: { type: 'string', description: 'bug | hollow | security | design | mismatch-with-intent | data-integrity' },
          evidence: { type: 'string', description: 'file:line or command+output' },
          evidence_type: { type: 'string', enum: ['EVIDENCE', 'INFERENCE'] },
          expected_vs_actual: { type: 'string' },
          impact: { type: 'string' },
          silent: { type: 'boolean', description: 'true if this fails without any alarm firing' },
        },
      },
    },
  },
};

const VERDICT_SCHEMA = {
  type: 'object',
  required: ['verdict', 'reasoning'],
  properties: {
    verdict: { type: 'string', enum: ['CONFIRMED', 'REFUTED', 'PARTIALLY_CONFIRMED'] },
    corrected_severity: { type: 'string', enum: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'] },
    reasoning: { type: 'string' },
    correction: { type: 'string', description: 'if the finding is wrong or overstated, what is actually true' },
  },
};

const DIMENSIONS = [
  {
    key: 'axis-liveness',
    prompt: `DIMENSION: are the three axes actually returning DATA today, post-Phase-R?

This is the heart of the "hollow" question. For EACH axis determine whether it delivers
non-empty content in the live system, and whether an empty axis is detectable:
  - structural / JIRA pull (was 'if False' stub at project_state_aggregator.py:299)
  - structural / cognee knowledge graph (was dying: sqlite unable to open database file;
    prod graph provider was embedded ladybug NOT neo4j)
  - temporal / graphiti (_DEFAULT_DISPATCHER never assigned -> axis always {})
  - causal / runner incidents (the live PG writer allegedly never existed; all 1935 rows
    were 64-hex sha256 backfill ids, live path would be 32-hex uuid)

Read backend/agents/project_state_aggregator.py and everything it calls. Then check the
LIVE data: query the prod postgres for runner_incidents (count, id shape 64-hex vs 32-hex
vs 'live-v1-' prefix, newest timestamp), check whether the cognee container is running and
whether its store has content, and look for any recorded project-state responses.
Determine per axis: WIRED? and NON-EMPTY? Those are different questions and both matter.`,
  },
  {
    key: 'anti-hollow',
    prompt: `DIMENSION: if an axis goes empty again, does ANYTHING notice?

Phase S was supposed to add anti-hollow hardening: a Prometheus exporter, a
NON-EMPTY-AXIS-RATE content SLO, alerting, and a staging-gate probe. The original audit
found slo_monitor querying a metric 'project_state_api_p95_ms' that NO exporter emits.

Determine: does that exporter exist now? Is the metric actually emitted (check the live
/metrics endpoint if reachable read-only, and check Prometheus targets/rules)? Is there a
content-quality SLO distinct from a liveness/latency SLO? Is there an alert that fires on
an EMPTY axis as opposed to on an error? Does the OP-2544 canary timer exist and has it
ever run? Check systemctl --user list-timers and the canary unit.

The key question: reproduce the reasoning that made this system hollow for months without
an alarm, and determine whether that could happen again TODAY. Look specifically for
degrade-silent paths: try/except that swallow, '|| true', empty-dict fallbacks, and
anywhere a failure becomes an empty result rather than an error.`,
  },
  {
    key: 'kernel-invariant',
    prompt: `DIMENSION: the safety invariant — can persistent memory authorize a side effect?

The stated NON-NEGOTIABLE invariant is: "persistent memory, and any model-authored
content, can NEVER authorize a side effect; enforce in code, not prompt."

Find the authorization kernel (search for authorization_kernel, capability, requires_grant,
propose_action). Read it. Then ADVERSARIALLY test the invariant by reading code paths:
 - does any mutating operation consult memory-derived content in its authorization decision?
 - can a memory record influence a capability grant, a label, a tier, a claim, or a route?
 - does the kernel ever return 'allow' on a model path, or only 'requires_grant'?
 - does it read provenance, and could a forged provenance value change its answer?
 - Sora holds propose_action/create_task: can an injected memory cause a task to be filed
   with attacker-chosen labels, class, tier, or capability grants?
Trace at least two concrete end-to-end paths from "a memory record exists" to "a side
effect happens" and state exactly what stops it, or that nothing does.`,
  },
  {
    key: 'poisoning-surface',
    prompt: `DIMENSION: memory poisoning and prompt-injection surface.

Memory that is injected into an acting agent is an injection channel. Audit:
 - WRITE side: what can write a memory row? Is there a write-time classification gate
   (declarative facts OK, imperative/instruction-shaped REFUSED)? Find it and test its
   robustness by reading its rules — what imperative phrasing would slip through?
 - Is user verbatim ever persisted, or only model-side distillation? Verify in code.
 - READ side: is injected memory fenced as data? Is there an injection-guard prelude? Is
   there a hard token cap? Find them and check they are actually applied on every path,
   not just one.
 - QUARANTINE/provenance: do new writes land quarantined? Who can promote? Is promotion
   human-gated or automatable?
 - Consider the MINJA threat named in the project's own research: poisoned content arriving
   via JIRA ticket bodies or repo content that then gets distilled into a lesson/memory.
   Is there any path where externally-controlled text becomes injected memory without a
   human in the loop?
Cite the actual guard code. If a guard exists but is bypassable on some path, that is the
finding.`,
  },
  {
    key: 'leg1-sora-3tier',
    prompt: `DIMENSION: leg-1, Sora's 3-tier persistent memory (L1 working / L2 episodic / L3 semantic).

The design mandates, as NON-NEGOTIABLE governance: never write user verbatim; write-time
classification gate; semantic layer starts ALL pending-confirm; provenance per row;
/memories list+delete UI; read-time fencing + token cap; per-user scoping; ship WITH
metrics (hit rate, injected tokens, non-empty rate, write+refusal counts) or don't ship;
flag default-off; staging dogfood first.

Audit each of those against the code and the live DB (l3_facts, l3_approvals, l3_eval_runs,
chat_session_summaries). Which are implemented, which are stubs, which are claimed but
absent? Is the flag actually default-off? Are the metrics emitted or just declared? Is
per-user scoping enforced in SQL (RLS or a predicate) or only in application code — and
note that the app DB role was measured as rolsuper=t rolbypassrls=t, so RLS-based scoping
may not bind. Check whether L2 episodic (chat_session_summaries) has any delete path.`,
  },
  {
    key: 'leg2-worker-loop',
    prompt: `DIMENSION: leg-2, the worker experience loop.

The loop is: merge -> verify -> curator -> distiller -> eval (2-leg holdout) ->
human-approval -> publication -> injection -> citations. It was reported CODE-COMPLETE and
STAGING-GREEN with a calibration pass (19/19 negatives refused, 21/21 positives).

Audit for the anti-hollow property specifically: does each stage actually run in
production, or only on staging? Is the eval genuinely anti-hollow (can it pass on an empty
or trivial input)? Is human approval actually required, or is there an auto-approve path?
Are citations tracked, and is the utility metric (hits x success-delta) computed from real
data or a placeholder? Look for the lessons corpus and check whether lesson injection is
live in runners (RPG_PROMPT_ENRICH, embedder) and whether the retrieval is measured.

Also: the project's own research says "NOBODY ships ungated reflection in production" and
"benign memory accumulation alone decays safety (Misevolution) -> periodic bank audit".
Is there a periodic bank audit? If not, that is a design gap worth naming.`,
  },
  {
    key: 'leg3-claude-memory',
    prompt: `DIMENSION: leg-3, Claude cross-session memory (the file store and its DB substrate).

Components: ~/.claude/projects/<project>/memory/*.md (file store), MEMORY.md (the index
loaded into context every session), scripts/claude_memory_ingest.py, claude_memory_regen.py,
the claude_memory_* tables, and the publish state machine.

Audit:
 - the index/store split: MEMORY.md is budgeted (~24KB) but measured at ~50KB with 145 of
   174 entries over the 200-char guideline. What breaks when the index exceeds the budget —
   is it truncated, and is the truncation silent? Find the actual mechanism.
 - ingest: is it idempotent by body sha as claimed? What happens to a file whose body
   changes — new version row, or overwrite? Is there any path that LOSES a prior version?
 - regen: claude_memory_regen.py rewrites MEMORY.md from the DB. What happens if the DB has
   fewer entries than the file (it had a shrink-ratchet added)? Could regen destroy
   hand-curated hooks?
 - the divergence: files were sanitised 2026-07-28 but the DB rows are append-only, so DB
   and files now differ. Is that divergence detected or silent? What does regen do about it?
 - publish: what makes a version LIVE, and is there a path where an unpublished or
   quarantined version gets injected anyway?`,
  },
  {
    key: 'durability-integrity',
    prompt: `DIMENSION: durability, retention and data integrity of memory.

 - Which memory stores are covered by a backup and which are not? The postgres side is
   backed up (encrypted, off-site since 2026-07-27). The file store under ~/.claude/projects
   was measured as NOT covered by any backup lane. Confirm both.
 - Retention: the project committed (OP-2747 decision 3) that retention is compliance-driven
   but MUST END, bounded by lifecycle. Do any memory tables have a retention/expiry rule?
   l3_facts, chat_session_summaries, claude_memory_versions, runner_incidents, episodic_memory.
   An unbounded personal-data store is a finding.
 - Erasure: does a DSAR erasure actually remove memory rows? Check whether the erasure path
   covers l3_facts / l3_eval_runs / l3_approvals / chat_session_summaries / episodic_memory.
   Note there is an open epic (OP-2750..2758) about exactly this — your job is to state the
   CURRENT live state, not the planned one.
 - Integrity: is there anything that detects silent corruption or unexpected deletion of
   memory rows? Any checksum, count monitor, or audit trail?`,
  },
  {
    key: 'design-coherence',
    prompt: `DIMENSION: does the architecture actually cohere, or has it drifted into silos?

The north star is ONE governed memory serving THREE principals sharing ONE kernel. The
2026-07-07 audit made a pointed observation: the HEALTHIEST subsystem (lessons + L2
distilled-skill retrieval + RPG enrich) is OUTSIDE the 3-axis design, and warned that the
upgrade should unify and feed the dead axes from the living systems rather than add a 7th silo.

Audit for drift:
 - enumerate every distinct memory store that exists today (tables, files, volumes, caches)
   and say which principal and which leg each belongs to. Count the silos.
 - do the three legs actually share the kernel, or does each have its own ad-hoc gate?
 - is there duplicated or contradictory state between stores (e.g. the same fact in
   episodic_memory and l3_facts and a lesson file)?
 - are there stores nobody reads (write-only), or readers with no writer (read-only-empty)?
 - name any place where the implemented design contradicts the stated design intent.
This dimension is about judgement, not just greps — but ground every claim in evidence.`,
  },
];

phase('Audit');

const audited = await pipeline(
  DIMENSIONS,
  (d) => agent(`${GROUND}\n\n${d.prompt}\n\nReturn structured findings. Be exhaustive within your dimension; do not stray into others.`,
    { label: `audit:${d.key}`, phase: 'Audit', schema: FINDINGS_SCHEMA }),
  // verify each finding as soon as its dimension completes — no barrier
  (res, d) => {
    if (!res || !res.findings || res.findings.length === 0) return { dimension: d.key, result: res, verified: [] };
    const toCheck = res.findings.slice(0, 12);
    return parallel(toCheck.map((f) => () =>
      agent(`${GROUND}

ADVERSARIAL VERIFICATION. Another auditor reported the finding below. Your job is to
REFUTE it. Default to REFUTED if you cannot independently reproduce the evidence.

Common ways such a finding is wrong: the code was read in a stale tree; the "live" state
was checked in the wrong place (repo vs prod image vs runner clone); a zero-row query was
actually a broken query; the behaviour is intentional and documented elsewhere; the
severity assumes a reachable path that is in fact gated upstream.

DIMENSION: ${d.key}
TITLE: ${f.title}
SEVERITY CLAIMED: ${f.severity}
EVIDENCE CLAIMED: ${f.evidence}
EXPECTED VS ACTUAL: ${f.expected_vs_actual}
IMPACT CLAIMED: ${f.impact}

Independently check it. Then return your verdict, and if it survives but is mis-sized,
correct the severity.`,
        { label: `verify:${d.key}`, phase: 'Verify', schema: VERDICT_SCHEMA })
        .then((v) => ({ finding: f, verdict: v }))
    )).then((verified) => ({ dimension: d.key, result: res, verified: verified.filter(Boolean) }));
  }
);

phase('Critic');

const coverage = audited.filter(Boolean).map((a) => ({
  dimension: a.dimension,
  summary: a.result && a.result.summary,
  checks: (a.result && a.result.checks_performed) || [],
  gaps: (a.result && a.result.could_not_check) || [],
  finding_titles: (a.result && a.result.findings || []).map((f) => `${f.severity}: ${f.title}`),
}));

const critics = await parallel([
  () => agent(`${GROUND}

COMPLETENESS CRITIC. Nine auditors swept the memory system. Below is what each covered and
what each said it could not check. Your job: name what the sweep MISSED.

Think about: subsystems nobody looked at; a failure mode that spans two dimensions so both
assumed the other covered it; claims accepted from documentation rather than verified
against running code; the difference between "merged to develop" and "live in the prod
image" and "live in the runner clone"; anything in the 2026-07-07 hollow audit that was
never re-checked.

Go and CHECK the most promising gaps yourself, then report them as findings.

${JSON.stringify(coverage).slice(0, 24000)}`,
    { label: 'critic:coverage', phase: 'Critic', schema: FINDINGS_SCHEMA }),

  () => agent(`${GROUND}

HOLLOW-HUNTER. Ignore the other auditors. Your single question is:

"What in this memory system today would keep reporting SUCCESS while delivering NOTHING?"

That exact failure shape already happened here once, for months, across three of four data
sources, with zero alarms. Hunt for its recurrence anywhere in the memory stack. Look for:
empty-dict/empty-list fallbacks on exception; functions whose failure path returns the same
type as success; metrics that count invocations rather than non-empty results; health checks
that assert reachability rather than content; caches that mask a dead source; any consumer
that cannot distinguish "no data" from "not wired".

Prove each candidate by tracing what an operator would actually SEE if that path went dead
today. Report only cases where the answer is "nothing".`,
    { label: 'critic:hollow', phase: 'Critic', schema: FINDINGS_SCHEMA }),

  () => agent(`${GROUND}

INTENT-MISMATCH AUDITOR. The user's question was whether the system matches EXPECTATIONS.
Several expectations are on record:
 - memory "keeps growing without being limited by capacity"
 - memory survives interruption/restart without loss
 - ONE governed memory, three principals, one kernel
 - "persistent memory can never authorize a side effect"
 - governance NON-NEGOTIABLES for Sora's tiers (never user verbatim; pending-confirm
   semantic; provenance; delete UI; metrics or don't ship)
 - the leg-3 store is unbounded but its INDEX is budgeted

For each, determine whether the built system actually delivers it, and where the gap
between stated intent and implementation is. Be specific about which expectation is
violated and by what. This is about design honesty, not bugs per se — but ground every
claim in code or live state.`,
    { label: 'critic:intent', phase: 'Critic', schema: FINDINGS_SCHEMA }),
]);

phase('Synthesis');

// The first run truncated its own input: 165 findings (~375k chars) were passed
// through .slice(0, 60000), so the report was written from ~16% of the material
// and correctly complained that six dimensions never reached it. Fixed by
// summarising per dimension FIRST, then synthesising over the summaries, so no
// stage ever has to swallow the whole corpus.

const perDimension = audited.filter(Boolean).map((a) => {
  const dim = (a.result && a.result.dimension) || a.dimension || 'unknown';
  const verified = (a.verified || []).map((v) => ({
    title: v.finding.title,
    claimed_severity: v.finding.severity,
    category: v.finding.category,
    evidence: v.finding.evidence,
    evidence_type: v.finding.evidence_type,
    expected_vs_actual: v.finding.expected_vs_actual,
    impact: v.finding.impact,
    silent: v.finding.silent,
    verdict: v.verdict && v.verdict.verdict,
    final_severity: (v.verdict && v.verdict.corrected_severity) || v.finding.severity,
    verification: v.verdict && v.verdict.reasoning,
    correction: v.verdict && v.verdict.correction,
  }));
  const unverified = ((a.result && a.result.findings) || []).slice(12).map((f) => ({
    title: f.title, claimed_severity: f.severity, category: f.category,
    evidence: f.evidence, evidence_type: f.evidence_type,
    expected_vs_actual: f.expected_vs_actual, impact: f.impact, silent: f.silent,
    verdict: 'NOT_VERIFIED',
  }));
  return {
    dim,
    summary: (a.result && a.result.summary) || '',
    checks: (a.result && a.result.checks_performed) || [],
    gaps: (a.result && a.result.could_not_check) || [],
    verified, unverified,
  };
});

const dimReports = await parallel(perDimension.map((d) => () =>
  agent(`${GROUND}

You are compacting ONE dimension of a completed audit into a report section. Do not
re-audit. Do not invent. Work only from what is given.

DIMENSION: ${d.dim}
AUDITOR SUMMARY: ${d.summary}
CHECKS PERFORMED: ${JSON.stringify(d.checks).slice(0, 4000)}
COULD NOT CHECK: ${JSON.stringify(d.gaps).slice(0, 2000)}

FINDINGS THAT WENT THROUGH ADVERSARIAL VERIFICATION:
${JSON.stringify(d.verified).slice(0, 40000)}

FINDINGS NOT VERIFIED (beyond the verification cap — treat as provisional):
${JSON.stringify(d.unverified).slice(0, 12000)}

Produce a tight section: the state of this dimension in 2-3 sentences, then the findings
that survived verification ranked by final severity (drop REFUTED ones but say how many
were refuted and if any refutation is itself interesting), then provisional findings
briefly, then what this dimension could not check. Preserve file:line evidence verbatim —
it is the value of the report. Mark EVIDENCE vs INFERENCE. Be concise but lose nothing
load-bearing.`,
    { label: `compact:${d.dim.slice(0, 28)}`, phase: 'Synthesis' })
));

const sections = dimReports.filter(Boolean);
log(`compacted ${sections.length} dimension reports; total chars ${sections.join('').length}`);

const stats = perDimension.map((d) => ({
  dim: d.dim.slice(0, 60),
  verified: d.verified.length,
  confirmed: d.verified.filter((v) => v.verdict === 'CONFIRMED').length,
  partial: d.verified.filter((v) => v.verdict === 'PARTIALLY_CONFIRMED').length,
  refuted: d.verified.filter((v) => v.verdict === 'REFUTED').length,
  unverified: d.unverified.length,
}));

const report = await agent(`${GROUND}

FINAL SYNTHESIS. Write the audit report the operator asked for: a deep audit of the 3D
memory system covering bugs, errors, mismatches with expectations, vulnerabilities, flaws
and unreasonable design. REPORT ONLY — propose no fixes beyond a recommended ordering.

You are given a compacted section per dimension, each already filtered through adversarial
verification. Twelve agents audited (nine dimensions + three critics); 165 findings were
produced and 103 went through refutation.

Write in this shape:

1. VERDICT — is the 3D memory system working, hollow, or partially hollow? One decisive
   paragraph that answers the operator's real question without hedging.
2. HEADLINE FINDINGS — the handful that actually matter, most severe first. For each:
   what is wrong, the evidence, what it means in practice, whether it fails SILENTLY.
3. BY DIMENSION — every one of the twelve, compactly. Do not skip any.
4. WHAT IS ACTUALLY HEALTHY — with evidence. Be fair; an audit that only lists problems is
   not usable, and some of this system was genuinely repaired.
5. EXPECTATION GAPS — where the built system differs from stated intent, including the
   operator's own expectations: memory grows without capacity limits; memory survives
   restart without loss; one governed memory across three principals; memory can never
   authorize a side effect.
6. WHAT COULD NOT BE VERIFIED — the honest limits.
7. RANKED REMEDIATION ORDER — sequence and reasoning only. No fixes.

Rules: dedupe aggressively across dimensions — several auditors found the same defects.
Distinguish EVIDENCE from INFERENCE. Do not inflate severity. Where a finding was REFUTED
and that refutation is itself informative (the system being healthier than assumed), say so.
Flag contradictions between dimensions rather than silently picking one.

VERIFICATION STATS PER DIMENSION:
${JSON.stringify(stats)}

DIMENSION SECTIONS:
${sections.map((s, i) => `\n\n========== SECTION ${i + 1} ==========\n${s}`).join('')}`,
  { label: 'synthesis-final', phase: 'Synthesis' });

const allVerified = perDimension.flatMap((d) => d.verified);
return {
  dimensions: perDimension.length,
  raw_findings: perDimension.reduce((n, d) => n + d.verified.length + d.unverified.length, 0),
  verified: allVerified.length,
  confirmed: allVerified.filter((v) => v.verdict === 'CONFIRMED').length,
  partially: allVerified.filter((v) => v.verdict === 'PARTIALLY_CONFIRMED').length,
  refuted: allVerified.filter((v) => v.verdict === 'REFUTED').length,
  severity_counts: allVerified.filter((v) => v.verdict !== 'REFUTED')
    .reduce((m, v) => { m[v.final_severity] = (m[v.final_severity] || 0) + 1; return m; }, {}),
  report,
};
