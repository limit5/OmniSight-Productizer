---
id: L-OP-748
ticket: OP-748
title: Stream consumers need durable cursors, not just restarts
date: 2026-05-08
tags: [gerrit, jira, runner, reliability]
---

# Stream consumers need durable cursors, not just restarts

**Situation**: A Gerrit `stream-events` disconnect dropped merge events
while the bridge daemon was down. Systemd restart recovered the process,
but the stream resumed from the current tail, leaving already-merged
tickets stranded in non-terminal JIRA states.

**Fix**: OP-748 added a daemon-private event cursor written after each
successfully processed stream event. On startup and reconnect, the
bridge loads that cursor and runs `gerrit query status:merged
after:<timestamp>` to synthesize `_backfilled` `change-merged` events
before resuming live stream consumption.

**Verification**: `backend/tests/test_gerrit_jira_bridge.py::test_replay_from_cursor_catches_up_three_merges_before_live_stream`
pins the three-missed-merge recovery path, and
`test_cursor_save_is_atomic_private_json` pins atomic private cursor
writes.

**Generalisation**: Long-running stream consumers that drive workflow
state need both a durable cursor and an idempotent replay path. Process
supervision only restores liveness; it does not recover events lost
between disconnect and reconnect.
