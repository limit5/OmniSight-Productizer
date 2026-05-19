# GitLab Container Registry — Daily Monitor

**Ticket:** OP-1514 (T1.4 of META OP-1478) • **Owner:** L2 operator
on the prod-on-call rotation • **Status:** ships disabled; operator
enables once OP-1478 main-sync lands the unit files.

This runbook documents the `gitlab-cr-monitor.timer` daily check that
confirms the GitLab Container Registry is reachable AND has fresh
images for the `omnisight/OmniSight-Productizer` project. It is the
heartbeat for the META OP-1478 activation chain:

```
register runner (OP-1511) → bundle.json populated by pipeline →
CR populated → compose cutover (OP-1473) → ghcr.io decommission
```

Without this monitor the failure mode is silent: pipelines run green,
the CR slowly empties under the cleanup policy, and the next deploy
finds an empty registry. The monitor is fire-and-forget on the steady
path and pages `operator_notifier` (severity=DEGRADED) when the latest
tag goes stale, matching `gerrit-jira-bridge-watchdog.timer` /
`auto-promote-develop.timer`.

## 1. What the monitor does

The service runs `scripts/verify_gitlab_cr.sh` once per timer firing:

1. Reads the GitLab personal-access token from
   `~/.config/omnisight/gitlab-claude-token` (override:
   `OMNISIGHT_GITLAB_TOKEN_FILE`).
2. Queries `GET /api/v4/projects/<encoded-path>/registry/repositories`
   on the configured GitLab API host (default
   `https://sora.services:49154`; override:
   `OMNISIGHT_GITLAB_API_URL`).
3. Picks the first repository in the listing, queries
   `GET .../registry/repositories/<id>/tags?per_page=20`, then walks
   each tag's detail endpoint to find the most-recently-created tag.
4. Compares that tag's timestamp against the freshness threshold
   (default 48 hours; override:
   `OMNISIGHT_GITLAB_CR_FRESHNESS_HOURS`).
5. Emits ONE JSON line on stderr with the result, and exits 0 on
   pass, 1 on any failure.
6. On `status="stale"` (CR reachable but pipeline has stopped
   producing fresh images) it dispatches an operator-notifier page at
   `severity=DEGRADED` carrying the runbook path back to this file.

The full status vocabulary (also returned in the JSON `status` field):

| status                | meaning                                           | exit | notifier? |
|-----------------------|---------------------------------------------------|------|-----------|
| `all_ok`              | repo exists; latest tag within freshness window   | 0    | no        |
| `cr_empty`            | no repos OR repo has no tags                      | 1    | no        |
| `stale`               | latest tag older than the freshness threshold     | 1    | yes       |
| `auth_failed`         | GitLab API returned 401 / 403                     | 1    | no        |
| `api_error`           | GitLab API returned any other non-200             | 1    | no        |
| `no_token`            | token file missing, unreadable, or empty          | 1    | no        |
| `missing_dependency`  | `curl` or `jq` not installed on the host          | 1    | no        |

`cr_empty` does NOT page on purpose. AC 3 explicitly states the script
returns exit 1 + `cr_empty` BEFORE O2 lands; paging on that pre-O2
state would generate noise during the activation window. The journal
line is sufficient signal until O2 closes.

## 2. Activation step (operator, ONCE)

The systemd unit files ship via this ticket but are NOT enabled. The
existing main-sync mechanism (OP-877) auto-pulls
`deploy/systemd/gitlab-cr-monitor.{service,timer}` into
`%h/.config/systemd/user/` on the prod host. After that lands the
operator runs ONE round of:

```bash
systemctl --user daemon-reload
systemctl --user enable --now gitlab-cr-monitor.timer
loginctl enable-linger $USER  # only if not already set; see
                              # auto-promote-develop.service docs
```

Verify:

```bash
systemctl --user list-timers --all | grep gitlab-cr-monitor
# expect a NEXT row dated *-*-* 11:00:00

systemctl --user status gitlab-cr-monitor.timer
# Active: active (waiting)

systemctl --user start gitlab-cr-monitor.service  # one-shot dry run
journalctl --user -u gitlab-cr-monitor.service -n 50
# expect a single JSON line ending with "status":"all_ok" (post-O2)
# or "status":"cr_empty" (pre-O2)
```

## 3. Reading an alert

The page body carries:

- `code=gitlab_cr_stale`
- `severity=DEGRADED`
- context: `api_url`, `project`, `freshness_hours`,
  `hours_since_last_tag`, `latest_tag`, `runbook` (points back to
  this file).

