#!/usr/bin/env bash
#
# OmniSight G3 HA-03 — atomic active/standby color switch (TODO row 1354).
#
# This script is the ONE primitive that maintains the blue-green state
# (active color + standby color + symlinked Caddy upstream snippet) so
# that the running proxy can be flipped from one color to the other
# without ever observing a half-written config. It is designed to be:
#
#   (a) Callable standalone for operators who want to flip manually:
#         scripts/bluegreen_switch.sh switch
#   (b) Callable from deploy.sh once the full ceremony (row 1355-1357)
#       is wired: pre-cut smoke on standby → `bluegreen_switch.sh
#       set-active <standby>` → 5 min observe → commit; or roll back
#       with `bluegreen_switch.sh rollback` (second-level fail-back).
#
# Atomicity model:
#   - `active_upstream.caddy` is a symlink. Swapping it uses a tmp-then-
#     rename dance: `ln -s <target> <tmp>` + `mv -Tf <tmp> <symlink>`.
#     On Linux/ext4/xfs `rename(2)` of a symlink within a single
#     directory is atomic: Caddy (or any other consumer) that `readlink`s
#     the symlink sees EITHER the old target or the new target — never
#     a missing file, never a half-written symlink.
#   - `active_color` is a plain file. Same pattern: write to `.tmp.$$`,
#     then `mv -f` into place (rename(2) again → atomic).
#   - `previous_color` records the outgoing color BEFORE the flip so
#     `rollback` is well-defined even if the operation crashes mid-way.
#
# Failure model (ordered ops, single process):
#   1. Write `previous_color` (breadcrumb).
#      → Crash here: no state change, rollback not possible (fine —
#        we haven't touched anything yet).
#   2. Swap the symlink (THE real cutover — Caddy sees new upstream).
#      → Crash here: symlink = new, `active_color` still old.
#        The next `status` call surfaces the mismatch so a human can
#        re-run `set-active <new>` to complete, or fix by hand.
#   3. Write `active_color` (state-of-record reflection).
#      → Crash here: fully consistent; the operation succeeded.
#
# Why symlink first and state-file second: the symlink IS what Caddy
# reads — it's the only load-bearing state. The plain file is a human-
# readable mirror. A crash between (2) and (3) means traffic is already
# on the new color but the mirror says old; `status` flags this.
#
# NEVER edit `active_upstream.caddy` by hand — always go through this
# script so the state-file + symlink stay in sync.
#
# Exit codes:
#   0  — success
#   1  — usage / validation error (operator fixable)
#   2  — state inconsistency detected (e.g. missing state file)
#   3  — I/O failure (e.g. symlink creation failed)

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)

# OMNISIGHT_BLUEGREEN_DIR override lets the contract tests point at a
# sandboxed copy without touching the committed state in the repo.
STATE_DIR="${OMNISIGHT_BLUEGREEN_DIR:-$ROOT/deploy/blue-green}"
ACTIVE_COLOR_FILE="$STATE_DIR/active_color"
PREVIOUS_COLOR_FILE="$STATE_DIR/previous_color"
ACTIVE_SYMLINK="$STATE_DIR/active_upstream.caddy"

VALID_COLORS=("blue" "green")

log() { printf '\033[36m[bluegreen]\033[0m %s\n' "$*"; }
# die <message> [exit-code]
# Only $1 is echoed; $2 (optional, default 1) is the exit status. We
# purposely don't use "$*" because a second positional that's meant as
# the exit code would otherwise leak into the error message.
die() { echo "error: $1" >&2; exit "${2:-1}"; }

is_valid_color() {
	case "$1" in
		blue|green) return 0 ;;
		*) return 1 ;;
	esac
}

other_color() {
	case "$1" in
		blue) echo "green" ;;
		green) echo "blue" ;;
		*) die "invalid color '$1'" 1 ;;
	esac
}

read_active_from_file() {
	if [[ ! -f "$ACTIVE_COLOR_FILE" ]]; then
		die "no active_color state at $ACTIVE_COLOR_FILE — is deploy/blue-green/ initialized?" 2
	fi
	local color
	color=$(tr -d '[:space:]' < "$ACTIVE_COLOR_FILE")
	if ! is_valid_color "$color"; then
		die "invalid active color '$color' in $ACTIVE_COLOR_FILE (expected blue|green)" 2
	fi
	echo "$color"
}

read_symlink_color() {
	# Returns the color implied by the symlink target. If the symlink
	# is missing we echo the special value "(missing)" so `status` can
	# still render a useful diagnostic instead of crashing.
	if [[ ! -L "$ACTIVE_SYMLINK" ]]; then
		echo "(missing)"
		return 0
	fi
	local target
	target=$(readlink "$ACTIVE_SYMLINK")
	case "$target" in
		upstream-blue.caddy) echo "blue" ;;
		upstream-green.caddy) echo "green" ;;
		*) echo "(unknown:$target)" ;;
	esac
}

write_active_atomic() {
	# Atomic plain-file replacement (same directory → same filesystem →
	# rename(2) is atomic, `set -e` propagates any failure).
	local new_color="$1"
	local tmp="${ACTIVE_COLOR_FILE}.tmp.$$"
	printf '%s\n' "$new_color" > "$tmp"
	mv -f "$tmp" "$ACTIVE_COLOR_FILE"
}

write_previous_atomic() {
	local prev="$1"
	local tmp="${PREVIOUS_COLOR_FILE}.tmp.$$"
	printf '%s\n' "$prev" > "$tmp"
	mv -f "$tmp" "$PREVIOUS_COLOR_FILE"
}

