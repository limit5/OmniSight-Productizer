---
id: L-OP-1761
ticket: OP-1761
title: A forensic-evidence emitter bolted onto an existing state machine must mirror that machine's verdict, not re-derive it — and record where the contract's source labels diverged from what the machine actually reads
date: 2026-05-27
tags: [devops, audit, deployment, family5, evidence, additive, contract-drift]
---

# A forensic-evidence emitter bolted onto an existing state machine must mirror that machine's verdict, not re-derive it — and record where the contract's source labels diverged from what the machine actually reads

**Situation**: OP-1761 added the family ⑤ §6 evidence-file writer
(`docs/audit/AUDIT-deployment/YYYY-MM-DD.json` + `latest.json` + `index.json`)
to `scripts/deployment-audit.sh`, on top of the OP-1753 image-SHA state machine
(`result_state` ∈ OK / WARN_STALE_IMAGE / PAGE_STALE_IMAGE / PAGE_INTEGRITY /
INCOMPLETE). The ticket's hard constraint was "additive only — do NOT touch the
OP-1753 classification". Two traps sat in the way:

1. **Re-deriving the verdict in the emitter would fork the truth.** The tempting
   shape is to re-read T1/T2/T4 in the writer and recompute the state. That
   double-queries GHCR/curl (a *different* snapshot than the one classified, so
   "replay the exact decision" — the whole point of a forensic file, §6.3 — is
   already broken) and creates a second copy of the threshold logic that can
   silently drift from the one that actually gated the run's exit code.
2. **The contract's source labels had drifted from the implementation.** The
   family ⑤ contract §2.1 labels truth-source T4 "image alembic heads", but the
   shipped OP-1753 machine reuses T4 as the baked `MANIFEST.json` *image_sha*
   integrity source (`T1.image_sha != T4.manifest.image_sha` ⇒ PAGE_INTEGRITY).
   And the §6.2 schema mandates a T3 (DB alembic head) that the image-SHA audit
   never reads — T3 is the DB axis, owned by Family ⑥, and explicitly out of
   OP-1761's area boundary.

**Fix**: The emitter is a pure side-channel.
`scripts/deployment-audit.sh:check_image_sha` captures the *raw* truth-sources
it already read (T1 JSON, T2 digest/pushed_at/ref, T4 manifest JSON) into
`EV_*` globals as a side-effect — never altering an `errors+=`/`record` line —
and `emit_evidence_file` parses the headline `result_state` and the emitted
alerts straight out of the recorded `ROWS` (`result_state=…` / `alert=…` tokens)
rather than recomputing them. So the file's verdict is, by construction, the
machine's verdict. For the divergences: T4 is recorded under
`truth_sources.T4_image_manifest` (with an inline note that it is the MANIFEST
integrity source, not alembic heads); T3 is populated only when an
`alembic-head` row ran in the same audit (read-only parse of that row's
`prod current=…`, no DB code added), otherwise a `{head: null, note: …}` stub
that says it is out of area — never a bare "error" that would falsely force
INCOMPLETE. INCOMPLETE keeps its §6.4 contract: evidence is still written, but
the stale-image alert is stripped and `OmniSightAuditIncomplete` is surfaced
instead.

**Verification**: `tests/test_deployment_audit_evidence.py` —
`test_ok_run_emits_schema_v62_evidence_and_latest_symlink` (schema + symlink +
empty incident index), `test_drift_run_records_state_and_alert_and_indexes_it`
(WARN/PAGE stale → matching alert + index row),
`test_integrity_finding_is_the_headline_when_combined_with_stale` (PAGE_INTEGRITY
outranks PAGE_STALE for the headline), and
`test_incomplete_run_writes_evidence_without_stale_alert` (§6.4: file written, no
stale alert, `OmniSightAuditIncomplete` present, T1 error sub-field). The
pre-existing `tests/test_deployment_audit_image_sha.py` still passes unchanged,
proving the classification was untouched.

**Generalisation**: **When you bolt a reporter (evidence file, metric, audit
log) onto an existing decision-maker, the reporter must read the decision-maker's
output, never re-run its logic** — recomputation forks the truth and the fork
drifts. Capture the decision-maker's *inputs* as a side-channel for forensic
replay, but take the *verdict* from what it already recorded. And when the
governing contract names a data source one thing but the shipped code reads
another (T4 = "alembic heads" in the spec, MANIFEST integrity in the code; a
mandated T3 the machine never reads), do not silently rename or fabricate —
record the source under the name that matches *what was actually read*, with an
inline note pointing at the divergence, so the next reader reconciles spec-vs-code
by reading rather than re-deriving. (Adjacent: L-LEGACY-003 "live-repo-state
alembic-head injection" — same family of "the audit must reflect the live read,
not a convenient local proxy".)
