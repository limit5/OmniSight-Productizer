#!/usr/bin/env bash
#
# [OP-973] AUDIT-19c — continuous develop -> staging sync orchestrator.
#
# One run == one timer tick (deploy/systemd/staging-sync.{service,timer},
# every 10 min — matching the OP-965 canary cadence). The job: keep the
# staging stack tracking the *develop tip* so the OP-965 staging-gate
# probes (canary / smoke) and scripts/release_milestone_checker.py see a
# fresh, real signal for the commit R3 is about to ship.
#
# Pipeline:
#   1. Resolve the develop tip SHA (local `git fetch` comparison in
#      $SYNC_REPO by default; honours $SYNC_DEVELOP_TIP for CI/tests).
#      A fetch failure is treated as transient — log + exit 0, keep the
#      current staging up, retry next tick (error code DevelopTipFetchFailed).
#   2. Compare against the tag currently active on staging
#      ($OMNISIGHT_STAGING_STATE_DIR/active_tag, written by OP-878's
#      scripts/staging_deploy.sh). Unchanged => idempotent no-op, exit 0.
#   3. Deploy: scripts/staging_deploy.sh --image-tag <develop-tip>. That
#      script does the blue-green dance (start standby, wait /health,
#      switch Caddy, drain old) and reverts to the previous color on its
#      own health failure. If it exits non-zero, staging is back on the
#      previous tag — we record `staging_sync_failed_revert` + alert.
#   4. Alembic migrations against the freshly-deployed staging stack
#      (`docker compose run --rm backend-a python -m alembic upgrade heads`
#      in the new active color's compose project — same step OP-767's
#      scripts/auto_deploy_staging.py runs; the AUDIT-19b prod->staging
#      snapshot path expects staging's schema to be ahead of, never behind,
#      prod). Failure => StagingMigrationFailed: re-deploy the previous
#      tag (rollback), alert, `staging_sync_failed_revert`.
#   5. Post-deploy health re-check ($OMNISIGHT_STAGING_URL/health). Red =>
#      StagingHealthzFailed: re-deploy the previous tag, alert,
#      `staging_sync_failed_revert`.
#   6. On success: one `release_audit` row + the run-log line below.
#
# `release_audit` row — same schema caveat as infra/staging/snapshot-restore.sh
# (OP-972): alembic 0207's `release_audit.outcome` is CHECK-constrained to the
# develop->main promote vocabulary ('promoted','noop','milestone_not_accepted',
# 'ff_not_possible','push_rejected') — there is no 'staging_synced' value and
# no `kind` column. Extending the enum is a one-line alembic migration but that
# is the `backend`/`db` area, out of scope for this `devops`/`tests` ticket.
# Until that follow-up lands, staging-sync rows are written with
# outcome='noop' (the develop->main promote *pipeline* did nothing here — this
# row is an out-of-band observation) and `detail` carries the real payload:
#   {"kind":"staging_sync",
#    "outcome":"staging_synced|staging_sync_failed_revert",
#    "develop_tip":"<sha>","previous_tag":"<sha>","active_color":"blue|green",
#    "step":"deploy|migrate|healthz","duration_s":N,
#    "source":"scripts/sync_staging_to_develop.sh"}
# Operators select staging-sync rows with `detail->>'kind' = 'staging_sync'`.
# The audit write is best-effort — it never blocks or fails the sync (same
# philosophy as backend.audit / staging_gate.py / snapshot-restore.sh: "don't
# kill the train because the receipt printer ran out of paper").
#
# Error catalog (-> exit code -> systemd unit result):
#   DevelopTipFetchFailed   0   transient; current staging untouched, retry next tick
#   StagingDeployFailed     1   staging_deploy.sh reverted to the previous tag; alert
#   StagingMigrationFailed  1   alembic failed; previous tag re-deployed (rollback); alert
#   StagingHealthzFailed    1   post-deploy /health red; previous tag re-deployed; alert
#   (prerequisite failure)  3   missing git/docker/curl, or staging_deploy.sh absent
# A non-zero exit fires the OnFailure= chain in staging-sync.service
# (staging-gate-alert.service -> T1 alerter, OP-722) and `alert()` below also
# POSTs the structured payload to $OMNISIGHT_STAGING_ALERT_WEBHOOK if set
# (the same Slack webhook scripts/staging_deploy.sh uses).
#
# GateTimerEnableFailed (from this ticket's DoD) is a *bring-up*-time error,
# not something this script handles — see deploy/systemd/staging-sync.timer's
# header for the enable sequence + the systemd-refuses fallback.
#
# Environment (all overridable; defaults match the prod 5a host):
#   SYNC_REPO               git checkout to read develop from   (/home/user/sora-bridge)
#   SYNC_REMOTE             remote to fetch                     (origin)
#   SYNC_BRANCH             branch to track                     (develop)
#   SYNC_DEVELOP_TIP        explicit tip SHA override (CI/tests; skips git fetch)
#   STAGING_DEPLOY_SH       path to OP-878 deployer  ($ROOT/scripts/staging_deploy.sh)
#   OMNISIGHT_STAGING_STATE_DIR   blue-green state dir          (/var/lib/omnisight/staging)
#   OMNISIGHT_STAGING_URL   public staging base URL             (https://staging.sora.services)
#   OMNISIGHT_STAGING_HEALTHZ_PATH   health path to re-check    (/health)
#   OMNISIGHT_STAGING_COMPOSE_FILE   staging compose file       ($ROOT/deploy/staging/docker-compose.yml)
#   OMNISIGHT_STAGING_ENV_FILE       staging compose env file   ($ROOT/deploy/staging/.env)
#   SYNC_MIGRATE_CMD        full alembic command override (eval'd; empty + SYNC_SKIP_MIGRATE unset => built-in)
#   SYNC_SKIP_MIGRATE       set to 1 to skip the alembic step entirely
#   OMNISIGHT_DATABASE_URL  release-audit DSN for the `release_audit` row (absent => row skipped, logged)
#   OMNISIGHT_STAGING_ALERT_WEBHOOK   Slack-compatible webhook for alert()  (optional)
#   DOCKER_BIN / GIT_BIN / CURL_BIN / PSQL_BIN   binary overrides
#
# Reference: OP-878 (staging_deploy.sh — the deployer), OP-965 (the gate
# producers this keeps fed), OP-972 (snapshot-restore.sh — the audit-row +
# error-catalog pattern this mirrors), OP-798 (sora-bridge sync — keeps
# $SYNC_REPO on origin/develop).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SELF="sync_staging_to_develop.sh"
START_TS="$(date +%s)"

