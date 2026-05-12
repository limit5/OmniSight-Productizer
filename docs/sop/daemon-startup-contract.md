# Daemon startup contract — required systemd unit directives (AUDIT-29c)

**Status**: authored 2026-05-13, OP-1027 (AUDIT-29c-3)
**Owner**: runner / orchestration on-call
**Scope**: every `*.service` unit committed under `deploy/systemd/`
**Enforced by**: `scripts/check_systemd_unit_contract.py` pre-commit hook (OP-1028 / AUDIT-29c-4)

## 1. Why this exists

OmniSight runs ~20 user-level systemd units (the runner instances, the
Gerrit/JIRA bridge, the canary, the release timers, …). Three production
incidents trace back to a unit file that *started* but did not behave like
a supervised daemon:

- **OP-717** — the bridge ran with default stdout buffering. `logger.info()`
  output sat in Python's buffer and only flushed at process exit, so
  `journalctl -f` showed nothing during the ~hour we were debugging a live
  enrichment failure. Fix: `Environment=PYTHONUNBUFFERED=1` **plus** a
  `StandardOutput=` that actually points somewhere a human tails.
- **OP-831** — the bridge hit a `PermissionError` on a root-owned cursor
  path. Because the unit had no `Restart=`, systemd marked it `failed` and
  walked away; the bridge was dark for ~16 minutes before an operator
  noticed. A `Restart=on-failure` + `RestartSec=` would have crash-looped
  loudly (and survived once the path was fixed) instead of going silent.
- **AUDIT-23 / OP-976** — several units shipped to `deploy/systemd/` were
  never `systemctl enable`d. A unit with no `[Install] WantedBy=` and no
  install instructions in its header is "shipped but not deployed" by
  construction.

The lesson each time: a `.service` file that merely launches a process is
not a daemon. A daemon is *supervised* (restarts), *observable* (logs land
where someone reads them), and *unambiguous about what it runs* (absolute
`ExecStart=`). This SOP codifies the minimum.

## 2. The contract — three required directives

Every `[Service]` section in `deploy/systemd/*.service` MUST set all three
of the following. The pre-commit hook (§5) rejects a unit that is missing
any of them.

### 2.1 `Restart=` (with `RestartSec=`)

| Unit shape | `Restart=` | `RestartSec=` | Notes |
|---|---|---|---|
| Long-lived daemon (`Type=simple` / `Type=notify`) | `always` | `5` | Survives transient JIRA/Gerrit/network outages; restarts after a clean operator `systemctl restart`. |
| Loop daemon that legitimately exits between ticks (runner instances) | `on-failure` | `90` | A clean exit is "no work, will be relaunched on the next tick"; only a *crash* should count against the restart budget. Align `RestartSec=` with the loop's natural tick cadence so it doesn't tight-loop on an outage. |
| `Type=oneshot` triggered by a `.timer` | *(omit `Restart=`)* | — | A oneshot has nothing to restart; the `.timer` re-runs it. **But** it MUST then carry an `OnFailure=` handler (see `omnisight-canary.service`) — otherwise a failing tick is silent, which is the same failure class `Restart=` exists to prevent. The contract is satisfied by `OnFailure=` *in lieu of* `Restart=` for oneshot units only. |

`RestartSec=` is part of the contract: an unqualified `Restart=always`
defaults to 100 ms and will hammer a downstream service that is mid-outage.

Also pair with the clean-shutdown directives the rest of the fleet uses so
SIGTERM is honoured and a stuck process is eventually killed:

```ini
KillSignal=SIGTERM
TimeoutStopSec=30        # 60 for runner instances that may be mid-CLI
```

### 2.2 `StandardOutput=` (and `StandardError=`)

Both MUST be set, and to the *same* sink, so a single `tail -F` / one
`journalctl` invocation sees the whole story:

```ini
StandardOutput=append:%h/work/sora/logs/<area>/<unit>.log
StandardError=append:%h/work/sora/logs/<area>/<unit>.log
```

- Use `append:<path>` when the daemon emits structured JSON status lines a
  tool consumes (canary, staging-gate) or when you want a flat file
  independent of journal rotation. Create the parent dir in the install
  instructions or the unit fails to start.
- `journal` is acceptable for chatty daemons whose logs are only ever read
  via `journalctl --user -u <unit>`.
- Never leave it default (`inherit`) — for a user-level unit started by
  `systemctl --user` that means "wherever the manager's stdout goes", which
  in practice is nowhere useful.

Pair with `Environment=PYTHONUNBUFFERED=1` for any Python `ExecStart=` —
without it the directive above captures a stream that only flushes at exit
(the OP-717 failure).

### 2.3 `ExecStart=` — absolute interpreter + absolute target

```ini
ExecStart=/usr/bin/python3 %h/work/sora/OmniSight-Productizer/auto-runner-jira.py --flags
```

- **Absolute interpreter path.** systemd does not run `ExecStart=` through a
  shell and does not inherit the operator's `PATH`; a bare `python3` or
  `scripts/foo.py` either fails to start or picks up the wrong interpreter.
