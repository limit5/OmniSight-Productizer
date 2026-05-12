#!/usr/bin/env bash
#
# [OP-971] AUDIT-19a — staging env contract verifier.
#
# Runs as ExecStartPre of deploy/systemd/omnisight-staging-compose.service
# (and is safe to run by hand: `infra/staging/verify-env-contract.sh`).
# It is the guard that stops a fat-fingered staging .env / .env.local from
# bringing the staging stack up against PRODUCTION resources on the
# co-tenanted 5a host.
#
# It refuses to start the stack (exit 1, error code EnvContractViolation)
# if ANY of these hold:
#
#   1. The Postgres DSN does not look like staging — its database-name
#      component does not end with `-staging` / `_staging` AND it does not
#      connect on a known staging port (55432 / 55433). i.e. it looks like
#      it is pointed at prod Postgres.
#   2. An external API key is missing its sandbox prefix:
#        - Stripe         STRIPE_SECRET_KEY            must start `sk_test_`
#                         (a `sk_live_` key is an instant fail)
#        - Anthropic      ANTHROPIC_API_KEY /
#                         OMNISIGHT_ANTHROPIC_API_KEY  must be empty or a
#                         test/sandbox/eval key (name contains test|sandbox|eval)
#        - Slack          SLACK_WEBHOOK_URL /
#                         OMNISIGHT_STAGING_ALERT_WEBHOOK
#                         must be empty or a staging webhook (URL contains
#                         "staging")
#   3. `environment=staging` is not exported (the lowercase var the ticket
#      pins; we also accept OMNISIGHT_ENV=staging as the canonical alias).
#
# Two more checks emit a *warning* by default but do not block — the
# systemd unit's `up -d --wait` surfaces a real port collision anyway,
# and the cgroup limits failing to apply is logged by the user manager:
#
#   - PortCollisionWithProd — a configured staging port is already bound.
#     Override the port in deploy/staging/.env / .env.local. Set
#     OMNISIGHT_STAGING_STRICT_PORTS=1 to make this a hard fail (exit 2).
#   - CgroupV1Fallback — the kernel exposes only cgroup v1, so the unit's
#     MemoryMax=30% / CPUQuota=30% percentage limits will not take effect.
#     Set OMNISIGHT_STAGING_REQUIRE_CGROUP_V2=1 to make this a hard fail,
#     or follow the runbook to switch the unit to absolute byte limits.
#
# Inputs (all optional, env-overridable):
#   OMNISIGHT_STAGING_ENV_FILE   default /home/user/sora-bridge/deploy/staging/.env
#                                also reads a sibling `.env.local` if present
#   OMNISIGHT_STAGING_HEALTHZ_PORT          default 18080
#   OMNISIGHT_STAGING_EXTRA_PORTS           space-separated extra ports to check
#   OMNISIGHT_STAGING_STRICT_PORTS          "1" => PortCollisionWithProd is fatal
#   OMNISIGHT_STAGING_REQUIRE_CGROUP_V2     "1" => CgroupV1Fallback is fatal
#
# Exit codes:  0 ok   1 EnvContractViolation
#              2 PortCollisionWithProd (only when STRICT_PORTS=1)
#              3 CgroupV1Fallback      (only when REQUIRE_CGROUP_V2=1)

set -euo pipefail

SELF="verify-env-contract.sh"
ENV_FILE="${OMNISIGHT_STAGING_ENV_FILE:-/home/user/sora-bridge/deploy/staging/.env}"
HEALTHZ_PORT="${OMNISIGHT_STAGING_HEALTHZ_PORT:-18080}"
STAGING_PG_PORTS=(55432 55433)

log()  { printf '[%s] %s\n' "$SELF" "$*" >&2; }
warn() { printf '[%s] WARNING: %s\n' "$SELF" "$*" >&2; }
fail() {
	# $1 = error code (EnvContractViolation|PortCollisionWithProd|CgroupV1Fallback)
	# $2 = human detail ; $3 = exit status
	printf '[%s] %s: %s\n' "$SELF" "$1" "$2" >&2
	exit "${3:-1}"
}