SYNC_REPO="${SYNC_REPO:-/home/user/sora-bridge}"
SYNC_REMOTE="${SYNC_REMOTE:-origin}"
SYNC_BRANCH="${SYNC_BRANCH:-develop}"
STAGING_DEPLOY_SH="${STAGING_DEPLOY_SH:-$ROOT/scripts/staging_deploy.sh}"
STATE_DIR="${OMNISIGHT_STAGING_STATE_DIR:-/var/lib/omnisight/staging}"
ACTIVE_TAG_FILE="$STATE_DIR/active_tag"
ACTIVE_COLOR_FILE="$STATE_DIR/active_color"
STAGING_URL="${OMNISIGHT_STAGING_URL:-https://staging.sora.services}"
HEALTHZ_PATH="${OMNISIGHT_STAGING_HEALTHZ_PATH:-/health}"
COMPOSE_FILE="${OMNISIGHT_STAGING_COMPOSE_FILE:-$ROOT/deploy/staging/docker-compose.yml}"
ENV_FILE="${OMNISIGHT_STAGING_ENV_FILE:-$ROOT/deploy/staging/.env}"

DOCKER_BIN="${DOCKER_BIN:-docker}"
GIT_BIN="${GIT_BIN:-git}"
CURL_BIN="${CURL_BIN:-curl}"
PSQL_BIN="${PSQL_BIN:-psql}"

# ── logging / alerting ──────────────────────────────────────────────────────
log()   { printf '[%s] %s\n'          "$SELF" "$*" >&2; }
warn()  { printf '[%s] WARNING: %s\n' "$SELF" "$*" >&2; }

# alert(code, detail) — loud journal line + best-effort Slack webhook POST,
# mirroring scripts/staging_deploy.sh's alert(). The systemd OnFailure= chain
# (staging-gate-alert.service) covers the case where this script itself dies.
alert() {
	local code="$1" detail="$2"
	printf '[%s] ALERT %s: %s\n' "$SELF" "$code" "$detail" >&2
	if [[ -n "${OMNISIGHT_STAGING_ALERT_WEBHOOK:-}" ]]; then
		"$CURL_BIN" -fsS -X POST -H 'Content-Type: application/json' \
			--data "{\"source\":\"staging-sync\",\"error\":\"$code\",\"detail\":\"$detail\"}" \
			"$OMNISIGHT_STAGING_ALERT_WEBHOOK" >/dev/null 2>&1 || true
	fi
}