relink_atomic() {
	# Atomic symlink replacement:
	#   1. Create the new symlink at a tmp path next to the target
	#      (`ln -s` does NOT silently overwrite when `-f` is omitted
	#      and `mv -Tf` refuses to treat the target as a directory).
	#   2. `mv -Tf` renames it on top of the existing symlink in one
	#      `rename(2)` call — atomic on Linux, POSIX-mandated on any
	#      filesystem that supports rename(2).
	#
	# The `-T` (no-target-directory) flag is critical: without it, if
	# `active_upstream.caddy` were ever mis-created as a directory
	# (operator error), `mv` would move the tmp symlink INTO it instead
	# of replacing it.
	local color="$1"
	local target_basename="upstream-${color}.caddy"
	if [[ ! -f "$STATE_DIR/$target_basename" ]]; then
		die "target snippet '$STATE_DIR/$target_basename' missing — cannot complete switch" 3
	fi
	local tmp="${ACTIVE_SYMLINK}.tmp.$$"
	# Defensive cleanup in case a previous failed run left a tmp behind.
	rm -f "$tmp"
	ln -s "$target_basename" "$tmp"
	mv -Tf "$tmp" "$ACTIVE_SYMLINK"
}

# ──────────────────────────────────────────────────────────────
# Subcommands
# ──────────────────────────────────────────────────────────────

cmd_status() {
	local current
	current=$(read_active_from_file)
	local standby
	standby=$(other_color "$current")
	local symlink_color
	symlink_color=$(read_symlink_color)
	local symlink_target="(missing)"
	if [[ -L "$ACTIVE_SYMLINK" ]]; then
		symlink_target=$(readlink "$ACTIVE_SYMLINK")
	fi
	local previous="(none)"
	if [[ -f "$PREVIOUS_COLOR_FILE" ]]; then
		previous=$(tr -d '[:space:]' < "$PREVIOUS_COLOR_FILE")
	fi
	echo "active=$current"
	echo "standby=$standby"
	echo "symlink_target=$symlink_target"
	echo "symlink_color=$symlink_color"
	echo "previous=$previous"
	# Emit a mismatch warning to stderr (non-fatal) when state file
	# and symlink disagree — usually a crash during the (2)→(3) window.
	if [[ "$symlink_color" != "(missing)" && "$symlink_color" != "$current" ]]; then
		echo "[bluegreen] WARN: state/symlink mismatch (state=$current, symlink=$symlink_color) — re-run set-active to reconcile" >&2
	fi
}

cmd_set_active() {
	local new_color="${1:-}"
	if [[ -z "$new_color" ]]; then
		die "usage: $0 set-active <blue|green>" 1
	fi
	if ! is_valid_color "$new_color"; then
		die "invalid color '$new_color' (expected blue|green)" 1
	fi

	local current
	current=$(read_active_from_file)
	if [[ "$current" == "$new_color" ]]; then
		# Idempotent no-op — but still reconcile the symlink in case a
		# previous crash left it stale (defensive, rare path).
		local symlink_color
		symlink_color=$(read_symlink_color)
		if [[ "$symlink_color" != "$new_color" ]]; then
			log "reconciling stale symlink ($symlink_color → $new_color)"
			relink_atomic "$new_color"
		fi
		log "already active: $current (no-op)"
		return 0
	fi

	log "switching: $current → $new_color"
	write_previous_atomic "$current"   # step 1: breadcrumb
	relink_atomic "$new_color"         # step 2: THE cutover
	write_active_atomic "$new_color"   # step 3: mirror
	log "switched: $current → $new_color (previous=$current)"
}

cmd_switch() {
	local current
	current=$(read_active_from_file)
	local target
	target=$(other_color "$current")
	cmd_set_active "$target"
}

cmd_rollback() {
	if [[ ! -f "$PREVIOUS_COLOR_FILE" ]]; then
		die "no previous_color state — nothing to roll back to (run 'switch' or 'set-active' first)" 2
	fi
	local prev
	prev=$(tr -d '[:space:]' < "$PREVIOUS_COLOR_FILE")
	if ! is_valid_color "$prev"; then
		die "invalid previous color '$prev' in $PREVIOUS_COLOR_FILE" 2
	fi
	log "rolling back to previous: $prev"
	cmd_set_active "$prev"
}

usage() {
	cat <<EOF >&2
usage: $0 {status|switch|set-active <blue|green>|rollback}

  status                — print active/standby/symlink/previous state
  switch                — atomically flip active color (blue ↔ green)
  set-active <color>    — set active color explicitly (idempotent)
  rollback              — switch back to the recorded previous color

State directory: \$OMNISIGHT_BLUEGREEN_DIR (default: $STATE_DIR)

Files maintained:
  active_color          — source-of-record for the active color
  active_upstream.caddy — symlink → upstream-<color>.caddy (Caddy reads this)
  previous_color        — breadcrumb for 'rollback' (written on each switch)
EOF
	exit 1
}

SUBCMD="${1:-}"
if [[ -z "$SUBCMD" ]]; then
	usage
fi
shift || true

case "$SUBCMD" in
	status)     cmd_status ;;
	switch)     cmd_switch ;;
	set-active) cmd_set_active "$@" ;;
	rollback)   cmd_rollback ;;
	-h|--help)  usage ;;
	*)
		echo "error: unknown subcommand: $SUBCMD" >&2
		usage
		;;
esac
