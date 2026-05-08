# Error Pager — Configuration SOP (OP-724)

The error pager (`scripts/journal_error_forwarder.py`) is the
"someone is listening" half of OmniSight's silent-failure story. It
tails the systemd journal for ERROR / CRIT / ALERT records emitted by
the units listed in `configs/error_pager.yaml`, classifies them, and
fans the survivors out through T1 OP-722's operator notifier. Every
ERROR-level log line in the program now reaches an operator without
the operator having to read the journal proactively.

> META ticket: OP-721. Builds on T1 OP-722 + T2 OP-723.

## 1. Pipeline at a glance

```
journalctl --priority=err --follow              (1) kernel-side gate
        │
        ▼
AllowList.match(code)  → drop on suppress       (2) allow-list
        │
        ▼
SeverityClassifier(priority, code) → DEGRADED   (3) severity mapping
        │
        ▼
Notifier.notify(severity, code=…, **fields)     (4) T1 fanout
```

Stage 1 is the cheapest gate (kernel-side priority filter); stage 4 is
the most expensive (HTTP fanout to JIRA / SMTP / Slack / LINE). Allow-
listing sits at stage 2 specifically so a noisy code never even
reaches T1's dedup buffer.

## 2. AC mapping

| AC | Mechanism | Pinned by |
|----|-----------|-----------|
| #1 — backend exception → DEGRADED within 1 min | `dedup_window_seconds=55` + `dispatch_poll_seconds=2` | `tests/test_journal_error_forwarder.py::test_ac1_dedup_window_meets_one_minute_latency_budget` |
| #2 — 5 identical errors → 1 alert with count=5 | T1 OP-722 burst-coalescer, fed unmodified by the forwarder | `test_ac2_five_identical_errors_collapse_to_count_five` |
| #3 — allow-listed code → no alert | `allow_list:` short-circuit in stage (2); cursor still advances | `test_ac3_*` |
| #4 — forwarder crash → systemd restart, ≤ 10s log loss | `Restart=always` + `RestartSec=5` + `journalctl --cursor-file` resume | `test_ac4_*` (unit-file contract + cursor primitives) |

## 3. Configuration (`configs/error_pager.yaml`)

### 3.1 `units:`

The systemd units to follow. Each entry has:

* `name` — the systemd unit name (e.g. `omnisight-backend.service`).
  The trailing `.service` is optional; `journalctl` accepts both.
* `scope` — `system` (default) or `user`. User-scope units are
  reached via `journalctl --user-unit`; the forwarder dispatches the
  flag automatically.

Adding a new daemon = append another `units:` entry. The forwarder
re-spawns its underlying `journalctl --follow` with the updated unit
list on the next process restart (config reload via SIGHUP only
hot-reloads stages 2–3, not stage 1 — see §5).

### 3.2 `code_extractors:`

Ordered list of regexes applied to each record's `MESSAGE` field. The
**first** pattern with a `code` named-capture group whose value is
non-empty wins. If none match, the fallback is `<unit>:p<priority>` —
coarse but stable.

A stable code is load-bearing: T1's burst coalescer dedups by
`(code, severity)`. If your error messages embed varying request-ids
or timestamps, write a regex that strips them and captures only the
invariant code prefix. Otherwise every distinct request-id fires a
separate alert, defeating AC #2.

The default extractors handle three common shapes:

1. Bridge-style structured logs: `"event": "<code>"`.
2. Python tracebacks: `<ExceptionType>: <message>` → code = `<ExceptionType>`.
3. Plain prefix: `FOO_BAR: …` → code = `FOO_BAR`.

### 3.3 `allow_list:`

The allow-list is the operator's escape hatch for known noise. Each
entry has:

* `code:` (exact match) **or** `code_pattern:` (regex)
* `suppress: true` (drop the record before stages 3–4)
* `note:` free-text for the next operator to read

Conventional shapes:

| Use case | Shape |
|----------|-------|
| Known boot-time race that retries successfully | exact-match code, `suppress: true`, note explaining the retry path |
| Family of transient codes (e.g. `transient_5xx_*`) | `code_pattern:` regex, `suppress: true` |
| Temporarily silence during planned maintenance | exact-match, `suppress: true`, note with end-date in the body |

> **DON'T** allow-list a code without a `note:` explaining the
> downgrade rationale. Future-you will look at it and wonder if it's
> still safe; without context the safe default is to remove the rule
> and re-page.
>
> **DON'T** allow-list a CRITICAL-promoted code. The severity-map
> override exists precisely so high-stakes codes can't be silenced by
> accident.

### 3.4 `severity_map:`

Per-code overrides for the default PRIORITY → severity mapping
(`3=DEGRADED, 0/1/2=CRITICAL`). The two common reasons to override:

* **Promote** an ERROR-level code to CRITICAL because losing the
  underlying capability is a paging-class incident even though the
  daemon flushed it as PRIORITY=3 (e.g. `refused_llm_unavailable`,
  `audit_write_failed`).
