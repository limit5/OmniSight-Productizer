#!/usr/bin/env bash
# scripts/portability-audit.sh — [OP-978] AUDIT-25
#
# Standalone shell CLI for the AUDIT-19d staging-portability audit. Salvaged
# from the abandoned Gerrit #487 (PS1 `25f65d35`); the audit *logic* is the
# same four facets the pytest version checks — see
# `tests/test_staging_migration_5a_to_5c.py` (the canonical definition; this
# script mirrors it) and §"Standalone shell audit" in
# `docs/operations/staging-migration-5a-to-5c-runbook.md`.
#
# Static portability audit of the AUDIT-19a/b/c staging artifacts — the
# files that have to move host-cleanly from **5a** (the co-tenanted Docker
# Compose project on the prod box) to **5c** (Win11 + WSL Ubuntu-24.04 +
# Docker on the same LAN segment). It encodes AUDIT-19d acceptance
# criterion #1:
#
#   A. host filesystem paths are relative OR an env-var-with-default
#      (`${VAR:-/abs/path}`) — never a bare absolute path baked into a
#      portable artifact. (systemd unit files are *expected* to carry
#      absolute paths — they are listed as operator-edit points, not
#      flagged; see the "operator-edit points" section in the output.)
#   B. no hard-coded prod connection-pool endpoint (`:6432`, the prod
#      pgbouncer port) anywhere outside a `.env.local` host override.
#   C. every published *host* port in deploy/staging/docker-compose.yml is
#      `${VAR...}`-parameterised, so a port collision on 5c is one env edit.
#   D. the staging-compose systemd unit's cgroup ceiling is expressed as a
#      fraction (`MemoryMax=…%` / `CPUQuota=…%`) so it auto-scales with the
#      target host's RAM/cores instead of being an absolute count tuned for
#      the 5a box.
#
# DoD (AUDIT-19d): this script "returns 0 hits" — exit 0. A non-zero exit
# means the migration runbook (docs/operations/staging-migration-5a-to-5c-runbook.md)
# would have a manual fix-up step that should instead be a `${VAR}` knob.
#
# Usage:
#   scripts/portability-audit.sh [--verbose] [--json]
#
#   --verbose   echo the env-var-default lines / port mappings it accepted
#   --json      emit a single machine-readable JSON object instead of the
#               human report (for CI gates / pre-commit hooks / editor tasks)
#
# Reads tracked files only — no docker / systemd / network. Run it from
# anywhere (it resolves the repo root from its own path); wire it into the
# 5c migration verification (runbook §4) and re-run it whenever an
# AUDIT-19* artifact changes.
#
# Exit: 0 = clean · 1 = ≥1 portability finding · 2 = bad usage / missing file.

set -uo pipefail

VERBOSE=0
JSON=0
for arg in "$@"; do
  case "$arg" in
    -v|--verbose) VERBOSE=1 ;;
    --json)       JSON=1 ;;
    -h|--help)    echo "usage: $0 [--verbose] [--json]"; exit 0 ;;
    *) echo "usage: $0 [--verbose] [--json]" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || { echo "❌ portability-audit: cannot cd to repo root $REPO" >&2; exit 2; }

# ── the portable artifact set (AUDIT-19a/b/c outputs that must travel) ────────
PORTABLE_FILES=(
  infra/staging/.env.template
  infra/staging/anonymize.sh
  infra/staging/anonymize-fields.yaml
  infra/staging/snapshot-restore.sh
  infra/staging/verify-env-contract.sh
  deploy/staging/docker-compose.yml
  deploy/staging/caddy.json
  scripts/staging_deploy.sh
  scripts/sync_staging_to_develop.sh
)
# systemd units travel too, but absolute paths in them are *expected* —
# the runbook has an explicit "rewrite the WorkingDirectory / Exec* /
# EnvironmentFile paths" step. We list, not flag, these.
SYSTEMD_UNITS=(
  deploy/systemd/omnisight-staging-compose.service
  deploy/systemd/staging-pg-snapshot.service
  deploy/systemd/staging-pg-snapshot.timer
  deploy/systemd/staging-sync.service
  deploy/systemd/staging-sync.timer
)
COMPOSE_UNIT="deploy/systemd/omnisight-staging-compose.service"

# Host-app-state path prefixes that MUST be env-driven in a portable file.
# (Container-internal paths like /var/lib/postgresql/data and the standard
# /var/run/docker.sock are not host-app state — they are the same on every
# Linux host, WSL included — so they are deliberately not in this list.)
ABS_PATH_RE='/(home|root)/|/var/lib/omnisight|/var/tmp/omnisight|/srv/omnisight|/opt/omnisight'

