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
#
# [OP-1574] RT-05b — deploy candidate BY DIGEST. The staging compose stays
# tag-based with pull_policy: always (RT-05a owns the compose). After
# `docker compose pull`, this script verifies that the digest actually
# pulled for backend + frontend equals the digest recorded in the candidate
# bundle (`--bundle path | $OMNISIGHT_CANDIDATE_BUNDLE`, the OP-1513
# bundle.json shape: `images.{backend,frontend}.digest`). A mismatch — the
# alias was retagged under us between candidate certification and this
# deploy — is REJECTED before the standby stack is brought up, so staging
# never serves an image that diverges from the certified candidate. When no
# bundle is supplied (e.g. the legacy change-merged webhook path) the check
# is skipped with a log line and behaviour is unchanged. Per RT-21 the
# release train tracks the backend+frontend pair only; bridge reuses the
# backend image and is not a separate runtime digest.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
COMPOSE_FILE="${OMNISIGHT_STAGING_COMPOSE_FILE:-$ROOT/deploy/staging/docker-compose.yml}"
ENV_FILE="${OMNISIGHT_STAGING_ENV_FILE:-$ROOT/deploy/staging/.env}"
# Registry default mirrors deploy/staging/docker-compose.yml's image refs so
# the digest verification inspects exactly what compose pulled.
REGISTRY="${OMNISIGHT_REGISTRY:-sora.services:49154/omnisight/OmniSight-Productizer}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
# Candidate bundle (OP-1513 bundle.json) used for post-pull digest equality;
# overridable via --bundle. Empty => digest verification skipped (legacy path).
CANDIDATE_BUNDLE="${OMNISIGHT_CANDIDATE_BUNDLE:-}"
STATE_DIR="${OMNISIGHT_STAGING_STATE_DIR:-/var/lib/omnisight/staging}"
# [OP-1606] RT-08 deploy-overlay lock writer + per-color host dir mounted at
# /etc/omnisight in the backend containers (see deploy/staging/docker-compose.yml).
OVERLAY_LOCK_WRITER="${OMNISIGHT_OVERLAY_LOCK_WRITER:-$ROOT/scripts/write_deploy_overlay_lock.py}"
OVERLAY_STATE_DIR="${OMNISIGHT_STAGING_OVERLAY_DIR:-$STATE_DIR/overlay}"
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
usage: $0 [--event-file path | --image-tag tag] [--bundle path]

Consumes a Gerrit change-merged webhook payload for main, pulls the D2
image tag, starts the standby staging stack, switches Caddy ingress, and
drains the old stack.

  --bundle path   candidate bundle.json (images.{backend,frontend}.digest).
                  After pulling, the digest actually pulled for backend +
                  frontend must equal the bundle's; a mismatch is rejected
                  before the standby stack starts (RT-05b deploy-by-digest).
                  Defaults to \$OMNISIGHT_CANDIDATE_BUNDLE; empty => skipped.
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
	local cmd=("$DOCKER_BIN" compose -p "$(compose_project "$color")" -f "$COMPOSE_FILE")
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

# ── post-pull digest equality (RT-05b) ──────────────────────────────────────
# bundle_image_digest <bundle.json> <backend|frontend> -> prints sha256:<...>
# (empty if the key is absent). Exits 3 only when the file can't be read/parsed,
# so the caller can tell "no digest recorded" from "bundle unreadable".
bundle_image_digest() {
	python3 - "$1" "$2" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
except Exception as exc:  # noqa: BLE001 — surface any read/parse error as exit 3
    sys.stderr.write(f"candidate bundle unreadable ({path}): {exc}\n")
    raise SystemExit(3)
images = (doc or {}).get("images") or {}
entry = images.get(key) or {}
print(str(entry.get("digest") or "").strip())
PY
}

# verify_image_digest <name> <image_ref> <expected_digest>
# Inspects the locally-pulled image's RepoDigests and asserts the candidate
# bundle's digest is among them. Returns non-zero on any mismatch / failure.
verify_image_digest() {
	local name="$1" ref="$2" expected="$3"
	local repo_digests
	if ! repo_digests="$("$DOCKER_BIN" image inspect "$ref" --format '{{json .RepoDigests}}' 2>&1)"; then
		alert StagingDigestInspectFailed "cannot inspect pulled image $ref for $name: ${repo_digests:-<no output>}"
		return 1
	fi
	if python3 - "$name" "$ref" "$expected" "$repo_digests" <<'PY'
import json, re, sys
name, ref, expected, raw = sys.argv[1:5]
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
if not DIGEST.match(expected):
    sys.stderr.write(f"{name}: candidate bundle digest malformed: {expected!r}\n")
    raise SystemExit(2)
raw = (raw or "").strip()
try:
    repo_digests = json.loads(raw) if raw and raw != "null" else []
except json.JSONDecodeError as exc:
    sys.stderr.write(f"{name}: RepoDigests not JSON ({exc}): {raw!r}\n")
    raise SystemExit(2)
observed = {str(rd).split("@", 1)[1] for rd in (repo_digests or []) if "@" in str(rd)}
if expected in observed:
    print(f"{name}: pulled digest matches candidate bundle ({expected})")
    raise SystemExit(0)
sys.stderr.write(
    f"{name}: pulled image {ref} resolves to "
    f"{sorted(observed) or '<no repo digests>'} but candidate bundle pins "
    f"{expected}\n"
)
raise SystemExit(1)
PY
	then
		return 0
	fi
	return 1
}

