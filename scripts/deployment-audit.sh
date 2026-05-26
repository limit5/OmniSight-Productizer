#!/usr/bin/env bash
# scripts/deployment-audit.sh — [OP-976] AUDIT-23
#
# Re-runnable "shipped vs deployed" verification harness. For each row in an
# expected-live manifest it applies the AUDIT-23 §4 verification methodology
# and prints a green/red table; exits non-zero if any row marked `expected=yes`
# is red. Designed to be the standing regression guard against the
# "shipped-but-not-deployed" anti-pattern (see
# docs/audit/2026-05-12-shipped-not-deployed-sprint-dEF.md §6.3).
#
# Run it ON THE HOST that owns the artefacts (the prod host for the release
# units; the sora-bridge host for the bridge units). Re-run quarterly minimum,
# or wrap it in deploy/systemd/deployment-audit.{service,timer} for a daily run.
#
# Usage:
#   scripts/deployment-audit.sh                 # built-in expected-live list
#   scripts/deployment-audit.sh MANIFEST.tsv    # host-specific manifest file
#   DEPLOYMENT_AUDIT_USER_SYSTEMD=0 scripts/deployment-audit.sh   # use system bus
#   DEPLOYMENT_AUDIT_JSONL_LOG=/path/audit.jsonl scripts/deployment-audit.sh
#   OMNISIGHT_DEPLOYED_TAG=v1.2.0 scripts/deployment-audit.sh     # pin the deployed
#                      release ref for the alembic-head `auto` comparison. When unset
#                      it now DEFAULTS to the last promoted release recorded in the
#                      committed promotion ledger (audit/image_promotion_audit.jsonl,
#                      OP-1738) so the alembic row is a real pinned-release assertion
#                      rather than the informational develop-trunk fallback. If neither
#                      a tag nor a ledger is resolvable the develop trunk head is used
#                      (informational only); OMNISIGHT_AUDIT_DEPLOY_REF /
#                      OMNISIGHT_AUDIT_DEVELOP_REF still override the ref directly.
#
# Manifest format — tab-separated, `#` comments and blank lines ignored:
#   <kind>  <name>  <expected>  <ticket>  [note]
#
#   kind ∈ {
#     systemd-unit     name = unit (e.g. auto-promote-main.service)
#     systemd-timer    name = timer (e.g. release-milestone-checker.timer); also
#                      checks the bound service's last Result + list-timers LAST
#     container        name = docker name substring, or  substr@http://host:port/healthz
#     env-var          name = VAR@unit.service   or   VAR@pgrep-pattern
#     image-sha        name = backend image identity join: T1 /version image_sha
#                      vs T2 :latest registry digest, plus T4 manifest image_sha
#                      integrity check. Configure with OMNISIGHT_AUDIT_VERSION_URL,
#                      OMNISIGHT_AUDIT_IMAGE_REF, OMNISIGHT_AUDIT_BACKEND_CONTAINER,
#                      or test override envs documented in check_image_sha.
#     alembic-head     name = expected-revision   or   "auto" (prod PG applied
#                      revision vs the DEPLOYED release / develop head computed
#                      from git — NEVER the local working tree; see
#                      check_alembic_head / OP-1701)
#   }
#   expected ∈ { yes, gated, n-a }
#     yes   → a red row makes this script exit 1
#     gated → peer-gated by design (e.g. staging-gate timers); red is reported, not fatal
#     n-a   → informational only
#
# Exit: 0 = every `expected=yes` row green · 1 = ≥1 red · 2 = bad usage / no host.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USE_USER_BUS="${DEPLOYMENT_AUDIT_USER_SYSTEMD:-1}"
SYSTEMCTL=(systemctl)
[ "$USE_USER_BUS" = "1" ] && SYSTEMCTL=(systemctl --user)

err()  { echo "❌ deployment-audit: $*" >&2; exit 2; }
have() { command -v "$1" >/dev/null 2>&1; }

looks_prod_value() {
  local val="$1"
  [ -n "$val" ] && ! echo "$val" | grep -qiE 'sqlite|localhost|127\.0\.0\.1|placeholder|changeme'
}

# ── deployed-release pin from the promotion ledger (OP-1738) ──────────────────
# The alembic-head `auto` check asserts prod's applied migration against the
# DEPLOYED release head computed from git. Historically OMNISIGHT_DEPLOYED_TAG
# was never set in the audit env, so the check always fell back to origin/develop
# = informational and NEVER asserted prod was actually at the promoted release.
# Default it from the last `image_bundle_promoted` row of the committed promotion
# ledger (OP-1732 landed that ledger on develop) so the audit pins against the
# real shipped tag. An explicit env value — or OMNISIGHT_AUDIT_DEPLOY_REF — still
# wins, and an absent/empty ledger leaves the develop fallback (informational,
# never RED) untouched per OP-1701.
deployed_tag_from_ledger() {
  local ledger="$REPO/audit/image_promotion_audit.jsonl"
  [ -r "$ledger" ] || return 0
  have python3 || return 0
  python3 - "$ledger" <<'PY' 2>/dev/null || true
import json
import sys

tag = ""
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") == "image_bundle_promoted" and rec.get("version"):
                tag = str(rec["version"])  # last promote wins (ledger is append-only)
except OSError:
    pass
print(tag)
PY
}

