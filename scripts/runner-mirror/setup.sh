#!/bin/bash
# OP-1136 / Sprint Boreas-C1 — Local bare-mirror setup for runner B+C.
#
# Idempotent. Creates ~/git-mirror/omnisight.git as a bare clone of the
# Gerrit repository. The OP-1137 (Boreas-C2) ephemeral cycle wrapper
# clones from this mirror with `--reference` so per-cycle clone time
# stays at 5-10s instead of 30-60s.
#
# Companion: deploy/systemd/runner-mirror-fetch.timer keeps the mirror
# fresh by fetching every 30 seconds.
#
# Usage:
#   bash scripts/runner-mirror/setup.sh
#
# Re-running is safe: if the mirror already exists, this just verifies
# remote URL + alternates and refreshes via `git remote update`.

set -euo pipefail

MIRROR_DIR="${OMNISIGHT_GIT_MIRROR_DIR:-$HOME/git-mirror/omnisight.git}"
GERRIT_URL="${OMNISIGHT_GERRIT_URL:-ssh://claude-bot@sora.services:29418/omnisight/OmniSight-Productizer}"
SSH_KEY="${OMNISIGHT_GERRIT_SSH_KEY:-$HOME/.config/omnisight/gerrit-claude-bot-ed25519}"

log() { printf '[runner-mirror/setup] %s\n' "$*" >&2; }

if [[ ! -f "$SSH_KEY" ]]; then
    log "ERROR: SSH key not found at $SSH_KEY"
    log "Set OMNISIGHT_GERRIT_SSH_KEY to override."
    exit 2
fi

export GIT_SSH_COMMAND="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"

mkdir -p "$(dirname "$MIRROR_DIR")"

if [[ -d "$MIRROR_DIR" ]] && git -C "$MIRROR_DIR" rev-parse --is-bare-repository >/dev/null 2>&1; then
    log "mirror already exists at $MIRROR_DIR — refreshing"
    # Verify remote URL matches expected; reset if drifted
    cur_url=$(git -C "$MIRROR_DIR" remote get-url origin 2>/dev/null || true)
    if [[ "$cur_url" != "$GERRIT_URL" ]]; then
        log "remote URL drift: was '$cur_url', resetting to '$GERRIT_URL'"
        git -C "$MIRROR_DIR" remote set-url origin "$GERRIT_URL"
    fi
    # Fetch to make sure it's reachable
    git -C "$MIRROR_DIR" remote update --prune
    log "✅ mirror refreshed (HEAD: $(git -C "$MIRROR_DIR" rev-parse refs/remotes/origin/develop 2>/dev/null || git -C "$MIRROR_DIR" rev-parse HEAD))"
    exit 0
fi

log "creating fresh bare mirror at $MIRROR_DIR"
git clone --bare --mirror "$GERRIT_URL" "$MIRROR_DIR"

log "✅ mirror created"
log "  path:   $MIRROR_DIR"
log "  HEAD:   $(git -C "$MIRROR_DIR" rev-parse refs/heads/develop 2>/dev/null || echo '(no develop yet)')"
log "  size:   $(du -sh "$MIRROR_DIR" | cut -f1)"
log ""
log "Next: enable the periodic fetch timer:"
log "  sudo install -m 644 deploy/systemd/runner-mirror-fetch.service /etc/systemd/system/"
log "  sudo install -m 644 deploy/systemd/runner-mirror-fetch.timer   /etc/systemd/system/"
log "  sudo systemctl daemon-reload"
log "  sudo systemctl enable --now runner-mirror-fetch.timer"
