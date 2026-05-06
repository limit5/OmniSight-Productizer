# Gerrit/JIRA Bridge SOP

`backend/agents/gerrit_jira_bridge.py` consumes Gerrit `stream-events`
and moves OP tickets from `Approved` to `Published` after Gerrit merges a
develop change. It is stateless: restart behavior is a startup catchup
JQL pass plus a fresh stream connection.

## Install

1. Verify bot credentials exist on the host:
   - `~/.config/omnisight/jira-claude.env`
   - `~/.config/omnisight/jira-claude-token`
   - `~/.config/omnisight/gerrit-claude-bot-ed25519`
2. Install the unit:
   ```bash
   sudo sed "s|USER_HOME|$USER|g; s|USERNAME|$USER|g" \
     deploy/systemd/gerrit-jira-bridge.service \
     | sudo tee /etc/systemd/system/gerrit-jira-bridge.service >/dev/null
   sudo systemctl daemon-reload
   sudo systemctl enable --now gerrit-jira-bridge.service
   ```
3. Confirm startup catchup ran:
   ```bash
   journalctl -u gerrit-jira-bridge.service -n 50 --no-pager
   ```

## Config

The launcher defaults to `--agent-class subscription-claude`, which maps
to `claude-bot` for both JIRA and Gerrit. This bot has Deploy permission
and the bridge only uses JIRA transition id=7 (`Approved` -> `Published`).
Set `OMNISIGHT_PYTHON=/path/to/python` in the unit environment if the host
must use a project venv instead of `python3`.

The daemon connects with:

```bash
ssh -i ~/.config/omnisight/gerrit-claude-bot-ed25519 \
  -p 29418 claude-bot@sora.services gerrit stream-events
```

## Logs

Logs are JSON one-per-line. Common inspection commands:

```bash
journalctl -u gerrit-jira-bridge.service -f
journalctl -u gerrit-jira-bridge.service --since "1 hour ago" | jq .
```

Heartbeat lines use `event=heartbeat` and include:
`events_received`, `transitions_made`, `parse_errors`, `jira_errors`,
`gerrit_reconnects`, and `last_event_at_ts`.

## Common Failures

- `event=gerrit_auth_failed`, `level=ALERT`: SSH key revoked, rotated, or
  not readable. The daemon exits 2 and systemd restarts it every 5s.
- `event=jira_auth_failed`, `level=ALERT`: JIRA token expired or missing
  Deploy permission. The daemon exits 2.
- `event=gerrit_stream_disconnected`: stream dropped. The daemon retries
  with exponential backoff capped at 60s; after 10 consecutive failures it
  logs `event=gerrit_stream_reconnects_high`.
- `event=malformed_json_line`: Gerrit emitted a line that was not JSON.
  The daemon increments `parse_errors` and continues.
- `event=ticket_unexpected_status_skip`: matching ticket is not Approved.
  This is intentional ADR 0003 protection; the bridge does not jump ahead
  of human approval.
- `event=multiple_tickets_for_change`: one Gerrit change subject mapped to
  multiple OP keys. Do not transition manually until the operator decides
  which ticket owns the change.

## First-Run Validation

After install, run:

```jql
project = "OP" AND status = "Approved" AND assignee in (codex-bot, claude-bot)
```

For any result, check the Gerrit change linked by the
`[runner-pushed-to-gerrit]` comment. If the change is already `MERGED`,
the bridge should publish it during startup catchup. The expected steady
state after first run is zero Approved-but-merged orphan tickets.