backend_latest_ref_from_ledger() {
  local ledger="$REPO/audit/image_promotion_audit.jsonl"
  [ -r "$ledger" ] || return 0
  have python3 || return 0
  python3 - "$ledger" <<'PY' 2>/dev/null || true
import json
import sys

repo = ""
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            for image in rec.get("images", []):
                if image.get("name") == "backend" and image.get("repository"):
                    repo = str(image["repository"])
except OSError:
    pass
if repo:
    print(repo + ":latest")
PY
}

# ── results accumulator ───────────────────────────────────────────────────────
FAIL=0          # number of red rows with expected=yes
ROWS=()         # "status|kind|name|expected|ticket|detail"
record() {      # record <STATUS> <kind> <name> <expected> <ticket> <detail>
  local status="$1" kind="$2" name="$3" exp="$4" ticket="$5" detail="$6"
  ROWS+=("${status}|${kind}|${name}|${exp}|${ticket}|${detail}")
  if [ "$status" = "RED" ] && [ "$exp" = "yes" ]; then FAIL=$((FAIL + 1)); fi
}

# ── prerequisite: linger (all --user units die at logout without it) ──────────
check_linger() {
  [ "$USE_USER_BUS" = "1" ] || return 0
  have loginctl || { record "WARN" "linger" "$USER" "n-a" "OP-976" "loginctl absent — cannot verify"; return 0; }
  local v; v="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo unknown)"
  if [ "$v" = "yes" ]; then
    record "OK" "linger" "$USER" "yes" "OP-976" "Linger=yes"
  else
    record "RED" "linger" "$USER" "yes" "OP-976" "Linger=$v — every --user timer stops at logout; run: loginctl enable-linger $USER"
  fi
}

# ── per-kind checks ───────────────────────────────────────────────────────────
check_systemd_unit() {  # name expected ticket
  local name="$1" exp="$2" ticket="$3"
  have systemctl || { record "WARN" "systemd-unit" "$name" "$exp" "$ticket" "systemctl absent"; return; }
  local active enabled load
  load="$("${SYSTEMCTL[@]}" show "$name" -p LoadState --value 2>/dev/null || true)"
  if [ "$load" = "not-found" ]; then
    record "RED" "systemd-unit" "$name" "$exp" "$ticket" "unit not installed (no $name in the user/systemd manager) — copy from deploy/systemd/ + daemon-reload + enable --now"
    return
  fi
  active="$("${SYSTEMCTL[@]}" is-active   "$name" 2>/dev/null || true)"
  enabled="$("${SYSTEMCTL[@]}" is-enabled "$name" 2>/dev/null || true)"
  if [ "$active" = "active" ] && [ "$enabled" = "enabled" ]; then
    record "OK" "systemd-unit" "$name" "$exp" "$ticket" "active+enabled"
  else
    record "RED" "systemd-unit" "$name" "$exp" "$ticket" "is-active=$active is-enabled=$enabled"
  fi
}

check_systemd_timer() {  # name expected ticket
  local timer="$1" exp="$2" ticket="$3"
  have systemctl || { record "WARN" "systemd-timer" "$timer" "$exp" "$ticket" "systemctl absent"; return; }
  local enabled svc last result load
  load="$("${SYSTEMCTL[@]}" show "$timer" -p LoadState --value 2>/dev/null || true)"
  if [ "$load" = "not-found" ]; then
    record "RED" "systemd-timer" "$timer" "$exp" "$ticket" "timer not installed — copy deploy/systemd/${timer%.timer}.{service,timer} to ~/.config/systemd/user/ + daemon-reload + enable --now"
    return
  fi
  enabled="$("${SYSTEMCTL[@]}" is-enabled "$timer" 2>/dev/null || true)"
  svc="$("${SYSTEMCTL[@]}" show "$timer" -p Unit --value 2>/dev/null || true)"
  [ -n "$svc" ] || svc="${timer%.timer}.service"
  last="$("${SYSTEMCTL[@]}" list-timers --all 2>/dev/null | awk -v t="$timer" '$0 ~ t {print "LAST="$5" "$6}' | head -1)"
  result="$("${SYSTEMCTL[@]}" show "$svc" -p Result --value 2>/dev/null || true)"
  if [ "$enabled" = "enabled" ] && { [ "$result" = "success" ] || [ -z "$result" ]; }; then
    record "OK" "systemd-timer" "$timer" "$exp" "$ticket" "enabled; ${svc} Result=${result:-n/a}; ${last:-no-list-timers-row}"
  else
    record "RED" "systemd-timer" "$timer" "$exp" "$ticket" "is-enabled=$enabled; ${svc} Result=${result:-n/a}; ${last:-no-list-timers-row}"
  fi
}