# verify_pulled_digests <image_tag> — checks backend + frontend pulled digests
# against $CANDIDATE_BUNDLE. No bundle => skipped (legacy path). Any mismatch,
# missing/incomplete bundle, or inspect failure => non-zero (deploy rejected).
verify_pulled_digests() {
	local image_tag="$1"
	if [[ -z "$CANDIDATE_BUNDLE" ]]; then
		log "digest equality: no candidate bundle supplied — skipping post-pull digest verification for $image_tag"
		return 0
	fi
	if [[ ! -f "$CANDIDATE_BUNDLE" ]]; then
		alert StagingDigestBundleMissing "candidate bundle not found: $CANDIDATE_BUNDLE"
		return 1
	fi
	local backend_digest frontend_digest
	backend_digest="$(bundle_image_digest "$CANDIDATE_BUNDLE" backend)" \
		|| { alert StagingDigestBundleUnreadable "could not read candidate bundle $CANDIDATE_BUNDLE"; return 1; }
	frontend_digest="$(bundle_image_digest "$CANDIDATE_BUNDLE" frontend)" \
		|| { alert StagingDigestBundleUnreadable "could not read candidate bundle $CANDIDATE_BUNDLE"; return 1; }
	if [[ -z "$backend_digest" || -z "$frontend_digest" ]]; then
		alert StagingDigestBundleIncomplete "candidate bundle $CANDIDATE_BUNDLE missing a digest (backend='${backend_digest:-}' frontend='${frontend_digest:-}')"
		return 1
	fi
	log "digest equality: verifying backend+frontend pulled digests against candidate bundle $CANDIDATE_BUNDLE"
	verify_image_digest backend  "$REGISTRY/backend:$image_tag"  "$backend_digest"  || return 1
	verify_image_digest frontend "$REGISTRY/frontend:$image_tag" "$frontend_digest" || return 1
	log "digest equality: backend+frontend pulled digests == candidate bundle"
}

# ── RT-08 deploy-overlay lock (OP-1606) ─────────────────────────────────────
# Write the deployment-identity lock for $color from the certified candidate
# bundle into a per-color host dir, and export OMNISIGHT_DEPLOY_OVERLAY_DIR so
# the compose mounts THAT dir at /etc/omnisight in the backend containers. The
# backend reads /etc/omnisight/deploy-overlay.lock once at startup, serves the
# six identity fields on /api/version, and gates /readyz on them when
# OMNISIGHT_REQUIRE_DEPLOY_OVERLAY=1 (staging .env). Per-color dirs keep the
# standby's identity separate from the still-serving active color during a
# blue-green switch. No candidate bundle => skip (the legacy change-merged
# webhook path); the overlay then stays observational, matching the digest-
# verification skip above.
write_overlay_lock() {
	local color="$1" image_tag="$2"
	local dir="${OVERLAY_STATE_DIR}-${color}"
	if [[ -z "$CANDIDATE_BUNDLE" ]]; then
		log "deploy-overlay: no candidate bundle — skipping RT-08 lock write for $color (overlay stays observational)"
		return 0
	fi
	mkdir -p "$dir"
	if ! python3 "$OVERLAY_LOCK_WRITER" \
		--bundle "$CANDIDATE_BUNDLE" \
		--tag "$image_tag" \
		--out "$dir/deploy-overlay.lock"; then
		alert StagingOverlayLockWriteFailed "could not write RT-08 deploy-overlay lock for $color from $CANDIDATE_BUNDLE"
		return 1
	fi
	export OMNISIGHT_DEPLOY_OVERLAY_DIR="$dir"
	log "deploy-overlay: wrote RT-08 lock $dir/deploy-overlay.lock for $color tag=$image_tag"
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
	# Deploy candidate BY DIGEST: refuse to bring up $color unless what we
	# just pulled matches the certified candidate bundle (RT-05b).
	if ! verify_pulled_digests "$image_tag"; then
		alert StagingDigestMismatch "post-pull digest != candidate bundle for tag $image_tag; refusing to start $color"
		return 1
	fi
	# RT-08: populate the deployment-identity lock BEFORE bringing $color up so
	# the backend reads a complete lock at startup (a write failure aborts the
	# deploy — fail-closed).
	if ! write_overlay_lock "$color" "$image_tag"; then
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
			--bundle) CANDIDATE_BUNDLE="${2:-}"; shift 2 ;;
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

# Allow `source`ing the script (tests exercise the digest helpers directly)
# without running the deploy. Direct execution still runs main.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
	main "$@"
fi