# ── release_audit row (best-effort; see the header caveat) ──────────────────
write_audit_row() {  # $1 = outcome name ; $2 = develop_tip ; $3 = previous_tag ; $4 = step
	local outcome="$1" tip="${2:-}" prev="${3:-}" step="${4:-}"
	local color duration
	color="$(cat "$ACTIVE_COLOR_FILE" 2>/dev/null | tr -d '[:space:]' || true)"
	duration=$(( $(date +%s) - START_TS ))
	local detail
	detail="$(python3 - "$outcome" "$tip" "$prev" "$color" "$step" "$duration" <<'PY'
import json, sys
outcome, tip, prev, color, step, duration = sys.argv[1:7]
def _int(x):
    try: return int(x)
    except Exception: return 0
print(json.dumps({
    "kind": "staging_sync",
    "outcome": outcome,
    "develop_tip": tip,
    "previous_tag": prev,
    "active_color": color,
    "step": step,
    "duration_s": _int(duration),
    "source": "scripts/sync_staging_to_develop.sh",
}, separators=(",", ":")))
PY
)"
	log "audit: outcome=$outcome detail=$detail"
	local dsn="${OMNISIGHT_DATABASE_URL:-}"
	if [[ -z "$dsn" ]]; then
		log "audit: OMNISIGHT_DATABASE_URL unset — release_audit row skipped (run log only)"
		return 0
	fi
	# SQLAlchemy URL -> libpq URL (strip a `+driver` suffix on the scheme).
	dsn="${dsn/+asyncpg/}"; dsn="${dsn/+psycopg2/}"; dsn="${dsn/+psycopg/}"; dsn="${dsn/+pg8000/}"
	local detail_sql="${detail//\'/\'\'}"   # SQL-escape single quotes (JSON has none here, but be safe)
	local tip_sql="${tip//\'/\'\'}"
	if "$PSQL_BIN" "$dsn" -v ON_ERROR_STOP=1 -qc \
		"INSERT INTO release_audit (outcome, fix_version, develop_sha, detail) VALUES ('noop', NULL, '$tip_sql', '$detail_sql')" \
		>/dev/null 2>&1
	then
		log "audit: release_audit row written (outcome=noop, detail.kind=staging_sync, detail.outcome=$outcome)"
	else
		warn "audit: release_audit INSERT failed — non-fatal (DSN unreachable or table absent). Run log retains the record."
	fi
}

# ── develop-tip resolution ──────────────────────────────────────────────────
resolve_develop_tip() {
	if [[ -n "${SYNC_DEVELOP_TIP:-}" ]]; then
		printf '%s\n' "$SYNC_DEVELOP_TIP"
		return 0
	fi
	[[ -d "$SYNC_REPO/.git" || -f "$SYNC_REPO/.git" ]] || { warn "DevelopTipFetchFailed: $SYNC_REPO is not a git checkout"; return 1; }
	if ! "$GIT_BIN" -C "$SYNC_REPO" fetch --quiet "$SYNC_REMOTE" "$SYNC_BRANCH" 2>/dev/null; then
		warn "DevelopTipFetchFailed: git fetch $SYNC_REMOTE $SYNC_BRANCH failed in $SYNC_REPO"
		return 1
	fi
	local sha
	sha="$("$GIT_BIN" -C "$SYNC_REPO" rev-parse --verify --quiet FETCH_HEAD 2>/dev/null || true)"
	[[ -n "$sha" ]] || { warn "DevelopTipFetchFailed: could not resolve FETCH_HEAD after fetch"; return 1; }
	printf '%s\n' "$sha"
}

active_tag() { cat "$ACTIVE_TAG_FILE" 2>/dev/null | tr -d '[:space:]' || true; }
active_color() { cat "$ACTIVE_COLOR_FILE" 2>/dev/null | tr -d '[:space:]' || echo "blue"; }

# ── deploy + migrate + health helpers ───────────────────────────────────────
deploy_tag() {  # $1 = image tag
	"$STAGING_DEPLOY_SH" --image-tag "$1"
}