check_container() {  # name(substr[@probeurl]) expected ticket
  local spec="$1" exp="$2" ticket="$3"
  local substr="${spec%@*}" probe=""
  [ "$spec" != "$substr" ] && probe="${spec#*@}"
  have docker || { record "WARN" "container" "$spec" "$exp" "$ticket" "docker absent"; return; }
  local line; line="$(docker ps --format '{{.Names}}\t{{.Status}}' 2>/dev/null | grep -i -- "$substr" | head -1)"
  if [ -z "$line" ]; then
    record "RED" "container" "$spec" "$exp" "$ticket" "no running container matching '$substr' (never \`docker compose up\`?)"
    return
  fi
  local status="${line#*$'\t'}"
  if [ -n "$probe" ] && have curl; then
    if curl -fsS -m 5 "$probe" >/dev/null 2>&1; then
      record "OK" "container" "$spec" "$exp" "$ticket" "$line; probe $probe → 2xx"
    else
      record "RED" "container" "$spec" "$exp" "$ticket" "$line; probe $probe FAILED"
    fi
  elif echo "$status" | grep -qi 'unhealthy'; then
    record "RED" "container" "$spec" "$exp" "$ticket" "$line (unhealthy)"
  else
    record "OK" "container" "$spec" "$exp" "$ticket" "$line"
  fi
}

check_env_var() {  # name(VAR@unit-or-pgrep) expected ticket
  local spec="$1" exp="$2" ticket="$3"
  local var="${spec%@*}" src="${spec#*@}"
  [ "$spec" != "$var" ] || { record "WARN" "env-var" "$spec" "$exp" "$ticket" "spec must be VAR@unit-or-pgrep"; return; }
  local pid=""
  if [[ "$src" == *.service ]] && have systemctl; then
    local env_line env_files file_path file_val
    env_line="$("${SYSTEMCTL[@]}" show "$src" -p Environment --value 2>/dev/null || true)"
    file_val="$(printf '%s\n' "$env_line" | tr ' ' '\n' | grep -E "^${var}=" | head -1 | cut -d= -f2- || true)"
    if [ -n "$file_val" ]; then
      if looks_prod_value "$file_val"; then
        record "OK" "env-var" "$spec" "$exp" "$ticket" "$var configured in systemd Environment"
      else
        record "RED" "env-var" "$spec" "$exp" "$ticket" "$var configured in systemd Environment but looks like a dev/default value"
      fi
      return
    fi
    env_files="$("${SYSTEMCTL[@]}" show "$src" -p EnvironmentFiles --value 2>/dev/null || true)"
    while IFS= read -r file_line; do
      file_path="${file_line%% *}"
      [ -r "$file_path" ] || continue
      file_val="$(grep -E "^${var}=" "$file_path" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
      [ -n "$file_val" ] || continue
      if looks_prod_value "$file_val"; then
        record "OK" "env-var" "$spec" "$exp" "$ticket" "$var configured via ${file_path}"
      else
        record "RED" "env-var" "$spec" "$exp" "$ticket" "$var configured via ${file_path} but looks like a dev/default value"
      fi
      return
    done <<<"$env_files"
    pid="$("${SYSTEMCTL[@]}" show "$src" -p MainPID --value 2>/dev/null || true)"
    [ "$pid" = "0" ] && pid=""
  fi
  if [ -z "$pid" ] && have pgrep; then pid="$(pgrep -f -- "$src" 2>/dev/null | head -1 || true)"; fi
  if [ -z "$pid" ]; then
    record "RED" "env-var" "$spec" "$exp" "$ticket" "no live PID for '$src' — cannot inspect /proc/<pid>/environ"
    return
  fi
  if [ ! -r "/proc/$pid/environ" ]; then
    record "WARN" "env-var" "$spec" "$exp" "$ticket" "PID $pid found but /proc/$pid/environ not readable (run as the owning user / root)"
    return
  fi
  local val; val="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -E "^${var}=" | head -1 | cut -d= -f2-)"
  if [ -z "$val" ]; then
    record "RED" "env-var" "$spec" "$exp" "$ticket" "$var not set in PID $pid env (silent fallback / default in effect)"
  elif ! looks_prod_value "$val"; then
    record "RED" "env-var" "$spec" "$exp" "$ticket" "$var=$val (PID $pid) — looks like a dev/default value, not the prod target"
  else
    record "OK" "env-var" "$spec" "$exp" "$ticket" "$var set in PID $pid (=${val%%:*}...)"
  fi
}

