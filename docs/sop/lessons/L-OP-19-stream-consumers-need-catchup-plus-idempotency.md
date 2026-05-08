---
id: L-OP-19
ticket: OP-19
title: Stream consumers need catchup plus idempotency
date: 2026-05-07
tags: [ci, events, gerrit, jira, runner]
legacy_lesson: 18
---

# Stream consumers need catchup plus idempotency

**Situation**: OP-19 stayed in `Approved` for hours after operator +2 because the interactive session that previously handled ad-hoc JIRA advancement had ended. Gerrit merge state existed, but there was no long-running consumer to translate `change-merged` into JIRA `Published`.

**Fix**: OP-689 added a stateless Gerrit `stream-events` consumer with a startup catchup pass and per-ticket status gate. The bridge only uses transition id=7 (`Deploy`) and first checks JIRA state, so duplicate stream/catchup races become harmless and the daemon cannot perform the ADR 0003 `Approve` hop.

**Verification**: `backend/tests/test_gerrit_jira_bridge.py` covers startup catchup, stream merge handling, malformed events, duplicate/multi-ticket protection, reconnect, auth failure, JIRA retry classes, and transition id drift guard.

**Generalisation**: Event-driven automation that represents terminal workflow state should never rely on the stream alone. Pair stream consumption with catchup from source-of-truth state, make the terminal transition idempotent, and make authority boundaries explicit in tests.
