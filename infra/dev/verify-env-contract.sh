#!/usr/bin/env bash
#
# [OP-1643] Boreas-B A2 — DEV env contract verifier (mirror of
# infra/staging/verify-env-contract.sh, inverted for dev).
#
# Wired as ExecStartPre of the dev compose systemd unit (A4 /
# deploy/systemd/omnisight-dev-compose.service) and safe to run by hand:
#   infra/dev/verify-env-contract.sh
#
# It is the belt-and-suspenders SHELL layer that complements the in-process
# Python guard (backend/env_contract.py). The Python guard fails closed at
# connection time; this guard refuses to even bring the dev stack up (exit 1,
# EnvContractViolation) if the dev .env points at PRODUCTION resources before a
# container starts. It also carries the live-credential checks codex asked to
# keep OUT of the narrow Python DB guard.
#
# Refuses to start (exit 1) if ANY of these hold:
#
#   1. The Postgres DSN does not look like DEV — its database-name component
#      does not end with `-dev` / `_dev`, its user is not `omnisight_dev`, and
#      it does not connect on the known dev port (58432). i.e. it looks like it
#      is pointed at prod/staging Postgres. A db named exactly `omnisight`
#      (prod) is an instant fail.
#   2. `OMNISIGHT_ENV=dev` (or environment=dev) is not exported.
#   3. A live external API key is present:
#        - Stripe     STRIPE_SECRET_KEY            an `sk_live_` key is an
#                     instant fail (dev must use sk_test_ or empty).
#        - Anthropic  ANTHROPIC_API_KEY /
#                     OMNISIGHT_ANTHROPIC_API_KEY  must be empty or a
#                     test/sandbox/eval key.
#        - Slack      SLACK_WEBHOOK_URL must be empty or contain "dev"/"staging".
#
# Inputs (all optional, env-overridable):
#   OMNISIGHT_DEV_ENV_FILE   default <repo>/deploy/dev/.env (also reads .env.local)
#
# Exit codes:  0 ok   1 EnvContractViolation

set -euo pipefail

SELF="verify-env-contract.sh(dev)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${OMNISIGHT_DEV_ENV_FILE:-$REPO_ROOT/deploy/dev/.env}"
DEV_PG_PORTS=(58432)

log()  { printf '[%s] %s\n' "$SELF" "$*" >&2; }
fail() { printf '[%s] EnvContractViolation: %s\n' "$SELF" "$*" >&2; exit 1; }

# ── Load env from the dev env file(s) into this shell ─────────────────
load_env_file() {
	local f="$1"
	[[ -f "$f" ]] || return 0
	log "loading env from $f"
	while IFS= read -r line || [[ -n "$line" ]]; do
		line="${line#"${line%%[![:space:]]*}"}"
		[[ -z "$line" || "$line" == \#* ]] && continue
		[[ "$line" == export\ * ]] && line="${line#export }"
		[[ "$line" == *=* ]] || continue
		local key="${line%%=*}" val="${line#*=}"
		key="${key%"${key##*[![:space:]]}"}"
		if [[ "$val" == \"*\" || "$val" == \'*\' ]]; then val="${val:1:${#val}-2}"; fi
		[[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
		if [[ -z "${!key+x}" ]]; then export "$key=$val"; fi
	done < "$f"
}

load_env_file "$ENV_FILE"
load_env_file "${ENV_FILE}.local"

# ── 1. Postgres DSN must look like dev (and never like prod) ──────────
dsn_db_name() { local d="${1##*/}"; printf '%s' "${d%%\?*}"; }
dsn_user()    { local r="${1#*://}"; r="${r%%@*}"; printf '%s' "${r%%:*}"; }

dsn_is_dev() {
	local dsn="$1"
	[[ -n "$dsn" ]] || return 1
	local db user; db="$(dsn_db_name "$dsn")"; user="$(dsn_user "$dsn")"
	# Hard reject: the exact prod db name.
	[[ "$db" == "omnisight" && "$user" == "omnisight" ]] && return 1
	case "$db" in *-dev|*_dev) return 0 ;; esac
	[[ "$user" == "omnisight_dev" ]] && return 0
	local p
	for p in "${DEV_PG_PORTS[@]}"; do
		[[ "$dsn" == *":$p/"* || "$dsn" == *":$p?"* || "$dsn" == *":$p" ]] && return 0
	done
	return 1
}

DSN="${OMNISIGHT_DATABASE_URL:-${DATABASE_URL:-}}"
if [[ -z "$DSN" && -n "${POSTGRES_DB:-}" ]]; then
	DSN="postgresql://${POSTGRES_USER:-omnisight_dev}@${POSTGRES_HOST:-postgres}:${DEV_POSTGRES_PORT:-58432}/${POSTGRES_DB}"
fi
[[ -n "$DSN" ]] || fail "no Postgres DSN found (set OMNISIGHT_DATABASE_URL or POSTGRES_DB in $ENV_FILE)"
if ! dsn_is_dev "$DSN"; then
	redacted="${DSN/\/\/*@/\/\/***@}"
	fail "Postgres DSN does not look like dev — database name must end with -dev/_dev or user be omnisight_dev or port 58432; refusing to start dev against what looks like prod/staging Postgres: $redacted"
fi
log "ok: Postgres DSN looks like dev"

# ── 2. OMNISIGHT_ENV=dev must be exported ─────────────────────────────
ENV_MARKER="${OMNISIGHT_ENV:-${environment:-}}"
case "$ENV_MARKER" in
	dev|develop|development) log "ok: OMNISIGHT_ENV=$ENV_MARKER" ;;
	*) fail "OMNISIGHT_ENV=dev is not exported (got OMNISIGHT_ENV='${OMNISIGHT_ENV:-}')" ;;
esac

# ── 3. No live external API keys in dev ───────────────────────────────
contains_ci() { local hay="${1,,}" needle="${2,,}"; [[ "$hay" == *"$needle"* ]]; }

if [[ -n "${STRIPE_SECRET_KEY:-}" ]]; then
	[[ "$STRIPE_SECRET_KEY" == sk_live_* ]] && fail "STRIPE_SECRET_KEY is a LIVE key (sk_live_) — dev must use sk_test_ or empty"
	log "ok: Stripe key is not a live key"
fi
for var in ANTHROPIC_API_KEY OMNISIGHT_ANTHROPIC_API_KEY; do
	val="${!var:-}"
	[[ -n "$val" ]] || continue
	if contains_ci "$val" "test" || contains_ci "$val" "sandbox" || contains_ci "$val" "eval"; then
		log "ok: $var looks like a non-prod Anthropic key"
	else
		fail "$var does not look like a test/sandbox Anthropic key — dev must not carry a production Anthropic key"
	fi
done
for var in SLACK_WEBHOOK_URL OMNISIGHT_DEV_ALERT_WEBHOOK SLACK_ALERT_WEBHOOK; do
	val="${!var:-}"
	[[ -n "$val" ]] || continue
	if contains_ci "$val" "dev" || contains_ci "$val" "staging"; then
		log "ok: $var is a non-prod Slack webhook"
	else
		fail "$var is not a dev Slack webhook (URL must contain 'dev' or 'staging')"
	fi
done

log "env contract OK — dev may start"
exit 0