When the alert fires:

1. **Confirm the alert is real.** Manually run
   `/bin/bash scripts/verify_gitlab_cr.sh` from the prod host. If the
   manual run prints `status="all_ok"`, the alert was either a one-off
   transient (slow API on the previous fire) or a clock-skew artifact;
   acknowledge in the notifier and move on.
2. **If `status="stale"` repeats**, the pipeline has stopped
   publishing. Likely root causes, in descending order of probability:
   - The runner went offline (`systemctl status gitlab-runner.service`
     on the runner host) — see OP-1511's template.
   - No new `v*` tag has been cut recently. Cross-check with
     `git tag --sort=-creatordate | head -5`.
   - The cleanup policy has aged out the most-recent tag (rare;
     `v*` is in the keep-regex per
     `docs/operations/gitlab-cr-pull-credentials.md` §2).
   - The CR itself is degraded. Check
     `curl -sSf -o /dev/null -w '%{http_code}\n' https://sora.services:49154/v2/`
     — `401` is healthy.
3. **If `status="auth_failed"`** the token has been rotated /
   revoked. Reissue per
   `docs/operations/gitlab-cr-pull-credentials.md` §4 and overwrite
   `~/.config/omnisight/gitlab-claude-token`.

## 4. Manual verification (CI / smoke)

The script is safe to run by hand at any time:

```bash
# default — uses production-shaped defaults
/bin/bash scripts/verify_gitlab_cr.sh

# override the freshness window (useful when re-testing right after
# a fresh tag lands)
OMNISIGHT_GITLAB_CR_FRESHNESS_HOURS=1 /bin/bash scripts/verify_gitlab_cr.sh

# suppress the operator_notifier page (for smoke tests that should
# never wake up the on-call)
OMNISIGHT_GITLAB_CR_NOTIFY=0 /bin/bash scripts/verify_gitlab_cr.sh
```

The exit code is the assertion; the stderr JSON is the structured
status for log scraping. AC 3 expects:

- Before O2 (CR empty): exit 1 + `status="cr_empty"`.
- After O2 (CR populated): exit 0 + `status="all_ok"`.

## 5. Configuration reference

| Env var                              | Default                                    | Meaning                                              |
|--------------------------------------|--------------------------------------------|------------------------------------------------------|
| `OMNISIGHT_GITLAB_API_URL`           | `https://sora.services:49154`              | GitLab API base URL                                  |
| `OMNISIGHT_GITLAB_PROJECT_PATH`      | `omnisight%2FOmniSight-Productizer`        | URL-encoded `:id` for `/api/v4/projects/:id/...`     |
| `OMNISIGHT_GITLAB_TOKEN_FILE`        | `~/.config/omnisight/gitlab-claude-token`  | Token file path; contents read whole, newlines stripped |
| `OMNISIGHT_GITLAB_CR_FRESHNESS_HOURS`| `48`                                       | Stale threshold for the most recent tag              |
| `OMNISIGHT_GITLAB_CR_NOTIFY`         | `1`                                        | Set `0` to suppress notifier page (tests/dry-runs)   |
| `OMNISIGHT_REPO_ROOT`                | `<repo>` (auto-detected)                   | Repo root, used as `PYTHONPATH` for notifier import  |

## 6. AC reference (OP-1514)

| AC # | Criterion                                                              | Evidence location                                       |
|------|------------------------------------------------------------------------|---------------------------------------------------------|
| 1    | Script + service + timer + docs files exist with the documented shape  | `scripts/verify_gitlab_cr.sh`, `deploy/systemd/gitlab-cr-monitor.{service,timer}`, this file |
| 2    | Documented operator activation procedure                               | §2 of this file                                         |
| 3    | Manual run pre-O2 returns exit 1 + `cr_empty`; post-O2 exit 0 + `all_ok` | §4 of this file; verified by operator after O2 lands  |
| 4    | One full timer cycle observed; journal shows exit 0; no notifier page  | Verified by operator the day after activation; logged on OP-1514 |

## 7. Related

- `deploy/systemd/gitlab-runner.service` — OP-1511 runner template
- `docs/operations/gitlab-cr-pull-credentials.md` — token issuance,
  rotation, cleanup policy
- `docs/operations/gitlab-cr-cutover-checklist.md` — OP-1473 prod
  cutover (the consumer of the CR images this monitor watches)
- `docs/operations/gitlab-ci-image-build.md` — pipeline that produces
  the images
- `backend/agents/operator_notifier.py` — alert fan-out (severity →
  channel matrix, dedup, rate-limit)