# ── Load env from the staging env file(s) into this shell ─────────────
# We deliberately read the file rather than trusting only the inherited
# environment: docker compose reads `deploy/staging/.env` directly, so the
# contract has to inspect the same source of truth. `.env.local` (if any)
# layers on top, matching compose's --env-file precedence convention.
load_env_file() {
	local f="$1"
	[[ -f "$f" ]] || return 0
	log "loading env from $f"
	# Only accept simple KEY=VALUE lines; ignore comments / blanks / exports.
	while IFS= read -r line || [[ -n "$line" ]]; do
		line="${line#"${line%%[![:space:]]*}"}"          # ltrim
		[[ -z "$line" || "$line" == \#* ]] && continue
		[[ "$line" == export\ * ]] && line="${line#export }"
		[[ "$line" == *=* ]] || continue
		local key="${line%%=*}" val="${line#*=}"
		key="${key%"${key##*[![:space:]]}"}"             # rtrim key
		# strip a single layer of matching surrounding quotes
		if [[ "$val" == \"*\" || "$val" == \'*\' ]]; then val="${val:1:${#val}-2}"; fi
		[[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
		# Inherited environment wins over the file (systemd EnvironmentFile,
		# operator `KEY=v ./verify-env-contract.sh`), so only set if unset.
		if [[ -z "${!key+x}" ]]; then export "$key=$val"; fi
	done < "$f"
}

load_env_file "$ENV_FILE"
load_env_file "${ENV_FILE}.local"
load_env_file "$(dirname "$ENV_FILE")/.env.local"

# ── 1. Postgres DSN must look like staging ────────────────────────────
# Accept the full SQLAlchemy URL (OMNISIGHT_DATABASE_URL / DATABASE_URL)
# or the decomposed POSTGRES_DB + STAGING_POSTGRES_PORT used by the compose
# file's defaults.
dsn_is_staging() {
	local dsn="$1"
	[[ -n "$dsn" ]] || return 1
	# database name = last path segment, minus any ?query
	local dbpart="${dsn##*/}"; dbpart="${dbpart%%\?*}"
	case "$dbpart" in
		*-staging|*_staging|*-staging-*|staging_*|staging-*) return 0 ;;
	esac
	# or: connects on a known staging Postgres port
	local p
	for p in "${STAGING_PG_PORTS[@]}"; do
		[[ "$dsn" == *":$p/"* || "$dsn" == *":$p?"* || "$dsn" == *":$p" ]] && return 0
	done
	return 1
}

DSN="${OMNISIGHT_DATABASE_URL:-${DATABASE_URL:-}}"
if [[ -z "$DSN" && -n "${POSTGRES_DB:-}" ]]; then
	# Reconstruct just enough of a DSN to validate the db-name + port.
	DSN="postgresql://x@${POSTGRES_HOST:-postgres}:${STAGING_POSTGRES_PORT:-5432}/${POSTGRES_DB}"
fi
if [[ -z "$DSN" ]]; then
	fail EnvContractViolation \
		"no Postgres DSN found (set OMNISIGHT_DATABASE_URL or POSTGRES_DB in $ENV_FILE)" 1
fi
if ! dsn_is_staging "$DSN"; then
	# Redact credentials before echoing the DSN back.
	redacted="${DSN/\/\/*@/\/\/***@}"
	fail EnvContractViolation \
		"Postgres DSN does not look like staging — database name must end with -staging/_staging or use a staging port (${STAGING_PG_PORTS[*]}); refusing to start staging against what looks like prod Postgres: $redacted" 1
fi
log "ok: Postgres DSN looks like staging"

# ── 2. External API keys must carry their sandbox prefixes ────────────
key_starts_with() { [[ "$1" == "$2"* ]]; }
contains_ci() {
	# case-insensitive substring
	local hay="${1,,}" needle="${2,,}"
	[[ "$hay" == *"$needle"* ]]
}

# Stripe — sk_test_ required; sk_live_ is an instant fail.
if [[ -n "${STRIPE_SECRET_KEY:-}" ]]; then
	if key_starts_with "$STRIPE_SECRET_KEY" "sk_live_"; then
		fail EnvContractViolation "STRIPE_SECRET_KEY is a LIVE key (sk_live_) — staging must use sk_test_" 1
	fi
	if ! key_starts_with "$STRIPE_SECRET_KEY" "sk_test_"; then
		fail EnvContractViolation "STRIPE_SECRET_KEY missing sandbox prefix — must start with sk_test_" 1
	fi
	log "ok: Stripe key is a test key"
fi

# Anthropic — empty, or a key whose name marks it test/sandbox/eval.
for var in ANTHROPIC_API_KEY OMNISIGHT_ANTHROPIC_API_KEY; do
	val="${!var:-}"
	[[ -n "$val" ]] || continue
	if contains_ci "$val" "test" || contains_ci "$val" "sandbox" || contains_ci "$val" "eval"; then
		log "ok: $var looks like a non-prod Anthropic key"
	else
		fail EnvContractViolation "$var does not look like a test/sandbox Anthropic key — staging must not use a production Anthropic key" 1
	fi
done

# Slack — empty, or a webhook URL that names staging.
for var in SLACK_WEBHOOK_URL OMNISIGHT_STAGING_ALERT_WEBHOOK SLACK_ALERT_WEBHOOK; do
	val="${!var:-}"
	[[ -n "$val" ]] || continue
	if contains_ci "$val" "staging"; then
		log "ok: $var is a staging Slack webhook"
	else
		fail EnvContractViolation "$var is not a staging Slack webhook (URL must contain 'staging')" 1
	fi
done

# ── 3. environment=staging must be exported ───────────────────────────
ENV_MARKER="${environment:-${OMNISIGHT_ENV:-}}"
if [[ "$ENV_MARKER" != "staging" ]]; then
	fail EnvContractViolation \
		"environment=staging is not exported (got environment='${environment:-}', OMNISIGHT_ENV='${OMNISIGHT_ENV:-}')" 1
fi
log "ok: environment=staging is set"

# ── 4. (warn) Port collisions with whatever else runs on the box ──────
port_in_use() {
	local p="$1"
	if command -v ss >/dev/null 2>&1; then
		ss -ltnH "( sport = :$p )" 2>/dev/null | grep -q . && return 0 || return 1
	fi
	# Fallback: bash /dev/tcp probe to localhost.
	( exec 3<>"/dev/tcp/127.0.0.1/$p" ) >/dev/null 2>&1 && { exec 3>&- 3<&- 2>/dev/null || true; return 0; }
	return 1
}
CHECK_PORTS=("$HEALTHZ_PORT" "${STAGING_HTTP_PORT:-18080}" "${STAGING_BACKEND_A_PORT:-8010}" \
	"${STAGING_BACKEND_B_PORT:-8011}" "${STAGING_POSTGRES_PORT:-55432}" ${OMNISIGHT_STAGING_EXTRA_PORTS:-})
collision_ports=()
for p in "${CHECK_PORTS[@]}"; do
	[[ "$p" =~ ^[0-9]+$ ]] || continue
	if port_in_use "$p"; then collision_ports+=("$p"); fi
done
if [[ "${#collision_ports[@]}" -gt 0 ]]; then
	detail="port(s) already bound: ${collision_ports[*]} — override the matching STAGING_*_PORT in $ENV_FILE / .env.local"
	if [[ "${OMNISIGHT_STAGING_STRICT_PORTS:-0}" == "1" ]]; then
		fail PortCollisionWithProd "$detail" 2
	fi
	warn "PortCollisionWithProd: $detail (will surface again as a bind error in \`up -d --wait\` if real)"
else
	log "ok: no obvious staging port collisions"
fi

# ── 5. (warn / opt-in fatal) cgroup v2 needed for percentage limits ───
if [[ -e /sys/fs/cgroup/cgroup.controllers ]]; then
	log "ok: cgroup v2 unified hierarchy present — MemoryMax=30%/CPUQuota=30% will apply"
else
	if [[ "${OMNISIGHT_STAGING_REQUIRE_CGROUP_V2:-0}" == "1" ]]; then
		fail CgroupV1Fallback \
			"kernel exposes only cgroup v1 — the unit's MemoryMax=30%/CPUQuota=30% would silently no-op; set absolute byte limits per the runbook or unset OMNISIGHT_STAGING_REQUIRE_CGROUP_V2" 3
	fi
	warn "CgroupV1Fallback: cgroup v2 not detected — omnisight-staging-compose.service percentage limits will NOT take effect; see docs/operations/staging-environment-runbook.md (CgroupV1Fallback)"
fi

log "env contract OK — staging may start"
exit 0
