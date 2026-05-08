# Operator Notifier — Configuration SOP (OP-722)

The operator notifier (`backend/agents/operator_notifier.py`) is the
single fan-out layer for every operator-facing alert in OmniSight: JIRA
events, daemon errors, scanner output. This doc walks an operator
through wiring it up.

> META ticket: OP-721. Foundation ticket: OP-722.

## 1. Severity → channel matrix

| Severity   | JIRA | Email | Slack | LINE | Re-paged?                          |
|------------|------|-------|-------|------|------------------------------------|
| `WARN`     | ✓    |       |       |      | no                                 |
| `DEGRADED` | ✓    | ✓     |       |      | no                                 |
| `CRITICAL` | ✓    | ✓     | ✓     | ✓    | every `critical_repage_seconds`    |
| `P0`       | ✓    | ✓     | ✓     | ✓    | every `p0_repage_seconds` (faster) |

Channels with missing config are silently omitted. So a host with only
JIRA + email configured will still happily `notify(CRITICAL, ...)` —
the message just won't reach Slack/LINE.

## 2. Environment variables

All read from the process environment on first call. The systemd unit
`deploy/systemd/gerrit-jira-bridge.service` shows the canonical layout.

| Variable                                       | Required for       | Notes                                                        |
|------------------------------------------------|--------------------|--------------------------------------------------------------|
| `OMNISIGHT_NOTIFIER_JIRA_TICKET`               | JIRA fanout        | Default ticket when caller does not pass `ticket=`.          |
| `OMNISIGHT_NOTIFIER_AGENT_CLASS`               | JIRA fanout        | `subscription-claude` (default) or `subscription-codex`.     |
| `OMNISIGHT_NOTIFIER_SMTP_HOST`                 | email              | `host` or `host:port`. Port defaults to 587 (STARTTLS).      |
| `OMNISIGHT_NOTIFIER_SMTP_USER` / `_PASSWORD`   | email (auth)       | Optional — omit for relays that auth by source IP.           |
| `OMNISIGHT_NOTIFIER_SMTP_FROM`                 | email              | `From:` address.                                             |
| `OMNISIGHT_NOTIFIER_EMAIL_RECIPIENTS`          | email              | Comma-separated.                                             |
| `OMNISIGHT_NOTIFIER_SLACK_WEBHOOK`             | Slack              | Slack incoming-webhook URL. URL carries auth.                |
| `OMNISIGHT_NOTIFIER_LINE_TOKEN`                | LINE               | LINE Notify bearer token (per-recipient).                    |
| `OMNISIGHT_NOTIFIER_ACK_BASE_URL`              | CRITICAL/P0 ack    | Notifier appends `/<notification_id>` for the ack URL.       |
| `OMNISIGHT_NOTIFIER_DEDUP_WINDOW_SECONDS`      | tuning             | Default 300s. See §4 for the latency trade-off.              |
| `OMNISIGHT_NOTIFIER_CRITICAL_REPAGE_SECONDS`   | tuning             | Default 600s.                                                |
| `OMNISIGHT_NOTIFIER_P0_REPAGE_SECONDS`         | tuning             | Default 300s.                                                |

## 3. Channel-by-channel setup

### 3.1 JIRA (always wired)

JIRA reuses the existing dispatch credentials from
`~/.config/omnisight/jira-claude.env` (or `jira-codex.env` per
`agent_class`). No extra config needed beyond
`OMNISIGHT_NOTIFIER_JIRA_TICKET`. The channel posts an ADF comment via
`backend.agents.jira_dispatch.add_comment`.

### 3.2 Email (Mailgun / SES / Gmail SMTP)

Three production-tested recipes:

```ini
# Mailgun
OMNISIGHT_NOTIFIER_SMTP_HOST=smtp.mailgun.org
OMNISIGHT_NOTIFIER_SMTP_USER=postmaster@mg.example.com
OMNISIGHT_NOTIFIER_SMTP_PASSWORD=<mailgun api password>
OMNISIGHT_NOTIFIER_SMTP_FROM=ops@example.com
OMNISIGHT_NOTIFIER_EMAIL_RECIPIENTS=oncall@example.com

# Amazon SES
OMNISIGHT_NOTIFIER_SMTP_HOST=email-smtp.us-east-1.amazonaws.com
OMNISIGHT_NOTIFIER_SMTP_USER=<SES SMTP username>
OMNISIGHT_NOTIFIER_SMTP_PASSWORD=<SES SMTP password>
OMNISIGHT_NOTIFIER_SMTP_FROM=ops@example.com  # must be a verified identity

# Gmail / Workspace SMTP
OMNISIGHT_NOTIFIER_SMTP_HOST=smtp.gmail.com
OMNISIGHT_NOTIFIER_SMTP_USER=ops@example.com
OMNISIGHT_NOTIFIER_SMTP_PASSWORD=<app password>
OMNISIGHT_NOTIFIER_SMTP_FROM=ops@example.com
```

