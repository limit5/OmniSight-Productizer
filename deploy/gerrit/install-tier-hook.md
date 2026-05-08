# Install the Tier-classification Gerrit hook (OP-805 / G3)

> Server-side Gerrit hook that runs on every patchset upload, computes
> the change's Tier (s/m/l/x) from its changed paths, and writes the
> `Tier` label. Implements [ADR-0005](../../docs/adr/ADR-0005-tier-authority-levels.md)
> §4 layer 1 (path-based force-upgrade — contributors cannot downgrade).

## What you're installing

| File | Role |
|---|---|
| `scripts/gerrit-hooks/ref-update-tier-classify.sh` | Hook entry point. Parses Gerrit args, runs `git diff-tree`, calls the wrapper, queries the existing label, applies monotonicity, posts the new label via SSH, writes the audit log. |
| `scripts/gerrit-hooks/lib/classify_tier_via_backend.py` | Strategy chain: in-process import → HTTPS → default `m`. Always exits 0; the hook script reads the tier letter from stdout. |

## Which Gerrit hook event to wire it to — `patchset-created`

The ticket title says `ref-update`, but `patchset-created` is the
correct hook event for this purpose:

| Event | Sync? | Fires when | Posting `gerrit review` works? |
|---|---|---|---|
| `ref-update` | yes (pre-receive — can reject the push) | before refs are updated | the change record may not exist yet |
| `commit-received` | yes (pre-receive) | before refs are updated | same — too early |
| **`patchset-created`** | **no (async)** | **after the change + patchset exist** | **yes — the change is queryable and reviewable** |

We deliberately do **not** want to reject the upload on classifier
failure (the ADR-0005 risk note says "default to Tier M, log loudly"),
so the synchronous hooks are wrong. The script accepts `ref-update`-
style args (`--newrev`/`--refname`) for completeness, but production
deployments should use `patchset-created`.

## Prerequisites

1. **Gerrit `hooks` plugin** installed (`gerrit plugin install hooks`).
   Confirm with `ssh -p 29418 <admin>@<host> gerrit plugin ls`.
2. A Gerrit account that will post the `Tier` label — typically
   `tier-bot`. It must be:
   - a member of `ai-reviewer-bots` (so the existing access blocks let
     it vote Code-Review labels — Tier is a separate label and is
     governed by the same group),
   - granted `label-Tier = -2..+2` on `refs/heads/*` in
     `project.config` (the `Tier` label block must already exist on
     `refs/meta/config` per the ADR-0005 §4 example).
3. The `Tier` label and submit-requirements deployed to
   `refs/meta/config` (sister ticket — outside G3 scope; G4 wires the
   submit-requirements that consume the label).