* **Demote** a CRITICAL-spelled code to DEGRADED or WARN because
  external pressure produced the high-priority record but our own
  remediation handled it (rare; allow-list is usually the right tool).

Severity values are the literal strings `WARN`, `DEGRADED`, `CRITICAL`,
`P0` — they are the same set as T1 OP-722's `Severity` enum. A typo
silently falls through to default; the test
`test_config_severity_map_uses_valid_values` is the guardrail.

### 3.5 Tuning knobs

| Key | Default | Why |
|-----|---------|-----|
| `dedup_window_seconds` | 55 | AC #1 latency budget — see §4. |
| `dispatch_poll_seconds` | 2 | How often the forwarder calls T1's `flush_expired`. |
| `cursor_file` | `/var/lib/omnisight/journal-error-forwarder.cursor` | AC #4 resume-after-crash. |
| `alerts_path` | `/home/user/work/sora/logs/error-pager/alerts.jsonl` | Defence-in-depth audit trail (file sink stays alive when T1 is down). |
| `notifier_module` | `backend.agents.operator_notifier` | T1 OP-722 import path. |

## 4. The 55-second dedup window — design note

T1 OP-722's default `OMNISIGHT_NOTIFIER_DEDUP_WINDOW_SECONDS=300` (5
min) is correct for general-purpose callers but violates AC #1's 1-min
latency budget (worst-case = 300 + poll). The forwarder builds its
**own** Notifier instance with the tighter 55s window so:

* Worst-case single-error latency = 55 + 2 = 57s ≤ 1 min ✓
* A typical retry-loop burst (errors at sub-second intervals) still
  collapses into one alert with `count=N` ✓
* Other T1 callers (T2 watchdog, future agents) keep the relaxed
  default — the global env var is untouched.

If you tighten further (say to 30s) AC #2 starts to fail when the 5
errors span more than 30s of wall-clock; if you loosen past 60s AC #1
starts to fail. The operating margin is narrow on purpose.

## 5. Operational runbook

### 5.1 Install (production host)

```bash
sudo cp deploy/systemd/omnisight-journal-error-forwarder.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now omnisight-journal-error-forwarder.service
```

Verify in-band:

```bash
journalctl -u omnisight-journal-error-forwarder -f
```

You should see `INFO ... reloaded config:` on startup and (if the
backend is healthy) silence afterwards.

### 5.2 Hot-reload allow-list / severity-map (no outage)

After editing `configs/error_pager.yaml`:

```bash
sudo systemctl reload omnisight-journal-error-forwarder.service
# or, equivalently:
sudo kill -HUP $(systemctl show -p MainPID --value \
  omnisight-journal-error-forwarder.service)
```

This reloads stages 2–3 (allow-list, severity map, code extractors)
without restarting the journalctl process, so you don't take the
RestartSec outage hit during an active alert storm. Stage 1 (the
`units:` list) and the dedup tuning require a full restart:

```bash
sudo systemctl restart omnisight-journal-error-forwarder.service
```

### 5.3 Verify after a deploy / config change

Trigger a known-good ERROR record on the host (or wait for one from
real traffic) and confirm:

```bash
# 1. The record reached the forwarder
tail -n 5 /home/user/work/sora/logs/error-pager/alerts.jsonl

# 2. T1 received it (per T1 OP-722 docs/sop/notifier-config.md §6)
journalctl -u omnisight-journal-error-forwarder | grep notifier
```

If the alerts file shows `"notified": false`, the file sink is
working but T1 isn't — typically a missing env var. Fix per
`docs/sop/notifier-config.md` §2 and reload.

### 5.4 Common failure modes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Forwarder restarts in a tight loop | YAML parse error in `configs/error_pager.yaml` | `python3 scripts/journal_error_forwarder.py --config configs/error_pager.yaml` to see the traceback; fix YAML; restart |
| Alerts file growing but no T1 paging | `notifier_module` missing or env vars unset | Set `OMNISIGHT_NOTIFIER_*` per `docs/sop/notifier-config.md` §2; reload |
| Identical alerts arriving every 55–60s | Allow-list miss — code is varying per record | Add a `code_extractor` regex that captures the stable prefix, or extend the allow-list with `code_pattern:` |
| AC #4 audit (operator triggers crash) shows >10s gap | StateDirectory ownership wrong → cursor write fails → resume restarts at journal head | `ls -la /var/lib/omnisight/`; ensure unit's user can write |

## 6. Cross-references

* `backend/agents/operator_notifier.py` — T1 OP-722 (consumer).
* `scripts/daemon_watchdog.py` — T2 OP-723 (parallel sibling: this is
  the heartbeat-monitor counterpart to T3's log-monitor role).
* `docs/sop/notifier-config.md` — T1 OP-722 operator SOP (channels,
  env vars, canary self-test).
* `tests/test_journal_error_forwarder.py` — AC matrix.
* `docs/sop/lessons-learned.md` Lesson 24 — the OP-689 silent-failure
  pattern this whole META was built to close.
