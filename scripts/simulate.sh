#!/bin/bash
# ============================================================
# OmniSight Dual-Track Simulation Runner
# Unified test entry point for AI Agent embedded development.
#
# Usage:
#   ./simulate.sh --type=[algo|hw] --module=[name] \
#     [--input=data] [--mock=true] [--coverage-check=true] \
#     [--platform=aarch64]
#
# stdout: JSON report (machine-parseable)
# stderr: Human-readable progress (for SSE streaming)
# ============================================================

set -euo pipefail

# ── Defaults ──
TYPE=""
MODULE=""
INPUT_DATA=""
MOCK="true"
COVERAGE_CHECK="false"
PLATFORM="aarch64"
WORKSPACE="${WORKSPACE:-/workspace}"
TEST_ASSETS="${WORKSPACE}/test_assets"
PLATFORM_DIR="${WORKSPACE}/configs/platforms"

# ── Parse arguments ──
for arg in "$@"; do
  case "$arg" in
    --type=*)       TYPE="${arg#*=}" ;;
    --module=*)     MODULE="${arg#*=}" ;;
    --input=*)      INPUT_DATA="${arg#*=}" ;;
    --mock=*)       MOCK="${arg#*=}" ;;
    --coverage-check=*) COVERAGE_CHECK="${arg#*=}" ;;
    --platform=*)   PLATFORM="${arg#*=}" ;;
    --toolchain-file=*) CMAKE_TOOLCHAIN_FILE="${arg#*=}" ;;
    *) ;;
  esac
done

# ── Validate ──
if [ -z "$TYPE" ] || [ -z "$MODULE" ]; then
  echo '{"version":"1.0","status":"error","errors":["Missing required --type and --module"]}'
  exit 1
fi

if [ "$TYPE" != "algo" ] && [ "$TYPE" != "hw" ]; then
  echo '{"version":"1.0","status":"error","errors":["--type must be algo or hw"]}'
  exit 1
fi

