# Evening Operator Tasks — 2026-05-18

Three follow-up tasks deferred to evening (sudo / OpenAI key / S3 creds availability):

| Task | Blocker resolved at | Estimated time |
|---|---|---|
| **A.** Family F per-instance Gerrit accounts | sora admin SSH access from home machine | ~30-45 min |
| **B.** Graphiti MCP deploy | OpenAI API key available | ~20-30 min |
| **C.** S3 backup pipeline activation | S3 credentials available | ~20-30 min |

**Total estimated time: ~75-105 min.** Tasks are independent; run in any order.

Prerequisites for all tasks: be on the prod host (this WSL/Linux machine) or SSH'd in with normal user (not sudo unless noted).

---

## Task A — Family F per-instance Gerrit accounts

**Goal**: Create 5 Gerrit accounts (`codex-bot-codex-1/2/3`, `claude-bot-claude-1/2`) so Family F systemd runners can use per-instance authentication instead of the umbrella `codex-bot`/`claude-bot` fallback that today's tactical fix installed.

**Why this matters**: per-instance accounts give per-runner audit trail, per-runner JIRA cred isolation, and proper claim mutex namespacing (per memory `project_family_f_gerrit_account_gap`).

### Pre-flight check (no changes yet)

```bash
# 1. Confirm sora SSH key works to Gerrit admin shell (must succeed)
ssh -i ~/.ssh/id_ed25519 -p 29418 sora@sora.services gerrit version

# 2. Confirm per-instance pubkeys exist locally (should print 5 lines, ed25519 keys)
for k in codex-bot-codex-1 codex-bot-codex-2 codex-bot-codex-3 claude-bot-claude-1 claude-bot-claude-2; do
  if [[ -f ~/.config/omnisight/gerrit-${k}-ed25519.pub ]]; then
    echo "✓ ${k} pubkey present"
  else
    # public key may need extraction from private key
    if [[ -f ~/.config/omnisight/gerrit-${k}-ed25519 ]]; then
      ssh-keygen -y -f ~/.config/omnisight/gerrit-${k}-ed25519 > ~/.config/omnisight/gerrit-${k}-ed25519.pub
      echo "✓ ${k} pubkey derived from private key"
    else
      echo "✗ ${k} BOTH MISSING — abort"
    fi
  fi
done
```

**Abort if anything ✗.** Don't proceed until all 5 keys are accessible.

### Step A.1 — Create 5 Gerrit accounts (via sora admin SSH)

```bash
# Run on this host (where ~/.ssh/id_ed25519 is sora's identity)
for spec in \
  "codex-bot-codex-1:rt3628+codex-bot-codex-1@gmail.com:codex-bot codex-1" \
  "codex-bot-codex-2:rt3628+codex-bot-codex-2@gmail.com:codex-bot codex-2" \
  "codex-bot-codex-3:rt3628+codex-bot-codex-3@gmail.com:codex-bot codex-3" \
  "claude-bot-claude-1:rt3628+claude-bot-claude-1@gmail.com:claude-bot claude-1" \
  "claude-bot-claude-2:rt3628+claude-bot-claude-2@gmail.com:claude-bot claude-2"
do
  IFS=":" read -r username email fullname <<< "$spec"
  pubkey=$(cat ~/.config/omnisight/gerrit-${username}-ed25519.pub)
  # Use --ssh-key '-' to read from stdin; --full-name needs quoting if it has space
  ssh -i ~/.ssh/id_ed25519 -p 29418 sora@sora.services \
    gerrit create-account \
      --email "$email" \
      --full-name \"$fullname\" \
      --group ai-reviewer-bots \
      --http-password $(openssl rand -hex 16) \
      --ssh-key "'$pubkey'" \
      "$username" 2>&1 | tee /tmp/gerrit-create-$username.log
done
```

> ⚠ The `--ssh-key '-'` form is preferred (reads stdin). If the above fails with quoting issues, use the explicit stdin form per `gerrit create-account --help`.

### Step A.2 — Verify accounts exist + can authenticate

```bash
# For each new account, SSH in as that user using its private key
for username in codex-bot-codex-1 codex-bot-codex-2 codex-bot-codex-3 claude-bot-claude-1 claude-bot-claude-2; do
  echo "─── $username ───"
  ssh -i ~/.config/omnisight/gerrit-${username}-ed25519 -p 29418 ${username}@sora.services \
    gerrit version 2>&1 | head -2
done
# Expected: all 5 return "gerrit version 3.13.5" — anything else means key/account isn't wired
```

