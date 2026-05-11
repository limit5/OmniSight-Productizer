#!/usr/bin/env bash
#
# OP-878 D6 - staging blue-green auto-deploy from Gerrit change-merged.
#
# Intended cron/webhook usage:
#   scripts/staging_deploy.sh --event-file /var/spool/omnisight/gerrit-last.json
#
# The script is idempotent for an already-active tag. It starts the
# standby color under a separate Compose project, waits for /health, then
# atomically switches the host Caddy ingress snippet and drains the old
# stack.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
COMPOSE_FILE="${OMNISIGHT_STAGING_COMPOSE_FILE:-$ROOT/deploy/staging/docker-compose.yml}"
ENV_FILE="${OMNISIGHT_STAGING_ENV_FILE:-$ROOT/deploy/staging/.env}"
STATE_DIR="${OMNISIGHT_STAGING_STATE_DIR:-/var/lib/omnisight/staging}"
ACTIVE_COLOR_FILE="$STATE_DIR/active_color"
ACTIVE_TAG_FILE="$STATE_DIR/active_tag"
ACTIVE_UPSTREAM_FILE="${OMNISIGHT_STAGING_ACTIVE_UPSTREAM:-$STATE_DIR/active-upstream.caddy}"
STAGING_URL="${OMNISIGHT_STAGING_URL:-https://staging.sora.services}"
HEALTH_INTERVAL_SECONDS="${OMNISIGHT_STAGING_HEALTH_INTERVAL_SECONDS:-30}"
HEALTH_FAIL_SECONDS="${OMNISIGHT_STAGING_HEALTH_FAIL_SECONDS:-180}"
OLD_DRAIN_SECONDS="${OMNISIGHT_STAGING_DRAIN_SECONDS:-60}"
CADDY_VALIDATE_CMD="${OMNISIGHT_STAGING_CADDY_VALIDATE_CMD:-caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile}"
CADDY_RELOAD_CMD="${OMNISIGHT_STAGING_CADDY_RELOAD_CMD:-caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile}"

usage() {
	cat >&2 <<EOF
usage: $0 [--event-file path | --image-tag tag]

Consumes a Gerrit change-merged webhook payload for main, pulls the D2
image tag, starts the standby staging stack, switches Caddy ingress, and
drains the old stack.
EOF
	exit 1
}

log() { printf '[staging-deploy] %s\n' "$*"; }
die() { echo "error: $1" >&2; exit "${2:-1}"; }

alert() {
	local code="$1"
	local detail="$2"
	log "ALERT $code: $detail"
	if [[ -n "${OMNISIGHT_STAGING_ALERT_WEBHOOK:-}" ]]; then
		curl -fsS -X POST -H 'Content-Type: application/json' \
			--data "{\"error\":\"$code\",\"detail\":\"$detail\"}" \
			"$OMNISIGHT_STAGING_ALERT_WEBHOOK" >/dev/null || true
	fi
}

other_color() {
	case "$1" in
		blue) echo "green" ;;
		green) echo "blue" ;;
		*) echo "blue" ;;
	esac
}

read_active_color() {
	if [[ -f "$ACTIVE_COLOR_FILE" ]]; then
		tr -d '[:space:]' < "$ACTIVE_COLOR_FILE"
	else
		echo "blue"
	fi
}

port_for() {
	local color="$1"
	local service="$2"
	case "$color:$service" in
		blue:http) echo 18080 ;;
		blue:https) echo 18443 ;;
		blue:postgres) echo 55432 ;;
		blue:backend_a) echo 18010 ;;
		blue:backend_b) echo 18011 ;;
		blue:frontend) echo 13010 ;;
		green:http) echo 28080 ;;
		green:https) echo 28443 ;;
		green:postgres) echo 55433 ;;
		green:backend_a) echo 28010 ;;
		green:backend_b) echo 28011 ;;
		green:frontend) echo 23010 ;;
		*) die "unknown color/service $color:$service" ;;
	esac
}

compose_project() {
	echo "omnisight-staging-$1"
}

