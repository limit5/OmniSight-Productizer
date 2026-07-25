# Ops alert channel — systemd failures and the host disk floor → JIRA

**Ticket:** OP-2728 · **Filed under:** `scope:failed-units-2026-07-25`

## Why this exists

Audited 2026-07-25: **this host had no working alert delivery path at all.**

- `deployment-audit-alert.service` and `staging-gate-alert.service` only `printf` a JSON line
  to the journal.
- There is no Alertmanager running (it *is* defined in `docker-compose.prod.yml` under
  `profiles: ["observability"]`, but not started).
- `OMNISIGHT_FAMILY5_DISCORD_CLIENT` is unset.

What that cost, found in a single sweep:

| unit | silent failure |
|---|---|
| `pipeline-coordinator` | failed **5 days** (host ENOSPC, 2026-07-20 → 07-25) |
| `omnisight-prod-backup` | failed **3 nights** — it had **no `OnFailure=` at all** |
| `staging-sync` | **10,093** failures, 0 successes, since 2026-05-12 |
| `staging-pg-snapshot` | **75/75** failures, never once succeeded |

JIRA is the only push channel empirically proven to work on this host, so that is what this
uses.

## What is installed

| artefact | purpose |
|---|---|
| `scripts/omnisight-alert-notify.py` | the channel: systemd `OnFailure=` → JIRA, idempotent |
| `scripts/omnisight-disk-floor-check.py` | host free-space floor → the same channel |
| `deploy/systemd/omnisight-alert@.service` | template, wire with `OnFailure=omnisight-alert@%n.service` |
| `deploy/systemd/omnisight-disk-floor.{service,timer}` | the floor check, every 15 min |
| `deploy/systemd/omnisight-onfailure-alert.conf.example` | the drop-in to add to any unit worth alerting on |

## Install (operator step — this is an anti-pattern-#13 trap otherwise)

```sh
install -m 700 -D scripts/omnisight-alert-notify.py     ~/.local/bin/omnisight-alert-notify.py
install -m 700 -D scripts/omnisight-disk-floor-check.py ~/.local/bin/omnisight-disk-floor-check.py
install -m 644 deploy/systemd/omnisight-alert@.service        ~/.config/systemd/user/
install -m 644 deploy/systemd/omnisight-disk-floor.service    ~/.config/systemd/user/
install -m 644 deploy/systemd/omnisight-disk-floor.timer      ~/.config/systemd/user/

# wire OnFailure onto every unit whose silence would matter
for u in omnisight-prod-backup pipeline-coordinator pipeline-coordinator-watchdog; do
  mkdir -p ~/.config/systemd/user/$u.service.d
  install -m 644 deploy/systemd/omnisight-onfailure-alert.conf.example \
    ~/.config/systemd/user/$u.service.d/onfailure.conf
done

systemctl --user daemon-reload
systemctl --user enable --now omnisight-disk-floor.timer
```

Verify: `systemctl --user show <unit> -p OnFailure --value` is non-empty, and
`omnisight-alert-notify.py --self-test` prints the authenticated JIRA account.

## Design decisions worth not re-litigating

**The scripts deliberately do not live in, or read from, a repo checkout.** All three checkouts
on this host are compromised in a different way — `/home/user/omnisight-prod` is release-pinned,
`/home/user/sora-bridge` is 202 commits behind with a dirty tree while its sync reports success,
and the canonical work tree has ~735 dirty paths. An alerting path must not inherit that. The
repo copy is the source of truth for *review and backup*; `~/.local/bin` is the runtime. The two
filenames are identical so drift is a `sha256sum` compare.

**Idempotent by construction.** At most ONE open JIRA issue per source, keyed on the label
`alert:unit=<slug>`; repeat failures add a throttled comment. This is load-bearing rather than
merely polite: the DLP gate this pairs with (OP-2729) blocks *by design* on every unreviewed
content change, so a chatty alert would be muted within a month and would recreate the exact
silence it exists to prevent.

**The handler always exits 0.** An `OnFailure=` handler that itself fails gives you restart loops
and lost signal. If JIRA is unreachable the event is spooled to
`~/.local/state/omnisight-alerts/spool.jsonl`. The template unit has no `OnFailure=` of its own.

**The disk floor is not a Prometheus alerting rule — on purpose.** Prod's `rule_files` is an
*explicit single-entry list* (`/etc/prometheus/obs-rules/project_state_health.yml`) inside
`configs/prometheus.yml`, and that whole tree is the release-pinned checkout. It is **not a
glob**, so a rules file merely mounted into that directory is never read — two files
(`alerts.yml`, `frontend-freshness-alerts.yml`) sit there unloaded today, proving it, and
`/api/v1/rules` reports exactly one loaded group. Making a new rule load would require either
dirtying the pinned checkout — which hard-blocks `deploy-prod.sh` *and emergency digest
rollback* — or mounting a replacement `prometheus.yml` that silently masks the release's own
config. Keeping the threshold in an operator-owned checker avoids all of it, and the checker's
own failure is wired to the same channel, so the watcher is watched.

**Floor calibration.** On 2026-07-20 the filesystem climbed 87.5% → 100% over nine days with no
floor and no alarm, filling the disk and killing `pipeline-coordinator` with ENOSPC. The default
warn threshold of 85% would have alerted roughly nine days before that outage. Critical is 92%.
Overridable via `DISK_WARN_PCT` / `DISK_CRIT_PCT`.

## Operating it

- One open issue per source. **Close it when the unit is healthy** — the next failure opens a
  fresh one. Leaving it open suppresses future alerts for that source by design.
- Throttle: `ALERT_THROTTLE_SECONDS` (default 3600). The disk check uses 6 h while merely above
  the warn floor and 1 h once critical.
- Local state: `~/.local/state/omnisight-alerts/` — `run.log` (one line per decision),
  `<slug>.stamp` (throttle), `spool.jsonl` (deliveries that could not reach JIRA).
- Credentials: `OMNISIGHT_ALERT_JIRA_ENV` (default `~/.config/omnisight/jira-claude.env`) plus
  the sibling `-token` file.

## Known gaps (tracked, not fixed here)

- The repo copy and the installed copy can drift (anti-pattern #13). Mitigated by identical
  filenames + a `sha256sum` compare; a scheduled drift check is a follow-up.
- `deployment-audit-alert.service` and `staging-gate-alert.service` still only `printf`. Whether
  to re-point them at this channel is an open decision on OP-2728.
- Alerts are created by the `claude-bot` JIRA account, so they inherit its permissions.