### Step A.3 — Generate HTTP passwords (for git push over HTTPS, even if you only use SSH today)

```bash
# Each per-instance account also needs an HTTP password for the auto-runner's
# JIRA-comment-via-HTTP path. Use Gerrit's set-account --generate-http-password.
for username in codex-bot-codex-1 codex-bot-codex-2 codex-bot-codex-3 claude-bot-claude-1 claude-bot-claude-2; do
  ssh -i ~/.ssh/id_ed25519 -p 29418 sora@sora.services \
    gerrit set-account --generate-http-password "$username" 2>&1 | tail -3
done
```

> Save the generated passwords. The auto-runner reads them from `~/.config/omnisight/gerrit-${username}-http-password` (per memory `project_codex_bot_http_password`).

### Step A.4 — Revert systemd to per-instance INSTANCE_ID

```bash
# Edit BOTH service templates to use per-instance instance_id
for cls in claude codex; do
  sed -i 's|^Environment=OMNISIGHT_RUNNER_INSTANCE_ID=default|Environment=OMNISIGHT_RUNNER_INSTANCE_ID='"$cls"'-%i|' \
    ~/.config/systemd/user/runner-${cls}@.service
done

# Verify
grep "INSTANCE_ID" ~/.config/systemd/user/runner-{claude,codex}@.service

# Daemon-reload + restart all 5
systemctl --user daemon-reload
for inst in claude@1 claude@2 codex@1 codex@2 codex@3; do
  systemctl --user restart runner-${inst}.service
  sleep 1
done

# Wait ~60s then verify each runner authenticated as ITS per-instance user
sleep 60
for log in /tmp/runner-{claude-1,claude-2,codex-1,codex-2,codex-3}.log; do
  inst=$(basename $log .log | sed 's/runner-//')
  echo "─── $inst ───"
  grep "authenticated as" $log | tail -1
done
# Expected: each runner says authenticated as rt3628+${class}-bot-${class}-N@gmail.com
```

### Step A.5 — Cleanup memory entry

```bash
# Mark project_family_f_gerrit_account_gap.md as RESOLVED in MEMORY.md (operator action)
echo "Task A complete: 5 per-instance Gerrit accounts created + systemd reverted to per-instance INSTANCE_ID."
echo "memory/project_family_f_gerrit_account_gap.md can be updated to status: resolved-2026-05-18-evening"
```

---

## Task B — Graphiti MCP deploy

**Goal**: bring up Graphiti container for temporal-axis memory in the 3D memory architecture (per Sprint F memory model).

**Blockers**:
- Image namespace wrong: compose references `getzep/graphiti:latest`, real image is `zepai/graphiti:latest`
- Needs OpenAI API key for embeddings (Graphiti is an OpenAI-coupled service)
- Cloudflare Tunnel route is operator dashboard work (defer if needed)

### Pre-flight

```bash
# 1. Confirm OPENAI_API_KEY is in .env (sk-... ~50 chars)
grep "^OPENAI_API_KEY=" /home/user/work/sora/OmniSight-Productizer/.env | cut -c1-30
# If empty: add it — see Step B.1

# 2. Confirm Neo4j (Graphiti's storage) is healthy
docker ps --filter "name=neo4j" --format "{{.Names}} {{.Status}}"
# Expected: omnisight-productizer-neo4j-1 ... Healthy
```

### Step B.1 — Add OpenAI API key to `.env`

```bash
cd /home/user/work/sora/OmniSight-Productizer

# Replace placeholder with real key (paste your actual key)
if grep -q "^OPENAI_API_KEY=" .env; then
  sed -i 's|^OPENAI_API_KEY=.*|OPENAI_API_KEY=sk-YOUR-REAL-KEY-HERE|' .env
else
  echo "OPENAI_API_KEY=sk-YOUR-REAL-KEY-HERE" >> .env
fi

# Verify (DO NOT cat in commit messages or logs)
grep "^OPENAI_API_KEY=" .env | cut -c1-25
```

### Step B.2 — Fix the Graphiti image namespace

There are 2 places to fix:

```bash
cd /home/user/work/sora/OmniSight-Productizer

# 1. docker-compose.yml line 154
sed -i 's|image: getzep/graphiti:latest|image: zepai/graphiti:latest|' docker-compose.yml
grep -A2 "graphiti:" docker-compose.yml | head -5
# Expected: "image: zepai/graphiti:latest"

# 2. docs/operations/graphiti-mcp-runbook.md (find + replace)
if [[ -f docs/operations/graphiti-mcp-runbook.md ]]; then
  sed -i 's|getzep/graphiti|zepai/graphiti|g' docs/operations/graphiti-mcp-runbook.md
fi

# 3. Commit + push to develop via standard claude-bot flow
# (or do it via runner ticket — for trivial doc/config fix, direct push is faster)
git add docker-compose.yml docs/operations/graphiti-mcp-runbook.md
git status
# Decide whether to commit + push now or via runner ticket
```