compose_cmd() {
	local color="$1"
	local cmd=(docker compose -p "$(compose_project "$color")" -f "$COMPOSE_FILE")
	if [[ -f "$ENV_FILE" ]]; then
		cmd+=(--env-file "$ENV_FILE")
	fi
	printf '%q ' "${cmd[@]}"
}

compose_env() {
	local color="$1"
	export STAGING_HTTP_PORT
	export STAGING_HTTPS_PORT
	export STAGING_POSTGRES_PORT
	export STAGING_BACKEND_A_PORT
	export STAGING_BACKEND_B_PORT
	export STAGING_FRONTEND_PORT
	STAGING_HTTP_PORT=$(port_for "$color" http)
	STAGING_HTTPS_PORT=$(port_for "$color" https)
	STAGING_POSTGRES_PORT=$(port_for "$color" postgres)
	STAGING_BACKEND_A_PORT=$(port_for "$color" backend_a)
	STAGING_BACKEND_B_PORT=$(port_for "$color" backend_b)
	STAGING_FRONTEND_PORT=$(port_for "$color" frontend)
}

image_tag_from_event() {
	local event_file="$1"
	local tmp_event=""
	if [[ "$event_file" == "-" ]]; then
		tmp_event=$(mktemp)
		cat > "$tmp_event"
		event_file="$tmp_event"
	fi
	set +e
	python3 - "$event_file" <<'PY'
import json, sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    payload = json.load(fh)

event_type = str(payload.get("type") or payload.get("event") or "")
if event_type and event_type != "change-merged":
    raise SystemExit(2)

branch = str(payload.get("branch") or payload.get("refName") or payload.get("ref") or "")
if branch not in ("main", "refs/heads/main", "master", "refs/heads/master"):
    raise SystemExit(2)

for key in ("image_tag", "imageTag", "newRev", "revision", "commit", "submitRevision"):
    value = str(payload.get(key) or "").strip()
    if value:
        print(value)
        raise SystemExit(0)

change = payload.get("change") or {}
current = payload.get("patchSet") or payload.get("currentPatchSet") or {}
for obj in (change, current):
    for key in ("commit", "revision", "current_revision", "newRev"):
        value = str(obj.get(key) or "").strip()
        if value:
            print(value)
            raise SystemExit(0)

raise SystemExit("change-merged event missing image tag/revision")
PY
	local rc=$?
	set -e
	if [[ -n "$tmp_event" ]]; then
		rm -f "$tmp_event"
	fi
	return "$rc"
}

wait_for_color_health() {
	local color="$1"
	local started
	started=$(date +%s)
	local deadline=$((started + HEALTH_FAIL_SECONDS))
	local http_port backend_a backend_b frontend_port
	http_port=$(port_for "$color" http)
	backend_a=$(port_for "$color" backend_a)
	backend_b=$(port_for "$color" backend_b)
	frontend_port=$(port_for "$color" frontend)

	while true; do
		if curl -fsS "http://127.0.0.1:$http_port/health" >/dev/null \
			&& curl -fsS "http://127.0.0.1:$backend_a/health" >/dev/null \
			&& curl -fsS "http://127.0.0.1:$backend_b/health" >/dev/null \
			&& curl -fsS "http://127.0.0.1:$frontend_port/" >/dev/null; then
			return 0
		fi
		if (( $(date +%s) >= deadline )); then
			return 1
		fi
		sleep "$HEALTH_INTERVAL_SECONDS"
	done
}

wait_for_public_health() {
	local deadline=$(( $(date +%s) + HEALTH_FAIL_SECONDS ))
	while true; do
		if curl -fsS "${STAGING_URL%/}/health" >/dev/null; then
			return 0
		fi
		if (( $(date +%s) >= deadline )); then
			return 1
		fi
		sleep "$HEALTH_INTERVAL_SECONDS"
	done
}

