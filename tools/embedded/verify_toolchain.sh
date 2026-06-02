#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: verify_toolchain.sh <catalog-entry-id-or-vendor>

Downloads and installs the resolved cross-toolchain catalog entry, then
cross-compiles and runs a hello.c smoke under qemu-aarch64 or qemu-arm.
EOF
}

die() {
  echo "verify_toolchain.sh: $*" >&2
  exit 1
}
trap 'die "command failed at line $LINENO"' ERR

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
catalog_dir="${OMNISIGHT_TOOLCHAIN_CATALOG_DIR:-$repo_root/configs/embedded_catalog}"
toolchain_root="${OMNISIGHT_TOOLCHAIN_ROOT:-$HOME/.local/share/omnisight/toolchains}"
vendor_id="${1:-}"
cleanup_paths=()

cleanup() {
  if ((${#cleanup_paths[@]} > 0)); then
    rm -rf "${cleanup_paths[@]}"
  fi
}
trap cleanup EXIT

if [[ -z "$vendor_id" || "$vendor_id" == "-h" || "$vendor_id" == "--help" ]]; then
  usage
  [[ -z "$vendor_id" ]] && exit 1 || exit 0
fi

case "$toolchain_root" in
  "$HOME/.local/share/omnisight/toolchains"|"$HOME/.local/share/omnisight/toolchains"/*) ;;
  *) die "OMNISIGHT_TOOLCHAIN_ROOT must stay under $HOME/.local/share/omnisight/toolchains" ;;
esac

need_cmd awk
need_cmd tar

entry="$(
  awk -v wanted="$vendor_id" '
    function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
    function reset() { id=vendor=method=url=triple="" }
    function emit_if_match() {
      if (id == "") return
      if (id == wanted || vendor == wanted) {
        print id "\t" vendor "\t" method "\t" url "\t" triple
        count++
      }
    }
    /^  - id:/ {
      emit_if_match()
      reset()
      id=trim(substr($0, index($0, ":") + 1))
      next
    }
    /^    vendor:/ { vendor=trim(substr($0, index($0, ":") + 1)); next }
    /^    install_method:/ { method=trim(substr($0, index($0, ":") + 1)); next }
    /^    install_url:/ { url=trim(substr($0, index($0, ":") + 1)); next }
    /^      target_triple:/ { triple=trim(substr($0, index($0, ":") + 1)); next }
    END {
      emit_if_match()
      if (count > 1) exit 2
      if (count < 1) exit 3
    }
  ' "$catalog_dir"/*.yaml
)" || case "$?" in
  2) die "catalog selector '$vendor_id' is ambiguous; pass the exact entry id" ;;
  3) die "catalog entry or vendor not found: $vendor_id" ;;
  *) die "failed to parse catalog under $catalog_dir" ;;
esac

IFS=$'\t' read -r entry_id vendor install_method install_url target_triple <<<"$entry"
[[ -n "$target_triple" ]] || die "catalog entry '$entry_id' has no metadata.target_triple"
[[ -n "$install_url" ]] || die "catalog entry '$entry_id' has no install_url"

case "$target_triple" in
  aarch64*) qemu_bin=qemu-aarch64 ;;
  arm-*|armv7*|armhf*|*-gnueabihf) qemu_bin=qemu-arm ;;
  *) die "unsupported target triple for qemu smoke: $target_triple" ;;
esac

need_cmd "$qemu_bin"

entry_root="$toolchain_root/$entry_id"
compiler="$entry_root/bin/$target_triple-gcc"

install_toolchain() {
  mkdir -p "$toolchain_root"
  case "$install_method" in
    vendor_installer)
      need_cmd mktemp
      tmpdir="$(mktemp -d)"
      cleanup_paths+=("$tmpdir")
      archive="$tmpdir/toolchain.tar"
      if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$install_url" -o "$archive"
      elif command -v wget >/dev/null 2>&1; then
        wget -qO "$archive" "$install_url"
      else
        die "required command not found: curl or wget"
      fi
      mkdir -p "$entry_root"
      tar -xf "$archive" -C "$entry_root" --strip-components=1
      ;;
    shell_script)
      die "shell_script install method is catalogued but not safe to run in this host smoke yet: $entry_id"
      ;;
    noop|docker_pull)
      die "install method '$install_method' cannot provide a host cross-compiler: $entry_id"
      ;;
    *)
      die "unsupported install_method '$install_method' for $entry_id"
      ;;
  esac
}

[[ -x "$compiler" ]] || install_toolchain
[[ -x "$compiler" ]] || die "compiler missing after install: $compiler"
sysroot="$("$compiler" --print-sysroot)"

workdir="$(mktemp -d)"
cleanup_paths+=("$workdir")
cat >"$workdir/hello.c" <<'EOF'
#include <stdio.h>
int main(void) {
    puts("omnisight-toolchain-smoke");
    return 0;
}
EOF

"$compiler" "$workdir/hello.c" -o "$workdir/hello"
if [[ -n "$sysroot" && -d "$sysroot" ]]; then
  output="$("$qemu_bin" -L "$sysroot" "$workdir/hello")"
else
  output="$("$qemu_bin" "$workdir/hello")"
fi
[[ "$output" == "omnisight-toolchain-smoke" ]] || die "unexpected qemu output: $output"

echo "verified $entry_id ($target_triple) with $qemu_bin"
