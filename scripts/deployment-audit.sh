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
#     alembic-head     name = expected-revision   or   "auto" (compare vs `alembic heads`)
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
  elif echo "$val" | grep -qiE 'sqlite|localhost|127\.0\.0\.1|placeholder|changeme'; then
    record "RED" "env-var" "$spec" "$exp" "$ticket" "$var=$val (PID $pid) — looks like a dev/default value, not the prod target"
  else
    record "OK" "env-var" "$spec" "$exp" "$ticket" "$var set in PID $pid (=${val%%:*}...)"
  fi
}

check_alembic_head() {  # name(expected-rev|"auto") expected ticket
  local want="$1" exp="$2" ticket="$3"
  have alembic || { record "WARN" "alembic-head" "$want" "$exp" "$ticket" "alembic absent — run from the backend venv on prod"; return; }
  local cur; cur="$( (cd "$REPO" && alembic current 2>/dev/null) | grep -oE '^[0-9a-f]{8,}' | head -1 || true)"
  [ -n "$cur" ] || { record "RED" "alembic-head" "$want" "$exp" "$ticket" "\`alembic current\` returned nothing — DB unreachable or alembic_version empty"; return; }
  if [ "$want" = "auto" ]; then
    local head; head="$( (cd "$REPO" && alembic heads 2>/dev/null) | grep -oE '^[0-9a-f]{8,}' | head -1 || true)"
    if [ "$cur" = "$head" ]; then record "OK"  "alembic-head" "$want" "$exp" "$ticket" "current=$cur == repo head"
    else                          record "RED" "alembic-head" "$want" "$exp" "$ticket" "current=$cur != repo head=$head — un-applied migration; run \`alembic upgrade head\` on prod"; fi
  else
    if [ "$cur" = "$want" ]; then record "OK"  "alembic-head" "$want" "$exp" "$ticket" "current=$cur"
    else                          record "RED" "alembic-head" "$want" "$exp" "$ticket" "current=$cur != expected=$want"; fi
  fi
}

# ── built-in expected-live manifest (AUDIT-23 §3 rows that should be live) ────
# Override by passing a manifest path as $1. Edit per host (the bridge units
# live on the sora-bridge host, not the prod host).
builtin_manifest() {
  cat <<'EOF'
# kind            name                                                            expected  ticket    note
systemd-timer     release-milestone-checker.timer                                 yes       OP-762    D1 milestone gate — was unenabled until 2026-05-12
systemd-timer     auto-promote-develop.timer                                      yes       OP-877    D5 daily develop->refs/for/main
systemd-unit      auto-promote-main.service                                       n-a       OP-766    superseded-in-place by OP-960 Gerrit-review path; if active, check it is not racing
env-var           OMNISIGHT_DATABASE_URL@auto-promote-develop.service             yes       OP-964    AUDIT-16 — release_audit DSN; absent => silent SQLite fallback
systemd-timer     sora-bridge-sync.timer                                          yes       OP-798    Phase A — RUN ON THE sora-bridge HOST
container         staging@http://localhost:8010/healthz                           gated     OP-927    R5 / AUDIT-19 staging env — not up yet by design
systemd-timer     staging-gate-canary.timer                                       gated     OP-965    AUDIT-17 — enable as final step of AUDIT-19
systemd-timer     staging-gate-smoke.timer                                        gated     OP-965    AUDIT-17 — enable as final step of AUDIT-19
alembic-head      auto                                                            yes       OP-964    prod PG must be at repo head (no manual upgrade pending)
EOF
}

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
    echo "RESULT: FAIL — $FAIL expected-live artefact(s) not deployed. See DETAIL column; remediation in docs/audit/2026-05-12-shipped-not-deployed-sprint-dEF.md §5."
    exit 1
  fi
  echo "RESULT: PASS — all expected-live artefacts confirmed (gated/warn rows are informational)."
  exit 0
}

main "$@"