# ── Input sanitization (prevent shell injection + path traversal) ──
validate_name() {
  local name="$1" label="$2"
  if [[ ! "$name" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "{\"version\":\"1.0\",\"status\":\"error\",\"errors\":[\"Invalid ${label}: only alphanumeric, dash, underscore allowed\"]}"
    exit 1
  fi
}
validate_name "$MODULE" "module"
validate_name "$PLATFORM" "platform"
if [ -n "$INPUT_DATA" ]; then
  # INPUT_DATA may include subdirectory path but no .. or absolute paths
  if [[ "$INPUT_DATA" == /* ]] || [[ "$INPUT_DATA" == *..* ]]; then
    echo '{"version":"1.0","status":"error","errors":["Invalid input_data: no absolute paths or .. allowed"]}'
    exit 1
  fi
fi

# ── Platform profile ──
TOOLCHAIN="gcc"
CROSS_PREFIX=""
QEMU_BIN=""
ARCH_FLAGS=""
CMAKE_TOOLCHAIN_FILE="${CMAKE_TOOLCHAIN_FILE:-}"
VENDOR_SYSROOT=""

if [ -f "${PLATFORM_DIR}/${PLATFORM}.yaml" ]; then
  # Simple YAML parsing (no dependency on python/yq)
  TOOLCHAIN=$(grep 'toolchain:' "${PLATFORM_DIR}/${PLATFORM}.yaml" 2>/dev/null | head -1 | sed 's/.*toolchain:\s*//' | tr -d '"' || echo "gcc")
  CROSS_PREFIX=$(grep 'cross_prefix:' "${PLATFORM_DIR}/${PLATFORM}.yaml" 2>/dev/null | head -1 | sed 's/.*cross_prefix:\s*//' | tr -d '"' || echo "")
  QEMU_BIN=$(grep 'qemu:' "${PLATFORM_DIR}/${PLATFORM}.yaml" 2>/dev/null | head -1 | sed 's/.*qemu:\s*//' | tr -d '"' || echo "")
  ARCH_FLAGS=$(grep 'arch_flags:' "${PLATFORM_DIR}/${PLATFORM}.yaml" 2>/dev/null | head -1 | sed 's/.*arch_flags:\s*//' | tr -d '[]"' || echo "")
  # Vendor SDK fields (may be empty for generic platforms)
  if [ -z "$CMAKE_TOOLCHAIN_FILE" ]; then
    CMAKE_TOOLCHAIN_FILE=$(grep 'cmake_toolchain_file:' "${PLATFORM_DIR}/${PLATFORM}.yaml" 2>/dev/null | head -1 | sed 's/.*cmake_toolchain_file:\s*//' | tr -d '"' || echo "")
  fi
  VENDOR_SYSROOT=$(grep 'sysroot_path:' "${PLATFORM_DIR}/${PLATFORM}.yaml" 2>/dev/null | head -1 | sed 's/.*sysroot_path:\s*//' | tr -d '"' || echo "")
elif [ "$PLATFORM" = "aarch64" ]; then
  TOOLCHAIN="aarch64-linux-gnu-gcc"
  CROSS_PREFIX="aarch64-linux-gnu-"
  QEMU_BIN="qemu-aarch64-static"
  ARCH_FLAGS="-march=armv8-a"
fi

# Build CMAKE_FLAGS (for cmake) and GCC_VENDOR_FLAGS (for direct gcc/g++)
CMAKE_FLAGS=""
GCC_VENDOR_FLAGS=""
if [ -n "$CMAKE_TOOLCHAIN_FILE" ] && [ -f "$CMAKE_TOOLCHAIN_FILE" ]; then
  CMAKE_FLAGS="-DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE}"
  log "  Vendor CMake toolchain: ${CMAKE_TOOLCHAIN_FILE}"
fi
if [ -n "$VENDOR_SYSROOT" ] && [ -d "$VENDOR_SYSROOT" ]; then
  CMAKE_FLAGS="${CMAKE_FLAGS} -DCMAKE_SYSROOT=${VENDOR_SYSROOT}"
  GCC_VENDOR_FLAGS="--sysroot=${VENDOR_SYSROOT}"
  log "  Vendor sysroot: ${VENDOR_SYSROOT}"
fi

# ── Working directories ──
BUILD_DIR=$(mktemp -d "/tmp/omnisight-sim-XXXXXX")
MODULE_DIR="${WORKSPACE}/src/${MODULE}"
MODULE_ASSETS="${TEST_ASSETS}/${MODULE}"
EXPECTED_DIR="${MODULE_ASSETS}/expected"

cleanup() { rm -rf "$BUILD_DIR"; }
trap cleanup EXIT

# ── Helpers ──
log() { echo >&2 "$@"; }
now_ms() { date +%s%N | cut -b1-13; }
START_MS=$(now_ms)

# JSON string escape: backslash, double-quote, newline, tab, carriage return
json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g' | tr '\n' ' '
}

# JSON building helpers
json_test_detail() {
  local name="$1" status="$2" duration="$3" msg
  msg=$(json_escape "$4")
  printf '{"name":"%s","status":"%s","duration_ms":%s,"message":"%s"}' \
    "$name" "$status" "$duration" "$msg"
}

# ── Result accumulators ──
TESTS_TOTAL=0
TESTS_PASSED=0
TESTS_FAILED=0
TEST_DETAILS=""
VALGRIND_RAN="false"
VALGRIND_ERRORS=0
VALGRIND_DEFINITELY_LOST=0
VALGRIND_SUMMARY="Not run"
QEMU_USED="false"
QEMU_EXIT=0
COVERAGE_EXPECTED=0
COVERAGE_RUN=0
PEAK_MEM_KB=0
WALL_TIME_MS=0
ERRORS=""

add_error() {
  local escaped
  escaped=$(json_escape "$1")
  if [ -n "$ERRORS" ]; then ERRORS="${ERRORS},"; fi
  ERRORS="${ERRORS}\"${escaped}\""
}

add_test_detail() {
  if [ -n "$TEST_DETAILS" ]; then TEST_DETAILS="${TEST_DETAILS},"; fi
  TEST_DETAILS="${TEST_DETAILS}$1"
}

# ============================================================
# TRACK 1: Algorithm Simulation (Data-Driven Replay)
# ============================================================
run_algo() {
  log "[1/5] Locating module source: ${MODULE}"

  # Find source file
  local src_file=""
  for ext in c cpp; do
    if [ -f "${MODULE_DIR}/${MODULE}.${ext}" ]; then
      src_file="${MODULE_DIR}/${MODULE}.${ext}"
      break
    fi
    if [ -f "${MODULE_DIR}/main.${ext}" ]; then
      src_file="${MODULE_DIR}/main.${ext}"
      break
    fi
  done

  if [ -z "$src_file" ]; then
    # Try workspace root
    for ext in c cpp; do
      if [ -f "${WORKSPACE}/${MODULE}.${ext}" ]; then
        src_file="${WORKSPACE}/${MODULE}.${ext}"
        break
      fi
    done
  fi

  if [ -z "$src_file" ]; then
    add_error "Source file not found for module: ${MODULE}"
    log "[ERROR] Source not found"
    return 1
  fi
  log "       Source: ${src_file}"
  log "       [OK]"

  # Compile for x86_64 (host)
  log "[2/5] Compiling for x86_64 (host)..."
  local compiler="gcc"
  local std_flag="-std=c11"
  if [[ "$src_file" == *.cpp ]]; then
    compiler="g++"
    std_flag="-std=c++17"
  fi

  local binary="${BUILD_DIR}/${MODULE}_test"
  if ! $compiler $std_flag -O2 -Wall -Werror -g $GCC_VENDOR_FLAGS -o "$binary" "$src_file" -lm 2>"${BUILD_DIR}/compile.log"; then
    local compile_err
    compile_err=$(head -5 "${BUILD_DIR}/compile.log" | tr '\n' ' ')
    add_error "Compilation failed: ${compile_err}"
    log "[ERROR] Compilation failed"
    cat >&2 "${BUILD_DIR}/compile.log"
    return 1
  fi
  log "       Binary: ${binary}"
  log "       [OK]"

  # Enumerate test cases
  log "[3/5] Running test cases..."
  local test_files=()
  if [ -d "$MODULE_ASSETS" ]; then
    while IFS= read -r -d '' f; do
      test_files+=("$f")
    done < <(find "$MODULE_ASSETS" -maxdepth 1 -type f \( -name '*.dat' -o -name '*.bin' -o -name '*.txt' -o -name '*.json' \) -print0 | sort -z)
  fi

  if [ -n "$INPUT_DATA" ] && [ -f "${TEST_ASSETS}/${INPUT_DATA}" ]; then
    test_files+=("${TEST_ASSETS}/${INPUT_DATA}")
  fi

  COVERAGE_EXPECTED=${#test_files[@]}
  COVERAGE_RUN=0

  if [ "$COVERAGE_CHECK" = "true" ] && [ "$COVERAGE_EXPECTED" -eq 0 ]; then
    add_error "Coverage check failed: no test files found in ${MODULE_ASSETS}"
    log "[ERROR] No test files"
    return 1
  fi

  for tf in "${test_files[@]}"; do
    local tname
    tname=$(basename "$tf")
    TESTS_TOTAL=$((TESTS_TOTAL + 1))
    COVERAGE_RUN=$((COVERAGE_RUN + 1))
    local t_start
    t_start=$(now_ms)

    local output_file="${BUILD_DIR}/output_${tname}"
    if timeout 30 "$binary" < "$tf" > "$output_file" 2>"${BUILD_DIR}/stderr_${tname}"; then
      local t_dur=$(( $(now_ms) - t_start ))

      # Compare with expected if available
      local expected_file="${EXPECTED_DIR}/${tname}"
      if [ -f "$expected_file" ]; then
        if diff -q "$output_file" "$expected_file" >/dev/null 2>&1; then
          TESTS_PASSED=$((TESTS_PASSED + 1))
          add_test_detail "$(json_test_detail "$tname" "pass" "$t_dur" "")"
          log "       [PASS] ${tname} (${t_dur}ms)"
        else
          TESTS_FAILED=$((TESTS_FAILED + 1))
          local diff_msg
          diff_msg=$(diff --brief "$output_file" "$expected_file" 2>&1 | head -1)
          add_test_detail "$(json_test_detail "$tname" "fail" "$t_dur" "Output mismatch: ${diff_msg}")"
          add_error "Test ${tname}: output mismatch"
          log "       [FAIL] ${tname} — output mismatch"
        fi
      else
        # No expected file — pass if exit code 0
        TESTS_PASSED=$((TESTS_PASSED + 1))
        add_test_detail "$(json_test_detail "$tname" "pass" "$t_dur" "No ground truth, exit 0")"
        log "       [PASS] ${tname} (no ground truth, exit 0)"
      fi
    else
      local t_dur=$(( $(now_ms) - t_start ))
      TESTS_FAILED=$((TESTS_FAILED + 1))
      local err_msg
      err_msg=$(head -3 "${BUILD_DIR}/stderr_${tname}" 2>/dev/null | tr '\n' ' ')
      add_test_detail "$(json_test_detail "$tname" "fail" "$t_dur" "Runtime error: ${err_msg}")"
      add_error "Test ${tname}: runtime error"
      log "       [FAIL] ${tname} — runtime error"
    fi
  done
  log "       Results: ${TESTS_PASSED}/${TESTS_TOTAL} passed"
  log "       [OK]"

  # Valgrind memory check
  log "[4/5] Running Valgrind memory check..."
  if command -v valgrind >/dev/null 2>&1; then
    VALGRIND_RAN="true"
    local valgrind_xml="${BUILD_DIR}/valgrind.xml"
    local valgrind_input="${test_files[0]:-/dev/null}"

    if valgrind --xml=yes --xml-file="$valgrind_xml" --leak-check=full \
       --error-exitcode=99 "$binary" < "$valgrind_input" >/dev/null 2>&1; then
      VALGRIND_ERRORS=0
      VALGRIND_SUMMARY="No errors detected"
      log "       [OK] No memory errors"
    else
      # Parse XML for error count
      VALGRIND_ERRORS=$(grep -c '<error>' "$valgrind_xml" 2>/dev/null || echo "0")
      VALGRIND_DEFINITELY_LOST=$(grep -oP '(?<=<leakedbytes>)\d+' "$valgrind_xml" 2>/dev/null | awk '{s+=$1} END {print s+0}' || echo "0")
      VALGRIND_SUMMARY="${VALGRIND_ERRORS} error(s), ${VALGRIND_DEFINITELY_LOST} bytes definitely lost"
      add_error "Valgrind: ${VALGRIND_SUMMARY}"
      log "       [WARN] ${VALGRIND_SUMMARY}"
    fi
  else
    log "       [SKIP] Valgrind not installed"
    VALGRIND_SUMMARY="Not available"
  fi

  # Performance metrics
  log "[5/5] Collecting performance metrics..."
  if [ ${#test_files[@]} -gt 0 ]; then
    local perf_out="${BUILD_DIR}/perf.txt"
    /usr/bin/time -v "$binary" < "${test_files[0]}" > /dev/null 2>"$perf_out" || true
    PEAK_MEM_KB=$(grep "Maximum resident" "$perf_out" 2>/dev/null | awk '{print $NF}' || echo "0")
  fi
  WALL_TIME_MS=$(( $(now_ms) - START_MS ))
  log "       Wall time: ${WALL_TIME_MS}ms, Peak mem: ${PEAK_MEM_KB}KB"
  log "       [OK]"
}

# ============================================================
# TRACK 2: Hardware Peripheral Simulation (Mock / QEMU)
# ============================================================
run_hw() {
  log "[1/4] Locating hardware module source: ${MODULE}"

  local src_file=""
  for ext in c cpp; do
    for dir in "${MODULE_DIR}" "${WORKSPACE}/src" "${WORKSPACE}"; do
      if [ -f "${dir}/${MODULE}.${ext}" ]; then
        src_file="${dir}/${MODULE}.${ext}"
        break 2
      fi
    done
  done

  if [ -z "$src_file" ]; then
    add_error "Source file not found for module: ${MODULE}"
    log "[ERROR] Source not found"
    return 1
  fi
  log "       Source: ${src_file}"
  log "       [OK]"

  if [ "$MOCK" = "true" ]; then
    # ── Mock Mode: compile for x86_64 with -DMOCK_ENV ──
    log "[2/4] Compiling with -DMOCK_ENV (mock sysfs)..."

    # Create mock sysfs tree
    local mock_sysfs="/tmp/mock_sysfs"
    rm -rf "$mock_sysfs"
    mkdir -p "$mock_sysfs/gpio/gpio10" "$mock_sysfs/pwm/pwmchip0/pwm0"
    echo "0" > "$mock_sysfs/gpio/gpio10/value"
    echo "0" > "$mock_sysfs/gpio/gpio10/direction"
    echo "0" > "$mock_sysfs/pwm/pwmchip0/pwm0/duty_cycle"
    echo "0" > "$mock_sysfs/pwm/pwmchip0/pwm0/period"

    local compiler="gcc"
    [[ "$src_file" == *.cpp ]] && compiler="g++"
    local binary="${BUILD_DIR}/${MODULE}_mock"

    if ! $compiler -O2 -Wall -g -DMOCK_ENV -DMOCK_SYSFS_ROOT="\"${mock_sysfs}\"" \
         $GCC_VENDOR_FLAGS -o "$binary" "$src_file" -lm 2>"${BUILD_DIR}/compile.log"; then
      local err
      err=$(head -5 "${BUILD_DIR}/compile.log" | tr '\n' ' ')
      add_error "Mock compilation failed: ${err}"
      log "[ERROR] Compilation failed"
      return 1
    fi
    log "       [OK]"

    log "[3/4] Running mock hardware test..."
    TESTS_TOTAL=$((TESTS_TOTAL + 1))
    local mock_log="${BUILD_DIR}/mock_hw_status.log"

    if timeout 30 "$binary" > "$mock_log" 2>&1; then
      # Compare with expected hardware state
      local expected_log="${MODULE_ASSETS}/expected/hw_state.log"
      if [ -f "$expected_log" ]; then
        if diff -q "$mock_log" "$expected_log" >/dev/null 2>&1; then
          TESTS_PASSED=$((TESTS_PASSED + 1))
          add_test_detail "$(json_test_detail "mock_hw" "pass" "0" "")"
          log "       [PASS] Hardware state matches expected"
        else
          TESTS_FAILED=$((TESTS_FAILED + 1))
          add_test_detail "$(json_test_detail "mock_hw" "fail" "0" "State mismatch")"
          add_error "Mock HW: state log mismatch"
          log "       [FAIL] Hardware state mismatch"
        fi
      else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        add_test_detail "$(json_test_detail "mock_hw" "pass" "0" "No ground truth, exit 0")"
        log "       [PASS] exit 0 (no ground truth)"
      fi
    else
      TESTS_FAILED=$((TESTS_FAILED + 1))
      add_test_detail "$(json_test_detail "mock_hw" "fail" "0" "Runtime crash")"
      add_error "Mock HW: runtime crash"
      log "       [FAIL] Runtime crash"
    fi

    COVERAGE_EXPECTED=1
    COVERAGE_RUN=1

  else
    # ── QEMU Mode: cross-compile and run in emulator ──
    log "[2/4] Cross-compiling for ${PLATFORM}..."

    if ! command -v "$TOOLCHAIN" >/dev/null 2>&1; then
      add_error "Cross-compiler not found: ${TOOLCHAIN}"
      log "[ERROR] ${TOOLCHAIN} not available"
      return 1
    fi

    local binary="${BUILD_DIR}/${MODULE}_cross"
    if ! $TOOLCHAIN -O2 -Wall -static $ARCH_FLAGS $GCC_VENDOR_FLAGS \
         -o "$binary" "$src_file" -lm 2>"${BUILD_DIR}/compile.log"; then
      local err
      err=$(head -5 "${BUILD_DIR}/compile.log" | tr '\n' ' ')
      add_error "Cross-compilation failed: ${err}"
      log "[ERROR] Cross-compilation failed"
      return 1
    fi
    log "       [OK]"

    log "[3/4] Running in QEMU (${QEMU_BIN:-qemu-aarch64-static})..."
    QEMU_USED="true"
    local qemu="${QEMU_BIN:-qemu-aarch64-static}"

    if ! command -v "$qemu" >/dev/null 2>&1; then
      add_error "QEMU not found: ${qemu}"
      log "[ERROR] ${qemu} not available"
      return 1
    fi

    TESTS_TOTAL=$((TESTS_TOTAL + 1))
    local qemu_out="${BUILD_DIR}/qemu_output.log"

    if timeout 60 "$qemu" "$binary" > "$qemu_out" 2>&1; then
      QEMU_EXIT=0
      TESTS_PASSED=$((TESTS_PASSED + 1))
      add_test_detail "$(json_test_detail "qemu_run" "pass" "0" "QEMU exit 0")"
      log "       [PASS] QEMU exit 0"
    else
      QEMU_EXIT=$?
      TESTS_FAILED=$((TESTS_FAILED + 1))
      add_test_detail "$(json_test_detail "qemu_run" "fail" "0" "QEMU exit ${QEMU_EXIT}")"
      add_error "QEMU runtime crash (exit ${QEMU_EXIT})"
      log "       [FAIL] QEMU exit ${QEMU_EXIT}"
    fi

    COVERAGE_EXPECTED=1
    COVERAGE_RUN=1
  fi

  # Valgrind (only for mock mode, x86_64 binary)
  log "[4/4] Memory check..."
  if [ "$MOCK" = "true" ] && command -v valgrind >/dev/null 2>&1; then
    VALGRIND_RAN="true"
    local binary="${BUILD_DIR}/${MODULE}_mock"
    if valgrind --leak-check=full --error-exitcode=99 "$binary" >/dev/null 2>&1; then
      VALGRIND_ERRORS=0
      VALGRIND_SUMMARY="No errors detected"
      log "       [OK] No memory errors"
    else
      VALGRIND_ERRORS=1
      VALGRIND_SUMMARY="Memory errors detected"
      add_error "Valgrind: memory errors in mock binary"
      log "       [WARN] Memory errors detected"
    fi
  else
    log "       [SKIP] (QEMU mode or Valgrind unavailable)"
    VALGRIND_SUMMARY="Skipped"
  fi

  WALL_TIME_MS=$(( $(now_ms) - START_MS ))
}

# ============================================================
# Main execution
# ============================================================
log "============================================"
log "  OmniSight Simulation Runner v1.0"
log "  Track: ${TYPE} | Module: ${MODULE}"
log "  Platform: ${PLATFORM} | Mock: ${MOCK}"
log "============================================"

case "$TYPE" in
  algo) run_algo || true ;;
  hw)   run_hw || true ;;
esac

# ── Coverage check ──
COVERAGE_PCT="0.0"
if [ "$COVERAGE_EXPECTED" -gt 0 ]; then
  COVERAGE_PCT=$(awk "BEGIN {printf \"%.1f\", ($COVERAGE_RUN / $COVERAGE_EXPECTED) * 100}")
fi

if [ "$COVERAGE_CHECK" = "true" ] && [ "$COVERAGE_EXPECTED" -gt 0 ]; then
  if [ "$COVERAGE_RUN" -lt "$COVERAGE_EXPECTED" ]; then
    add_error "Coverage check failed: ran ${COVERAGE_RUN}/${COVERAGE_EXPECTED} test files"
  fi
fi

# ── Determine final status ──
STATUS="pass"
if [ "$TESTS_FAILED" -gt 0 ] || [ -n "$ERRORS" ]; then
  STATUS="fail"
fi

# ── Emit JSON report to stdout ──
cat <<JSONEOF
{
  "version": "1.0",
  "track": "${TYPE}",
  "module": "${MODULE}",
  "platform": "${PLATFORM}",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "status": "${STATUS}",
  "duration_ms": ${WALL_TIME_MS},
  "tests": {
    "total": ${TESTS_TOTAL},
    "passed": ${TESTS_PASSED},
    "failed": ${TESTS_FAILED},
    "details": [${TEST_DETAILS}]
  },
  "coverage": {
    "expected": ${COVERAGE_EXPECTED},
    "run": ${COVERAGE_RUN},
    "percentage": ${COVERAGE_PCT}
  },
  "valgrind": {
    "ran": ${VALGRIND_RAN},
    "errors": ${VALGRIND_ERRORS},
    "definitely_lost": ${VALGRIND_DEFINITELY_LOST:-0},
    "summary": "${VALGRIND_SUMMARY}"
  },
  "performance": {
    "wall_time_ms": ${WALL_TIME_MS},
    "peak_memory_kb": ${PEAK_MEM_KB:-0}
  },
  "qemu": {
    "used": ${QEMU_USED},
    "arch": "${PLATFORM}",
    "exit_code": ${QEMU_EXIT}
  },
  "errors": [${ERRORS}]
}
JSONEOF

log ""
log "============================================"
log "  Status: ${STATUS^^}"
log "  Tests: ${TESTS_PASSED}/${TESTS_TOTAL} passed"
log "  Duration: ${WALL_TIME_MS}ms"
log "============================================"

# Exit with status reflecting test results
[ "$STATUS" = "pass" ] && exit 0 || exit 1
