#!/usr/bin/env bash
# OP-915 (AUDIT-1) — smoke test for the Cognee install path chosen for
# Sprint F.
#
# What this verifies (AC #4 of OP-915):
#   1. The active Python interpreter can ``import cognee`` and prints a
#      version string.
#   2. ``aiosqlite`` resolves cleanly alongside cognee — Option B's
#      compatibility claim (cognee==1.0.9 + aiosqlite==0.21.0).
#   3. ``claude-agent-sdk`` is importable, matching the Dockerfile bake
#      list that ships with the OP-899 image.
#
# Usage:
#   scripts/verify_cognee_install.sh                     # uses host python3
#   scripts/verify_cognee_install.sh path/to/python      # explicit interpreter
#   PYTHON_BIN=path/to/python scripts/verify_cognee_install.sh
#   scripts/verify_cognee_install.sh --container <name>  # exec in a docker container
#
# Exit codes:
#   0  — all three imports succeeded ("OK" line printed to stdout)
#   1  — one or more imports failed ("FAIL" line printed to stderr)
#   2  — bad invocation / missing prerequisite

set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  scripts/verify_cognee_install.sh [python-interpreter]
  scripts/verify_cognee_install.sh --container <container-name>

Prints either:
  OK cognee=<v> aiosqlite=<v> claude_agent_sdk=<v>
or:
  FAIL <module> <error-text>
EOF
}

CONTAINER=""
PYTHON_BIN="${PYTHON_BIN:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --container)
      CONTAINER="${2:-}"
      [ -n "$CONTAINER" ] || { echo "FAIL: --container needs a name" >&2; exit 2; }
      shift 2 ;;
    --container=*)
      CONTAINER="${1#--container=}"
      shift ;;
    *)
      if [ -z "$PYTHON_BIN" ]; then
        PYTHON_BIN="$1"
        shift
      else
        echo "FAIL: unexpected argument: $1" >&2
        usage
        exit 2
      fi ;;
  esac
done

# Single embedded probe so the host and container paths exercise the
# exact same import sequence + error format.
read -r -d '' PROBE <<'PY' || true
import importlib, sys, traceback

mods = ("cognee", "aiosqlite", "claude_agent_sdk")
report = []
for name in mods:
    try:
        m = importlib.import_module(name)
        report.append(f"{name}={getattr(m, '__version__', 'unknown')}")
    except Exception as exc:  # noqa: BLE001 — surface the raw error
        sys.stderr.write(f"FAIL {name}: {exc.__class__.__name__}: {exc}\n")
        traceback.print_exc()
        sys.exit(1)
print("OK " + " ".join(report))
PY

if [ -n "$CONTAINER" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "FAIL: docker CLI not on PATH" >&2
    exit 2
  fi
  exec docker exec -i "$CONTAINER" python - <<<"$PROBE"
fi

if [ -z "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "FAIL: python interpreter not found (set PYTHON_BIN or pass as arg)" >&2
  exit 2
fi

exec "$PYTHON_BIN" - <<<"$PROBE"