run_migrations() {  # against the freshly-deployed staging stack
	if [[ "${SYNC_SKIP_MIGRATE:-}" == "1" ]]; then
		log "migrate: SYNC_SKIP_MIGRATE=1 — skipping alembic upgrade"
		return 0
	fi
	if [[ -n "${SYNC_MIGRATE_CMD:-}" ]]; then
		log "migrate: running override SYNC_MIGRATE_CMD"
		eval "$SYNC_MIGRATE_CMD"
		return $?
	fi
	local color project
	color="$(active_color)"
	project="omnisight-staging-$color"
	local -a cmd=("$DOCKER_BIN" compose -p "$project" -f "$COMPOSE_FILE")
	[[ -f "$ENV_FILE" ]] && cmd+=(--env-file "$ENV_FILE")
	cmd+=(run --rm --no-deps -w /app/backend backend-a python -m alembic upgrade heads)
	log "migrate: ${cmd[*]}"
	"${cmd[@]}"
}

healthz_ok() {
	"$CURL_BIN" -fsS --max-time 20 "${STAGING_URL%/}${HEALTHZ_PATH}" >/dev/null 2>&1
}

# ── main ────────────────────────────────────────────────────────────────────
main() {
	# Prerequisites.
	command -v "$GIT_BIN"    >/dev/null 2>&1 || { warn "git not found ($GIT_BIN)"; exit 3; }
	command -v "$CURL_BIN"   >/dev/null 2>&1 || { warn "curl not found ($CURL_BIN)"; exit 3; }
	command -v "$DOCKER_BIN" >/dev/null 2>&1 || { warn "docker not found ($DOCKER_BIN)"; exit 3; }
	command -v python3       >/dev/null 2>&1 || { warn "python3 not found"; exit 3; }
	[[ -x "$STAGING_DEPLOY_SH" || -f "$STAGING_DEPLOY_SH" ]] || { warn "staging deployer not found: $STAGING_DEPLOY_SH"; exit 3; }

	local tip
	if ! tip="$(resolve_develop_tip)"; then
		# DevelopTipFetchFailed — transient; keep current staging up, retry next tick.
		log "develop tip unresolved this tick; leaving staging untouched (will retry)"
		exit 0
	fi
	log "develop tip = $tip"

	local prev
	prev="$(active_tag)"
	if [[ -n "$prev" && "$prev" == "$tip" ]]; then
		log "staging already on develop tip ($tip) — idempotent no-op"
		exit 0
	fi
	log "staging needs sync: active=${prev:-<none>} -> develop=$tip"

	# 1. Deploy the develop-tip image. staging_deploy.sh reverts to the
	#    previous color/tag on its own /health failure, so a non-zero exit
	#    here means staging is back on $prev.
	if ! deploy_tag "$tip"; then
		alert StagingDeployFailed "staging_deploy.sh --image-tag $tip failed; staging remains on ${prev:-unknown}"
		write_audit_row "staging_sync_failed_revert" "$tip" "$prev" "deploy"
		exit 1
	fi

	# 2. Alembic migrations against the new active color.
	if ! run_migrations; then
		alert StagingMigrationFailed "alembic upgrade failed on develop tip $tip; rolling staging back to ${prev:-<none>}"
		if [[ -n "$prev" ]]; then
			deploy_tag "$prev" || alert StagingMigrationFailed "rollback to $prev ALSO failed — staging may be wedged; operator action required"
		else
			warn "no previous tag recorded — cannot roll back; staging is on $tip with a failed migration"
		fi
		write_audit_row "staging_sync_failed_revert" "$tip" "$prev" "migrate"
		exit 1
	fi

	# 3. Post-deploy health re-check (catches a migration that left the app red).
	if ! healthz_ok; then
		alert StagingHealthzFailed "post-deploy ${STAGING_URL%/}${HEALTHZ_PATH} not healthy on $tip; rolling staging back to ${prev:-<none>}"
		if [[ -n "$prev" ]]; then
			deploy_tag "$prev" || alert StagingHealthzFailed "rollback to $prev ALSO failed — staging may be wedged; operator action required"
		else
			warn "no previous tag recorded — cannot roll back; staging is on $tip and unhealthy"
		fi
		write_audit_row "staging_sync_failed_revert" "$tip" "$prev" "healthz"
		exit 1
	fi

	write_audit_row "staging_synced" "$tip" "$prev" "deploy"
	log "staging synced to develop tip $tip (color=$(active_color), url=$STAGING_URL)"
}

main "$@"
