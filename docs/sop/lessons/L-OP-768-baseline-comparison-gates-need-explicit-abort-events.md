---
id: L-OP-768
ticket: OP-768
title: Baseline comparison gates need explicit abort events
date: 2026-05-08
tags: [deploy, observability, gating]
---

# Baseline comparison gates need explicit abort events

**Situation**: A staging deploy can look healthy on `/readyz` while
still regressing user-visible quality. A gate that only prints failed
metric deltas is easy to miss and does not create a durable block for
the D9 production deploy decision.

**Fix**: OP-768 made the staging gate run a 60-transaction smoke suite,
wait for the 15-minute metric window, compare current values against the
rolling 7-day median, and emit a `staging_regression` observation before
exiting non-zero.

**Verification**: `backend/tests/test_staging_validation.py` covers the
smoke count, Prometheus baseline/current pulls, threshold comparator,
event emission, and a synthetic 50% error-rate injection.

**Generalisation**: Baseline gates should fail in two channels: a process
exit code that aborts automation now, and a durable event that explains
why later promotion remains blocked until an operator explicitly
overrides it.
