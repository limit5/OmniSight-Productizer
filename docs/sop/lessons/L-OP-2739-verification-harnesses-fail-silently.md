---
id: L-OP-2739
ticket: OP-2739
title: A broken verification harness produces output that looks like a result
date: 2026-07-29
tags: [process, testing, verification, shell, debugging]
---

# A broken verification harness produces output that looks like a result

**Situation**: During the 2026-07-25 failed-unit sweep, the checks I wrote to
*verify* fixes broke **eight separate times**. Not one of them announced itself.
Every one produced confident, well-formatted output that read as a finding:

1. `rc=$?` after a pipeline captured `tail`'s exit status, not the script's —
   every failure mode of a new probe reported `rc=0`, i.e. "all correct".
2. `grep -Ff <empty file>` matches **everything**. A failed query produced an
   empty pattern file, and the overlap check reported "44 of 44 already
   reviewed" — a number that was simply the line count of the other file.
3. `sha256(body::bytea)` is the wrong cast; with `2>/dev/null` the error
   vanished and the query returned zero rows, which read as "the corpus is
   clean".
4. An injected-failure test died at a preflight before creating anything, so
   "0 orphans before, 0 orphans after" looked like the cleanup handler working.
5. `setsid cmd &` then `kill -9 $!` kills the *setsid* process, not the holder.
   The lock was never released, and I nearly recorded "flock does not self-heal".
6. A registry `tags/list` returned `0` because anonymous listing is denied, not
   because the registry was empty.
7. A YAML `tables:` key was a **list of table objects**, not a map, so
   already-declared columns counted as undeclared: 51 findings instead of 36 —
   and I invented "the schema grew" to explain away disagreeing with the ticket.
8. `awk` summing sizes with mixed units (`kB`/`MB`/`GB`) produced "4147.4 GB" of
   cache on a 1 TB disk.

**Root cause**: a verification harness has no one verifying it. Production code
gets reviewed, tested and mutation-tested; the ten-line shell snippet that
*checks* production code gets none of that, runs once, and its output is trusted
precisely because it was written to be trusted. Worse, most of these failure
modes are silent by construction — `2>/dev/null`, `|| true`, and pipeline exit
codes all exist to suppress noise, and they suppress the harness's own failures
just as effectively.

**Fix**: no single edit — the eight were caught individually. What actually
caught every one of them was the same reflex: **the number was implausible.**
4147 GB on a 1 TB disk. 44 of 44 already reviewed. Every failure mode of a new
probe passing on the first try. Zero credential-shaped rows in a corpus the
ticket said had eleven.

**Verification**: each instance was re-run after correcting the harness and gave
a different, defensible answer — 36 not 51, `rc=1` not `rc=0`, "0 objects" only
after a probe validated against a known-good tag. The OP-2732 cleanup handler
was then additionally proven by reproducing the defect against the *unfixed*
script, which is the only reason its "0 orphans" result means anything.

**Generalisation**: treat a verification result as a claim that needs its own
evidence, not as evidence.

- **Prove the harness can fail.** Before trusting "no orphans found", make it
  find one. Before trusting a fix's test, run it against the *unfixed* code and
  watch it go red. A check that has only ever passed has not been tested; it has
  been executed.
- **Distrust the clean result specifically.** A harness that reports a problem
  usually works — it found something. A harness that reports *nothing* is
  indistinguishable from a harness that does nothing.
- **Sanity-check magnitudes before interpreting them.** "Is this number
  physically possible?" caught more of these than any technique.
- **Never `2>/dev/null` a step whose failure changes the conclusion**, and never
  read `$?` after a pipeline unless you mean the last stage's status.
- **When your result contradicts a written ticket, suspect yourself first.**
  Twice the ticket was right and my explanation for disagreeing with it was
  invented after the fact.
- **A green from an untested harness is not weaker evidence than a red — it is
  no evidence.** That is the same defect this whole sweep existed to remove,
  applied one level up: an alert path that never delivered, a timer that never
  succeeded, a backup drill that passed against a four-day-old artefact.

**Related**: [[L-OP-2728]] — an alerting artefact is proven only by a
deliberately induced failure; this is the same rule applied to the checks
themselves. [[L-OP-2747]] — reviewing the seams, not just the pieces.