HITS=0
FINDINGS=()      # one "FACET\tlocation\tmessage" line per finding (for --json)

# say()  — decorative output; suppressed in --json mode.
# note() — extra detail; only with --verbose, and never in --json mode.
say()  { [ "$JSON" = 1 ] || printf '%s\n' "$*"; }
note() { { [ "$VERBOSE" = 1 ] && [ "$JSON" != 1 ]; } && printf '   · %s\n' "$*" || true; }
hit()  {
  HITS=$((HITS + 1))
  FINDINGS+=("$1"$'\t'"$2"$'\t'"$3")
  [ "$JSON" = 1 ] || printf '❌ [%s] %s — %s\n' "$1" "$2" "$3"
}
ok()   { say "✓  $1"; }
die2() { echo "❌ portability-audit: $1" >&2; exit 2; }

json_escape() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\t'/\\t}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  printf '%s' "$s"
}

for f in "${PORTABLE_FILES[@]}" "${SYSTEMD_UNITS[@]}"; do
  [ -f "$f" ] || die2 "expected artifact missing: $f"
done

say "portability-audit (OP-978 / AUDIT-25 — salvaged from #487; logic == AUDIT-19d) — repo: $REPO"
say ""

# ── A. absolute host-app-state paths in portable artifacts ───────────────────
say "A. absolute paths in portable artifacts (must be relative or \${VAR:-default})"
A_HITS_BEFORE=$HITS
for f in "${PORTABLE_FILES[@]}"; do
  lineno=0
  while IFS= read -r ln || [ -n "$ln" ]; do
    lineno=$((lineno + 1))
    # strip a full-line comment (leading whitespace then #) and a trailing
    # " #..." inline comment — leaves the executable part of the line.
    stripped="${ln#"${ln%%[![:space:]]*}"}"          # ltrim
    case "$stripped" in '#'*) continue ;; esac        # full comment line
    code="${ln%% #*}"                                 # drop trailing " #comment"
    case "$code" in *$'\t#'*) code="${code%%$'\t#'*}" ;; esac
    printf '%s' "$code" | grep -Eq "$ABS_PATH_RE" || continue
    # allowed: the path is an env-var default — ${VAR:-/abs/path}
    case "$code" in *':-/'*) note "$f:$lineno env-default OK: $(printf '%s' "$ln" | sed 's/^[[:space:]]*//')"; continue ;; esac
    hit "A" "$f:$lineno" "bare absolute path (make it \${VAR:-…}): $(printf '%s' "$ln" | sed 's/^[[:space:]]*//')"
  done < "$f"
done
[ "$HITS" -eq "$A_HITS_BEFORE" ] && ok "no bare absolute host paths in portable artifacts"
say ""

# ── B. hard-coded prod pgbouncer endpoint (:6432) outside *.env.local ────────
say "B. hard-coded prod pool endpoint (:6432) in the staging artifacts"
B_HITS_BEFORE=$HITS
# Scope: the AUDIT-19* artifact surface (the portable files + the systemd
# units). `.env.local` host overrides are exempt (that file is *meant* to
# carry host-specific endpoints and is .gitignored). A `:6432` literal in
# any of these is a prod connection-pool endpoint that should be a `${VAR}`
# fed from the host's `.env.local`, not baked in.
for f in "${PORTABLE_FILES[@]}" "${SYSTEMD_UNITS[@]}"; do
  case "$f" in *.env.local|*/.env.local) continue ;; esac
  while IFS= read -r match; do
    [ -n "$match" ] || continue
    hit "B" "$f:${match%%:*}" "hard-coded prod pool endpoint :6432 — feed it from a host \`.env.local\` via \${VAR}: ${match#*:}"
  done < <(grep -nE '(localhost|127\.0\.0\.1|[A-Za-z0-9_.-]+):6432\b' -- "$f" 2>/dev/null || true)
done
[ "$HITS" -eq "$B_HITS_BEFORE" ] && ok "no hard-coded :6432 endpoints in the staging artifacts"
say ""

# ── C. every published host port in the staging compose is ${VAR}-driven ─────
say "C. published host ports in deploy/staging/docker-compose.yml are \${VAR}-parameterised"
C_HITS_BEFORE=$HITS
# `ports:` short-syntax entries look like:  - "HOST:CONTAINER"  or "HOST:CONTAINER/proto"
while IFS= read -r portln; do
  # extract the quoted "…" mapping
  mapping="$(printf '%s' "$portln" | sed -nE 's/.*"([^"]+)".*/\1/p')"
  [ -n "$mapping" ] || continue
  host_side="${mapping%%:*}"
  case "$host_side" in
    *'${'*) note "compose port OK: $mapping" ;;
    *) hit "C" "deploy/staging/docker-compose.yml" "host port not parameterised: $mapping (use \${STAGING_*_PORT:-…})" ;;
  esac