json_field() {
  local field="$1" payload
  payload="$(cat)"
  have python3 || return 1
  python3 - "$field" "$payload" <<'PY' 2>/dev/null
import json
import sys

try:
    payload = json.loads(sys.argv[2])
except Exception:
    sys.exit(1)

value = payload
for part in sys.argv[1].split("."):
    if isinstance(value, dict) and part in value:
        value = value[part]
    else:
        sys.exit(1)
if value is None:
    sys.exit(1)
print(value)
PY
}

iso_epoch() {
  local value="$1"
  [ -n "$value" ] || return 1
  have python3 || return 1
  python3 - "$value" <<'PY' 2>/dev/null
from datetime import datetime, timezone
import sys

value = sys.argv[1].strip()
try:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    print(int(dt.timestamp()))
except Exception:
    sys.exit(1)
PY
}

now_epoch() {
  if [ -n "${OMNISIGHT_AUDIT_NOW:-}" ]; then
    iso_epoch "$OMNISIGHT_AUDIT_NOW"
    return
  fi
  date -u +%s
}

read_t1_version_json() {
  if [ -n "${OMNISIGHT_AUDIT_T1_VERSION_JSON:-}" ]; then
    printf '%s\n' "$OMNISIGHT_AUDIT_T1_VERSION_JSON"
    return 0
  fi
  have curl || return 1
  curl -fsS -m "${OMNISIGHT_AUDIT_HTTP_TIMEOUT:-5}" "${OMNISIGHT_AUDIT_VERSION_URL:-http://localhost:8000/version}"
}

read_t2_registry() {  # emits digest|pushed_at|ref
  local ref="${OMNISIGHT_AUDIT_IMAGE_REF:-}"
  [ -n "$ref" ] || ref="$(backend_latest_ref_from_ledger)"
  [ -n "$ref" ] || ref="ghcr.io/omnisight/omnisight-backend:latest"

  if [ -n "${OMNISIGHT_AUDIT_T2_DIGEST:-}" ]; then
    printf '%s|%s|%s\n' "$OMNISIGHT_AUDIT_T2_DIGEST" "${OMNISIGHT_AUDIT_T2_PUSHED_AT:-}" "$ref"
    return 0
  fi

  have docker || return 1
  local out digest pushed_at
  out="$(docker buildx imagetools inspect "$ref" 2>/dev/null)" || return 1
  digest="$(printf '%s\n' "$out" | awk '/Digest:[[:space:]]*sha256:/ {print $2; exit}')"
  pushed_at="$(printf '%s\n' "$out" | awk '/Created:[[:space:]]*/ {sub(/^[[:space:]]*Created:[[:space:]]*/, ""); print; exit}')"
  [ -n "$digest" ] || return 1
  printf '%s|%s|%s\n' "$digest" "$pushed_at" "$ref"
}

read_t4_manifest_json() {
  if [ -n "${OMNISIGHT_AUDIT_T4_MANIFEST_JSON:-}" ]; then
    printf '%s\n' "$OMNISIGHT_AUDIT_T4_MANIFEST_JSON"
    return 0
  fi
  if [ -n "${OMNISIGHT_AUDIT_T4_IMAGE_SHA:-}" ]; then
    printf '{"image_sha":"%s"}\n' "$OMNISIGHT_AUDIT_T4_IMAGE_SHA"
    return 0
  fi
  have docker || return 1
  docker exec "${OMNISIGHT_AUDIT_BACKEND_CONTAINER:-omnisight-backend}" cat "${OMNISIGHT_AUDIT_MANIFEST_PATH:-/app/MANIFEST.json}" 2>/dev/null
}