Port 587 + STARTTLS is the default. Set `host:25` only for an inside-LAN
relay that doesn't need TLS — STARTTLS is suppressed for port 25.

### 3.3 Slack

Create a Slack app with an **Incoming Webhook** scoped to the channel
that should receive alerts. Paste the webhook URL into
`OMNISIGHT_NOTIFIER_SLACK_WEBHOOK`. The webhook URL carries the auth
token; treat it like a secret. There is no other config — the notifier
posts JSON `{"text": "..."}`.

### 3.4 LINE Notify

LINE Notify is a per-recipient access pattern: the operator generates a
bearer token under their LINE account at <https://notify-bot.line.me/>
and either keeps it personal or publishes it to a group. Paste the
token into `OMNISIGHT_NOTIFIER_LINE_TOKEN`.

## 4. Dedup window — the latency / count-accuracy trade-off

Per the AC `5 identical notify() calls within 1 minute → 1 outbound
notification with count=5`, the notifier coalesces bursts. Mechanically:

- `notify()` is non-dispatching — it updates the in-memory burst entry
  for `(code, severity)` and returns.
- The background dispatch loop (or an explicit `flush_expired()`) sweeps
  entries whose window has elapsed since `first_seen` and emits ONE
  outbound carrying the accumulated `count`.

Worst-case dispatch delay = `OMNISIGHT_NOTIFIER_DEDUP_WINDOW_SECONDS`.

- **Default 300s** — fine for non-urgent monitors; bursts of repeat
  warnings collapse cleanly.
- **Set 5–10s** — if you need low-latency CRITICAL paging and accept
  that a dense burst still dispatches inside ~10s.
- **Set 0** — currently equivalent to "always dispatch immediately on
  flush"; not recommended (defeats the point of dedup).

The dispatch loop in long-lived processes runs every 5s, so a 5s
window means worst-case ~10s end-to-end (5s wait + 5s polling slack).

## 5. Acknowledgement loop

CRITICAL and P0 carry an ack URL of the form
`<OMNISIGHT_NOTIFIER_ACK_BASE_URL>/<notification_id>`. If
`acknowledge(notification_id)` is not called within
`*_repage_seconds`, the notifier re-dispatches on every wired channel.
`count` increments on each re-page so operators can see how many
re-pages have already fired.

Defaults (one re-page per the cadence below):

- CRITICAL: 600s (10 min) — matches the AC.
- P0: 300s (5 min) — matches the spec ("repeat every 5min until ack").

The HTTP endpoint that turns an inbound ack URL hit into a call to
`acknowledge()` is **not part of OP-722**; it is a follow-up ticket
under META OP-721. Until that lands, an operator can call
`acknowledge(...)` directly from a Python REPL or via a small wrapper
script.

## 6. Canary self-test

Call `canary_self_test()` at process startup. It pings every wired
channel directly (bypassing the dedup layer) and returns
`{channel_name: "ok" | error_str}`. The systemd unit logs this on
boot — a misconfigured webhook fails loudly here instead of going
undetected until the first real incident.

```python
from backend.agents.operator_notifier import canary_self_test

if __name__ == "__main__":
    results = canary_self_test()
    for ch, status in results.items():
        print(f"{ch}: {status}")
```

## 7. Programmatic API summary

```python
from backend.agents.operator_notifier import notify, acknowledge, Severity

# Normal usage — convenience wrapper around the lazy module-level singleton.
notify(Severity.WARN, "ingest:slow", "ingest queue lag > 30s",
       context={"queue_depth": 412}, ticket="OP-700")

# CRITICAL — fans out to every wired channel and starts the re-page clock.
n = notify(Severity.CRITICAL, "scanner:db-down", "ingest scanner cannot reach db")
# `n.notification_id` is what the ack URL embeds.

# Operator acks via the ack endpoint (or directly in a REPL):
acknowledge(n.notification_id)
```

In tests, **always** construct your own `Notifier` with fake channels
and a `FakeClock` — see `backend/tests/test_operator_notifier.py` for
the recipe. The module-level singleton is intentionally lazy precisely
so test runs don't hit the env-driven factory by accident.

## 8. Reference

- Module: `backend/agents/operator_notifier.py`
- Tests: `backend/tests/test_operator_notifier.py`
- Systemd: `deploy/systemd/gerrit-jira-bridge.service`
- Parent META: OP-721. Foundation ticket: OP-722.