- **Absolute path to the script**, using `%h` for the unit owner's home so
  the file is portable across the dev/prod host split rather than
  hard-coding `/home/user/...` (the OP-831-adjacent portability trap). For
  units that live in the bridge checkout, the canonical root is
  `/home/user/sora-bridge` (see `gerrit-jira-bridge.service`); for runner
  instances it is `%h/work/sora/OmniSight-Productizer`.
- Set `WorkingDirectory=` explicitly to that same root — don't rely on the
  default (`/`).

## 3. When to use linger (`loginctl enable-linger`)

There is no `linger=yes` *unit* directive — linger is a per-user logind
setting, toggled with `loginctl enable-linger $USER`. It controls whether
the user's systemd manager (and therefore all `systemctl --user` units)
keeps running when the user is not logged in, and whether it starts at boot
rather than at first login.

Enable linger when **all** of these hold (the common case for this fleet):

1. The unit is user-level (`systemctl --user` — it needs the bot account's
   `~/.config/omnisight/*` creds and SSH keys, so it can't be a root unit).
2. It must survive the operator logging out of their SSH session.
3. It must come up on host boot without someone logging in first.

That covers the bridge, the runner instances, the canary, and the release
timers — i.e. anything in `deploy/systemd/` that is meant to be "always on".
Every such unit's header MUST include the line in its install block:

```text
loginctl enable-linger $USER     # survive logout / start at boot
```

Do **not** enable linger for a unit you only ever start by hand for a
debugging session, or for a oneshot you trigger ad hoc — leave it
session-scoped so it dies with your shell.

## 4. Canonical templates

### 4.1 Long-lived daemon

```ini
[Unit]
Description=OmniSight <thing> (OP-NNN)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/user/sora-bridge
Environment=PYTHONPATH=/home/user/sora-bridge
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /home/user/sora-bridge/scripts/run_<thing>.py
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=append:/home/user/work/sora/logs/<area>/<unit>.log
StandardError=append:/home/user/work/sora/logs/<area>/<unit>.log

[Install]
WantedBy=default.target
```

Install header MUST document `loginctl enable-linger $USER`.

### 4.2 Loop / template-instance daemon (runner)

```ini
[Service]
Type=simple
WorkingDirectory=%h/work/sora/OmniSight-Productizer
Environment=PYTHONUNBUFFERED=1
Environment=OMNISIGHT_RUNNER_INSTANCE_ID=%i
ExecStart=/usr/bin/python3 %h/work/sora/OmniSight-Productizer/auto-runner-jira.py
Restart=on-failure
RestartSec=90
KillSignal=SIGTERM
TimeoutStopSec=60
StandardOutput=append:%h/work/sora/logs/runner/<bot>-%i.log
StandardError=append:%h/work/sora/logs/runner/<bot>-%i.log
```

(See `deploy/systemd/runner-codex@.service` for the fully commented version.)

### 4.3 Oneshot triggered by a `.timer`

```ini
[Unit]
Description=OmniSight <periodic thing> (OP-NNN)
After=network-online.target
Wants=network-online.target
OnFailure=<unit>-alert.service          # required substitute for Restart= on oneshot

[Service]
Type=oneshot
WorkingDirectory=%h/work/sora/OmniSight-Productizer
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 %h/work/sora/OmniSight-Productizer/scripts/<thing>.py
StandardOutput=append:%h/work/sora/logs/<area>/<unit>.log
StandardError=append:%h/work/sora/logs/<area>/<unit>.log
TimeoutStartSec=300

[Install]
WantedBy=default.target
```

(See `deploy/systemd/omnisight-canary.service`.)

## 5. Pre-commit checklist (what the OP-1028 hook enforces)

For each `deploy/systemd/*.service` in the commit:

- [ ] `[Service]` has `ExecStart=` whose first token is an absolute path.
- [ ] `[Service]` has `StandardOutput=` **and** `StandardError=` (neither
      defaulted).
- [ ] `[Service]` has `Restart=` **and** `RestartSec=` — **unless**
      `Type=oneshot`, in which case `[Unit]` has `OnFailure=` instead.
- [ ] (advisory, not blocking) `[Install] WantedBy=` is present, and the
      header comment names `loginctl enable-linger $USER` for always-on
      units.

A unit that legitimately needs to deviate must carry an inline comment
`; daemon-startup-contract: <directive> waived because <reason>` on the line
above the omission, which the hook treats as an acknowledged exception.

## 6. Cross-references

- `deploy/systemd/gerrit-jira-bridge.service` — reference long-lived daemon.
- `deploy/systemd/runner-codex@.service`, `runner-claude@.service` —
  reference loop / template-instance daemons.
- `deploy/systemd/omnisight-canary.service` — reference oneshot + timer +
  `OnFailure=`.
- `docs/audit/2026-05-12-shipped-not-deployed-sprint-dEF.md` — the
  shipped-but-not-deployed pattern this contract's `[Install]`/linger clause
  closes (AUDIT-23 / OP-976).
- `docs/sop/architecture-anti-patterns.md` — pattern #13
  (shipped-but-not-deployed).
- `docs/sop/jira-ticket-conventions.md` §11 — discovered-dependency rule for
  tickets that turn out to need an out-of-area systemd change.
