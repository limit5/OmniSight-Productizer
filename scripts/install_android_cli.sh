#!/usr/bin/env bash
# OmniSight — install Google Android CLI on host (P11 #351)
#
# Google's Android CLI (released 2026-04-18) replaces the self-built
# Gradle wrapper path used by backend/mobile_toolchain.py and
# backend/mobile_simulator.py. See: https://d.android.com/tools/agents
#
# This script installs the `android` binary under /opt/android-cli
# with a symlink at ${INSTALL_PREFIX}/bin/android. backend code
# auto-detects presence via ``shutil.which("android")`` (see P11
# fallback semantics) — if absent, the existing ``./gradlew`` path
# remains in use. Installation is therefore OPTIONAL for OmniSight
# to function; it is recommended because Google's benchmark shows
# 3× faster mobile build runs and ~70% lower agent token usage
# vs. the wrapper-based path.
#
# Usage:
#   sudo scripts/install_android_cli.sh
#   # override version / prefix:
#   ANDROID_CLI_VERSION=1.0.1 INSTALL_PREFIX=/usr/local sudo -E scripts/install_android_cli.sh
#
# Environment knobs:
#   ANDROID_CLI_VERSION   release tag (default: 1.0.0)
#   INSTALL_PREFIX        symlink parent (default: /usr/local)
#   ANDROID_CLI_URL       full tarball URL override (for air-gapped
#                         mirrors / pinned CDN); when set the version
#                         knob is ignored for URL construction.
#
# Exits 0 on success, non-zero on any failure. The Docker image build
# (backend/docker/Dockerfile.mobile-build) tolerates download failure
# so CI still green-builds during transient outages, but on host
# install we want explicit failure so the operator can retry.
set -euo pipefail

ANDROID_CLI_VERSION="${ANDROID_CLI_VERSION:-1.0.0}"
INSTALL_PREFIX="${INSTALL_PREFIX:-/usr/local}"
ANDROID_CLI_URL="${ANDROID_CLI_URL:-https://d.android.com/tools/agents/android-cli-${ANDROID_CLI_VERSION}-linux.tar.gz}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/android-cli}"

log() { printf '[install_android_cli] %s\n' "$*" >&2; }

need() {
    command -v "$1" >/dev/null 2>&1 || {
        log "missing required tool: $1"
        exit 1
    }
}
need curl
need tar

# Idempotency — if already installed at the requested version, exit 0.
if command -v android >/dev/null 2>&1; then
    current="$(android --version 2>/dev/null | head -n1 || true)"
    log "android already on PATH: ${current:-unknown}"
    if [[ "${FORCE:-0}" != "1" ]]; then
        log "set FORCE=1 to reinstall; exiting"
        exit 0
    fi
fi

# Must run as root (or via sudo) because we write to /opt and
# ${INSTALL_PREFIX}/bin. Fail fast with a clear message.
if [[ "$(id -u)" -ne 0 ]]; then
    log "this script writes to ${INSTALL_ROOT} and ${INSTALL_PREFIX}/bin; re-run with sudo"
    exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

log "downloading ${ANDROID_CLI_URL}"
if ! curl -fsSLo "${tmp}/android-cli.tar.gz" "${ANDROID_CLI_URL}"; then
    log "download failed from ${ANDROID_CLI_URL}"
    log "check URL availability or set ANDROID_CLI_URL to an internal mirror"
    exit 1
fi

log "extracting to ${INSTALL_ROOT}"
mkdir -p "${INSTALL_ROOT}"
tar -xzf "${tmp}/android-cli.tar.gz" -C "${INSTALL_ROOT}" --strip-components=1

log "linking ${INSTALL_PREFIX}/bin/android -> ${INSTALL_ROOT}/bin/android"
mkdir -p "${INSTALL_PREFIX}/bin"
ln -sf "${INSTALL_ROOT}/bin/android" "${INSTALL_PREFIX}/bin/android"

# Smoke test — if the binary can't print a version the tarball layout
# changed upstream; fail loudly so operator investigates before
# relying on the `shutil.which("android")` path in production.
log "smoke: android --version"
"${INSTALL_PREFIX}/bin/android" --version || {
    log "android --version failed — tarball layout may have shifted"
    exit 1
}

log "done"