done < <(awk '/^[[:space:]]*ports:[[:space:]]*$/{inports=1;next}
              inports && /^[[:space:]]*-[[:space:]]*"/{print;next}
              inports && /^[[:space:]]*[^-[:space:]]/{inports=0}' deploy/staging/docker-compose.yml)
[ "$HITS" -eq "$C_HITS_BEFORE" ] && ok "all published host ports are \${VAR}-parameterised"
say ""

# ── D. cgroup ceiling on the staging-compose unit is a fraction ──────────────
say "D. cgroup ceiling in $COMPOSE_UNIT is a fraction (auto-scales with the host)"
D_HITS_BEFORE=$HITS
for key in MemoryMax MemoryHigh CPUQuota; do
  while IFS= read -r val; do
    [ -n "$val" ] || continue
    case "$val" in
      *%) note "$key=$val — fraction OK" ;;
      ''|infinity) ;;  # unset / infinity is not a portability problem
      *) hit "D" "$COMPOSE_UNIT" "$key=$val is an absolute value — use a percentage so it auto-scales on 5c" ;;
    esac
  done < <(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$COMPOSE_UNIT" | sed -E "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//")
done
# the unit should carry *some* fractional ceiling, not none at all.
if ! grep -Eq '^[[:space:]]*(MemoryMax|MemoryHigh)[[:space:]]*=[[:space:]]*[0-9]+%' "$COMPOSE_UNIT"; then
  hit "D" "$COMPOSE_UNIT" "no fractional MemoryMax/MemoryHigh ceiling — staging could starve the 5c host"
fi
[ "$HITS" -eq "$D_HITS_BEFORE" ] && ok "cgroup ceiling is a host-relative fraction"
say ""

# ── informational: operator-edit points (absolute paths in systemd units) ────
EDIT_POINTS=()
for u in "${SYSTEMD_UNITS[@]}"; do
  n="$(grep -Ec '(/home/|/var/lib/omnisight|/var/tmp/omnisight)' "$u" || true)"
  EDIT_POINTS+=("$u"$'\t'"${n:-0}")
done
if [ "$JSON" != 1 ]; then
  echo "ℹ  operator-edit points on 5c — absolute paths in the systemd units"
  echo "   (expected; the migration runbook §3 step \"rewrite unit paths\" covers these)"
  for e in "${EDIT_POINTS[@]}"; do
    printf '   - %-50s %s path line(s) to review\n' "${e%%$'\t'*}" "${e#*$'\t'}"
  done
  echo ""
fi

# ── verdict ──────────────────────────────────────────────────────────────────
EXIT=$([ "$HITS" -eq 0 ] && echo 0 || echo 1)

if [ "$JSON" = 1 ]; then
  printf '{'
  printf '"audit":"portability-audit","ticket":"OP-978","spec":"AUDIT-19d-AC1",'
  printf '"repo":"%s",' "$(json_escape "$REPO")"
  printf '"hits":%d,"exit":%d,' "$HITS" "$EXIT"
  printf '"findings":['
  for i in "${!FINDINGS[@]}"; do
    IFS=$'\t' read -r facet loc msg <<<"${FINDINGS[$i]}"
    [ "$i" -gt 0 ] && printf ','
    printf '{"facet":"%s","location":"%s","message":"%s"}' \
      "$(json_escape "$facet")" "$(json_escape "$loc")" "$(json_escape "$msg")"
  done
  printf '],'
  printf '"operator_edit_points":['
  for i in "${!EDIT_POINTS[@]}"; do
    IFS=$'\t' read -r unit cnt <<<"${EDIT_POINTS[$i]}"
    [ "$i" -gt 0 ] && printf ','
    printf '{"unit":"%s","path_lines":%d}' "$(json_escape "$unit")" "$cnt"
  done
  printf ']}'
  printf '\n'
  exit "$EXIT"
fi

if [ "$HITS" -eq 0 ]; then
  echo "✅ portability-audit: 0 hits — AUDIT-19a/b/c artifacts are 5c-portable."
  exit 0
fi
echo "❌ portability-audit: $HITS hit(s) — fix before the 5a→5c migration (see the runbook)."
exit 1