### Step B.3 — Bring up Graphiti

```bash
# Pre-pull the image to verify the namespace fix is right
docker pull zepai/graphiti:latest
# Expected: pull succeeds (vs. getzep namespace which 404'd before)

# Bring up just the graphiti profile (does NOT touch backend/frontend)
docker compose --profile graphiti up -d graphiti

# Wait + check
sleep 30
docker ps --filter "name=graphiti" --format "{{.Names}} {{.Status}}"
docker logs --tail 30 omnisight-productizer-graphiti-1 2>&1
```

### Step B.4 — Smoke test

```bash
# Get the host port (per compose file — check first)
graphiti_port=$(grep -A20 "graphiti:" /home/user/work/sora/OmniSight-Productizer/docker-compose.yml | grep "ports:" -A2 | head -3 | grep -oP '\d+:\d+' | head -1 | cut -d: -f1)
echo "Graphiti port: $graphiti_port"

# Inside container, test the HTTP endpoint
docker exec omnisight-productizer-graphiti-1 curl -s -o /dev/null -w "status=%{http_code}\n" http://127.0.0.1:8000/health 2>&1
# If 200: graphiti is up
# If empty/error: container failed to start — check logs
```

### Step B.5 — Cloudflare Tunnel route (DEFERRED if dashboard access not available)

The audit doc notes the CF Tunnel only routes `ai.sora-dev.app` → caddy. To expose Graphiti externally:

**Option 1** (no dashboard, recommended for tonight): Path-based proxy through Caddy

```bash
# Add a Caddy block for /mcp-graphiti/* → graphiti:8000
# Edit deploy/reverse-proxy/Caddyfile (operator-side; details out of scope here)
```

**Option 2** (needs dashboard login, defer to tomorrow): add `mcp-graphiti.sora.services` route in Cloudflare Tunnel config.

For now, leave Graphiti internal-only — backend can talk to it via `graphiti:8000` internal DNS without external exposure.

---

## Task C — S3 backup pipeline activation

**Goal**: Activate existing `scripts/backup_postgres_daily.sh` (already shipped; OP-887) with a systemd daily timer + S3 credentials. Replaces today's pg_dump-to-local-disk fallback.

### Pre-flight

```bash
# 1. Confirm the backup script is present (it should be — shipped earlier)
ls /home/user/work/sora/OmniSight-Productizer/scripts/backup_postgres_daily.sh
# Expected: -rwxr-xr-x ...

# 2. Read what env vars it needs
head -20 /home/user/work/sora/OmniSight-Productizer/scripts/backup_postgres_daily.sh
# Required: OMNISIGHT_DATABASE_URL, OMNISIGHT_BACKUP_S3_URI
# Optional: OMNISIGHT_BACKUP_S3_ENDPOINT, OMNISIGHT_BACKUP_DIR, OMNISIGHT_BACKUP_ALERT_WEBHOOK
```

### Step C.1 — Acquire S3 credentials

You said "S3 creds available tonight." Pick an S3-compatible target:

- **AWS S3**: standard `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` + bucket name + region
- **Cloudflare R2** (recommended — no egress fees): account ID + R2 token (access key id + secret) + bucket name + endpoint `https://<acct>.r2.cloudflarestorage.com`
- **Backblaze B2**: similar, endpoint `https://s3.us-west-001.backblazeb2.com` or per-region
- **MinIO self-hosted**: endpoint `https://minio.sora.services` (if you have one)

### Step C.2 — Populate env

```bash
# Add to ~/.config/omnisight/backup.env (NOT committed; secret)
mkdir -p ~/.config/omnisight
cat > ~/.config/omnisight/backup.env <<'EOF'
# pg_dump backup credentials — do NOT commit
OMNISIGHT_DATABASE_URL=postgresql://omnisight:CHANGEME@omnisight-pg-primary:5432/omnisight
OMNISIGHT_BACKUP_S3_URI=s3://your-bucket-name/omnisight-prod/postgres
OMNISIGHT_BACKUP_S3_ENDPOINT=https://your-r2-or-s3-endpoint
AWS_ACCESS_KEY_ID=YOUR_KEY
AWS_SECRET_ACCESS_KEY=YOUR_SECRET
AWS_DEFAULT_REGION=auto
EOF
chmod 600 ~/.config/omnisight/backup.env

# Verify
ls -la ~/.config/omnisight/backup.env
# Expected: -rw------- (mode 600)
```