4. An audit log location with weekly rotation. The wrapper's
   `TimedRotatingFileHandler` handles rotation in-process; if you
   prefer OS-level rotation, see [Logrotate (optional)](#logrotate-optional)
   below.

## Install

```bash
# 1. Mirror the hooks tree into the Gerrit site.
GERRIT_SITE=/var/gerrit
sudo install -d -o gerrit -g gerrit "$GERRIT_SITE/hooks/lib"
sudo install -m 0755 -o gerrit -g gerrit \
    scripts/gerrit-hooks/ref-update-tier-classify.sh \
    "$GERRIT_SITE/hooks/patchset-created"
sudo install -m 0755 -o gerrit -g gerrit \
    scripts/gerrit-hooks/lib/classify_tier_via_backend.py \
    "$GERRIT_SITE/hooks/lib/classify_tier_via_backend.py"

# 2. Drop the env file with credentials.
sudo install -m 0640 -o gerrit -g gerrit /dev/stdin /etc/default/gerrit-tier-hook <<'EOF'
GERRIT_HOOK_SSH_HOST=localhost
GERRIT_HOOK_SSH_PORT=29418
GERRIT_HOOK_SSH_USER=tier-bot
GERRIT_HOOK_SSH_KEY=/var/gerrit/.ssh/tier-bot.ed25519
GERRIT_HOOK_AUDIT_LOG=/var/log/gerrit-tier-hook.log

# Strategy 1 (preferred): in-process classifier import.
GERRIT_HOOK_BACKEND_PYTHONPATH=/opt/omnisight/backend-clone

# Strategy 2 (fallback): HTTPS to the backend's classify endpoint.
# Comment out if the host can run the in-process import.
#GERRIT_HOOK_BACKEND_URL=https://api.sora.services
#GERRIT_HOOK_BACKEND_API_KEY=ak-xxxxxxxxxxxxxxxx
EOF

# 3. Make the hook script source the env file.
#    The hooks plugin invokes the script directly, so we wrap it.
sudo tee "$GERRIT_SITE/hooks/patchset-created" >/dev/null <<'EOF'
#!/bin/bash
set -a
[ -f /etc/default/gerrit-tier-hook ] && . /etc/default/gerrit-tier-hook
set +a
exec /var/gerrit/hooks/ref-update-tier-classify.sh "$@"
EOF
sudo chmod 0755 "$GERRIT_SITE/hooks/patchset-created"
sudo install -m 0755 -o gerrit -g gerrit \
    scripts/gerrit-hooks/ref-update-tier-classify.sh \
    "$GERRIT_SITE/hooks/ref-update-tier-classify.sh"

# 4. Create the audit log with the right ownership.
sudo install -d -o gerrit -g adm -m 0750 /var/log
sudo install -m 0640 -o gerrit -g adm /dev/null /var/log/gerrit-tier-hook.log

# 5. Verify the hook runs.
#    (Push a synthetic patchset; see "Synthetic tests" below.)
```

> **Why two scripts?** The Gerrit `hooks` plugin invokes a *single
> file* per event name (`patchset-created`). We use a one-line shim at
> `$GERRIT_SITE/hooks/patchset-created` that loads the env file, then
> exec's the real implementation in `ref-update-tier-classify.sh`.
> Keeps credentials in `/etc/default/...` (mode `0640`, root-owned)
> rather than leaking them via `ps`.

## Synthetic tests (AC verification)

These tests assume the hook is installed and `tier-bot` can SSH to
Gerrit. Replace `<repo>` / `<remote>` as appropriate for your site.

### AC2 — Tier S whitelist (test-only paths)

```bash
git checkout -b op805-ac2 develop
mkdir -p backend/tests
echo "def test_synthetic(): assert True" > backend/tests/test_op805_ac2.py
git add backend/tests/test_op805_ac2.py
git commit -m "[OP-805] AC2 synthetic: tests-only patch should classify as Tier S"
git push <remote> HEAD:refs/for/develop
sleep 5
ssh -p 29418 tier-bot@<host> gerrit query --format=JSON \
    --current-patch-set --all-approvals \
    "owner:self change:op805-ac2" | jq '.currentPatchSet.approvals[] | select(.type=="Tier")'
# Expect: { "type": "Tier", "value": "s", ... }
```

Acceptable end-to-end latency target: **< 5s** from push to label
visible (per ticket AC). Inspect `/var/log/gerrit-tier-hook.log` for
the `classified ... computed_tier=s` line.

### AC3 — Path force-upgrade

```bash
git checkout -b op805-ac3 develop
echo "# noop" >> backend/security/foo.py    # path force-upgrades to L
git add backend/security/foo.py
git commit -m "[OP-805] AC3 synthetic: backend/security/* must force Tier L"
git push <remote> HEAD:refs/for/develop
# Expect Tier=l (even if a contributor tried to set it lower — the
# hook overwrites unconditionally on each patchset upload).
```

### AC4 — Reviewer monotonicity (promote-only)

1. Push any Tier M change.
2. Manually promote: `ssh ... gerrit review --label Tier=l <change>,<ps>`.
3. Push a follow-up patchset with no scope change (e.g. an amend that
   only touches an existing M-tier path).
4. The hook computes `m`, reads the existing `l`, and posts `Tier=l`
   (`max(existing, computed) = l`) — verify in
   `/var/log/gerrit-tier-hook.log`:

   ```
   classified change_id=... computed_tier=m existing_label=l final_label=l
   ```

### AC5 — Audit log + weekly rotation

```bash
ls -l /var/log/gerrit-tier-hook.log*
# Force a roll-over for testing (simulates the weekly W0 event):
python3 - <<'EOF'
import logging.handlers
h = logging.handlers.TimedRotatingFileHandler(
    "/var/log/gerrit-tier-hook.log", when="W0", backupCount=8, utc=True,
)
h.doRollover()
EOF
ls -l /var/log/gerrit-tier-hook.log*    # expect a .YYYY-MM-DD suffix appearing
```

## Local dry-run (no Gerrit needed)

The script runs in dry-run mode against any local commit, useful for
operator smoke tests before wiring to a live Gerrit:

```bash
GERRIT_HOOK_DRY_RUN=1 GERRIT_HOOK_AUDIT_LOG=/tmp/tier-hook.log \
    scripts/gerrit-hooks/ref-update-tier-classify.sh \
    --change 9999 --commit HEAD --project test --patchset 1
cat /tmp/tier-hook.log
```

In dry-run mode the SSH `gerrit query` and `gerrit review` calls are
skipped; everything else (path extraction, classifier invocation, audit
log) runs as in production.

## Logrotate (optional)

The Python wrapper's `TimedRotatingFileHandler` rotates weekly on its
own, so this is only needed if your ops policy mandates OS-level
logrotate consistency:

```
# /etc/logrotate.d/gerrit-tier-hook
/var/log/gerrit-tier-hook.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

If you enable this, drop the in-process rotation by setting
`GERRIT_HOOK_AUDIT_LOG=/dev/stderr` and piping stderr to systemd
journal — but most operators prefer the default (in-process
`W0` rotation, 8-week retention).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Label never appears, no audit lines | hook not invoked | Check `$GERRIT_SITE/logs/error_log` for "executing hook patchset-created"; verify the symlink/shim path. |
| Audit shows `strategy=in-process status=unavailable reason=import-error` | G2 classifier not on `PYTHONPATH` | Set `GERRIT_HOOK_BACKEND_PYTHONPATH` to a backend repo clone, or fall through to HTTPS. |
| Audit shows `strategy=https status=fail` | backend unreachable / wrong key | Check `GERRIT_HOOK_BACKEND_URL` reachability + `GERRIT_HOOK_BACKEND_API_KEY` validity. |
| Audit shows `gerrit_review_failed` | tier-bot ACL missing | Grant `label-Tier=-2..+2` to the bot's group on `refs/meta/config`. |
| All changes get `Tier=m` regardless of paths | classifier fallback | The script defaulted to `m` deny-by-default; this is intentional when both strategies fail. Address the upstream cause first. |

## Related

- ADR-0005 §4 — 4-layer protection rationale
- OP-803 / G1 — `tier-paths.yaml` (path map)
- OP-804 / G2 — `backend.governance.tier_classifier` (the function this hook calls)
- OP-806 / G4 — submit-requirements that consume the `Tier` label
- OP-808 / G5 — cooldown enforcement on misclassification (not in this hook)
