---
id: L-OP-760
ticket: OP-760
title: Classify before reacting; generic error handlers leak partial state
date: 2026-05-08
tags: [runner, jira, gerrit, recovery]
legacy_lesson:
---

# Classify before reacting; generic error handlers leak partial state

**Situation**: A Gerrit push failure used one generic runner path: comment, exit non-zero, and leave the JIRA ticket in progress. That was correct for transient failures but wrong for duplicate work, invalid identities, missing trees, Change-Id problems, conflicts, and unknown policy failures.

**Fix**: OP-760 classifies push stderr before mutating JIRA. Each category maps to an explicit recovery action: force-publish duplicate merged work, revert deterministic code-quality failures, retry transient network failures, or pause unknown failures with a dead-letter label.

**Verification**: `backend/tests/test_push_failure_classifier.py` covers the classifier patterns, force-publish fallback split, revert categories, retry path, and unknown escalation label.

**Generalisation**: Runner error handlers should convert external stderr into a typed event before changing workflow state. Generic "log and fail" paths are acceptable only when the workflow state remains valid after the failure.