check_image_sha() {  # name expected ticket
  local name="$1" exp="$2" ticket="$3"
  local t1_json t1_sha t2_line t2_sha t2_pushed_at t2_ref t4_json t4_sha errors=()

  # Test overrides:
  #   OMNISIGHT_AUDIT_T1_VERSION_JSON, OMNISIGHT_AUDIT_T2_DIGEST,
  #   OMNISIGHT_AUDIT_T2_PUSHED_AT, OMNISIGHT_AUDIT_T4_IMAGE_SHA,
  #   OMNISIGHT_AUDIT_NOW.
  if ! t1_json="$(read_t1_version_json)"; then
    errors+=("T1_running_version.error=/version unreadable")
  else
    t1_sha="$(printf '%s\n' "$t1_json" | json_field image_sha || true)"
    [ -n "$t1_sha" ] || errors+=("T1_running_version.error=image_sha missing")
  fi

  if ! t2_line="$(read_t2_registry)"; then
    errors+=("T2_registry_latest.error=:latest digest unreadable")
  else
    IFS='|' read -r t2_sha t2_pushed_at t2_ref <<<"$t2_line"
    [ -n "$t2_sha" ] || errors+=("T2_registry_latest.error=digest missing")
  fi

  if ! t4_json="$(read_t4_manifest_json)"; then
    errors+=("T4_image_manifest.error=MANIFEST.json unreadable")
  else
    t4_sha="$(printf '%s\n' "$t4_json" | json_field image_sha || true)"
    [ -n "$t4_sha" ] || errors+=("T4_image_manifest.error=image_sha missing")
  fi

  if [ "${#errors[@]}" -gt 0 ]; then
    record "WARN" "image-sha" "$name" "$exp" "$ticket" "result_state=INCOMPLETE; metric=omnisight_deployment_audit_incomplete value=1; ${errors[*]}"
    return
  fi

  local findings=0
  if [ "$t1_sha" != "$t4_sha" ]; then
    record "RED" "image-sha" "$name" "$exp" "$ticket" "result_state=PAGE_INTEGRITY; alert=OmniSightImageIntegrity; T1.image_sha=$t1_sha != T4.manifest.image_sha=$t4_sha"
    findings=$((findings + 1))
  fi

  if [ "$t1_sha" != "$t2_sha" ]; then
    local now pushed_epoch age
    pushed_epoch="$(iso_epoch "$t2_pushed_at" || true)"
    if [ -z "$pushed_epoch" ]; then
      record "WARN" "image-sha" "$name" "$exp" "$ticket" "result_state=INCOMPLETE; metric=omnisight_deployment_audit_incomplete value=1; T2_registry_latest.error=pushed_at missing/invalid; T1.image_sha=$t1_sha T2.digest=$t2_sha ref=$t2_ref"
      return
    fi
    now="$(now_epoch)"
    age=$((now - pushed_epoch))
    [ "$age" -lt 0 ] && age=0
    if [ "$age" -lt "${OMNISIGHT_AUDIT_STALE_SECONDS:-86400}" ]; then
      record "WARN" "image-sha" "$name" "$exp" "$ticket" "result_state=WARN_STALE_IMAGE; alert=OmniSightStaleImage severity=warn; T1.image_sha=$t1_sha != T2.digest=$t2_sha ref=$t2_ref age_seconds=$age"
    else
      record "RED" "image-sha" "$name" "$exp" "$ticket" "result_state=PAGE_STALE_IMAGE; alert=OmniSightStaleImage severity=page; T1.image_sha=$t1_sha != T2.digest=$t2_sha ref=$t2_ref age_seconds=$age"
    fi
    findings=$((findings + 1))
  fi

  if [ "$findings" -eq 0 ]; then
    record "OK" "image-sha" "$name" "$exp" "$ticket" "result_state=OK; T1.image_sha == T2.digest == T4.manifest.image_sha ($t1_sha)"
  fi
}

# ── git-derived alembic head (OP-1701 / finding #21) ──────────────────────────
# The audit can run from an arbitrary or stale working tree (e.g. a per-ticket
# feature branch), so the prod-vs-DB comparison must NOT use the local checkout's
# `alembic heads`. These helpers read the migration tree straight from a git ref
# (the deployed release tag, or develop) and compute the single leaf revision,
# tolerating mixed quote styles and the tuple `down_revision` of merge migrations.
alembic_versions_at_ref() {  # ref -> emits `revision <id>` / `down <id>` tokens
  local ref="$1" vdir="backend/alembic/versions"
  have git || return 0
  git -C "$REPO" rev-parse --verify --quiet "${ref}^{commit}" >/dev/null 2>&1 || return 0
  git -C "$REPO" grep -hI -E '^(revision|down_revision)[[:space:]]*=' "$ref" -- "$vdir" 2>/dev/null \
    | tr "'" '"' \
    | awk '
        { key=$1; rhs=$0; sub(/^[^=]*=[[:space:]]*/,"",rhs)
          while (match(rhs, /"[^"]*"/)) {
            v=substr(rhs, RSTART+1, RLENGTH-2)
            print (key=="revision" ? "revision " : "down ") v
            rhs=substr(rhs, RSTART+RLENGTH)
          } }'
}

