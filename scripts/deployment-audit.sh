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
#                      release ref for the alembic-head `auto` comparison (else the
#                      develop trunk head is used; OMNISIGHT_AUDIT_DEPLOY_REF /
#                      OMNISIGHT_AUDIT_DEVELOP_REF override the ref directly)
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
systemd-timer     sora-bridge-sync.timer                                          yes       OP-798    REAL gap: control-plane stranded on main@rc1; re-point off main deferred to cutover
systemd-unit      pipeline-coordinator.service                                    yes       OP-1547   coordinator daemon (ADR-0021) — must be live
systemd-unit      pipeline-coordinator-watchdog.service                           yes       OP-1547   coordinator liveness watchdog — must be live
systemd-unit      omnisight-slo-monitor.service                                   yes       OP-1636   SLO auto-rollback monitor (OP-883) — activated 2026-05-23 (F4); migration-safe rollback (OP-1641, fail-closed)
container         staging@http://localhost:8010/healthz                           yes       OP-927    AUDIT-19 staging stood up 2026-05-22 (project omnisight-staging, repo compose)
systemd-timer     staging-gate-canary.timer                                       gated     OP-965    AUDIT-17 — active (green) since staging stood up
systemd-timer     staging-gate-smoke.timer                                        gated     OP-965    AUDIT-17 — red until bucket-D digest-resolution lands (OP-1607)
alembic-head      auto                                                            n-a       OP-964    informational — prod PG vs deployed-release/develop head from git, NOT the working tree (OP-1701); /readyz authoritatively gates image-vs-DB drift (prod release tag lags develop by design)
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

  echo "deployment-audit (OP-976 / AUDIT-23) — host=$(hostname) user=$USER bus=$([ "$USE_USER_BUS" = 1 ] && echo --user || echo system) date=$(date -u +%FT%TZ)"
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