write_ingress_atomic() {
	local color="$1"
	local port
	port=$(port_for "$color" http)
	mkdir -p "$(dirname "$ACTIVE_UPSTREAM_FILE")"
	local tmp="${ACTIVE_UPSTREAM_FILE}.tmp.$$"
	printf 'reverse_proxy 127.0.0.1:%s\n' "$port" > "$tmp"
	mv -f "$tmp" "$ACTIVE_UPSTREAM_FILE"
}

run_caddy_cmd() {
	local raw="$1"
	bash -lc "$raw"
}

switch_ingress() {
	local new_color="$1"
	local old_color="$2"
	write_ingress_atomic "$new_color"
	if ! run_caddy_cmd "$CADDY_VALIDATE_CMD"; then
		write_ingress_atomic "$old_color"
		alert StagingIngressSwitchFailed "Caddy validation failed after writing $new_color upstream"
		return 1
	fi
	if ! run_caddy_cmd "$CADDY_RELOAD_CMD"; then
		write_ingress_atomic "$old_color"
		run_caddy_cmd "$CADDY_RELOAD_CMD" || true
		alert StagingIngressSwitchFailed "Caddy reload failed for $new_color upstream"
		return 1
	fi
}

deploy_color() {
	local color="$1"
	local image_tag="$2"
	compose_env "$color"
	export OMNISIGHT_IMAGE_TAG="$image_tag"

	local compose
	compose=$(compose_cmd "$color")
	if ! eval "$compose pull"; then
		alert StagingImagePullFailed "image tag $image_tag could not be pulled for $color"
		return 1
	fi
	eval "$compose up -d"
}

drain_color() {
	local color="$1"
	compose_env "$color"
	local compose
	compose=$(compose_cmd "$color")
	eval "$compose stop -t '$OLD_DRAIN_SECONDS'" || true
}

main() {
	local event_file=""
	local image_tag=""
	while [[ $# -gt 0 ]]; do
		case "$1" in
			--event-file) event_file="${2:-}"; shift 2 ;;
			--image-tag) image_tag="${2:-}"; shift 2 ;;
			-h|--help) usage ;;
			*) usage ;;
		esac
	done
	if [[ -z "$image_tag" ]]; then
		[[ -n "$event_file" ]] || event_file="-"
		if ! image_tag=$(image_tag_from_event "$event_file"); then
			log "ignored non-main or non-change-merged event"
			return 0
		fi
	fi

	mkdir -p "$STATE_DIR"
	local active_color standby_color active_tag
	active_color=$(read_active_color)
	standby_color=$(other_color "$active_color")
	active_tag="$(cat "$ACTIVE_TAG_FILE" 2>/dev/null || true)"
	if [[ "$active_tag" == "$image_tag" ]]; then
		log "tag $image_tag already active on $active_color"
		return 0
	fi

	log "deploying tag=$image_tag standby=$standby_color active=$active_color"
	if ! deploy_color "$standby_color" "$image_tag"; then
		return 1
	fi
	if ! wait_for_color_health "$standby_color"; then
		alert StagingHealthFlatlined "standby $standby_color failed /health for ${HEALTH_FAIL_SECONDS}s; active remains $active_color tag=${active_tag:-unknown}"
		drain_color "$standby_color"
		return 1
	fi
	if ! switch_ingress "$standby_color" "$active_color"; then
		return 1
	fi
	if ! wait_for_public_health; then
		alert StagingHealthFlatlined "public /health failed after switching to $standby_color; rolling back to $active_color tag=${active_tag:-unknown}"
		switch_ingress "$active_color" "$standby_color" || true
		drain_color "$standby_color"
		return 1
	fi

	printf '%s\n' "$standby_color" > "$ACTIVE_COLOR_FILE.tmp.$$"
	mv -f "$ACTIVE_COLOR_FILE.tmp.$$" "$ACTIVE_COLOR_FILE"
	printf '%s\n' "$image_tag" > "$ACTIVE_TAG_FILE.tmp.$$"
	mv -f "$ACTIVE_TAG_FILE.tmp.$$" "$ACTIVE_TAG_FILE"
	drain_color "$active_color"
	log "staging active color=$standby_color tag=$image_tag url=$STAGING_URL"
}

main "$@"