check_alembic_head() {  # name(expected-rev|"auto") expected ticket
  local want="$1" exp="$2" ticket="$3"
  local alembic_cmd="alembic"
  [ -x "$REPO/backend/.venv/bin/alembic" ] && alembic_cmd="$REPO/backend/.venv/bin/alembic"
  [ "$alembic_cmd" = "alembic" ] && [ -x "$HOME/.local/bin/alembic" ] && alembic_cmd="$HOME/.local/bin/alembic"
  # Read PROD's live PG head first (the audit shell has no prod DSN, so a bare
  # `alembic current` reads the stale local sqlite and falsely reports drift).
  # This is the genuine live-artifact read — unchanged. Fall back to local alembic
  # only if the prod PG container is unreachable.
  local pg_ctr="${OMNISIGHT_PROD_PG_CONTAINER:-omnisight-pg-primary}"
  local cur; cur="$(docker exec "$pg_ctr" psql -U "${OMNISIGHT_PROD_PG_USER:-omnisight}" -d "${OMNISIGHT_PROD_PG_DB:-omnisight}" -tA -c 'SELECT version_num FROM alembic_version' 2>/dev/null | grep -oE '^[0-9a-f]{4,}' | head -1 || true)"
  [ -n "$cur" ] || cur="$( (cd "$REPO/backend" && have "$alembic_cmd" && "$alembic_cmd" current 2>/dev/null) | grep -oE '^[0-9a-f]{4,}' | head -1 || true)"
  [ -n "$cur" ] || { record "RED" "alembic-head" "$want" "$exp" "$ticket" "prod PG ($pg_ctr) unreachable AND local alembic empty"; return; }

  # ── explicit expected revision wins ──────────────────────────────────────────
  if [ "$want" != "auto" ]; then
    if [ "$cur" = "$want" ]; then record "OK"  "alembic-head" "$want" "$exp" "$ticket" "prod current=$cur"
    else                          record "RED" "alembic-head" "$want" "$exp" "$ticket" "prod current=$cur != expected=$want"; fi
    return
  fi

  # ── auto: compare prod's applied revision against the DEPLOYED RELEASE head (or
  #    the develop trunk head) computed straight from git — NEVER the local working
  #    tree (OP-1701 / finding #21). Comparing prod against an arbitrary checkout
  #    falsely flagged "run upgrade on prod" whenever prod was simply ahead of that
  #    stale tree. /readyz is the authoritative image-vs-DB drift gate (a prod
  #    release tag legitimately lags develop), so this row is informational — only
  #    an explicitly-pinned deployed release that prod has NOT caught up to is a
  #    genuine RED.
  local ref src pinned=no
  if [ -n "${OMNISIGHT_AUDIT_DEPLOY_REF:-}" ]; then
    ref="$OMNISIGHT_AUDIT_DEPLOY_REF"; src="deployed ref ($ref)"; pinned=yes
  elif [ -n "${OMNISIGHT_DEPLOYED_TAG:-}" ]; then
    ref="$OMNISIGHT_DEPLOYED_TAG";     src="deployed release ($ref)"; pinned=yes
  else
    ref="${OMNISIGHT_AUDIT_DEVELOP_REF:-origin/develop}"; src="develop trunk ($ref)"
  fi
  local tree head cur_known=no
  tree="$(alembic_versions_at_ref "$ref")"
  head="$(printf '%s\n' "$tree" | awk '$1=="revision"{r[$2]=1} $1=="down"{d[$2]=1} END{n=0; for(x in r) if(!(x in d)){h=x; n++} if(n==1) print h}')"
  printf '%s\n' "$tree" | awk '$1=="revision"{print $2}' | grep -qxF "$cur" && cur_known=yes

  if [ -z "$head" ]; then
    record "OK" "alembic-head" "$want" "$exp" "$ticket" "prod current=$cur; could not resolve $src head from git (ref/migrations unavailable) — informational, /readyz gates image-vs-DB drift"
  elif [ "$cur" = "$head" ]; then
    record "OK" "alembic-head" "$want" "$exp" "$ticket" "prod current=$cur == $src head"
  elif [ "$cur_known" = "yes" ]; then
    # prod's revision is in this ref's history (an ancestor) → prod lags the ref.
    if [ "$pinned" = "yes" ]; then
      record "RED" "alembic-head" "$want" "$exp" "$ticket" "prod current=$cur is behind the pinned $src head=$head — running release has un-applied migrations; run \`alembic upgrade head\` on prod"
    else
      record "OK" "alembic-head" "$want" "$exp" "$ticket" "prod current=$cur lags $src head=$head (prod runs a release tag behind develop by design) — informational"
    fi
  else
    # cur not in this ref's history → prod is ahead of / divergent from the ref
    # (the "correctly ahead" case against a stale develop). Never a RED.
    record "OK" "alembic-head" "$want" "$exp" "$ticket" "prod current=$cur is ahead of / not contained in $src (head=$head) — prod likely on a newer release; /readyz is authoritative"
  fi
}