### Step C.3 — Dry run

```bash
cd /home/user/work/sora/OmniSight-Productizer

# Source the env, then dry-run
( set -a; source ~/.config/omnisight/backup.env; set +a; \
  bash scripts/backup_postgres_daily.sh --dry-run --label "evening-test" ) 2>&1 | tee /tmp/backup-dryrun-$(date +%Y%m%dT%H%M%S).log

# Look for: "DRY RUN" indicators + size/file calculations
# Expected: 0 actual S3 PUTs but valid plan output
```

### Step C.4 — Real run (one-shot)

```bash
( set -a; source ~/.config/omnisight/backup.env; set +a; \
  bash scripts/backup_postgres_daily.sh --label "evening-first" )

# Then verify S3 has the object
( set -a; source ~/.config/omnisight/backup.env; set +a; \
  aws s3 ls "$OMNISIGHT_BACKUP_S3_URI/" --endpoint-url "$OMNISIGHT_BACKUP_S3_ENDPOINT" )
# Expected: a .sql.gz file with today's date
```

### Step C.5 — Wire systemd user timer for daily 03:00 UTC

```bash
# Service unit
cat > ~/.config/systemd/user/omnisight-pgdump-s3-daily.service <<'EOF'
[Unit]
Description=Daily Postgres dump → S3 (OP-887, evening activation 2026-05-18)
After=docker.service network-online.target

[Service]
Type=oneshot
EnvironmentFile=%h/.config/omnisight/backup.env
WorkingDirectory=%h/work/sora/OmniSight-Productizer
ExecStart=/bin/bash %h/work/sora/OmniSight-Productizer/scripts/backup_postgres_daily.sh --label daily-s3
StandardOutput=append:%h/work/sora/logs/omnisight-pgdump-s3-daily.log
StandardError=append:%h/work/sora/logs/omnisight-pgdump-s3-daily.log
EOF

# Timer unit
cat > ~/.config/systemd/user/omnisight-pgdump-s3-daily.timer <<'EOF'
[Unit]
Description=Trigger daily Postgres dump → S3 at 03:00 UTC

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

mkdir -p ~/work/sora/logs
systemctl --user daemon-reload
systemctl --user enable --now omnisight-pgdump-s3-daily.timer

# Verify
systemctl --user list-timers omnisight-pgdump-s3-daily.timer
# Expected: NEXT shows tomorrow 03:00 UTC, LAST n/a (or today if already fired)
```

### Step C.6 — Disable the local-only fallback timer (if active)

We added a local pg_dump timer (`omnisight-pgdump-local-daily.timer`) earlier today as a fallback. Now that S3 is wired, you can keep both running (defense-in-depth) OR disable the local one.

```bash
# Option: keep both (recommended — local file is a faster restore source, S3 is offsite)
# Option: disable local fallback
# systemctl --user disable --now omnisight-pgdump-local-daily.timer

# Verify what's enabled
systemctl --user list-timers omnisight-pgdump* 2>/dev/null
```

---

## Post-tasks audit

After all 3 tasks done:

```bash
# A. Family F: are all 5 runners authed as per-instance user?
for log in /tmp/runner-{claude-1,claude-2,codex-1,codex-2,codex-3}.log; do
  grep "authenticated as" $log | tail -1
done

# B. Graphiti: is it healthy + responding?
docker ps --filter "name=graphiti" --format "{{.Names}} {{.Status}}"

# C. S3 backup: is the timer scheduled?
systemctl --user list-timers omnisight-pgdump-s3-daily.timer
```

If anything fails, file a ticket (or comment in this doc) — most failure modes here will need operator decision.

---

## Memory updates after completion

```bash
# Update / archive these memory entries when tasks complete:
#
# A. project_family_f_gerrit_account_gap.md → mark resolved
# B. (new memory) project_graphiti_mcp_deploy_2026_05_18
# C. (new memory) project_s3_backup_pipeline_activated_2026_05_18
#
# These can wait until you're back at the keyboard — Claude will help write them.
```

---

**Total estimated time: ~75-105 min** (assuming no per-task surprises).

If a task fails hard (e.g. Gerrit account creation rejects), STOP and document in this doc + open a JIRA ticket. Don't try to recover destructively; the morning's TOCTOU lessons all apply.
