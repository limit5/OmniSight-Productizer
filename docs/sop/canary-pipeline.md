# Canary pipeline (OP-725)

**Status**: shipped 2026-05-08, OP-725 (META OP-721 / T4)
**Owner**: devops on-call
**Cadence**: hourly (systemd timer)

## Purpose

Catch the OP-708 failure mode where a synthetic-curl health check
returns 200 even though Gerrit's `webhooks` plugin v3.13.5 cannot
deliver real events to the merger. The canary runs the *real*
end-to-end path on a schedule:

```
git push -> Gerrit -> stream-events SSH -> gerrit-jira-bridge daemon
       -> structured log: proactive_merger_thread_spawned
```

If any link is down, the next hourly tick fires `DEGRADED` via the
T1 alerter (OP-722).

## What it does, per run

1. Snapshots the byte offset of the bridge structured-log file
   (`~/work/sora/logs/bridge/systemd.log`).
2. Refreshes a local clone under `~/.cache/omnisight/canary-workdir`
   (clean fast-forward to `origin/develop`, no inherited cruft).
3. Stages a single-line write to `canary/heartbeat.txt` with the
   current ISO-second timestamp.
4. Commits with subject `[CANARY] hourly synthetic event <iso>` —
   no `[OP-NNN]` key, so the bridge's JIRA dispatch path skips it.
5. Pushes to `refs/for/develop%hashtag=canary,wip,private` so the
   change is hidden from human reviewers.
6. Polls the bridge log for up to 60 s waiting for
   `event=proactive_merger_thread_spawned` with our change number.
7. Abandons the change via `ssh ... gerrit review --abandon` with
   a `[CANARY]` audit message.
8. Emits a single structured-JSON status line on stdout — the
   `omnisight-canary.service` unit appends this to
   `~/work/sora/logs/canary/canary.log`.

Exit codes:

| rc | meaning | who reads it |
|----|---------|--------------|
| 0  | pipeline OK | timer just resets, no alert fires |
| 2  | DEGRADED — push succeeded but daemon log silent OR push failed | systemd `OnFailure=` chains to `omnisight-canary-alert.service` |
| 3  | canary configuration error (missing SSH key) | same OnFailure path; the structured log surfaces `stage=config` |

## Pollution controls (AC #3)

| Risk | Mitigation |
|---|---|
| Reviewers see a change in the queue | `wip`+`private` push options |
| Real JIRA tickets get hourly comments | Subject contains no OP key — `extract_ticket_keys_from_subject` returns `[]` |
| Stranded changes accumulate | `--abandon` runs in a `finally` block; ops can grep `hashtag:canary` to clean up if SSH ever fails |
| Hot path on `develop` | Push targets `refs/for/develop` (review only); never lands on the branch |

## Install / enable

User-level systemd, same identity as `gerrit-jira-bridge.service`:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/omnisight-canary.service       ~/.config/systemd/user/
cp deploy/systemd/omnisight-canary.timer         ~/.config/systemd/user/
cp deploy/systemd/omnisight-canary-alert.service ~/.config/systemd/user/
mkdir -p ~/work/sora/logs/canary
systemctl --user daemon-reload
systemctl --user enable --now omnisight-canary.timer
```

Confirm next fire time:

```bash
systemctl --user list-timers omnisight-canary.timer
```

## Manual run (debugging)

```bash
python3 scripts/canary_pipeline.py \
  --bridge-log ~/work/sora/logs/bridge/systemd.log \
  --timeout 60
```

Add `--no-cleanup` to leave the change in Gerrit for inspection.
**Don't forget to abandon by hand** — `gerrit review --abandon
<num>,1` over SSH.

## Failure-mode runbook

| Symptom | Likely cause | First check |
|---|---|---|
| `stage=push, err=...permission denied` | bot SSH key changed / Gerrit ACL revoked | `ssh -i ~/.config/omnisight/gerrit-claude-bot-ed25519 -p 29418 claude-bot@sora.services gerrit version` |
| `stage=push, err=...no change number in stderr` | push was a no-op fast-forward (subject seed broken) OR Gerrit error swallowed | check the push stderr in `canary.log`; verify subject contains a fresh ISO timestamp |
| `stage=poll, change_number=NNN, expected_event=proactive_merger_thread_spawned` | bridge daemon stopped consuming stream-events (the OP-708 failure mode the canary was built to catch) | `systemctl --user status gerrit-jira-bridge`; tail bridge log for `gerrit_stream_reconnects_high` |
| Repeated `stage=poll` over multiple hours, but daemon `status` is `active` | stream-events SSH is silently disconnected (TCP half-open) | restart the bridge: `systemctl --user restart gerrit-jira-bridge` |
| `stage=config, err=ssh key not readable` | rc=3, key path moved or permissions wrong | re-check `~/.config/omnisight/gerrit-claude-bot-ed25519` mode 600 |

## Test verification

| AC item | Evidence |
|---|---|
| Hourly cadence + auto-abandon | `tests/test_canary_pipeline.py::test_canary_timer_is_hourly_and_persistent`, `::test_abandon_argv_includes_message_and_strict_host_key_policy` |
| Failure injection -> DEGRADED within 1 h | `::test_parse_log_returns_none_when_event_missing`, `::test_poll_for_event_returns_none_after_timeout`, `::test_canary_service_chains_alert_on_failure` |
| No JIRA / Gerrit pollution | `::test_subject_excludes_op_keys_to_avoid_jira_pollution` (and the `wip,private` push spec in `push_canary_for_review`) |

## Hand-off to T1 (OP-722)

The T1 alerter consumes structured-log records in the journal /
`canary.log` matching:

```
event in ("canary_pipeline_check_failed", "canary_pipeline_alert")
level == "DEGRADED"
```

`canary_pipeline_check_failed` carries the failure detail
(`stage`, `change_number`, `err`). `canary_pipeline_alert` is the
fallback emitted by `omnisight-canary-alert.service` even if the
canary process itself crashed before printing — guarantees at least
one DEGRADED record per failed run.

## See also

- `docs/sop/gerrit-jira-bridge.md` — the daemon under test
- `docs/sop/lessons/L-OP-708-webhook-endpoints-with-require-operator-signature-are-dual-g.md`
  — the OP-708 webhook auth-surface incident this canary defends against
- `scripts/prod_smoke_test.py` — sister synthetic-DAG smoke check
  for the backend HTTP path (does NOT cover the Gerrit pipeline)