append_jsonl() {
  local result="$1" ok="$2" red="$3" warn="$4" log="${DEPLOYMENT_AUDIT_JSONL_LOG:-}"
  [ -n "$log" ] || return 0
  mkdir -p "$(dirname "$log")" || return 0
  {
    printf '%s|%s|%s|%s|%s\n' "$result" "$ok" "$red" "$warn" "$FAIL"
    printf '%s\n' "${ROWS[@]}"
  } | python3 -c '
import json
import os
import socket
import sys
from datetime import datetime, timezone

log = os.environ["DEPLOYMENT_AUDIT_JSONL_LOG"]
summary = sys.stdin.readline().rstrip("\n").split("|")
result, ok, red, warn, fatal = summary
rows = []
for line in sys.stdin:
    status, kind, name, expected, ticket, detail = line.rstrip("\n").split("|", 5)
    rows.append({
        "status": status,
        "kind": kind,
        "name": name,
        "expected": expected,
        "ticket": ticket,
        "detail": detail,
    })
record = {
    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "event": "deployment_audit",
    "source": "scripts/deployment-audit.sh",
    "host": socket.gethostname(),
    "result": result,
    "green": int(ok),
    "red": int(red),
    "warn": int(warn),
    "fatal_red": int(fatal),
    "rows": rows,
}
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record, sort_keys=True) + "\n")
'
}

# ── built-in expected-live manifest (AUDIT-23 §3 rows that should be live) ────
# Override by passing a manifest path as $1. Edit per host (the bridge units
# live on the sora-bridge host, not the prod host).
builtin_manifest() {
  cat <<'EOF'
# kind            name                                                            expected  ticket    note
systemd-timer     release-milestone-checker.timer                                 yes       OP-762    D1 milestone gate — was unenabled until 2026-05-12
systemd-timer     auto-promote-develop.timer                                      n-a       OP-877    RETIRED by release-train (ADR-0040 / RT-01) — develop->main promote removed
# RETIRED (OP-1701 / finding #30): auto-promote-main.service is permanently dead under
# ADR-0040 (single-trunk release train — `main` is retired; no develop->main promotion).
# Probing it as a systemd-unit always emitted a (non-fatal) false RED "unit not installed",
# so the row is dropped. Was: systemd-unit  auto-promote-main.service  n-a  OP-766
env-var           OMNISIGHT_DATABASE_URL@auto-promote-develop.service             n-a       OP-964    auto-promote-develop retired (RT-01) — env-var no longer expected
systemd-timer     sora-bridge-sync.timer                                          yes       OP-798    keeps the sora-bridge control-plane checkout fast-forwarded to origin/develop (re-pointed off main@rc1 per OP-1608, landed 2026-05-22)
systemd-unit      pipeline-coordinator.service                                    yes       OP-1547   coordinator daemon (ADR-0021) — must be live
systemd-unit      pipeline-coordinator-watchdog.service                           yes       OP-1547   coordinator liveness watchdog — must be live
systemd-unit      omnisight-slo-monitor.service                                   yes       OP-1636   SLO auto-rollback monitor (OP-883) — activated 2026-05-23 (F4); migration-safe rollback (OP-1641, fail-closed)
container         staging@http://localhost:8010/healthz                           yes       OP-927    AUDIT-19 staging stood up 2026-05-22 (project omnisight-staging, repo compose)
systemd-timer     staging-gate-canary.timer                                       gated     OP-965    AUDIT-17 — active (green) since staging stood up
systemd-timer     staging-gate-smoke.timer                                        gated     OP-965    AUDIT-17 — red until bucket-D digest-resolution lands (OP-1607)
image-sha         backend                                                         yes       OP-1753   Family ⑤ image-SHA state machine — T1 /version image_sha vs T2 registry :latest digest plus T4 MANIFEST.json image_sha integrity. Detect/report only; emits omnisight_deployment_audit_incomplete on partial reads
alembic-head      auto                                                            yes       OP-1738   pinned-release assertion — prod PG applied head vs the DEPLOYED release head from git (OMNISIGHT_DEPLOYED_TAG, defaulted from the promotion ledger; OP-1701 git-ref logic, NOT the working tree). RED only when prod LAGS a resolvable pinned tag; if no tag is set/resolvable it falls back to origin/develop = informational (never RED). /readyz remains the authoritative image-vs-DB drift gate
EOF
}

# ── cross-stage parity mode (OP-1720) ─────────────────────────────────────────
# `--cross-stage-parity` is the standing, re-runnable declarative cross-stage
# parity audit (the successor to the OP-1709 deep audit). It is a separate
# check family from the shipped-vs-deployed manifest above, so it shells out to
# the sibling engine scripts/deploy_line_parity.sh (single entrypoint here;
# remaining flags such as --live are passed straight through). See that script's
# header for the dimensions + direction-aware verdicts + JSONL/exit semantics.
if [ "${1:-}" = "--cross-stage-parity" ]; then
  shift
  parity="$REPO/scripts/deploy_line_parity.sh"
  [ -x "$parity" ] || err "cross-stage parity engine not found/executable: $parity"
  exec "$parity" "$@"
fi

# ── main ──────────────────────────────────────────────────────────────────────
main() {
  local manifest_src
  if [ $# -ge 1 ]; then
    [ -r "$1" ] || err "manifest '$1' not readable"
    manifest_src="$(cat "$1")"
  else
    manifest_src="$(builtin_manifest)"
  fi

  # Default the deployed-release pin from the committed promotion ledger so the
  # alembic-head `auto` row asserts against the real shipped tag (OP-1738). An
  # explicit OMNISIGHT_DEPLOYED_TAG / OMNISIGHT_AUDIT_DEPLOY_REF always wins.
  if [ -z "${OMNISIGHT_DEPLOYED_TAG:-}" ] && [ -z "${OMNISIGHT_AUDIT_DEPLOY_REF:-}" ]; then
    local ledger_tag; ledger_tag="$(deployed_tag_from_ledger)"
    [ -n "$ledger_tag" ] && export OMNISIGHT_DEPLOYED_TAG="$ledger_tag"
  fi

  echo "deployment-audit (OP-976 / AUDIT-23) — host=$(hostname) user=$USER bus=$([ "$USE_USER_BUS" = 1 ] && echo --user || echo system) date=$(date -u +%FT%TZ)"
  # Self-identify the running copy + its git ref so a run from the STALE main
  # checkout (vs the canonical develop-tip sora-bridge copy the service runs) is
  # immediately visible instead of a silent foot-gun (OP-1738). One canonical
  # copy is expected; remove/symlink any stale main checkout to it.
  local self_ref; self_ref="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  echo "running: ${BASH_SOURCE[0]} (repo=$REPO ref=$self_ref deployed_tag=${OMNISIGHT_DEPLOYED_TAG:-<none>})"
  echo

  check_linger

  while IFS= read -r raw; do
    raw="${raw%%$'\r'}"
    case "$raw" in ''|\#*) continue;; esac
    # split on runs of whitespace into up to 5 fields (note may contain spaces)
    read -r kind name expected ticket note <<<"$raw"
    [ -n "${kind:-}" ] && [ -n "${name:-}" ] || { echo "  (skipping malformed manifest line: $raw)" >&2; continue; }
    expected="${expected:-yes}"; ticket="${ticket:--}"
    case "$kind" in
      systemd-unit)  check_systemd_unit  "$name" "$expected" "$ticket" ;;
      systemd-timer) check_systemd_timer "$name" "$expected" "$ticket" ;;
      container)     check_container     "$name" "$expected" "$ticket" ;;
      env-var)       check_env_var       "$name" "$expected" "$ticket" ;;
      image-sha)     check_image_sha     "$name" "$expected" "$ticket" ;;
      alembic-head)  check_alembic_head  "$name" "$expected" "$ticket" ;;
      *)             record "WARN" "$kind" "$name" "$expected" "$ticket" "unknown kind" ;;
    esac
  done <<<"$manifest_src"

  # ── report ──────────────────────────────────────────────────────────────────
  echo
  printf '%-5s  %-14s  %-58s  %-8s  %-9s  %s\n' STATUS KIND NAME EXPECTED TICKET DETAIL
  printf '%-5s  %-14s  %-58s  %-8s  %-9s  %s\n' '-----' '--------------' '----------------------------------------------------------' '--------' '---------' '------'
  local r status kind name exp ticket detail mark
  for r in "${ROWS[@]}"; do
    IFS='|' read -r status kind name exp ticket detail <<<"$r"
    case "$status" in
      OK)   mark='✓ OK  ' ;;
      RED)  mark='✗ RED ' ;;
      WARN) mark='? WARN' ;;
      *)    mark="$status" ;;
    esac
    printf '%-5s  %-14s  %-58s  %-8s  %-9s  %s\n' "$mark" "$kind" "$name" "$exp" "$ticket" "$detail"
  done
  echo
  local ok red warn
  ok=$(  printf '%s\n' "${ROWS[@]}" | grep -c '^OK|'   || true)
  red=$( printf '%s\n' "${ROWS[@]}" | grep -c '^RED|'  || true)
  warn=$(printf '%s\n' "${ROWS[@]}" | grep -c '^WARN|' || true)
  echo "summary: ${ok} green · ${red} red · ${warn} warn · ${FAIL} red-with-expected=yes (fatal)"
  if [ "$FAIL" -gt 0 ]; then
    append_jsonl "FAIL" "$ok" "$red" "$warn"
    echo "RESULT: FAIL — $FAIL expected-live artefact(s) not deployed. See DETAIL column; remediation in docs/audit/2026-05-12-shipped-not-deployed-sprint-dEF.md §5."
    exit 1
  fi
  append_jsonl "PASS" "$ok" "$red" "$warn"
  echo "RESULT: PASS — all expected-live artefacts confirmed (gated/warn rows are informational)."
  exit 0
}

main "$@"
